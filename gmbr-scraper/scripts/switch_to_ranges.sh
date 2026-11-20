#!/usr/bin/env bash
# Uso único: espera a listagem de todos os fóruns terminar, encerra os workers
# por seção (run_parallel.sh antigo) e continua com run_ranges.sh.
set -u
cd "$(dirname "$0")/.."
LOG=data/logs/switch.log
log() { echo "$(date +%H:%M:%S) $*" >> "$LOG"; }

pending_listing() {
  python3 - <<'PY'
import sqlite3
c = sqlite3.connect("data/gmbr.sqlite", timeout=300)
print(c.execute("SELECT COUNT(*) FROM forums WHERE scraped_at IS NULL").fetchone()[0])
PY
}

log "INFO aguardando listagens"
while [ "$(pending_listing)" != 0 ]; do sleep 60; done
log "INFO listagens concluídas; trocando para faixas de ID"

OLD=$(pgrep -o -f "scripts/run_parallel.sh")
if [ -n "$OLD" ]; then
  PGID=$(ps -o pgid= -p "$OLD" | tr -d ' ')
  kill -TERM -- "-$PGID" 2>/dev/null
  for _ in $(seq 30); do
    pgrep -f "gmbr_scraper --db data/gmbr.sqlite crawl" >/dev/null || break
    sleep 1
  done
fi
if pgrep -f "gmbr_scraper --db data/gmbr.sqlite crawl" >/dev/null; then
  log "ERROR workers antigos não terminaram; troca abortada"
  exit 1
fi

setsid nohup scripts/run_ranges.sh 10 20 >> data/logs/ranges_main.log 2>&1 < /dev/null &
sleep 5
log "INFO $(head -1 data/logs/ranges_main.log)"

# O loop de backup encerra se não vir workers; garante que siga rodando
if ! pgrep -f "scripts/backup_loop.sh" >/dev/null; then
  setsid nohup scripts/backup_loop.sh 600 >/dev/null 2>&1 < /dev/null &
  log "INFO loop de backup reiniciado"
fi
