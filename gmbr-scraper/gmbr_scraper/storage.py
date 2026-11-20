"""Persistência em SQLite. Permite interromper e retomar o crawl."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS forums (
    id            INTEGER PRIMARY KEY,
    name          TEXT,
    slug          TEXT,
    url           TEXT,
    parent_id     INTEGER,
    topics_listed INTEGER,
    scraped_at    TEXT,     -- quando a listagem do fórum foi concluída
    listing_next_start INTEGER  -- offset onde retomar uma listagem interrompida
);

CREATE TABLE IF NOT EXISTS topics (
    id                 INTEGER PRIMARY KEY,
    forum_id           INTEGER,
    title              TEXT,
    description        TEXT,
    url                TEXT,
    section            TEXT,
    author_name        TEXT,
    replies            INTEGER,  -- valor da listagem do fórum
    views              INTEGER,
    last_post_id       INTEGER,
    last_post_author   TEXT,
    last_post_date_raw TEXT,
    last_post_date     TEXT,
    pages              INTEGER,  -- nº de páginas vistas no último crawl
    posts_scraped      INTEGER,
    scraped_replies    INTEGER,  -- valor de "replies" quando foi raspado
    completed          INTEGER DEFAULT 0,
    scraped_at         TEXT
);

CREATE TABLE IF NOT EXISTS topic_pages (
    topic_id   INTEGER,
    start      INTEGER,
    post_count INTEGER,
    scraped_at TEXT,
    PRIMARY KEY (topic_id, start)
);

CREATE TABLE IF NOT EXISTS posts (
    id             INTEGER PRIMARY KEY,
    topic_id       INTEGER,
    page_start     INTEGER,
    position       INTEGER,   -- posição global no tópico (0 = mensagem inicial)
    title          TEXT,
    author_name    TEXT,
    author_id      INTEGER,
    date_raw       TEXT,
    date           TEXT,
    edit_info      TEXT,
    edit_count     INTEGER,
    last_edit_date TEXT,
    likes          INTEGER,
    dislikes       INTEGER,
    likes_text     TEXT,
    liked_by       TEXT,      -- JSON
    vote_title     TEXT,
    content_html   TEXT,
    content_text   TEXT,
    signature_html TEXT,
    scraped_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_topic  ON posts(topic_id);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_name);

CREATE TABLE IF NOT EXISTS groups (
    class TEXT PRIMARY KEY,  -- "group-14"
    id    INTEGER,
    name  TEXT,
    slug  TEXT,
    color TEXT
);

CREATE TABLE IF NOT EXISTS members (
    name             TEXT PRIMARY KEY,
    user_id          INTEGER,
    profile_url      TEXT,
    avatar_url       TEXT,
    group_class      TEXT,
    color            TEXT,
    rank_title       TEXT,
    rank_image       TEXT,
    joined_raw       TEXT,
    joined_date      TEXT,
    reputation       INTEGER,
    post_count       INTEGER,   -- contador exibido no perfil
    website          TEXT,
    signature_html   TEXT,
    extra_fields     TEXT,      -- JSON (Medalhas, Moedas, etc.)
    profile_images   TEXT,      -- JSON (prêmios, imagens de campos)
    profile_post_id  INTEGER,   -- mensagem de onde o perfil foi lido por último
    first_seen_post_id INTEGER,
    is_guest         INTEGER DEFAULT 0, -- "Convidado": visitante/conta apagada
    updated_at       TEXT
);
"""


