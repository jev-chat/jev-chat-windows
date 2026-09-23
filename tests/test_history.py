import os
import sqlite3
import tempfile
import unittest

from core.history import HistoryStore


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "history.sqlite3")
        self.store = HistoryStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_load_and_minute_deduplication(self):
        messages = [("her", "小王", "今天腰疼"), ("me", "", "那你早点休息")]
        self.assertTrue(self.store.save_messages("qq::小王", messages, 7, 50))
        self.assertFalse(self.store.save_messages("qq::小王", messages[:1], 7, 50))
        loaded = self.store.load_messages("qq::小王", 40)
        self.assertEqual([(x["who"], x["text"], x["name"]) for x in loaded], [
            ("her", "今天腰疼", "小王"), ("me", "那你早点休息", "")
        ])

    def test_judgment_is_stored_separately(self):
        result = {"answers": {"physical_score": {"value": 4}, "tension": {"value": 4}}}
        self.assertTrue(self.store.save_judgment("wechat::小王", result))
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0], 1)
        finally:
            conn.close()

    def test_clear_removes_messages_and_judgments(self):
        self.store.save_messages("wechat::小王", [("her", "", "不舒服")], 7, 50)
        self.store.save_judgment("wechat::小王", {"answers": {"scene": {"choice": "comfort"}}})
        self.store.clear_all()
        self.assertEqual(self.store.load_messages("wechat::小王", 40), [])
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
