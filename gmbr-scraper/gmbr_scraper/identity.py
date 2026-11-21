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
STRONG_KINDS = ("confirma_staff", "confirma_admin")


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
    # alegações do administrador (sem mensagem) contam como as mais recentes
    rows = conn.execute(
        """SELECT c.id, c.kind, c.evidence, COALESCE(p.date, '9999-12-31') AS d,
                  p.topic_id, c.post_id
           FROM identity_claims c LEFT JOIN posts p ON p.id = c.post_id
           ORDER BY d, c.id""").fetchall()
    names = defaultdict(list)
    for cid, n in conn.execute("SELECT claim_id, name FROM identity_claim_names"):
        names[cid].append(n)
    status: dict[tuple[str, str], dict] = {}
    for cid, kind, evidence, date, tid, post_id in rows:
        strong = kind in STRONG_KINDS or (kind == "afirma" and tid in DENUNCIA_TOPICS)
        for a, b in itertools.combinations(sorted(names[cid]), 2):
            st = status.setdefault((a, b), {"linked": False, "history": []})
            st["history"].append((date, kind, post_id))
            if st.get("admin"):
                continue  # o que o administrador informou prevalece
            if kind == "confirma_admin":
                st["admin"] = True
            if kind == "nega":
                st["linked"] = False
            elif strong:
                st["linked"] = True
    return status


def save_pairs(conn: sqlite3.Connection) -> Counter:
    """Grava identity_pairs: confirmado (agrupa), suspeita (indícios sem
    vínculo firme, inclusive confirmações desfeitas por negação posterior) ou
    negado (só negações)."""
    conn.execute("DELETE FROM identity_pairs")
    out = Counter()
    for (a, b), st in pair_status(conn).items():
        kinds = {h[1] for h in st["history"]}
        if st["linked"]:
            status = "confirmado"
        elif kinds - {"nega"}:
            status = "suspeita"
        else:
            status = "negado"
        out[status] += 1
        conn.execute("INSERT INTO identity_pairs VALUES (?,?,?,?,?)",
                     (a, b, status, len(st["history"]), st["history"][-1][2]))
    conn.commit()
    return out


def add_admin_claim(conn: sqlite3.Connection, names: list[str], note: str) -> None:
    """Registra o que o administrador do fórum informou (prevalece sobre as
    alegações das mensagens)."""
    cur = conn.execute(
        "INSERT INTO identity_claims (post_id, kind, evidence, excerpt, source) "
        "VALUES (NULL, 'confirma_admin', 'administrador', ?, 'administrador')", (note,))
    conn.executemany("INSERT OR IGNORE INTO identity_claim_names VALUES (?,?)",
                     [(cur.lastrowid, n) for n in names])
    conn.commit()


def detect_renames(conn: sqlite3.Connection, min_quotes: int = 3,
                   min_share: float = 0.8) -> int:
    """Nicks antigos por citação: "X escreveu:" cujo texto está numa mensagem
    anterior do mesmo tópico hoje assinada pela conta Y (o fórum guarda na
    citação o nome da época). Também importa members.nick_history."""
    from bs4 import BeautifulSoup

    def norm(t):
        return " ".join(re.findall(r"\w+", (t or "").lower()))

    topic_posts: dict[int, list] = defaultdict(list)
    for tid, pos, a, aid, t in conn.execute(
            "SELECT topic_id, position, author_name, author_id, content_text FROM posts"):
        topic_posts[tid].append((pos, a, aid, norm(t)))
    match: dict[str, Counter] = defaultdict(Counter)
    for tid, pos, html in conn.execute(
            "SELECT topic_id, position, content_html FROM posts "
            "WHERE content_html LIKE '%escreveu:</cite>%'"):
        for bq in BeautifulSoup(html, "lxml").find_all("blockquote"):
            cite = bq.find("cite")
            m = cite and re.match(r"(.+?)\s+escreveu:\s*$", cite.get_text(" ").strip(), re.S)
            if not m:
                continue
            name = m.group(1).strip()
            clone = BeautifulSoup(str(bq), "lxml").find("blockquote")
            clone.find("cite").extract()
            for inner in clone.find_all("blockquote"):
                inner.extract()
            qt = norm(clone.get_text(" "))[:60]
            if len(qt) < 20:
                continue
            authors = {(a, aid) for p, a, aid, t in topic_posts[tid] if p < pos and qt in t}
            if len(authors) == 1:
                a, aid = authors.pop()
                match[name][aid if a != name else 0] += 1
    conn.execute("DELETE FROM account_aliases WHERE source IN ('citações', 'nick_history')")
    n = 0
    for name, cnt in match.items():
        aid, k = cnt.most_common(1)[0]
        if aid and k >= min_quotes and k >= min_share * sum(cnt.values()):
            conn.execute("INSERT OR REPLACE INTO account_aliases VALUES (?,?,?,?)",
                         (name, aid, "citações", k))
            n += 1
    for uid, name, hist in conn.execute(
            "SELECT user_id, name, nick_history FROM members WHERE nick_history IS NOT NULL"
            ).fetchall():
        for old in (x.strip() for x in hist.split("->")):
            if old and old != name:
                conn.execute("INSERT OR IGNORE INTO account_aliases VALUES (?,?,?,NULL)",
                             (old, uid, "nick_history"))
                n += 1
    conn.commit()
    return n


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
    admin_names = {n for (n,) in conn.execute(
        """SELECT n.name FROM identity_claim_names n JOIN identity_claims c
           ON c.id = n.claim_id WHERE c.kind = 'confirma_admin'""")}
    mentions = dict(conn.execute(
        "SELECT name, COUNT(*) FROM identity_claim_names GROUP BY name"))
    joined = {}
    for n, j in conn.execute("SELECT name, joined_date FROM members"):
        if j and (n not in joined or j < joined[n]):
            joined[n] = j
    last_claim = dict(conn.execute(
        """SELECT n.name, MAX(p.date) FROM identity_claim_names n
           JOIN identity_claims c ON c.id = n.claim_id JOIN posts p ON p.id = c.post_id
           GROUP BY n.name"""))

    def usable(name: str) -> bool:
        if name in admin_names:
            return True
        if name in NOT_ACCOUNT_NAMES:
            return False
        # conta com esse nome criada depois de TODAS as alegações sobre ele: é
        # outra pessoa (quem voltou com o mesmo nome tem alegação posterior)
        j, d = joined.get(name), last_claim.get(name)
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
                         "VALUES (?,?,?)", (n, pid, "informado" if n in admin_names
                                            else certainty))
            if n in admin_names:
                conn.execute("UPDATE person_names SET certainty='informado' WHERE name=?", (n,))
        label = conn.execute("SELECT label FROM persons WHERE id=?", (pid,)).fetchone()[0]
        done.append((label, sorted(g)))
    conn.commit()
    return done


def claims_from_files(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        out += json.load(open(p, encoding="utf-8"))
    return out
