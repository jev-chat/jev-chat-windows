# -*- coding: utf-8 -*-
"""三种接口协议的薄适配层，一律走官方 SDK：
OpenAI Chat Completions（`openai`）/ Anthropic Messages（`anthropic`）/ Gemini（`google-genai`）。

只做两件事：chat() 发一轮对话拿文本，list_models() 列模型。base_url / key / model 全由调用方传，
这里不认识任何「来源」——来源表在 core/providers.py。出错一律转成 JevError，消息过脱敏。

SDK 都在函数里 import：桌面端一次只用到其中一家，启动时没必要把另外两家的依赖也拖起来。
"""
from __future__ import annotations

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .jev_client import JevError, _fail
except ImportError:
    from jev_client import JevError, _fail

# Anthropic 开思考模式时的预算：起草三句话用不上更多；max_tokens 必须比它大，下面会兜住
_THINK_BUDGET = 2048
# 阶跃 step-5 这类先写 reasoning 再写 content；额度不够时 content 是空串，解析就会报「候选: ''」
_EMPTY_CONTENT_RETRY = 8192


def _assistant_text(msg) -> str:
    """assistant 正文。content 可能是 str、None，或 SDK 拆成的 parts 列表。不把 reasoning 当回复。"""
    content = getattr(msg, "content", None)
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                chunks.append(str(part.get("text") or ""))
            else:
                chunks.append(str(getattr(part, "text", "") or ""))
        joined = "".join(chunks).strip()
        if joined:
            return joined
    return ""


def _turns(user_turns: list[str], assistant: str = "assistant") -> list[dict]:
    """[user, assistant, user, …] 交替；第一条和最后一条都是用户。"""
    return [{"role": assistant if i % 2 else "user", "content": text}
            for i, text in enumerate(user_turns)]


def chat(protocol: str, base_url: str | None, api_key: str, model: str, system: str,
         user_turns: list[str], *, temperature: float = 1.0, max_tokens: int = 400,
         thinking: bool = False, extra_body: dict | None = None,
         headers: dict | None = None, timeout: float = 30) -> str:
    """发一轮对话，返回模型输出的纯文本。

    user_turns: 用户/助手交替的文本，奇数条，首尾都是用户说的（追问补齐候选就是 3 条）。
    thinking: 思考模式。OpenAI 协议没有统一字段，各家自己的开关由调用方经 extra_body 带进来；
              anthropic / gemini 是协议自带的参数，这里直接处理。
    """
    if protocol == "anthropic":
        return _anthropic(base_url, api_key, model, system, user_turns,
                          temperature, max_tokens, thinking, timeout)
    if protocol == "gemini":
        return _gemini(base_url, api_key, model, system, user_turns,
                       temperature, max_tokens, thinking, timeout)
    return _openai(base_url, api_key, model, system, user_turns,
                   temperature, max_tokens, extra_body, headers, timeout)


def _openai(base_url, api_key, model, system, user_turns, temperature, max_tokens,
            extra_body, headers, timeout) -> str:
    import openai

    text = ""
    finish = ""
    try:
        client = openai.OpenAI(base_url=base_url or None, api_key=api_key,
                               timeout=timeout, max_retries=2,
                               **({"default_headers": headers} if headers else {}))
        messages = [{"role": "system", "content": system}] + _turns(user_turns)
        extra = {"extra_body": extra_body} if extra_body else {}

        def once(tokens):
            return client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
                max_tokens=tokens, stream=False,  # DeepSeek 要显式关；别家无所谓
                **extra)

        resp = once(max_tokens)
        finish = getattr(resp.choices[0], "finish_reason", "") or ""
        text = _assistant_text(resp.choices[0].message)
        if not text and max_tokens < _EMPTY_CONTENT_RETRY:
            resp = once(_EMPTY_CONTENT_RETRY)
            finish = getattr(resp.choices[0], "finish_reason", "") or finish
            text = _assistant_text(resp.choices[0].message)
    except Exception as exc:
        _fail(exc, "起草")
    if not text:
        raise JevError(
            "起草模型只返回了思考、没有正文"
            + (f"（finish={finish}）" if finish else "")
            + "。阶跃 step-5 这类推理模型常这样，可换 step-3.5-flash 或把思考关掉后再试。"
        )
    return text


def _anthropic(base_url, api_key, model, system, user_turns, temperature, max_tokens,
               thinking, timeout) -> str:
    import anthropic

    extra = {}
    if thinking:
        extra["thinking"] = {"type": "enabled", "budget_tokens": _THINK_BUDGET}
        max_tokens = max(max_tokens, _THINK_BUDGET + 1024)  # max_tokens 得装得下思考 + 正文
    try:
        client = anthropic.Anthropic(base_url=base_url or None, api_key=api_key,
                                     timeout=timeout, max_retries=2)
        # anthropic>=1.8 的 Messages.create 已无 temperature，采样走模型默认
        message = client.messages.create(model=model, system=system,
                                         messages=_turns(user_turns), max_tokens=max_tokens,
                                         **extra)
    except Exception as exc:
        _fail(exc, "起草")
    # 开了思考的话前面还有 thinking 块，只取文本块
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


