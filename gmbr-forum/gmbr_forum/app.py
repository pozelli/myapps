"""Servidor do fórum: lê o banco do gmbr-scraper (só leitura) e guarda as
mensagens novas num banco local separado."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import ceil
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import compat
from .sanitize import clean_html

TOPICS_PER_PAGE = 40
POSTS_PER_PAGE = 25
ORIGIN = "https://gmbr.forumeiros.com"

LOCAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS new_topics (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    forum_id  INTEGER NOT NULL,
    title     TEXT NOT NULL,
    author    TEXT NOT NULL,
    created   TEXT NOT NULL
);
-- topic_ref: 't<id>' = tópico do arquivo, 'n<id>' = tópico novo
CREATE TABLE IF NOT EXISTS new_posts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_ref TEXT NOT NULL,
    author    TEXT NOT NULL,
    body      TEXT NOT NULL,
    created   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_new_posts_ref ON new_posts(topic_ref);
"""


def e(v) -> str:
    return escape("" if v is None else str(v))


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def fmt_date(iso: str | None, raw: str | None = None) -> str:
    if iso:
        try:
            return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M")
        except ValueError:
            pass
    return raw or ""


def text_to_html(body: str) -> str:
    """Mensagens novas são texto puro; links viram <a>."""
    out = []
    for para in re.split(r"\n{2,}", body.strip()):
        p = e(para).replace("\n", "<br>")
        p = re.sub(r"(https?://[^\s<&]+)",
                   r'<a href="\1" rel="nofollow noopener noreferrer" target="_blank">\1</a>', p)
        out.append(f"<p>{p}</p>")
    return "".join(out)


