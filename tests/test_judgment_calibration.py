# -*- coding: utf-8 -*-
"""判断链路的无联网回归样例；运行：python -m unittest discover -s tests -v"""
import unittest

from core.engine import _needs_deepseek_rank
from core.questions import build_state, calibrate_answers, recent_state


def answer(score=1, intent="casual_chat"):
    return {
        "danger_level": {"type": "score", "score": score},
        "true_intent": {"type": "choice", "choice": intent, "confidence": 0.5},
        "secondary_intent": {"type": "choice", "choice": "unclear", "confidence": 0.5},
        "reply_scene": {"type": "choice", "choice": "intimacy", "confidence": 0.5},
    }


class JudgmentCalibrationTests(unittest.TestCase):
    def test_deepseek_rank_is_reserved_for_high_risk_states(self):
        self.assertFalse(_needs_deepseek_rank({"physical_score": {"score": 3}}))
        self.assertTrue(_needs_deepseek_rank({"physical_score": {"score": 4}}))

    def test_explicit_waist_pain_has_physical_floor(self):
        got = calibrate_answers(answer(), [("her", "今天腰疼")])
        self.assertGreaterEqual(got["physical_score"]["score"], 3)
        self.assertGreaterEqual(got["danger_level"]["score"], 3)
        self.assertEqual(got["true_intent"]["choice"], "state_update")

    def test_discomfort_is_not_casual_chat(self):
        got = calibrate_answers(answer(), [("her", "有点不舒服")])
        self.assertNotEqual(got["true_intent"]["choice"], "casual_chat")
        self.assertEqual(got["secondary_intent"]["choice"], "seek_care")

    def test_recent_repeated_state_is_inherited(self):
        messages = [
            ("her", "今天腰一直疼"),
            ("me", "那你先别硬撑"),
            ("her", "感觉不行，下班回去睡觉"),
        ]
        state = recent_state(messages)
        got = calibrate_answers(answer(), messages)
        self.assertTrue(state["physical_discomfort"])
        self.assertTrue(state["recently_repeated"])
        self.assertEqual(got["true_intent"]["choice"], "state_update")
        self.assertIn(got["reply_scene"]["choice"], {"intimacy", "comfort"})
        self.assertGreaterEqual(got["danger_level"]["score"], 4)

    def test_no_sleep_evidence_is_not_invented_by_state_layer(self):
        got = build_state([("her", "感觉不行，下班回去睡觉")], "romantic partners")
        self.assertFalse(got["chat"]["recent_state"]["physical_discomfort"])
        self.assertNotIn("睡眠", got["chat"]["recent_state"]["symptoms"])

    def test_recent_state_is_bounded(self):
        messages = [("her", "腰疼")] * 50
        state = recent_state(messages)
        self.assertEqual(state["messages_considered"], 40)


if __name__ == "__main__":
    unittest.main()
