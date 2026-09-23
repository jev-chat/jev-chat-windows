# -*- coding: utf-8 -*-
"""读取并选择随程序发布的关系/场景 Markdown 规则。"""
from __future__ import annotations

import os
import sys


_RELATION_FILES = {
    "romantic partners": "girlfriend.md",
    "romantic interest": "pursuing.md",
    "ambiguous romantic interest": "ambiguous.md",
    "friends": "friend.md",
    "colleagues": "colleague.md",
    "family": "family.md",
}
_SCENE_FILES = {
    "comfort": "comfort.md",
    "repair": "conflict.md",
    "intimacy": "daily_chat.md",
    "playful": "flirt.md",
    "action": "daily_chat.md",
    "close": "daily_chat.md",
    "daily_chat": "daily_chat.md",
    "physical_discomfort": "comfort.md",
    "fatigue": "comfort.md",
    "low_mood": "comfort.md",
    "stress_anxiety": "comfort.md",
    "hurt": "comfort.md",
    "anger": "conflict.md",
    "complaint": "conflict.md",
    "conflict": "conflict.md",
    "flirt": "flirt.md",
    "teasing": "flirt.md",
    "work": "daily_chat.md",
    "serious": "conflict.md",
}


def _rules_roots() -> list[str]:
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    # onedir 的 data 文件位于 _internal；源码运行时位于项目根目录。
    return [os.path.join(base, "rules"), os.path.join(base, "_internal", "rules")]


def _read(relative: str) -> str:
    for root in _rules_roots():
        path = os.path.join(root, relative)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
            if text:
                return text
        except OSError:
            continue
    return ""


def selected_rules(relationship: str, guidance: str = "") -> str:
    """按关系和判断出的当前场景选最相关的规则，避免每次把全部 MD 都塞给模型。"""
    relationship_file = _RELATION_FILES.get(str(relationship or "").strip())
    scene_file = next((filename for scene, filename in _SCENE_FILES.items()
                       if f"({scene})" in (guidance or "")), None)
    parts = []
    if relationship_file:
        text = _read(os.path.join("relationships", relationship_file))
        if text:
            parts.append(text)
    if scene_file and (not parts or scene_file != relationship_file):
        text = _read(os.path.join("scenes", scene_file))
        if text:
            parts.append(text)
    if not parts:
        text = _read("reply_strategy.md")
        if text:
            parts.append(text)
    return "\n\n---\n\n".join(parts)[:14000]
