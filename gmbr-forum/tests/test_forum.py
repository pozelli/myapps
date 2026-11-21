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
from gmbr_forum.search import fts_query
from gmbr_forum.search_index import build, split_quotes

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
  (101,10,1,'Re','Bia','2020-01-02T10:00:00','<blockquote><div><cite>Ana escreveu:</cite>Olá colisão</div></blockquote><b>resposta</b> sobre plataforma','resposta');
UPDATE posts SET likes=1, liked_by='["Bia"]' WHERE id=100;
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

    def test_bad_url(self):
        self.assertEqual(clean_html('<a href="http://[bad">x</a>'), "<a>x</a>")

    def test_style_filter(self):
        out = clean_html('<span style="color: red; background:url(x)">a</span>')
        self.assertIn('style="color: red"', out)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        arch = os.path.join(self.tmp.name, "gmbr.sqlite")
        with sqlite3.connect(arch) as c:
            c.executescript(SCHEMA)
        local = os.path.join(self.tmp.name, "local.sqlite")
        index = os.path.join(self.tmp.name, "search.sqlite")
        Forum(arch, local)
        build(arch, local, index, log=lambda *_: None)
        forum = Forum(arch, local, index)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(forum))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

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
        html = self.get("/membros?q=an")[1]
        self.assertIn("/membro/Ana", html)
        self.assertNotIn("/membro/Bia", html)
        self.assertIn("resposta", self.get("/busca?q=resposta")[1])  # redireciona
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

    def test_search_messages(self):
        html = self.get("/busca/mensagens?q=resposta")[1]
        self.assertIn("1 mensagens encontradas", html)
        self.assertIn("<mark>resposta</mark>", html)
        # texto citado só entra se pedido
        # "colisão" só aparece no texto citado: fica de fora, a menos que seja pedido
        self.assertIn("0 mensagens", self.get("/busca/mensagens?q=colisao")[1])
        self.assertIn("0 mensagens", self.get("/busca/mensagens?q=colisao&campo=texto")[1])
        self.assertIn("1 mensagens", self.get("/busca/mensagens?q=colisao&campo=texto&incluir_citado=1")[1])
        self.assertIn("1 mensagens", self.get("/busca/mensagens?cita=ana")[1])
        self.assertIn("1 mensagens", self.get("/busca/mensagens?curtida_por=Bia")[1])
        self.assertIn("1 mensagens", self.get("/busca/mensagens?autor=bia&com_participante=ana")[1])
        self.assertIn("2 mensagens", self.get("/busca/mensagens?forum=1&data_de=2020-01-01&data_ate=2020-01-02")[1])
        self.assertIn("1 tópicos encontrados", self.get("/busca/mensagens?forum=1&agrupar=1&q=ola+OR+resposta")[1])
        self.assertIn("pelo menos um termo", self.get("/busca/mensagens?q=-x")[1])
        self.assertEqual(self.get('/busca/mensagens?q=%22%22%22(&ordem=x&p=abc')[0], 200)

    def test_search_topics_and_users(self):
        self.assertIn("1 tópicos", self.get("/busca/topicos?q=plataforma")[1])
        self.assertIn("1 tópicos", self.get("/busca/topicos?participantes=ana,bia")[1])
        self.assertIn("0 tópicos", self.get("/busca/topicos?participantes=ana&sem_participante=bia")[1])
        self.assertIn("1 tópicos", self.get("/busca/topicos?q=resposta&onde=qualquer")[1])
        self.assertIn("1 tópicos", self.get("/busca/topicos?resp_min=5")[1])
        html = self.get("/busca/usuarios?rel_user=ana&rel=citou")[1]
        self.assertIn("1 usuários", html)
        self.assertIn("/membro/Bia", html)
        self.assertIn("1 usuários", self.get("/busca/usuarios?q=an&nome_modo=comeca")[1])
        self.assertIn("2 usuários", self.get("/busca/usuarios?com_mensagens=1")[1])
        html = self.get("/membro/Ana")[1]
        self.assertIn("Atividade e correlações", html)
        self.assertIn("Quem mais o citou", html)

    def test_rankings(self):
        status, html = self.get("/rankings")
        self.assertEqual(status, 200)
        self.assertIn("Quem mais postou", html)
        self.assertIn("Dúvidas mais comuns", html)
        self.assertNotIn("Não foi possível calcular", html)
        self.assertIn("/membro/Ana", html)
        for q in ("de=2020&ate=2020", "de=2030&ate=2019", "juntar=nome", "de=abc"):
            status, html = self.get("/rankings?" + q)
            self.assertEqual(status, 200, q)
            self.assertNotIn("Não foi possível calcular", html, q)

    def test_new_posts_are_indexed(self):
        self.post("/f/2/novo", {"nome": "Zeca", "titulo": "Tópico de teste xyzzy", "texto": "palavraunica aqui"})
        self.assertIn("1 mensagens", self.get("/busca/mensagens?q=palavraunica")[1])
        self.assertIn("1 tópicos", self.get("/busca/topicos?q=xyzzy")[1])
        self.assertIn("/membro/Zeca", self.get("/busca/usuarios?q=zeca")[1])
        self.post("/t/10/responder", {"nome": "Zeca", "texto": "outraunica"})
        self.assertIn("1 tópicos", self.get("/busca/topicos?participantes=zeca,ana")[1])


