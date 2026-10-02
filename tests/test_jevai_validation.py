# -*- coding: utf-8 -*-
"""Deterministic, offline tests for the fail-closed JevAI Community validation
contract added when closing out PR #38 review feedback.

These tests never touch the network and never import typesafe_sdk; the HTTP
boundary is mocked. Run either way from the repo root:

    python -m unittest tests.test_jevai_validation -v
    python tests/test_jevai_validation.py

Coverage:
  * valid noul / choice / score / full Jev response
  * the reviewer's required failures: missing answers, extra answers,
    out-of-set choice, non-finite probabilities
  * every other malformed case in the validation contract (wrong type, missing
    or extra probability keys, out-of-range / non-finite probabilities and
    confidence and noul, probability sum far from 1, score outside domain)
  * an engine-level test proving a code==0 envelope with malformed answers
    raises JevError, is NOT treated as a successful judgment, and triggers the
    existing blind-draft + combined-question fallback.
"""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

# Make `import core...` work both under `python -m unittest` and direct execution.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import urllib.request  # noqa: E402

import core.engine as engine  # noqa: E402
from core.jev_client import JevError, _validate_answers, ask  # noqa: E402
from core.questions import JUDGE_QUESTIONS, build_rank_question  # noqa: E402

# A fixed offline key so ask()'s key lookup succeeds; never a real credential.
os.environ["JEV_API_KEY"] = "offline-test-key"

NOUL_QUESTION = {
    "literal_question": {
        "type": "noul",
        "instructions": "Is the message purely literal?",
        "criteria": {"true": "no subtext", "false": "has subtext"},
    }
}
CHOICE_QUESTION = {
    "best_reply": {
        "type": "choice",
        "instructions": "Which candidate reply fits best?",
        "criteria": {"reply_a": "candidate A", "reply_b": "candidate B",
                     "reply_c": "candidate C"},
    }
}
SCORE_QUESTION = {
    "danger_level": {
        "type": "score",
        "instructions": "How close to a fight?",
        "criteria": [f"level {i}" for i in range(10)],  # levels 0..9
    }
}
ALL_QUESTIONS = {**NOUL_QUESTION, **CHOICE_QUESTION, **SCORE_QUESTION}


def make_valid_answers(questions: dict) -> dict:
    """Build a schema-valid, peaked answer set for any questions dict.

    Peaked distributions put all mass on one option/level so they sum to 1
    exactly and stay easy to reason about.
    """
    answers = {}
    for qid, question in questions.items():
        qtype = question["type"]
        if qtype == "noul":
            answers[qid] = {"type": "noul", "noul": 0.8}
        elif qtype == "choice":
            keys = list(question["criteria"])
            answers[qid] = {
                "type": "choice",
                "choice": keys[0],
                "confidence": 1.0,
                "probabilities": {k: (1.0 if k == keys[0] else 0.0) for k in keys},
            }
        else:  # score
            levels = len(question["criteria"])
            peak = min(4, levels - 1)
            answers[qid] = {
                "type": "score",
                "score": float(peak),
                "confidence": 1.0,
                "probabilities": {str(i): (1.0 if i == peak else 0.0)
                                  for i in range(levels)},
            }
    return answers


# Questions the app really sends: the 7 fixed judge questions plus the rank one.
FULL_QUESTIONS = dict(JUDGE_QUESTIONS)
FULL_QUESTIONS.update(build_rank_question(["甲", "乙", "丙"]))


def three_key_distribution(kind: str, probs3):
    """Wrap the same 3-value distribution as either a choice or 3-level score.

    Lets one set of numbers exercise both distribution validators (choice uses
    the criteria keys, score uses the string level keys "0".."2").
    """
    if kind == "choice":
        question = CHOICE_QUESTION  # keys: reply_a / reply_b / reply_c
        keys = list(question["best_reply"]["criteria"])
        answer = {
            "type": "choice",
            "choice": keys[max(range(3), key=lambda i: probs3[i])],
            "confidence": 0.9,
            "probabilities": dict(zip(keys, probs3)),
        }
        return question, {"best_reply": answer}
    # score: a 3-level rubric, probabilities keyed by string level number
    question = {
        "danger3": {"type": "score", "instructions": "three levels",
                    "criteria": ["level 0", "level 1", "level 2"]},
    }
    answer = {
        "type": "score", "score": 1.0, "confidence": 0.9,
        "probabilities": {str(i): float(probs3[i]) for i in range(3)},
    }
    return question, {"danger3": answer}


