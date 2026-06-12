import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

import bitrix_client
import llm_parser
import router
import sheets_client
import telegram_client
from config import get_settings
from models import DealFields

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(title="Bitrix AI Router")


@app.get("/health")
def health():
    return {"status": "ok"}


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
        deal = DealFields(
            id=deal_id,
            title=raw.get("TITLE"),
            category=raw.get(settings.FIELD_CATEGORY),
            machine_type=raw.get(settings.FIELD_MACHINE_TYPE),
            budget_text=raw.get(settings.FIELD_BUDGET),
            timeline_text=raw.get(settings.FIELD_TIMELINE),
            region=raw.get(settings.FIELD_REGION),
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
            partners=partners,
            equipment_map=equipment_map,
        )
        logger.info(
            f"Parsed: subcategory={parsed.subcategory!r}, "
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

            update_fields = {"STAGE_ID": partner.bitrix_stage_id}
            if parsed.subcategory:
                update_fields[settings.FIELD_SUBCATEGORY] = parsed.subcategory

            bitrix_client.update_deal(deal_id, update_fields)
            sheets_client.increment_count(partner.id)

            today_total = sum(today_counts.values()) + 1
            telegram_client.notify_routed(
                deal_id=deal_id,
                deal_title=deal.title or "",
                partner_name=partner.name,
                subcategory=parsed.subcategory,
                budget_rub=parsed.budget_rub,
                need_days=parsed.need_days,
                region=deal.region,
                today_total=today_total,
            )

            parts = [f"Лид передан партнёру: {partner.name}"]
            if parsed.subcategory:
                parts.append(f"Подкатегория: {parsed.subcategory}")
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
            if parsed.subcategory:
                lines.append(f"Вид станка (нормализован): {parsed.subcategory}")
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
                subcategory=parsed.subcategory,
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
