"""Enquetes, lista de membros e perfis."""
from gmbr_scraper import parser
from gmbr_scraper.storage import Storage

POLL = """<div class="content"><h2 class="left-box">Qual logo você prefere?</h2>
<fieldset class="polls">
<dl><dt><label>Logo 1</label></dt><dd style="width:35%;"><img alt="15%"/> 15% </dd><dd>[ 13 ]</dd></dl>
<dl><dt><label>Logo 2</label></dt><dd style="width:35%;"><img alt="85%"/> 85% </dd><dd>[ 73 ]</dd></dl>
<dl><dt> </dt><dd><strong>Total de votos : 86</strong></dd></dl>
<d1><dt> </dt><dd><b>Votação encerrada</b></dd></d1>
</fieldset></div>"""

MEMBERLIST = """<table><tr><th>#</th></tr>
<tr class="row2"><td> 2 </td><td class="avatar-mini"><a href="/u14611"><img alt="Yak"
 src="https://2img.net/a.png"/> Yakchimi</a></td><td></td><td>10/10/2026</td>
<td>Qua 18 Nov 2026, 02:24</td><td>7</td><td></td><td> <a href="https://site.example">w</a></td></tr>
</table><a href="/memberlist?mode=joined&amp;order=ASC&amp;start=17370&amp;username">fim</a>"""

PROFILE = """<div id="main-content"><h1 class="page-title">Perfil de <span style="color:#FF0000">
<strong>GMBR</strong></span></h1>
<dl class="left-box details"><dt>Avatar:</dt><dd><img src="https://2img.net/av.png"/></dd></dl>
<dl class="left-box details"><dt>Rank:</dt><dd><strong>Fundador</strong></dd>
<dt>Estado:</dt><dd><strong>Desconectado</strong></dd></dl>
<dl class="left-box details" id="groups-details"><dt>Grupos:</dt><dd><strong>
<div><a href="/g14-administradores">Administradores</a></div></strong></dd></dl>
<dl id="field_id-4" class="left-box details"><dt><span>Data de inscrição</span> : </dt>
<dd><div class="field_uneditable">26/02/2008</div></dd></dl>
<dl id="field_id-6" class="left-box details"><dt><span>Número de Mensagens</span> : </dt>
<dd><div class="field_uneditable">352</div> [0.16% do total]</dd></dl>
<dl id="field_id1" class="left-box details"><dt><span>Humor</span> : </dt>
<dd><div class="field_uneditable"> &nbsp;-</div><div class="field_editable invisible">
<input value="segredo"/></div></dd></dl>
<dl class="left-box details"><dt>Mensagem privada:</dt><dd><a href="/privmsg?u=1">MP</a></dd></dl>
<dl class="left-box details"><dt>Mensagens privadas:&nbsp;</dt><dd>71</dd></dl>
<dl class="left-box details"><dt>Última visita:&nbsp;</dt><dd>Sex 11 Abr 2025, 17:14</dd></dl>
</div>"""


def test_parse_poll():
    poll = parser.parse_poll(parser.soup_of(POLL))
    assert poll["question"] == "Qual logo você prefere?"
    assert [(o["text"], o["votes"], o["percent"]) for o in poll["options"]] == [
        ("Logo 1", 13, 15.0), ("Logo 2", 73, 85.0)]
    assert poll["total_votes"] == 86 and poll["closed"] is True


def test_parse_memberlist():
    data = parser.parse_memberlist_page(MEMBERLIST)
    assert data["last_offset"] == 17370
    m = data["members"][0]
    assert (m["user_id"], m["name"], m["post_count"]) == (14611, "Yakchimi", 7)
    assert m["joined_date"] == "2026-10-10" and m["last_visit"] == "2026-11-18T02:24:00"
    assert m["website"] == "https://site.example"


def test_parse_profile():
    p = parser.parse_profile_page(PROFILE, 1)
    assert p["name"] == "GMBR" and p["color"] == "#FF0000"
    assert p["avatar_url"] == "https://2img.net/av.png"
    assert p["groups"] == [{"id": 14, "name": "Administradores"}]
    f = p["fields"]
    assert f["Rank"] == "Fundador" and f["Data de inscrição"] == "26/02/2008"
    assert f["Número de Mensagens"] == {"text": "352", "extra": "[0.16% do total]"}
    assert f["Humor"] is None  # não lê o valor do formulário de edição
    assert "Mensagem privada" not in f and "Mensagens privadas" not in f
    assert p["last_visit"] == "2025-04-11T17:14:00"


def test_memberlist_and_profile_storage(tmp_path):
    db = Storage(str(tmp_path / "t.sqlite"))
    db.upsert_member({"name": "Yakchimi", "user_id": 14611, "post_count": 5}, 1, None)
    for m in parser.parse_memberlist_page(MEMBERLIST)["members"]:
        db.upsert_memberlist_row(m)
    db.upsert_memberlist_row({"user_id": 99, "name": "Novo", "post_count": 0})
    assert db.conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 2
    assert db.profiles_pending() == [14611, 99]  # mais mensagens primeiro
    db.save_profile(14611, parser.parse_profile_page(PROFILE, 14611))
    assert db.profiles_pending() == [99]
    db.upsert_poll(5, parser.parse_poll(parser.soup_of(POLL)))
    assert db.conn.execute("SELECT COUNT(*) FROM poll_options").fetchone()[0] == 2


def test_throttle_shared_and_decays(tmp_path, monkeypatch):
    from gmbr_scraper import crawler
    path = str(tmp_path / "throttle.json")
    a, b = crawler.Throttle(path), crawler.Throttle(path)
    assert a.factor() == 1.0
    a.hit("HTTP 429")
    a.hit("HTTP 429")
    assert b.factor() == 4.0  # o outro processo enxerga o freio
    t0 = crawler.time.time()
    monkeypatch.setattr(crawler.time, "time", lambda: t0 + 2 * crawler.Throttle.DECAY + 1)
    assert b.factor() == 1.0  # caiu pela metade duas vezes
    for _ in range(10):
        a.hit("HTTP 429")
    assert a.factor() == crawler.Throttle.MAX_FACTOR


def test_unclosed_iframe_does_not_swallow_following_posts():
    post = ('<div id="p{0}" class="post row1"><div class="postbody"><div class="content">'
            '{1}</div></div></div>')
    html = ("<html><body>"
            + post.format(1, '<object><iframe src="https://www.youtube.com/embed/x"></embed></object>')
            + post.format(2, "segundo") + post.format(3, "terceiro") + "</body></html>")
    soup = parser.soup_of(html)
    assert [d["id"] for d in soup.select("div.post[id^=p]")] == ["p1", "p2", "p3"]
