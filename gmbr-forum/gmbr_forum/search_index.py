"""Índice de busca (data/search.sqlite), derivado do banco do scraper e das
mensagens locais. O banco do scraper continua só leitura.

Tabelas:
  pm         metadados de cada mensagem (autor, data, fórum, sinais de conteúdo)
  post_fts   texto completo (título, texto próprio, texto citado) — FTS5
  tm         metadados de cada tópico (+ topic_fts com título/descrição)
  tp         participantes de cada tópico (quem postou, quantas vezes)
  quotes     quem citou quem, em qual mensagem
  likes      quem curtiu quem, em qual mensagem
  um         metadados de cada usuário (+ user_fts com nome e perfil)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from html import unescape

from . import compat

INDEX_VERSION = "5"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE pm (
    id          INTEGER PRIMARY KEY,   -- id da mensagem; negativo = mensagem local
    topic_ref   TEXT NOT NULL,         -- 't123' (arquivo) ou 'n5' (tópico novo)
    forum_id    INTEGER,
    author      TEXT,
    author_lc   TEXT,
    date        TEXT,
    position    INTEGER,
    likes       INTEGER DEFAULT 0,
    edited      INTEGER DEFAULT 0,
    has_img     INTEGER DEFAULT 0,
    has_link    INTEGER DEFAULT 0,
    has_code    INTEGER DEFAULT 0,
    has_quote   INTEGER DEFAULT 0,
    has_spoiler INTEGER DEFAULT 0,
    has_video   INTEGER DEFAULT 0,
    nchars      INTEGER DEFAULT 0,
    is_local    INTEGER DEFAULT 0,
    author_acc  TEXT,                  -- chave da conta (nicks antigos juntos)
    author_per  TEXT                   -- chave da pessoa (várias contas juntas)
);
CREATE TABLE post_text (id INTEGER PRIMARY KEY, title TEXT, body TEXT, quoted TEXT);
CREATE VIRTUAL TABLE post_fts USING fts5(
    title, body, quoted, content='post_text', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2');

CREATE TABLE tm (
    ref            TEXT PRIMARY KEY,
    rid            INTEGER UNIQUE,     -- rowid em topic_fts
    forum_id       INTEGER,
    title          TEXT,
    description    TEXT,
    author         TEXT,
    author_lc      TEXT,
    created        TEXT,
    last_date      TEXT,
    replies        INTEGER DEFAULT 0,
    views          INTEGER,
    n_posts        INTEGER DEFAULT 0,
    n_participants INTEGER DEFAULT 0,
    has_poll       INTEGER DEFAULT 0,
    poll_votes     INTEGER,
    locked         INTEGER DEFAULT 0,
    sticky         INTEGER DEFAULT 0,
    prefix         TEXT,
    is_local       INTEGER DEFAULT 0,
    author_acc     TEXT,
    author_per     TEXT
);
CREATE VIRTUAL TABLE topic_fts USING fts5(
    title, description, tokenize='unicode61 remove_diacritics 2');

CREATE TABLE tp (
    topic_ref  TEXT,
    author_lc  TEXT,
    n          INTEGER,
    first_date TEXT,
    last_date  TEXT,
    PRIMARY KEY (topic_ref, author_lc)
) WITHOUT ROWID;

CREATE TABLE quotes (post_id INTEGER, quoter_lc TEXT, quoted_lc TEXT,
                     quoter_acc TEXT, quoted_acc TEXT, quoter_per TEXT, quoted_per TEXT);
CREATE TABLE likes  (post_id INTEGER, liker_lc TEXT, liked_lc TEXT,
                     liker_acc TEXT, liked_acc TEXT, liker_per TEXT, liked_per TEXT);

-- Identidade: cada nome aponta para um nome representante da sua conta
-- (acc_key) e da sua pessoa (per_key). Sem informação, aponta para si mesmo.
CREATE TABLE ident (
    name_lc  TEXT PRIMARY KEY,
    name     TEXT,
    acc_key  TEXT,
    per_key  TEXT,
    source   TEXT              -- membro / nick antigo / pessoa:<certeza> / nome
);
-- participantes por chave de conta e de pessoa (mesmo formato de tp)
CREATE TABLE tp_acc (topic_ref TEXT, author_lc TEXT, n INTEGER, first_date TEXT, last_date TEXT,
                     PRIMARY KEY (topic_ref, author_lc)) WITHOUT ROWID;
CREATE TABLE tp_per (topic_ref TEXT, author_lc TEXT, n INTEGER, first_date TEXT, last_date TEXT,
                     PRIMARY KEY (topic_ref, author_lc)) WITHOUT ROWID;

CREATE TABLE um (
    name_lc        TEXT PRIMARY KEY,
    name           TEXT,
    rid            INTEGER UNIQUE,     -- rowid em user_fts
    user_id        INTEGER,
    groups         TEXT,               -- '|Lendas|Membros|'
    rank_title     TEXT,
    joined         TEXT,
    last_visit     TEXT,
    post_count     INTEGER,            -- contador do perfil
    reputation     INTEGER,
    posts_found    INTEGER DEFAULT 0,  -- mensagens no arquivo/locais
    topics_started INTEGER DEFAULT 0,
    topics_in      INTEGER DEFAULT 0,
    first_post     TEXT,
    last_post      TEXT,
    quotes_given   INTEGER DEFAULT 0,
    quotes_recv    INTEGER DEFAULT 0,
    likes_given    INTEGER DEFAULT 0,
    likes_recv     INTEGER DEFAULT 0,
    has_avatar     INTEGER DEFAULT 0,
    has_signature  INTEGER DEFAULT 0,
    has_website    INTEGER DEFAULT 0,
    is_local       INTEGER DEFAULT 0
);
CREATE VIRTUAL TABLE user_fts USING fts5(
    name, profile, signature, tokenize='unicode61 remove_diacritics 2');
"""