class QueryTest(unittest.TestCase):
    def test_fts_query(self):
        self.assertEqual(fts_query("a b")[0], '("a" AND "b")')
        self.assertEqual(fts_query('"x y" -z')[0], '"x y" NOT "z"')
        self.assertEqual(fts_query("a OR b c")[0], '(("a" OR "b") AND "c")')
        self.assertEqual(fts_query("spr*")[0], '"spr"*')
        self.assertEqual(fts_query('a"b')[0], '"a""b"')
        self.assertIsNone(fts_query("-a")[0])

    def test_split_quotes(self):
        body, quoted, names = split_quotes(
            "<blockquote><div><cite>A escreveu:</cite>q1<blockquote><div><cite>B escreveu:</cite>q2"
            "</div></blockquote></div></blockquote>meu texto")
        self.assertEqual(body, "meu texto")
        self.assertIn("q1", quoted)
        self.assertEqual(names, ["B", "A"])


class NewSchemaTest(ServerTest):
    """Banco de versões novas do scraper: prefixo, enquetes e perfil completo."""

    def setUp(self):
        global SCHEMA
        old = SCHEMA
        SCHEMA = old + """
ALTER TABLE topics ADD COLUMN prefix TEXT; ALTER TABLE topics ADD COLUMN topic_type TEXT;
ALTER TABLE members ADD COLUMN humor TEXT; ALTER TABLE members ADD COLUMN last_visit_raw TEXT;
ALTER TABLE members ADD COLUMN profile_fields TEXT; ALTER TABLE members ADD COLUMN profile_groups TEXT;
CREATE TABLE polls (topic_id INTEGER PRIMARY KEY, question TEXT, total_votes INTEGER, closed INTEGER, scraped_at TEXT);
CREATE TABLE poll_options (topic_id INTEGER, position INTEGER, text TEXT, votes INTEGER, percent REAL,
  PRIMARY KEY (topic_id, position));
UPDATE topics SET prefix='[Votação]', topic_type='topic_read_locked' WHERE id=10;
UPDATE members SET last_visit_raw='Hoje', profile_groups='[{"id": 11, "name": "Lendas"}]',
  profile_fields='{"Quem sou eu": "Programadora", "Moedas": "5", "Games Ranking": {"text": null, "images": ["https://i.example/x.png"]}}'
  WHERE name='Ana';
INSERT INTO polls VALUES (10,'Qual engine?',4,1,NULL);
INSERT INTO poll_options VALUES (10,0,'GameMaker',3,75.0),(10,1,'Godot',1,25.0);
"""
        try:
            super().setUp()
        finally:
            SCHEMA = old

    def test_new_fields(self):
        html = self.get("/f/1")[1]
        self.assertIn("[Votação]", html)
        self.assertIn("trancado", html)
        html = self.get("/t/10")[1]
        self.assertIn("Enquete: Qual engine?", html)
        self.assertIn("[Votação]</span> Motor de plataforma", html)
        self.assertIn("75%", html)
        self.assertIn("encerrada", html)
        html = self.get("/membro/Ana")[1]
        self.assertIn("Programadora", html)
        self.assertIn("Lendas", html)
        self.assertIn("Último acesso", html)
        self.assertIn("https://i.example/x.png", html)
        self.assertEqual(html.count("Moedas"), 1)


