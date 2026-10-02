# -*- coding: utf-8 -*-
"""整条链的唯一入口：对话 → Jev 判断 → 带着判断起草 3 条 → Jev 排序 → 结构化结果。

平台无关。SSE 消费者、悬浮窗、命令行 demo 都只调 analyze()。
"""
from __future__ import annotations

try:
    from .draft import draft_candidates
    from .jev_client import JevError, ask
    from .questions import JUDGE_QUESTIONS, build_rank_question, build_state, guidance_text
except ImportError:
    from draft import draft_candidates
    from jev_client import JevError, ask
    from questions import JUDGE_QUESTIONS, build_rank_question, build_state, guidance_text

_REPLY_IDX = {"reply_a": 0, "reply_b": 1, "reply_c": 2}


def _add_usage(total: dict, one: dict | None) -> None:
    """两次 Jev 调用的 usage 相加（tokens、cost）；非数字的字段后来的盖掉前面的。"""
    for k, v in (one or {}).items():
        total[k] = total.get(k, 0) + v if isinstance(v, (int, float)) else v


def analyze(messages: list, relationship: str, model: str | None = None,
            timeout: float = 30, context: int = 10, provider: str = "deepseek",
            base_url: str | None = None, reply_to: str | None = None, style: str = "",
            thinking: bool = False, jev_provider: str = "openrouter",
            jev_model: str | None = None) -> dict:
    """messages: [(from, text)] from ∈ {her, me}，最新一条在最后；
    群聊里可以带第三项 name（说这句话的人），单聊不带。
    context: 起草和判断各看最近多少条消息（用户设置里的「参考上下文」）。
    provider: 起草走哪家（core.providers.DRAFT_PROVIDERS），base_url 只有自定义来源要传。
    jev_provider / jev_model: 判断和排序走哪家、哪个模型（core.providers.JEV_PROVIDERS）。
    reply_to: 群聊里指定回复给谁；None = 正常回复。
    style: 用户自己描述的说话风格，只影响起草。
    thinking: 起草时是否开思考模式，只影响起草，默认关。
    model / jev_model = None 用该来源的默认模型。

    返回 {candidates, best_index, best_reply, scores, answers, usage, reply_to}。
    scores 是每条候选的胜出概率（0~1），取自 best_reply.probabilities，取不到记 0.0。
    只有对方最新说话时才有意义调它——是不是该触发由调用方判断（看 latest_from）。

    三段式（issue #4）：先让 Jev 答 7 道判断题，把判断当小抄喂给起草，最后 Jev 只排序。
    判断那次挂了就退回老路：盲起草 + 判断和排序一次问完，行为跟以前一样。usage 是两次之和。
    """
    state = build_state(messages, relationship, keep=context, reply_to=reply_to)
    usage: dict = {}
    answers: dict = {}
    judged = False
    try:
        first = ask(state, dict(JUDGE_QUESTIONS), timeout=timeout,
                    provider=jev_provider, model=jev_model)
        answers = first.get("answers") or {}
        _add_usage(usage, first.get("usage"))
        judged = True
    except JevError:
        pass  # 退回盲起草 + 老的一次合问；错误不打日志（里面可能带请求内容）

    candidates = draft_candidates(messages, relationship, provider=provider, model=model,
                                  base_url=base_url, timeout=timeout, keep=context,
                                  reply_to=reply_to, style=style, thinking=thinking,
                                  guidance=guidance_text(answers) if judged else None)
    if not candidates:  # 注入过滤可以把起草结果全扔掉；接着取 [0] 会 IndexError
        raise JevError("起草结果没有可用候选回复")

    questions = {} if judged else dict(JUDGE_QUESTIONS)
    if len(candidates) >= 2:  # 起草只给了 1 条就没什么可排的，判断题照问
        questions.update(build_rank_question(candidates))
    if questions:
        try:
            second = ask(state, questions, timeout=timeout,
                         provider=jev_provider, model=jev_model)
        except JevError:
            if not judged:  # 老路只有这一次调用，挂了就是挂了
                raise
            second = {}  # 判断还在，只是没排上序：下面按第一条推荐
        answers = {**answers, **(second.get("answers") or {})}
        _add_usage(usage, second.get("usage"))

    best_key = (answers.get("best_reply") or {}).get("choice")
    best_index = _REPLY_IDX.get(best_key, 0)  # 解析不出就退第一条
    if best_index >= len(candidates):
        best_index = 0

    probabilities = (answers.get("best_reply") or {}).get("probabilities") or {}
    scores = [0.0, 0.0, 0.0]
    for key, idx in _REPLY_IDX.items():
        try:
            scores[idx] = float(probabilities.get(key, 0.0))
        except (TypeError, ValueError):
            scores[idx] = 0.0  # 脏数据一律按 0 处理

    return {
        "candidates": candidates,
        "best_index": best_index,
        "best_reply": candidates[best_index],
        "scores": scores,
        "answers": answers,
        "usage": usage,
        "reply_to": reply_to,
    }


if __name__ == "__main__":
    # 候选被过滤光时要抛 JevError，不能在取第一条时 IndexError。
    import io
    import json
    import os
    import urllib.request
    from unittest.mock import patch

    os.environ.setdefault("JEV_API_KEY", "self-test-key")

    with patch("__main__.ask", return_value={"answers": {}, "usage": {}}), \
         patch("__main__.draft_candidates", return_value=[]):
        try:
            analyze([("her", "hello")], "friends")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "没有可用候选" in str(e)

    # PR #38：JevAI HTTP/envelope 成功但 answers 畸形 → 校验抛 JevError →
    # 第一次判定不算成功（盲起草、guidance=None）→ 走老路（判断+排序一次合问）。
    def _valid_answers(qs: dict) -> dict:
        out = {}
        for name, q in qs.items():
            kind = q["type"]
            if kind == "noul":
                out[name] = {"type": "noul", "noul": 0.8}
            elif kind == "choice":
                keys = list(q["criteria"])
                out[name] = {"type": "choice", "choice": keys[0], "confidence": 1.0,
                             "probabilities": {k: (1.0 if k == keys[0] else 0.0)
                                               for k in keys}}
            else:
                levels = len(q["criteria"])
                peak = min(4, levels - 1)
                out[name] = {"type": "score", "score": float(peak), "confidence": 1.0,
                             "probabilities": {str(i): (1.0 if i == peak else 0.0)
                                               for i in range(levels)}}
        return out

    seen_requests = []

    def _fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        seen_requests.append(body)
        # 第一次（只判 7 题）回畸形空答案；第二次（兜底合问，含 best_reply）回合法答案
        answers = _valid_answers(body["questions"]) if "best_reply" in body["questions"] else {}
        return io.BytesIO(json.dumps(
            {"code": 0, "message": "ok",
             "data": {"answers": answers, "usage": {}}}).encode("utf-8"))

    captured = {}

    def _fake_draft(messages, relationship, **kwargs):
        captured["guidance"] = kwargs.get("guidance")
        return ["候选甲", "候选乙", "候选丙"]

    with patch.object(urllib.request, "urlopen", _fake_urlopen), \
         patch("__main__.draft_candidates", _fake_draft):
        result = analyze([("her", "你还在乎我吗")], "friends", jev_provider="jevai")

    assert captured["guidance"] is None, "畸形判定不能被当成成功，必须盲起草"
    assert len(seen_requests) == 2, "畸形判定后应再发一次兜底合问"
    second_q = seen_requests[1]["questions"]
    assert "best_reply" in second_q and "true_intent" in second_q, "兜底必须判断+排序合问"
    assert result["answers"]["best_reply"]["choice"] == "reply_a"
    print("engine ok")
