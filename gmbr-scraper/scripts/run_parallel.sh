#!/usr/bin/env bash
# Crawl completo em duas fases:
#   1. lista os fóruns em paralelo (5 grupos de seções + Perguntas e Respostas);
#      depois da primeira vez a listagem é incremental e leva poucos minutos;
#   2. raspa os tópicos pendentes com N processos em faixas de ID equilibradas.
#   GMBR_USER=... GMBR_PASS=... scripts/run_parallel.sh [delay] [N]
# Logs em data/logs/; banco em data/gmbr.sqlite.
set -u
cd "$(dirname "$0")/.."
DELAY="${1:-20}"
N="${2:-10}"
DB=data/gmbr.sqlite
LOGS=data/logs
mkdir -p "$LOGS"

# Grupos de seções (balanceados pelo nº de páginas de listagem)
SECTIONS=(
  "--forum 10"                                      # Perguntas e Respostas
  "--forum 23"                                      # Assuntos Diversos
  "--forum 17 --forum 6"                            # Assets, Projetos Completos
  "--forum 5 --forum 24 --forum 19"                 # Projetos em Andamento, Lixeira (+19)
  "--forum 12 --forum 55 --forum 7"                 # Engines, Tutoriais, Equipes
  # Demais seções + subfóruns (13 de 9; 8,20,26,27,39,56 de 25)
  "--forum 1 --forum 33 --forum 25 --forum 41 --forum 4 --forum 9 --forum 2 --forum 3 --forum 13 --forum 8 --forum 20 --forum 26 --forum 27 --forum 39 --forum 56"
)

# Garante que os fóruns (inclusive novos) estejam no banco
python3 -m gmbr_scraper --db "$DB" discover --delay "$DELAY" \
  >>"$LOGS/list0.log" 2>&1 || true

n=0
for g in "${SECTIONS[@]}"; do
  n=$((n+1))
  ( python3 -m gmbr_scraper --db "$DB" crawl --delay "$DELAY" --list-only $g
    echo "WORKER l$n FIM (exit $?)" ) >>"$LOGS/l$n.log" 2>&1 &
done
wait
echo "$(date +%H:%M:%S) listagem concluída"

exec scripts/run_ranges.sh "$N" "$DELAY"
