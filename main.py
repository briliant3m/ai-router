import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

import bitrix_client
import llm_parser
import router
import sheets_client
import telegram_client
from config import get_settings
from models import DealFields, Partner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()

# ── Мониторинг изменений в Google Sheets ──────────────────────────────────────

_TRACKED_FIELDS = ["budget_conditions", "special_notes", "daily_quota", "max_need_days"]
_partners_snapshot: Optional[Dict[str, dict]] = None


def _partner_snapshot(p: Partner) -> dict:
    return {
        "budget_conditions": p.budget_conditions,
        "special_notes": p.special_notes,
        "daily_quota": p.daily_quota,
        "max_need_days": p.max_need_days,
        "regions": sorted(p.regions),
        "categories": sorted(p.categories),
    }


def _check_and_notify() -> list:
    global _partners_snapshot

    partners = sheets_client.get_partners_fresh()
    current: Dict[str, dict] = {p.id: _partner_snapshot(p) for p in partners}
    current_names: Dict[str, str] = {p.id: p.name for p in partners}

    if _partners_snapshot is None:
        _partners_snapshot = current
        logger.info("Partners snapshot initialized")
        return []

    changes = []

    # Новые партнёры
    for pid in current:
        if pid not in _partners_snapshot:
            changes.append(f"➕ <b>{current_names[pid]}</b> — добавлен")

    # Удалённые партнёры
    for pid in _partners_snapshot:
        if pid not in current:
            changes.append(f"➖ <b>{pid}</b> — удалён")

    # Изменения у существующих
    labels = {
        "budget_conditions": "Бюджетные условия",
        "special_notes": "Примечания",
        "daily_quota": "Дневная квота",
        "max_need_days": "Макс. срок (дней)",
        "regions": "Регионы",
        "categories": "Категории",
    }
    for pid, snap in current.items():
        if pid not in _partners_snapshot:
            continue
        old = _partners_snapshot[pid]
        partner_changes = []
        for field, label in labels.items():
            if snap[field] != old[field]:
                partner_changes.append(f"  • {label}: <i>{old[field]}</i> → <i>{snap[field]}</i>")
        if partner_changes:
            changes.append(f"\n<b>{current_names[pid]}</b>:")
            changes.extend(partner_changes)

    _partners_snapshot = current

    if changes:
        logger.info(f"Sheets changes detected: {len(changes)} items")
        telegram_client.notify_sheets_changed(changes)
    else:
        logger.info("Sheets check: no changes")

    return changes


async def _monitor_loop():
    await asyncio.sleep(60)  # дать серверу время запуститься
    while True:
        try:
            _check_and_notify()
        except Exception as e:
            logger.error(f"Sheets monitor error: {e}")
        await asyncio.sleep(600)  # 10 минут


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_monitor_loop())
    yield
    task.cancel()


