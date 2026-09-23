# -*- coding: utf-8 -*-
"""Jev 判断 API 客户端：OpenRouter 或 TypeSafe 直连。

TypeSafe 直连走官方 `typesafe_sdk`；OpenRouter 这条是唯一自己拼 HTTP 的路——
SDK 把路径写死成 `/v1/systemone`，打不到 OpenRouter 的 `/api/alpha/decisions`。
两条路返回同一个 dict 形状，engine 不关心跑的是哪条。key 只从环境变量读，绝不打进日志。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import NoReturn

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .providers import (ENV_VARS, JEV_BASE, JEV_ENV, JEV_PROVIDERS, LEGACY, OPENROUTER_BASE,
                            OPENROUTER_DECISIONS, TYPESAFE_BASE)
except ImportError:
    from providers import (ENV_VARS, JEV_BASE, JEV_ENV, JEV_PROVIDERS, LEGACY, OPENROUTER_BASE,
                           OPENROUTER_DECISIONS, TYPESAFE_BASE)

MAX_RETRIES = 3


class JevError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def redact_secrets(text: str) -> str:
    """Strip every live key from any string before print or disk write."""
    if not isinstance(text, str):
        text = str(text)
    for env in ENV_VARS:
        key = os.environ.get(env) or ""
        if key:
            text = text.replace(key, "[REDACTED]")
    return text


def _status_of(exc: Exception) -> int | None:
    """各家 SDK 放 HTTP 状态码的属性名不一样：openai/anthropic 是 status_code，
    google-genai 是 code（它的 status 是 'NOT_FOUND' 这种字符串），typesafe 是 status。"""
    for name in ("status_code", "code", "status"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    return None


def _fail(exc: Exception, what: str) -> NoReturn:
    """SDK 抛的异常 → 一句人话的 JevError。消息过脱敏，绝不把 key 带出来。"""
    if isinstance(exc, JevError):
        raise exc
    status = _status_of(exc)
    hint = {401: "密钥被拒", 403: "没有权限", 404: "模型或地址不对", 422: "请求被拒",
            429: "被限流", 529: "服务过载"}.get(status, "")
    detail = redact_secrets(str(exc)).strip()[:300]
    head = f"{what} HTTP {status}" if status else f"{what}失败"
    raise JevError(f"{head}: {hint or detail or type(exc).__name__}", status) from None


def _api_key(env: str = JEV_ENV) -> str:
    """两把 key 之一（JEV_API_KEY / LLM_API_KEY）。新名字空着就退回老名字，老用户不用重填。"""
    key = ((os.environ.get(env) or "").strip()
           or (os.environ.get(LEGACY.get(env, "")) or "").strip())
    if not key:
        raise JevError(
            f"{env} is not set. Export it in the environment; "
            "do not put the key in a file."
        )
    return key


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    return redact_secrets(raw)[:800]


def ask(state: dict, questions: dict, timeout: float = 20,
        provider: str = "openrouter", model: str | None = None,
        base_url: str | None = None) -> dict:
    """问 Jev 一轮判断，返回 {"answers": {名字: 答案}, "usage": {...}}。

    provider ∈ JEV_PROVIDERS（openrouter / typesafe 直连 / custom 同协议自建）；
    model=None 用该来源的默认模型。base_url：typesafe 是可选的代理地址；custom 必填。
    OpenRouter 和 custom 走同一条私有协议（POST {model, state, questions} 到
    /api/alpha/decisions），返回的 dict 形状一模一样；typesafe 走官方 SDK，答案形状也对齐。
    429/529 都会退避重试。绝不打印或写出 key。
    """
    spec = JEV_PROVIDERS.get(provider) or JEV_PROVIDERS["openrouter"]
    key = _api_key(JEV_ENV)  # 各来源共用同一把判断 key，换来源不用重填
    model = model or spec.default
    if provider == "typesafe":
        return _ask_typesafe(state, questions, key, model, timeout, base_url)
    # openrouter / custom 走同一条私有协议；base_url 没填用 OpenRouter 官方地址，
    # 填了就按它算（openrouter 填镜像、custom 填自建，同一个道理）
    return _ask_decisions(_decisions_url(base_url) if (base_url or "").strip()
                          else OPENROUTER_DECISIONS,
                          state, questions, key, model, timeout)


def _answer(answer) -> dict:
    """SDK 的答案对象 → OpenRouter 那条路 JSON 出来的同一个形状。"""
    if answer.type == "noul":
        return {"type": "noul", "noul": answer.noul}
    if answer.type == "choice":
        return {"type": "choice", "choice": answer.choice, "confidence": answer.confidence,
                "probabilities": dict(answer.probabilities)}
    # score：SDK 把概率的 key 转成了 int，这里转回字符串，跟 JSON 那条路对齐
    return {"type": "score", "score": answer.score, "confidence": answer.confidence,
            "probabilities": {str(k): v for k, v in answer.probabilities.items()}}


def _ask_typesafe(state: dict, questions: dict, key: str, model: str, timeout: float,
                  base_url: str | None = None) -> dict:
    """官方 typesafe_sdk。questions 原样传：core/questions.py 里那几个 dict 本身就是 SDK 的
    NoulModel / ChoiceModel / ScoreModel（SDK 的 normalize_questions 认 dict），不用再包一层对象。
    base_url 空 = 表里的默认地址，填了 = 代理/自建。重试用 RetryPolicy 的默认值——它本来就重试
    408/429/5xx（含 529）并退避。"""
    import typesafe_sdk

    try:
        with typesafe_sdk.TypeSafeClient(api_key=key, base_url=base_url or TYPESAFE_BASE, model=model,
                                         timeout=timeout) as client:
            result = client.system_one(state, questions, model=model)
    except Exception as exc:
        _fail(exc, "Jev 判断")
    return {
        "answers": {name: _answer(a) for name, a in result.answers.items()},
        "usage": {"input_tokens": result.usage.input_tokens,
                  "output_tokens": result.usage.output_tokens},
    }


def _decisions_url(base_url: str) -> str:
    """私有协议的完整地址：给 base 补 /api/alpha/decisions；已经写到 /decisions 的照用。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise JevError("自定义判断来源要填 Base URL")
    return base if base.endswith("/decisions") else base + "/api/alpha/decisions"


