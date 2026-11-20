#!/usr/bin/env bash
# Relança tudo (ex.: após reinício da máquina): raspagem pendente, passada
# complementar e backup. As credenciais vêm de data/.env.sh (fora do git):
#   export GMBR_USER=...
#   export GMBR_PASS=...
#   scripts/restart.sh [N] [delay] [N_extras]
set -u
cd "$(dirname "$0")/.."
N="${1:-10}"; DELAY="${2:-20}"; NX="${3:-20}"
mkdir -p data/logs/old
ts=$(date +%H%M)
for f in data/logs/r*.log data/logs/x*.log data/logs/ranges_main.log data/logs/all_main.log; do
  [ -f "$f" ] && mv "$f" "data/logs/old/$(basename "$f" .log)-$ts.log"
done
# shellcheck disable=SC1091
. data/.env.sh
setsid nohup bash scripts/run_all.sh "$N" "$DELAY" "$NX" > data/logs/all_main.log 2>&1 < /dev/null &
sleep 5
pgrep -f backup_loop >/dev/null || \
  setsid nohup bash scripts/backup_loop.sh 600 >/dev/null 2>&1 < /dev/null &
