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
    extra_comment: Optional[str] = None,
) -> ParsedDeal:
    import calendar
    from datetime import date as _date

    today = _date.today()

    def _days(d: _date) -> int:
        return max(1, (d - today).days)

    # Конец текущего квартала
    qm = ((today.month - 1) // 3 + 1) * 3
    if qm > 12:
        qm = 12
    days_to_quarter_end = _days(_date(today.year, qm, calendar.monthrange(today.year, qm)[1]))
    days_to_year_end = _days(_date(today.year, 12, 31))

    # Начало каждого месяца — ближайшие 15 месяцев вперёд
    _mru_gen = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
                "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    _months_ref_lines = []
    for i in range(1, 16):
        mm = (today.month - 1 + i) % 12 + 1
        yy = today.year + (today.month - 1 + i) // 12
        d = _days(_date(yy, mm, 1))
        label = f"до {_mru_gen[mm]}" + (f" {yy}" if yy != today.year else "")
        _months_ref_lines.append(f"  {label}: {d} дн.")
    months_ref = "\n".join(_months_ref_lines)

    # Ближайшие сезоны
    _season_starts = {"лета": (6, 1), "осени": (9, 1), "зимы": (12, 1), "весны": (3, 1)}
    _season_lines = []
    for name, (sm, sd) in _season_starts.items():
        sy = today.year
        target = _date(sy, sm, sd)
        if target <= today:
            target = _date(sy + 1, sm, sd)
        _season_lines.append(f"  до {name}: {_days(target)} дн.")
    seasons_ref = "\n".join(_season_lines)

    active_partner_ids = [p.id for p in partners if p.daily_quota != 0]

    eligible_placeholder = ",\n    ".join(
        f'"{pid}": true или false или null' for pid in active_partner_ids
    )

    user_prompt = f"""\
СЕГОДНЯ: {today.strftime("%d.%m.%Y")}
СПРАВОЧНИК ДАТ (используй при разборе срока потребности):
  до конца квартала: {days_to_quarter_end} дн.
  до конца года: {days_to_year_end} дн.
{months_ref}
{seasons_ref}

ЗАЯВКА:
- Категория: {category or "не указана"}
- Вид станка: {machine_type or "не указан"}
- Бюджет: {budget_text or "не указан"}
- Срок потребности: {timeline_text or "не указан"}
- Регион: {region or "не указан"}
- Доп. комментарий оператора: {extra_comment or "не указан"}

---
КАТАЛОГ ОБОРУДОВАНИЯ (найди наиболее подходящую позицию для «Вид станка» — по смыслу, не обязательно дословно):
{_catalog_text(equipment_map)}

---
БЮДЖЕТНЫЕ УСЛОВИЯ ПАРТНЁРОВ:
{_partner_conditions_text(partners)}

---
Верни JSON строго следующей структуры:
{{
  "machine_type_ids": ["тип1 из каталога", "тип2 из каталога"],
  "machine_type_id": "первый/основной тип из machine_type_ids или null",
  "subcategories": ["подкатегория1", "подкатегория2"],
  "budget_rub": число_или_null,
  "need_days": число_или_null,
  "is_edm": true_или_false,
  "is_universal": true_или_false,
  "eligible_by_budget": {{
    {eligible_placeholder}
  }},
  "parse_notes": "важные наблюдения или пустая строка"
}}

ПРАВИЛА:
- machine_type_ids: СПИСОК всех типов станков из заявки — каждый как точное название из каталога. Если клиент упомянул несколько станков — включи каждый. Пустой массив [] только если вообще ничего не распознано. КРИТИЧНО: если оператор упомянул несколько станков — список должен содержать ВСЕ, не только первый.
- machine_type_id: первый элемент из machine_type_ids. null если machine_type_ids пустой.
- subcategories: массив подкатегорий из каталога для ВСЕХ упомянутых видов станков. Пустой массив [] если ничего не нашёл.
- budget_rub: "2 млн" → 2000000, "700 тыс" → 700000, "примерно 1.5 млн" → 1500000. Число без единицы (например "80000") — считай рублями. Если совсем неизвестно — null (не 0).
- eligible_by_budget: ВАЖНО — если budget_rub известен и ЯВНО НИЖЕ минимального порога партнёра для данного типа станка — ОБЯЗАТЕЛЬНО верни false, не null. null только когда бюджет вообще неизвестен.
- need_days: число дней от сегодня до момента потребности. Используй СПРАВОЧНИК ДАТ выше. Правила разбора:
    • Диапазоны — бери ВЕРХНЮЮ границу: "2–3 мес" → 90, "3–4 мес" → 120, "4–6 мес" → 180, "1–2 года" → 730
    • Месяцы: "1 мес" / "месяц" / "через месяц" → 30, "2 мес" → 60, "3 мес" / "квартал" → 90, "полгода" / "6 мес" → 180, "год" → 365
    • Недели: "неделя" → 7, "2 недели" / "пара недель" → 14, "месяц-полтора" → 45
    • Срочно: "срочно" / "как можно скорее" / "ASAP" / "чем раньше тем лучше" → 14
    • Скоро: "в ближайшее время" / "скоро" / "в ближайшие месяцы" → 30
    • Дата-ориентир (смотри СПРАВОЧНИК ДАТ):
        "до конца квартала" / "в этом квартале" → {days_to_quarter_end}
        "до конца года" / "в этом году" / "до нового года" / "к новому году" → {days_to_year_end}
        "до [месяц]" / "к [месяц]" / "в [месяц]" → соответствующее значение из справочника
        "до лета/осени/зимы/весны" / "летом/осенью/зимой/весной" → из справочника сезонов
        "в следующем году" / "в следующем квартале" → значение из справочника + 90/365
    • Неопределённость: "не знает" / "не сказал" / "не определился" / "не указан" / "без ограничений" / "не горит" / "не срочно" / "когда-нибудь" → null
    • Любой другой текст — рассуждай по смыслу как человек, используй СПРАВОЧНИК ДАТ как точку отсчёта. Даже если формулировка нестандартная — попробуй оценить срок. null только если из текста вообще невозможно понять даже порядок (дни/недели/месяцы/годы).
- is_edm: true если станок электроэрозионный / EDM / проволочный / эрозия / прожиг отверстий.
- is_universal: true если станок УНИВЕРСАЛЬНЫЙ токарный или УНИВЕРСАЛЬНЫЙ фрезерный (без ЧПУ, ручное управление). Примеры: "токарный универсальный", "универсально-фрезерный", "фрезерный универсальный", "токарно-винторезный универсальный". НЕ путать с ЧПУ-станками.
- Доп. комментарий оператора учитывай при определении machine_type_id, subcategories, budget_rub, need_days и is_edm — там может уточняться тип станка, бюджет или срок. Отражай важные наблюдения в parse_notes.
- ВАЖНО: анализируй ВСЕ поля заявки в совокупности. Если «Вид станка» размытый — уточняй по категории и доп. комментарию. Каждое поле влияет на маршрутизацию.
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
        machine_type_ids = data.get("machine_type_ids") or []
        machine_type_id = data.get("machine_type_id") or (machine_type_ids[0] if machine_type_ids else None)
        return ParsedDeal(
            machine_type_id=machine_type_id,
            machine_type_ids=machine_type_ids,
            subcategories=data.get("subcategories") or [],
            budget_rub=data.get("budget_rub"),
            need_days=data.get("need_days"),
            is_edm=bool(data.get("is_edm", False)),
            is_universal=bool(data.get("is_universal", False)),
            eligible_by_budget=data.get("eligible_by_budget", {}),
            parse_notes=data.get("parse_notes"),
        )
    except Exception as e:
        logger.error(f"Claude parse failed: {e}")
        return ParsedDeal()
