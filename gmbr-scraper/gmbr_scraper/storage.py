"""Persistência em SQLite. Permite interromper e retomar o crawl.

Convenções do schema (versão 4):

* Contagens que dependem de outras tabelas (mensagens por tópico, tópicos por
  fórum, mensagens por membro, votos de uma enquete, curtidas…) NÃO são
  gravadas como fonte de verdade: são calculadas na hora pelas views ``v_*``.
* Colunas com sufixo ``_forum`` guardam o número que o próprio fórum exibe.
  Servem para detectar mudanças (crawl incremental) e conferir a raspagem
  (``v_count_checks``); não substituem a contagem real.
* Colunas marcadas "informativo" são anotações da raspagem (quantas páginas
  ou mensagens foram vistas naquela passada). Nenhuma lógica depende delas.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

SCHEMA_VERSION = 4

TABLES = """
CREATE TABLE IF NOT EXISTS forums (
    id                 INTEGER PRIMARY KEY,
    name               TEXT,
    slug               TEXT,
    url                TEXT,
    parent_id          INTEGER REFERENCES forums(id) DEFERRABLE INITIALLY DEFERRED,
    topics_listed      INTEGER,  -- informativo: tópicos vistos na última listagem
    scraped_at         TEXT,     -- quando a listagem do fórum foi concluída
    listing_next_start INTEGER   -- offset onde retomar uma listagem interrompida
);

CREATE TABLE IF NOT EXISTS topics (
    id                 INTEGER PRIMARY KEY,
    forum_id           INTEGER REFERENCES forums(id) DEFERRABLE INITIALLY DEFERRED,
    title              TEXT,
    description        TEXT,
    url                TEXT,
    section            TEXT,
    prefix             TEXT,     -- ex.: "[Votação]"
    topic_type         TEXT,     -- ícone: topic_read_locked, sticky_read...
    replies_forum      INTEGER,  -- respostas segundo a listagem do fórum
    views_forum        INTEGER,  -- visualizações segundo a listagem do fórum
    last_post_id_forum INTEGER,  -- última mensagem segundo a listagem (incremental)
    scraped_replies    INTEGER,  -- replies_forum no momento da raspagem (incremental)
    pages              INTEGER,  -- informativo: páginas vistas na última raspagem
    posts_scraped      INTEGER,  -- informativo: mensagens vistas na última raspagem
    completed          INTEGER DEFAULT 0,
    scraped_at         TEXT
);

CREATE TABLE IF NOT EXISTS topic_pages (
    topic_id   INTEGER REFERENCES topics(id) DEFERRABLE INITIALLY DEFERRED,
    start      INTEGER,
    post_count INTEGER,  -- informativo: mensagens vistas nesta página
    scraped_at TEXT,
    PRIMARY KEY (topic_id, start)
);

CREATE TABLE IF NOT EXISTS groups (
    id    INTEGER PRIMARY KEY,  -- /gNN (a classe CSS é "group-NN")
    name  TEXT,
    slug  TEXT,
    color TEXT
);

CREATE TABLE IF NOT EXISTS members (
    user_id            INTEGER PRIMARY KEY,  -- /uNN
    name               TEXT,                 -- nome atual
    profile_url        TEXT,
    avatar_url         TEXT,
    group_id           INTEGER REFERENCES groups(id) DEFERRABLE INITIALLY DEFERRED,
                                             -- grupo que dá a cor do nome
    color              TEXT,
    rank_title         TEXT,
    rank_image         TEXT,
    joined_raw         TEXT,
    joined_date        TEXT,
    last_visit_raw     TEXT,
    last_visit         TEXT,
    reputation         INTEGER,  -- valor do perfil (não há como recalcular)
    post_count_forum   INTEGER,  -- nº de mensagens exibido no perfil
    website            TEXT,
    humor              TEXT,
    age                INTEGER,
    about_me           TEXT,     -- "Quem sou eu"
    specialty          TEXT,     -- "Especialidade"
    platforms          TEXT,     -- "Plataformas"
    projects           TEXT,     -- "Meus projetos"
    infractions        TEXT,     -- "Infrações"
    grades_received    TEXT,     -- "Notas recebidas"
    nick_history       TEXT,     -- "Alterações no Nick"
    signature_html     TEXT,     -- uma por membro (sem os alt que o fórum injeta)
    sidebar_fields     TEXT,     -- JSON: campos do bloco ao lado das mensagens
    sidebar_images     TEXT,     -- JSON: imagens desse bloco (prêmios, notas)
    profile_fields     TEXT,     -- JSON: todos os campos da página /uNN (bruto)
    in_memberlist      INTEGER,
    profile_status     TEXT,     -- "ok" ou motivo de falha
    profile_scraped_at TEXT,
    updated_at         TEXT
);

