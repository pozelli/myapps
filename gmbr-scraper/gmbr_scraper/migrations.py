"""Migração do banco da versão 1 (schema original) para a 2.

Roda uma vez, numa transação só: se algo falhar, o banco fica como estava.
"""
from __future__ import annotations

import json
import logging
import sqlite3

log = logging.getLogger("gmbr.migrations")

_OLD = ("forums", "topics", "topic_pages", "posts", "groups", "members",
        "polls", "poll_options")


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    from .storage import INDEXES, TABLES, _group_id, now_iso, profile_columns

    log.warning("Migrando o banco para o schema 2…")
    old_level = conn.isolation_level
    conn.commit()
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Colunas acrescentadas ao longo da v1 podem faltar em bancos antigos.
        for table, col in (("forums", "listing_next_start"), ("topics", "prefix"),
                           ("topics", "topic_type"), ("members", "humor"),
                           ("members", "last_visit_raw"), ("members", "last_visit"),
                           ("members", "in_memberlist"), ("members", "profile_fields"),
                           ("members", "profile_groups"), ("members", "profile_status"),
                           ("members", "profile_scraped_at")):
            if col not in _cols(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        for (idx,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ).fetchall():
            conn.execute(f"DROP INDEX {idx}")
        for (view,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view'").fetchall():
            conn.execute(f"DROP VIEW {view}")
        for t in _OLD:
            conn.execute(f"ALTER TABLE {t} RENAME TO {t}_v1")

        for stmt in (TABLES + INDEXES).split(";"):
            if stmt.strip():
                conn.execute(stmt)

        conn.execute("""INSERT INTO forums SELECT id, name, slug, url, parent_id,
                          topics_listed, scraped_at, listing_next_start FROM forums_v1""")
        conn.execute("""INSERT INTO topics (id, forum_id, title, description, url, section,
                          prefix, topic_type, replies_forum, views_forum, last_post_id_forum,
                          scraped_replies, pages, posts_scraped, completed, scraped_at)
                        SELECT id, forum_id, title, description, url, section, prefix,
                          topic_type, replies, views, last_post_id, scraped_replies, pages,
                          posts_scraped, completed, scraped_at FROM topics_v1""")
        conn.execute("INSERT INTO topic_pages SELECT * FROM topic_pages_v1")
        conn.execute("""INSERT INTO groups (id, name, slug, color)
                        SELECT id, name, slug, color FROM groups_v1 WHERE id IS NOT NULL""")

        # Membros: só contas com /uNN. Convidados/contas apagadas ficam no nome
        # gravado em cada mensagem.
        conn.execute("""INSERT INTO members (user_id, name, profile_url, avatar_url,
                          color, rank_title, rank_image, joined_raw, joined_date,
                          last_visit_raw, last_visit, reputation, post_count_forum, website,
                          humor, signature_html, sidebar_fields, sidebar_images,
                          profile_fields, in_memberlist, profile_status,
                          profile_scraped_at, updated_at)
                        SELECT user_id, name, profile_url, avatar_url, color, rank_title,
                          rank_image, joined_raw, joined_date, last_visit_raw, last_visit,
                          reputation, post_count, website, humor, signature_html,
                          extra_fields, profile_images, profile_fields, in_memberlist,
                          profile_status, profile_scraped_at, updated_at
                        FROM members_v1 WHERE user_id IS NOT NULL""")
        rows = conn.execute("""SELECT user_id, group_class, profile_fields, profile_groups
                               FROM members_v1 WHERE user_id IS NOT NULL""").fetchall()
        for uid, gclass, pf, pg in rows:
            gid = _group_id(gclass)
            if gid is not None:
                conn.execute("INSERT OR IGNORE INTO groups (id) VALUES (?)", (gid,))
                conn.execute("UPDATE members SET group_id=? WHERE user_id=?", (gid, uid))
            if pf:
                cols, awards = profile_columns(json.loads(pf))
                sets = ", ".join(f"{c}=COALESCE(?, {c})" for c in cols)
                conn.execute(f"UPDATE members SET {sets} WHERE user_id=?",
                             (*cols.values(), uid))
                conn.executemany(
                    "INSERT INTO member_awards (user_id, award, quantity) VALUES (?,?,?)",
                    [(uid, a, q) for a, q in awards.items()])
            for g in json.loads(pg or "[]"):
                conn.execute("INSERT OR IGNORE INTO groups (id, name) VALUES (?,?)",
                             (g["id"], g.get("name")))
                conn.execute("UPDATE groups SET name=? WHERE id=? AND name IS NULL",
                             (g.get("name"), g["id"]))
                conn.execute("INSERT OR IGNORE INTO member_groups VALUES (?,?)",
                             (uid, g["id"]))

        conn.execute("""INSERT INTO posts (id, topic_id, page_start, position, title,
                          author_id, author_name, date_raw, date, edit_info, edit_count,
                          last_edit_date, dislikes, vote_title, content_html, content_text,
                          signature_html, scraped_at)
                        SELECT id, topic_id, page_start, position, title, author_id,
                          author_name, date_raw, date, edit_info, edit_count, last_edit_date,
                          dislikes, vote_title, content_html, content_text, signature_html,
                          scraped_at FROM posts_v1""")
        # Mensagens sem link para o perfil cujo nome é de um único membro que já
        # estava inscrito na data da mensagem: liga ao membro e marca como deduzido.
        n = conn.execute("""
            UPDATE posts SET author_id = (
                SELECT m.user_id FROM members m WHERE m.name = posts.author_name
                   AND m.joined_date <= substr(posts.date, 1, 10)),
              author_id_inferred = 1
            WHERE author_id IS NULL AND posts.date IS NOT NULL
              AND (SELECT COUNT(*) FROM members m WHERE m.name = posts.author_name
                     AND m.joined_date <= substr(posts.date, 1, 10)) = 1""").rowcount
        log.warning("%d mensagens ligadas ao autor pelo nome", n)

        conn.execute("""INSERT OR IGNORE INTO post_likes (post_id, liker_name, user_id)
                        SELECT p.id, j.value,
                          (SELECT MAX(m.user_id) FROM members m WHERE m.name = j.value)
                        FROM posts_v1 p, json_each(p.liked_by) j
                        WHERE p.liked_by IS NOT NULL AND p.liked_by <> '[]'""")

        conn.execute("""INSERT INTO polls (topic_id, question, closed, scraped_at)
                        SELECT topic_id, question, closed, scraped_at FROM polls_v1""")
        conn.execute("""INSERT INTO poll_options (topic_id, position, text, votes)
                        SELECT topic_id, position, text, votes FROM poll_options_v1""")

        # Pais que faltarem viram registros mínimos (para as chaves estrangeiras).
        conn.execute("""INSERT OR IGNORE INTO forums (id)
                        SELECT DISTINCT forum_id FROM topics WHERE forum_id IS NOT NULL
                        UNION SELECT DISTINCT parent_id FROM forums
                        WHERE parent_id IS NOT NULL""")

        for t in _OLD:
            conn.execute(f"DROP TABLE {t}_v1")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"chaves estrangeiras quebradas após a migração: "
                               f"{[tuple(p) for p in problems[:10]]}")
        conn.execute("PRAGMA user_version=2")
        conn.execute("COMMIT")
        log.warning("Migração concluída (%s)", now_iso())
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old_level