class Forum:
    def __init__(self, archive_path: str, local_path: str, index_path: str | None = None):
        self.archive_path = archive_path
        self.local_path = local_path
        self.index_path = index_path
        self._tls = threading.local()
        self._write_lock = threading.Lock()
        with sqlite3.connect(local_path) as c:
            c.executescript(LOCAL_SCHEMA)
        # Colunas/tabelas que só existem em versões mais novas do scraper
        ro = sqlite3.connect(f"file:{quote(archive_path)}?mode=ro", uri=True)
        self.compat = compat.needs_compat(ro)
        names = {r[0] for r in ro.execute("SELECT name FROM sqlite_master")}
        # Identidade (schema 3+): autores prováveis de mensagens de convidado,
        # nicks antigos e contas da mesma pessoa
        self.has_guesses = "v_post_author_best" in names
        self.has_aliases = "account_aliases" in names
        self.has_persons = {"persons", "person_names"} <= names
        if self.compat:  # schema 2+: as VIEWs de compat.py têm todas as colunas
            topic_cols = {"prefix", "topic_type"}
            member_cols = {"humor", "last_visit_raw", "profile_fields", "profile_groups"}
            self.has_polls = True
        else:
            cols = lambda t: {r[1] for r in ro.execute(f"PRAGMA table_info({t})")}
            topic_cols, member_cols = cols("topics"), cols("members")
            self.has_polls = bool(cols("polls")) and bool(cols("poll_options"))
        ro.close()
        opt = lambda name, have: f"t.{name}" if name in have else f"NULL"
        self.topic_extra = (f"{opt('prefix', topic_cols)} AS prefix, "
                            f"{opt('topic_type', topic_cols)} AS topic_type")
        self.member_extra = ", ".join(
            f"m.{c}" if c in member_cols else f"NULL AS {c}"
            for c in ("humor", "last_visit_raw", "profile_fields", "profile_groups"))

    @property
    def db(self) -> sqlite3.Connection:
        c = getattr(self._tls, "conn", None)
        if c is None:
            uri = f"file:{quote(self.archive_path)}?mode=ro"
            c = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("ATTACH DATABASE ? AS loc", (self.local_path,))
            has_index = bool(self.index_path and os.path.exists(self.index_path))
            if has_index:
                c.execute("ATTACH DATABASE ? AS s", (self.index_path,))
            if self.compat:
                compat.prepare(c, "s" if has_index else None)
            self._tls.conn = c
        return c

    # --------------------------------------------------------------- consultas
    def forums(self):
        return self.db.execute("SELECT * FROM forums ORDER BY id").fetchall()

    def forum(self, fid: int):
        return self.db.execute("SELECT * FROM forums WHERE id=?", (fid,)).fetchone()

    def forum_stats(self) -> dict[int, dict]:
        stats: dict[int, dict] = {}
        for r in self.db.execute(
                """SELECT forum_id, COUNT(*) n, SUM(COALESCE(replies,0)) r,
                          MAX(last_post_date) last FROM topics GROUP BY forum_id"""):
            stats[r["forum_id"]] = {"topics": r["n"], "posts": r["n"] + (r["r"] or 0),
                                    "last": r["last"]}
        for r in self.db.execute(
                """SELECT t.forum_id, COUNT(DISTINCT t.id) n, MAX(t.created) last
                   FROM loc.new_topics t GROUP BY t.forum_id"""):
            s = stats.setdefault(r["forum_id"], {"topics": 0, "posts": 0, "last": None})
            s["topics"] += r["n"]
            s["last"] = max(filter(None, [s["last"], r["last"]]), default=None)
        return stats

    def last_topic(self, fid: int):
        return self.db.execute(
            """SELECT id, title, last_post_author, last_post_date, last_post_date_raw
               FROM topics WHERE forum_id=? ORDER BY last_post_date DESC LIMIT 1""",
            (fid,)).fetchone()

    @property
    def TOPIC_UNION(self) -> str:
        return f"""
        SELECT 't' || t.id AS ref, {self.topic_extra}, t.id, t.title, t.description, t.author_name AS author,
               t.section,
               COALESCE(t.replies, 0) + (SELECT COUNT(*) FROM loc.new_posts p
                                         WHERE p.topic_ref = 't' || t.id) AS replies,
               t.views, t.completed, COALESCE(t.posts_scraped, 0) AS scraped,
               MAX(COALESCE(t.last_post_date, ''),
                   COALESCE((SELECT MAX(created) FROM loc.new_posts p
                             WHERE p.topic_ref = 't' || t.id), '')) AS last_date,
               t.last_post_author AS last_author, t.forum_id
        FROM topics t
        UNION ALL
        SELECT 'n' || n.id, NULL, NULL, n.id, n.title, NULL, n.author, NULL,
               (SELECT COUNT(*) FROM loc.new_posts p WHERE p.topic_ref = 'n' || n.id) - 1,
               NULL, 1, 0,
               (SELECT MAX(created) FROM loc.new_posts p WHERE p.topic_ref = 'n' || n.id),
               (SELECT author FROM loc.new_posts p WHERE p.topic_ref = 'n' || n.id
                ORDER BY id DESC LIMIT 1),
               n.forum_id
        FROM loc.new_topics n
    """

    def topics(self, fid: int, page: int):
        sql = f"""SELECT * FROM ({self.TOPIC_UNION}) WHERE forum_id=?
                  ORDER BY (section LIKE '%Anúncio%' OR section LIKE '%Fixo%') DESC,
                           last_date DESC LIMIT ? OFFSET ?"""
        rows = self.db.execute(sql, (fid, TOPICS_PER_PAGE,
                                     (page - 1) * TOPICS_PER_PAGE)).fetchall()
        total = self.db.execute(
            "SELECT (SELECT COUNT(*) FROM topics WHERE forum_id=?) + "
            "(SELECT COUNT(*) FROM loc.new_topics WHERE forum_id=?)", (fid, fid)).fetchone()[0]
        return rows, total

    def recent(self, limit: int = 10):
        return self.db.execute(
            f"SELECT * FROM ({self.TOPIC_UNION}) ORDER BY last_date DESC LIMIT ?",
            (limit,)).fetchall()

    def archive_topic(self, tid: int):
        return self.db.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()

    def new_topic(self, nid: int):
        return self.db.execute("SELECT * FROM loc.new_topics WHERE id=?", (nid,)).fetchone()

    def posts(self, ref: str, page: int):
        """Mensagens do arquivo (se for tópico do arquivo) seguidas das novas."""
        params: list = []
        archive = ""
        if ref.startswith("t"):
            archive = """SELECT 'a' AS kind, p.id, p.position AS pos, p.title, p.author_name AS author,
                                p.date, p.date_raw, p.edit_info, p.likes, p.content_html AS html,
                                p.signature_html, NULL AS body
                         FROM posts p WHERE p.topic_id=?
                         UNION ALL """
            params.append(int(ref[1:]))
        sql = f"""SELECT * FROM (
                    {archive}
                    SELECT 'n' AS kind, n.id, 1000000000 + n.id AS pos, NULL AS title,
                           n.author AS author, n.created AS date, NULL AS date_raw,
                           NULL AS edit_info, NULL AS likes, NULL AS html,
                           NULL AS signature_html, n.body AS body
                    FROM loc.new_posts n WHERE n.topic_ref=?)
                  ORDER BY pos LIMIT ? OFFSET ?"""
        params += [ref, POSTS_PER_PAGE, (page - 1) * POSTS_PER_PAGE]
        rows = self.db.execute(sql, params).fetchall()
        total = self.db.execute(
            "SELECT COUNT(*) FROM loc.new_posts WHERE topic_ref=?", (ref,)).fetchone()[0]
        if ref.startswith("t"):
            total += self.db.execute("SELECT COUNT(*) FROM posts WHERE topic_id=?",
                                     (int(ref[1:]),)).fetchone()[0]
        return rows, total

    def members_by_name(self, names) -> dict[str, sqlite3.Row]:
        names = list({n for n in names if n})
        if not names:
            return {}
        q = ",".join("?" * len(names))
        return {r["name"]: r for r in self.db.execute(
            f"""SELECT m.*, {self.member_extra}, g.name AS group_name, g.color AS group_color
                FROM members m LEFT JOIN groups g ON g.class = m.group_class
                WHERE m.name IN ({q})""", names)}

    def poll(self, tid: int):
        if not self.has_polls:
            return None, []
        p = self.db.execute("SELECT * FROM polls WHERE topic_id=?", (tid,)).fetchone()
        if not p:
            return None, []
        opts = self.db.execute("SELECT * FROM poll_options WHERE topic_id=? ORDER BY position",
                               (tid,)).fetchall()
        return p, opts

    def author_guesses(self, post_ids) -> dict[int, sqlite3.Row]:
        ids = [i for i in post_ids if i and i > 0]
        if not self.has_guesses or not ids:
            return {}
        q = ",".join("?" * len(ids))
        return {r["post_id"]: r for r in self.db.execute(
            f"""SELECT g.*, m.name AS member_name FROM main.v_post_author_best g
                LEFT JOIN main.members m ON m.user_id = g.user_id
                WHERE g.post_id IN ({q})""", ids)}

    def identity(self, name: str, user_id) -> dict:
        out: dict = {"aliases": [], "person": None, "names": [], "guessed": []}
        if self.has_aliases and user_id is not None:
            out["aliases"] = self.db.execute(
                "SELECT name, source, evidence FROM main.account_aliases WHERE user_id = ? "
                "AND lower(name) <> lower(?) ORDER BY evidence DESC", (user_id, name)).fetchall()
        if self.has_persons:
            pid = self.db.execute("SELECT person_id FROM main.person_names WHERE lower(name) = lower(?)",
                                  (name,)).fetchone()
            if pid:
                out["person"] = self.db.execute("SELECT * FROM main.persons WHERE id = ?", (pid[0],)).fetchone()
                out["names"] = self.db.execute(
                    """SELECT pn.name, pn.certainty,
                              (SELECT COUNT(*) FROM main.members m WHERE m.name = pn.name) AS is_member
                       FROM main.person_names pn WHERE pn.person_id = ? AND lower(pn.name) <> lower(?)
                       ORDER BY pn.certainty DESC, pn.name""", (pid[0], name)).fetchall()
        if self.has_guesses:
            out["guessed"] = self.db.execute(
                """SELECT confidence, COUNT(*) AS n FROM main.v_post_author_best
                   WHERE lower(name) = lower(?) GROUP BY confidence""", (name,)).fetchall()
        return out

    def member(self, name: str):
        return self.members_by_name([name]).get(name)

    def members(self, page: int, per: int = 50, q: str = ""):
        where = "COALESCE(m.is_guest, 0) = 0"
        args: list = []
        if q:
            where += " AND m.name LIKE ? ESCAPE '\\'"
            args.append("%" + re.sub(r"([%_\\])", r"\\\1", q) + "%")
        rows = self.db.execute(
            """SELECT m.*, g.name AS group_name, g.color AS group_color,
                      (SELECT COUNT(*) FROM main.posts p WHERE p.author_name = m.name) AS found
               FROM members m LEFT JOIN groups g ON g.class = m.group_class
               WHERE {where}
               ORDER BY COALESCE(m.post_count, 0) DESC, m.name LIMIT ? OFFSET ?""".replace("{where}", where),
            (*args, per, (page - 1) * per)).fetchall()
        total = self.db.execute(
            f"SELECT COUNT(*) FROM members m WHERE {where}", args).fetchone()[0]
        return rows, total

    def member_posts(self, name: str, limit: int = 30):
        return self.db.execute(
            """SELECT p.id, p.topic_id, p.position, p.date, p.date_raw, p.content_text,
                      t.title AS topic_title
               FROM posts p LEFT JOIN topics t ON t.id = p.topic_id
               WHERE p.author_name = ? ORDER BY p.date DESC LIMIT ?""",
            (name, limit)).fetchall()

    def totals(self) -> dict:
        one = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "topics": one("SELECT COUNT(*) FROM topics") + one("SELECT COUNT(*) FROM loc.new_topics"),
            "posts": one("SELECT COUNT(*) FROM posts") + one("SELECT COUNT(*) FROM loc.new_posts"),
            "members": one("SELECT COUNT(*) FROM members WHERE COALESCE(is_guest,0)=0"),
            "completed": one("SELECT COUNT(*) FROM topics WHERE completed=1"),
        }

    # ------------------------------------------------------------------ escrita
    def _write(self):
        return sqlite3.connect(self.local_path, timeout=30)

    def add_topic(self, fid: int, title: str, author: str, body: str) -> int:
        with self._write_lock, self._write() as c:
            ts = now_iso()
            nid = c.execute("INSERT INTO new_topics (forum_id, title, author, created) "
                            "VALUES (?,?,?,?)", (fid, title, author, ts)).lastrowid
            pid = c.execute("INSERT INTO new_posts (topic_ref, author, body, created) VALUES (?,?,?,?)",
                            (f"n{nid}", author, body, ts)).lastrowid
            c.commit()
            self._index_post(pid, f"n{nid}", fid, author, body, ts, title)
            return nid

    def _index_post(self, pid, ref, fid, author, body, ts, new_title=None):
        if not self.index_path:
            return
        from .search_index import add_local_post
        try:
            add_local_post(self.index_path, pid, ref, fid, author, body, ts, new_title)
        except sqlite3.Error as exc:  # o índice é reconstruído no próximo início
            print(f"aviso: falha ao indexar mensagem local: {exc}")

    def add_reply(self, ref: str, author: str, body: str):
        with self._write_lock, self._write() as c:
            ts = now_iso()
            pid = c.execute("INSERT INTO new_posts (topic_ref, author, body, created) VALUES (?,?,?,?)",
                            (ref, author, body, ts)).lastrowid
            c.commit()
            if ref[0] == "t":
                t = self.archive_topic(int(ref[1:]))
            else:
                t = self.new_topic(int(ref[1:]))
            self._index_post(pid, ref, t["forum_id"] if t else None, author, body, ts)