class ValidAnswerTests(unittest.TestCase):
    def test_valid_noul(self):
        out = _validate_answers(
            NOUL_QUESTION, {"literal_question": {"type": "noul", "noul": 0.9}})
        self.assertEqual(out, {"literal_question": {"type": "noul", "noul": 0.9}})
        # Boundaries and integer 0/1 are legitimate finite values in [0, 1].
        for value in (0.0, 1.0, 0, 1, 0.5):
            _validate_answers(
                NOUL_QUESTION, {"literal_question": {"type": "noul", "noul": value}})

    def test_valid_choice(self):
        answer = {
            "type": "choice", "choice": "reply_b", "confidence": 0.8,
            "probabilities": {"reply_a": 0.1, "reply_b": 0.8, "reply_c": 0.1},
        }
        out = _validate_answers(CHOICE_QUESTION, {"best_reply": answer})
        self.assertEqual(out["best_reply"]["choice"], "reply_b")
        self.assertAlmostEqual(sum(out["best_reply"]["probabilities"].values()), 1.0)
        self.assertEqual(set(out["best_reply"]["probabilities"]),
                         {"reply_a", "reply_b", "reply_c"})

    def test_valid_score(self):
        answer = {
            "type": "score", "score": 4.0, "confidence": 0.6,
            "probabilities": {str(i): (0.6 if i == 4 else (0.4 if i == 5 else 0.0))
                              for i in range(10)},
        }
        out = _validate_answers(SCORE_QUESTION, {"danger_level": answer})
        self.assertEqual(set(out["danger_level"]["probabilities"]),
                         {str(i) for i in range(10)})
        # A fractional score is a valid position between two levels.
        answer["score"] = 4.5
        _validate_answers(SCORE_QUESTION, {"danger_level": copy.deepcopy(answer)})

    def test_probability_sum_tolerance(self):
        # Each value stays in [0, 1]; only the total carries tiny floating slop,
        # which is accepted (it is not "far from 1").
        base = {"type": "choice", "choice": "reply_a", "confidence": 1.0}
        _validate_answers(CHOICE_QUESTION, {"best_reply": {
            **base, "probabilities": {"reply_a": 0.99999995, "reply_b": 0.00000005,
                                      "reply_c": 0.00000005}}})  # sum ~1.00000005
        _validate_answers(CHOICE_QUESTION, {"best_reply": {
            **base, "probabilities": {"reply_a": 0.99999995, "reply_b": 0.0,
                                      "reply_c": 0.0}}})  # sum 0.99999995

    def test_approx_one_sum_accepted_choice_and_score(self):
        # Contract says the probabilities "sum to approximately 1": three
        # 0.3333 bins total 0.9999, which must PASS for both choice and score.
        probs = [0.3333, 0.3333, 0.3333]
        self.assertAlmostEqual(sum(probs), 0.9999, places=7)
        for kind in ("choice", "score"):
            question, answers = three_key_distribution(kind, probs)
            _validate_answers(question, answers)  # must not raise

    def test_clearly_wrong_sum_rejected_choice_and_score(self):
        # A clearly wrong total (0.98) must FAIL / raise JevError for both.
        probs = [0.34, 0.32, 0.32]
        self.assertAlmostEqual(sum(probs), 0.98, places=7)
        for kind in ("choice", "score"):
            question, answers = three_key_distribution(kind, probs)
            with self.assertRaises(JevError, msg=f"{kind} distribution sums to 0.98"):
                _validate_answers(question, answers)

    def test_valid_full_jev_response_through_adapter(self):
        envelope = {
            "code": 0, "message": "ok",
            "data": {"answers": make_valid_answers(FULL_QUESTIONS),
                     "usage": {"input_tokens": 21, "output_tokens": 9}},
        }

        def fake_urlopen(req, timeout=None):
            self.assertEqual(req.full_url, "https://www.jevai.org/api/v1/decisions")
            self.assertEqual(req.headers["Authorization"], "Bearer offline-test-key")
            return io.BytesIO(json.dumps(envelope).encode("utf-8"))

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            result = ask({"chat": {}}, FULL_QUESTIONS,
                         provider="jevai", model="typesafe-ai/jev")

        self.assertEqual(set(result["answers"]), set(FULL_QUESTIONS))
        self.assertEqual(result["usage"], {"input_tokens": 21, "output_tokens": 9})
        # Every answer is the canonical provider-independent shape.
        self.assertEqual(result["answers"]["literal_question"],
                         {"type": "noul", "noul": 0.8})
        self.assertEqual(set(result["answers"]["danger_level"]["probabilities"]),
                         {str(i) for i in range(10)})