app = FastAPI(title="Bitrix AI Router", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/check-changes")
def check_changes(token: str = Query(None)):
    if token != settings.BITRIX_INCOMING_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    changes = _check_and_notify()
    return {"changes_found": len(changes) > 0, "changes": changes}


@app.get("/debug-deal/{deal_id}")
def debug_deal(deal_id: str):
    """Показывает все UF_CRM поля сделки + связанного контакта/компании."""
    deal_raw = bitrix_client.get_deal(deal_id)
    enriched = bitrix_client.get_deal_enriched(deal_id)

    deal_uf = {k: v for k, v in deal_raw.items() if k.startswith("UF_") and v not in ("", None, False, "0")}
    enriched_uf = {k: v for k, v in enriched.items() if k.startswith("UF_") and v not in ("", None, False, "0")}

    contact_id = deal_raw.get("CONTACT_ID")
    company_id = deal_raw.get("COMPANY_ID")

    return {
        "deal_id": deal_id,
        "title": deal_raw.get("TITLE"),
        "contact_id": contact_id,
        "company_id": company_id,
        "non_empty_uf_from_deal_only": deal_uf,
        "non_empty_uf_enriched": enriched_uf,
        "configured_fields": {
            "FIELD_CATEGORY": settings.FIELD_CATEGORY,
            "FIELD_MACHINE_TYPE": settings.FIELD_MACHINE_TYPE,
            "FIELD_BUDGET": settings.FIELD_BUDGET,
            "FIELD_TIMELINE": settings.FIELD_TIMELINE,
            "FIELD_REGION": settings.FIELD_REGION,
        },
        "values_resolved": {
            "category": enriched.get(settings.FIELD_CATEGORY),
            "machine_type": enriched.get(settings.FIELD_MACHINE_TYPE),
            "budget": enriched.get(settings.FIELD_BUDGET),
            "timeline": enriched.get(settings.FIELD_TIMELINE),
            "region": enriched.get(settings.FIELD_REGION),
        },
    }


@app.get("/debug-partners")
def debug_partners():
    """Показывает всех активных партнёров с их условиями маршрутизации."""
    partners = sheets_client.get_partners()
    equipment_map = sheets_client.get_equipment_map()
    today_counts = sheets_client.get_today_counts()
    return {
        "partners": [
            {
                "id": p.id,
                "name": p.name,
                "bitrix_stage_id": p.bitrix_stage_id,
                "categories": p.categories,
                "regions": p.regions,
                "max_need_days": p.max_need_days,
                "daily_quota": p.daily_quota,
                "roi_score": p.roi_score,
                "budget_conditions": p.budget_conditions,
                "special_notes": p.special_notes,
                "today_count": today_counts.get(p.id, 0),
            }
            for p in partners
        ],
        "equipment_map_count": len(equipment_map),
        "equipment_map_sample": [
            {"category": e.category, "subcategory": e.subcategory, "machine_type": e.machine_type, "partners": e.partners}
            for e in equipment_map[:10]
        ],
    }


@app.get("/debug-enums")
def debug_enums():
    """Показывает enum-значения для поля FIELD_CATEGORY из crm.contact.fields."""
    raw_fields = bitrix_client._call("crm.contact.fields", {})
    field_id = settings.FIELD_CATEGORY
    field_def = raw_fields.get(field_id, "NOT FOUND")
    sample = {k: v for i, (k, v) in enumerate(raw_fields.items()) if k.startswith("UF_") and i < 5}
    return {
        "field_id": field_id,
        "field_def": field_def,
        "sample_uf_fields": sample,
        "enum_cache": bitrix_client._get_contact_enums().get(field_id, "EMPTY"),
    }


@app.get("/debug-contact-fields")
def debug_contact_fields(search: str = Query(None)):
    """Возвращает все UF-поля контакта с их названиями. ?search=доп для фильтрации."""
    raw_fields = bitrix_client._call("crm.contact.fields", {})
    result = {}
    for fid, fdef in raw_fields.items():
        if not fid.startswith("UF_"):
            continue
        if not isinstance(fdef, dict):
            continue
        label = fdef.get("formLabel") or fdef.get("listLabel") or fdef.get("filterLabel") or ""
        if search and search.lower() not in (fid + label).lower():
            continue
        result[fid] = {
            "label": label,
            "type": fdef.get("type"),
        }
    return {"total": len(result), "fields": result}


@app.get("/debug-subcategory-field")
def debug_subcategory_field():
    """Диагностика поля Подкатегория — ищет ID enum-значений всеми доступными методами."""
    field_id = settings.FIELD_SUBCATEGORY

    # 1. crm.deal.fields без categoryId
    deal_fields_all = bitrix_client._call("crm.deal.fields", {})
    in_deal_fields = field_id in deal_fields_all
    deal_field_def = deal_fields_all.get(field_id)

    # 2. crm.deal.fields с categoryId=12 (воронка C12)
    try:
        deal_fields_cat = bitrix_client._call("crm.deal.fields", {"categoryId": 12})
        in_deal_fields_cat12 = field_id in deal_fields_cat
        deal_field_cat_def = deal_fields_cat.get(field_id)
    except Exception as e:
        in_deal_fields_cat12 = f"error: {e}"
        deal_field_cat_def = None

    # 3. Все UF поля сделки категории 12 с "LIST" в ключах (ищем поля-списки)
    uf_list_fields = {
        k: v for k, v in (deal_fields_cat if isinstance(deal_fields_cat, dict) else {}).items()
        if k.startswith("UF_") and isinstance(v, dict) and v.get("USER_TYPE_ID") == "enumeration"
    }

    # 4. crm.deal.get на тестовой сделке — смотрим сырое значение поля
    try:
        raw_deal = bitrix_client._call("crm.deal.get", {"id": "1350028"})
        field_raw_value = raw_deal.get(field_id, "KEY_NOT_PRESENT_IN_DEAL")
        all_uf_keys = [k for k in raw_deal if k.startswith("UF_")]
    except Exception as e:
        field_raw_value = f"error: {e}"
        all_uf_keys = []

    return {
        "field_id": field_id,
        "in_crm_deal_fields": in_deal_fields,
        "in_crm_deal_fields_cat12": in_deal_fields_cat12,
        "deal_field_definition": deal_field_cat_def or deal_field_def,
        "list_type_uf_fields_in_cat12": list(uf_list_fields.keys()),
        "raw_value_in_deal_1350028": field_raw_value,
        "all_uf_keys_in_deal_1350028": all_uf_keys,
    }


@app.get("/debug")
def debug():
    import json
    result = {}
    s = get_settings()

    result["sheets_id"] = s.GOOGLE_SHEETS_ID[:20] + "..." if s.GOOGLE_SHEETS_ID else "NOT SET"
    result["field_category"] = s.FIELD_CATEGORY
    result["field_machine"] = s.FIELD_MACHINE_TYPE
    result["target_stage"] = s.TARGET_STAGE_ID

    try:
        creds_raw = s.GOOGLE_CREDENTIALS_JSON.strip()
        creds = json.loads(creds_raw)
        result["creds_type"] = creds.get("type")
        result["creds_email"] = creds.get("client_email")
    except Exception as e:
        result["creds_error"] = str(e)

    try:
        partners = sheets_client.get_partners()
        result["sheets_ok"] = True
        result["partners_count"] = len(partners)
    except Exception as e:
        result["sheets_ok"] = False
        result["sheets_error"] = str(e)

    return result


@app.post("/webhook")
def handle_webhook(token: str = Query(None), deal_id: str = Query(None)):
    # ── Проверка токена ────────────────────────────────────────────────────────
    if token != settings.BITRIX_INCOMING_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    if not deal_id:
        raise HTTPException(status_code=400, detail="deal_id is required")
    deal_id = deal_id.strip()

    logger.info(f"=== Processing deal {deal_id} ===")

    try:
        # ── 1. Получаем данные сделки + UF-поля из контакта/компании ─────────
        raw = bitrix_client.get_deal_enriched(deal_id)

        # Идемпотентность: если сделка уже ушла из целевой стадии — пропускаем
        # (защита от повторных Bitrix-ретраев при рестарте сервера)
        current_stage = raw.get("STAGE_ID")
        if current_stage != settings.TARGET_STAGE_ID:
            logger.info(f"Deal {deal_id} is on stage {current_stage!r}, not target {settings.TARGET_STAGE_ID!r} — skipping")
            return JSONResponse({"status": "skipped", "reason": "already_processed", "stage": current_stage})
        contact_id = raw.get("CONTACT_ID")
        deal = DealFields(
            id=deal_id,
            title=raw.get("TITLE"),
            category=raw.get(settings.FIELD_CATEGORY),
            machine_type=raw.get(settings.FIELD_MACHINE_TYPE),
            budget_text=raw.get(settings.FIELD_BUDGET),
            timeline_text=raw.get(settings.FIELD_TIMELINE),
            region=raw.get(settings.FIELD_REGION),
            company=raw.get(settings.FIELD_COMPANY),
            extra_comment=raw.get(settings.FIELD_COMMENT),
        )
        logger.info(
            f"Deal {deal_id}: category={deal.category!r}, "
            f"machine={deal.machine_type!r}, budget={deal.budget_text!r}, "
            f"timeline={deal.timeline_text!r}, region={deal.region!r}"
        )

        # ── 2. Загружаем данные из Google Sheets (кэш 5 мин) ─────────────────
        partners = sheets_client.get_partners()
        equipment_map = sheets_client.get_equipment_map()
        today_counts = sheets_client.get_today_counts()

        # ── 3. Claude разбирает свободный текст + проверяет бюджет ────────────
        parsed = llm_parser.parse_deal(
            category=deal.category,
            machine_type=deal.machine_type,
            budget_text=deal.budget_text,
            timeline_text=deal.timeline_text,
            region=deal.region,
            extra_comment=deal.extra_comment,
            partners=partners,
            equipment_map=equipment_map,
        )
        logger.info(
            f"Parsed: subcategories={parsed.subcategories!r}, "
            f"budget_rub={parsed.budget_rub}, need_days={parsed.need_days}, "
            f"is_edm={parsed.is_edm}"
        )

        # ── 4. Маршрутизация ──────────────────────────────────────────────────
        result = router.select_partner(
            deal=deal,
            parsed=parsed,
            partners=partners,
            equipment_map=equipment_map,
            today_counts=today_counts,
        )

        # ── 5. Применяем результат ────────────────────────────────────────────
        if result.selected_partner:
            partner = result.selected_partner

            # Стадию обновляем на сделке
            bitrix_client.update_deal(deal_id, {"STAGE_ID": partner.bitrix_stage_id})

            # Подкатегорию пишем в контакт (поле создано на сущности Контакт)
            if parsed.subcategories and contact_id:
                try:
                    ids = bitrix_client.subcategory_values_to_ids(
                        settings.FIELD_SUBCATEGORY, parsed.subcategories
                    )
                    bitrix_client.update_contact(
                        str(contact_id),
                        {settings.FIELD_SUBCATEGORY: ids},
                    )
                    logger.info(f"Contact {contact_id} subcategory set: {ids}")
                except Exception as e:
                    logger.warning(f"Could not update contact subcategory: {e}")
            sheets_client.increment_count(partner.id)

            # Читаем свежие счётчики после инкремента — для точного итога
            fresh_counts = sheets_client.get_today_counts()
            today_total = sum(fresh_counts.values())
            telegram_client.notify_routed(
                deal_id=deal_id,
                deal_title=deal.title or "",
                partner_name=partner.name,
                category=deal.category,
                machine_type=deal.machine_type,
                company=deal.company,
                subcategories=parsed.subcategories,
                budget_rub=parsed.budget_rub,
                need_days=parsed.need_days,
                region=deal.region,
                today_total=today_total,
            )

            parts = [f"Лид передан партнёру: {partner.name}"]
            if parsed.subcategories:
                parts.append(f"Подкатегория: {', '.join(parsed.subcategories)}")
            if parsed.budget_rub:
                parts.append(f"Распознанный бюджет: {parsed.budget_rub:,} руб.")
            if parsed.need_days:
                parts.append(f"Срок потребности: {parsed.need_days} дней")
            if parsed.parse_notes:
                parts.append(f"Примечания: {parsed.parse_notes}")
            bitrix_client.add_timeline_comment(deal_id, "🤖 AI Роутер\n" + "\n".join(parts))

            logger.info(f"Deal {deal_id} successfully routed to {partner.id}")
            return JSONResponse({"status": "routed", "partner_id": partner.id, "partner_name": partner.name})

        else:
            # Партнёр не найден — оставляем сделку в текущей стадии, добавляем комментарий
            lines = ["Подходящий партнёр не найден.\n"]
            if parsed.subcategories:
                lines.append(f"Вид станка (нормализован): {', '.join(parsed.subcategories)}")
            if parsed.budget_rub:
                lines.append(f"Распознанный бюджет: {parsed.budget_rub:,} руб.")
            if parsed.need_days:
                lines.append(f"Срок: {parsed.need_days} дней")
            lines.append("\nПричины отказа:")
            for pid, reason in result.rejection_reasons.items():
                name = next((p.name for p in partners if p.id == pid), pid)
                lines.append(f"• {name}: {reason}")
            if parsed.parse_notes:
                lines.append(f"\nПримечания парсера: {parsed.parse_notes}")

            bitrix_client.add_timeline_comment(deal_id, "🤖 AI Роутер\n" + "\n".join(lines))

            partner_names = {p.id: p.name for p in partners}
            telegram_client.notify_no_partner(
                deal_id=deal_id,
                deal_title=deal.title or "",
                category=deal.category,
                machine_type=deal.machine_type,
                company=deal.company,
                subcategories=parsed.subcategories,
                budget_rub=parsed.budget_rub,
                region=deal.region,
                rejection_reasons=result.rejection_reasons,
                partner_names=partner_names,
            )

            logger.warning(f"Deal {deal_id}: no partner found. Reasons: {result.rejection_reasons}")
            return JSONResponse({"status": "no_partner", "reasons": result.rejection_reasons})

    except Exception as e:
        logger.exception(f"Error processing deal {deal_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
