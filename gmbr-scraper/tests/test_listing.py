"""Testes da listagem incremental com um fórum simulado (sem rede)."""
import os
import re
import tempfile
import unittest

from gmbr_scraper.crawler import Crawler
from gmbr_scraper.storage import Storage

STEP = 30


def forum_html(topics, start):
    """Página de listagem no formato do Forumeiros com topics[start:start+30].

    topics: lista de (id, last_post_id) já ordenada pela última mensagem.
    """
    rows = []
    for tid, lp in topics[start:start + STEP]:
        rows.append(
            f'<li class="row"><dl><dd class="dterm"><a class="topictitle" '
            f'href="/t{tid}-x">T{tid}</a><span class="span-tab">por <strong>A</strong>'
            f'</span></dd><dd class="posts">1</dd><dd class="views">1</dd>'
            f'<dd class="lastpost"><div><span>A <dfn>Última mensagem</dfn>'
            f'<a href="/t{tid}-x#{lp}">v</a><br/>Ter 24 Jan 2017, 15:35</span>'
            f'</div></dd></dl></li>')
    last = ((len(topics) - 1) // STEP) * STEP
    pag = f'<a href="/f7p{STEP}-x">2</a><a href="/f7p{last}-x">n</a>' if last else ""
    return f'<html><body><ul class="topiclist">{"".join(rows)}</ul>{pag}</body></html>'


class FakeFetcher:
    def __init__(self, topics):
        self.topics = topics
        self.starts = []

    def get(self, path, check_session=True):
        m = re.match(r"^/f7(?:p(\d+))?-", path)
        start = int(m.group(1) or 0)
        self.starts.append(start)
        return forum_html(self.topics, start)


class ListingTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.db = Storage(self.path)
        self.db.upsert_forum({"id": 7, "name": "F", "slug": "x", "url": "/f7-x"})
        # 200 tópicos, 7 páginas; ordem = última mensagem mais recente primeiro
        self.topics = [(1000 - i, 5000 - i) for i in range(200)]

    def tearDown(self):
        self.db.close()
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suf):
                os.remove(self.path + suf)

    def crawl(self, topics, **kw):
        f = FakeFetcher(topics)
        Crawler(self.db, f).list_forum_topics(7, **kw)
        return f.starts

    def test_first_listing_is_full(self):
        self.assertEqual(self.crawl(self.topics), [0, 30, 60, 90, 120, 150, 180])
        self.assertIsNotNone(self.db.get_forum(7)["scraped_at"])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 200)

    def test_unchanged_forum_reads_one_page(self):
        self.crawl(self.topics)
        self.assertEqual(self.crawl(self.topics), [0])

    def test_new_reply_and_new_topic(self):
        self.crawl(self.topics)
        # tópico 950 recebeu resposta e foi para o topo; tópico 2000 é novo
        moved = [t for t in self.topics if t[0] != 950]
        updated = [(2000, 9001), (950, 9000)] + moved
        self.assertEqual(self.crawl(updated), [0, 30])
        row = self.db.get_topic(950)
        self.assertEqual(row["last_post_id"], 9000)
        self.assertIsNotNone(self.db.get_topic(2000))

    def test_changes_spanning_pages_keep_going(self):
        self.crawl(self.topics)
        bumped = [(1000 - i, 9000 - i) for i in range(45)] + self.topics[45:]
        self.assertEqual(self.crawl(bumped), [0, 30, 60])

    def test_interrupted_listing_resumes(self):
        self.crawl(self.topics, max_pages=3)  # parou depois de start=60
        forum = self.db.get_forum(7)
        self.assertIsNone(forum["scraped_at"])
        self.assertEqual(forum["listing_next_start"], 90)
        # Retomada: página 0 sem mudanças → pula para 60 (uma de folga) e segue
        self.assertEqual(self.crawl(self.topics), [0, 60, 90, 120, 150, 180])
        self.assertIsNotNone(self.db.get_forum(7)["scraped_at"])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 200)

    def test_full_listing_flag(self):
        self.crawl(self.topics)
        self.assertEqual(len(self.crawl(self.topics, full=True)), 7)


if __name__ == "__main__":
    unittest.main()