# ======================================================================= HTML
CSS = """
:root{--bg:#eef1f4;--card:#fff;--ink:#1d2733;--muted:#5d6b7a;--line:#d5dce3;
--head:#12416b;--head2:#1d5d94;--link:#105289;--alt:#f6f8fa;--quote:#f3f5f7}
@media (prefers-color-scheme:dark){:root{--bg:#14181d;--card:#1c2229;--ink:#dde3ea;
--muted:#93a1b0;--line:#2e3742;--head:#0f2a44;--head2:#174066;--link:#7db8ef;
--alt:#20272f;--quote:#252d36}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1080px;margin:0 auto;padding:0 16px}
header.top{background:linear-gradient(var(--head2),var(--head));color:#fff;padding:14px 0}
header.top .wrap{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header.top h1{margin:0;font-size:22px}header.top h1 a{color:#fff}
header.top nav{display:flex;gap:4px 14px;flex:1;flex-wrap:wrap}header.top nav a{color:#d8e8f7}
header.top form{display:flex;gap:6px}
input,textarea,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:4px;background:var(--card);color:var(--ink)}
button{font:inherit;padding:6px 14px;border:0;border-radius:4px;background:var(--head2);color:#fff;cursor:pointer}
.crumbs{margin:14px 0;color:var(--muted);font-size:14px}
.box{background:var(--card);border:1px solid var(--line);border-radius:6px;margin:0 0 16px;overflow:hidden}
.box>h2{margin:0;padding:8px 12px;font-size:15px;background:var(--head2);color:#fff}
table.list{width:100%;border-collapse:collapse}
table.list th{text-align:left;font-size:12px;text-transform:uppercase;color:var(--muted);padding:6px 12px;border-bottom:1px solid var(--line)}
table.list td{padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
table.list tr:nth-child(even) td{background:var(--alt)}
td.num{text-align:center;white-space:nowrap;width:80px;color:var(--muted)}
td.last{width:220px;font-size:13px;color:var(--muted)}
.title{font-weight:600}.desc,.small{font-size:13px;color:var(--muted)}
.sub a{font-size:13px;margin-right:8px}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:9px;background:var(--alt);border:1px solid var(--line);color:var(--muted);margin-left:6px;vertical-align:1px}
.tag.new{background:#2e7d32;color:#fff;border-color:#2e7d32}
.post{display:flex;background:var(--card);border:1px solid var(--line);border-radius:6px;margin:0 0 12px}
.post .who{width:180px;flex:none;padding:12px;border-right:1px solid var(--line);font-size:13px;color:var(--muted);text-align:center}
.post .who .name{font-weight:700;font-size:15px;display:block;margin-bottom:6px}
.post .who img.av{max-width:120px;max-height:140px;border-radius:4px;display:block;margin:0 auto 6px}
.post .body{flex:1;min-width:0;padding:12px 16px}
.post .meta{font-size:13px;color:var(--muted);border-bottom:1px solid var(--line);padding-bottom:6px;margin-bottom:10px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.content{overflow-wrap:anywhere}.content img{max-width:100%;height:auto}
.content blockquote,.content dl.codebox,.content .codebox{background:var(--quote);border-left:3px solid var(--line);margin:8px 0;padding:6px 10px}
.content pre,.content code{white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:13px}
.content table{max-width:100%}
.guess{margin-top:8px;padding-top:6px;border-top:1px dashed var(--line);font-size:12px}
.conf{font-weight:600}.conf.alta{color:#2e7d32}.conf.média{color:#b26a00}.conf.baixa{color:var(--muted)}
.sig{margin-top:14px;padding-top:8px;border-top:1px dashed var(--line);font-size:13px;color:var(--muted);max-height:220px;overflow:hidden}
.sig img{max-width:100%;max-height:150px}
.edit{font-size:12px;color:var(--muted);font-style:italic;margin-top:10px}
.pages{margin:10px 0;display:flex;gap:4px;flex-wrap:wrap;align-items:center;font-size:14px}
.pages a,.pages b{padding:2px 9px;border:1px solid var(--line);border-radius:4px;background:var(--card)}
.pages b{background:var(--head2);color:#fff;border-color:var(--head2)}
.notice{background:#fff8e1;color:#5d4500;border:1px solid #f1d27a;padding:10px 14px;border-radius:6px;margin:0 0 16px}
@media (prefers-color-scheme:dark){.notice{background:#3a2f0d;color:#f5dc96;border-color:#6a5617}}
form.reply{padding:12px}form.reply textarea{width:100%;min-height:140px}
form.reply .row{display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap}form.reply .row input{flex:1;min-width:160px}
.stats{display:flex;gap:24px;padding:10px 12px;font-size:14px;color:var(--muted);flex-wrap:wrap}
.stats b{color:var(--ink)}
footer{color:var(--muted);font-size:13px;text-align:center;padding:24px 0}
.poll{padding:12px}.poll .opt{margin-bottom:10px}
.poll .bar{height:8px;background:var(--alt);border:1px solid var(--line);border-radius:4px;overflow:hidden;margin-top:3px}
.poll .bar span{display:block;height:100%;background:var(--head2)}
.hit{padding:10px 12px;border-bottom:1px solid var(--line)}.hit p{margin:4px 0 0;font-size:14px}
@media (max-width:700px){td.last,th.last,td.num.v,th.v{display:none}
.post{flex-direction:column}.post .who{width:auto;border-right:0;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center;text-align:left}
.post .who img.av{max-width:48px;max-height:48px;margin:0}.post .who .extra{display:none}}
"""


