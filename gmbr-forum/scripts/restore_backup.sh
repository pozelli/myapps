#!/usr/bin/env bash
# Baixa o backup criptografado da branch claude/gmbr-data e restaura em data/gmbr.sqlite.
# Senha: arquivo data/backup.key (não versionado) ou variável GMBR_BACKUP_KEY.
#   scripts/restore_backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
BRANCH="${GMBR_DATA_BRANCH:-claude/gmbr-data}"
KEY=data/backup.key
mkdir -p data
if [ -n "${GMBR_BACKUP_KEY:-}" ]; then
  (umask 077; printf '%s' "$GMBR_BACKUP_KEY" > "$KEY")
fi
[ -s "$KEY" ] || { echo "Falta a senha: crie $KEY ou defina GMBR_BACKUP_KEY" >&2; exit 1; }
git fetch -q origin "$BRANCH"
parts=$(git ls-tree --full-tree --name-only FETCH_HEAD | grep '^gmbr.sqlite.gz.enc.part-' | sort)
[ -n "$parts" ] || { echo "Nenhuma parte de backup em $BRANCH" >&2; exit 1; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
for p in $parts; do git show "FETCH_HEAD:$p" > "$tmp/$p"; done
git show FETCH_HEAD:SHA256SUMS > "$tmp/SHA256SUMS"
(cd "$tmp" && sha256sum -c --quiet SHA256SUMS)
cat "$tmp"/gmbr.sqlite.gz.enc.part-* \
  | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "file:$KEY" \
  | gunzip > data/gmbr.sqlite.new
rm -f data/gmbr.sqlite-wal data/gmbr.sqlite-shm
mv data/gmbr.sqlite.new data/gmbr.sqlite
echo "Restaurado: data/gmbr.sqlite ($(du -h data/gmbr.sqlite | cut -f1))"
git show FETCH_HEAD:README.md | awk '/^```$/{n++; next} n==1'