class InvalidAnswerTests(unittest.TestCase):
    """Each malformed payload must raise JevError (fail closed), never coerce."""

    def assertInvalid(self, answers, label):  # noqa: N802 - unittest assertion style
        with self.assertRaises(JevError, msg=label):
            _validate_answers(ALL_QUESTIONS, answers)

    def fresh(self):
        return copy.deepcopy(make_valid_answers(ALL_QUESTIONS))

    # 1. missing answer -------------------------------------------------------
    def test_01_missing_answer(self):
        answers = self.fresh()
        del answers["best_reply"]
        self.assertInvalid(answers, "missing answer id")

    # 2. extra unknown answer -------------------------------------------------
    def test_02_extra_answer(self):
        answers = self.fresh()
        answers["not_requested"] = {"type": "noul", "noul": 0.5}
        self.assertInvalid(answers, "extra unknown answer id")

    # 3. wrong answer type ----------------------------------------------------
    def test_03_wrong_answer_type(self):
        answers = self.fresh()
        answers["best_reply"] = {"type": "noul", "noul": 0.5}
        self.assertInvalid(answers, "choice question answered as noul")

    # 4. choice outside criteria ---------------------------------------------
    def test_04_choice_outside_criteria(self):
        answers = self.fresh()
        answers["best_reply"]["choice"] = "reply_z"
        self.assertInvalid(answers, "choice not in criteria keys")
        answers = self.fresh()
        answers["best_reply"]["choice"] = None
        self.assertInvalid(answers, "missing choice")

    # 5. missing probability key ---------------------------------------------
    def test_05_missing_probability_key(self):
        answers = self.fresh()
        del answers["best_reply"]["probabilities"]["reply_c"]
        self.assertInvalid(answers, "missing choice probability key")

    # 6. extra probability key ------------------------------------------------
    def test_06_extra_probability_key(self):
        answers = self.fresh()
        answers["best_reply"]["probabilities"]["reply_d"] = 0.0
        self.assertInvalid(answers, "unknown choice probability key")

    # 7. NaN probability ------------------------------------------------------
    def test_07_nan_probability(self):
        answers = self.fresh()
        answers["best_reply"]["probabilities"]["reply_b"] = float("nan")
        self.assertInvalid(answers, "NaN probability")

    # 8. Infinity / -Infinity probability ------------------------------------
    def test_08_infinite_probability(self):
        for bad in (float("inf"), float("-inf")):
            answers = self.fresh()
            answers["best_reply"]["probabilities"]["reply_b"] = bad
            self.assertInvalid(answers, f"infinite probability {bad}")

    # 9. negative probability -------------------------------------------------
    def test_09_negative_probability(self):
        answers = self.fresh()
        answers["best_reply"]["probabilities"]["reply_b"] = -0.1
        self.assertInvalid(answers, "negative probability")

    # 10. probability > 1 -----------------------------------------------------
    def test_10_probability_above_one(self):
        answers = self.fresh()
        answers["best_reply"]["probabilities"]["reply_b"] = 1.5
        self.assertInvalid(answers, "probability above 1")

    # 11. probability sum far from 1 -----------------------------------------
    def test_11_probability_sum_far_from_one(self):
        answers = self.fresh()
        answers["best_reply"]["probabilities"] = {
            "reply_a": 0.1, "reply_b": 0.1, "reply_c": 0.1}  # sums to 0.3
        self.assertInvalid(answers, "probability sum 0.3")

    # 12. invalid confidence --------------------------------------------------
    def test_12_invalid_confidence(self):
        for bad in (float("nan"), float("inf"), float("-inf"), -0.1, 1.01,
                    True, "0.9", None):
            answers = self.fresh()
            answers["best_reply"]["confidence"] = bad
            self.assertInvalid(answers, f"bad confidence {bad!r}")

    # 13. invalid noul --------------------------------------------------------
    def test_13_invalid_noul(self):
        for bad in (True, False, "yes", float("nan"), float("inf"),
                    1.5, -0.2, None):
            answers = self.fresh()
            answers["literal_question"]["noul"] = bad
            self.assertInvalid(answers, f"bad noul {bad!r}")

    # 14. score outside allowed domain ---------------------------------------
    def test_14_score_outside_domain(self):
        for bad in (10, -1, 10.0, -0.01, float("nan"), float("inf"), True, "4"):
            answers = self.fresh()
            answers["danger_level"]["score"] = bad
            self.assertInvalid(answers, f"score {bad!r} outside 0..9")

    def test_score_probability_keys_must_be_complete(self):
        # Score distributions are over every level 0..n-1, like choice ones.
        answers = self.fresh()
        del answers["danger_level"]["probabilities"]["9"]
        self.assertInvalid(answers, "missing score probability key")
        answers = self.fresh()
        answers["danger_level"]["probabilities"]["10"] = 0.0
        self.assertInvalid(answers, "extra score probability key")

    def test_non_dict_answers_rejected(self):
        with self.assertRaises(JevError):
            _validate_answers(ALL_QUESTIONS, ["not", "an", "object"])
        with self.assertRaises(JevError):
            _validate_answers(ALL_QUESTIONS, None)