def layout(title: str, body: str, crumbs: list[tuple[str, str | None]] | None = None,
           q: str = "") -> str:
    trail = ""
    if crumbs:
        trail = '<div class="crumbs">' + " › ".join(
            f'<a href="{e(u)}">{e(t)}</a>' if u else e(t) for t, u in crumbs) + "</div>"
    return f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · GMBR</title><style>{CSS}</style></head><body>
<header class="top"><div class="wrap"><h1><a href="/">GMBR</a></h1>
<nav><a href="/">Índice</a><a href="/membros">Membros</a><a href="/recentes">Recentes</a><a href="/busca/mensagens">Busca avançada</a></nav>
<form action="/busca/mensagens"><input name="q" value="{e(q)}" placeholder="Buscar…" aria-label="Buscar"><button>Buscar</button></form>
</div></header><main class="wrap">{trail}{body}</main>
<footer class="wrap">Arquivo de <a href="{ORIGIN}/" rel="noreferrer" target="_blank">gmbr.forumeiros.com</a>
· conteúdo dos respectivos autores · mensagens novas ficam só neste servidor</footer>
</body></html>"""


def pager(base: str, page: int, total: int, per: int, param: str = "p") -> str:
    pages = max(1, ceil(total / per))
    if pages <= 1:
        return ""
    sep = "&" if "?" in base else "?"
    shown = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    out, prev = ["Página:"], 0
    for n in shown:
        if n - prev > 1:
            out.append("…")
        out.append(f"<b>{n}</b>" if n == page else f'<a href="{e(base)}{sep}{param}={n}">{n}</a>')
        prev = n
    return '<div class="pages">' + " ".join(out) + "</div>"


def member_link(name: str | None, m=None) -> str:
    if not name:
        return ""
    color = (m["group_color"] or m["color"]) if m else None
    style = f' style="color:{e(color)}"' if color and re.fullmatch(r"#[0-9a-fA-F]{3,8}", color) else ""
    return f'<a href="/membro/{quote(name)}"{style}>{e(name)}</a>'


def prefix_html(prefix: str | None, title: str | None) -> str:
    if not prefix or (title or "").lower().startswith(prefix.lower()):
        return ""
    return f'<span class="small">{e(prefix)}</span> '


def topic_href(ref: str) -> str:
    return f"/{ref[0]}/{ref[1:]}"


def name_form_default(handler) -> str:
    c = SimpleCookie(handler.headers.get("Cookie", ""))
    return unquote(c["nome"].value) if "nome" in c else ""


# ===================================================================== páginas
class Pages:
    def __init__(self, forum: Forum):
        self.f = forum
        self.search = None  # SearchPages, para as correlações no perfil

    def index(self, h):
        forums = self.f.forums()
        stats = self.f.forum_stats()
        children: dict = {}
        for fr in forums:
            children.setdefault(fr["parent_id"], []).append(fr)

        def agg(fid):
            s = dict(stats.get(fid, {"topics": 0, "posts": 0, "last": None}))
            for ch in children.get(fid, []):
                c = agg(ch["id"])
                s["topics"] += c["topics"]
                s["posts"] += c["posts"]
                s["last"] = max(filter(None, [s["last"], c["last"]]), default=None)
            return s

        rows = []
        for fr in children.get(None, []):
            s = agg(fr["id"])
            subs = children.get(fr["id"], [])
            sub = ('<div class="sub small">Subfóruns: ' + "".join(
                f'<a href="/f/{c["id"]}">{e(c["name"])}</a>' for c in subs) + "</div>") if subs else ""
            lt = self.f.last_topic(fr["id"])
            last = (f'<a href="/t/{lt["id"]}">{e(lt["title"])}</a><br>'
                    f'{e(lt["last_post_author"])} · {e(fmt_date(lt["last_post_date"], lt["last_post_date_raw"]))}'
                    ) if lt else ""
            rows.append(f"""<tr><td><a class="title" href="/f/{fr['id']}">{e(fr['name'])}</a>{sub}</td>
