# -*- coding: utf-8 -*-
"""Jev 判断 API 客户端：OpenRouter、TypeSafe 直连或 JevAI Community。

TypeSafe 直连走官方 `typesafe_sdk`；OpenRouter 和 JevAI Community
使用各自的 HTTP Decisions endpoint。
三条路最终归一化为同一个 dict 形状，engine 不关心具体来源。
key 只从环境变量读，绝不打进日志。
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
import urllib.error
import urllib.request
from typing import NoReturn

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .providers import (ENV_VARS, JEVAI_DECISIONS, JEV_ENV, JEV_PROVIDERS, LEGACY,
                            OPENROUTER_DECISIONS, OPENROUTER_KEY_URL, TYPESAFE_BASE)
except ImportError:
    from providers import (ENV_VARS, JEVAI_DECISIONS, JEV_ENV, JEV_PROVIDERS, LEGACY,
                           OPENROUTER_DECISIONS, OPENROUTER_KEY_URL, TYPESAFE_BASE)

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
        provider: str = "openrouter", model: str | None = None) -> dict:
    """问 Jev 一轮判断，返回 {"answers": {名字: 答案}, "usage": {...}}。

    provider ∈ JEV_PROVIDERS（openrouter / typesafe 直连）；model=None 用该来源的默认模型。
    两条路返回的 dict 形状一模一样，429/529 都会退避重试。绝不打印或写出 key。
    """
    spec = JEV_PROVIDERS.get(provider) or JEV_PROVIDERS["openrouter"]
    key = _api_key(JEV_ENV)  # 所有 Jev 来源共用这一 key 槽位，换来源时重填
    model = model or spec.default
    if provider == "typesafe":
        return _ask_typesafe(state, questions, key, model, timeout)
    if provider == "jevai":
        return _ask_jevai(state, questions, key, model, timeout)
    return _ask_openrouter(state, questions, key, model, timeout)


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


def _ask_typesafe(state: dict, questions: dict, key: str, model: str, timeout: float) -> dict:
    """官方 typesafe_sdk。questions 原样传：core/questions.py 里那几个 dict 本身就是 SDK 的
    NoulModel / ChoiceModel / ScoreModel（SDK 的 normalize_questions 认 dict），不用再包一层对象。
    重试用 RetryPolicy 的默认值——它本来就重试 408/429/5xx（含 529）并退避。"""
    import typesafe_sdk

    try:
        with typesafe_sdk.TypeSafeClient(api_key=key, base_url=TYPESAFE_BASE, model=model,
                                         timeout=timeout) as client:
            result = client.system_one(state, questions, model=model)
    except Exception as exc:
        _fail(exc, "Jev 判断")
    return {
        "answers": {name: _answer(a) for name, a in result.answers.items()},
        "usage": {"input_tokens": result.usage.input_tokens,
                  "output_tokens": result.usage.output_tokens},
    }


# ---------------------------------------------------------------------------
# Application-owned response validation (fail-closed).
#
# A provider only proves it speaks HTTP with code == 0; it does not prove the
# answers are well-formed.  engine.analyze() treats any answers dict it receives
# as a successful judgment, so malformed choice/score payloads would otherwise
# flow into guidance_text() and silently degrade ranking to "first candidate /
# zero probabilities".  Validate against the EXACT questions we requested and
# raise JevError on any mismatch; the engine already turns that into its normal
# network/schema-failure fallback.  Nothing here coerces bad data into defaults.
# ---------------------------------------------------------------------------

# Probability distributions "sum to approximately 1" per the TypeSafe/Jev
# contract.  Tolerate provider rounding (e.g. three 0.3333 bins -> 0.9999);
# a genuinely wrong total such as 0.98 is still rejected.
_PROB_SUM_TOLERANCE = 1e-3


def _is_finite_number(value) -> bool:
    """True only for real int/float (bool excluded) that is not NaN/±Infinity."""
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value))


def _require_unit_interval(value, qid: str, field: str) -> float:
    """Validate a noul / confidence / probability value: finite number in [0, 1]."""
    if not _is_finite_number(value):
        raise JevError(
            f"JevAI answer {qid!r}: {field} must be a finite number, got {value!r}")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise JevError(
            f"JevAI answer {qid!r}: {field} must be within [0, 1], got {number!r}")
    return number


def _validate_distribution(probabilities, ordered_keys: list, qid: str) -> dict:
    """Validate a choice/score probability map against the exact allowed keys.

    Every allowed key must be present (complete distribution), no unknown keys
    are accepted, and each value is finite in [0, 1] with the total ~= 1.
    Returns the distribution normalized to float values in canonical key order.
    """
    if not isinstance(probabilities, dict):
        raise JevError(
            f"JevAI answer {qid!r}: probabilities must be an object, "
            f"got {type(probabilities).__name__}")
    allowed = list(ordered_keys)
    allowed_set = set(allowed)
    found = set(probabilities)
    missing = sorted(allowed_set - found)
    unknown = sorted(found - allowed_set)
    if missing:
        raise JevError(
            f"JevAI answer {qid!r}: probabilities missing keys {missing}")
    if unknown:
        raise JevError(
            f"JevAI answer {qid!r}: probabilities have unknown keys {unknown}")
    normalized: dict = {}
    total = 0.0
    for key in allowed:
        probability = _require_unit_interval(probabilities[key], qid,
                                             f"probabilities[{key!r}]")
        normalized[key] = probability
        total += probability
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=_PROB_SUM_TOLERANCE):
        raise JevError(
            f"JevAI answer {qid!r}: probabilities must sum to ~1, got {total!r}")
    return normalized


def _validate_answers(questions: dict, answers: dict) -> dict:
    """Fail-closed validation of one JevAI ``answers`` payload.

    ``questions`` is the exact question dict sent in this request; ``answers``
    is the provider's ``data.answers``.  Returns a normalized, provider-
    independent answers dict with the same shape the other providers produce:

    - noul:   {"type": "noul", "noul": float}
    - choice: {"type": "choice", "choice": str, "confidence": float,
               "probabilities": {choice_key: float}}
    - score:  {"type": "score", "score": float, "confidence": float,
               "probabilities": {"0".."n-1": float}}

    Raises JevError on any structural or value-level violation.
    """
    if not isinstance(questions, dict):
        raise JevError("JevAI validation: requested questions must be an object")
    if not isinstance(answers, dict):
        raise JevError("JevAI response: answers must be an object keyed by question id")

    requested = set(questions)
    received = set(answers)
    missing = sorted(requested - received)
    unknown = sorted(received - requested)
    if missing:
        raise JevError(f"JevAI response is missing answers: {missing}")
    if unknown:
        raise JevError(f"JevAI response contains unknown answers: {unknown}")

    normalized: dict = {}
    for qid, question in questions.items():
        if not isinstance(question, dict) or "type" not in question:
            # Questions are application-owned; an unreadable one is our bug, but
            # fail closed rather than trust the provider against an unknown schema.
            raise JevError(f"JevAI validation: question {qid!r} is malformed")
        answer = answers[qid]
        if not isinstance(answer, dict):
            raise JevError(f"JevAI answer {qid!r} must be an object, got {type(answer).__name__}")

        qtype = question["type"]
        if answer.get("type") != qtype:
            raise JevError(
                f"JevAI answer {qid!r}: expected type {qtype!r}, "
                f"got {answer.get('type')!r}")
        criteria = question.get("criteria")

        if qtype == "noul":
            normalized[qid] = {
                "type": "noul",
                "noul": _require_unit_interval(answer.get("noul"), qid, "noul"),
            }

        elif qtype == "choice":
            if not isinstance(criteria, dict) or not criteria:
                raise JevError(
                    f"JevAI validation: choice question {qid!r} needs a non-empty "
                    "criteria object")
            choice = answer.get("choice")
            if not isinstance(choice, str) or choice not in criteria:
                raise JevError(
                    f"JevAI answer {qid!r}: choice {choice!r} is not one of "
                    f"{list(criteria)}")
            confidence = _require_unit_interval(answer.get("confidence"), qid, "confidence")
            probabilities = _validate_distribution(
                answer.get("probabilities"), list(criteria), qid)
            normalized[qid] = {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": probabilities,
            }

        elif qtype == "score":
            # Score criteria is an ordered list of levels numbered 0..n-1.
            if not isinstance(criteria, list) or len(criteria) < 2:
                raise JevError(
                    f"JevAI validation: score question {qid!r} needs a criteria list "
                    "of at least two levels")
            top = len(criteria) - 1
            score = answer.get("score")
            if not _is_finite_number(score):
                raise JevError(
                    f"JevAI answer {qid!r}: score must be a finite number, got {score!r}")
            score = float(score)
            if not 0.0 <= score <= top:
                raise JevError(
                    f"JevAI answer {qid!r}: score {score!r} outside domain [0, {top}]")
            confidence = _require_unit_interval(answer.get("confidence"), qid, "confidence")
            probabilities = _validate_distribution(
                answer.get("probabilities"), [str(i) for i in range(top + 1)], qid)
            normalized[qid] = {
                "type": "score",
                "score": score,
                "confidence": confidence,
                "probabilities": probabilities,
            }

        else:
            raise JevError(
                f"JevAI validation: question {qid!r} has unsupported type {qtype!r}")

    return normalized


def _ask_jevai(state: dict, questions: dict, key: str, model: str, timeout: float) -> dict:
    """JevAI Community native Decisions adapter.

    The community API wraps Jev answers in {code, message, data}; normalize that
    envelope to the same {"answers": ..., "usage": ...} shape used elsewhere.
    """
    payload = json.dumps(
        {"model": model, "state": state, "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")

    last_status: int | None = None
    last_body = ""
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            JEVAI_DECISIONS,
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
                body = json.loads(raw)

            if not isinstance(body, dict):
                raise JevError("JevAI response is not a JSON object")

            code = body.get("code")
            if code != 0:
                message = redact_secrets(str(body.get("message") or "request rejected"))[:300]
                raise JevError(f"JevAI code {code}: {message}")

            data = body.get("data")
            if not isinstance(data, dict):
                raise JevError("JevAI response missing data")

            answers = data.get("answers")
            if not isinstance(answers, dict):
                raise JevError("JevAI response missing data.answers")

            # Fail closed: validate against the exact questions we sent before any
            # answer can reach the engine.  A malformed payload raises JevError here
            # (never retried, never coerced), so the engine takes its normal fallback.
            answers = _validate_answers(questions, answers)

            usage = data.get("usage")
            return {
                "answers": answers,
                "usage": usage if isinstance(usage, dict) else {},
            }

        except JevError:
            raise
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            last_body = _error_body(exc)
            if last_status in (429, 503, 529) and attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            readable = {
                401: "JevAI HTTP 401: API key rejected.",
                403: "JevAI HTTP 403: API key has no permission.",
                422: f"JevAI HTTP 422: request body rejected. {last_body}",
                429: f"JevAI HTTP 429: rate limited after {MAX_RETRIES} retries. {last_body}",
                503: f"JevAI HTTP 503: service unavailable after {MAX_RETRIES} retries. {last_body}",
                529: f"JevAI HTTP 529: provider overloaded after {MAX_RETRIES} retries. {last_body}",
            }.get(last_status, f"JevAI HTTP {last_status}: {last_body}")
            raise JevError(readable, last_status) from None
        except (TimeoutError, socket.timeout) as exc:
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise JevError(f"JevAI request timed out after {timeout}s") from exc
        except urllib.error.URLError as exc:
            reason = redact_secrets(getattr(exc, "reason", exc))
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise JevError(f"JevAI request failed: {reason}") from None
        except (ValueError, json.JSONDecodeError) as exc:
            raise JevError(f"JevAI returned invalid JSON: {redact_secrets(exc)}") from None

    raise JevError(
        f"JevAI HTTP {last_status}: exhausted retries. {last_body}", last_status
    )


def _ask_openrouter(state: dict, questions: dict, key: str, model: str, timeout: float) -> dict:
    """OpenRouter 的 /api/alpha/decisions，手写 urllib。429/529 退避重试 3 次。"""
    payload = json.dumps(
        {"model": model, "state": state, "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")

    last_status: int | None = None
    last_body = ""
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            OPENROUTER_DECISIONS,
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


def _check_openrouter_key(key: str, timeout: float) -> None:
    """免费的 auth/key 探测：401/403 说明 key 不对，别的错（超时/断网）也如实上报。
    列表本身是写死的，key 对不对只有靠它才知道，别等第一次判断才暴露。"""
    req = urllib.request.Request(OPENROUTER_KEY_URL,
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        hint = {401: "密钥被拒", 403: "没有权限"}.get(exc.code, _error_body(exc)[:200])
        raise JevError(f"取模型列表 HTTP {exc.code}: {hint}") from None
    except (TimeoutError, socket.timeout):
        raise JevError(f"取模型列表请求超时（{timeout}s）") from None
    except urllib.error.URLError as exc:
        raise JevError(
            f"取模型列表失败: {redact_secrets(getattr(exc, 'reason', exc))}") from None


def list_models(provider: str, key: str, timeout: float = 10) -> list[str]:
    """某家能用的 Jev 模型 id，去重排序。失败抛 JevError（设置页直接显示这句话）。"""
    if provider == "jevai":
        # JevAI currently documents no model-list or free key-validation endpoint.
        # Expose the documented Jev identifier; credentials are validated on the
        # first real decision request.
        return ["typesafe-ai/jev"]
    if provider == "typesafe":
        import typesafe_sdk

        try:
            with typesafe_sdk.TypeSafeClient(api_key=key, base_url=TYPESAFE_BASE,
                                             timeout=timeout) as client:
                return sorted({m.name for m in client.models.list().models})
        except Exception as exc:
            _fail(exc, "取模型列表")
    # OpenRouter 的 Jev 是 Decisions API 专属模型，不在 /api/v1/models 目录里
    # （也没有列它的专用端点），列表按官方模型页写死，别名列排最前（永远指向最新版）。
    # key 对不对由探测兜着，别让坏密钥等到第一次判断才暴露。
    _check_openrouter_key(key, timeout)
    return ["~typesafe/jev-latest", "typesafe/jev-1.13"]


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

    # JevAI Community uses the same state/questions payload but wraps answers in
    # {code, message, data}; the adapter normalizes that envelope AND fail-closed
    # validates every answer against the exact questions we sent.
    def _valid_jevai_answers(qs: dict) -> dict:
        """A schema-valid answer set for any questions dict (peaked distributions)."""
        out = {}
        for name, q in qs.items():
            kind = q["type"]
            if kind == "noul":
                out[name] = {"type": "noul", "noul": 0.8}
            elif kind == "choice":
                keys = list(q["criteria"])
                out[name] = {
                    "type": "choice", "choice": keys[0], "confidence": 1.0,
                    "probabilities": {k: (1.0 if k == keys[0] else 0.0) for k in keys},
                }
            else:  # score：criteria 是有序 level 列表，概率 key 是字符串序号 0..n-1
                levels = len(q["criteria"])
                peak = min(4, levels - 1)
                out[name] = {
                    "type": "score", "score": float(peak), "confidence": 1.0,
                    "probabilities": {str(i): (1.0 if i == peak else 0.0)
                                      for i in range(levels)},
                }
        return out

    jevai_answers = _valid_jevai_answers(questions)
    jevai_body = {
        "code": 0,
        "message": "ok",
        "data": {
            "answers": jevai_answers,
            "usage": {"input_tokens": 7, "output_tokens": 0},
        },
    }

    def _fake_jevai(req, timeout=None):
        seen["jevai_url"] = req.full_url
        seen["jevai_auth"] = req.headers["Authorization"]
        seen["jevai_body"] = json.loads(req.data.decode("utf-8"))
        return io.BytesIO(json.dumps(jevai_body).encode("utf-8"))

    with patch.object(urllib.request, "urlopen", _fake_jevai):
        got = ask(
            {"chat": {}}, questions, provider="jevai",
            model="typesafe-ai/jev"
        )

    assert seen["jevai_url"] == JEVAI_DECISIONS
    assert seen["jevai_auth"] == "Bearer ts-key"
    assert seen["jevai_body"]["model"] == "typesafe-ai/jev"
    assert seen["jevai_body"]["questions"] == questions
    # 校验 + 归一化后仍是 provider-independent 的同一形状（legend 等多余字段不带进 engine）
    assert got == {
        "answers": _valid_jevai_answers(questions),
        "usage": jevai_body["data"]["usage"],
    }
    assert list_models("jevai", "unused") == ["typesafe-ai/jev"]

    # Fail-closed：HTTP/envelope 成功但答案畸形，必须抛 JevError，绝不静默兜底。
    # 评审点名的四类：缺答案、多答案、选项越界、非有限概率。
    def _expect_jevai_error(label: str, malformed: dict) -> None:
        try:
            _validate_answers(questions, malformed)
        except JevError:
            return
        raise SystemExit(f"应当抛错: {label}")

    missing = _valid_jevai_answers(questions)
    del missing["true_intent"]
    _expect_jevai_error("missing answer", missing)

    extra = _valid_jevai_answers(questions)
    extra["not_asked"] = {"type": "noul", "noul": 0.5}
    _expect_jevai_error("extra answer", extra)

    bad_choice = _valid_jevai_answers(questions)
    bad_choice["best_reply"]["choice"] = "reply_z"
    _expect_jevai_error("choice outside criteria", bad_choice)

    for bad in (float("nan"), float("inf"), float("-inf"), -0.1, 1.5):
        bad_prob = _valid_jevai_answers(questions)
        bad_prob["best_reply"]["probabilities"]["reply_b"] = bad
        _expect_jevai_error(f"non-finite/out-of-range probability {bad}", bad_prob)

    # 端到端：code==0 但 data.answers 缺题，ask() 也得抛，不能把畸形答案交给 engine
    bad_body = {"code": 0, "message": "ok",
                "data": {"answers": {"best_reply": jevai_answers["best_reply"]},
                         "usage": {}}}
    with patch.object(urllib.request, "urlopen",
                      lambda req, timeout=None: io.BytesIO(
                          json.dumps(bad_body).encode("utf-8"))):
        try:
            ask({"chat": {}}, questions, provider="jevai")
            raise SystemExit("应当抛错: envelope ok but answers malformed")
        except JevError:
            pass

    # OpenRouter 路的列表是写死的，但 key 要过 auth/key 探测：mock urlopen 验两头
    def _fake_key_ok(req, timeout=None):
        seen["key_url"] = req.full_url
        assert req.headers["Authorization"] == "Bearer or-key"
        return io.BytesIO(b'{"data":{}}')

    with patch.object(urllib.request, "urlopen", _fake_key_ok):
        assert list_models("openrouter", "or-key") == [
            "~typesafe/jev-latest", "typesafe/jev-1.13"]
    assert seen["key_url"] == OPENROUTER_KEY_URL

    def _fake_key_rejected(req, timeout=None):
        raise urllib.error.HTTPError(OPENROUTER_KEY_URL, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"error":{"message":"bad key or-key"}}'))

    with patch.object(urllib.request, "urlopen", _fake_key_rejected):
        try:
            list_models("openrouter", "or-key")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert e.status is None and "密钥被拒" in str(e) and "or-key" not in str(e)

    # 401/403 以外的状态码要把响应体带出来（别提前 read 把流吃空）
    def _fake_key_429(req, timeout=None):
        raise urllib.error.HTTPError(OPENROUTER_KEY_URL, 429, "Too Many", {},
                                     io.BytesIO(b'{"error":{"message":"rate limited"}}'))

    with patch.object(urllib.request, "urlopen", _fake_key_429):
        try:
            list_models("openrouter", "or-key")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "HTTP 429" in str(e) and "rate limited" in str(e)

    assert redact_secrets("key=ts-key or-key") == "key=[REDACTED] [REDACTED]"
    print("jev_client ok")
