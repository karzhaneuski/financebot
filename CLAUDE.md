# FinanceBot — CLAUDE.md

## Project overview
Telegram bot for personal finance tracking via receipt photos.
User sends a photo of a receipt → the vision LLM (Gemini by default, or Claude) parses it → data is stored in PostgreSQL → user can query statistics.

## Tech stack
- **Language**: Python 3.12
- **Telegram framework**: aiogram 3.x (async)
- **Database**: PostgreSQL 18 + SQLAlchemy 2 (async) + Alembic
- **Cache / FSM state**: Redis 7
- **AI parsing**: provider abstraction `bot/services/llm.py` — Google Gemini
  (default, `google-genai`, structured output, `GEMINI_MODEL`) or Anthropic
  Claude (`claude-sonnet-5` vision, Haiku for search and item-name
  normalization); `LLM_PROVIDER=gemini|anthropic`
- **Currency rates**: exchangerate-api.com (free tier, cached in Redis 1h)
- **Charts**: matplotlib
- **Excel export**: openpyxl
- **Config**: pydantic-settings + .env
- **Deploy**: Docker Compose on an Oracle Cloud arm64 VM (`docker-compose.prod.yml`, see DEPLOY.md); Redis as a compose service

## Project structure
```
financebot/
├── CLAUDE.md
├── .env.example
├── docker-compose.yml       # local development
├── docker-compose.prod.yml  # production (db, migrate, bot, api)
├── DEPLOY.md
├── scripts/                 # restore.sh, backup.sh
├── deploy/cron/             # backup cron entry
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
│   │   ├── vision.py        # receipt / bank-screenshot recognition (prompts + JSON schemas)
│   │   ├── llm.py           # LLM provider abstraction (gemini | anthropic)
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
receipts: id, user_id, store, date, currency, total, total_pln, personal_total_pln,
          photo_file_id, tx_type, source, category, created_at
items: id, receipt_id, name, normalized_name, quantity, unit_price, total_price,
       category, volume_ml, is_personal
budgets: id, user_id, category, limit_pln, month (YYYY-MM), last_notified_pct
users: user_id (PK, Telegram id), language (ru|en|pl), created_at
```
`items.unit_price` / `items.total_price` are in the **receipt's currency**
(`receipts.currency`) for every source. Any item-based sum (categories,
budgets, products, Wrapped, reports, API, export) must go through
`crud._item_pln_col()` (Python: `Receipt.to_pln()`), which converts with the
receipt's own rate `total_pln / total` — never sum `Item.total_price` raw.

`receipts.personal_total_pln` and `items.is_personal` are both nullable —
NULL means "never split", which must behave exactly like the pre-/split
schema (see "Personal totals & splitting" below).

### Categories (fixed enum)
`groceries | cafe | pharmacy | transport | electronics | clothing | household | housing | entertainment | health | subscriptions | other`

## Vision — system prompt (shared by all providers)
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
- Receipt total = the model's "Suma PLN" value. If it is null/0 while the
  items sum is > 0, `validate_receipt` uses the items sum (warning logged,
  `total_from_items=True`); if it differs from the items sum,
  `total_mismatch=True` and the user sees the mismatch warning. Multi-page
  PDF receipts send every page (up to 5) to the model — the total is often
  on the last page.
- Currency conversion: always store original currency + PLN equivalent
- Redis TTL for currency rates: 3600 seconds
- Never hardcode API keys — always read from `config.py` (pydantic-settings)
- All user-facing text goes through gettext (`from bot.i18n import _, __, ngettext`);
  error responses must be friendly text in the user's language
- Category names come only from `bot.categories.category_label()`; store
  categories as enum keys, never as translated names
- Log all vision provider errors to stderr with full traceback
- LLM calls go through `bot.services.llm.get_provider().generate_json()`
  with a JSON schema matching the prompt's format; never call an SDK
  directly. Quota/balance/rate-limit/outage errors raise
  `LLMUnavailableError` → users see `vision_unavailable_text()`
  (bot/handlers/receipt.py, gettext), not a generic error

## Bot commands
```
/start          — welcome message
/help           — list of commands
/stats          — show stats menu (week / month / year)
/subscriptions  — detected recurring subscriptions
/budget         — budget management
/add            — manual expense entry
/export         — export to Excel
/reports        — daily/weekly/monthly report settings
/search, /find  — natural-language transaction search (LLM parser + regex fallback)
/wrapped        — year-in-review summary image
/split          — split a receipt's items between personal/not-personal
/language       — change the bot language (ru / en / pl)
/cancel         — cancel current operation
/reset          — reset FSM state (requires "confirm" arg)
```

## Personal totals & splitting (`/split`)
- `items.is_personal` marks individual line items as mine (`True`) vs not
  mine (`False`) after running `/split` on a receipt. NULL = item was never
  touched by `/split` and counts as fully personal (pre-split behaviour).
- **Category aggregates are all-or-nothing**, not proportional:
  `get_spending_by_category*` filter on `COALESCE(items.is_personal, TRUE)`
  — an item counts in full toward its category or not at all.
- **Receipt-level personal sums** (totals, income/expenses, daily, by-store,
  by-currency, cash withdrawals) use `COALESCE(receipts.personal_total_pln,
  receipts.total_pln)` (`crud._personal_pln_col()`).