<td class="num">{s['topics']}</td><td class="num v">{s['posts']}</td><td class="last">{last}</td></tr>""")
        t = self.f.totals()
        body = f"""<div class="box"><h2>Fóruns</h2><table class="list">
<tr><th>Fórum</th><th>Tópicos</th><th class="v">Mensagens</th><th class="last">Última mensagem</th></tr>
{''.join(rows)}</table></div>
<div class="box"><h2>Estatísticas</h2><div class="stats">
<span><b>{t['topics']}</b> tópicos</span><span><b>{t['completed']}</b> tópicos com mensagens arquivadas</span>
<span><b>{t['posts']}</b> mensagens</span><span><b>{t['members']}</b> membros</span></div></div>"""
        return layout("Índice", body)

    def topic_rows(self, rows) -> str:
        out = []
        for t in rows:
            tags = ""
            if t["ref"][0] == "n":
                tags += '<span class="tag new">novo</span>'
            elif not t["scraped"]:
                tags += '<span class="tag">não arquivado</span>'
            if t["topic_type"] and "locked" in t["topic_type"]:
                tags += '<span class="tag">trancado</span>'
            if t["section"] and ("Fixo" in t["section"] or "Anúncio" in t["section"]):
                tags += f'<span class="tag">{e(t["section"])}</span>'
            desc = f'<div class="desc">{e(t["description"])}</div>' if t["description"] else ""
            out.append(f"""<tr><td>{prefix_html(t['prefix'], t['title'])}<a class="title" href="{topic_href(t['ref'])}">{e(t['title'])}</a>{tags}{desc}
<div class="small">por {member_link(t['author'])}</div></td>
<td class="num">{max(t['replies'] or 0, 0)}</td><td class="num v">{e(t['views'] if t['views'] is not None else '–')}</td>
<td class="last">{member_link(t['last_author'])}<br>{e(fmt_date(t['last_date']))}</td></tr>""")
        return "".join(out)

    def forum_page(self, h, fid: int, page: int):
        fr = self.f.forum(fid)
        if not fr:
            return None
        subs = [x for x in self.f.forums() if x["parent_id"] == fid]
        rows, total = self.f.topics(fid, page)
        crumbs = [("Índice", "/")]
        if fr["parent_id"] and (p := self.f.forum(fr["parent_id"])):
            crumbs.append((p["name"], f"/f/{p['id']}"))
        crumbs.append((fr["name"], None))
        sub = ""
        if subs:
            sub = '<div class="box"><h2>Subfóruns</h2><table class="list">' + "".join(
                f'<tr><td><a class="title" href="/f/{s["id"]}">{e(s["name"])}</a></td></tr>' for s in subs
            ) + "</table></div>"
        pg = pager(f"/f/{fid}", page, total, TOPICS_PER_PAGE)
        listing = (f"""<div class="box"><h2>{e(fr['name'])}</h2><table class="list">
<tr><th>Tópico</th><th>Respostas</th><th class="v">Exibições</th><th class="last">Última mensagem</th></tr>
{self.topic_rows(rows)}</table></div>""" if rows else
                   '<div class="notice">Nenhum tópico deste fórum foi arquivado ainda.</div>')
        body = sub + pg + listing + pg + self.new_topic_form(h, fid)
        return layout(fr["name"], body, crumbs)

    def new_topic_form(self, h, fid: int) -> str:
        return f"""<div class="box"><h2>Novo tópico</h2><form class="reply" method="post" action="/f/{fid}/novo">
