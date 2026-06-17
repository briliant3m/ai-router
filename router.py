import logging
from typing import Dict, List, Optional, Tuple

from models import DealFields, EquipmentEntry, ParsedDeal, Partner, RoutingResult

logger = logging.getLogger(__name__)


def _condition_met(eq_value: str, machine_type_raw: str) -> bool:
    """Проверяет, выполняется ли условие партнёра из карты оборудования."""
    val = eq_value.strip().lower()
    if val == "+":
        return True
    if val in ("-", "", "none"):
        return False
    # Условные значения — проверяем по тексту вида станка от оператора
    mt = (machine_type_raw or "").lower()
    if "только с чпу" in val:
        return "чпу" in mt or "cnc" in mt
    if "только гидравлические" in val:
        return "гидравл" in mt
    if "автоматические" in val:
        return "автомат" in mt
    # Любое другое условие — считаем что условие выполнено (перестраховка)
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


def select_partner(
    deal: DealFields,
    parsed: ParsedDeal,
    partners: List[Partner],
    equipment_map: List[EquipmentEntry],
    today_counts: Dict[str, int],
) -> RoutingResult:
    rejection_reasons: Dict[str, str] = {}

    # ── Приоритет: EDM → Доминик ─────────────────────────────────────────────
    # Электроэрозионные станки всегда идут к Доминику, независимо от категории
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

    eq_entry = _find_equipment_entry(equipment_map, parsed.machine_type_id)

    candidates: List[Tuple[Partner, float]] = []

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

        # ── 3. Карта оборудования ─────────────────────────────────────────────
        if eq_entry is not None:
            eq_val = eq_entry.partners.get(pid, "-")
            if not _condition_met(eq_val, deal.machine_type or ""):
                rejection_reasons[pid] = f"вид станка не принимается (условие: {eq_val})"
                continue
        else:
            # Тип станка не найден в карте — ни один обычный партнёр не получает лид,
            # только КЕ через фолбэк (ниже)
            rejection_reasons[pid] = "вид станка не найден в карте оборудования"
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
        if parsed.need_days is not None and partner.max_need_days < 9999:
            if parsed.need_days > partner.max_need_days:
                rejection_reasons[pid] = (
                    f"срок {parsed.need_days} дн. > макс. {partner.max_need_days} дн."
                )
                continue

        # ── 6. Бюджет (оценка Claude) ─────────────────────────────────────────
        # Если бюджет не указан совсем — не фильтруем, передаём всем подходящим
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
        # Только если тип станка вообще не найден в карте оборудования.
        # Если в карте есть запись с КЕ = "-" — КЕ тоже не получает.
        if eq_entry is None:
            ke = next((p for p in partners if p.id == "ke"), None)
            if ke and ke.daily_quota > 0:
                # Фолбэк всё равно соблюдает срок потребности
                if parsed.need_days is not None and ke.max_need_days < 9999 and parsed.need_days > ke.max_need_days:
                    rejection_reasons["ke"] = f"КЕ (фолбэк): срок {parsed.need_days} дн. > макс. {ke.max_need_days} дн."
                else:
                    ke_count = today_counts.get("ke", 0)
                    if ke_count < ke.daily_quota:
                        logger.info(f"Deal {deal.id}: КЕ fallback (оборудование не в карте)")
                        return RoutingResult(
                            selected_partner=ke,
                            rejection_reasons=rejection_reasons,
                            parsed_deal=parsed,
                        )
                    else:
                        rejection_reasons["ke"] = f"КЕ (фолбэк): квота исчерпана ({ke_count}/{ke.daily_quota})"
            else:
                rejection_reasons["ke"] = "КЕ (фолбэк): на паузе"

        logger.warning(f"Deal {deal.id}: no eligible partner found")
        return RoutingResult(rejection_reasons=rejection_reasons, parsed_deal=parsed)

    # Round-robin: выбираем партнёра с наименьшим количеством лидов сегодня.
    # При равенстве — по порядку строки в таблице (row_index).
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
