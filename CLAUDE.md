# FinanceBot — CLAUDE.md

## Project overview
Telegram bot for personal finance tracking via receipt photos.
User sends a photo of a receipt → Claude Vision parses it → data is stored in PostgreSQL → user can query statistics.

## Tech stack
- **Language**: Python 3.12
- **Telegram framework**: aiogram 3.x (async)
- **Database**: PostgreSQL 16 + SQLAlchemy 2 (async) + Alembic
- **Cache / FSM state**: Redis 7
- **AI parsing**: Anthropic Claude Vision API (`claude-sonnet-4-20250514`)
- **Currency rates**: exchangerate-api.com (free tier, cached in Redis 1h)
- **Charts**: matplotlib
- **Excel export**: openpyxl
- **Config**: pydantic-settings + .env
- **Deploy**: Docker Compose

## Project structure
```
financebot/
├── CLAUDE.md
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── alembic.ini
├── alembic/
│   └── versions/
├── bot/
│   ├── __init__.py
│   ├── main.py              # entry point, dp + bot setup
│   ├── config.py            # pydantic-settings Config
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── receipt.py       # photo handler → parse → save
│   │   ├── stats.py         # /stats week|month|year
│   │   ├── budget.py        # /budget set|show
│   │   ├── manual.py        # /add manual entry
│   │   └── common.py        # /start, /help, /cancel
│   ├── services/
│   │   ├── __init__.py
│   │   ├── vision.py        # Claude Vision API call
│   │   ├── currency.py      # currency conversion, Redis cache
│   │   ├── stats.py         # aggregation queries
│   │   ├── budget.py        # budget check logic
│   │   └── charts.py        # matplotlib chart generator
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py        # async engine + session factory
│   │   ├── models.py        # SQLAlchemy ORM models
│   │   └── crud.py          # all DB operations
│   ├── keyboards/
│   │   ├── __init__.py
│   │   └── inline.py        # inline keyboards
│   └── utils/
│       ├── __init__.py
│       ├── validators.py    # validate parsed JSON from Claude
│       └── formatters.py    # format numbers, dates, currencies
└── tests/
    └── test_vision.py
```

## Database schema
```
receipts: id, user_id, store, date, currency, total, total_pln, photo_file_id, created_at
items: id, receipt_id, name, quantity, unit_price, total_price, category
budgets: id, user_id, category, limit_pln, month (YYYY-MM)
```

### Categories (fixed enum)
`groceries | cafe | pharmacy | transport | electronics | clothing | household | other`

## Claude Vision — system prompt
```
You are a receipt parser. Extract all data from the receipt image and return ONLY valid JSON, no markdown, no explanation.
Rules:
- currency: detect from symbols/context (PLN, EUR, USD, CZK, BYR)
- category: one of [groceries, cafe, pharmacy, transport, electronics, clothing, household, other]
- quantity default is 1 if not shown
- If you cannot read a value, use null
- total must equal sum of all items × quantity
```

### JSON schema to return
```json
{
  "store": "string or null",
  "date": "YYYY-MM-DD or null",
  "currency": "PLN",
  "total": 0.00,
  "items": [
    {
      "name": "string",
      "quantity": 1,
      "unit_price": 0.00,
      "total_price": 0.00,
      "category": "groceries"
    }
  ]
}
```

## Key rules for Claude Code
- Always use **async/await** everywhere (aiogram 3 is fully async)
- All DB calls go through `crud.py` — no raw SQL in handlers
- Parsed receipt JSON must be validated in `validators.py` before saving
- Currency conversion: always store original currency + PLN equivalent
- Redis TTL for currency rates: 3600 seconds
- Never hardcode API keys — always read from `config.py` (pydantic-settings)
- All error responses to user must be friendly Russian text
- Log all Claude Vision API errors to stderr with full traceback

## Bot commands
```
/start     — welcome message
/help      — list of commands
/stats     — show stats menu (week / month / year)
/budget    — budget management
/add       — manual expense entry
/export    — export to Excel
/cancel    — cancel current operation
```

## Environment variables (.env)
```
BOT_TOKEN=
ANTHROPIC_API_KEY=
DATABASE_URL=postgresql+asyncpg://user:pass@db:5432/financebot
REDIS_URL=redis://redis:6379/0
EXCHANGE_API_KEY=
```