<div class="row"><input name="nome" required maxlength="40" placeholder="Seu nome" value="{e(name_form_default(h))}">
<input name="titulo" required maxlength="150" placeholder="Título"></div>
<textarea name="texto" required maxlength="20000" placeholder="Mensagem"></textarea>
<div><button>Criar tópico</button></div></form></div>"""

    def topic_page(self, h, ref: str, page: int, start: int | None):
        if ref[0] == "t":
            t = self.f.archive_topic(int(ref[1:]))
            if not t:
                return None
            title, fid = t["title"], t["forum_id"]
            prefix = t["prefix"] if "prefix" in t.keys() else None
        else:
            prefix = None
            t = self.f.new_topic(int(ref[1:]))
            if not t:
                return None
            title, fid = t["title"], t["forum_id"]
        if start is not None:  # links do fórum original: ?start=N (posição)
            page = start // POSTS_PER_PAGE + 1
        posts, total = self.f.posts(ref, page)
        members = self.f.members_by_name(p["author"] for p in posts)
        guesses = self.f.author_guesses(
            p["id"] for p in posts if p["kind"] == "a" and p["author"] not in members)
        crumbs = [("Índice", "/")]
        if fid and (fr := self.f.forum(fid)):
            crumbs.append((fr["name"], f"/f/{fr['id']}"))
        crumbs.append((title or "Tópico", None))
        notice = ""
        if ref[0] == "t":
            orig = t["url"] or f"{ORIGIN}/t{t['id']}-"
            if not t["completed"]:
                notice = (f'<div class="notice">Este tópico ainda não foi totalmente arquivado '
                          f'({t["posts_scraped"] or 0} de {(t["replies"] or 0) + 1} mensagens). '
                          f'<a href="{e(orig)}" rel="noreferrer" target="_blank">Ver no fórum original</a>.</div>')
        html = [notice]
        if ref[0] == "t" and page == 1:
            html.append(self.render_poll(*self.f.poll(int(ref[1:]))))
        pg = pager(topic_href(ref), page, total, POSTS_PER_PAGE)
        html.append(pg)
        for p in posts:
            html.append(self.render_post(p, members.get(p["author"]),
                                         guesses.get(p["id"]) if p["kind"] == "a" else None))
        html.append(pg)
        html.append(f"""<div class="box"><h2>Responder</h2><form class="reply" method="post" action="{topic_href(ref)}/responder">
<div class="row"><input name="nome" required maxlength="40" placeholder="Seu nome" value="{e(name_form_default(h))}"></div>
<textarea name="texto" required maxlength="20000" placeholder="Mensagem"></textarea>
<div><button>Enviar resposta</button></div></form></div>""")
        return layout(title or "Tópico", f"<h2>{prefix_html(prefix, title)}{e(title)}</h2>" + "".join(html), crumbs)

    def render_poll(self, poll, opts) -> str:
        if not poll:
            return ""
        rows = []
        for o in opts:
            pct = o["percent"] if o["percent"] is not None else (
                100 * (o["votes"] or 0) / poll["total_votes"] if poll["total_votes"] else 0)
            rows.append(f"""<div class="opt"><div>{e(o['text'])}<span class="small"> · {e(o['votes'] if o['votes'] is not None else '?')} votos ({pct:.0f}%)</span></div>
<div class="bar"><span style="width:{max(0, min(100, pct)):.1f}%"></span></div></div>""")
        total = f"Total de votos: {e(poll['total_votes'])}" if poll["total_votes"] is not None else ""
        closed = " · encerrada" if poll["closed"] else ""
        return f"""<div class="box"><h2>Enquete: {e(poll['question'])}</h2><div class="poll">{''.join(rows)}
<div class="small">{total}{closed}</div></div></div>"""

    METHODS = {"citacao": "citação", "edicao": "edição", "mencao": "menção",
               "continuidade": "continuidade no tópico", "periodo": "atividade no mesmo período"}

    def render_guess(self, g) -> str:
        """Autor provável de uma mensagem de convidado/conta apagada."""
        name = g["member_name"] or g["name"]
        methods = ", ".join(self.METHODS.get(x, x) for x in (g["methods"] or "").split(",") if x)
        others = f" · {g['candidates']} candidatos" if (g["candidates"] or 1) > 1 else ""
        names = f" ({e(g['names'])})" if g["names"] and g["names"] != g["name"] else ""
        return (f'<div class="guess" title="Sugestão automática, não é o autor registrado no fórum">'
                f'provável: {member_link(name)}{names}<br><span class="conf {e(g["confidence"])}">'
                f'confiança {e(g["confidence"])}</span> · {e(methods)}{others}</div>')

    def render_post(self, p, m, guess=None) -> str:
        who = [f'<span class="name">{member_link(p["author"], m)}</span>']
        if m:
            if m["avatar_url"]:
                who.insert(0, f'<img class="av" src="{e(m["avatar_url"])}" alt="" loading="lazy" referrerpolicy="no-referrer">')
            extra = []
            if m["group_name"]:
                extra.append(e(m["group_name"]))
            elif m["rank_title"]:
                extra.append(e(m["rank_title"]))
            if m["post_count"] is not None:
                extra.append(f"Mensagens: {e(m['post_count'])}")
            if m["joined_raw"]:
                extra.append(f"Desde: {e(m['joined_raw'])}")
            if m["reputation"] is not None:
                extra.append(f"Reputação: {e(m['reputation'])}")
            who.append('<div class="extra">' + "<br>".join(extra) + "</div>")
        elif p["kind"] == "n":
            who.append('<div class="extra">membro local</div>')
        if guess is not None:
            who.append(self.render_guess(guess))
        if p["kind"] == "a":
            content = clean_html(p["html"])
            date = fmt_date(p["date"], p["date_raw"])
            anchor = f'p{p["id"]}'
        else:
            content = text_to_html(p["body"])
            date = fmt_date(p["date"])
            anchor = f'n{p["id"]}'
        extra = ""
        if p["kind"] == "a" and p["edit_info"]:
            extra += f'<div class="edit">{e(p["edit_info"])}</div>'
        if p["kind"] == "a" and p["likes"]:
            extra += f'<div class="edit">👍 {e(p["likes"])}</div>'
        sig = p["signature_html"] if p["kind"] == "a" else None
        if sig:
            extra += f'<div class="sig">{clean_html(sig)}</div>'
        new = ' <span class="tag new">nova</span>' if p["kind"] == "n" else ""
        return f"""<article class="post" id="{anchor}"><div class="who">{''.join(who)}</div>
