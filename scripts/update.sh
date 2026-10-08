#!/bin/sh
# Update Kruidenier on the NAS in one command (run over SSH):
#
#   sudo sh /volume2/docker/Kruidenier-NAS/scripts/update.sh 0.4.0
#
# Sets TAG in .env (optional argument), pulls the new image, restarts web + worker and
# waits until /healthz is ok. Migrations run automatically when web starts.
set -eu

cd "$(dirname "$0")/.."
[ -f docker-compose.yml ] && [ -f .env ] || { echo "Geen docker-compose.yml/.env in $(pwd)" >&2; exit 1; }

if [ $# -ge 1 ]; then
  case "$1" in
    *[!A-Za-z0-9._-]*|"") echo "Ongeldige versie: $1" >&2; exit 1 ;;
  esac
  sed -i "s/^TAG=.*/TAG=$1/" .env
fi
echo "Versie: $(sed -n 's/^TAG=//p' .env)"

if docker compose version >/dev/null 2>&1; then
  dc() { docker compose "$@"; }
else
  dc() { docker-compose "$@"; }
fi

dc pull web worker
dc up -d
dc restart backup  # picks up a changed scripts/backup.sh

port="$(sed -n 's/^WEB_PORT=//p' .env)"
port="${port:-8085}"
printf "Wachten op Kruidenier"
i=0
while [ $i -lt 40 ]; do
  if curl -fs "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
    echo " klaar."
    curl -s "http://127.0.0.1:${port}/healthz"; echo
    exit 0
  fi
  printf "."
  sleep 3
  i=$((i + 1))
done
echo
echo "Kruidenier reageert nog niet gezond. Bekijk de logs met: $(command -v docker) logs kruidenier-web-1" >&2
exit 1