def _gemini_client(base_url, api_key, timeout):
    from google import genai
    from google.genai import types

    # HttpOptions.timeout 的单位是毫秒，不是秒
    options = types.HttpOptions(timeout=int(timeout * 1000))
    if base_url:
        options.base_url = base_url
    return genai.Client(api_key=api_key, http_options=options), types


def _gemini(base_url, api_key, model, system, user_turns, temperature, max_tokens,
            thinking, timeout) -> str:
    try:
        client, types = _gemini_client(base_url, api_key, timeout)
        config = types.GenerateContentConfig(
            system_instruction=system, temperature=temperature, max_output_tokens=max_tokens,
            # thinking_budget=0 才是真的关掉；不传是让模型自己定（等于开着）
            thinking_config=None if thinking else types.ThinkingConfig(thinking_budget=0))
        contents = [types.Content(role=m["role"], parts=[types.Part(text=m["content"])])
                    for m in _turns(user_turns, assistant="model")]  # Gemini 那边助手叫 model
        resp = client.models.generate_content(model=model, contents=contents, config=config)
    except Exception as exc:
        _fail(exc, "起草")
    return resp.text or ""


def list_models(protocol: str, base_url: str | None, api_key: str,
                timeout: float = 10, headers: dict | None = None) -> list[str]:
    """某个地址上能用的模型 id，去重排序。失败抛 JevError，消息直接显示在设置页上。"""
    if protocol == "anthropic":
        import anthropic

        try:
            client = anthropic.Anthropic(base_url=base_url or None, api_key=api_key,
                                         timeout=timeout, max_retries=1)
            ids = [m.id for m in client.models.list()]
        except Exception as exc:
            _fail(exc, "取模型列表")
    elif protocol == "gemini":
        try:
            client, _ = _gemini_client(base_url, api_key, timeout)
            # 名字带 models/ 前缀，调用时用不上，剥掉
            ids = [str(m.name).removeprefix("models/") for m in client.models.list() if m.name]
        except Exception as exc:
            _fail(exc, "取模型列表")
    else:
        import openai

        try:
            client = openai.OpenAI(base_url=base_url or None, api_key=api_key,
                                   timeout=timeout, max_retries=1,
                                   **({"default_headers": headers} if headers else {}))
            ids = [m.id for m in client.models.list()]
        except Exception as exc:
            _fail(exc, "取模型列表")
    return sorted(set(ids))


