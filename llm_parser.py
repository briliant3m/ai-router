import json
import logging
from typing import List, Optional

import anthropic

from config import get_settings
from models import EquipmentEntry, ParsedDeal, Partner

logger = logging.getLogger(__name__)
settings = get_settings()

_client: Optional[anthropic.Anthropic] = None

SYSTEM_PROMPT = """\
Ты — парсер заявок на промышленное оборудование (станки) для системы маршрутизации лидов.
Извлеки структурированные данные из заявки оператора колл-центра и определи, каким партнёрам подходит этот лид по бюджету.
Отвечай ТОЛЬКО валидным JSON без пояснений и без markdown-блоков кода.\
"""


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def _catalog_text(equipment_map: List[EquipmentEntry]) -> str:
    lines = []
    for e in equipment_map:
        lines.append(f"• {e.machine_type}  [подкатегория: {e.subcategory}, категория: {e.category}]")
    return "\n".join(lines)


def _partner_conditions_text(partners: List[Partner]) -> str:
    lines = []
    for p in partners:
        if p.daily_quota == 0:
            continue  # на паузе — бюджет не проверяем
        lines.append(f"{p.id} ({p.name}): {p.budget_conditions}")
    return "\n".join(lines)


def parse_deal(
    category: Optional[str],
    machine_type: Optional[str],
    budget_text: Optional[str],
    timeline_text: Optional[str],
    region: Optional[str],
    partners: List[Partner],
    equipment_map: List[EquipmentEntry],
) -> ParsedDeal:
    active_partner_ids = [p.id for p in partners if p.daily_quota != 0]

    eligible_placeholder = ",\n    ".join(
        f'"{pid}": true или false или null' for pid in active_partner_ids
    )

    user_prompt = f"""\
ЗАЯВКА:
- Категория: {category or "не указана"}
- Вид станка: {machine_type or "не указан"}
- Бюджет: {budget_text or "не указан"}
- Срок потребности: {timeline_text or "не указан"}
- Регион: {region or "не указан"}

---
КАТАЛОГ ОБОРУДОВАНИЯ (найди точное совпадение для «Вид станка»):
{_catalog_text(equipment_map)}

---
БЮДЖЕТНЫЕ УСЛОВИЯ ПАРТНЁРОВ:
{_partner_conditions_text(partners)}

---
Верни JSON строго следующей структуры:
{{
  "machine_type_id": "точное название из каталога выше или null если не нашёл",
  "subcategory": "подкатегория из каталога или null",
  "budget_rub": число_или_null,
  "need_days": число_или_null,
  "is_edm": true_или_false,
  "eligible_by_budget": {{
    {eligible_placeholder}
  }},
  "parse_notes": "важные наблюдения или пустая строка"
}}

ПРАВИЛА:
- budget_rub: "2 млн" → 2000000, "700 тыс" → 700000, "примерно 1.5 млн" → 1500000. Если совсем неизвестно — null (не 0).
- need_days: "2–3 месяца" → 75, "6 мес" → 180, "в ближайшее время" → 30, "без ограничений" → null.
- is_edm: true если станок электроэрозионный / EDM / проволочный / эрозия / прожиг отверстий.
- eligible_by_budget:
    true  — бюджет из заявки точно покрывает минимальный порог партнёра для данного типа станка
    false — бюджет явно ниже порога
    null  — бюджет неизвестен или неоднозначен (партнёр потенциально подходит)\
"""

    try:
        msg = _get_client().messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = msg.content[0].text.strip()

        # Убираем markdown-блок если Claude всё же добавил
        if raw.startswith("```"):
            raw = raw.strip("`").lstrip("json").strip()

        data = json.loads(raw)
        return ParsedDeal(
            machine_type_id=data.get("machine_type_id"),
            subcategory=data.get("subcategory"),
            budget_rub=data.get("budget_rub"),
            need_days=data.get("need_days"),
            is_edm=bool(data.get("is_edm", False)),
            eligible_by_budget=data.get("eligible_by_budget", {}),
            parse_notes=data.get("parse_notes"),
        )
    except Exception as e:
        logger.error(f"Claude parse failed: {e}")
        # Возвращаем пустой результат — агент добавит комментарий в Bitrix
        return ParsedDeal()
