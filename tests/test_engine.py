import unittest
from unittest.mock import patch

from core import engine


class AnalyzeTests(unittest.TestCase):
    def test_empty_candidates_raise_a_user_facing_error(self):
        with patch.object(engine, "ask", return_value={"answers": {}, "usage": {}}), \
             patch.object(engine, "draft_candidates", return_value=[]):
            with self.assertRaises(engine.JevError) as raised:
                engine.analyze([("her", "hello")], "friends")

        self.assertEqual(str(raised.exception), "起草结果没有可用候选回复")


if __name__ == "__main__":
    unittest.main()
