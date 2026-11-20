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
# Freio compartilhado: um 429/503 em qualquer processo desacelera todos.
export GMBR_THROTTLE_FILE="${GMBR_THROTTLE_FILE:-data/throttle.json}"
mkdir -p "$LOGS"

mapfile -t RANGES < <(python3 -m gmbr_scraper --db "$DB" plan-ranges -n "$N")
echo "$(date +%H:%M:%S) faixas: ${RANGES[*]}"
i=0
for r in "${RANGES[@]}"; do
  i=$((i+1))
  # Novas tentativas se o processo cair (ex.: login falhou com o fórum lento).
  ( for try in 1 2 3 4 5 6; do
      python3 -m gmbr_scraper --db "$DB" crawl --delay "$DELAY" --skip-listing --id-range "$r"
      rc=$?
      [ "$rc" -eq 0 ] && break
      echo "WORKER r$i caiu (exit $rc); nova tentativa em 2 min"
      sleep 120
    done
    echo "WORKER r$i FIM (exit $rc)" ) >>"$LOGS/r$i.log" 2>&1 &
  sleep 15  # escalona os logins
done
wait
echo "$(date +%H:%M:%S) TODOS OS WORKERS TERMINARAM"
