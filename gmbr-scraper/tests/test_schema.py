"""Schema 2: contagens calculadas por views, chaves estrangeiras e migração da v1."""
import json
import sqlite3

import pytest

from gmbr_scraper.storage import Storage

V1 = """
CREATE TABLE forums (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, url TEXT,
  parent_id INTEGER, topics_listed INTEGER, scraped_at TEXT, listing_next_start INTEGER);
CREATE TABLE topics (id INTEGER PRIMARY KEY, forum_id INTEGER, title TEXT, description TEXT,
  url TEXT, section TEXT, author_name TEXT, replies INTEGER, views INTEGER,
  last_post_id INTEGER, last_post_author TEXT, last_post_date_raw TEXT, last_post_date TEXT,
  pages INTEGER, posts_scraped INTEGER, scraped_replies INTEGER, completed INTEGER DEFAULT 0,
  scraped_at TEXT, prefix TEXT, topic_type TEXT);
CREATE TABLE topic_pages (topic_id INTEGER, start INTEGER, post_count INTEGER,
  scraped_at TEXT, PRIMARY KEY (topic_id, start));
CREATE TABLE posts (id INTEGER PRIMARY KEY, topic_id INTEGER, page_start INTEGER,
  position INTEGER, title TEXT, author_name TEXT, author_id INTEGER, date_raw TEXT, date TEXT,
  edit_info TEXT, edit_count INTEGER, last_edit_date TEXT, likes INTEGER, dislikes INTEGER,
  likes_text TEXT, liked_by TEXT, vote_title TEXT, content_html TEXT, content_text TEXT,
  signature_html TEXT, scraped_at TEXT);
CREATE INDEX idx_posts_topic ON posts(topic_id);
CREATE INDEX idx_posts_author ON posts(author_name);
CREATE TABLE groups (class TEXT PRIMARY KEY, id INTEGER, name TEXT, slug TEXT, color TEXT);
CREATE TABLE members (name TEXT PRIMARY KEY, user_id INTEGER, profile_url TEXT,
  avatar_url TEXT, group_class TEXT, color TEXT, rank_title TEXT, rank_image TEXT,
  joined_raw TEXT, joined_date TEXT, reputation INTEGER, post_count INTEGER, website TEXT,
  signature_html TEXT, extra_fields TEXT, profile_images TEXT, profile_post_id INTEGER,
  first_seen_post_id INTEGER, is_guest INTEGER DEFAULT 0, updated_at TEXT, humor TEXT,
  last_visit_raw TEXT, last_visit TEXT, in_memberlist INTEGER, profile_fields TEXT,
  profile_groups TEXT, profile_status TEXT, profile_scraped_at TEXT);
CREATE TABLE polls (topic_id INTEGER PRIMARY KEY, question TEXT, total_votes INTEGER,
  closed INTEGER, scraped_at TEXT);
CREATE TABLE poll_options (topic_id INTEGER, position INTEGER, text TEXT, votes INTEGER,
  percent REAL, PRIMARY KEY (topic_id, position));

INSERT INTO forums (id, name) VALUES (1, 'Geral');
INSERT INTO topics (id, forum_id, title, replies, last_post_id, prefix)
  VALUES (10, 1, 'Olá', 2, 102, '[Votação]');
INSERT INTO groups VALUES ('group-14', 14, 'Administradores', 'adm', '#F00');
INSERT INTO members (name, user_id, group_class, post_count, joined_date, profile_fields,
  profile_groups, extra_fields, profile_images)
  VALUES ('Ana', 1, 'group-14', 9, '2010-01-01',
          '{"Idade": "30", "Ouro": "x 2", "Moedas": "x 0", "Quem sou eu": "Oi"}',
          '[{"id": 14, "name": "Administradores"}, {"id": 8, "name": "Membros"}]', '{}', '[]'),
         ('Bia', 2, NULL, 1, '2015-01-01', NULL, NULL, '{}', '[]'),
         ('Convidado', NULL, NULL, NULL, NULL, NULL, NULL, '{}', '[]');
INSERT INTO posts (id, topic_id, position, author_name, author_id, date, likes, liked_by,
  signature_html) VALUES
  (100, 10, 0, 'Ana', 1, '2012-01-01T10:00:00', 2, '["Bia", "Ana"]',
   '<img alt="Olá Logo" src="l.png"/>'),
  (101, 10, 1, 'Bia', NULL, '2016-01-01T10:00:00', 0, '[]', NULL),
  (102, 10, 2, 'Convidado', NULL, '2016-01-02T10:00:00', 0, '[]', ''),
  (103, 10, 3, 'Ana', 1, '2016-01-03T10:00:00', 0, '[]',
   '<img alt=''Outro tópico Logo'' src="l.png"/>'),
  (104, 10, 4, 'Ana', 1, '2016-01-04T10:00:00', 0, '[]', NULL);
INSERT INTO polls VALUES (10, 'Qual?', 5, 0, NULL);
INSERT INTO poll_options VALUES (10, 0, 'A', 2, 40.0), (10, 1, 'B', 3, 60.0);
"""


