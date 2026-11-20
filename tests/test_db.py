"""SQLite 流水的离线测试。"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store.db import Database  # noqa: E402


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "events.db")
        self.db.init_sync()

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self, **kwargs):
        base = {"ts": time.time(), "kind": "judge", "umo": "umo1", "gid": "g1",
                "sender_id": "u1", "sender_name": "甲", "reason": "directed",
                "act": "directed", "confidence": 85, "wait_seconds": None,
                "followup": "", "ms": 120, "model": "m1", "result": "passed",
                "detail": "测试"}
        base.update(kwargs)
        return base

    def test_insert_and_query(self):
        inserted = self.db.insert_sync([self._row(), self._row(kind="silence",
                                                               reason="goodbye")])
        self.assertEqual(inserted, 2)
        rows, total = self.db.query_sync()
        self.assertEqual(total, 2)
        self.assertEqual(len(rows), 2)
        self.assertIn(rows[0]["kind"], {"judge", "silence"})

    def test_filter_and_keyword(self):
        self.db.insert_sync([self._row(detail="无关消息"),
                             self._row(kind="silence", detail="再见")])
        rows, total = self.db.query_sync(filters={"kind": "silence"})
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["kind"], "silence")
        rows, total = self.db.query_sync(keyword="再见")
        self.assertEqual(total, 1)

    def test_pagination(self):
        self.db.insert_sync([self._row(ts=time.time() - i) for i in range(120)])
        rows, total = self.db.query_sync(page=2, size=50)
        self.assertEqual(total, 120)
        self.assertEqual(len(rows), 50)

    def test_stats(self):
        self.db.insert_sync([
            self._row(act="directed", confidence=90, ms=100),
            self._row(act="not_for_me", confidence=60, ms=200, reason="not_for_me"),
            self._row(kind="silence", result="discarded", act="", confidence=None,
                      ms=None),
        ])
        stats = self.db.stats_sync(time.time() - 60)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["by_act"]["directed"], 1)
        self.assertEqual(stats["by_result"]["discarded"], 1)
        self.assertEqual(stats["avg_ms"], 150.0)

    def test_detail(self):
        self.db.insert_sync([self._row()])
        rows, _ = self.db.query_sync()
        row = self.db.detail_sync(rows[0]["id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["reason"], "directed")

    def test_clear(self):
        self.db.insert_sync([self._row(), self._row()])
        removed = self.db.clear_sync()
        self.assertEqual(removed, 2)
        self.assertEqual(self.db.row_count_sync(), 0)

    def test_prune_by_age(self):
        old = time.time() - 40 * 86400
        self.db.insert_sync([self._row(ts=old), self._row()])
        removed = self.db.prune_sync(days=14, max_rows=0)
        self.assertEqual(removed, 1)
        self.assertEqual(self.db.row_count_sync(), 1)

    def test_prune_by_rows(self):
        self.db.insert_sync([self._row(ts=time.time() - i) for i in range(10)])
        removed = self.db.prune_sync(days=365, max_rows=4)
        self.assertEqual(removed, 6)
        self.assertEqual(self.db.row_count_sync(), 4)

    def test_update_status_fills_judge_fields(self):
        """判定行：一行到底，结算时把原因/置信度/原始输出一并补上。"""
        self.db.insert_sync([self._row(result="judging", reason="", confidence=None)])
        rows, _total = self.db.query_sync()
        row_id = rows[0]["id"]
        ok = self.db.update_status_sync(
            row_id, "interrupted", detail="已停止 1 个在途执行",
            reason="requirement_change", confidence=88, raw_output="{\"score\":88}")
        self.assertTrue(ok)
        again, _ = self.db.query_sync()
        row = again[0]
        self.assertEqual(row["result"], "interrupted")
        self.assertEqual(row["reason"], "requirement_change")
        self.assertEqual(row["confidence"], 88)
        self.assertEqual(row["detail"], "已停止 1 个在途执行")
        self.assertIn("score", row["raw_output"])

    def test_async_wrappers(self):
        async def run():
            await self.db.insert([self._row()])
            rows, total = await self.db.query()
            stats = await self.db.stats(1)
            detail = await self.db.detail(rows[0]["id"]) if rows else None
            return total, stats["total"], detail

        total, stats_total, detail = asyncio.run(run())
        self.assertEqual(total, 1)
        self.assertEqual(stats_total, 1)
        self.assertIsNotNone(detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)