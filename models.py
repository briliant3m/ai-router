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
    company: Optional[str] = None
    extra_comment: Optional[str] = None


class ParsedDeal(BaseModel):
    machine_type_id: Optional[str] = None   # первый/основной тип из equipment_map
    machine_type_ids: List[str] = []         # ВСЕ типы станков из заявки (из equipment_map)
    subcategories: List[str] = []            # подкатегории из equipment_map (может быть несколько)
    budget_rub: Optional[int] = None
    need_days: Optional[int] = None
    is_edm: bool = False
    is_universal: bool = False               # универсальный токарный/фрезерный → РСО
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
    # Для заявок с несколькими станками без единого партнёра: {тип_станка: [имена партнёров]}
    alternatives_by_machine: Optional[Dict[str, List[str]]] = None


class WebhookPayload(BaseModel):
    deal_id: str
