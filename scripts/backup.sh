#!/usr/bin/env bash
# Daily logical backup of the production database.
#
#   scripts/backup.sh [--force]
#
# Writes pg_dump -Fc to $BACKUP_DIR (default /var/backups/financebot) with
# mode 600 and keeps the newest $KEEP (default 7) dumps.
#
# Cron fires hourly (see deploy/cron/financebot-backup) because the server
# clock is UTC while the backup is due at 03:30 Europe/Warsaw, whose UTC
# offset changes with DST. Without --force the script exits unless it is
# currently the 03:xx hour in Warsaw, so exactly one run per day proceeds.
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.prod.yml --env-file .env)
BACKUP_DIR=${BACKUP_DIR:-/var/backups/financebot}
KEEP=${KEEP:-7}
BACKUP_HOUR=${BACKUP_HOUR:-03}

if [[ ${1:-} != --force && $(TZ=Europe/Warsaw date +%H) != "$BACKUP_HOUR" ]]; then
  exit 0
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

stamp=$(TZ=Europe/Warsaw date +%Y-%m-%d_%H%M)
target="$BACKUP_DIR/financebot_${stamp}.dump"
tmp="$target.partial"

"${COMPOSE[@]}" exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$tmp"
# A truncated dump fails to list; never let a broken file rotate out a good one.
"${COMPOSE[@]}" exec -T db pg_restore --list < "$tmp" > /dev/null
chmod 600 "$tmp"
mv "$tmp" "$target"

# Keep the newest $KEEP dumps.
ls -1t "$BACKUP_DIR"/financebot_*.dump | tail -n +"$((KEEP + 1))" | xargs -r rm -f --

echo "$(date -u +%FT%TZ) backup ok: $target ($(du -h "$target" | cut -f1))"
