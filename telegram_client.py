import logging
from typing import Dict, Optional

import requests

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _send(text: str) -> None:
    token = settings.TG_TOKEN.strip()
    chat_id = settings.TG_CHAT.strip()
    if not token or not chat_id:
        logger.warning(f"Telegram skipped: token={'set' if token else 'EMPTY'}, chat_id={'set' if chat_id else 'EMPTY'}")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Telegram notification sent")
    except Exception as e:
        logger.warning(f"Telegram notification failed: {e}")


def notify_routed(
    deal_id: str,
    deal_title: str,
    partner_name: str,
    subcategories: list,
    budget_rub: Optional[int],
    need_days: Optional[int],
    region: Optional[str],
    today_total: int,
) -> None:
    lines = [
        "✅ <b>Лид распределён</b>",
        f"Сделка: #{deal_id}  {deal_title or ''}".strip(),
    ]
    if subcategories:
        lines.append(f"Вид станка: {', '.join(subcategories)}")
    if region:
        lines.append(f"Регион: {region}")
    if budget_rub:
        lines.append(f"Бюджет: {budget_rub:,} руб.".replace(",", " "))
    if need_days:
        lines.append(f"Срок: {need_days} дн.")
    lines.append(f"➡️ Партнёр: <b>{partner_name}</b>")
    lines.append(f"📊 Всего за сегодня: {today_total} лид(а/ов)")
    _send("\n".join(lines))


def notify_sheets_changed(changes: list) -> None:
    lines = ["📋 <b>Изменения в таблице партнёров</b>", ""]
    lines.extend(changes)
    _send("\n".join(lines))


def notify_no_partner(
    deal_id: str,
    deal_title: str,
    subcategories: list,
    budget_rub: Optional[int],
    region: Optional[str],
    rejection_reasons: Dict[str, str],
    partner_names: Dict[str, str],
) -> None:
    alert_user = settings.TG_USER.strip()
    lines = [
        "⚠️ <b>Лид не распределён!</b>",
    ]
    if alert_user:
        lines.append(f"{alert_user}, нужно ручное распределение")
    lines.append("")
    lines.append(f"Сделка: #{deal_id}  {deal_title or ''}".strip())
    if subcategories:
        lines.append(f"Вид станка: {', '.join(subcategories)}")
    if region:
        lines.append(f"Регион: {region}")
    if budget_rub:
        lines.append(f"Бюджет: {budget_rub:,} руб.".replace(",", " "))
    else:
        lines.append("Бюджет: не указан")
    lines.append("")
    lines.append("Причины отказа:")
    for pid, reason in rejection_reasons.items():
        name = partner_names.get(pid, pid)
        lines.append(f"• {name}: {reason}")
    _send("\n".join(lines))
