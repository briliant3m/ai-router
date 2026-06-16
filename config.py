from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Bitrix24 — исходящий REST (для API-вызовов от агента к Bitrix)
    BITRIX_WEBHOOK_URL: str  # https://YOUR.bitrix24.ru/rest/1/TOKEN/

    # Секретный токен, который агент проверяет в ?token=... при входящем webhook
    BITRIX_INCOMING_TOKEN: str

    # ID стадии «Лиды Общая» — триггер для запуска агента
    TARGET_STAGE_ID: str  # например C12:NEW

    # ID пользовательских полей сделки (UF_CRM_XXXXXXXX)
    FIELD_CATEGORY: str
    FIELD_MACHINE_TYPE: str
    FIELD_BUDGET: str
    FIELD_TIMELINE: str
    FIELD_REGION: str
    FIELD_SUBCATEGORY: str  # поле, куда агент записывает нормализованную подкатегорию
    FIELD_COMPANY: str = "UF_CRM_1781532376767"  # Станки | Компания (на контакте)
    FIELD_COMMENT: str = "UF_CRM_1781556999369"  # Станки | Доп-коммент (на контакте)

    # Anthropic
    ANTHROPIC_API_KEY: str
    CLAUDE_MODEL: str = "claude-haiku-4-5-20251001"

    # Google Sheets
    GOOGLE_SHEETS_ID: str  # ID таблицы из URL: /spreadsheets/d/{ID}/
    GOOGLE_CREDENTIALS_JSON: str  # JSON service account как строка

    # TTL кэша данных из Sheets (секунды)
    SHEETS_CACHE_TTL: int = 300

    # Если True — анализирует лиды, но не трогает Bitrix; результат пишется в лист dry_run_results
    DRY_RUN: bool = False

    # Telegram-уведомления (необязательные)
    TG_TOKEN: str = ""
    TG_CHAT: str = ""
    TG_USER: str = ""  # например @username — тегается при нераспределённом лиде

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()