- `crud.set_item_personal_flags()` derives `personal_total_pln` from the
  flagged items' share of the receipt's **native**-currency total
  (`my_native / receipt.total`, clamped to `[0, 1]`), multiplied by the
  receipt's already-stored `total_pln` — it reuses the FX rate baked in at
  parse time and never re-fetches one. Item sums exceeding the receipt total
  (discount-line quirks) simply clamp to 100% personal rather than erroring.
- Product-stats functions and Excel export stay household-level by design —
  `is_personal` deliberately does not filter them.

## Localization (i18n)
- gettext catalogs in `bot/locales/<lang>/LC_MESSAGES/messages.po`; msgids
  are the English texts. Workflow (commit both .po and .mo — a test checks
  the .mo matches the .po):
  ```
  pybabel extract -F babel.cfg -k __ --no-location --sort-output -o bot/locales/messages.pot .
  pybabel update -i bot/locales/messages.pot -d bot/locales -D messages
  pybabel compile -d bot/locales -D messages
  ```
- Language resolution (`crud.get_or_create_user_language`): stored
  `users.language`, else Russian for anyone with rows in receipts / budgets /
  report_settings (pre-localization users; migration 011 backfills them),
  else the Telegram `language_code` if supported, else English.
- `UserLocaleMiddleware` sets the language per update; code outside an
  update (scheduler, API) must wrap work in `i18n.use_locale(lang)`
  (`crud.resolve_user_language` for scheduled sends).
- Never assign to `_` (e.g. `_, x = data.split(":")`) in a module that
  imports gettext's `_` — it shadows the function.
- Keep `%` out of msgids (pass `"83%"` as the placeholder value): Babel
  flags `% o`/`% s` sequences as printf format and rejects the catalog.
- Bot-generated store/item names are stored as language-neutral markers
  (`bot.markers`, e.g. `@cash_withdrawal`) and translated with
  `markers.display_name()`. Pre-localization rows keep their Russian text;
  `markers.canonical_sql()` maps them to the marker in GROUP BY so old and
  new rows aggregate together.
- Dates use Babel/CLDR month names via `bot.utils.formatters`
  (`format_day_month_year`, `format_month_year`, ...).
- `tests/test_i18n_snapshots.py` records every user-visible string per
  language; regenerate with `UPDATE_SNAPSHOTS=1` and review the diff.
- Mini App: strings in `miniapp/src/i18n/messages.ts`, language from
  `GET /api/me`; category names come localized from the API.

## Known limitations
- **Miniapp native vs. PLN**: `/api/transactions/recent` returns `amount`
  (personal PLN share via `Receipt.personal_amount()`) next to
  `original_amount` (the full native-currency `total`, *not* scaled by the
  split share). For a split receipt these two figures are not proportional —
  don't assume `original_amount` reflects only the personal portion.
- **`/search` languages**: the LLM prompt and the regex fallback have rules
  for Russian, English and Polish (months incl. Polish inflections, relative
  periods, amount bounds, categories, cash). The fallback never extracts a
  merchant (see BACKLOG.md).
- **`/search` fallback honesty**: when the Haiku query parser
  (`search_parser.parse_search_query`) fails, `/search` falls back to a
  regex parser and appends "⚠️ Поиск выполнен по упрощённым правилам —
  результаты могут быть неточными." to the results. Any future change to
  search must keep this fallback self-disclosed, not silent.

## Process roles & production
- `APP_ROLE` (`bot/main.py`): `bot` = Telegram polling + scheduler +
  heartbeat file, `api` = FastAPI only, `all` (default) = everything in one
  process incl. `alembic upgrade head` (local development). **The scheduler
  must only run in the bot role** — otherwise reports are sent twice
  (`tests/test_app_roles.py`).
- Production (`docker-compose.prod.yml`): `db` (postgres:18), `redis`
  (redis:7-alpine, AOF, cache/sessions only — no data to migrate), one-shot
  `migrate`, `bot`, `api` on `127.0.0.1:8000` (public via Tailscale Funnel).
  `GET /healthz` (no auth) checks the DB. Details in DEPLOY.md.
- The `DEV_TOKEN` auth shortcut works only with `DEV_MODE=true`; the prod
  compose forces it off.
- Logs (stdout) must not contain amounts, receipt contents, search queries
  or keys at INFO+ (`tests/test_log_hygiene.py`). The exchangerate-api key
  is part of its URL, so httpx logs at WARNING and FX errors are scrubbed.

## Environment variables (.env)
See `.env.example` (no values):
```
BOT_TOKEN=
LLM_PROVIDER=gemini             # or anthropic
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.8-flash
ANTHROPIC_API_KEY=             # only for LLM_PROVIDER=anthropic
EXCHANGE_API_KEY=
POSTGRES_USER=
POSTGRES_PASSWORD=
POSTGRES_DB=
DATABASE_URL=postgresql+asyncpg://<user>:<password>@db:5432/<db>
REDIS_URL=redis://redis:6379/0   # bundled redis service (default)
# local only: DEV_MODE=true, DEV_TOKEN=, DEV_USER_ID=
```
