#!/usr/bin/env bash
# Passada complementar, depois da raspagem das mensagens, com N processos:
#  fase 1: relista todos os fóruns (marca tópicos com enquete, fixos, trancados)
#          e percorre /memberlist (todos os membros, inclusive quem nunca postou);
#  fase 2: raspa as enquetes e os perfis /uNN (quem tem mais mensagens primeiro).
#   GMBR_USER=... GMBR_PASS=... scripts/run_extras.sh [N] [delay]
set -u
cd "$(dirname "$0")/.."
N="${1:-10}"
DELAY="${2:-20}"
DB=data/gmbr.sqlite
LOGS=data/logs
mkdir -p "$LOGS"

DONE1="$LOGS/extras_fase1.ok"  # apague para refazer a fase 1
if [ ! -f "$DONE1" ]; then
# Divide as páginas de listagem (30 tópicos) e da lista de membros (30 membros)
# em N filas de tamanho parecido; cada linha é um comando do gmbr_scraper.
python3 - "$DB" "$N" "$LOGS" <<'PY'
import math, sqlite3, sys
db, n, logs = sys.argv[1], int(sys.argv[2]), sys.argv[3]
c = sqlite3.connect(db)
pages = []  # (tipo, alvo, offset)
for fid, cnt in c.execute("SELECT forum_id, COUNT(*) FROM topics GROUP BY forum_id"):
    if fid is None:
        continue
    pages += [("f", fid, i * 30) for i in range(math.ceil(cnt / 30) + 1)]
total_members = 17376  # "Temos N usuários registrados" no índice
pages += [("m", None, i * 30) for i in range(math.ceil(total_members / 30) + 2)]
per = math.ceil(len(pages) / n)
for w in range(n):
    chunk = pages[w * per:(w + 1) * per]
    # junta páginas consecutivas do mesmo alvo num só comando
    cmds, cur = [], None
    for kind, target, off in chunk:
        if cur and cur[0] == kind and cur[1] == target and off == cur[3] + 30:
            cur[3] = off
        else:
            if cur:
                cmds.append(cur)
            cur = [kind, target, off, off]
    if cur:
        cmds.append(cur)
    out = []
    for kind, target, a, b in cmds:
        if kind == "f":
            out.append(f"list-pages --forum {target} --first {a} --last {b}")
        else:
            last = "" if w == n - 1 else f" --last {b}"
            out.append(f"memberlist --first {a}{last}")
    with open(f"{logs}/extras_plan_x{w + 1}.txt", "w") as f:
        f.write("\n".join(out) + "\n")
PY
echo "$(date +%H:%M:%S) fase 1 (relistagem + lista de membros)"
for i in $(seq "$N"); do
  ( while read -r cmd; do
      for try in 1 2 3; do
        # shellcheck disable=SC2086
        python3 -m gmbr_scraper --db "$DB" $cmd --delay "$DELAY" </dev/null && break
        echo "EXTRA x$i falhou: $cmd; nova tentativa em 2 min"; sleep 120
      done
    done < "$LOGS/extras_plan_x$i.txt"
    echo "EXTRA x$i fase 1 FIM" ) >>"$LOGS/x$i.log" 2>&1 &
  sleep 15
done
wait
touch "$DONE1"
fi

echo "$(date +%H:%M:%S) fase 2 (enquetes + perfis)"
for i in $(seq "$N"); do
  ( for step in polls profiles; do
      for try in 1 2 3 4 5 6; do
        python3 -m gmbr_scraper --db "$DB" "$step" --shard "$((i-1))/$N" --delay "$DELAY"
        rc=$?; [ "$rc" -eq 0 ] && break
        echo "EXTRA x$i $step caiu (exit $rc); nova tentativa em 2 min"; sleep 120
      done
    done
    echo "WORKER x$i FIM (exit $rc)" ) >>"$LOGS/x$i.log" 2>&1 &
  sleep 15
done
wait
echo "$(date +%H:%M:%S) EXTRAS TERMINARAM"
