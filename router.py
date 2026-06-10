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
        if eq_entry:
            eq_val = eq_entry.partners.get(pid, "-")
            if not _condition_met(eq_val, deal.machine_type or ""):
                rejection_reasons[pid] = f"вид станка не принимается (условие: {eq_val})"
                continue
        # Если eq_entry не найден (Claude не смог сопоставить) — не блокируем по карте,
        # другие фильтры (бюджет, категория) сработают.

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
        budget_ok = parsed.eligible_by_budget.get(pid)
        if budget_ok is False:
            rejection_reasons[pid] = "бюджет ниже минимального порога партнёра"
            continue
        # None = бюджет неизвестен → продолжаем (потенциально подходит)

        # ── 7. Дневная квота ──────────────────────────────────────────────────
        today_count = today_counts.get(pid, 0)
        if today_count >= partner.daily_quota:
            rejection_reasons[pid] = (
                f"дневная квота исчерпана ({today_count}/{partner.daily_quota})"
            )
            continue

        fill_ratio = today_count / partner.daily_quota
        candidates.append((partner, fill_ratio))
        logger.debug(f"Candidate: {pid}, fill_ratio={fill_ratio:.3f}")

    if not candidates:
        logger.warning(f"Deal {deal.id}: no eligible partner found")
        return RoutingResult(rejection_reasons=rejection_reasons, parsed_deal=parsed)

    # Выбираем партнёра с наименьшим fill_ratio (пропорциональное распределение).
    # При равенстве — по порядку строки в таблице (row_index).
    candidates.sort(key=lambda x: (round(x[1], 4), x[0].row_index))
    selected = candidates[0][0]

    logger.info(
        f"Deal {deal.id} → {selected.id} ({selected.name}), "
        f"fill_ratio={candidates[0][1]:.3f}, {len(candidates)} candidates"
    )
    return RoutingResult(
        selected_partner=selected,
        rejection_reasons=rejection_reasons,
        parsed_deal=parsed,
    )
