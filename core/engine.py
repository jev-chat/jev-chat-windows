# -*- coding: utf-8 -*-
"""整条链的唯一入口：对话 → Jev 判断 → 带着判断起草 3 条 → Jev 排序 → 结构化结果。

平台无关。SSE 消费者、悬浮窗、命令行 demo 都只调 analyze()。
"""
from __future__ import annotations

try:
    from . import draft as draft_mod
    from .draft import draft_candidates, vision_transcribe
    from .jev_client import JevError, ask
    from .questions import JUDGE_QUESTIONS, build_rank_question, build_state, guidance_text
except ImportError:
    from . import draft as draft_mod
    from draft import draft_candidates, vision_transcribe
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
            jev_model: str | None = None, draft_extra: dict | None = None,
            jev_base_url: str | None = None, filter_noise: bool = False,
            vision_image: bytes | None = None) -> dict:
    """messages: [(from, text)] from ∈ {her, me}，最新一条在最后；
    群聊里可以带第三项 name（说这句话的人），单聊不带。
    context: 起草和判断各看最近多少条消息（用户设置里的「参考上下文」）。
    provider: 起草走哪家（core.providers.DRAFT_PROVIDERS），base_url 只有自定义来源要传。
    jev_provider / jev_model: 判断和排序走哪家、哪个模型（core.providers.JEV_PROVIDERS）；
    jev_base_url 只有 TypeSafe 直连认（代理/自建地址）。
    draft_extra: 设置里「高级参数」的 JSON，浅合并进起草请求体。
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
    if vision_image:
        # 视觉模式：先让视觉模型把截图转写成消息列表（免 OCR 文字），判断和起草都用它
        try:
            transcribed = draft_mod.vision_transcribe(
                vision_image, relationship, provider=provider, model=model, base_url=base_url,
                timeout=timeout, extra_params=draft_extra)
            if transcribed:
                messages = transcribed[-context:] if context else transcribed
        except JevError:
            pass  # 转写失败退回 OCR 文字路线
    state = build_state(messages, relationship, keep=context, reply_to=reply_to)
    usage: dict = {}
    answers: dict = {}
    judged = False
    try:
        first = ask(state, dict(JUDGE_QUESTIONS), timeout=timeout,
                    provider=jev_provider, model=jev_model, base_url=jev_base_url)
        answers = first.get("answers") or {}
        _add_usage(usage, first.get("usage"))
        judged = True
    except JevError:
        pass  # 退回盲起草 + 老的一次合问；错误不打日志（里面可能带请求内容）

    candidates = draft_candidates(messages, relationship, provider=provider, model=model,
                                  base_url=base_url, timeout=timeout, keep=context,
                                  reply_to=reply_to, style=style, thinking=thinking,
                                  guidance=guidance_text(answers) if judged else None,
                                  extra_params=draft_extra, filter_noise=filter_noise,
                                  vision_image=vision_image)
    if not candidates:
        # 过滤后候选全空：不抛的话下面 candidates[best_index] 会 IndexError，
        # 界面只能显示通用失败提示；走 JevError 才能带上人话原因
        raise JevError("三条候选全被过滤（对话可能全是系统通知或注入样本），稍后再试")

    questions = {} if judged else dict(JUDGE_QUESTIONS)
    if len(candidates) >= 2:  # 起草只给了 1 条就没什么可排的，判断题照问
        questions.update(build_rank_question(candidates))
    if questions:
        try:
            second = ask(state, questions, timeout=timeout,
                         provider=jev_provider, model=jev_model,
                         base_url=jev_base_url)
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