SCHEMA_V5 = """
PRAGMA user_version = 5;
CREATE TABLE forums (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, url TEXT, parent_id INTEGER,
  topics_listed INTEGER, scraped_at TEXT, listing_next_start INTEGER);
CREATE TABLE topics (id INTEGER PRIMARY KEY, forum_id INTEGER, title TEXT, description TEXT, url TEXT,
  section TEXT, prefix TEXT, topic_type TEXT, replies_forum INTEGER, views_forum INTEGER,
  last_post_id_forum INTEGER, scraped_replies INTEGER, pages INTEGER, posts_scraped INTEGER,
  completed INTEGER, scraped_at TEXT);
CREATE TABLE posts (id INTEGER PRIMARY KEY, topic_id INTEGER, page_start INTEGER, position INTEGER,
  title TEXT, author_id INTEGER, author_name TEXT, author_id_inferred INTEGER, date_raw TEXT, date TEXT,
  edit_info TEXT, edit_count INTEGER, last_edit_date TEXT, dislikes INTEGER, vote_title TEXT,
  content_html TEXT, content_text TEXT, scraped_at TEXT, has_signature INTEGER);
CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, color TEXT);
CREATE TABLE members (user_id INTEGER PRIMARY KEY, name TEXT, profile_url TEXT, avatar_url TEXT,
  group_id INTEGER, color TEXT, rank_title TEXT, rank_image TEXT, joined_raw TEXT, joined_date TEXT,
  last_visit_raw TEXT, last_visit TEXT, reputation INTEGER, post_count_forum INTEGER, website TEXT,
  humor TEXT, age TEXT, about_me TEXT, specialty TEXT, platforms TEXT, projects TEXT, infractions TEXT,
  grades_received TEXT, nick_history TEXT, signature_html TEXT, sidebar_fields TEXT, sidebar_images TEXT,
  profile_fields TEXT, in_memberlist INTEGER, profile_status TEXT, profile_scraped_at TEXT, updated_at TEXT);
CREATE TABLE member_groups (user_id INTEGER, group_id INTEGER);
CREATE TABLE post_likes (post_id INTEGER, liker_name TEXT, user_id INTEGER);
CREATE TABLE polls (topic_id INTEGER PRIMARY KEY, question TEXT, closed INTEGER, scraped_at TEXT);
CREATE TABLE poll_options (topic_id INTEGER, position INTEGER, text TEXT, votes INTEGER,
  PRIMARY KEY (topic_id, position));
CREATE TABLE account_aliases (name TEXT, user_id INTEGER, source TEXT, evidence INTEGER);
CREATE TABLE persons (id INTEGER PRIMARY KEY, label TEXT, note TEXT);
CREATE TABLE person_names (name TEXT, person_id INTEGER, certainty TEXT, note TEXT);
CREATE TABLE post_author_guesses (post_id INTEGER, name TEXT, person_id INTEGER, user_id INTEGER,
  user_link TEXT, method TEXT, confidence TEXT, score REAL, evidence TEXT);
CREATE VIEW v_post_author_best AS SELECT post_id, name, name AS names, person_id, user_id, user_link,
  score, method AS methods, confidence, 1 AS candidates FROM post_author_guesses;
CREATE VIEW v_topic_stats AS
  SELECT t.id AS topic_id, t.forum_id, COUNT(p.id) AS post_count, COUNT(p.id) - 1 AS reply_count,
         COUNT(DISTINCT p.author_name) AS participants,
         (SELECT author_id FROM posts WHERE topic_id = t.id ORDER BY position LIMIT 1) AS author_id,
         (SELECT author_name FROM posts WHERE topic_id = t.id ORDER BY position LIMIT 1) AS author_name,
         MIN(p.date) AS first_post_date,
         (SELECT id FROM posts WHERE topic_id = t.id ORDER BY position DESC LIMIT 1) AS last_post_id,
         (SELECT author_name FROM posts WHERE topic_id = t.id ORDER BY position DESC LIMIT 1) AS last_author_name,
         MAX(p.date) AS last_post_date
  FROM topics t LEFT JOIN posts p ON p.topic_id = t.id GROUP BY t.id;
CREATE VIEW v_poll_options AS
  SELECT o.topic_id, o.position, o.text, o.votes,
         100.0 * o.votes / NULLIF((SELECT SUM(votes) FROM poll_options x WHERE x.topic_id = o.topic_id), 0) AS percent
  FROM poll_options o;

INSERT INTO forums VALUES (1,'Engines',NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO groups VALUES (11,'Lendas','lendas','#9E0387'),(8,'Membros',NULL,NULL);
INSERT INTO members (user_id,name,group_id,post_count_forum,reputation,signature_html,sidebar_fields,profile_fields,joined_date)
  VALUES (1,'Ana',11,10,5,'<i>assinatura da Ana</i>','{"Moedas": "7"}','{"Quem sou eu": "Dev"}','2010-01-01'),
         (2,'Bia',NULL,3,1,NULL,'{}','{}','2011-01-01');
INSERT INTO member_groups VALUES (1,11),(1,8);
INSERT INTO topics (id,forum_id,title,prefix,topic_type,replies_forum,views_forum,completed,posts_scraped)
  VALUES (10,1,'Motor de plataforma','[Votação]','topic_read_locked',2,50,1,3);
INSERT INTO posts (id,topic_id,position,title,author_id,author_name,date,content_html,content_text,has_signature) VALUES
  (100,10,0,'Motor',1,'Ana','2020-01-01T10:00:00','<p>Olá mundo</p>','Olá mundo',1),
  (101,10,1,'Re',2,'Bia','2020-01-02T10:00:00','<blockquote><div><cite>Ana escreveu:</cite>Olá</div></blockquote>boa ideia','boa ideia',0),
  (102,10,2,'Re',NULL,'Convidado','2020-01-03T10:00:00','<p>sou eu mesmo</p>','sou eu mesmo',0),
  (103,10,3,'Re',2,'Bia','2020-01-04T10:00:00','<blockquote><div><cite>AnaVelha escreveu:</cite>antigo</div></blockquote>lembro','lembro',0),
  (104,10,4,'Re',2,'Bia','2020-01-05T10:00:00','<blockquote><div><cite>Fantasma escreveu:</cite>boo</div></blockquote>vi','vi',0),
  (105,10,5,'Re',2,'Bia','2020-01-06T10:00:00','<blockquote><div><cite>Reusado escreveu:</cite>x</div></blockquote>ok','ok',0);
-- 'Reusado' foi nick da Ana e virou outra conta só em 2030: a citação de 2020 é da Ana
INSERT INTO members (user_id,name,joined_date,sidebar_fields,profile_fields) VALUES (3,'Reusado','2030-01-01','{}','{}');
INSERT INTO account_aliases VALUES ('Reusado',1,'citações',2);
INSERT INTO post_likes VALUES (100,'Bia',2);
INSERT INTO polls VALUES (10,'Qual engine?',0,NULL);
INSERT INTO poll_options VALUES (10,0,'GameMaker',3),(10,1,'Godot',1);
INSERT INTO account_aliases VALUES ('AnaVelha',1,'citações',4);
INSERT INTO persons VALUES (1,'Ana','informado pelo admin');
INSERT INTO person_names VALUES ('Ana',1,'informado',NULL),('Fantasma',1,'denúncia',NULL);
INSERT INTO post_author_guesses VALUES (102,'Fantasma',1,NULL,NULL,'citacao','alta',0.9,'{}');
"""