INDEXES = """
CREATE INDEX pm_topic   ON pm(topic_ref, position);
CREATE INDEX pm_author  ON pm(author_lc, date);
CREATE INDEX pm_forum   ON pm(forum_id, date);
CREATE INDEX pm_date    ON pm(date);
CREATE INDEX tm_forum   ON tm(forum_id, last_date);
CREATE INDEX tm_author  ON tm(author_lc);
CREATE INDEX tm_last    ON tm(last_date);
CREATE INDEX tp_author  ON tp(author_lc, topic_ref);
CREATE INDEX q_quoter   ON quotes(quoter_lc, quoted_lc);
CREATE INDEX q_quoted   ON quotes(quoted_lc, quoter_lc);
CREATE INDEX q_post     ON quotes(post_id);
CREATE INDEX l_liker    ON likes(liker_lc, liked_lc);
CREATE INDEX l_liked    ON likes(liked_lc, liker_lc);
CREATE INDEX l_post     ON likes(post_id);
CREATE INDEX pm_acc     ON pm(author_acc, date);
CREATE INDEX pm_per     ON pm(author_per, date);
CREATE INDEX tm_acc     ON tm(author_acc);
CREATE INDEX tm_per     ON tm(author_per);
CREATE INDEX tpa_author ON tp_acc(author_lc, topic_ref);
CREATE INDEX tpp_author ON tp_per(author_lc, topic_ref);
CREATE INDEX q_quoter_a ON quotes(quoter_acc, quoted_acc);
CREATE INDEX q_quoted_a ON quotes(quoted_acc, quoter_acc);
CREATE INDEX q_quoter_p ON quotes(quoter_per, quoted_per);
CREATE INDEX q_quoted_p ON quotes(quoted_per, quoter_per);
CREATE INDEX l_liker_a  ON likes(liker_acc, liked_acc);
CREATE INDEX l_liked_a  ON likes(liked_acc, liker_acc);
CREATE INDEX l_liker_p  ON likes(liker_per, liked_per);
CREATE INDEX l_liked_p  ON likes(liked_per, liker_per);
CREATE INDEX ident_acc  ON ident(acc_key);
CREATE INDEX ident_per  ON ident(per_key);
"""

