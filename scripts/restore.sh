#!/usr/bin/env bash
# Restore a pg_dump -Fc backup into the production stack's PostgreSQL.
#
#   scripts/restore.sh <dump-file> [--force]
#
# Only the "public" schema is restored: the Fly.io dump also contains the
# repmgr schema/extension, which does not exist (and is not wanted) here.
# Ownership and grants are dropped so objects belong to POSTGRES_USER.
# Refuses to run against a database that already has tables unless --force
# (then the public schema is dropped and recreated first).
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.prod.yml --env-file .env)
TABLES=(receipts items budgets report_settings)

usage() { echo "usage: $0 <dump-file> [--force]" >&2; exit 2; }
[[ $# -ge 1 && $# -le 2 ]] || usage
DUMP=$1
FORCE=${2:-}
[[ -z $FORCE || $FORCE == --force ]] || usage
[[ -f $DUMP ]] || { echo "dump not found: $DUMP" >&2; exit 1; }

psql_db() {
  # Runs psql inside the db container as POSTGRES_USER on POSTGRES_DB.
  "${COMPOSE[@]}" exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' psql "$@"
}

echo "==> Starting db"
"${COMPOSE[@]}" up -d --wait db

existing=$(psql_db -tAc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
if [[ $existing -gt 0 ]]; then
  if [[ $FORCE != --force ]]; then
    echo "public schema already has $existing tables; re-run with --force to replace them" >&2
    exit 1
  fi
  echo "==> --force: stopping bot/api and recreating the public schema"
  "${COMPOSE[@]}" stop bot api 2>/dev/null || true
  psql_db -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
fi

echo "==> Restoring public schema from $DUMP"
"${COMPOSE[@]}" exec -T db sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema=public --no-owner --no-privileges --single-transaction --exit-on-error' \
  < "$DUMP"

echo "==> alembic current"
"${COMPOSE[@]}" run --rm --no-deps -T migrate alembic current

echo "==> Row counts"
for table in "${TABLES[@]}"; do
  printf '%-16s %s\n' "$table" "$(psql_db -tAc "SELECT count(*) FROM public.$table")"
done
