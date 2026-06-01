# FinanceBot

Telegram-bot for personal finance tracking via receipt photos.

## Features
- 📸 Receipt parsing via Claude Vision API
- 💱 Multi-currency support (PLN, EUR, USD, CZK, BYR)
- 📊 Statistics with charts (weekly / monthly / yearly)
- 💰 Budget tracking with alerts
- ➕ Manual expense entry
- 📤 Excel export

## Stack
Python 3.12 · aiogram 3 · PostgreSQL 16 · Redis 7 · Claude Vision API · Docker

## Quick start

1. Copy `.env.example` to `.env` and fill in all values
2. Run:
```bash
docker compose up -d
```
3. Open Telegram, find your bot, send /start

## Environment variables
See `.env.example` for all required variables.

## Commands
| Command | Description |
|---------|-------------|
| /start  | Welcome message |
| /stats  | View spending statistics |
| /budget | Manage budgets |
| /add    | Add expense manually |
| /export | Export to Excel |
| /help   | Show help |