def test_migration_from_v1(tmp_path):
    path = str(tmp_path / "v1.sqlite")
    c = sqlite3.connect(path)
    c.executescript(V1)
    c.close()
    db = Storage(path)
    q = lambda sql: [tuple(r) for r in db.conn.execute(sql)]  # noqa: E731
    assert q("PRAGMA user_version") == [(3,)]
    assert db.foreign_key_problems() == []
    # convidado não vira membro; o nome continua na mensagem
    assert q("SELECT user_id, name, group_id, age, about_me, post_count_forum "
             "FROM members ORDER BY user_id") == [(1, "Ana", 14, 30, "Oi", 9),
                                                  (2, "Bia", None, None, None, 1)]
    assert q("SELECT award, quantity FROM member_awards") == [("Ouro", 2)]
    assert q("SELECT user_id, group_id FROM member_groups ORDER BY group_id") == \
        [(1, 8), (1, 14)]
    assert q("SELECT name FROM groups WHERE id=8") == [("Membros",)]
    # mensagem sem link, nome de membro já inscrito na data: ligada e marcada
    assert q("SELECT id, author_id, author_id_inferred FROM posts ORDER BY id") == \
        [(100, 1, 0), (101, 2, 1), (102, None, 0), (103, 1, 0), (104, 1, 0)]
    # assinatura: uma por membro, sem o alt com o título do tópico
    assert q("SELECT signature_html FROM members WHERE user_id=1") == \
        [('<img src="l.png"/>',)]
    assert q("SELECT id, has_signature, signature_override_html FROM posts ORDER BY id") == \
        [(100, 1, None), (101, 0, None), (102, 0, None), (103, 1, None), (104, 0, None)]
    assert "signature_html" not in {r[1] for r in q("PRAGMA table_info(posts)")}
    assert q("SELECT liker_name, user_id FROM post_likes ORDER BY user_id") == \
        [("Ana", 1), ("Bia", 2)]
    # contagens calculadas
    assert q("SELECT post_count, reply_count, author_name, last_author_name, likes "
             "FROM v_topic_stats") == [(5, 4, "Ana", "Ana", 2)]
    assert q("SELECT user_id, post_count, topics_started, likes_received, likes_given "
             "FROM v_member_stats ORDER BY user_id") == [(1, 3, 1, 2, 1), (2, 1, 0, 0, 1)]
    assert q("SELECT total_votes, option_count FROM v_poll_results") == [(5, 2)]
    assert q("SELECT percent FROM v_poll_options ORDER BY position") == [(40.0,), (60.0,)]
    assert q("SELECT kind, ref_id FROM v_count_checks ORDER BY kind, ref_id") == \
        [("member", 1), ("topic", 10)]  # perfil diz 9 (banco 3); tópico diz 3 (banco 5)
    assert q("SELECT author_name, post_count FROM v_guest_authors") == [("Convidado", 1)]
    names = {r[0] for r in q("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_posts_topic_pos", "idx_posts_author", "idx_posts_authorname"} <= names
    db.close()
    Storage(path).close()  # reabrir não migra de novo


def test_foreign_keys_enforced(tmp_path):
    db = Storage(str(tmp_path / "t.sqlite"))
    db.conn.execute("INSERT INTO posts (id, topic_id) VALUES (1, 999)")
    with pytest.raises(sqlite3.IntegrityError):
        db.commit()  # chaves adiadas: verificadas no commit


def test_counts_follow_posts(tmp_path):
    db = Storage(str(tmp_path / "t.sqlite"))
    db.upsert_topic_listing({"id": 5, "forum_id": 3, "replies": 1, "last_post_id": 2})
    db.upsert_member({"user_id": 7, "name": "Caio", "group_class": "group-11"})
    for i, (uid, likers) in enumerate([(7, []), (None, ["Caio"])]):
        db.upsert_post({"id": i + 1, "topic_id": 5, "author_id": uid,
                        "author_name": "Caio" if uid else "Convidado",
                        "liked_by": likers}, 0, i)
    db.commit()
    assert db.foreign_key_problems() == []
    row = db.conn.execute("SELECT post_count, likes FROM v_topic_stats").fetchone()
    assert tuple(row) == (2, 1)
    db.conn.execute("DELETE FROM posts WHERE id=2")
    db.commit()
    row = db.conn.execute("SELECT post_count, likes FROM v_topic_stats").fetchone()
    assert tuple(row) == (1, 0)  # nada a "atualizar": as views contam na hora
    assert db.conn.execute("SELECT kind FROM v_count_checks").fetchall()[0][0] == "topic"


def test_v2_to_v3_and_guesses(tmp_path):
    from gmbr_scraper.guesses import build_guesses
    path = str(tmp_path / "v1.sqlite")
    c = sqlite3.connect(path)
    c.executescript(V1)
    c.executescript("""
    INSERT INTO members (name, user_id, joined_date, extra_fields, profile_images)
      VALUES ('Moderador', 3, '2009-01-01', '{}', '[]'), ('Carlos', 4, '2009-01-01', '{}', '[]');
    INSERT INTO topics (id, forum_id, title, replies) VALUES (20, 1, 'Ajuda', 5);
    INSERT INTO posts (id, topic_id, position, author_name, author_id, date, content_text,
                       content_html, edit_info, liked_by) VALUES
      (200, 20, 0, 'Convidad', NULL, '2010-01-01T10:00:00',
       'Como faço para mover o personagem com o teclado no jogo?', '', NULL, '[]'),
      (201, 20, 1, 'Ana', 1, '2010-01-01T11:00:00', 'resposta',
       '<blockquote><cite>Zeca escreveu:</cite>Como faço para mover o personagem com o
        teclado no jogo?</blockquote>Use as setas.', NULL, '[]'),
      (202, 20, 2, 'Convidad', NULL, '2010-01-01T12:00:00', 'Valeu, funcionou!', '',
       'Última edição por Zeca em Sex 01 Jan 2010, 12:30, editado 1 vez(es)', '[]'),
      (203, 20, 3, 'Convidad', NULL, '2010-01-01T13:00:00', 'Mais uma dúvida.', '', NULL,
       '[]'),
      (204, 20, 4, 'Carlos', 4, '2010-01-01T14:00:00', 'Zeca, abre outro tópico.', '',
       NULL, '[]');
    """)
    c.close()
    db = Storage(path)
    stats = build_guesses(db.conn)
    q = lambda sql: [tuple(r) for r in db.conn.execute(sql)]  # noqa: E731
    got = q("SELECT post_id, name, method, confidence FROM post_author_guesses "
            "ORDER BY post_id, method")
    assert got == [(200, "Zeca", "citacao", "alta"),
                   (202, "Zeca", "edicao", "média"),
                   (203, "Zeca", "continuidade", "baixa"),
                   (203, "Zeca", "mencao", "baixa")]
    assert q("SELECT post_id, name, confidence, candidates FROM v_post_author_best "
             "WHERE post_id=203") == [(203, "Zeca", "baixa", 1)]
    assert stats["mensagens"] == 3
    assert q("SELECT author_id FROM posts WHERE id=200") == [(None,)]  # não altera
    db.close()
