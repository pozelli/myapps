"""Alegações de que nomes são da mesma pessoa e agrupamento em persons.

As alegações (identity_claims) vêm da leitura das mensagens — sobretudo dos
tópicos de denúncias — e trazem o trecho que as sustenta. Daqui saem grupos:

* vínculo forte: confirma_staff (IP, admissão, banimento pela equipe) em
  qualquer tópico, ou afirma nos tópicos de denúncias;
* para cada par de nomes vale a alegação mais recente: uma negação posterior
  (ex.: "são irmãos, mesma rede" aceito pela equipe) desfaz o vínculo, e uma
  confirmação posterior a uma negação o refaz;
* suspeitas e afirmações fora das denúncias ficam registradas, mas não agrupam.
"""
from __future__ import annotations

import itertools
import json
import re
import sqlite3
from collections import Counter, defaultdict

DENUNCIA_TOPICS = (5, 21237)

# Apelidos usados na conversa que coincidem com contas de OUTRAS pessoas (ou
# não são nomes de conta). Ficam nas alegações, mas não viram person_names.
# Revisados à mão em 2026-11-21.
NOT_ACCOUNT_NAMES = {
    "gabriel", "gabriel números", "gabrielnúmeros", "gabriel968432523^54",  # = gabriel33245900
    "Kevin",        # conta 527 (1.166 mensagens) é outra pessoa; refere-se a kevipegoraro
    "Kaique",       # conta 15 (2008) é outra pessoa; refere-se ao grupo kaique*
    "Evandro", "Die", "Bomber", "Terra", "Hiroshi", "raul", "Removed",
}
STRONG_KINDS = ("confirma_staff",)


def _clean_name(name: str) -> str:
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name.strip())  # "X (banido)", "X (ccphreaker)"
    return name.strip(" \"'")


def _key(name: str) -> str:
    """Chave de grafia: "Need 90" = "Need_90", "Os Carlos Show" = "OscarlosShow"."""
    return re.sub(r"[\W_]+", "", name.lower())


def resolver(conn: sqlite3.Connection):
    """Função nome-como-escrito -> nome canônico do fórum (ou o próprio)."""
    by_lower: dict[str, Counter] = defaultdict(Counter)
    for (n, k) in conn.execute("SELECT author_name, COUNT(*) FROM posts GROUP BY 1"):
        if n:
            by_lower[n.lower()][n] += k
    for (n,) in conn.execute("SELECT name FROM members"):
        if n:
            by_lower[n.lower()][n] += 1
    for (n,) in conn.execute("SELECT name FROM person_names"):
        by_lower[n.lower()][n] += 1
    by_uid = dict(conn.execute("SELECT user_id, name FROM members"))

    def resolve(raw: str) -> str:
        n = _clean_name(raw)
        m = re.fullmatch(r"u(\d+)", n)
        if m and int(m.group(1)) in by_uid:
            return by_uid[int(m.group(1))]
        c = by_lower.get(n.lower()) or by_key.get(_key(n))
        return c.most_common(1)[0][0] if c else n
    by_key: dict[str, Counter] = defaultdict(Counter)
    for low, cnt in by_lower.items():
        by_key[_key(low)].update(cnt)
    return resolve


def load_claims(conn: sqlite3.Connection, claims: list[dict], source: str) -> int:
    """Grava alegações (formato da extração) em identity_claims; substitui as
    da mesma fonte."""
    conn.execute("DELETE FROM identity_claims WHERE source=?", (source,))
    resolve = resolver(conn)
    spelled: dict[str, Counter] = defaultdict(Counter)
    for c in claims:
        for x in c.get("nomes") or []:
            if x and x.strip():
                r = resolve(x)
                spelled[_key(r)][r] += 1
    canon = {r: cnt.most_common(1)[0][0] for cnt in spelled.values() for r in cnt}
    n = 0
    for c in claims:
        names = sorted({canon[resolve(x)] for x in c.get("nomes") or [] if x and x.strip()})
        if len(names) < 2:
            continue
        cur = conn.execute(
            "INSERT INTO identity_claims (post_id, kind, evidence, excerpt, source) "
            "VALUES (?,?,?,?,?)",
            (c["post_id"], c["tipo"], c.get("evidencia"), c.get("trecho"), source))
        conn.executemany("INSERT OR IGNORE INTO identity_claim_names VALUES (?,?)",
                         [(cur.lastrowid, x) for x in names])
        n += 1
    conn.commit()
    return n


