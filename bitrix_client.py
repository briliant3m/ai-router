import logging
from typing import Any
import requests
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _call(method: str, params: dict) -> Any:
    url = f"{settings.BITRIX_WEBHOOK_URL.rstrip('/')}/{method}"
    resp = requests.post(url, json=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Bitrix API error [{method}]: {data['error']} — {data.get('error_description', '')}")
    return data.get("result", data)


def get_deal(deal_id: str) -> dict:
    return _call("crm.deal.get", {"id": deal_id})


def update_deal(deal_id: str, fields: dict) -> None:
    _call("crm.deal.update", {"id": deal_id, "fields": fields})
    logger.info(f"Deal {deal_id} updated: {list(fields.keys())}")


def add_timeline_comment(deal_id: str, comment: str) -> None:
    try:
        _call("crm.timeline.comment.add", {
            "fields": {
                "ENTITY_ID": deal_id,
                "ENTITY_TYPE": "deal",
                "COMMENT": comment,
            }
        })
    except Exception as e:
        logger.warning(f"Could not add timeline comment to deal {deal_id}: {e}")
