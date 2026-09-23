# -*- coding: utf-8 -*-
"""回归测试：候选回复被过滤后全空时，analyze() 应抛 JevError 而不是 IndexError。

不联网：draft_candidates / ask 都在边界上换成假的。
"""
import unittest
from unittest.mock import patch

from core import engine
from core.jev_client import JevError


class EmptyCandidatesTest(unittest.TestCase):
    def test_empty_candidates_raises_jev_error(self):
        """候选全被过滤（返回空列表）→ 抛 JevError，带人话原因，而不是 IndexError。"""
        with patch.object(engine, "draft_candidates", return_value=[]), \
             patch.object(engine, "ask", return_value={"answers": {}, "usage": {}}):
            with self.assertRaises(JevError) as ctx:
                engine.analyze([("her", "在吗")], "恋人")
        self.assertIn("候选", str(ctx.exception))

    def test_normal_path_untouched(self):
        """有候选时行为不变：三段式两问、推荐按概率。"""
        def fake_ask(state, questions, *, timeout, provider, model, base_url=None):
            if "literal_question" in questions:
                return {"answers": {}, "usage": {"input_tokens": 1}}
            return {"answers": {"best_reply": {"choice": "reply_b",
                                               "probabilities": {"reply_a": 0.2, "reply_b": 0.7,
                                                                 "reply_c": 0.1}}},
                    "usage": {"input_tokens": 2}}

        def fake_chat(*a, **k):
            return ["甲", "乙", "丙"]

        with patch.object(engine, "draft_candidates", side_effect=fake_chat), \
             patch.object(engine, "ask", side_effect=fake_ask):
            r = engine.analyze([("her", "在吗")], "恋人")
        self.assertEqual(r["best_reply"], "乙")
        self.assertAlmostEqual(r["scores"][1], 0.7)


class VisionTranscribeTest(unittest.TestCase):
    """视觉模式：截图先转写成消息列表，判断和起草都用转写文本（context 截取生效）。"""

    def test_vision_transcribe_feeds_state_and_draft(self):
        import core.draft as draft_mod
        import core.engine as engine_mod
        transcribed = [("her", "在吗", None), ("me", "在的", None), ("her", "明天上线", None)]
        states = []

        def fake_ask(state, questions, *, timeout, provider, model, base_url=None):
            states.append(state)
            return {"answers": {"best_reply": {"choice": "reply_a",
                                               "probabilities": {"reply_a": 1, "reply_b": 0,
                                                                 "reply_c": 0}},
                                "danger_level": {"score": 0}}, "usage": {}}

        with patch.object(draft_mod, "vision_transcribe",
                          side_effect=lambda img, rel, **k: list(transcribed)),              patch.object(engine_mod, "vision_transcribe",
                          side_effect=lambda img, rel, **k: list(transcribed)),              patch.object(draft_mod, "chat", side_effect=lambda *a, **k: '["甲","乙","丙"]'),              patch.object(engine_mod, "ask", side_effect=fake_ask):
            engine_mod.analyze([("her", "OCR碎片")], "恋人", vision_image=b"frame", context=10)
            self.assertEqual([m["text"] for m in states[0]["chat"]["messages"]],
                             ["在吗", "在的", "明天上线"])
            states.clear()  # 三段式每轮 ask 两次（判断+排序），轮与轮之间清掉
            engine_mod.analyze([("her", "OCR碎片")], "恋人", vision_image=b"frame", context=2)
            self.assertEqual([m["text"] for m in states[0]["chat"]["messages"]],
                             ["在的", "明天上线"])


if __name__ == "__main__":
    unittest.main()
