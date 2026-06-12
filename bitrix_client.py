import logging
from typing import Any
import requests
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _call(method: str, params: dict) -> Any:
    url = f"{settings.BITRIX_WEBHOOK_URL.strip().rstrip('/')}/{method}"
    resp = requests.post(url, json=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Bitrix API error [{method}]: {data['error']} — {data.get('error_description', '')}")
    return data.get("result", data)


def get_deal(deal_id: str) -> dict:
    return _call("crm.deal.get", {"id": deal_id})


def get_contact(contact_id: str) -> dict:
    return _call("crm.contact.get", {"id": contact_id})


def get_company(company_id: str) -> dict:
    return _call("crm.company.get", {"id": company_id})


def get_deal_enriched(deal_id: str) -> dict:
    """Возвращает поля сделки + UF_CRM_ поля из связанного контакта и компании.

    Приоритет: сделка > контакт > компания.
    """
    deal = get_deal(deal_id)

    # Собираем UF-поля из компании (наименьший приоритет)
    merged_uf: dict = {}
    company_id = deal.get("COMPANY_ID")
    if company_id:
        try:
            company = get_company(str(company_id))
            merged_uf.update({k: v for k, v in company.items() if k.startswith("UF_")})
        except Exception as e:
            logger.warning(f"Could not fetch company {company_id}: {e}")

    # UF-поля из контакта (перекрывают компанию)
    contact_id = deal.get("CONTACT_ID")
    if contact_id:
        try:
            contact = get_contact(str(contact_id))
            merged_uf.update({k: v for k, v in contact.items() if k.startswith("UF_")})
        except Exception as e:
            logger.warning(f"Could not fetch contact {contact_id}: {e}")

    # Поля сделки перекрывают всё
    merged_uf.update(deal)
    return merged_uf


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
