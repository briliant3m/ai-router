# Claude AI-Router — Агент маршрутизации лидов Bitrix24

## Что это

Python-агент, который автоматически анализирует лиды из Bitrix24 (стадия «Лиды Общая») и маршрутизирует их к нужному партнёру: переносит сделку в стадию партнёра и заполняет поле «Подкатегория».

---

## Как это работает (основной флоу)

```
Bitrix24: сделка → стадия "Лиды Общая"
        │
        ▼ webhook POST
FastAPI-сервер (Railway.app)
        │
        ├─ Забирает поля сделки из Bitrix REST API
        │   • Категория (напр. "Металлообработка")
        │   • Вид станка (напр. "Токарный с ЧПУ")  ← свободный текст
        │   • Бюджет (напр. "2 млн примерно")      ← свободный текст
        │   • Срок потребности (напр. "2-3 месяца") ← свободный текст
        │   • Компания
        │   • Регион
        │
        ├─ Загружает условия партнёров из Google Sheets
        │   (кэш 5 минут, обновляется по TTL)
        │
        ├─ Claude API (claude-haiku-4-5) разбирает свободные поля:
        │   бюджет → число в рублях, срок → дни
        │   + нормализует "Вид станка" → "Подкатегория"
        │
        ├─ Фильтрует партнёров по условиям:
        │   регион ✓, категория ✓, бюджет ≥ мин, срок ≤ макс,
        │   дневная квота не исчерпана
        │
        ├─ Среди подходящих выбирает с наибольшей окупаемостью (ROI)
        │
        └─ Bitrix REST API:
            • Обновляет поле "Подкатегория"
            • Перемещает сделку в стадию выбранного партнёра
```

Если ни один партнёр не подошёл — сделка остаётся в «Лиды Общая», в чат добавляется комментарий с причинами отказа каждого партнёра.

---

## Стек технологий

| Компонент | Решение | Почему |
|---|---|---|
| Язык | Python 3.11 | Богатая экосистема, все SDK есть |
| Веб-фреймворк | FastAPI | Быстро, async, автодокументация |
| Хостинг | **Railway.app** | Деплой из GitHub за 5 минут, ~$5/мес, не падает (в отличие от Render free tier) |
| LLM | Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) | Быстро, дёшево, точно разбирает текст |
| Данные партнёров | Google Sheets API v4 | Менеджеры редактируют без разработчика |
| CRM | Bitrix24 REST API (webhook + outgoing) | Нативная интеграция |
| Кэш | In-memory TTL (dict + timestamp) | Без Redis, Railway не требует доп. сервисов |

---

## Google Sheets — структура таблицы партнёров

Лист `partners` (одна строка = один партнёр):

| Поле | Тип | Пример |
|---|---|---|
| `id` | строка | `partner_a` |
| `name` | строка | `ООО Станкоцентр` |
| `bitrix_stage_id` | строка | `C1:NEW` — ID стадии в Bitrix |
| `regions` | через запятую | `Челябинская область, Свердловская область` |
| `categories` | через запятую | `Металлообработка, Деревообработка` |
| `min_budget_rub` | число | `500000` |
| `max_lead_days` | число | `90` (макс срок потребности, дней) |
| `daily_quota` | число | `5` |
| `roi_score` | число 1–100 | `75` (окупаемость, задаёт менеджер) |
| `active` | TRUE/FALSE | `TRUE` |

Лист `daily_counts` (счётчик переданных лидов за сегодня):

| `partner_id` | `date` | `count` |
|---|---|---|
| `partner_a` | `2026-06-10` | `3` |

---

## Структура проекта

```
/
├── main.py              # FastAPI app, webhook endpoint
├── router.py            # Логика выбора партнёра
├── bitrix_client.py     # Bitrix24 REST API
├── sheets_client.py     # Google Sheets API + кэш
├── llm_parser.py        # Claude API: разбор свободного текста
├── models.py            # Pydantic-схемы (DealFields, Partner)
├── config.py            # Настройки из env-переменных
├── requirements.txt
├── Procfile             # Railway: web: uvicorn main:app --host 0.0.0.0 --port $PORT
└── .env.example         # Шаблон переменных окружения
```

---

## Переменные окружения

```env
BITRIX_WEBHOOK_URL=https://your-domain.bitrix24.ru/rest/1/xxxxx/
BITRIX_INCOMING_TOKEN=secret_token   # для валидации входящих webhook от Bitrix
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_SHEETS_ID=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhpI3...
GOOGLE_CREDENTIALS_JSON={"type":"service_account",...}  # base64 или raw JSON
TARGET_STAGE_ID=C1:NEW               # ID стадии "Лиды Общая" в Bitrix
SUBCATEGORY_FIELD_ID=UF_CRM_XXXXX    # ID пользовательского поля "Подкатегория"
LOG_LEVEL=INFO
```

---

## Ключевые решения

### Парсинг свободного текста (llm_parser.py)
Claude получает все свободные поля и возвращает JSON:
```json
{
  "budget_rub": 2000000,
  "need_days": 75,
  "subcategory": "Токарный станок с ЧПУ",
  "confidence": "high"
}
```
Используется `claude-haiku-4-5` с температурой 0 — детерминированный разбор.

### Выбор партнёра (router.py)
1. Фильтр: регион ∈ partner.regions
2. Фильтр: категория ∈ partner.categories
3. Фильтр: budget_rub ≥ partner.min_budget_rub
4. Фильтр: need_days ≤ partner.max_lead_days
5. Фильтр: today_count < partner.daily_quota
6. Сортировка по `roi_score` DESC → берём первого

### Обновление счётчика квоты
После успешной маршрутизации — инкремент в листе `daily_counts`.
Если записи за сегодня нет — создаём новую строку.

### Валидация webhook
Входящий запрос от Bitrix содержит токен в теле (поле `auth[application_token]`).
Сверяем с `BITRIX_INCOMING_TOKEN` из env — если не совпадает, возвращаем 403.

---

## Деплой на Railway.app (шаги после написания кода)

1. `git init && git push` репозитория на GitHub
2. Зайти на railway.app → New Project → Deploy from GitHub Repo
3. Добавить переменные окружения в Railway dashboard
4. Railway автоматически подхватит `Procfile` и запустит сервер
5. Скопировать URL сервиса (напр. `https://ai-router.up.railway.app`)
6. В Bitrix24: Настройки → Входящий webhook → указать этот URL
7. В автоматизации воронки: при смене стадии на «Лиды Общая» → вызвать webhook

---

## Что НЕ входит в скоуп (чтобы не усложнять)

- UI-панель для просмотра истории маршрутизации (логи Railway достаточно)
- Авто-перезапуск при изменении Google Sheets структуры (менеджер должен следить за форматом)
- Очередь/retry при падении Bitrix (Railway перезапустит сервис, Bitrix повторяет webhook 3 раза)
