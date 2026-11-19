#!/usr/bin/env bash
# Dispara 10 processos de crawl em paralelo, cada um numa parte do fórum.
#   GMBR_USER=... GMBR_PASS=... scripts/run_parallel.sh [delay]
# Logs em data/logs/w*.log; banco em data/gmbr.sqlite.
set -u
cd "$(dirname "$0")/.."
DELAY="${1:-20}"
DB=data/gmbr.sqlite
LOGS=data/logs
mkdir -p "$LOGS"

# Grupos de seções menores (balanceados pelo nº de tópicos)
SECTIONS=(
  "--forum 23"                                                       # Assuntos Diversos
  "--forum 17 --forum 6"                                             # Assets, Projetos Completos
  "--forum 5 --forum 24 --forum 19"                                  # Projetos em Andamento, Lixeira (+sub 19)
  "--forum 12 --forum 55 --forum 7"                                  # Engines, Tutoriais, Equipes
  # Demais seções + subfóruns (13 de 9; 8,20,26,27,39,56 de 25)
  "--forum 1 --forum 33 --forum 25 --forum 41 --forum 4 --forum 9 --forum 2 --forum 3 --forum 13 --forum 8 --forum 20 --forum 26 --forum 27 --forum 39 --forum 56"
)
# f10 (Perguntas e Respostas, ~14 mil tópicos) dividido em 5 shards
F10_SHARDS=5

crawl() { python3 -m gmbr_scraper --db "$DB" crawl --delay "$DELAY" "$@"; }

n=0
for g in "${SECTIONS[@]}"; do
  n=$((n+1))
  ( crawl $g; echo "WORKER w$n FIM (exit $?)" ) >"$LOGS/w$n.log" 2>&1 &
done

# w6: lista a f10 e raspa o shard 0
n=$((n+1))
( crawl --forum 10 --shard "0/$F10_SHARDS"; echo "WORKER w$n FIM (exit $?)" ) >"$LOGS/w$n.log" 2>&1 &
LISTER=$!

# w7..w10: raspam os demais shards da f10 à medida que a listagem avança
pending() {
  python3 - "$DB" "$1" "$F10_SHARDS" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], timeout=300)
i, n = int(sys.argv[2]), int(sys.argv[3])
print(c.execute("SELECT COUNT(*) FROM topics WHERE forum_id=10 AND id % ? = ? AND "
                "(completed=0 OR scraped_replies IS NULL OR replies <> scraped_replies)",
                (n, i)).fetchone()[0])
PY
}
for i in $(seq 1 $((F10_SHARDS-1))); do
  n=$((n+1))
  (
    while kill -0 "$LISTER" 2>/dev/null; do
      if [ "$(pending "$i")" -gt 0 ]; then
        crawl --forum 10 --skip-listing --shard "$i/$F10_SHARDS"
      else
        sleep 120
      fi
    done
    # listagem terminou: uma última passada no que restou
    crawl --forum 10 --skip-listing --shard "$i/$F10_SHARDS"
    echo "WORKER w$n FIM (exit $?)"
  ) >"$LOGS/w$n.log" 2>&1 &
done
wait
echo "TODOS OS WORKERS TERMINARAM"