<div class="body"><div class="meta"><span>{e(p['title'] or '')}{new}</span>
<a href="#{anchor}">{e(date)}</a></div><div class="content">{content}</div>{extra}</div></article>"""

    def members_page(self, h, page: int, q: str = ""):
        q = q.strip()[:40]
        rows, total = self.f.members(page, q=q)
        trs = "".join(
            f"""<tr><td>{member_link(m['name'], m)}<div class="small">{e(m['group_name'] or m['rank_title'] or '')}</div></td>
<td class="num">{e(m['post_count'] if m['post_count'] is not None else '–')}</td>
<td class="num v">{m['found']}</td><td class="last">{e(m['joined_raw'] or '')}</td></tr>""" for m in rows)
        pg = pager("/membros" + (f"?q={quote(q)}" if q else ""), page, total, 50)
        form = f"""<form action="/membros" class="pages"><input name="q" value="{e(q)}" placeholder="Filtrar por nome" aria-label="Filtrar por nome"><button>Filtrar</button></form>"""
        body = f"""{form}{pg}<div class="box"><h2>Membros ({total})</h2><table class="list">
<tr><th>Nome</th><th>Mensagens</th><th class="v">Arquivadas</th><th class="last">Inscrição</th></tr>{trs}</table></div>{pg}"""
        return layout("Membros", body, [("Índice", "/"), ("Membros", None)])

    def member_page(self, h, name: str, mode: str = "pessoa"):
        m = self.f.member(name)
        posts = self.f.member_posts(name)
        ident = self.identity_box(name, m["user_id"] if m else None)
        if not m and not posts and not ident:
            return None
        info = []
        if m:
            if m["avatar_url"]:
                info.append(f'<img src="{e(m["avatar_url"])}" alt="" style="max-width:140px;float:right" referrerpolicy="no-referrer">')
            for label, key in [("Grupo", "group_name"), ("Cargo", "rank_title"), ("Inscrição", "joined_raw"),
                               ("Mensagens", "post_count"), ("Reputação", "reputation")]:
                if m[key] not in (None, ""):
                    info.append(f"<div><b>{label}:</b> {e(m[key])}</div>")
            if m["website"]:
                info.append(f'<div><b>Site:</b> <a href="{e(m["website"])}" rel="nofollow noreferrer" target="_blank">{e(m["website"])}</a></div>'
                            if m["website"].startswith("http") else f"<div><b>Site:</b> {e(m['website'])}</div>")
            try:
                extra = json.loads(m["extra_fields"] or "{}")
            except ValueError:
                extra = {}
            for k, v in extra.items():
                if v not in ("", "0", None):
                    info.append(f"<div><b>{e(k)}:</b> {e(v)}</div>")
            if m["last_visit_raw"]:
                info.append(f"<div><b>Último acesso:</b> {e(m['last_visit_raw'])}</div>")
            try:
                groups = json.loads(m["profile_groups"] or "[]")
            except ValueError:
                groups = []
            if groups:
                info.append("<div><b>Grupos:</b> " + ", ".join(e(g.get("name")) for g in groups if isinstance(g, dict)) + "</div>")
            try:
                fields = json.loads(m["profile_fields"] or "{}")
            except ValueError:
                fields = {}
            shown = {"Rank", "Estado", "Data de inscrição", "Reputação", "Número de Mensagens",
                     "Última visita", "Último acesso"}
            for k, v in fields.items():
                if k in shown or k in extra or v in (None, "", "0", {}):
                    continue
                if isinstance(v, dict):
                    imgs = "".join(f'<img src="{e(u)}" alt="" style="max-height:40px;vertical-align:middle" referrerpolicy="no-referrer">'
                                   for u in (v.get("images") or []) if str(u).startswith("http"))
                    v_html = (e(v.get("text") or "") + " " + imgs).strip()
                elif isinstance(v, list):
                    v_html = e(", ".join(map(str, v)))
                else:
                    v_html = e(v).replace("\n", "<br>")
                if v_html:
                    info.append(f"<div><b>{e(k)}:</b> {v_html}</div>")
            if m["signature_html"]:
                info.append(f'<div class="sig">{clean_html(m["signature_html"])}</div>')
        hits = "".join(self.hit(p) for p in posts) or '<div class="hit">Nenhuma mensagem arquivada.</div>'
        body = f"""<div class="box"><h2>{e(name)}</h2>
<div style="padding:12px">{''.join(info) or 'Sem dados de perfil.'}<div style="clear:both"></div></div></div>
{ident}{self.search.member_correlations(name, mode) if self.search else ''}
<div class="box"><h2>Mensagens arquivadas recentes</h2>{hits}</div>"""
        return layout(name, body, [("Índice", "/"), ("Membros", "/membros"), (name, None)])

    def identity_box(self, name: str, user_id) -> str:
        idt = self.f.identity(name, user_id)
        parts = []
        if idt["aliases"]:
            parts.append("<div><b>Outros nomes desta conta:</b> " + ", ".join(
                f'{member_link(a["name"])} <span class="small">({e(a["source"])})</span>'
                for a in idt["aliases"]) + "</div>")
        if idt["person"] is not None:
            names = ", ".join(
                (member_link(n["name"]) if n["is_member"] else e(n["name"])) +
                f' <span class="small">({e(n["certainty"])})</span>' for n in idt["names"])
            note = f'<div class="small">{e(idt["person"]["note"])}</div>' if idt["person"]["note"] else ""
            parts.append(f"<div><b>Mesma pessoa:</b> {names or '–'}</div>{note}")
        if idt["guessed"]:
            g = {r["confidence"]: r["n"] for r in idt["guessed"]}
            txt = ", ".join(f"{g[c]} com confiança {c}" for c in ("alta", "média", "baixa") if g.get(c))
            parts.append(f"<div><b>Mensagens de convidado atribuídas a este nome:</b> {txt}</div>")
        if not parts:
            return ""
        return ('<div class="box"><h2>Identidade</h2><div style="padding:12px">' + "".join(parts) +
                '<div class="small" style="margin-top:6px">Levantado pelo scraper a partir de citações, '
                'edições, histórico de nick e denúncias; pode conter erros.</div></div></div>')

    def hit(self, p, q: str = "") -> str:
        text = (p["content_text"] or "").strip()
        if q and (i := text.lower().find(q.lower())) > 80:
            text = "…" + text[i - 60:]
        text = text[:300] + ("…" if len(text) > 300 else "")
        snippet = e(text)
        if q:
            snippet = re.sub(re.escape(e(q)), lambda mm: f"<mark>{mm.group(0)}</mark>", snippet, flags=re.I)
        page = (p["position"] or 0) // POSTS_PER_PAGE + 1
        author = member_link(p["author_name"]) + " · " if "author_name" in p.keys() else ""
        return f"""<div class="hit"><a class="title" href="/t/{p['topic_id']}?p={page}#p{p['id']}">{e(p['topic_title'] or f"Tópico {p['topic_id']}")}</a>
