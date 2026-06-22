import logging
from typing import Dict, List, Optional, Tuple

from models import DealFields, EquipmentEntry, ParsedDeal, Partner, RoutingResult

logger = logging.getLogger(__name__)


def _condition_met(eq_value: str, machine_type_name: str) -> bool:
    """Проверяет условие из карты оборудования.
    machine_type_name — название позиции из каталога (не сырой текст оператора).
    """
    val = eq_value.strip().lower()
    if val == "+":
        return True
    if val in ("-", "", "none"):
        return False
    mt = machine_type_name.lower()
    if "только с чпу" in val:
        return "чпу" in mt or "cnc" in mt
    if "только гидравлические" in val:
        return "гидравл" in mt
    if "автоматические" in val:
        return "автомат" in mt
    return True


def _find_equipment_entry(
    equipment_map: List[EquipmentEntry],
    machine_type_id: Optional[str],
) -> Optional[EquipmentEntry]:
    if not machine_type_id:
        return None
    for entry in equipment_map:
        if entry.machine_type == machine_type_id:
            return entry
    return None


def _compute_alternatives(
    machine_ids: List[str],
    partners: List[Partner],
    equipment_map: List[EquipmentEntry],
) -> Dict[str, List[str]]:
    """Для каждого типа станка — список партнёров, которые его принимают (без учёта квоты/срока)."""
    result = {}
    for mid in machine_ids:
        eq_e = _find_equipment_entry(equipment_map, mid)
        if eq_e is None:
            result[mid] = []
            continue
        eligible = []
        for p in partners:
            if p.daily_quota == 0:
                continue
            eq_val = eq_e.partners.get(p.id, "-")
            if _condition_met(eq_val, mid):
                eligible.append(p.name)
        result[mid] = eligible
    return result


