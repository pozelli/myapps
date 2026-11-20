#!/usr/bin/env bash
# Backup periódico do banco, criptografado, numa branch própria do GitHub.
#   scripts/backup_loop.sh [intervalo_segundos]
# Senha em data/backup.key. Mantém só a cópia mais recente (force-push).
# Restaurar:  cat gmbr.sqlite.gz.enc.part-* | openssl enc -d -aes-256-cbc \
#               -pbkdf2 -iter 200000 -pass file:backup.key | gunzip > gmbr.sqlite
set -u
cd "$(dirname "$0")/.."
INTERVAL="${1:-600}"
DB=data/gmbr.sqlite
KEY=data/backup.key
BRANCH=claude/gmbr-data
REMOTE_URL="$(git -C .. remote get-url origin)"
WORK="${BACKUP_WORKDIR:-$HOME/gmbr-data-backup}"
LOG=data/logs/backup.log
mkdir -p data/logs

log() { echo "$(date +%H:%M:%S) $*" >> "$LOG"; }

init_repo() {
  [ -d "$WORK/.git" ] && return
  mkdir -p "$WORK" && git -C "$WORK" init -q -b "$BRANCH" \
    && git -C "$WORK" remote add origin "$REMOTE_URL"
}

backup_once() {
  local snap=data/.snapshot.sqlite ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  rm -f "$snap"
  # Cópia consistente mesmo com os workers gravando
  python3 - "$DB" "$snap" <<'PY' || return 1
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=300)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
  local stats
  stats="$(python3 -m gmbr_scraper --db "$snap" stats 2>/dev/null | head -5)"
  init_repo || return 1
  rm -f "$WORK"/gmbr.sqlite.gz.enc.part-*
  gzip -c "$snap" \
    | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 -pass "file:$KEY" \
    | split -b 90m -d -a 3 - "$WORK/gmbr.sqlite.gz.enc.part-" || return 1
  rm -f "$snap"
  cat > "$WORK/README.md" <<MD
# Backup do banco do gmbr-scraper

Cópia criptografada (AES-256-CBC, PBKDF2) de \`gmbr-scraper/data/gmbr.sqlite\`.
Gerada em: $ts

\`\`\`
$stats
\`\`\`

Restaurar (precisa da senha em \`backup.key\`):

\`\`\`bash
cat gmbr.sqlite.gz.enc.part-* \\
  | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:backup.key \\
  | gunzip > gmbr.sqlite
\`\`\`
MD
  (cd "$WORK" && sha256sum gmbr.sqlite.gz.enc.part-* > SHA256SUMS)
  git -C "$WORK" add -A || return 1
  local msg="Backup do banco gmbr-scraper ($ts)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01B43M86Mv3rBLhkbSg9CfsN"
  if git -C "$WORK" rev-parse -q --verify HEAD >/dev/null; then
    git -C "$WORK" commit -q --amend -m "$msg" || return 1
  else
    git -C "$WORK" commit -q -m "$msg" || return 1
  fi
  local i ok=0
  for i in 1 2 3 4; do
    if git -C "$WORK" push -q -f origin "HEAD:$BRANCH" 2>>"$LOG"; then ok=1; break; fi
    sleep $((i * 15))
  done
  [ "$ok" = 1 ] || return 1
  # Descarta cópias antigas do disco local (só a última importa)
  git -C "$WORK" reflog expire --expire=now --all && git -C "$WORK" gc -q --prune=now
  log "INFO backup ok $(du -ch "$WORK"/gmbr.sqlite.gz.enc.part-* | tail -1 | cut -f1)"
}

[ -s "$KEY" ] || { log "ERROR backup: senha $KEY não encontrada"; exit 1; }
while :; do
  backup_once || log "ERROR backup falhou (ver linhas acima)"
  pgrep -f "gmbr_scraper --db $DB (crawl|list-pages|mark-listed|plan-ranges)" >/dev/null || { backup_once; log "INFO backup final; workers terminaram"; break; }
  sleep "$INTERVAL"
done