# ------------------------------------------------------------------ texto/html
_TAG = re.compile(r"<[^>]+>")
_DROP = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
_BREAK = re.compile(r"<br\s*/?>|</(?:p|div|li|tr|h\d|dd|dt|pre)>", re.I)
_INNER_QUOTE = re.compile(r"<blockquote\b[^>]*>((?:(?!<blockquote\b).)*?)</blockquote>", re.S | re.I)
_CITE = re.compile(r"<cite>(.*?)</cite>", re.S | re.I)
_REAL_IMG = re.compile(r"<img\b(?![^>]*(?:emojione|longdesc=|/smiles/|/smilies/|icon_))", re.I)
_VIDEO = re.compile(r"youtube\.com|youtu\.be|<iframe|<video|<embed|vimeo\.com", re.I)
_CODE = re.compile(r"codebox|<code\b|<pre\b", re.I)
_SPOILER = re.compile(r"spoiler", re.I)
_LINK = re.compile(r"<a\s[^>]*href=", re.I)


def html_text(html: str) -> str:
    html = _DROP.sub(" ", html)
    html = _BREAK.sub("\n", html)
    txt = unescape(_TAG.sub(" ", html))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\s*\n\s*", "\n", txt)).strip()


def split_quotes(html: str) -> tuple[str, str, list[str]]:
    """Separa o texto próprio do texto citado e devolve quem foi citado."""
    quoted, names = [], []
    for _ in range(60):
        m = _INNER_QUOTE.search(html)
        if not m:
            break
        inner = m.group(1)
        for c in _CITE.findall(inner):
            n = html_text(c)
            n = re.sub(r"\s*escreveu\s*:?\s*$", "", n, flags=re.I).strip()
            if n and len(n) <= 60 and "escreveu" not in n.lower():
                names.append(n)
        quoted.append(html_text(_CITE.sub(" ", inner)))
        html = html[:m.start()] + " " + html[m.end():]
    return html_text(html), "\n".join(q for q in quoted if q), names


def content_flags(html: str) -> dict:
    return {
        "has_img": int(bool(_REAL_IMG.search(html))),
        "has_link": int(bool(_LINK.search(html))),
        "has_code": int(bool(_CODE.search(html))),
        "has_quote": int("<blockquote" in html.lower()),
        "has_spoiler": int(bool(_SPOILER.search(html))),
        "has_video": int(bool(_VIDEO.search(html))),
    }


GUESTS = ("convidado", "convidad")  # o fórum às vezes trunca "Convidado"


def lc(name: str | None) -> str | None:
    return name.strip().lower() if name else None


# ------------------------------------------------------------------- carimbo
def archive_stamp(archive: str) -> str:
    st = os.stat(archive)
    return f"v{INDEX_VERSION}:{st.st_size}:{int(st.st_mtime)}"


def needs_rebuild(archive: str, index: str) -> bool:
    if not os.path.exists(index):
        return True
    try:
        c = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
        row = c.execute("SELECT value FROM meta WHERE key='stamp'").fetchone()
        c.close()
    except sqlite3.Error:
        return True
    return not row or row[0] != archive_stamp(archive)


