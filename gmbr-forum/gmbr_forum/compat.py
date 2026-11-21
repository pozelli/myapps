"""Compatibilidade com o schema 2+ do gmbr-scraper (PRAGMA user_version >= 2).

No schema novo as contagens viraram views (v_topic_stats...), curtidas foram
para post_likes, grupos do perfil para member_groups e members passou a ter
user_id como chave. O fórum foi escrito para o schema 1; em vez de reescrever
todas as consultas, cada conexão ganha VIEWs TEMP com os nomes e colunas
antigos (objetos TEMP têm precedência sobre os do banco principal).

As partes caras (estatísticas por tópico, curtidas por mensagem, grupos por
membro) são materializadas em tabelas c_* — no índice de busca, que já é
reconstruído quando o banco muda.
"""
from __future__ import annotations

import sqlite3


def schema_version(conn: sqlite3.Connection, schema: str = "main") -> int:
    return conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0]


def needs_compat(conn: sqlite3.Connection, schema: str = "main") -> bool:
    return schema_version(conn, schema) >= 2


def materialize(conn: sqlite3.Connection, src: str = "main", dst: str = "temp") -> None:
    """Cria {dst}.c_topic_stats, c_post_likes e c_member_groups a partir de {src}."""
    for t in ("c_topic_stats", "c_post_likes", "c_member_groups"):
        conn.execute(f"DROP TABLE IF EXISTS {dst}.{t}")
    conn.execute(f"""
        CREATE TABLE {dst}.c_topic_stats AS
        SELECT st.topic_id, st.post_count, st.reply_count, st.participants,
               st.author_id, st.author_name, st.first_post_date,
               st.last_post_id, st.last_author_name, st.last_post_date,
               lp.date_raw AS last_post_date_raw
        FROM {src}.v_topic_stats st LEFT JOIN {src}.posts lp ON lp.id = st.last_post_id""")
    conn.execute(f"""
        CREATE TABLE {dst}.c_post_likes AS
        SELECT post_id, COUNT(*) AS likes, json_group_array(liker_name) AS liked_by
        FROM {src}.post_likes GROUP BY post_id""")
    conn.execute(f"""
        CREATE TABLE {dst}.c_member_groups AS
        SELECT mg.user_id, json_group_array(json_object('id', g.id, 'name', g.name)) AS profile_groups
        FROM {src}.member_groups mg JOIN {src}.groups g ON g.id = mg.group_id
        GROUP BY mg.user_id""")
    conn.execute(f"CREATE UNIQUE INDEX {dst}.c_topic_stats_pk ON c_topic_stats(topic_id)")
    conn.execute(f"CREATE UNIQUE INDEX {dst}.c_post_likes_pk ON c_post_likes(post_id)")
    conn.execute(f"CREATE UNIQUE INDEX {dst}.c_member_groups_pk ON c_member_groups(user_id)")


def has_materialized(conn: sqlite3.Connection, schema: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {schema}.c_topic_stats LIMIT 1")
        return True
    except sqlite3.Error:
        return False


def install_views(conn: sqlite3.Connection, stats: str = "temp") -> None:
    """VIEWs TEMP com o formato do schema 1. `stats` é o schema das tabelas c_*."""
    views = {
        "topics": f"""
            SELECT t.id, t.forum_id, t.title, t.description, t.url, t.section,
                   st.author_name,
                   COALESCE(t.replies_forum, st.reply_count) AS replies,
                   t.views_forum AS views,
                   COALESCE(t.last_post_id_forum, st.last_post_id) AS last_post_id,
                   st.last_author_name AS last_post_author,
                   st.last_post_date_raw, st.last_post_date,
                   t.pages, t.posts_scraped, t.scraped_replies, t.completed, t.scraped_at,
                   t.prefix, t.topic_type
            FROM main.topics t LEFT JOIN {stats}.c_topic_stats st ON st.topic_id = t.id""",
        "posts": f"""
            SELECT p.id, p.topic_id, p.page_start, p.position, p.title, p.author_name,
                   p.author_id, p.date_raw, p.date, p.edit_info, p.edit_count, p.last_edit_date,
                   COALESCE(l.likes, 0) AS likes, p.dislikes, NULL AS likes_text,
                   COALESCE(l.liked_by, '[]') AS liked_by, p.vote_title,
                   p.content_html, p.content_text,
                   CASE WHEN p.has_signature THEN m.signature_html END AS signature_html,
                   p.scraped_at
            FROM main.posts p
            LEFT JOIN {stats}.c_post_likes l ON l.post_id = p.id
            LEFT JOIN main.members m ON m.user_id = p.author_id""",
        "members": f"""
            SELECT m.name, m.user_id, m.profile_url, m.avatar_url,
                   'group-' || m.group_id AS group_class, m.color, m.rank_title, m.rank_image,
                   m.joined_raw, m.joined_date, m.reputation, m.post_count_forum AS post_count,
                   m.website, m.signature_html, m.sidebar_fields AS extra_fields,
                   m.sidebar_images AS profile_images, NULL AS profile_post_id,
                   NULL AS first_seen_post_id, 0 AS is_guest, m.updated_at,
                   m.humor, m.last_visit_raw, m.last_visit, m.in_memberlist, m.profile_fields,
                   g.profile_groups, m.profile_status, m.profile_scraped_at, m.nick_history
            FROM main.members m LEFT JOIN {stats}.c_member_groups g ON g.user_id = m.user_id""",
        "groups": "SELECT 'group-' || id AS class, id, name, slug, color FROM main.groups",
        "polls": """
            SELECT p.topic_id, p.question,
                   (SELECT SUM(votes) FROM main.poll_options o WHERE o.topic_id = p.topic_id) AS total_votes,
                   p.closed, p.scraped_at
            FROM main.polls p""",
        "poll_options": "SELECT topic_id, position, text, votes, percent FROM main.v_poll_options",
    }
    for name, sql in views.items():
        conn.execute(f"DROP VIEW IF EXISTS temp.{name}")
        conn.execute(f"CREATE TEMP VIEW {name} AS {sql}")


def prepare(conn: sqlite3.Connection, stats: str | None = None) -> bool:
    """Instala a compatibilidade numa conexão ao banco do scraper, se precisar.
    Sem `stats` (ou sem as tabelas c_* lá), materializa em TEMP."""
    if not needs_compat(conn):
        return False
    if not stats or not has_materialized(conn, stats):
        materialize(conn, "main", "temp")
        stats = "temp"
    install_views(conn, stats)
    return True