def _looks_like_guest(prof: dict) -> bool:
    """Mensagens de visitantes/contas excluídas aparecem como "Convidado",
    sem data de inscrição nem contador de mensagens."""
    return (prof.get("rank_title") == "Convidado" or prof.get("name") == "Convidado") \
        and not prof.get("joined_date") and prof.get("post_count") is None


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class Storage:
    def __init__(self, path: str):
        # timeout alto: vários processos podem gravar no mesmo banco
        self.conn = sqlite3.connect(path, timeout=300)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=300000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Adiciona colunas novas a bancos criados por versões anteriores."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(forums)")}
        if "listing_next_start" not in cols:
            self.conn.execute("ALTER TABLE forums ADD COLUMN listing_next_start INTEGER")
            self.conn.commit()

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()

    # ------------------------------------------------------------------ forums
    def upsert_forum(self, forum: dict, parent_id: int | None = None):
        self.conn.execute(
            """INSERT INTO forums (id, name, slug, url, parent_id) VALUES (?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=COALESCE(excluded.name, forums.name),
                 slug=COALESCE(excluded.slug, forums.slug),
                 url=COALESCE(excluded.url, forums.url),
                 parent_id=COALESCE(excluded.parent_id, forums.parent_id)""",
            (forum["id"], forum.get("name"), forum.get("slug"), forum.get("url"), parent_id),
        )

    def mark_forum_scraped(self, forum_id: int, topics_listed: int):
        """Marca a listagem do fórum como concluída."""
        self.conn.execute(
            "UPDATE forums SET topics_listed=?, scraped_at=?, listing_next_start=NULL "
            "WHERE id=?",
            (topics_listed, now_iso(), forum_id),
        )

    def set_listing_progress(self, forum_id: int, next_start: int):
        self.conn.execute(
            "UPDATE forums SET listing_next_start=? WHERE id=?", (next_start, forum_id)
        )

    def get_forum(self, forum_id: int):
        return self.conn.execute("SELECT * FROM forums WHERE id=?", (forum_id,)).fetchone()

    def topic_last_post_ids(self, topic_ids: list[int]) -> dict[int, int | None]:
        if not topic_ids:
            return {}
        rows = self.conn.execute(
            f"SELECT id, last_post_id FROM topics WHERE id IN ({','.join('?' * len(topic_ids))})",
            topic_ids,
        ).fetchall()
        return {r["id"]: r["last_post_id"] for r in rows}

    def forums(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM forums ORDER BY id").fetchall()

    # ------------------------------------------------------------------ topics
    def upsert_topic_listing(self, t: dict):
        self.conn.execute(
            """INSERT INTO topics (id, forum_id, title, description, url, section,
                   author_name, replies, views, last_post_id, last_post_author,
                   last_post_date_raw, last_post_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 forum_id=COALESCE(topics.forum_id, excluded.forum_id), title=excluded.title,
                 description=excluded.description, url=excluded.url,
                 section=excluded.section, author_name=excluded.author_name,
                 replies=excluded.replies, views=excluded.views,
                 last_post_id=excluded.last_post_id,
                 last_post_author=excluded.last_post_author,
                 last_post_date_raw=excluded.last_post_date_raw,
                 last_post_date=excluded.last_post_date""",
            (t["id"], t.get("forum_id"), t.get("title"), t.get("description"), t.get("url"),
             t.get("section"), t.get("author_name"), t.get("replies"), t.get("views"),
             t.get("last_post_id"), t.get("last_post_author"),
             t.get("last_post_date_raw"), t.get("last_post_date")),
        )

    def ensure_topic(self, topic_id: int, url: str | None = None):
        self.conn.execute(
            "INSERT OR IGNORE INTO topics (id, url) VALUES (?, ?)", (topic_id, url)
        )

    def get_topic(self, topic_id: int):
        return self.conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()

    def topics_to_scrape(self, forum_ids: list[int] | None = None, refresh: bool = False,
                         shard: tuple[int, int] | None = None):
        """Tópicos pendentes: nunca concluídos, ou com novas respostas desde o crawl."""
        sql = "SELECT * FROM topics"
        cond, args = [], []
        if not refresh:
            cond.append(
                "(completed=0 OR scraped_replies IS NULL OR "
                "(replies IS NOT NULL AND replies <> scraped_replies))"
            )
        if forum_ids:
            cond.append(f"forum_id IN ({','.join('?' * len(forum_ids))})")
            args += forum_ids
        if shard:
            cond.append("id % ? = ?")
            args += [shard[1], shard[0]]
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id"
        return self.conn.execute(sql, args).fetchall()

    def page_done(self, topic_id: int, start: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM topic_pages WHERE topic_id=? AND start=?", (topic_id, start)
        ).fetchone() is not None

    def last_page_start(self, topic_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(start) FROM topic_pages WHERE topic_id=?", (topic_id,)
        ).fetchone()
        return row[0]

    def mark_page(self, topic_id: int, start: int, post_count: int):
        self.conn.execute(
            "INSERT OR REPLACE INTO topic_pages VALUES (?,?,?,?)",
            (topic_id, start, post_count, now_iso()),
        )

    def finish_topic(self, topic_id: int, title: str | None, forum_id: int | None,
                     pages: int):
        posts = self.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE topic_id=?", (topic_id,)
        ).fetchone()[0]
        self.conn.execute(
            """UPDATE topics SET
                 title=COALESCE(title, ?), forum_id=COALESCE(?, forum_id),
                 pages=?, posts_scraped=?, completed=1, scraped_at=?,
                 replies=COALESCE(replies, ?), scraped_replies=COALESCE(replies, ?)
               WHERE id=?""",
            (title, forum_id, pages, posts, now_iso(), posts - 1, posts - 1, topic_id),
        )

    # ------------------------------------------------------------------- posts
    def upsert_post(self, p: dict, page_start: int, position: int):
        self.conn.execute(
            """INSERT OR REPLACE INTO posts (id, topic_id, page_start, position, title,
                 author_name, author_id, date_raw, date, edit_info, edit_count,
                 last_edit_date, likes, dislikes, likes_text, liked_by, vote_title,
                 content_html, content_text, signature_html, scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p["id"], p["topic_id"], page_start, position, p.get("title"),
             p.get("author_name"), p.get("author_id"), p.get("date_raw"), p.get("date"),
             p.get("edit_info"), p.get("edit_count"), p.get("last_edit_date"),
             p.get("likes"), p.get("dislikes"), p.get("likes_text"),
             json.dumps(p.get("liked_by") or [], ensure_ascii=False),
             p.get("vote_title"), p.get("content_html"), p.get("content_text"),
             p.get("signature_html"), now_iso()),
        )

    # ----------------------------------------------------------------- members
    def upsert_member(self, prof: dict, post_id: int, signature_html: str | None):
        """Atualiza o membro com o perfil exibido ao lado da mensagem.

        Os dados de perfil no Forumeiros refletem o estado ATUAL do membro (não
        o da época da mensagem), então qualquer mensagem serve; guardamos sempre
        o último visto, sem apagar campos que vierem vazios.
        """
        name = prof.get("name")
        if not name:
            return
        self.conn.execute(
            """INSERT INTO members (name, user_id, profile_url, avatar_url, group_class,
                 color, rank_title, rank_image, joined_raw, joined_date, reputation,
                 post_count, website, signature_html, extra_fields, profile_images,
                 profile_post_id, first_seen_post_id, is_guest, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 user_id=COALESCE(excluded.user_id, members.user_id),
                 profile_url=COALESCE(excluded.profile_url, members.profile_url),
                 avatar_url=COALESCE(excluded.avatar_url, members.avatar_url),
                 group_class=COALESCE(excluded.group_class, members.group_class),
                 color=COALESCE(excluded.color, members.color),
                 rank_title=COALESCE(excluded.rank_title, members.rank_title),
                 rank_image=COALESCE(excluded.rank_image, members.rank_image),
                 joined_raw=COALESCE(excluded.joined_raw, members.joined_raw),
                 joined_date=COALESCE(excluded.joined_date, members.joined_date),
                 reputation=COALESCE(excluded.reputation, members.reputation),
                 post_count=COALESCE(excluded.post_count, members.post_count),
                 website=COALESCE(excluded.website, members.website),
                 signature_html=COALESCE(excluded.signature_html, members.signature_html),
                 extra_fields=CASE WHEN excluded.extra_fields <> '{}'
                                   THEN excluded.extra_fields ELSE members.extra_fields END,
                 profile_images=CASE WHEN excluded.profile_images <> '[]'
                                   THEN excluded.profile_images ELSE members.profile_images END,
                 profile_post_id=MAX(excluded.profile_post_id, members.profile_post_id),
                 first_seen_post_id=MIN(excluded.first_seen_post_id, members.first_seen_post_id),
                 is_guest=excluded.is_guest,
                 updated_at=excluded.updated_at""",
            (name, prof.get("user_id"), prof.get("profile_url"), prof.get("avatar_url"),
             prof.get("group_class"), prof.get("color"), prof.get("rank_title"),
             prof.get("rank_image"), prof.get("joined_raw"), prof.get("joined_date"),
             prof.get("reputation"), prof.get("post_count"), prof.get("website"),
             signature_html,
             json.dumps(prof.get("extra_fields") or {}, ensure_ascii=False),
             json.dumps(prof.get("profile_images") or [], ensure_ascii=False),
             post_id, post_id, int(_looks_like_guest(prof)), now_iso()),
        )

    def upsert_group(self, g: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO groups (class, id, name, slug, color) VALUES (?,?,?,?,?)",
            (g["class"], g["id"], g.get("name"), g.get("slug"), g.get("color")),
        )

    def members_report(self) -> list[dict]:
        """Membros + estatísticas calculadas a partir das mensagens raspadas."""
        rows = self.conn.execute(
            """SELECT m.*, g.name AS group_name,
                      COUNT(p.id)            AS posts_found,
                      COUNT(DISTINCT p.topic_id) AS topics_participated,
                      MIN(p.date)            AS first_post_date,
                      MAX(p.date)            AS last_post_date,
                      (SELECT COUNT(*) FROM topics t
                         WHERE t.author_name = m.name) AS topics_started
               FROM members m
               LEFT JOIN groups g ON g.class = m.group_class
               LEFT JOIN posts p ON p.author_name = m.name
               GROUP BY m.name
               ORDER BY posts_found DESC, m.name"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["extra_fields"] = json.loads(d["extra_fields"] or "{}")
            d["profile_images"] = json.loads(d["profile_images"] or "[]")
            out.append(d)
        return out