class SchemaV5Test(unittest.TestCase):
    """Banco no schema 5 do scraper (contagens em views, post_likes, member_groups...)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        arch = os.path.join(self.tmp.name, "gmbr.sqlite")
        with sqlite3.connect(arch) as c:
            c.executescript(SCHEMA_V5)
        local = os.path.join(self.tmp.name, "local.sqlite")
        index = os.path.join(self.tmp.name, "search.sqlite")
        Forum(arch, local)
        build(arch, local, index, log=lambda *_: None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Forum(arch, local, index)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    get = ServerTest.get
    post = ServerTest.post

    def test_pages(self):
        html = self.get("/")[1]
        self.assertIn("Engines", html)
        html = self.get("/f/1")[1]
        self.assertIn("Motor de plataforma", html)
        self.assertIn("[Votação]", html)
        self.assertIn("trancado", html)
        html = self.get("/t/10")[1]
        self.assertIn("Lendas", html)                    # grupo via group_id
        self.assertIn("assinatura da Ana", html)          # assinatura via has_signature
        self.assertIn("👍 1", html)                        # curtidas via post_likes
        self.assertIn("Enquete: Qual engine?", html)
        self.assertIn("75%", html)
        self.assertIn("provável:", html)                  # autor provável do convidado
        self.assertIn("/membro/Fantasma", html)
        html = self.get("/membro/Ana")[1]
        self.assertIn("Moedas", html)
        self.assertIn("AnaVelha", html)                   # nick antigo
        self.assertIn("Fantasma", html)                   # mesma pessoa
        self.assertIn("Quem mais o citou", html)
        html = self.get("/membro/Fantasma")[1]            # só existe pela identidade
        self.assertIn("com confiança alta", html)
        self.assertIn("Bia", self.get("/membros")[1])

    def test_join_names(self):
        # Ana = AnaVelha (nick antigo da conta) = Fantasma (mesma pessoa)
        self.assertIn("4 mensagens", self.get("/busca/mensagens?cita=ana")[1])
        self.assertIn("3 mensagens", self.get("/busca/mensagens?cita=ana&juntar=conta")[1])
        self.assertIn("1 mensagens", self.get("/busca/mensagens?cita=ana&juntar=nome")[1])
        self.assertIn("4 mensagens", self.get("/busca/mensagens?cita=Fantasma")[1])
        # a conta nova 'Reusado' não herda a citação antiga
        self.assertIn("0 mensagens", self.get("/busca/mensagens?cita=Reusado&juntar=conta")[1])
        self.assertIn("1 mensagens", self.get("/busca/mensagens?cita=Reusado&juntar=nome")[1])
        html = self.get("/busca/usuarios?rel_user=Fantasma&rel=citou")[1]
        self.assertIn("1 usuários", html)
        self.assertIn('<a href="/busca/mensagens?', html)
        self.assertIn(">4</a>", html)                     # Bia citou 4 vezes a pessoa
        html = self.get("/membro/Ana")[1]
        self.assertIn("4</b> recebidas", html)
        self.assertIn("Contando também", html)
        self.assertIn("AnaVelha", html)
        html = self.get("/membro/Ana?juntar=nome")[1]
        self.assertIn("1</b> recebidas", html)

    def test_rankings(self):
        html = self.get("/rankings")[1]
        self.assertNotIn("Não foi possível calcular", html)
        # citações de AnaVelha/Fantasma/Reusado somam para a pessoa da Ana
        cited = html.split('id="citados"')[1].split("</section>")[0]
        self.assertIn("/membro/Ana", cited)
        self.assertIn('<span class="rk-v">4</span>', cited)
        cited = self.get("/rankings?juntar=nome")[1].split('id="citados"')[1].split("</section>")[0]
        self.assertIn('<span class="rk-v">1</span>', cited)

    def test_search(self):
        self.assertIn("1 mensagens", self.get("/busca/mensagens?curtida_por=bia")[1])
        self.assertIn("1 mensagens", self.get("/busca/mensagens?cita=ana&juntar=nome")[1])
        self.assertIn("1 tópicos", self.get("/busca/topicos?enquete=sim&trancado=sim")[1])
        self.assertIn("1 usuários", self.get("/busca/usuarios?grupo=Lendas")[1])
        self.post("/t/10/responder", {"nome": "Caio", "texto": "nova resposta aqui"})
        self.assertIn("nova resposta aqui", self.get("/t/10")[1])
        self.assertIn("1 mensagens", self.get("/busca/mensagens?q=nova+resposta")[1])


if __name__ == "__main__":
    unittest.main()
