#!/usr/bin/env bash
# Espera a raspagem em andamento terminar, faz uma passada final pelos tópicos
# que ficaram pendentes (falhas de rede) e então roda scripts/run_extras.sh.
#   GMBR_USER=... GMBR_PASS=... scripts/run_all.sh [N] [delay] [N_extras]
set -u
cd "$(dirname "$0")/.."
N="${1:-10}"
DELAY="${2:-20}"
NX="${3:-$N}"  # processos da passada complementar
LOGS=data/logs
while pgrep -f "run_ranges\.sh|gmbr_scraper --db data/gmbr.sqlite crawl" >/dev/null; do
  sleep 60
done
pending=$(python3 -c "
import sqlite3; c = sqlite3.connect('data/gmbr.sqlite', timeout=300)
print(c.execute('SELECT COUNT(*) FROM topics WHERE completed=0').fetchone()[0])")
echo "$(date +%H:%M:%S) raspagem terminou; $pending tópicos pendentes"
if [ "$pending" -gt 0 ]; then
  bash scripts/run_ranges.sh "$N" "$DELAY"
fi
bash scripts/run_extras.sh "$NX" "$DELAY"