def select_partner(
    deal: DealFields,
    parsed: ParsedDeal,
    partners: List[Partner],
    equipment_map: List[EquipmentEntry],
    today_counts: Dict[str, int],
) -> RoutingResult:
    rejection_reasons: Dict[str, str] = {}

    # Срок ≤7 месяцев (≤210 дней) → округляем до 180 дней для повышения шансов маршрутизации
    effective_need_days = parsed.need_days
    if effective_need_days is not None and 180 < effective_need_days <= 210:
        logger.info(f"Deal {deal.id}: срок {parsed.need_days} дн. → 180 дн. (≤7 мес., округляем)")
        effective_need_days = 180

    # ── Приоритет: EDM → Доминик ─────────────────────────────────────────────
    if parsed.is_edm:
        dominik = next((p for p in partners if p.id == "dominik"), None)
        if dominik and dominik.daily_quota > 0:
            dom_count = today_counts.get("dominik", 0)
            if dom_count < dominik.daily_quota:
                logger.info(f"Deal {deal.id}: EDM → Доминик (приоритетный маршрут)")
                return RoutingResult(
                    selected_partner=dominik,
                    rejection_reasons={},
                    parsed_deal=parsed,
                )
            else:
                logger.warning(f"Deal {deal.id}: EDM, Доминик исчерпал квоту ({dom_count}/{dominik.daily_quota}) — обычная маршрутизация")
        else:
            logger.warning(f"Deal {deal.id}: EDM, Доминик на паузе — обычная маршрутизация")


    # ── Определяем все типы станков для проверки ─────────────────────────────
    all_machine_ids = parsed.machine_type_ids if parsed.machine_type_ids else (
        [parsed.machine_type_id] if parsed.machine_type_id else []
    )
    eq_entries: List[Tuple[str, Optional[EquipmentEntry]]] = [
        (mid, _find_equipment_entry(equipment_map, mid)) for mid in all_machine_ids
    ]
    # True — все типы станков отсутствуют в карте (неизвестное оборудование)
    all_not_in_map = bool(eq_entries) and all(e is None for _, e in eq_entries)

    candidates: List[Tuple[Partner, int]] = []

    for partner in partners:
        pid = partner.id

        # ── 1. Пауза ──────────────────────────────────────────────────────────
        if partner.daily_quota == 0:
            rejection_reasons[pid] = "на паузе (квота = 0)"
            continue

        # ── 2. Категория ──────────────────────────────────────────────────────
        if partner.categories and deal.category:
            cat_lower = deal.category.lower()
            if not any(c.lower() in cat_lower for c in partner.categories):
                rejection_reasons[pid] = f"категория «{deal.category}» не в списке партнёра"
                continue

        # ── 3. Карта оборудования — ПРОВЕРЯЕМ ВСЕ ТИПЫ СТАНКОВ ───────────────
        if eq_entries:
            if all_not_in_map:
                # Все типы не в карте — только КЕ через фолбэк (ниже)
                rejection_reasons[pid] = "вид станка не найден в карте оборудования"
                continue
            equipment_blocked = False
            blocked_reasons = []
            for mid, eq_e in eq_entries:
                if eq_e is None:
                    if pid != "ke":
                        blocked_reasons.append(f"«{mid}» не в карте")
                        equipment_blocked = True
                        break
                    # КЕ — неизвестный тип пропускаем (catch-all)
                else:
                    eq_val = eq_e.partners.get(pid, "-")
                    if not _condition_met(eq_val, mid):
                        blocked_reasons.append(f"«{mid}»: {eq_val}")
                        equipment_blocked = True
                        break
            if equipment_blocked:
                rejection_reasons[pid] = "не принимает: " + "; ".join(blocked_reasons)
                continue

        # ── 4. Регион ─────────────────────────────────────────────────────────
        if partner.regions and deal.region:
            region_lower = deal.region.lower()
            region_match = any(
                r.lower() in region_lower or region_lower in r.lower()
                for r in partner.regions
            )
            if not region_match:
                rejection_reasons[pid] = f"регион «{deal.region}» вне географии партнёра"
                continue

        # ── 5. Срок потребности ───────────────────────────────────────────────
        if effective_need_days is not None and partner.max_need_days < 9999:
            if effective_need_days > partner.max_need_days:
                rejection_reasons[pid] = (
                    f"срок {effective_need_days} дн. > макс. {partner.max_need_days} дн."
                )
                continue

        # ── 6. Бюджет (оценка Claude) ─────────────────────────────────────────
        if parsed.budget_rub is not None:
            budget_ok = parsed.eligible_by_budget.get(pid)
            if budget_ok is False:
                rejection_reasons[pid] = "бюджет ниже минимального порога партнёра"
                continue

        # ── 7. Дневная квота ──────────────────────────────────────────────────
        today_count = today_counts.get(pid, 0)
        if today_count >= partner.daily_quota:
            rejection_reasons[pid] = (
                f"дневная квота исчерпана ({today_count}/{partner.daily_quota})"
            )
            continue

        candidates.append((partner, today_count))
        logger.debug(f"Candidate: {pid}, today_count={today_count}")

    if not candidates:
        # ── Фолбэк на Кросс-Экспорт ──────────────────────────────────────────
        # Условия: оборудование не найдено в карте (all_not_in_map)
        # ИЛИ металлообработка без подходящих партнёров (КЕ — catch-all для металла)
        ke = next((p for p in partners if p.id == "ke"), None)
        is_metal_for_ke = bool(
            ke and ke.daily_quota > 0
            and deal.category
            and any(c.lower() in deal.category.lower() for c in ke.categories)
        )
        if all_not_in_map or is_metal_for_ke:
            if ke and ke.daily_quota > 0:
                ke_ok = True
                if ke.categories and deal.category:
                    if not any(c.lower() in deal.category.lower() for c in ke.categories):
                        rejection_reasons["ke"] = f"КЕ (фолбэк): категория «{deal.category}» не подходит"
                        ke_ok = False
                if ke_ok:
                    if effective_need_days is not None and ke.max_need_days < 9999 and effective_need_days > ke.max_need_days:
                        rejection_reasons["ke"] = f"КЕ (фолбэк): срок {effective_need_days} дн. > макс. {ke.max_need_days} дн."
                    else:
                        ke_count = today_counts.get("ke", 0)
                        if ke_count < ke.daily_quota:
                            reason = "оборудование не в карте" if all_not_in_map else "металлообработка, нет других кандидатов"
                            logger.info(f"Deal {deal.id}: КЕ fallback ({reason})")
                            return RoutingResult(
                                selected_partner=ke,
                                rejection_reasons=rejection_reasons,
                                parsed_deal=parsed,
                            )
                        else:
                            rejection_reasons["ke"] = f"КЕ (фолбэк): квота исчерпана ({ke_count}/{ke.daily_quota})"
            else:
                rejection_reasons["ke"] = "КЕ (фолбэк): на паузе"

        # ── Правило: единственный партнёр, blocked только по сроку → всё равно отправляем ──
        timeline_exempt: List[Partner] = []
        for partner in partners:
            pid = partner.id
            if partner.daily_quota == 0:
                continue
            if partner.categories and deal.category:
                if not any(c.lower() in deal.category.lower() for c in partner.categories):
                    continue
            if eq_entries:
                if all_not_in_map:
                    continue  # КЕ-фолбэк уже обработал этот случай
                blocked = False
                for mid, eq_e in eq_entries:
                    if eq_e is None:
                        if pid != "ke":
                            blocked = True
                            break
                    else:
                        if not _condition_met(eq_e.partners.get(pid, "-"), mid):
                            blocked = True
                            break
                if blocked:
                    continue
            if partner.regions and deal.region:
                region_lower = deal.region.lower()
                if not any(r.lower() in region_lower or region_lower in r.lower() for r in partner.regions):
                    continue
            if parsed.budget_rub is not None and parsed.eligible_by_budget.get(pid) is False:
                continue
            today_count = today_counts.get(pid, 0)
            if today_count >= partner.daily_quota:
                continue
            # Партнёр прошёл все фильтры кроме срока — добавляем как кандидата
            timeline_exempt.append(partner)

        if len(timeline_exempt) == 1:
            selected = timeline_exempt[0]
            logger.info(
                f"Deal {deal.id} → {selected.id} "
                f"(единственный кандидат, срок {parsed.need_days} дн. игнорируется)"
            )
            return RoutingResult(
                selected_partner=selected,
                rejection_reasons=rejection_reasons,
                parsed_deal=parsed,
            )

        # Для заявок с несколькими станками — собираем альтернативы
        alternatives = None
        if len(all_machine_ids) > 1:
            alternatives = _compute_alternatives(all_machine_ids, partners, equipment_map)

        logger.warning(f"Deal {deal.id}: no eligible partner found")
        return RoutingResult(
            rejection_reasons=rejection_reasons,
            parsed_deal=parsed,
            alternatives_by_machine=alternatives,
        )

    # Приоритет РСО для универсальных токарных/фрезерных — если РСО прошёл все фильтры
    if parsed.is_universal:
        rso_candidate = next((c for c in candidates if c[0].id == "rso"), None)
        if rso_candidate:
            selected = rso_candidate[0]
            logger.info(f"Deal {deal.id} → РСО (приоритет для универсального станка, прошёл все фильтры)")
            return RoutingResult(
                selected_partner=selected,
                rejection_reasons=rejection_reasons,
                parsed_deal=parsed,
            )

    # Round-robin: выбираем партнёра с наименьшим количеством лидов сегодня.
    candidates.sort(key=lambda x: (x[1], x[0].row_index))
    selected = candidates[0][0]

    logger.info(
        f"Deal {deal.id} → {selected.id} ({selected.name}), "
        f"today_count={candidates[0][1]}, {len(candidates)} candidates"
    )
    return RoutingResult(
        selected_partner=selected,
        rejection_reasons=rejection_reasons,
        parsed_deal=parsed,
    )
