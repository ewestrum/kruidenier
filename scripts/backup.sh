#!/bin/sh
# Nightly pg_dump into /backups, keep 14 days (SPEC §15). Runs in the postgres:16-alpine image.
set -eu

BACKUP_HOUR="${BACKUP_HOUR:-3}"
KEEP_DAYS="${KEEP_DAYS:-14}"
export PGHOST="${PGHOST:-db}" PGUSER="${PGUSER:-kruidenier}" PGDATABASE="${PGDATABASE:-kruidenier}"

dump() {
  stamp="$(date +%Y-%m-%d_%H%M)"
  tmp="/backups/.kruidenier-${stamp}.sql"
  # No pipe: in POSIX sh `pg_dump | gzip` would only report gzip's exit status.
  if pg_dump --no-owner --clean --if-exists --file="$tmp" && gzip -9 "$tmp"; then
    mv "$tmp.gz" "/backups/kruidenier-${stamp}.sql.gz"
    echo "backup ok: kruidenier-${stamp}.sql.gz"
    find /backups -name 'kruidenier-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
    return 0
  fi
  rm -f "$tmp" "$tmp.gz"
  echo "backup FAILED" >&2
  return 1
}

# `backup.sh now`: one backup immediately, then exit (e.g. before an update).
if [ "${1:-}" = "now" ]; then
  dump
  exit $?
fi

echo "backup service: daily at ${BACKUP_HOUR}:00, keeping ${KEEP_DAYS} days"
while true; do
  # strip leading zeros: POSIX sh would read "08" as invalid octal
  now_h="$(date +%H | sed 's/^0//')"; now_m="$(date +%M | sed 's/^0//')"
  # seconds until the next BACKUP_HOUR:00
  wait=$(( ((BACKUP_HOUR - ${now_h:-0} + 24) % 24) * 3600 - ${now_m:-0} * 60 ))
  [ "$wait" -le 0 ] && wait=$(( wait + 86400 ))
  sleep "$wait"
  dump || true  # a failed night is logged; keep the service running for the next one
done
