# -*- coding: utf-8 -*-
"""Per-chat-app profile: which window to capture, which OCR engine, which bubble is mine.

Everything that used to be hard-coded for WeChat lives here, so adding an app is one entry.
The pixel rules stay where they were proven (see ocr.who_said); this file only names them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np


def _wechat_me(bg: np.ndarray) -> bool:
    """WeChat's own bubble is green: G clearly above both R and B."""
    return bool(bg[1] > bg[0] + 40 and bg[1] > bg[2] + 40)


def _kakao_me(bg: np.ndarray) -> bool:
    """KakaoTalk's own bubble is #FEE500: R and G high, B low. Measured on a live window;
    the chat ground is #BACEE0 and the other side is #FFFFFF, so both stay clear of this."""
    return bool(bg[0] > 200 and bg[1] > 180 and bg[2] < 120)


def _kakao_notice_rows(chat: np.ndarray) -> int:
    """KakaoTalk 群聊置顶公告：一张几乎铺满面板宽度的白卡片，贴在消息区顶上。
    气泡最宽也就六七成宽，所以「整行白色占宽度 80% 以上」只会是公告卡。
    返回要跳过的行数（没有公告就是 0）。实测：群聊 115~146 和 167~230 行命中，单聊一行都没有。"""
    top = int(chat.shape[0] * 0.35)   # 只在顶部找：底下再宽的白也不是公告
    white = (np.abs(chat[:top].astype(int) - 255).sum(axis=2) < 30).mean(axis=1)
    rows = np.flatnonzero(white > 0.8)
    if rows.size == 0 or rows[0] > chat.shape[0] * 0.2:
        return 0
    return int(rows[-1]) + 1


@dataclass(frozen=True)
class ChatApp:
    key: str
    label: str
    exes: tuple[str, ...]        # process names, lowercase
    main_title: str              # preferred top-level window title; "" = pick the biggest window
    skip_titles: tuple[str, ...]  # windows that are never a conversation (roster, toasts)
    ocr: str                     # "rapidocr" (zh/en) or "windows" (Windows.Media.Ocr, ko/ja/...)
    ocr_lang: str                # BCP-47 tag for the "windows" backend; "" = user profile order
    join: str                    # how OCR fragments inside one bubble are glued
    is_me: Callable[[np.ndarray], bool]
    trim_top: Callable[[np.ndarray], int] = lambda chat: 0  # rows to skip (pinned notice, ...)


WECHAT = ChatApp("wechat", "微信", ("weixin.exe", "wechat.exe"), "微信", (),
                 "rapidocr", "", "", _wechat_me)
# KakaoTalk opens one window per conversation, so the roster ("카카오톡") is never the target;
# the biggest remaining window is the chat that is actually being read.
# ocr_lang="ko" 是必须的：只按用户首选语言挑识别器，系统首选是中文或英文时会选错。
KAKAOTALK = ChatApp("kakaotalk", "카카오톡", ("kakaotalk.exe",), "", ("카카오톡", ""),
                    "windows", "ko", " ", _kakao_me, _kakao_notice_rows)

APPS = {a.key: a for a in (WECHAT, KAKAOTALK)}
DEFAULT = WECHAT


def by_exe(exe: str) -> ChatApp | None:
    return next((a for a in APPS.values() if exe in a.exes), None)


def get(key: str | None) -> ChatApp:
    return APPS.get(key or "", DEFAULT)


if __name__ == "__main__":  # 自测：颜色规则用实测像素锁住，改错了当场炸
    assert by_exe("kakaotalk.exe") is KAKAOTALK and by_exe("weixin.exe") is WECHAT
    assert by_exe("chrome.exe") is None and get(None) is DEFAULT and get("kakaotalk") is KAKAOTALK
    assert KAKAOTALK.ocr_lang == "ko" and WECHAT.ocr_lang == ""
    kakao_bubble, kakao_other, kakao_ground = (254, 229, 0), (255, 255, 255), (186, 206, 224)
    assert KAKAOTALK.is_me(np.array(kakao_bubble))
    assert not KAKAOTALK.is_me(np.array(kakao_other))
    assert not KAKAOTALK.is_me(np.array(kakao_ground))
    assert WECHAT.is_me(np.array((149, 236, 105))) and not WECHAT.is_me(np.array(kakao_bubble))
    pane = np.full((400, 300, 3), kakao_ground, np.uint8)
    assert KAKAOTALK.trim_top(pane) == 0 and WECHAT.trim_top(pane) == 0   # 공고 없는 화면
    pane[20:60] = 255                      # 폭을 꽉 채운 흰 카드 = 공지
    assert KAKAOTALK.trim_top(pane) == 60
    pane2 = np.full((400, 300, 3), kakao_ground, np.uint8)
    pane2[20:60, :150] = 255               # 반만 채운 흰색 = 그냥 기포
    assert KAKAOTALK.trim_top(pane2) == 0
    print("chatapps ok")