CREATE TABLE IF NOT EXISTS member_groups (
    user_id  INTEGER REFERENCES members(user_id) DEFERRABLE INITIALLY DEFERRED,
    group_id INTEGER REFERENCES groups(id) DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY (user_id, group_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS member_awards (
    user_id  INTEGER REFERENCES members(user_id) DEFERRABLE INITIALLY DEFERRED,
    award    TEXT,      -- Medalhas, Tutoriais, Moedas, Ouro, Prata, Bronze, Insignia N
    quantity INTEGER,   -- só quantidades diferentes de 0 (o fórum tem valores negativos)
    PRIMARY KEY (user_id, award)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS posts (
    id                 INTEGER PRIMARY KEY,
    topic_id           INTEGER REFERENCES topics(id) DEFERRABLE INITIALLY DEFERRED,
    page_start         INTEGER,
    position           INTEGER,  -- posição no tópico (0 = mensagem inicial)
    title              TEXT,
    author_id          INTEGER REFERENCES members(user_id) DEFERRABLE INITIALLY DEFERRED,
    author_name        TEXT,     -- nome exibido na mensagem (convidados só têm este)
    author_id_inferred INTEGER DEFAULT 0,  -- 1 = author_id deduzido pelo nome
    date_raw           TEXT,
    date               TEXT,
    edit_info          TEXT,
    edit_count         INTEGER,
    last_edit_date     TEXT,
    dislikes           INTEGER,  -- o fórum só mostra o número (sem a lista)
    vote_title         TEXT,
    content_html       TEXT,
    content_text       TEXT,
    has_signature      INTEGER DEFAULT 0,  -- exibia a assinatura (members.signature_html)
    scraped_at         TEXT
);

CREATE TABLE IF NOT EXISTS post_likes (
    post_id    INTEGER REFERENCES posts(id) ON DELETE CASCADE
                       DEFERRABLE INITIALLY DEFERRED,
    liker_name TEXT,     -- nome como aparece na mensagem
    user_id    INTEGER REFERENCES members(user_id) DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY (post_id, liker_name)
) WITHOUT ROWID;

-- Autores prováveis de mensagens de contas apagadas ("Convidado").
-- Não altera posts.author_id (veja gmbr_scraper/guesses.py e v_post_author_best).
CREATE TABLE IF NOT EXISTS post_author_guesses (
    post_id    INTEGER REFERENCES posts(id) ON DELETE CASCADE
                       DEFERRABLE INITIALLY DEFERRED,
    name       TEXT,
    user_id    INTEGER REFERENCES members(user_id) DEFERRABLE INITIALLY DEFERRED,
    user_link  TEXT,     -- conta: a conta já existia na data da mensagem
                         -- recadastro: conta de mesmo nome criada depois (a pessoa voltou)
    method     TEXT,     -- citacao, edicao, mencao, continuidade
    confidence TEXT,     -- alta, média, baixa
    score      REAL,
    evidence   TEXT,     -- JSON
    PRIMARY KEY (post_id, name, method)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS polls (
    topic_id   INTEGER PRIMARY KEY REFERENCES topics(id) DEFERRABLE INITIALLY DEFERRED,
    question   TEXT,
    closed     INTEGER,
    scraped_at TEXT
);

CREATE TABLE IF NOT EXISTS poll_options (
    topic_id INTEGER REFERENCES polls(topic_id) ON DELETE CASCADE
                     DEFERRABLE INITIALLY DEFERRED,
    position INTEGER,
    text     TEXT,
    votes    INTEGER,
    PRIMARY KEY (topic_id, position)
) WITHOUT ROWID;
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_forums_parent    ON forums(parent_id);
CREATE INDEX IF NOT EXISTS idx_topics_forum     ON topics(forum_id);
CREATE INDEX IF NOT EXISTS idx_posts_topic_pos  ON posts(topic_id, position);
CREATE INDEX IF NOT EXISTS idx_posts_author     ON posts(author_id, date);
CREATE INDEX IF NOT EXISTS idx_posts_authorname ON posts(author_name);
CREATE INDEX IF NOT EXISTS idx_posts_date       ON posts(date);
CREATE INDEX IF NOT EXISTS idx_members_name     ON members(name);
CREATE INDEX IF NOT EXISTS idx_members_group    ON members(group_id);
CREATE INDEX IF NOT EXISTS idx_mgroups_group    ON member_groups(group_id);
CREATE INDEX IF NOT EXISTS idx_mawards_award    ON member_awards(award);
CREATE INDEX IF NOT EXISTS idx_post_likes_user  ON post_likes(user_id);
CREATE INDEX IF NOT EXISTS idx_guesses_name     ON post_author_guesses(name);
CREATE INDEX IF NOT EXISTS idx_guesses_user     ON post_author_guesses(user_id);
"""

# Views recriadas a cada abertura (DROP + CREATE), então mudanças aqui valem
# para bancos existentes sem migração.
VIEWS = {
    "v_topic_stats": """
        WITH agg AS (
            SELECT topic_id, COUNT(*) AS post_count, MAX(position) AS max_pos,
                   MIN(date) AS first_post_date, MAX(date) AS last_post_date,
                   COUNT(DISTINCT COALESCE(CAST(author_id AS TEXT), 'n:' || author_name))
                       AS participants
            FROM posts GROUP BY topic_id)
        SELECT t.id AS topic_id, t.forum_id,
               COALESCE(a.post_count, 0)            AS post_count,
               MAX(COALESCE(a.post_count, 0) - 1, 0) AS reply_count,
               (SELECT COUNT(*) FROM topic_pages tp WHERE tp.topic_id = t.id) AS page_count,
               a.participants,
               f.id AS first_post_id, f.author_id, f.author_name, a.first_post_date,
               l.id AS last_post_id, l.author_id AS last_author_id,
               l.author_name AS last_author_name, a.last_post_date,
               (SELECT COUNT(*) FROM post_likes pl JOIN posts p ON p.id = pl.post_id
                 WHERE p.topic_id = t.id) AS likes
        FROM topics t
        LEFT JOIN agg a   ON a.topic_id = t.id
        LEFT JOIN posts f ON f.topic_id = t.id AND f.position = 0
        LEFT JOIN posts l ON l.topic_id = t.id AND l.position = a.max_pos
    """,
    "v_forum_stats": """
        SELECT f.id AS forum_id, f.name, f.parent_id,
               (SELECT COUNT(*) FROM topics t WHERE t.forum_id = f.id) AS topic_count,
               (SELECT COUNT(*) FROM topics t JOIN posts p ON p.topic_id = t.id
                 WHERE t.forum_id = f.id) AS post_count,
               (SELECT MAX(p.date) FROM topics t JOIN posts p ON p.topic_id = t.id
                 WHERE t.forum_id = f.id) AS last_post_date
        FROM forums f
    """,
    "v_member_stats": """
        WITH agg AS (
            SELECT author_id, COUNT(*) AS post_count,
                   COUNT(DISTINCT topic_id) AS topics_participated,
                   MIN(date) AS first_post_date, MAX(date) AS last_post_date,
                   SUM(position = 0) AS topics_started
            FROM posts WHERE author_id IS NOT NULL GROUP BY author_id),
        recv AS (
            SELECT p.author_id, COUNT(*) AS n FROM post_likes pl
            JOIN posts p ON p.id = pl.post_id
            WHERE p.author_id IS NOT NULL GROUP BY p.author_id),
        given AS (
            SELECT user_id, COUNT(*) AS n FROM post_likes
            WHERE user_id IS NOT NULL GROUP BY user_id)
        SELECT m.user_id, m.name,
               COALESCE(a.post_count, 0)          AS post_count,
               COALESCE(a.topics_started, 0)      AS topics_started,
               COALESCE(a.topics_participated, 0) AS topics_participated,
               a.first_post_date, a.last_post_date,
               COALESCE(r.n, 0) AS likes_received,
               COALESCE(g.n, 0) AS likes_given,
               m.post_count_forum
        FROM members m
        LEFT JOIN agg a   ON a.author_id = m.user_id
        LEFT JOIN recv r  ON r.author_id = m.user_id
        LEFT JOIN given g ON g.user_id = m.user_id
    """,
    # Melhor sugestão por mensagem: soma dos scores de cada nome.
    "v_post_author_best": """
        WITH per_name AS (
            SELECT post_id, name, MAX(user_id) AS user_id, MAX(user_link) AS user_link,
                   SUM(score) AS score,
                   group_concat(method, ',') AS methods,
                   MAX(CASE confidence WHEN 'alta' THEN 3 WHEN 'média' THEN 2 ELSE 1 END)
                       AS best_level
            FROM post_author_guesses GROUP BY post_id, name),
        ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY post_id
                                         ORDER BY score DESC, best_level DESC, name) AS rn,
                      COUNT(*) OVER (PARTITION BY post_id) AS candidates
            FROM per_name)
        SELECT post_id, name, user_id, user_link, score, methods,
               CASE best_level WHEN 3 THEN 'alta' WHEN 2 THEN 'média' ELSE 'baixa' END
                   AS confidence,
               candidates
        FROM ranked WHERE rn = 1
    """,
    "v_guest_authors": """
        SELECT author_name, COUNT(*) AS post_count,
               MIN(date) AS first_post_date, MAX(date) AS last_post_date
        FROM posts WHERE author_id IS NULL GROUP BY author_name
    """,
    "v_poll_results": """
        SELECT p.topic_id, p.question, p.closed,
               COUNT(o.position)          AS option_count,
               COALESCE(SUM(o.votes), 0)  AS total_votes
        FROM polls p LEFT JOIN poll_options o ON o.topic_id = p.topic_id
        GROUP BY p.topic_id
    """,
    "v_poll_options": """
        SELECT o.topic_id, o.position, o.text, o.votes,
               ROUND(100.0 * o.votes / NULLIF(SUM(o.votes) OVER (PARTITION BY o.topic_id), 0),
                     1) AS percent
        FROM poll_options o
    """,
    "v_group_members": """
        SELECT g.id AS group_id, g.name,
               (SELECT COUNT(*) FROM member_groups mg WHERE mg.group_id = g.id) AS member_count
        FROM groups g
    """,
    # Onde o número exibido pelo fórum difere do que está no banco.
    "v_count_checks": """
        SELECT 'topic' AS kind, t.id AS ref_id, t.title AS label,
               t.replies_forum + 1 AS forum_value, s.post_count AS db_value
        FROM topics t JOIN v_topic_stats s ON s.topic_id = t.id
        WHERE t.replies_forum IS NOT NULL AND t.replies_forum + 1 <> s.post_count
        UNION ALL
        SELECT 'member', s.user_id, s.name, s.post_count_forum, s.post_count
        FROM v_member_stats s
        WHERE s.post_count_forum IS NOT NULL AND s.post_count_forum <> s.post_count
    """,
}

# Campos de /uNN que viram colunas de members
_PROFILE_TEXT_COLUMNS = {
    "Quem sou eu": "about_me",
    "Especialidade": "specialty",
    "Plataformas": "platforms",
    "Meus projetos": "projects",
    "Infrações": "infractions",
    "Notas recebidas": "grades_received",
    "Alterações no Nick": "nick_history",
    "Humor": "humor",
}
# Campos "x N" que viram linhas de member_awards
AWARD_FIELDS = ("Medalhas", "Tutoriais", "Moedas", "Ouro", "Prata", "Bronze",
                "Insignia 1", "Insignia 2", "Insignia 3")


def sql_statements(script: str) -> list[str]:
    """Divide um script em comandos completos (respeita ';' em comentários)."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    return out


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _field_text(v) -> str | None:
    if isinstance(v, dict):
        v = v.get("text")
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _to_int(v) -> int | None:
    t = _field_text(v)
    if t is None:
        return None
    m = re.search(r"-?\d[\d.]*", t)
    return int(m.group(0).replace(".", "")) if m else None


def profile_columns(fields: dict) -> tuple[dict, dict]:
    """Campos brutos de /uNN -> (colunas de members, {prêmio: quantidade != 0})."""
    cols = {col: _field_text(fields.get(label))
            for label, col in _PROFILE_TEXT_COLUMNS.items()}
    cols["age"] = _to_int(fields.get("Idade"))
    cols["reputation"] = _to_int(fields.get("Reputação"))
    cols["post_count_forum"] = _to_int(fields.get("Número de Mensagens"))
    awards = {}
    for a in AWARD_FIELDS:
        q = _to_int(fields.get(a))
        if q:
            awards[a] = q
    return cols, awards


_IMG_TEXT_ATTR = re.compile(r"""\s(?:alt|title)=(?:"[^"]*"|'[^']*')""")


def clean_signature(html: str | None) -> str | None:
    """Tira de <img> os alt/title, onde o fórum injeta o título do tópico
    (a mesma assinatura vinha diferente em cada tópico)."""
    if not html or not html.strip():
        return None
    return re.sub(r"<img\b[^>]*>", lambda m: _IMG_TEXT_ATTR.sub("", m.group(0)),
                  html).strip()


def _group_id(group_class: str | None) -> int | None:
    m = re.fullmatch(r"group-(\d+)", group_class or "")
    return int(m.group(1)) if m else None


class Storage:
    def __init__(self, path: str):
        # timeout alto: vários processos podem gravar no mesmo banco
        self.conn = sqlite3.connect(path, timeout=300)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=300000")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='members'").fetchone()
        if has_tables and version < 2:
            from .migrations import migrate_v1
            migrate_v1(self.conn)
        elif has_tables and version < 4:
            from .migrations import migrate_to_v4
            migrate_to_v4(self.conn, version)
        self.conn.executescript(TABLES)
        self.conn.executescript(INDEXES)
        self._create_views()
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.commit()

    def _create_views(self):
        for name, sql in VIEWS.items():
            self.conn.execute(f"DROP VIEW IF EXISTS {name}")
        for name, sql in VIEWS.items():
            self.conn.execute(f"CREATE VIEW {name} AS {sql}")

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()

    # ------------------------------------------------------------ integridade
    def foreign_key_problems(self) -> list[tuple]:
        return [tuple(r) for r in self.conn.execute("PRAGMA foreign_key_check")]

    def _ensure_forum(self, forum_id: int | None):
        if forum_id is not None:
            self.conn.execute("INSERT OR IGNORE INTO forums (id) VALUES (?)", (forum_id,))

    def _ensure_group(self, group_id: int | None, name: str | None = None):
        if group_id is None:
            return
        self.conn.execute("INSERT OR IGNORE INTO groups (id, name) VALUES (?, ?)",
                          (group_id, name))
        if name:
            self.conn.execute("UPDATE groups SET name=? WHERE id=? AND name IS NULL",
                              (name, group_id))

    # ------------------------------------------------------------------ forums
    def upsert_forum(self, forum: dict, parent_id: int | None = None):
        self._ensure_forum(parent_id)
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
            f"SELECT id, last_post_id_forum FROM topics "
            f"WHERE id IN ({','.join('?' * len(topic_ids))})",
            topic_ids,
        ).fetchall()
        return {r["id"]: r["last_post_id_forum"] for r in rows}

    def forums(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM forums ORDER BY id").fetchall()

    # ------------------------------------------------------------------ topics
    def upsert_topic_listing(self, t: dict):
        self._ensure_forum(t.get("forum_id"))
        self.conn.execute(
            """INSERT INTO topics (id, forum_id, title, description, url, section,
                   replies_forum, views_forum, last_post_id_forum, prefix, topic_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 forum_id=COALESCE(topics.forum_id, excluded.forum_id), title=excluded.title,
                 description=excluded.description, url=excluded.url,
                 section=excluded.section,
                 replies_forum=excluded.replies_forum, views_forum=excluded.views_forum,
                 last_post_id_forum=excluded.last_post_id_forum,
                 prefix=excluded.prefix, topic_type=excluded.topic_type""",
            (t["id"], t.get("forum_id"), t.get("title"), t.get("description"), t.get("url"),
             t.get("section"), t.get("replies"), t.get("views"), t.get("last_post_id"),
             t.get("prefix"), t.get("topic_type")),
        )

    def ensure_topic(self, topic_id: int, url: str | None = None):
        self.conn.execute(
            "INSERT OR IGNORE INTO topics (id, url) VALUES (?, ?)", (topic_id, url)
        )

    def get_topic(self, topic_id: int):
        return self.conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()

    _PENDING = ("(completed=0 OR scraped_replies IS NULL OR "
                "(replies_forum IS NOT NULL AND replies_forum <> scraped_replies))")

    def topics_to_scrape(self, forum_ids: list[int] | None = None, refresh: bool = False,
                         shard: tuple[int, int] | None = None,
                         id_range: tuple[int | None, int | None] | None = None):
        """Tópicos pendentes: nunca concluídos, ou com novas respostas desde o crawl."""
        sql = "SELECT * FROM topics"
        cond, args = [], []
        if not refresh:
            cond.append(self._PENDING)
        if forum_ids:
            cond.append(f"forum_id IN ({','.join('?' * len(forum_ids))})")
            args += forum_ids
        if shard:
            cond.append("id % ? = ?")
            args += [shard[1], shard[0]]
        if id_range:
            lo, hi = id_range
            if lo is not None:
                cond.append("id >= ?")
                args.append(lo)
            if hi is not None:
                cond.append("id <= ?")
                args.append(hi)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id"
        return self.conn.execute(sql, args).fetchall()

    def plan_id_ranges(self, n: int) -> list[tuple[int | None, int | None]]:
        """Divide os tópicos pendentes em n faixas contíguas de ID com trabalho
        (páginas a raspar) equilibrado. A primeira faixa começa em None e a
        última termina em None, cobrindo tópicos que surgirem depois."""
        rows = self.conn.execute(
            f"""SELECT t.id,
                      MAX(1, COALESCE(t.replies_forum, 0) / 50 + 1
                             - (SELECT COUNT(*) FROM topic_pages p WHERE p.topic_id = t.id)
                             + CASE WHEN EXISTS (SELECT 1 FROM topic_pages p
                                                 WHERE p.topic_id = t.id) THEN 1 ELSE 0 END)
               FROM topics t
               WHERE {self._PENDING}
               ORDER BY t.id"""
        ).fetchall()
        total = sum(r[1] for r in rows)
        if n <= 1 or not rows:
            return [(None, None)]
        cuts, acc, k = [], 0, 1
        for tid, w in rows:
            acc += w
            if k < n and acc >= total * k / n:
                cuts.append(tid)
                k += 1
        ranges, lo = [], None
        for c in cuts:
            ranges.append((lo, c))
            lo = c + 1
        ranges.append((lo, None))
        return ranges

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
        self._ensure_forum(forum_id)
        posts = self.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE topic_id=?", (topic_id,)
        ).fetchone()[0]
        self.conn.execute(
            """UPDATE topics SET
                 title=COALESCE(title, ?), forum_id=COALESCE(?, forum_id),
                 pages=?, posts_scraped=?, completed=1, scraped_at=?,
                 replies_forum=COALESCE(replies_forum, ?),
                 scraped_replies=COALESCE(replies_forum, ?)
               WHERE id=?""",
            (title, forum_id, pages, posts, now_iso(), posts - 1, posts - 1, topic_id),
        )

    # ------------------------------------------------------------------- posts
    def upsert_post(self, p: dict, page_start: int, position: int):
        self.conn.execute(
            """INSERT INTO posts (id, topic_id, page_start, position, title,
                 author_id, author_name, date_raw, date, edit_info, edit_count,
                 last_edit_date, dislikes, vote_title, content_html, content_text,
                 has_signature, scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 topic_id=excluded.topic_id, page_start=excluded.page_start,
                 position=excluded.position, title=excluded.title,
                 author_id=COALESCE(excluded.author_id,
                                    CASE WHEN posts.author_id_inferred THEN posts.author_id END),
                 author_id_inferred=CASE WHEN excluded.author_id IS NULL
                                         THEN posts.author_id_inferred ELSE 0 END,
                 author_name=excluded.author_name, date_raw=excluded.date_raw,
                 date=excluded.date, edit_info=excluded.edit_info,
                 edit_count=excluded.edit_count, last_edit_date=excluded.last_edit_date,
                 dislikes=excluded.dislikes, vote_title=excluded.vote_title,
                 content_html=excluded.content_html, content_text=excluded.content_text,
                 has_signature=excluded.has_signature,
                 scraped_at=excluded.scraped_at""",
            (p["id"], p["topic_id"], page_start, position, p.get("title"),
             p.get("author_id"), p.get("author_name"), p.get("date_raw"), p.get("date"),
             p.get("edit_info"), p.get("edit_count"), p.get("last_edit_date"),
             p.get("dislikes"), p.get("vote_title"), p.get("content_html"),
             p.get("content_text"), self._signature(p), now_iso()),
        )
        self.set_post_likes(p["id"], p.get("liked_by") or [])

    def _signature(self, p: dict) -> int:
        """A assinatura fica uma vez em members; a mensagem só diz se a exibia.

        O fórum às vezes pendura numa mensagem a assinatura de outra do mesmo
        tópico (bloco sig<ID de outra mensagem>); por isso a primeira vista do
        membro não é substituída por versões diferentes."""
        sig = clean_signature(p.get("signature_html"))
        if not sig:
            return 0
        uid = p.get("author_id")
        if uid is not None:
            self.conn.execute(
                "UPDATE members SET signature_html=? WHERE user_id=? AND signature_html IS NULL",
                (sig, uid))
        return 1

    def set_post_likes(self, post_id: int, names: list[str]):
        self.conn.execute("DELETE FROM post_likes WHERE post_id=?", (post_id,))
        for n in dict.fromkeys(names):
            row = self.conn.execute(
                "SELECT user_id FROM members WHERE name=? ORDER BY user_id DESC LIMIT 1",
                (n,)).fetchone()
            self.conn.execute(
                "INSERT INTO post_likes (post_id, liker_name, user_id) VALUES (?,?,?)",
                (post_id, n, row[0] if row else None))

    def resolve_like_users(self) -> int:
        """Liga curtidas a membros pelo nome, quando o membro chegou depois."""
        cur = self.conn.execute(
            """UPDATE post_likes SET user_id = (
                   SELECT MAX(m.user_id) FROM members m WHERE m.name = post_likes.liker_name)
               WHERE user_id IS NULL""")
        return cur.rowcount

    # ----------------------------------------------------------------- members
    def upsert_member(self, prof: dict, post_id: int | None = None,
                      signature_html: str | None = None):
        """Atualiza o membro com o perfil exibido ao lado da mensagem.

        Os dados de perfil no Forumeiros refletem o estado ATUAL do membro (não
        o da época da mensagem), então qualquer mensagem serve; guardamos sempre
        o último visto, sem apagar campos que vierem vazios. Autores sem /uNN
        (convidados, contas apagadas) não viram membros: ficam só no nome
        gravado em posts.author_name.
        """
        uid = prof.get("user_id")
        if uid is None:
            return
        gid = _group_id(prof.get("group_class"))
        self._ensure_group(gid)
        self.conn.execute(
            """INSERT INTO members (user_id, name, profile_url, avatar_url, group_id,
                 color, rank_title, rank_image, joined_raw, joined_date, reputation,
                 post_count_forum, website, sidebar_fields, sidebar_images,
                 updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 name=COALESCE(excluded.name, members.name),
                 profile_url=COALESCE(excluded.profile_url, members.profile_url),
                 avatar_url=COALESCE(excluded.avatar_url, members.avatar_url),
                 group_id=COALESCE(excluded.group_id, members.group_id),
                 color=COALESCE(excluded.color, members.color),
                 rank_title=COALESCE(excluded.rank_title, members.rank_title),
                 rank_image=COALESCE(excluded.rank_image, members.rank_image),
                 joined_raw=COALESCE(excluded.joined_raw, members.joined_raw),
                 joined_date=COALESCE(excluded.joined_date, members.joined_date),
                 reputation=COALESCE(excluded.reputation, members.reputation),
                 post_count_forum=COALESCE(excluded.post_count_forum,
                                           members.post_count_forum),
                 website=COALESCE(excluded.website, members.website),
                 sidebar_fields=CASE WHEN excluded.sidebar_fields <> '{}'
                                   THEN excluded.sidebar_fields ELSE members.sidebar_fields END,
                 sidebar_images=CASE WHEN excluded.sidebar_images <> '[]'
                                   THEN excluded.sidebar_images ELSE members.sidebar_images END,
                 updated_at=excluded.updated_at""",
            (uid, prof.get("name"), prof.get("profile_url"), prof.get("avatar_url"),
             gid, prof.get("color"), prof.get("rank_title"),
             prof.get("rank_image"), prof.get("joined_raw"), prof.get("joined_date"),
             prof.get("reputation"), prof.get("post_count"), prof.get("website"),
             json.dumps(prof.get("extra_fields") or {}, ensure_ascii=False),
             json.dumps(prof.get("profile_images") or [], ensure_ascii=False),
             now_iso()),
        )

    def upsert_memberlist_row(self, m: dict):
        """Membro vindo de /memberlist (nome atual, datas, contador)."""
        self.conn.execute(
            """INSERT INTO members (user_id, name, profile_url, avatar_url, joined_raw,
                 joined_date, post_count_forum, website, humor, last_visit_raw, last_visit,
                 in_memberlist, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 name=COALESCE(excluded.name, members.name),
                 profile_url=COALESCE(excluded.profile_url, members.profile_url),
                 avatar_url=COALESCE(excluded.avatar_url, members.avatar_url),
                 joined_raw=COALESCE(members.joined_raw, excluded.joined_raw),
                 joined_date=COALESCE(members.joined_date, excluded.joined_date),
                 post_count_forum=COALESCE(excluded.post_count_forum,
                                           members.post_count_forum),
                 website=COALESCE(excluded.website, members.website),
                 humor=COALESCE(excluded.humor, members.humor),
                 last_visit_raw=excluded.last_visit_raw, last_visit=excluded.last_visit,
                 in_memberlist=1, updated_at=excluded.updated_at""",
            (m["user_id"], m.get("name"), m.get("profile_url"), m.get("avatar_url"),
             m.get("joined_raw"), m.get("joined_date"), m.get("post_count"),
             m.get("website"), m.get("humor"), m.get("last_visit_raw"),
             m.get("last_visit"), now_iso()))

    def profiles_pending(self, shard: tuple[int, int] | None = None) -> list[int]:
        """IDs de membros sem perfil raspado, quem tem mais mensagens primeiro."""
        rows = self.conn.execute(
            "SELECT user_id FROM members WHERE profile_scraped_at IS NULL "
            "ORDER BY COALESCE(post_count_forum, 0) DESC, user_id").fetchall()
        ids = [r[0] for r in rows]
        return [i for i in ids if not shard or i % shard[1] == shard[0]]

    def save_profile(self, user_id: int, prof: dict | None, status: str = "ok"):
        if prof is None:
            self.conn.execute(
                "UPDATE members SET profile_status=?, profile_scraped_at=? WHERE user_id=?",
                (status, now_iso(), user_id))
            return
        f = prof.get("fields") or {}
        cols, awards = profile_columns(f)
        rank = f.get("Rank") if isinstance(f.get("Rank"), str) else None
        sets = ", ".join(f"{c}=COALESCE(?, {c})" for c in cols)
        self.conn.execute(
            f"""UPDATE members SET {sets},
                 avatar_url=COALESCE(?, avatar_url), color=COALESCE(?, color),
                 rank_title=COALESCE(rank_title, ?),
                 last_visit_raw=COALESCE(?, last_visit_raw),
                 last_visit=COALESCE(?, last_visit),
                 profile_fields=?, profile_status=?, profile_scraped_at=?
               WHERE user_id=?""",
            (*cols.values(), prof.get("avatar_url"), prof.get("color"), rank,
             prof.get("last_visit_raw"), prof.get("last_visit"),
             json.dumps(f, ensure_ascii=False), status, now_iso(), user_id))
        self.conn.execute("DELETE FROM member_awards WHERE user_id=?", (user_id,))
        self.conn.executemany(
            "INSERT INTO member_awards (user_id, award, quantity) VALUES (?,?,?)",
            [(user_id, a, q) for a, q in awards.items()])
        self.conn.execute("DELETE FROM member_groups WHERE user_id=?", (user_id,))
        for g in prof.get("groups") or []:
            self._ensure_group(g["id"], g.get("name"))
            self.conn.execute(
                "INSERT OR IGNORE INTO member_groups (user_id, group_id) VALUES (?,?)",
                (user_id, g["id"]))

    def upsert_group(self, g: dict):
        gid = g.get("id") if g.get("id") is not None else _group_id(g.get("class"))
        self.conn.execute(
            """INSERT INTO groups (id, name, slug, color) VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=COALESCE(excluded.name, groups.name),
                 slug=COALESCE(excluded.slug, groups.slug),
                 color=COALESCE(excluded.color, groups.color)""",
            (gid, g.get("name"), g.get("slug"), g.get("color")),
        )

    # ---------------------------------------------------------------- enquetes
    def upsert_poll(self, topic_id: int, poll: dict):
        self.conn.execute(
            """INSERT INTO polls (topic_id, question, closed, scraped_at) VALUES (?,?,?,?)
               ON CONFLICT(topic_id) DO UPDATE SET question=excluded.question,
                 closed=excluded.closed, scraped_at=excluded.scraped_at""",
            (topic_id, poll.get("question"), int(bool(poll.get("closed"))), now_iso()))
        self.conn.execute("DELETE FROM poll_options WHERE topic_id=?", (topic_id,))
        for i, o in enumerate(poll.get("options") or []):
            self.conn.execute(
                "INSERT INTO poll_options (topic_id, position, text, votes) VALUES (?,?,?,?)",
                (topic_id, i, o.get("text"), o.get("votes")))

    def poll_topics_pending(self, shard: tuple[int, int] | None = None) -> list[int]:
        rows = self.conn.execute(
            "SELECT id FROM topics WHERE prefix LIKE '%Vota%' "
            "AND id NOT IN (SELECT topic_id FROM polls) ORDER BY id").fetchall()
        ids = [r[0] for r in rows]
        return [i for i in ids if not shard or i % shard[1] == shard[0]]

    # ---------------------------------------------------------------- relatórios
    def members_report(self) -> list[dict]:
        """Membros + estatísticas calculadas na hora a partir das mensagens."""
        rows = self.conn.execute(
            """SELECT m.*, g.name AS group_name,
                      s.post_count, s.topics_started, s.topics_participated,
                      s.first_post_date, s.last_post_date,
                      s.likes_received, s.likes_given,
                      (SELECT json_group_array(json_object('id', mg.group_id, 'name', gg.name))
                         FROM member_groups mg JOIN groups gg ON gg.id = mg.group_id
                        WHERE mg.user_id = m.user_id) AS groups,
                      (SELECT json_group_object(award, quantity) FROM member_awards a
                        WHERE a.user_id = m.user_id) AS awards
               FROM members m
               LEFT JOIN groups g ON g.id = m.group_id
               JOIN v_member_stats s ON s.user_id = m.user_id
               ORDER BY s.post_count DESC, m.user_id"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k, empty in (("sidebar_fields", "{}"), ("sidebar_images", "[]"),
                             ("profile_fields", "{}"), ("groups", "[]"), ("awards", "{}")):
                d[k] = json.loads(d.get(k) or empty)
            out.append(d)
        return out
