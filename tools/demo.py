# -*- coding: utf-8 -*-
"""端到端冒烟：截图里那段真实对话跑一遍完整链，打印判断 + 排好序的候选。

链路是三段式：Jev 判断（7 道题） → 带着判断起草 3 条 → Jev 排序，两次 Jev 调用。

全程只要两把 key：判断一把 JEV_API_KEY（OpenRouter 或 TypeSafe 的），起草一把 LLM_API_KEY。
判断选 Laya 本地（离线）则连 JEV_API_KEY 都不用。

    set JEV_API_KEY=...   &  set LLM_API_KEY=...    (Windows)
    export JEV_API_KEY=... && export LLM_API_KEY=...(mac/Linux)
    python tools/demo.py

默认：判断走 OpenRouter，起草走 DeepSeek 官网直连。换别家改下面两个常量
（可选的来源见 core/providers.py 的两张表）。
"""
from __future__ import annotations

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core.engine import analyze
from core.jev_client import JevError
from core.questions import guidance_text

MESSAGES = [
    ("her", "你今天是不是又忘了我跟你说过什么？"),
    ("me", "记得，你先别提示我，让我自己说。"),
    ("her", "那你说。"),
    ("me", "等一下，我想说完整一点。"),
    ("her", "你最好是。"),
]
RELATIONSHIP = "romantic partners"
PROVIDER = "deepseek"        # 起草来源，见 core.providers.DRAFT_PROVIDERS
JEV_PROVIDER = "openrouter"  # 判断来源：openrouter / typesafe / laya（本地，不要 key）


def fmt(name: str, ans: dict) -> str:
    t = ans.get("type")
    if t == "noul":
        return f"{name}: {ans.get('noul'):.2f}"
    if t == "choice":
        return f"{name}: {ans.get('choice')} (conf {ans.get('confidence'):.2f})"
    if t == "score":
        return f"{name}: {ans.get('score'):.1f}/9 (conf {ans.get('confidence'):.2f})"
    return f"{name}: {ans}"


def main() -> int:
    print("对话:")
    for w, t in MESSAGES:
        print(f"  {w}: {t}")
    try:
        r = analyze(MESSAGES, RELATIONSHIP, provider=PROVIDER, jev_provider=JEV_PROVIDER)
    except JevError as e:
        print(f"\n失败: {e}")
        return 1

    print("\n判断:")
    for name in ("literal_question", "true_intent", "danger_level",
                 "should_reply_now", "best_action", "she_needs", "tension_resolved"):
        if name in r["answers"]:
            print("  " + fmt(name, r["answers"][name]))

    block = guidance_text(r["answers"])  # 起草时喂进去的那张小抄
    if block:
        print("\n" + block)

    print("\n候选（Jev 排序，★ = 推荐）:")
    scores = r.get("scores")
    for i, c in enumerate(r["candidates"]):
        pct = f"  {scores[i]:.0%}" if scores else ""
        print(f"  {'★' if i == r['best_index'] else ' '} {c}{pct}")

    u = r["usage"]
    if u:
        print(f"\nusage: in={u.get('input_tokens')} out={u.get('output_tokens')} "
              f"cost=${u.get('cost')}")
    print("\n期望核对: true_intent≈confirm_you_care, best_action≈check_history, danger_level 中高档")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