if __name__ == "__main__":
    # ponytail: 不联网。在 SDK 边界上把客户端换成假的，只验「喂给 SDK 的参数对不对」——
    # 各家的字段名和思考开关是这层唯一会坏的东西。config 对象仍用真类型，字段名写错会当场炸。
    import os
    import types as _t

    import anthropic
    import openai
    from google import genai

    seen: dict = {}

    def _fake(kind):
        """记下构造参数和调用参数的假客户端。"""
        def make(**kw):
            seen[kind + ".init"] = kw
            def call(**k):
                seen[kind + ".call"] = k
                if kind == "openai":
                    return _t.SimpleNamespace(choices=[_t.SimpleNamespace(
                        message=_t.SimpleNamespace(content='["甲","乙","丙"]'))])
                if kind == "anthropic":
                    return _t.SimpleNamespace(content=[
                        _t.SimpleNamespace(type="thinking", thinking="…"),
                        _t.SimpleNamespace(type="text", text="嗯")])
                return _t.SimpleNamespace(text="嗯")
            listing = {"openai": [_t.SimpleNamespace(id="b"), _t.SimpleNamespace(id="a"),
                                  _t.SimpleNamespace(id="a")],
                       "anthropic": [_t.SimpleNamespace(id="claude-y"), _t.SimpleNamespace(id="claude-x")],
                       "gemini": [_t.SimpleNamespace(name="models/gemini-2"),
                                  _t.SimpleNamespace(name="models/gemini-1")]}[kind]
            models = _t.SimpleNamespace(list=lambda **_: listing, generate_content=call)
            return _t.SimpleNamespace(
                models=models, messages=_t.SimpleNamespace(create=call),
                chat=_t.SimpleNamespace(completions=_t.SimpleNamespace(create=call)))
        return make

    openai.OpenAI, anthropic.Anthropic, genai.Client = (
        _fake("openai"), _fake("anthropic"), _fake("gemini"))

    # OpenAI 协议：base_url / key / model / 思考字段都得原样到位
    out = chat("openai", "https://api.deepseek.com", "sk-ds", "deepseek-flash", "S", ["U"],
               temperature=1.2, max_tokens=400, extra_body={"thinking": {"type": "disabled"}})
    assert out == '["甲","乙","丙"]'
    assert _assistant_text(_t.SimpleNamespace(content='["甲","乙","丙"]')) == '["甲","乙","丙"]'
    assert _assistant_text(_t.SimpleNamespace(content=None, reasoning="think")) == ""
    assert _assistant_text(_t.SimpleNamespace(content=[{"text": '["甲"]'}])) == '["甲"]'
    assert seen["openai.init"]["base_url"] == "https://api.deepseek.com"
    assert seen["openai.init"]["api_key"] == "sk-ds" and seen["openai.init"]["max_retries"] == 2
    assert seen["openai.call"]["model"] == "deepseek-flash"
    assert seen["openai.call"]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert seen["openai.call"]["temperature"] == 1.2 and seen["openai.call"]["max_tokens"] == 400
    assert seen["openai.call"]["messages"] == [
        {"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    # 没有思考开关的来源（extra_body 空）就不该出现这个字段
    chat("openai", "https://api.moonshot.cn/v1", "k", "kimi", "S", ["U"], extra_body={})
    assert "extra_body" not in seen["openai.call"]
    # 来源要求的额外头（OpenCode Go）要进 SDK，别的来源不带
    chat("openai", "https://opencode.ai/zen/go/v1", "k", "deepseek-v4.1-flash", "S", ["U"],
         headers={"x-opencode-session": "sid", "User-Agent": "jev-chat-windows"})
    assert seen["openai.init"]["default_headers"] == {
        "x-opencode-session": "sid", "User-Agent": "jev-chat-windows"}
    chat("openai", "https://api.deepseek.com", "k", "m", "S", ["U"])
    assert "default_headers" not in seen["openai.init"]
    # 追问补齐：user / assistant / user 三轮
    chat("openai", "", "k", "m", "S", ["U1", "A1", "U2"])
    assert [m["role"] for m in seen["openai.call"]["messages"]] == [
        "system", "user", "assistant", "user"]
    assert seen["openai.init"]["base_url"] is None  # 空 base_url = 用 SDK 默认地址

    # Anthropic：system 单独传，思考是协议自带参数；Messages.create 不再收 temperature
    assert chat("anthropic", "https://api.anthropic.com", "sk-an", "claude-x", "S", ["U"],
                temperature=1.2, max_tokens=400) == "嗯"
    assert seen["anthropic.call"]["system"] == "S" and "thinking" not in seen["anthropic.call"]
    assert seen["anthropic.call"]["messages"] == [{"role": "user", "content": "U"}]
    assert "temperature" not in seen["anthropic.call"]
    chat("anthropic", "", "k", "claude-x", "S", ["U"], temperature=1.2, max_tokens=400, thinking=True)
    assert seen["anthropic.call"]["thinking"] == {"type": "enabled", "budget_tokens": _THINK_BUDGET}
    assert "temperature" not in seen["anthropic.call"]
    assert seen["anthropic.call"]["max_tokens"] > _THINK_BUDGET

    # Gemini：助手那一轮叫 model；关思考 = thinking_budget 0，开 = 不传让模型自己定
    assert chat("gemini", "", "k", "gemini-2", "S", ["U1", "A1", "U2"], max_tokens=400) == "嗯"
    cfg = seen["gemini.call"]["config"]
    assert cfg.system_instruction == "S" and cfg.max_output_tokens == 400
    assert cfg.thinking_config.thinking_budget == 0
    assert [c.role for c in seen["gemini.call"]["contents"]] == ["user", "model", "user"]
    assert seen["gemini.call"]["contents"][0].parts[0].text == "U1"
    chat("gemini", "https://my.proxy", "k", "gemini-2", "S", ["U"], thinking=True)
    assert seen["gemini.call"]["config"].thinking_config is None
    assert seen["gemini.init"]["http_options"].base_url == "https://my.proxy"
    assert seen["gemini.init"]["http_options"].timeout == 30000  # 毫秒，不是秒

    # 列模型：去重排序；gemini 剥掉 models/ 前缀
    assert list_models("openai", "https://x/v1", "k") == ["a", "b"]
    assert "default_headers" not in seen["openai.init"]
    list_models("openai", "https://x/v1", "k", headers={"User-Agent": "jev-chat-windows"})
    assert seen["openai.init"]["default_headers"] == {"User-Agent": "jev-chat-windows"}
    assert list_models("anthropic", "", "k") == ["claude-x", "claude-y"]
    assert list_models("gemini", "", "k") == ["gemini-1", "gemini-2"]

    # 出错 → 一句人话的 JevError，带上状态码，不泄露 key
    os.environ["DEEPSEEK_API_KEY"] = "sk-secret"

    class _Boom(Exception):
        status_code = 401

    def _explode(**kw):
        raise _Boom("bad key sk-secret")

    openai.OpenAI = _explode
    try:
        chat("openai", "", "sk-secret", "m", "S", ["U"])
        raise SystemExit("应当抛错")
    except JevError as e:
        assert e.status == 401 and "密钥被拒" in str(e) and "sk-secret" not in str(e)
    print("llm ok")
