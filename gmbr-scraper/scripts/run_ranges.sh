#!/usr/bin/env bash
# Raspa os tópicos pendentes de TODOS os fóruns com N processos, cada um numa
# faixa de IDs de tópico (sem sobreposição), equilibradas pelo trabalho
# pendente. Requer a listagem já feita.
#   GMBR_USER=... GMBR_PASS=... scripts/run_ranges.sh [N] [delay]
set -u
cd "$(dirname "$0")/.."
N="${1:-10}"
DELAY="${2:-20}"
DB=data/gmbr.sqlite
LOGS=data/logs
mkdir -p "$LOGS"

mapfile -t RANGES < <(python3 -m gmbr_scraper --db "$DB" plan-ranges -n "$N")
echo "$(date +%H:%M:%S) faixas: ${RANGES[*]}"
i=0
for r in "${RANGES[@]}"; do
  i=$((i+1))
  ( python3 -m gmbr_scraper --db "$DB" crawl --delay "$DELAY" --skip-listing --id-range "$r"
    echo "WORKER r$i FIM (exit $?)" ) >>"$LOGS/r$i.log" 2>&1 &
done
wait
echo "$(date +%H:%M:%S) TODOS OS WORKERS TERMINARAM"
