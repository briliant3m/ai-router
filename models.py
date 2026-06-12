from typing import Dict, List, Optional
from pydantic import BaseModel


class DealFields(BaseModel):
    id: str
    title: Optional[str] = None
    category: Optional[str] = None
    machine_type: Optional[str] = None
    budget_text: Optional[str] = None
    timeline_text: Optional[str] = None
    region: Optional[str] = None


class ParsedDeal(BaseModel):
    machine_type_id: Optional[str] = None   # точное название из equipment_map
    subcategory: Optional[str] = None        # подкатегория из equipment_map
    budget_rub: Optional[int] = None
    need_days: Optional[int] = None
    is_edm: bool = False
    # partner_id -> True/False/None (None = бюджет неизвестен, потенциально подходит)
    eligible_by_budget: Dict[str, Optional[bool]] = {}
    parse_notes: Optional[str] = None


class Partner(BaseModel):
    id: str
    name: str
    bitrix_stage_id: str
    categories: List[str]
    regions: List[str]         # пусто = вся Россия
    max_need_days: int
    daily_quota: int            # 0 = пауза
    roi_score: int
    active: bool
    budget_conditions: str
    special_notes: str
    row_index: int              # порядок строки в таблице (для приоритизации)


class EquipmentEntry(BaseModel):
    category: str
    subcategory: str
    machine_type: str
    # partner_id -> значение: "+", "-", "только с ЧПУ", "только гидравлические", etc.
    partners: Dict[str, str]


class RoutingResult(BaseModel):
    selected_partner: Optional[Partner] = None
    rejection_reasons: Dict[str, str] = {}
    parsed_deal: Optional[ParsedDeal] = None


class WebhookPayload(BaseModel):
    deal_id: str
