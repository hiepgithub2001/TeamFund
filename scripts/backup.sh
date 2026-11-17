#!/usr/bin/env bash
# Dump the database to ~/backups and keep the last 14 days. Suggested cron (03:00 daily):
#   0 3 * * * /home/lehiepvps/projects/teamfund/scripts/backup.sh >> /home/lehiepvps/backups/backup.log 2>&1
# Restore:
#   gunzip -c ~/backups/teamfund-YYYY-MM-DD.sql.gz | docker compose exec -T db psql -U teamfund teamfund
set -euo pipefail
cd "$(dirname "$0")/.."
dir="${BACKUP_DIR:-$HOME/backups}"
mkdir -p "$dir"
umask 077
file="$dir/teamfund-$(date +%F).sql.gz"
docker compose exec -T db pg_dump -U teamfund --clean --if-exists teamfund | gzip > "$file"
find "$dir" -name 'teamfund-*.sql.gz' -mtime +14 -delete
echo "$(date -Is) wrote $file ($(du -h "$file" | cut -f1))"