def _ask_decisions(url: str, state: dict, questions: dict, key: str, model: str,
                   timeout: float) -> dict:
    """Jev 私有协议：POST {model, state, questions}，返回 JSON。OpenRouter 官方和自定义
    （代理/镜像/自建，同协议）都走这一条。429/529 退避重试 3 次。"""
    payload = json.dumps(
        {"model": model, "state": state, "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")

    last_status: int | None = None
    last_body = ""
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            last_body = _error_body(exc)
            if last_status in (429, 529) and attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            readable = {
                401: f"Jev HTTP 401: API key rejected. Check {JEV_ENV}.",
                422: f"Jev HTTP 422: request body rejected. {last_body}",
                429: f"Jev HTTP 429: rate limited after {MAX_RETRIES} retries. {last_body}",
                529: f"Jev HTTP 529: provider overloaded after {MAX_RETRIES} retries. {last_body}",
            }.get(last_status, f"Jev HTTP {last_status}: {last_body}")
            raise JevError(readable, last_status) from None
        except (TimeoutError, socket.timeout) as exc:
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise JevError(f"Jev request timed out after {timeout}s") from exc
        except urllib.error.URLError as exc:
            reason = redact_secrets(getattr(exc, "reason", exc))
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise JevError(f"Jev request failed: {reason}") from None

    raise JevError(
        f"Jev HTTP {last_status}: exhausted retries. {last_body}", last_status
    )


def list_models(provider: str, key: str, timeout: float = 10,
                base_url: str | None = None) -> list[str]:
    """某家能用的 Jev 模型 id，去重排序。失败抛 JevError（设置页直接显示这句话）。"""
    if provider == "typesafe":
        import typesafe_sdk

        try:
            with typesafe_sdk.TypeSafeClient(api_key=key, base_url=base_url or TYPESAFE_BASE,
                                             timeout=timeout) as client:
                return sorted({m.name for m in client.models.list().models})
        except Exception as exc:
            _fail(exc, "取模型列表")
    if provider == "custom":
        if not (base_url or "").strip():
            raise JevError("自定义判断来源要填 Base URL")
        from .llm import list_models as _models
        return _models("openai", base_url, key, timeout)
    try:  # 只在这儿 import：llm 模块头上要 jev_client 的 _fail，放模块级就转圈了
        from .llm import list_models as _models
    except ImportError:
        from llm import list_models as _models
    if base_url:  # openrouter 换成了镜像：地址跟设置页里填的走，全量返回（镜像上没有 typesafe/ 前缀约定）
        return _models("openai", base_url, key, timeout)
    # OpenRouter 官方上几百个模型，只有 typesafe/ 这几个是 Jev
    return [i for i in _models("openai", OPENROUTER_BASE, key, timeout)
            if i.startswith("typesafe/")]


if __name__ == "__main__":
    # ponytail: 不联网。两条路各测一次：SDK 那条在 typesafe_sdk 边界换成假客户端，
    # urllib 那条 mock urlopen。会坏的地方就一个——答案对象 → dict 的映射得跟 JSON 那条一模一样。
    import io
    import types as _t
    from unittest.mock import patch

    import typesafe_sdk

    try:
        from .questions import JUDGE_QUESTIONS, build_rank_question
    except ImportError:
        from questions import JUDGE_QUESTIONS, build_rank_question

    os.environ.pop(JEV_ENV, None)
    os.environ["OPENROUTER_API_KEY"] = "or-key"  # 老名字：新名字没设时该退回它
    assert _api_key(JEV_ENV) == "or-key"
    os.environ[JEV_ENV] = "ts-key"  # 新名字在就用新的，两家来源共用这一把
    questions = dict(JUDGE_QUESTIONS)
    questions.update(build_rank_question(["甲", "乙", "丙"]))
    seen: dict = {}

    class _FakeClient:
        def __init__(self, **kw):
            seen["init"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def system_one(self, state, qs, **kw):
            seen["state"], seen["questions"], seen["kw"] = state, qs, kw
            return _t.SimpleNamespace(
                answers={
                    "literal_question": _t.SimpleNamespace(type="noul", noul=0.9),
                    "best_reply": _t.SimpleNamespace(
                        type="choice", choice="reply_b", confidence=0.7,
                        probabilities={"reply_a": 0.2, "reply_b": 0.7, "reply_c": 0.1}),
                    "danger_level": _t.SimpleNamespace(
                        type="score", score=4.0, confidence=0.6, probabilities={4: 0.6, 5: 0.4}),
                },
                usage=_t.SimpleNamespace(input_tokens=11, output_tokens=22))

        @property
        def models(self):
            return _t.SimpleNamespace(list=lambda: _t.SimpleNamespace(models=(
                _t.SimpleNamespace(name="jev-preview"), _t.SimpleNamespace(name="jev-latest"))))

    with patch.object(typesafe_sdk, "TypeSafeClient", _FakeClient):
        got = ask({"chat": {}}, questions, timeout=15, provider="typesafe", model="jev-1.13.0")
        ask_init = seen["init"]
        assert list_models("typesafe", "ts-key") == ["jev-latest", "jev-preview"]
        assert seen["init"] == {"api_key": "ts-key", "base_url": TYPESAFE_BASE, "timeout": 10}
    assert ask_init == {"api_key": "ts-key", "base_url": TYPESAFE_BASE,
                        "model": "jev-1.13.0", "timeout": 15}
    assert seen["kw"] == {"model": "jev-1.13.0"}
    # 题目原样进 SDK：它们本身就是 NoulModel / ChoiceModel / ScoreModel，不用再包一层
    assert seen["questions"] is questions
    assert seen["questions"]["danger_level"]["type"] == "score"
    assert isinstance(seen["questions"]["danger_level"]["criteria"], list)
    assert seen["questions"]["best_reply"]["criteria"] == {
        "reply_a": "甲", "reply_b": "乙", "reply_c": "丙"}
    # 映射出来的形状跟 OpenRouter 那条路的 JSON 必须一致（engine 不关心跑的是哪条）
    assert got["answers"]["literal_question"] == {"type": "noul", "noul": 0.9}
    assert got["answers"]["best_reply"] == {
        "type": "choice", "choice": "reply_b", "confidence": 0.7,
        "probabilities": {"reply_a": 0.2, "reply_b": 0.7, "reply_c": 0.1}}
    assert got["answers"]["danger_level"] == {
        "type": "score", "score": 4.0, "confidence": 0.6,
        "probabilities": {"4": 0.6, "5": 0.4}}  # score 的概率 key 转回字符串
    assert got["usage"] == {"input_tokens": 11, "output_tokens": 22}

    class _Boom(Exception):
        status = 429

    with patch.object(typesafe_sdk, "TypeSafeClient", lambda **kw: (_ for _ in ()).throw(_Boom("x"))):
        try:
            ask({"chat": {}}, questions, provider="typesafe")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert e.status == 429 and "被限流" in str(e)

    # OpenRouter 那条没动：还是自己拼 body、打 /api/alpha/decisions
    body = {"answers": {"best_reply": {"type": "choice", "choice": "reply_a"}}, "usage": {}}

    def _fake_urlopen(req, timeout=None):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data.decode("utf-8"))
        return io.BytesIO(json.dumps(body).encode("utf-8"))

    with patch.object(urllib.request, "urlopen", _fake_urlopen):
        assert ask({"chat": {}}, questions) == body
    assert seen["url"] == OPENROUTER_DECISIONS
    assert seen["body"]["model"] == "typesafe/jev-1.13" and seen["body"]["questions"] == questions

    with patch("llm.list_models" if __package__ is None else "core.llm.list_models",
               lambda *a, **k: ["openai/gpt-4o", "typesafe/jev-1.13", "typesafe/jev-preview"]):
        assert list_models("openrouter", "or-key") == ["typesafe/jev-1.13", "typesafe/jev-preview"]

    assert redact_secrets("key=ts-key or-key") == "key=[REDACTED] [REDACTED]"
    print("jev_client ok")
