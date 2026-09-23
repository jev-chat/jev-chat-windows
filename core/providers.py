# -*- coding: utf-8 -*-
"""两张来源表：判断模型 Jev / 起草语言模型。纯数据，不联网、不认 key。

表里只有协议、地址和默认模型，**绝不出现 key**（KICKOFF 硬约束 #6）——
key 一律由调用方从环境变量/注册表取了再传进来。协议具体怎么调见 core/llm.py。

全程只有两把 key：判断一把 JEV_API_KEY、起草一把 LLM_API_KEY，跟选哪家来源无关，
换来源就是换同一个槽里的值。
"""
from __future__ import annotations

from collections import namedtuple

OPENROUTER_BASE = "https://openrouter.ai/api/v1"  # OpenAI 兼容，列模型走它
# Jev 判断只有 OpenRouter 这条路要自己拼 HTTP：typesafe_sdk 把路径写死成 /v1/systemone，打不到这个地址
OPENROUTER_DECISIONS = "https://openrouter.ai/api/alpha/decisions"
TYPESAFE_BASE = "https://api.typesafe.ai"

JEV_ENV = "JEV_API_KEY"    # 判断那把，不管选 OpenRouter 还是 TypeSafe
LLM_ENV = "LLM_API_KEY"    # 起草那把，不管选哪家语言模型
# 迁移：老版本按来源各存一个变量。新变量空着、老变量有值就先用老的（保存时抄进新的）
LEGACY = {JEV_ENV: "OPENROUTER_API_KEY", LLM_ENV: "DEEPSEEK_API_KEY"}

_Jev = namedtuple("_Jev", "name base default")
JEV_PROVIDERS = {
    "openrouter": _Jev("OpenRouter", OPENROUTER_DECISIONS, "typesafe/jev-1.13"),
    "typesafe": _Jev("TypeSafe 直连", TYPESAFE_BASE, "jev-latest"),
    "custom": _Jev("自定义 · 同协议", "", ""),  # 自建/代理/镜像，同 Jev 私有协议，Base URL 必填
}
# Jev 判断要在 Base URL 行里露地址的来源：设置页常驻这一行，切来源自动填表里的默认值，
# 也能手改（typesafe 填代理、custom 填自建、openrouter 直接是 /decisions 完整地址）
JEV_BASE = ("typesafe", "custom")

# protocol ∈ {openai, anthropic, gemini}：决定 core/llm.py 用哪个官方 SDK
# base 空 = 用 SDK 自带的默认地址（gemini），或者等用户自己填（自定义来源）
# default 空 = 这家没有钦点的默认模型，用户得「获取模型」自己挑一个
# extra：OpenAI 协议下开/关思考模式要额外带的 body 字段，各家不一样；
#        anthropic / gemini 的思考开关是协议自带的参数，由 llm.py 直接处理，这里给空
_Draft = namedtuple("_Draft", "name protocol base default extra")
_NONE = lambda on: {}  # noqa: E731 —— 没有思考开关的来源
DRAFT_PROVIDERS = {  # 第一个就是默认：DeepSeek 官网直连
    "deepseek": _Draft("DeepSeek 官网", "openai", "https://api.deepseek.com", "deepseek-flash",
                       lambda on: {"thinking": {"type": "enabled" if on else "disabled"}}),
    "openrouter": _Draft("OpenRouter", "openai", OPENROUTER_BASE,
                         "deepseek/deepseek-v4.1-flash", lambda on: {"reasoning": {"enabled": on}}),
    "openai": _Draft("OpenAI", "openai", "https://api.openai.com/v1", "", _NONE),
    "moonshot": _Draft("Moonshot (Kimi)", "openai", "https://api.moonshot.cn/v1", "", _NONE),
    "zhipu": _Draft("智谱 GLM", "openai", "https://open.bigmodel.cn/api/paas/v4", "", _NONE),
    "dashscope": _Draft("通义千问", "openai",
                        "https://dashscope.aliyuncs.com/compatible-mode/v1", "", _NONE),
    "siliconflow": _Draft("硅基流动", "openai", "https://api.siliconflow.cn/v1", "", _NONE),
    "anthropic": _Draft("Anthropic", "anthropic", "https://api.anthropic.com", "", _NONE),
    "gemini": _Draft("Google Gemini", "gemini", "", "", _NONE),
    "custom_openai": _Draft("自定义 · OpenAI 兼容", "openai", "", "", _NONE),
    "custom_anthropic": _Draft("自定义 · Anthropic 兼容", "anthropic", "", "", _NONE),
}

# 这两个来源没有固定地址，设置页要多露一行 Base URL 出来
CUSTOM = ("custom_openai", "custom_anthropic")
# 起草时认思考开关的来源，设置页那句提示照着这里写
THINKING = ("DeepSeek", "OpenRouter", "Anthropic", "Gemini")
# 所有可能存 key 的环境变量（新两把 + 两个老名字），脱敏时一次全过一遍（jev_client.redact_secrets）
ENV_VARS = sorted({JEV_ENV, LLM_ENV, *LEGACY.values()})


if __name__ == "__main__":
    # ponytail: 纯数据，只查几条不变式——协议打错字、自定义来源漏配 Base URL、思考字段写反最容易出。
    assert {p.protocol for p in DRAFT_PROVIDERS.values()} == {"openai", "anthropic", "gemini"}
    assert all(p.base or key in CUSTOM or p.protocol == "gemini"
               for key, p in DRAFT_PROVIDERS.items())
    assert all(not DRAFT_PROVIDERS[key].base for key in CUSTOM)
    assert next(iter(DRAFT_PROVIDERS)) == "deepseek"  # 默认就是列表第一个
    assert DRAFT_PROVIDERS["deepseek"].extra(True) == {"thinking": {"type": "enabled"}}
    assert DRAFT_PROVIDERS["deepseek"].extra(False) == {"thinking": {"type": "disabled"}}
    assert DRAFT_PROVIDERS["openrouter"].extra(True) == {"reasoning": {"enabled": True}}
    assert DRAFT_PROVIDERS["moonshot"].extra(True) == {}
    # 全程只有两把 key，脱敏还得管老名字
    assert ENV_VARS == ["DEEPSEEK_API_KEY", "JEV_API_KEY", "LLM_API_KEY", "OPENROUTER_API_KEY"]
    print("providers ok")