# --------------------------------------------------------------------- build
def build(archive: str, local: str, out: str, log=print) -> None:
    t0 = time.time()
    tmp = out + ".tmp"
    for f in (tmp, tmp + "-wal", tmp + "-shm", tmp + "-journal"):
        if os.path.exists(f):
            os.remove(f)
    db = sqlite3.connect(f"file:{tmp}", uri=True)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA cache_size=-200000")
    db.executescript(SCHEMA)
    src = sqlite3.connect(f"file:{archive}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    if compat.needs_compat(src):
        # schema 2+ do scraper: VIEWs com o formato antigo (ver compat.py),
        # e as tabelas c_* vão também para o índice, para o servidor usar.
        log("Banco no schema novo: preparando compatibilidade…")
        compat.prepare(src)
        db.execute("ATTACH DATABASE ? AS a", (f"file:{archive}?mode=ro",))
        compat.materialize(db, "a", "main")
        db.commit()
        db.execute("DETACH DATABASE a")
    loc = sqlite3.connect(local) if os.path.exists(local) else None
    if loc:
        loc.row_factory = sqlite3.Row

    def cols(conn, t):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}

    tcols, mcols = cols(src, "topics"), cols(src, "members")
    has_polls = bool(cols(src, "polls"))
    topic_forum = {r[0]: r[1] for r in src.execute("SELECT id, forum_id FROM topics")}

    # ---------------------------------------------------------------- mensagens
    log("Indexando mensagens…")
    n = 0
    batch_pm, batch_fts, batch_txt, batch_q, batch_l = [], [], [], [], []

    def flush():
        db.executemany("INSERT INTO pm VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)", batch_pm)
        db.executemany("INSERT INTO post_fts(rowid, title, body, quoted) VALUES (?,?,?,?)", batch_fts)
        db.executemany("INSERT INTO post_text VALUES (?,?,?,?)", batch_txt)
        db.executemany("INSERT INTO quotes (post_id, quoter_lc, quoted_lc) VALUES (?,?,?)", batch_q)
        db.executemany("INSERT INTO likes (post_id, liker_lc, liked_lc) VALUES (?,?,?)", batch_l)
        for b in (batch_pm, batch_fts, batch_txt, batch_q, batch_l):
            b.clear()

    for r in src.execute(
            """SELECT id, topic_id, position, title, author_name, date, edit_count, edit_info,
                      likes, liked_by, content_html FROM posts"""):
        html = r["content_html"] or ""
        body, quoted, qnames = split_quotes(html)
        fl = content_flags(html)
        a_lc = lc(r["author_name"])
        batch_pm.append((r["id"], f"t{r['topic_id']}", topic_forum.get(r["topic_id"]),
                         r["author_name"], a_lc, r["date"], r["position"], r["likes"] or 0,
                         int(bool(r["edit_count"] or r["edit_info"])),
                         fl["has_img"], fl["has_link"], fl["has_code"], fl["has_quote"],
                         fl["has_spoiler"], fl["has_video"], len(body), 0))
        batch_fts.append((r["id"], r["title"] or "", body, quoted))
        batch_txt.append((r["id"], r["title"] or "", body, quoted))
        for q in dict.fromkeys(qnames):
            if lc(q) != a_lc:
                batch_q.append((r["id"], a_lc, lc(q)))
        try:
            for liker in json.loads(r["liked_by"] or "[]"):
                if isinstance(liker, str) and liker.strip():
                    batch_l.append((r["id"], lc(liker), a_lc))
        except ValueError:
            pass
        n += 1
        if len(batch_pm) >= 5000:
            flush()
            log(f"  {n} mensagens")
    flush()

    local_topics = {}
    if loc:
        local_topics = {r["id"]: r for r in loc.execute("SELECT * FROM new_topics")}
        for p in loc.execute("SELECT * FROM new_posts ORDER BY id"):
            ref = p["topic_ref"]
            fid = (topic_forum.get(int(ref[1:])) if ref[0] == "t"
                   else (local_topics.get(int(ref[1:])) or {"forum_id": None})["forum_id"])
            _index_local_post(db, p["id"], ref, fid, p["author"], p["body"], p["created"])
    log(f"  {n} mensagens do arquivo indexadas")

    # ---------------------------------------------------------- participantes
    log("Calculando participantes e tópicos…")
    db.execute("""INSERT INTO tp SELECT topic_ref, author_lc, COUNT(*), MIN(date), MAX(date)
                  FROM pm WHERE author_lc IS NOT NULL GROUP BY topic_ref, author_lc""")
    stats = {r[0]: r[1:] for r in db.execute(
        """SELECT topic_ref, COUNT(*), MIN(CASE WHEN position=0 THEN date END), MIN(date), MAX(date)
           FROM pm GROUP BY topic_ref""")}
    parts = dict(db.execute("SELECT topic_ref, COUNT(*) FROM tp GROUP BY topic_ref").fetchall())
    polls = {}
    if has_polls:
        polls = {r[0]: r[1] for r in src.execute("SELECT topic_id, total_votes FROM polls")}

    opt = lambda c: c if c in tcols else "NULL"
    rid = 0
    rows, fts = [], []
    for t in src.execute(f"""SELECT id, forum_id, title, description, author_name, replies, views,
                                    last_post_date, section, {opt('prefix')} AS prefix,
                                    {opt('topic_type')} AS topic_type FROM topics"""):
        ref = f"t{t['id']}"
        rid += 1
        cnt, first0, first, last = stats.get(ref, (0, None, None, None))
        ttype = t["topic_type"] or ""
        sticky = int("sticky" in ttype or "announce" in ttype or
                     bool(t["section"] and ("Fixo" in t["section"] or "Anúncio" in t["section"])))
        rows.append((ref, rid, t["forum_id"], t["title"], t["description"], t["author_name"],
                     lc(t["author_name"]), first0 or first,
                     max(filter(None, [t["last_post_date"], last]), default=None),
                     t["replies"] or max(cnt - 1, 0), t["views"], cnt, parts.get(ref, 0),
                     int(t["id"] in polls), polls.get(t["id"]), int("locked" in ttype), sticky,
                     t["prefix"], 0))
        fts.append((rid, t["title"] or "", t["description"] or ""))
    for nid, t in local_topics.items():
        ref = f"n{nid}"
        rid += 1
        cnt, first0, first, last = stats.get(ref, (0, None, t["created"], t["created"]))
        rows.append((ref, rid, t["forum_id"], t["title"], None, t["author"], lc(t["author"]),
                     first0 or first or t["created"], last or t["created"], max(cnt - 1, 0), None,
                     cnt, parts.get(ref, 0), 0, None, 0, 0, None, 1))
        fts.append((rid, t["title"], ""))
    db.executemany(f"INSERT INTO tm VALUES ({','.join('?' * 19)},NULL,NULL)", rows)
    db.executemany("INSERT INTO topic_fts(rowid, title, description) VALUES (?,?,?)", fts)

    # ------------------------------------------------------------------ usuários
    log("Indexando usuários…")
    agg = {r[0]: r[1:] for r in db.execute(
        "SELECT author_lc, MAX(author), COUNT(*), MIN(date), MAX(date) FROM pm "
        "WHERE author_lc IS NOT NULL GROUP BY author_lc")}
    started = dict(db.execute("SELECT author_lc, COUNT(*) FROM tm WHERE author_lc IS NOT NULL GROUP BY 1"))
    t_in = dict(db.execute("SELECT author_lc, COUNT(*) FROM tp GROUP BY 1"))
    qg = dict(db.execute("SELECT quoter_lc, COUNT(*) FROM quotes GROUP BY 1"))
    qr = dict(db.execute("SELECT quoted_lc, COUNT(*) FROM quotes GROUP BY 1"))
    lg = dict(db.execute("SELECT liker_lc, COUNT(*) FROM likes GROUP BY 1"))
    lr = dict(db.execute("SELECT liked_lc, COUNT(*) FROM likes GROUP BY 1"))
    groups_by_class = {r[0]: r[1] for r in src.execute("SELECT class, name FROM groups")}
    mopt = lambda c: c if c in mcols else "NULL"
    all_groups, all_ranks = set(), set()
    urows, ufts, seen = [], [], set()
    rid = 0

    def add_user(name, m=None):
        nonlocal rid
        key = lc(name)
        if not key or key in seen:
            return
        seen.add(key)
        rid += 1
        a = agg.get(key, (name, 0, None, None))
        groups, profile = [], []
        if m is not None:
            if m["group_class"] and groups_by_class.get(m["group_class"]):
                groups.append(groups_by_class[m["group_class"]])
            try:
                for g in json.loads(m["profile_groups"] or "[]"):
                    if isinstance(g, dict) and g.get("name"):
                        groups.append(g["name"])
            except ValueError:
                pass
            for raw in (m["profile_fields"], m["extra_fields"]):
                try:
                    for k, v in json.loads(raw or "{}").items():
                        if isinstance(v, dict):
                            v = v.get("text")
                        if v and str(v) not in ("0", ""):
                            profile.append(f"{k}: {v}")
                except ValueError:
                    pass
            if m["humor"]:
                profile.append(m["humor"])
            if m["website"]:
                profile.append(m["website"])
        groups = list(dict.fromkeys(groups))
        all_groups.update(groups)
        rank = m["rank_title"] if m is not None else None
        if rank:
            all_ranks.add(rank)
        urows.append((
            key, m["name"] if m is not None else a[0], rid,
            m["user_id"] if m is not None else None,
            ("|" + "|".join(groups) + "|") if groups else None, rank,
            m["joined_date"] if m is not None else None,
            m["last_visit"] if m is not None else None,
            m["post_count"] if m is not None else None,
            m["reputation"] if m is not None else None,
            a[1], started.get(key, 0), t_in.get(key, 0), a[2], a[3],
            qg.get(key, 0), qr.get(key, 0), lg.get(key, 0), lr.get(key, 0),
            int(bool(m is not None and m["avatar_url"] and "s2bg4k" not in m["avatar_url"])),
            int(bool(m is not None and m["signature_html"])),
            int(bool(m is not None and m["website"])),
            int(m is None)))
        sig = html_text(m["signature_html"]) if m is not None and m["signature_html"] else ""
        ufts.append((rid, name, "\n".join(profile), sig))

    for m in src.execute(f"""SELECT name, user_id, group_class, rank_title, joined_date, post_count,
                                    reputation, avatar_url, signature_html, website, extra_fields,
                                    {mopt('humor')} AS humor, {mopt('last_visit')} AS last_visit,
                                    {mopt('profile_fields')} AS profile_fields,
                                    {mopt('profile_groups')} AS profile_groups
                             FROM members WHERE COALESCE(is_guest, 0) = 0"""):
        add_user(m["name"], m)
    for key, a in agg.items():  # autores sem perfil (ex.: só mensagens locais)
        if key not in seen and key != "convidado":
            add_user(a[0])
    db.executemany(f"INSERT INTO um VALUES ({','.join('?' * 23)})", urows)
    db.executemany("INSERT INTO user_fts(rowid, name, profile, signature) VALUES (?,?,?,?)", ufts)

    # ---------------------------------------------------------------- identidade
    log("Juntando nomes da mesma conta e da mesma pessoa…")
    build_ident(db, src)

    # ------------------------------------------------------------------- final
    log("Criando índices…")
    db.executescript(INDEXES)
    prefixes = [r[0] for r in db.execute(
        "SELECT prefix FROM tm WHERE prefix IS NOT NULL GROUP BY prefix ORDER BY COUNT(*) DESC")]
    years = [r[0] for r in db.execute(
        "SELECT DISTINCT substr(date,1,4) FROM pm WHERE date IS NOT NULL ORDER BY 1")]
    for k, v in {"stamp": archive_stamp(archive), "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "groups": json.dumps(sorted(all_groups), ensure_ascii=False),
                 "ranks": json.dumps(sorted(all_ranks), ensure_ascii=False),
                 "prefixes": json.dumps(prefixes, ensure_ascii=False),
                 "years": json.dumps(years)}.items():
        db.execute("INSERT INTO meta VALUES (?,?)", (k, v))
    db.execute("INSERT INTO post_fts(post_fts) VALUES ('optimize')")
    db.commit()
    db.execute("ANALYZE")
    db.close()
    src.close()
    if loc:
        loc.close()
    for f in (out + "-wal", out + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    os.replace(tmp, out)
    log(f"Índice pronto em {time.time() - t0:.0f}s: {out} "
        f"({os.path.getsize(out) / 1e6:.0f} MB)")


# ---------------------------------------------------------------- identidade
def build_ident(db, src) -> None:
    """Liga nomes da mesma conta (nicks antigos) e da mesma pessoa (várias
    contas), escolhendo um nome representante para cada grupo."""
    names_in_src = {r[0] for r in src.execute("SELECT name FROM sqlite_master")}
    members = {}  # name_lc -> (name, user_id)
    joined = {}   # name_lc -> data de inscrição
    for name, uid, jd in src.execute(
            "SELECT name, user_id, joined_date FROM members WHERE COALESCE(is_guest, 0) = 0"):
        if name:
            members.setdefault(lc(name), (name, uid))
            if jd:
                joined.setdefault(lc(name), jd)
    by_uid = {uid: n_lc for n_lc, (_, uid) in members.items() if uid is not None}
    posts = dict(db.execute("SELECT name_lc, posts_found FROM um"))
    display: dict[str, str] = {}
    for n_lc, (n, _) in members.items():
        display[n_lc] = n
    for (n,) in db.execute("SELECT DISTINCT author FROM pm WHERE author IS NOT NULL"):
        display.setdefault(lc(n), n)
    # nomes que só aparecem em citações/curtidas (já em minúsculas): grafia
    # provisória, trocada pela do histórico de nicks/pessoas quando houver
    lowercase_only = set()
    for table, cols in (("quotes", ("quoter_lc", "quoted_lc")), ("likes", ("liker_lc", "liked_lc"))):
        for col in cols:
            for (n,) in db.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL"):
                if n not in display:
                    display[n] = n
                    lowercase_only.add(n)

    def nice(n: str, name: str):
        if n not in display or n in lowercase_only:
            display[n] = name
            lowercase_only.discard(n)

    # 1) conta: nick antigo -> nome atual da conta
    acc = {n: n for n in display}
    source = {n: ("membro" if n in members else "nome") for n in display}
    # nick antigo que depois virou nome de OUTRA conta (recadastro): as citações
    # anteriores à criação dessa conta são do dono antigo do nick
    reused: list[tuple[str, str, str]] = []  # (nome, conta antiga, data de criação da conta nova)
    if "account_aliases" in names_in_src:
        for name, uid in src.execute("SELECT name, user_id FROM account_aliases"):
            n, target = lc(name), by_uid.get(uid)
            if not n or not target or n in GUESTS or n == target:
                continue
            if n in members:
                if joined.get(n):
                    reused.append((n, target, joined[n]))
                continue
            nice(n, name)
            acc[n] = target
            source[n] = "nick antigo"

    # 2) pessoa: contas ligadas em persons/person_names
    per = dict(acc)
    if {"persons", "person_names"} <= names_in_src:
        groups: dict[int, list[tuple[str, str]]] = {}
        for name, pid, certainty in src.execute("SELECT name, person_id, certainty FROM person_names"):
            n = lc(name)
            if not n or n in GUESTS:
                continue
            nice(n, name)
            acc.setdefault(n, n)
            source.setdefault(n, "nome")
            groups.setdefault(pid, []).append((n, certainty or ""))
        for pid, items in groups.items():
            keys = list(dict.fromkeys(acc[n] for n, _ in items))
            # representante: a conta (membro) com mais mensagens; senão o nome com mais mensagens
            rep_key = max(keys, key=lambda k: (k in members, posts.get(k, 0), -keys.index(k)))
            cert = {acc[n]: c for n, c in items}
            for n in list(acc):
                if acc[n] in keys:
                    per[n] = rep_key
                    if n not in members and source[n] == "nome":
                        source[n] = f"pessoa: {cert.get(acc[n], '')}".strip()
            for n, c in items:
                per[n] = rep_key
                if n not in members and source.get(n) == "nome":
                    source[n] = f"pessoa: {c}"
    db.executemany("INSERT OR REPLACE INTO ident VALUES (?,?,?,?,?)",
                   [(n, display.get(n, n), acc.get(n, n), per.get(n, acc.get(n, n)), source.get(n, "nome"))
                    for n in display])
    # propaga as chaves para as tabelas de correlação
    db.execute("CREATE TEMP TABLE ik AS SELECT name_lc, acc_key, per_key FROM ident")
    db.execute("CREATE UNIQUE INDEX temp.ik_pk ON ik(name_lc)")
    for table, cols in (("pm", ["author"]), ("tm", ["author"]),
                        ("quotes", ["quoter", "quoted"]), ("likes", ["liker", "liked"])):
        sets = ", ".join(
            f"{c}_acc = COALESCE((SELECT acc_key FROM ik WHERE ik.name_lc = {table}.{c}_lc), {c}_lc), "
            f"{c}_per = COALESCE((SELECT per_key FROM ik WHERE ik.name_lc = {table}.{c}_lc), {c}_lc)"
            for c in cols)
        db.execute(f"UPDATE {table} SET {sets}")
    for n, target, since in reused:
        db.execute("""UPDATE quotes SET quoted_acc = ?, quoted_per = ?
                      WHERE quoted_lc = ? AND post_id IN (SELECT id FROM pm WHERE date < ?)""",
                   (target, per.get(target, target), n, since))
    for table, key in (("tp_acc", "author_acc"), ("tp_per", "author_per")):
        db.execute(f"""INSERT INTO {table}
                       SELECT topic_ref, {key}, COUNT(*), MIN(date), MAX(date) FROM pm
                       WHERE {key} IS NOT NULL GROUP BY topic_ref, {key}""")
    db.execute("DROP TABLE temp.ik")


def ensure_ident(db, name: str | None) -> tuple[str | None, str | None]:
    """Chaves de conta/pessoa de um nome (cria a entrada se for nome novo)."""
    n = lc(name)
    if not n:
        return None, None
    row = db.execute("SELECT acc_key, per_key FROM ident WHERE name_lc = ?", (n,)).fetchone()
    if row:
        return row[0], row[1]
    db.execute("INSERT INTO ident VALUES (?,?,?,?,?)", (n, name, n, n, "nome"))
    return n, n


# ------------------------------------------------------- mensagens locais
def _index_local_post(db, local_id, ref, forum_id, author, body, created):
    pid = -int(local_id)
    a_lc = lc(author)
    pos = db.execute("SELECT COALESCE(MAX(position) + 1, 0) FROM pm WHERE topic_ref = ?",
                     (ref,)).fetchone()[0]
    acc, per = ensure_ident(db, author)
    db.execute("INSERT INTO pm VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (pid, ref, forum_id, author, a_lc, created, pos, 0, 0, 0,
                int(bool(re.search(r"https?://", body))), 0, 0, 0,
                int(bool(_VIDEO.search(body))), len(body), 1, acc, per))
    db.execute("INSERT INTO post_fts(rowid, title, body, quoted) VALUES (?,?,?,?)", (pid, "", body, ""))
    db.execute("INSERT INTO post_text VALUES (?,?,?,?)", (pid, "", body, ""))