<div class="small">{author}{e(fmt_date(p['date'], p['date_raw']))}</div><p>{snippet}</p></div>"""

    def recent_page(self, h):
        rows = self.f.recent(50)
        body = f"""<div class="box"><h2>Tópicos com atividade recente</h2><table class="list">
<tr><th>Tópico</th><th>Respostas</th><th class="v">Exibições</th><th class="last">Última mensagem</th></tr>
{self.topic_rows(rows)}</table></div>"""
        return layout("Recentes", body, [("Índice", "/"), ("Recentes", None)])


# ==================================================================== servidor
def make_handler(forum: Forum):
    pages = Pages(forum)
    from .search import SearchPages
    search_pages = SearchPages(forum)
    pages.search = search_pages

    class Handler(BaseHTTPRequestHandler):
        server_version = "gmbr-forum"

        def log_message(self, fmt, *args):
            pass

        def send(self, code: int, html: str, headers: dict | None = None):
            data = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; img-src * data:; style-src 'unsafe-inline'; "
                             "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def redirect(self, to: str, cookie: str | None = None):
            self.send_response(303)
            self.send_header("Location", to)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def not_found(self):
            self.send(404, layout("Não encontrado", '<div class="notice">Página não encontrada.</div>',
                                  [("Índice", "/")]))

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self._safe(self._get)

        def do_POST(self):
            self._safe(self._post)

        def _safe(self, fn):
            try:
                fn()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                import traceback
                traceback.print_exc()
                try:
                    self.send(500, layout("Erro", '<div class="notice">Erro interno.</div>', [("Índice", "/")]))
                except Exception:
                    pass

        def _get(self):
            u = urlsplit(self.path)
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                page = max(1, int(qs.get("p", "1")))
            except ValueError:
                page = 1
            path = u.path.rstrip("/") or "/"
            html = None
            if path == "/":
                html = pages.index(self)
            elif m := re.fullmatch(r"/f/(\d+)", path):
                html = pages.forum_page(self, int(m[1]), page)
            elif m := re.fullmatch(r"/([tn])/(\d+)", path):
                start = int(qs["start"]) if qs.get("start", "").isdigit() else None
                html = pages.topic_page(self, m[1] + m[2], page, start)
            elif path == "/membros":
                html = pages.members_page(self, page, qs.get("q", ""))
            elif m := re.fullmatch(r"/membro/(.+)", path):
                html = pages.member_page(self, unquote(m[1]), qs.get("juntar", "pessoa"))
            elif path == "/busca":
                return self.redirect("/busca/mensagens" + (f"?{u.query}" if u.query else ""))
            elif m := re.fullmatch(r"/busca/(mensagens|topicos|usuarios)", path):
                html = search_pages.page(m[1], parse_qs(u.query))
            elif path == "/recentes":
                html = pages.recent_page(self)
            if html is None:
                return self.not_found()
            self.send(200, html)

        def _post(self):
            path = urlsplit(self.path).path
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = 0
            if n > 100_000:
                return self.send(413, layout("Erro", '<div class="notice">Mensagem grande demais.</div>'))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
            nome = re.sub(r"\s+", " ", form.get("nome", "")).strip()[:40]
            texto = form.get("texto", "").replace("\r\n", "\n").strip()[:20000]
            if not nome or not texto:
                return self.send(400, layout("Erro", '<div class="notice">Preencha nome e mensagem.</div>'))
            cookie = f"nome={quote(nome)}; Path=/; Max-Age=31536000; SameSite=Lax"
            if m := re.fullmatch(r"/f/(\d+)/novo", path):
                titulo = re.sub(r"\s+", " ", form.get("titulo", "")).strip()[:150]
                fid = int(m[1])
                if not titulo or not forum.forum(fid):
                    return self.send(400, layout("Erro", '<div class="notice">Informe o título.</div>'))
                nid = forum.add_topic(fid, titulo, nome, texto)
                return self.redirect(f"/n/{nid}", cookie)
            if m := re.fullmatch(r"/([tn])/(\d+)/responder", path):
                ref = m[1] + m[2]
                exists = forum.archive_topic(int(m[2])) if m[1] == "t" else forum.new_topic(int(m[2]))
                if not exists:
                    return self.not_found()
                forum.add_reply(ref, nome, texto)
                _, total = forum.posts(ref, 1)
                last = ceil(total / POSTS_PER_PAGE)
                return self.redirect(f"/{m[1]}/{m[2]}?p={last}", cookie)
            self.not_found()

    return Handler


def serve(archive: str, local: str, host: str, port: int, index: str | None = None,
          reindex: bool = False):
    if index:
        from .search_index import build, needs_rebuild
        if reindex or needs_rebuild(archive, index):
            print("Construindo o índice de busca (só na primeira vez ou quando o banco muda)…")
            Forum(archive, local)  # garante o banco local
            build(archive, local, index)
    forum = Forum(archive, local, index)
    httpd = ThreadingHTTPServer((host, port), make_handler(forum))
    print(f"GMBR em http://{host}:{port}/  (arquivo: {archive}, local: {local})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
