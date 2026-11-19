"""Testes offline do parser com trechos de HTML no formato do Forumeiros."""
import unittest
from datetime import datetime

from gmbr_scraper import parser

TOPIC_HTML = """
<html><head><title>Retorno da GMBR!!!</title></head><body>
<a class="nav" href="/"><span>GMBR</span></a>
<a class="nav" href="/f1-central-do-forum"><span>Central do Fórum</span></a>
<div class="post row2 post--100 post-group-14" id="p100">
 <div class="inner"><div class="postbody">
  <h2 class="topic-title"><a href="/t5-retorno#100">Retorno da GMBR!!!</a></h2>
  <p class="author"><img alt="Mensagem"/> por <span class="group-14 usr_grp_clr" style="color:#FF0000"><strong>GMBR</strong></span> Ter 24 Jan 2017, 15:35</p>
  <div class="content clearfix"><div>Linha 1<br/>Linha 2</div></div>
  <br/>Última edição por GMBR em Sex 23 Jun 2017, 18:49, editado 2 vez(es)
  <div class="signature_div" id="sig100">assinatura</div>
 </div>
 <div class="postprofile" id="profile100"><dl>
  <dt><img alt="avatar" src="https://2img.net/a.png"/><br/>
   <strong><span class="group-14 usr_grp_clr" style="color:#FF0000"><strong>GMBR</strong></span></strong></dt>
  <dd></dd><dd class="dd_award right"></dd>
  <dd><span class="label"><span>Data de inscrição</span> :</span> 26/02/2008<br/>
   <span class="label"><span><b>Reputação</b></span> :</span> 6<br/>
   <span class="label"><span>Número de Mensagens</span> :</span> 1.352<hr/>
   <span class="label"><span><img alt="Medalhas" src="m.png"/></span></span> x 3
  </dd>
  <dd><a href="https://exemplo.com" title="Visitar a homepage do usuário">site</a></dd>
 </dl></div>
 <div class="fa_like_div"><p class="fa_like_list">moisesBR e Diemorth gostam desta mensagem</p></div>
 </div>
</div>
<div class="post row2 post--0" id="p0"><p>Conteúdo patrocinado</p></div>
<div class="post row1" id="p101"><div class="postbody">
  <p class="author"><img alt="Mensagem"/> por KinKing Hoje às 04:18</p>
  <div class="content clearfix"><div>oi</div></div></div>
 <div class="postprofile"><dl><dt><img src="https://2img.net/b.png"/><br/><strong>KinKing</strong></dt>
 <dd>Convidado<br/></dd></dl></div>
</div>
<a href="/t5p50-retorno">2</a><a href="/t5p100-retorno">3</a>
</body></html>
"""

FORUM_HTML = """
<html><body>
<a class="nav" href="/f12-engines"><span>Engines</span></a>
<a class="forumtitle" href="/f40-sub-forum">Sub Fórum</a>
<ul class="topiclist"><li class="header"><dl><dt>Tópicos</dt></dl></li></ul>
<ul class="topiclist topics"><li class="row row2"><dl class="icon">
 <dd class="dterm"><div><h2><a class="topictitle" href="/t33521-plataforma">Plataforma</a></h2></div>
  <br/>smooth plataform<br/><span class="span-tab">por <strong>TS</strong></span></dd>
 <dd class="posts">2 <dfn>Respostas</dfn></dd><dd class="views">3040 <dfn>Vistos</dfn></dd>
 <dd class="lastpost"><span class="lastpost-avatar"><img src="https://2img.net/av.jpg"/></span>
  <div><span>TS <dfn>Última mensagem</dfn><a href="/t33521-plataforma#241207"><img/></a><br/>Sex 16 maio 2025, 08:07</span></div></dd>
</dl></li></ul>
<a href="/f12p30-engines">2</a><a href="/f12p1320-engines">45</a>
<a style="color: #FF0000" class="gensmall usr_grp_clr" href="/g14-administradores">Administradores</a>
</body></html>
"""