def add_local_post(index_path: str, local_id: int, ref: str, forum_id, author: str,
                   body: str, created: str, new_topic_title: str | None = None):
    """Atualiza o índice com uma mensagem nova, sem reconstruir tudo."""
    if not os.path.exists(index_path):
        return
    db = sqlite3.connect(index_path, timeout=30)
    try:
        with db:
            _index_local_post(db, local_id, ref, forum_id, author, body, created)
            a_lc = lc(author)
            acc, per = ensure_ident(db, author)
            for table, key in (("tp", a_lc), ("tp_acc", acc), ("tp_per", per)):
                db.execute(f"""INSERT INTO {table} VALUES (?,?,1,?,?) ON CONFLICT(topic_ref, author_lc)
                               DO UPDATE SET n = n + 1, last_date = excluded.last_date""",
                           (ref, key, created, created))
            if new_topic_title is not None:
                rid = (db.execute("SELECT COALESCE(MAX(rid), 0) + 1 FROM tm").fetchone()[0])
                acc, per = ensure_ident(db, author)
                db.execute(f"INSERT INTO tm VALUES ({','.join('?' * 21)})",
                           (ref, rid, forum_id, new_topic_title, None, author, a_lc, created,
                            created, 0, None, 0, 0, 0, None, 0, 0, None, 1, acc, per))
                db.execute("INSERT INTO topic_fts(rowid, title, description) VALUES (?,?,?)",
                           (rid, new_topic_title, ""))
            db.execute("""UPDATE tm SET n_posts = n_posts + 1, last_date = ?,
                          replies = CASE WHEN n_posts > 0 THEN replies + 1 ELSE replies END,
                          n_participants = (SELECT COUNT(*) FROM tp WHERE topic_ref = ?)
                          WHERE ref = ?""", (created, ref, ref))
            if new_topic_title is not None:
                db.execute("UPDATE um SET topics_started = topics_started + 1 WHERE name_lc = ?", (a_lc,))
            cur = db.execute("""UPDATE um SET posts_found = posts_found + 1, last_post = ?,
                                first_post = COALESCE(first_post, ?),
                                topics_in = (SELECT COUNT(*) FROM tp WHERE author_lc = ?)
                                WHERE name_lc = ?""", (created, created, a_lc, a_lc))
            if cur.rowcount == 0:
                rid = db.execute("SELECT COALESCE(MAX(rid), 0) + 1 FROM um").fetchone()[0]
                db.execute(f"INSERT INTO um VALUES ({','.join('?' * 23)})",
                           (a_lc, author, rid, None, None, None, None, None, None, None, 1,
                            int(new_topic_title is not None), 1, created, created,
                            0, 0, 0, 0, 0, 0, 0, 1))
                db.execute("INSERT INTO user_fts(rowid, name, profile, signature) VALUES (?,?,?,?)",
                           (rid, author, "", ""))
    finally:
        db.close()
