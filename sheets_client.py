import json
import logging
import time
from datetime import date
from typing import Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

from config import get_settings
from models import EquipmentEntry, Partner

logger = logging.getLogger(__name__)
settings = get_settings()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

_gc: Optional[gspread.Client] = None
_cache: Dict[str, object] = {}
_cache_time: Dict[str, float] = {}


def _client() -> gspread.Client:
    global _gc
    if _gc is None:
        creds_dict = json.loads(settings.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        _gc = gspread.authorize(creds)
    return _gc


def _cached(key: str, fetch_fn):
    now = time.time()
    if key not in _cache or now - _cache_time.get(key, 0) > settings.SHEETS_CACHE_TTL:
        _cache[key] = fetch_fn()
        _cache_time[key] = now
    return _cache[key]


def _fetch_partners() -> List[Partner]:
    sheet = _client().open_by_key(settings.GOOGLE_SHEETS_ID)
    rows = sheet.worksheet("partners").get_all_records()
    result = []
    for i, row in enumerate(rows):
        if not row.get("id"):
            continue
        if str(row.get("active", "")).upper() != "TRUE":
            continue
        result.append(Partner(
            id=str(row["id"]),
            name=str(row.get("name", "")),
            bitrix_stage_id=str(row.get("bitrix_stage_id", "")),
            categories=[c.strip() for c in str(row.get("category", "")).split(",") if c.strip()],
            regions=[r.strip() for r in str(row.get("regions", "")).split(",") if r.strip()],
            max_need_days=int(row.get("max_need_days", 9999)),
            daily_quota=int(row.get("daily_quota", 0)),
            roi_score=int(row.get("roi_score", 50)),
            active=True,
            budget_conditions=str(row.get("budget_conditions", "")),
            special_notes=str(row.get("special_notes", "")),
            row_index=i,
        ))
    logger.info(f"Loaded {len(result)} active partners from Sheets")
    return result


def _fetch_equipment_map() -> List[EquipmentEntry]:
    sheet = _client().open_by_key(settings.GOOGLE_SHEETS_ID)
    rows = sheet.worksheet("equipment_map").get_all_records()
    result = []
    for row in rows:
        if not row.get("machine_type"):
            continue
        category = str(row.get("category", ""))
        stankoinkom_val = str(row.get("stankoinkom", "-"))

        # Станкоинком имеет два партнёра (металл и дерево) в одной колонке.
        # Разделяем по категории оборудования в таблице.
        if category in ("Деревообработка", "Другое"):
            sk_metal, sk_wood = "-", stankoinkom_val
        else:
            sk_metal, sk_wood = stankoinkom_val, "-"

        result.append(EquipmentEntry(
            category=category,
            subcategory=str(row.get("subcategory", "")),
            machine_type=str(row.get("machine_type", "")),
            partners={
                "ke": str(row.get("ke", "-")),
                "lidermash": str(row.get("lidermash", "-")),
                "stankoinkom_metal": sk_metal,
                "stankoinkom_wood": sk_wood,
                "dominik": str(row.get("dominik", "-")),
                "topstanki": str(row.get("topstanki", "-")),
                "sps_techno": str(row.get("sps_techno", "-")),
                "rso": str(row.get("rso", "-")),
            },
        ))
    logger.info(f"Loaded {len(result)} equipment map entries from Sheets")
    return result


def get_partners() -> List[Partner]:
    return _cached("partners", _fetch_partners)


def get_equipment_map() -> List[EquipmentEntry]:
    return _cached("equipment_map", _fetch_equipment_map)


def get_today_counts() -> Dict[str, int]:
    """Всегда читает свежо — без кэша, чтобы не было двойных отправок."""
    try:
        sheet = _client().open_by_key(settings.GOOGLE_SHEETS_ID)
        rows = sheet.worksheet("daily_counts").get_all_records()
        today = str(date.today())
        return {
            str(row["partner_id"]): int(row.get("count", 0))
            for row in rows
            if str(row.get("date", "")) == today and row.get("partner_id")
        }
    except Exception as e:
        logger.error(f"Failed to read daily_counts: {e}")
        return {}


def increment_count(partner_id: str) -> None:
    try:
        sheet = _client().open_by_key(settings.GOOGLE_SHEETS_ID)
        ws = sheet.worksheet("daily_counts")
        all_rows = ws.get_all_values()
        today = str(date.today())

        if not all_rows:
            ws.append_row([partner_id, today, 1])
            return

        headers = all_rows[0]
        try:
            id_col = headers.index("partner_id")
            date_col = headers.index("date")
            count_col = headers.index("count")
        except ValueError:
            logger.error("daily_counts: missing required column headers")
            return

        for row_i, row in enumerate(all_rows[1:], start=2):
            if len(row) > max(id_col, date_col, count_col):
                if row[id_col] == partner_id and row[date_col] == today:
                    new_count = int(row[count_col] or 0) + 1
                    ws.update_cell(row_i, count_col + 1, new_count)
                    logger.info(f"daily_counts: {partner_id} → {new_count} on {today}")
                    return

        ws.append_row([partner_id, today, 1])
        logger.info(f"daily_counts: new row {partner_id} = 1 on {today}")
    except Exception as e:
        logger.error(f"Failed to increment count for {partner_id}: {e}")
