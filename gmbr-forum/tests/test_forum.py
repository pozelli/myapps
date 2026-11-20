import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

from gmbr_forum.app import Forum, make_handler
from gmbr_forum.sanitize import clean_html

SCHEMA = """
CREATE TABLE forums (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, url TEXT, parent_id INTEGER,
  topics_listed INTEGER, scraped_at TEXT, listing_next_start INTEGER);
CREATE TABLE topics (id INTEGER PRIMARY KEY, forum_id INTEGER, title TEXT, description TEXT, url TEXT,
  section TEXT, author_name TEXT, replies INTEGER, views INTEGER, last_post_id INTEGER,
  last_post_author TEXT, last_post_date_raw TEXT, last_post_date TEXT, pages INTEGER,
  posts_scraped INTEGER, scraped_replies INTEGER, completed INTEGER DEFAULT 0, scraped_at TEXT);
CREATE TABLE posts (id INTEGER PRIMARY KEY, topic_id INTEGER, page_start INTEGER, position INTEGER,
  title TEXT, author_name TEXT, author_id INTEGER, date_raw TEXT, date TEXT, edit_info TEXT,
  edit_count INTEGER, last_edit_date TEXT, likes INTEGER, dislikes INTEGER, likes_text TEXT,
  liked_by TEXT, vote_title TEXT, content_html TEXT, content_text TEXT, signature_html TEXT, scraped_at TEXT);
CREATE TABLE groups (class TEXT PRIMARY KEY, id INTEGER, name TEXT, slug TEXT, color TEXT);
CREATE TABLE members (name TEXT PRIMARY KEY, user_id INTEGER, profile_url TEXT, avatar_url TEXT,
  group_class TEXT, color TEXT, rank_title TEXT, rank_image TEXT, joined_raw TEXT, joined_date TEXT,
  reputation INTEGER, post_count INTEGER, website TEXT, signature_html TEXT, extra_fields TEXT,
  profile_images TEXT, profile_post_id INTEGER, first_seen_post_id INTEGER, is_guest INTEGER DEFAULT 0,
  updated_at TEXT);
INSERT INTO forums VALUES (1,'Engines',NULL,NULL,NULL,NULL,NULL,NULL),(2,'Sub',NULL,NULL,1,NULL,NULL,NULL);
INSERT INTO groups VALUES ('group-14',14,'Administradores','administradores','#FF0000');
INSERT INTO topics (id,forum_id,title,author_name,replies,views,last_post_author,last_post_date,completed,posts_scraped)
  VALUES (10,1,'Motor de plataforma','Ana',1,50,'Bia','2020-01-02T10:00:00',1,2),
         (11,1,'Ainda não raspado','Bia',5,9,'Ana','2019-01-01T10:00:00',0,0);
INSERT INTO posts (id,topic_id,position,title,author_name,date,content_html,content_text) VALUES
  (100,10,0,'Motor','Ana','2020-01-01T10:00:00','<p onclick="x()">Olá <script>alert(1)</script><a href="https://gmbr.forumeiros.com/t11-ainda">link</a></p>','Olá link'),
  (101,10,1,'Re','Bia','2020-01-02T10:00:00','<b>resposta</b>','resposta');
INSERT INTO members (name,group_class,post_count,extra_fields) VALUES ('Ana','group-14',10,'{"Moedas":"5"}'),('Bia',NULL,3,'{}');
"""


class SanitizeTest(unittest.TestCase):
    def test_strips_dangerous(self):
        out = clean_html('<div onmouseover="x"><script>bad()</script><a href="javascript:x">a</a>'
                         '<img src="/i.png" onerror="x"><iframe src="//e"></iframe>ok</div>')
        self.assertNotIn("script", out)
        self.assertNotIn("onmouseover", out)
        self.assertNotIn("javascript", out)
        self.assertNotIn("iframe", out)
        self.assertIn('src="https://gmbr.forumeiros.com/i.png"', out)
        self.assertTrue(out.endswith("ok</div>"))

    def test_internal_links(self):
        self.assertIn('href="/t/123?start=30"',
                      clean_html('<a href="https://gmbr.forumeiros.com/t123p30-titulo">x</a>'))

    def test_style_filter(self):
        out = clean_html('<span style="color: red; background:url(x)">a</span>')
        self.assertIn('style="color: red"', out)


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        arch = os.path.join(cls.tmp.name, "gmbr.sqlite")
        with sqlite3.connect(arch) as c:
            c.executescript(SCHEMA)
        forum = Forum(arch, os.path.join(cls.tmp.name, "local.sqlite"))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(forum))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def post(self, path, data):
        req = urllib.request.Request(self.base + path, data=urlencode(data).encode(), method="POST")
        with urllib.request.urlopen(req) as r:  # segue o 303
            return r.status, r.url, r.read().decode()

    def test_pages(self):
        s, html = self.get("/")
        self.assertEqual(s, 200)
        self.assertIn("Engines", html)
        self.assertIn("Sub", html)
        s, html = self.get("/f/1")
        self.assertIn("Motor de plataforma", html)
        self.assertIn("não arquivado", html)
        s, html = self.get("/t/10")
        self.assertIn("Administradores", html)
        self.assertIn('href="/t/11"', html)
        self.assertNotIn("alert(1)", html)
        self.assertNotIn("onclick", html)
        s, html = self.get("/t/11")
        self.assertIn("ainda não foi totalmente arquivado", html)
        self.assertIn("Moedas", self.get("/membro/Ana")[1])
        self.assertIn("Bia", self.get("/membros")[1])
        self.assertIn("resposta", self.get("/busca?q=resposta")[1])
        self.assertEqual(self.get("/t/999")[0], 404)
        self.assertEqual(self.get("/membro/Ninguem")[0], 404)

    def test_new_topic_and_reply(self):
        s, url, html = self.post("/f/1/novo", {"nome": "Caio", "titulo": "<i>Oi</i>", "texto": "<b>x</b>"})
        self.assertTrue(url.endswith("/n/1") or "/n/" in url)
        self.assertIn("&lt;i&gt;Oi&lt;/i&gt;", html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        s, url, html = self.post("/t/10/responder", {"nome": "Caio", "texto": "nova resposta"})
        self.assertIn("nova resposta", html)
        self.assertIn("Motor de plataforma", self.get("/recentes")[1])
        self.assertIn("&lt;i&gt;Oi&lt;/i&gt;", self.get("/f/1")[1])


if __name__ == "__main__":
    unittest.main()
