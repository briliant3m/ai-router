import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

import bitrix_client
import llm_parser
import router
import sheets_client
from config import get_settings
from models import DealFields, WebhookPayload

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


@app.post("/webhook")
def handle_webhook(payload: WebhookPayload, token: str = Query(None)):
    # ── Проверка токена ────────────────────────────────────────────────────────
    if token != settings.BITRIX_INCOMING_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    deal_id = payload.deal_id.strip()
    if not deal_id:
        raise HTTPException(status_code=400, detail="deal_id is required")

    logger.info(f"=== Processing deal {deal_id} ===")

    try:
        # ── 1. Получаем данные сделки из Bitrix ───────────────────────────────
        raw = bitrix_client.get_deal(deal_id)
        deal = DealFields(
            id=deal_id,
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
            logger.warning(f"Deal {deal_id}: no partner found. Reasons: {result.rejection_reasons}")
            return JSONResponse({"status": "no_partner", "reasons": result.rejection_reasons})

    except Exception as e:
        logger.exception(f"Error processing deal {deal_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