class DateTests(unittest.TestCase):
    def test_absolute(self):
        self.assertEqual(parser.parse_date("Ter 24 Jan 2017, 15:35"), "2017-01-24T15:35:00")
        self.assertEqual(parser.parse_date("Sex 16 maio 2025, 08:07"), "2025-05-16T08:07:00")
        self.assertEqual(parser.parse_date("Sáb 13 Dez 2018, 17:21"), "2018-12-13T17:21:00")

    def test_relative(self):
        now = datetime(2026, 11, 19, 12, 0)
        self.assertEqual(parser.parse_date("Hoje às 04:18", now), "2026-11-19T04:18:00")
        self.assertEqual(parser.parse_date("Ontem às 23:10", now), "2026-11-18T23:10:00")

    def test_dmy(self):
        self.assertEqual(parser.parse_date("26/02/2008"), "2008-02-26")


class TopicTests(unittest.TestCase):
    def setUp(self):
        self.data = parser.parse_topic_page(TOPIC_HTML, 5, now=datetime(2026, 1, 2, 10, 0))

    def test_posts_skip_ads(self):
        self.assertEqual([p["id"] for p in self.data["posts"]], [100, 101])

    def test_author_and_date(self):
        p1, p2 = self.data["posts"]
        self.assertEqual(p1["author_name"], "GMBR")
        self.assertEqual(p1["date"], "2017-01-24T15:35:00")
        self.assertEqual(p2["author_name"], "KinKing")
        self.assertEqual(p2["date_raw"], "Hoje às 04:18")
        self.assertEqual(p2["date"], "2026-01-02T04:18:00")

    def test_post_metadata(self):
        p1 = self.data["posts"][0]
        self.assertEqual(p1["edit_count"], 2)
        self.assertEqual(p1["last_edit_date"], "2017-06-23T18:49:00")
        self.assertEqual(p1["liked_by"], ["moisesBR", "Diemorth"])
        self.assertEqual(p1["content_text"], "Linha 1\nLinha 2")
        self.assertEqual(p1["signature_html"], "assinatura")

    def test_profile(self):
        prof = self.data["posts"][0]["profile"]
        self.assertEqual(prof["avatar_url"], "https://2img.net/a.png")
        self.assertEqual(prof["group_class"], "group-14")
        self.assertEqual(prof["color"], "#FF0000")
        self.assertEqual(prof["joined_date"], "2008-02-26")
        self.assertEqual(prof["reputation"], 6)
        self.assertEqual(prof["post_count"], 1352)
        self.assertEqual(prof["extra_fields"]["Medalhas"], "3")
        self.assertEqual(prof["website"], "https://exemplo.com")
        self.assertEqual(self.data["posts"][1]["profile"]["rank_title"], "Convidado")

    def test_pagination_and_forum(self):
        self.assertEqual(self.data["page_step"], 50)
        self.assertEqual(self.data["last_offset"], 100)
        self.assertEqual(self.data["forum_id"], 1)


class ForumTests(unittest.TestCase):
    def test_forum_page(self):
        data = parser.parse_forum_page(FORUM_HTML, 12)
        t = data["topics"][0]
        self.assertEqual(t["id"], 33521)
        self.assertEqual(t["author_name"], "TS")
        self.assertEqual(t["description"], "smooth plataform")
        self.assertEqual((t["replies"], t["views"]), (2, 3040))
        self.assertEqual(t["last_post_author"], "TS")
        self.assertEqual(t["last_post_id"], 241207)
        self.assertEqual(t["last_post_date"], "2025-05-16T08:07:00")
        self.assertEqual(t["section"], "Tópicos")
        self.assertEqual((data["page_step"], data["last_offset"]), (30, 1320))
        self.assertEqual([f["id"] for f in data["subforums"]], [40])

    def test_groups(self):
        g = parser.parse_groups(FORUM_HTML)[0]
        self.assertEqual((g["class"], g["name"], g["color"]),
                         ("group-14", "Administradores", "#FF0000"))


if __name__ == "__main__":
    unittest.main()
