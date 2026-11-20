"""Testes da divisão dos tópicos pendentes em faixas de ID."""
import os
import tempfile
import unittest

from gmbr_scraper.storage import Storage


class RangeTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.db = Storage(self.path)
        # 100 tópicos de 1 página e um tópico grande (500 respostas = 11 páginas)
        for tid in range(1, 101):
            self.db.upsert_topic_listing({"id": tid, "replies": 0})
        self.db.upsert_topic_listing({"id": 200, "replies": 500})
        # tópicos já concluídos não contam
        for tid in range(1, 21):
            self.db.mark_page(tid, 0, 1)
            self.db.finish_topic(tid, None, None, 1)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suf):
                os.remove(self.path + suf)

    def pending_ids(self, rng):
        return {r["id"] for r in self.db.topics_to_scrape(id_range=rng)}

    def test_ranges_partition_pending_topics(self):
        ranges = self.db.plan_id_ranges(4)
        self.assertEqual(len(ranges), 4)
        self.assertIsNone(ranges[0][0])
        self.assertIsNone(ranges[-1][1])
        for (_, hi), (lo, _) in zip(ranges, ranges[1:]):
            self.assertEqual(lo, hi + 1)  # contíguas, sem sobreposição
        parts = [self.pending_ids(r) for r in ranges]
        everything = set().union(*parts)
        self.assertEqual(everything, set(range(21, 101)) | {200})
        self.assertEqual(sum(len(p) for p in parts), len(everything))

    def test_ranges_balance_pages(self):
        # 80 páginas pequenas + 11 do tópico grande = 91; 4 faixas de ~23
        ranges = self.db.plan_id_ranges(4)
        weights = []
        for r in ranges:
            ids = self.pending_ids(r)
            weights.append(sum(11 if i == 200 else 1 for i in ids))
        self.assertLessEqual(max(weights) - min(weights), 12)

    def test_open_ended_range_filters(self):
        self.assertEqual(self.pending_ids((None, 25)), set(range(21, 26)))
        self.assertEqual(self.pending_ids((100, None)), {100, 200})


if __name__ == "__main__":
    unittest.main()
