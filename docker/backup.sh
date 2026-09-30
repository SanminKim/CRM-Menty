#!/bin/sh
# Ежедневный бэкап PostgreSQL. Хранит последние BACKUP_KEEP_DAYS дней в ./backups на сервере.
set -e
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
mkdir -p /backups
while true; do
  FILE="/backups/crm_$(date +%Y-%m-%d_%H%M).sql.gz"
  if pg_dump -h db -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$FILE"; then
    echo "Backup OK: $FILE"
  else
    echo "Backup FAILED" && rm -f "$FILE"
  fi
  find /backups -name "crm_*.sql.gz" -mtime +"$KEEP_DAYS" -delete
  sleep 86400
done