class EngineFallbackTests(unittest.TestCase):
    """code==0 envelope + malformed answers must trigger the existing fallback."""

    def make_envelope(self, answers):
        return io.BytesIO(json.dumps(
            {"code": 0, "message": "ok",
             "data": {"answers": answers, "usage": {}}}).encode("utf-8"))

    def test_malformed_answers_trigger_engine_fallback(self):
        requests_seen = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data.decode("utf-8"))
            requests_seen.append(body)
            questions = body["questions"]
            if "best_reply" in questions:
                # Second call = the old combined judge+rank fallback path.
                answers = make_valid_answers(questions)
                answers["best_reply"] = {
                    "type": "choice", "choice": "reply_b", "confidence": 0.8,
                    "probabilities": {"reply_a": 0.1, "reply_b": 0.8,
                                      "reply_c": 0.1},
                }
            else:
                # First call: HTTP/envelope success, but answers are malformed
                # (every requested judge answer is missing).
                answers = {}
            return self.make_envelope(answers)

        draft_calls = {}

        def fake_draft(messages, relationship, **kwargs):
            draft_calls["guidance"] = kwargs.get("guidance")
            return ["候选甲", "候选乙", "候选丙"]

        with patch.object(urllib.request, "urlopen", fake_urlopen), \
                patch.object(engine, "draft_candidates", fake_draft):
            result = engine.analyze(
                [("her", "你到底还在不在乎我？")], "friends",
                jev_provider="jevai", provider="deepseek")

        # Two Jev calls happened: the malformed judgment, then the fallback.
        self.assertEqual(len(requests_seen), 2)
        # The malformed first judgment was NOT treated as successful: blind draft.
        self.assertIsNone(draft_calls["guidance"])
        # The fallback second request re-asked the judge questions AND ranking.
        second_questions = requests_seen[1]["questions"]
        self.assertIn("best_reply", second_questions)
        self.assertIn("true_intent", second_questions)
        # Ranking from the valid fallback response is honored, not zeroed.
        self.assertEqual(result["best_index"], 1)
        self.assertEqual(result["answers"]["best_reply"]["choice"], "reply_b")
        for got, want in zip(result["scores"], (0.1, 0.8, 0.1)):
            self.assertAlmostEqual(got, want)

    def test_valid_first_judgment_keeps_three_stage_path(self):
        """Contrast: a valid first judgment yields guidance and a rank-only 2nd call."""
        requests_seen = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data.decode("utf-8"))
            requests_seen.append(body)
            return self.make_envelope(make_valid_answers(body["questions"]))

        draft_calls = {}

        def fake_draft(messages, relationship, **kwargs):
            draft_calls["guidance"] = kwargs.get("guidance")
            return ["候选甲", "候选乙", "候选丙"]

        with patch.object(urllib.request, "urlopen", fake_urlopen), \
                patch.object(engine, "draft_candidates", fake_draft):
            result = engine.analyze(
                [("her", "谢谢你昨天陪我")], "friends",
                jev_provider="jevai", provider="deepseek")

        # Successful judgment feeds non-empty guidance to drafting.
        self.assertIsNotNone(draft_calls["guidance"])
        # The second call is ranking-only (no judge questions re-asked).
        self.assertEqual(set(requests_seen[1]["questions"]), {"best_reply"})
        self.assertEqual(result["best_index"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
