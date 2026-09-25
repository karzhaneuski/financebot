# Deploying FinanceBot on the Oracle Cloud server

Target: Ubuntu 24.04 **arm64** VM with Docker, reachable over Tailscale.
Fly.io is gone (the app is suspended) — there is no fallback there; the only
rollback is restoring a local backup on this server (see [Rollback](#rollback)).

```
                     Tailscale Funnel (https://<host>.<tailnet>.ts.net)
                                      │
Mini App (Cloudflare Pages) ──HTTPS──▶│  127.0.0.1:8000
                                      ▼
┌──────────── docker network "financebot" ────────────┐
│ api  (APP_ROLE=api)  FastAPI, /healthz               │
│ bot  (APP_ROLE=bot)  Telegram polling + scheduler    │──▶ Telegram, Anthropic, FX APIs
│ migrate (one-shot)   alembic upgrade head            │
│ db   postgres:18     volume financebot_prod_pgdata   │
│ redis redis:7-alpine volume financebot_prod_redisdata│
└──────────────────────────────────────────────────────┘
```

- PostgreSQL major version **18** — same as the Fly database the dump came from.
- The API port is bound to `127.0.0.1` only; the internet reaches it solely
  through Tailscale Funnel. PostgreSQL and Redis have no published ports.
- Redis holds only a cache and short-lived sessions (FX rates, statement
  import drafts, `/split` and list sessions — all with TTLs; FSM state is in
  process memory). Nothing in it needs migrating or backing up; AOF
  (`appendonly yes`) just keeps it across restarts.
- The scheduler (daily/weekly/monthly reports, FX refresh) runs only in `bot`.
- `DEV_MODE`/`DEV_TOKEN` are forced off by `docker-compose.prod.yml`.

All commands below run on the server from `/opt/financebot` unless noted.
`dc` is short for:

```bash
alias dc='docker compose -f docker-compose.prod.yml --env-file .env'
```

## 1. Server prerequisites

```bash
uname -m                      # must print aarch64
docker version && docker compose version
sudo usermod -aG docker ubuntu   # then log out/in; cron runs backups as ubuntu
tailscale status              # the server is in your tailnet
timedatectl                   # usually UTC — that's fine, see Backups
```

## 2. Code and secrets

```bash
sudo mkdir -p /opt/financebot && sudo chown ubuntu: /opt/financebot
git clone <repo-url> /opt/financebot      # or rsync the checkout over Tailscale
cd /opt/financebot && git checkout infra/oracle

cp .env.example .env && chmod 600 .env
$EDITOR .env
```

Before filling `.env`:

- **BOT_TOKEN** — revoke the old token in @BotFather and use the new one.
- **EXCHANGE_API_KEY** — re-issue it at exchangerate-api.com (the old key was
  committed and logged).
- **POSTGRES_*** — pick them now; `DATABASE_URL` must repeat them literally:
  `postgresql+asyncpg://<user>:<password>@db:5432/<db>`.
- **REDIS_URL** — keep `redis://redis:6379/0` (the bundled `redis` service;
  also the default when unset).
- **LLM_PROVIDER / GEMINI_API_KEY / GEMINI_MODEL** — see
  [LLM provider](#llm-provider-gemini--anthropic). Default: Gemini.
- Leave `DEV_MODE`, `DEV_TOKEN`, `DEV_USER_ID` unset.

## LLM provider (Gemini / Anthropic)

Receipt photos, PDF receipts, bank screenshots, item-name normalization and
`/search` queries go to the provider selected by `LLM_PROVIDER` (`gemini` by
default, or `anthropic`).

**Gemini (default):**

1. Create a key in Google AI Studio → **Get API key**
   (https://aistudio.google.com/apikey) and put it in `GEMINI_API_KEY`.
2. `GEMINI_MODEL` defaults to `gemini-3.8-flash` (stable, image input,
   structured output, free tier — checked against ai.google.dev on
   2026-09-25). Google publishes free-tier limits only per account: open
   AI Studio → **Rate limits** and check requests/day for this model. Each
   receipt photo costs **three** requests (bank-screenshot detection,
   receipt parsing, item-name normalization); a PDF receipt two; a bank
   screenshot or a `/search` query one.
   If the daily limit is too low, switch `GEMINI_MODEL` to a Flash-Lite model
   from the same page (e.g. `gemini-3.5-flash-lite`) and re-check quality.
3. **Privacy:** on the free tier Google may use submitted content — here,
   photos of receipts and bank screens — to improve its products (see the
   "Used to improve our products" row on the pricing page). The paid tier
   does not.

**Anthropic:** set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.

Switching: edit `.env`, then `dc up -d bot api` (the provider is read at
startup). When the provider rejects requests for quota, balance, rate limits,
auth/billing or an outage, users get "recognition temporarily unavailable —
add the expense with /add or upload a bank statement" instead of a generic
error; the cause is logged by `bot` (`dc logs bot | grep -i "provider unavailable"`).

If only item-name normalization fails (quota hit mid-receipt), the receipt
is still saved, with raw item names (product statistics group less well).

## 3. First start with data from the dump

Copy the dump from your Mac over Tailscale:

```bash
# on the Mac
scp financebot_backup.dump ubuntu@<server>:/opt/financebot/
```

Then on the server:

```bash
dc build                                   # native arm64 build
scripts/restore.sh financebot_backup.dump  # public schema only, no repmgr
```

`restore.sh` starts `db`, restores, then prints `alembic current` (expect
`010_item_is_personal (head)`) and row counts of `receipts`, `items`,
`budgets`, `report_settings` — compare them with the Fly database. It refuses
to overwrite a database that already has tables unless given `--force`.

```bash
dc up -d
dc ps            # db/redis/api/bot healthy, migrate "Exited (0)"
curl -fsS http://127.0.0.1:8000/healthz     # {"status":"ok"}
dc logs -f bot   # "Starting FinanceBot, role=bot" and polling started
```

Send `/start` to the bot to confirm it answers. Then remove the dump copy
(`shred -u financebot_backup.dump`) — it contains all financial data.

## 4. Public API via Tailscale Funnel

1. Tailscale admin console → **DNS**: enable MagicDNS and **HTTPS
   certificates**.
2. **Access controls**: allow Funnel for this node, e.g.
   ```json
   "nodeAttrs": [{ "target": ["<server-tag-or-user>"], "attr": ["funnel"] }]
   ```
3. On the server:
   ```bash
   sudo tailscale funnel --bg 8000      # https://<host>.<tailnet>.ts.net → 127.0.0.1:8000
   tailscale funnel status
   ```
   The setting persists across reboots. To stop: `sudo tailscale funnel --https=443 off`.
4. From a device **outside** the tailnet (e.g. phone on mobile data):
   `curl https://<host>.<tailnet>.ts.net/healthz` → `{"status":"ok"}`.
   Unauthenticated `/api/...` calls must return 401/422.

## 5. Point the Mini App at the new API (Cloudflare Pages)

The Mini App reads the API base URL at **build time** from `VITE_API_URL`
(`miniapp/src/api/client.ts`).

1. Cloudflare dashboard → Workers & Pages → the Mini App project →
   **Settings → Variables and Secrets** (Environment variables) →
   **Production**: set `VITE_API_URL=https://<host>.<tailnet>.ts.net`
   (no trailing slash).
2. Trigger a new build: **Deployments → … → Retry deployment** (or push a
   commit). The variable only takes effect on a rebuild.
3. Optional: also update `miniapp/.env` (it still holds the Fly URL). Build
   variables set in Cloudflare take precedence over that file.
4. Open the Mini App from Telegram and check that the dashboard loads.
   Production builds must not contain the dev token:
   `npm run build && ! grep -r devsecret dist/`.

## 6. Backups

Daily `pg_dump -Fc` at **03:30 Europe/Warsaw**, 7 newest kept, files mode 600
in `/var/backups/financebot` (dir mode 700).

```bash
sudo mkdir -p /var/backups/financebot
sudo chown ubuntu: /var/backups/financebot && chmod 700 /var/backups/financebot
sudo touch /var/log/financebot-backup.log && sudo chown ubuntu: /var/log/financebot-backup.log
sudo install -m 644 deploy/cron/financebot-backup /etc/cron.d/financebot-backup

scripts/backup.sh --force        # take one now, check it works
ls -l /var/backups/financebot
```

Why hourly in cron: the server clock is UTC, and 03:30 in Warsaw is 01:30 UTC
in summer but 02:30 UTC in winter. Cron fires at :30 every hour and
`backup.sh` exits unless it is the 03:xx hour in `Europe/Warsaw`, so exactly
one backup per day is taken at the right local time, DST included, whatever
the server timezone. Each dump is validated (`pg_restore --list`) before it
can rotate out an older one.

Check: `tail /var/log/financebot-backup.log`.

### External copy of backups

Not implemented yet. Planned: pull the dumps to the Mac over Tailscale, so a
lost VM doesn't take the backups with it.

- On the Mac, a daily `launchd` job (the Mac may be asleep at 03:30, so pull
  rather than push):
  ```bash
  rsync -a --chmod=F600,D700 ubuntu@<server>:/var/backups/financebot/ ~/Backups/financebot/
  ```
  (No `--delete`: the Mac keeps a longer history than the server's 7 days.)
- Use a dedicated SSH key restricted on the server with `rrsync -ro
  /var/backups/financebot` in `authorized_keys`, reachable only via the
  tailnet.
- The Mac copy holds all financial data — keep it on an encrypted volume
  (FileVault) and prune old files periodically.

## 7. Updating

```bash
git pull
dc build
dc up -d          # migrate runs first; bot/api restart only if it succeeds
dc ps && curl -fsS http://127.0.0.1:8000/healthz
```

Take `scripts/backup.sh --force` before updates that include migrations.

## Rollback

There is no other environment to fall back to. Rollback = restore the last
good local backup on this server.

**Bad code release** (data fine):

```bash
git checkout <previous-good-commit>
dc build && dc up -d
```

If the bad release ran a migration, also restore the backup taken before it
(below): the old code expects the old schema.

**Bad data / broken database:**

```bash
ls -lt /var/backups/financebot                    # pick the dump to return to
scripts/restore.sh /var/backups/financebot/financebot_<date>.dump --force
dc up -d
```

`--force` stops `bot` and `api`, drops and recreates the `public` schema, then
restores. Everything written after that dump is lost — take
`scripts/backup.sh --force` first if the current state might still be needed.

## Useful commands

```bash
dc ps                    # status + health
dc logs -f --tail=100 bot api
dc exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"   # after `set -a; . ./.env; set +a`
dc run --rm migrate alembic current
```

Logs go to Docker's json-file driver (10 MB × 5 per container). They contain
no amounts, receipt contents, search queries or API keys at the default INFO
level.