def pair_status(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Situação final de cada par de nomes citado junto numa alegação."""
    rows = conn.execute(
        """SELECT c.id, c.kind, c.evidence, p.date, p.topic_id, c.post_id
           FROM identity_claims c JOIN posts p ON p.id = c.post_id
           ORDER BY p.date, c.id""").fetchall()
    names = defaultdict(list)
    for cid, n in conn.execute("SELECT claim_id, name FROM identity_claim_names"):
        names[cid].append(n)
    status: dict[tuple[str, str], dict] = {}
    for cid, kind, evidence, date, tid, post_id in rows:
        strong = kind in STRONG_KINDS or (kind == "afirma" and tid in DENUNCIA_TOPICS)
        for a, b in itertools.combinations(sorted(names[cid]), 2):
            st = status.setdefault((a, b), {"linked": False, "history": []})
            st["history"].append((date, kind, post_id))
            if kind == "nega":
                st["linked"] = False
            elif strong:
                st["linked"] = True
    return status


def groups(conn: sqlite3.Connection) -> list[set[str]]:
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), st in pair_status(conn).items():
        if st["linked"]:
            parent[find(a)] = find(b)
    out: dict[str, set] = defaultdict(set)
    for x in list(parent):
        out[find(x)].add(x)
    return sorted((g for g in out.values() if len(g) > 1), key=len, reverse=True)


def apply_groups(conn: sqlite3.Connection, groups_: list[set[str]],
                 certainty: str = "denúncia") -> list[tuple[str, list[str]]]:
    """Cria/atualiza persons. Grupo que já tem nome numa pessoa junta-se a ela
    (nomes já registrados mantêm a certeza que tinham)."""
    from .storage import Storage  # noqa: F401  (só para o tipo do conn)
    post_count = dict(conn.execute(
        "SELECT author_name, COUNT(*) FROM posts WHERE author_id IS NOT NULL GROUP BY 1"))
    mentions = dict(conn.execute(
        "SELECT name, COUNT(*) FROM identity_claim_names GROUP BY name"))
    joined = {}
    for n, j in conn.execute("SELECT name, joined_date FROM members"):
        if j and (n not in joined or j < joined[n]):
            joined[n] = j
    first_claim = dict(conn.execute(
        """SELECT n.name, MIN(p.date) FROM identity_claim_names n
           JOIN identity_claims c ON c.id = n.claim_id JOIN posts p ON p.id = c.post_id
           GROUP BY n.name"""))

    def usable(name: str) -> bool:
        if name in NOT_ACCOUNT_NAMES:
            return False
        # conta com esse nome criada depois da alegação: é outra pessoa
        j, d = joined.get(name), first_claim.get(name)
        return not (j and d and j > d[:10])

    done = []
    for g in groups_:
        g = {n for n in g if usable(n)}
        if len(g) < 2:
            continue
        existing = {pid for (pid,) in conn.execute(
            f"SELECT DISTINCT person_id FROM person_names WHERE name IN "
            f"({','.join('?' * len(g))})", sorted(g))}
        if len(existing) > 1:
            # junta pessoas que a denúncia mostra serem a mesma
            keep, *others = sorted(existing)
            for o in others:
                conn.execute("UPDATE person_names SET person_id=? WHERE person_id=?", (keep, o))
                conn.execute("DELETE FROM persons WHERE id=?", (o,))
            existing = {keep}
        if existing:
            pid = existing.pop()
        else:
            # rótulo: o nome mais citado nas alegações (o "principal" na fala da
            # comunidade); empate decidido pelo nº de mensagens
            label = max(g, key=lambda n: (mentions.get(n, 0), post_count.get(n, 0), n))
            conn.execute("INSERT OR IGNORE INTO persons (label, note) VALUES (?, ?)",
                         (label, "Agrupado por alegações no fórum (identity_claims)"))
            pid = conn.execute("SELECT id FROM persons WHERE label=?", (label,)).fetchone()[0]
        for n in g:
            conn.execute("INSERT OR IGNORE INTO person_names (name, person_id, certainty) "
                         "VALUES (?,?,?)", (n, pid, certainty))
        label = conn.execute("SELECT label FROM persons WHERE id=?", (pid,)).fetchone()[0]
        done.append((label, sorted(g)))
    conn.commit()
    return done


def claims_from_files(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        out += json.load(open(p, encoding="utf-8"))
    return out
