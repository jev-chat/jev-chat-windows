# -*- coding: utf-8 -*-
"""用合成数据预览 Qt 界面；不采集、不联网、不操作真实微信。

    python tools/preview_ui.py --state ready
    python tools/preview_ui.py --state ready --screenshot docs/ui_home.png

演示设置只保存在内存，不读取真实密钥，也不修改环境变量或 config.json。
「获取模型」按钮也走得通：两个列模型的接口都被换成了本地假列表，全程不联网。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from app import settings


_STATES = ("ready", "waiting", "loading", "error", "setup", "settings", "paused", "debug")

# 调试视图预览用的真微信截图（只读进内存，不改不存）；没有就退一张空画面
_FRAME = Path("/private/tmp/claude-501/-Users-lpitiless-Documents-project-wechatjev"
              "/26954c2b-b4b9-432e-bad7-0d0b803e4309/images/9.png")
_AREA = (433, 152, 1298, 767)  # 那张图里的消息区，头部从 y=40 起
# 消息区裁剪坐标的框：前面这些是真跑一遍 OCR 得到的，灰字/小字那两个是手摆的，凑齐六种颜色
_BOXES = [
    (83, 99, 156, 122, "name", "Asterlion"),
    (26, 131, 70, 146, "image", "借仲夏夜之梦"),
    (99, 136, 143, 162, "her", "难绷"),
    (83, 194, 156, 218, "name", "Asterlion"),
    (101, 233, 315, 256, "her", "怎么识别到仲夏夜之梦的（"),
    (612, 30, 700, 44, "gray", "链接卡片的灰字"),
    (612, 50, 690, 62, "tiny", "表情包里的小字"),
    (633, 298, 760, 328, "me", "好像识别头像了"),
    (685, 368, 762, 399, "me", "笑死我了"),
    (85, 432, 157, 454, "name", "Asterlion"),
    (27, 464, 70, 478, "image", "借仲夏夜之梦"),
    (99, 467, 159, 493, "her", "还真是"),
    (98, 563, 160, 592, "her", "哈哈哈"),
]
_LINES = [("her", "Asterlion", "难绷"), ("her", "Asterlion", "怎么识别到仲夏夜之梦的（"),
          ("me", None, "好像识别头像了"), ("me", None, "笑死我了"),
          ("her", "Asterlion", "还真是"), ("her", "Asterlion", "哈哈哈")]


def _debug_packet():
    """合成一份子进程会发的调试包。QImage 读 PNG 进内存取 RGB 裸字节（行有 4 字节对齐，按行裁）。"""
    import time

    from PySide6.QtGui import QImage

    img = QImage(str(_FRAME)) if _FRAME.exists() else QImage()
    if img.isNull():
        img = QImage(1303, 979, QImage.Format_RGB888)
        img.fill(0x202524)
    img = img.convertToFormat(QImage.Format_RGB888)
    w, h = img.width(), img.height()
    rgb = b"".join(bytes(img.constScanLine(y))[:w * 3] for y in range(h))
    return {"w": w, "h": h, "rgb": rgb, "scale": 1, "area": _AREA, "pane_top": 40,
            "title": "白金搬砖小分队", "boxes": _BOXES, "lines": _LINES,
            "ocr_ms": 261, "ts": time.time()}

_CHAT = "白金搬砖小分队"  # 演示里「微信当前开着的」会话：用群聊，回复对象那一行才看得见
# (会话, 谁, 内容, 群里的发言人, 时间)：两个会话，下拉框里都能看到
_MESSAGES = (
    ("白金搬砖小分队", "her", "周末有人去爬山吗", "阿杰", "09:12"),
    ("白金搬砖小分队", "me", "我有空，几点集合？", "", "09:15"),
    ("白金搬砖小分队", "her", "我也去，带上我一个", "陈与小金", "09:15"),
    ("白金搬砖小分队", "her", "八点地铁口见，记得带水", "阿杰", "09:16"),
    (_CHAT, "me", "有空呀，还是上次那家？", "", "18:43"),
    (_CHAT, "her", "好呀！六点见怎么样？我好久没吃了 😋", "", "18:43"),
)
_GROUP = "白金搬砖小分队"
_SENDERS = ("阿杰", "陈与小金")  # 最近说话的排最前，跟 main.py 那边一个口径

_RESULT = {
    "candidates": [
        "周六六点没问题，上次那家见～",
        "可以呀，周六六点在上次那家见！我也有点馋了 😋",
        "好呀，就周六六点！需要我先订个位吗？",
    ],
    # 推荐故意放在第二项，方便检查视觉排序和按钮对应关系。
    "best_index": 1,
    "best_reply": "可以呀，周六六点在上次那家见！我也有点馋了 😋",
    "scores": [0.21, 0.66, 0.13],
    "answers": {
        "literal_question": {"type": "noul", "noul": 0.98},
        "true_intent": {"type": "choice", "choice": "casual_chat"},
        "danger_level": {"type": "score", "score": 0},
        "should_reply_now": {"type": "noul", "noul": 0.96},
        "best_action": {"type": "choice", "choice": "make_plan"},
        "she_needs": {"type": "choice", "choice": "action"},
        "tension_resolved": {"type": "noul", "noul": 0.99},
        "best_reply": {
            "type": "choice", "choice": "reply_b",
            "probabilities": {"reply_a": 0.21, "reply_b": 0.66, "reply_c": 0.13},
        },
    },
    "usage": {},
    "reply_to": "阿杰",  # 跟 _SENDERS[0] 一致，让「回复给 …」那行在演示里看得见
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用合成聊天预览 Qt UI；绝不采集、联网或填入真实微信。"
    )
    parser.add_argument("--state", choices=_STATES, default="ready", help="预览界面状态")
    parser.add_argument("--screenshot", metavar="PATH", help="将演示界面保存为 PNG 后退出（合成数据，不含微信内容）")
    args = parser.parse_args()
    target = Path(args.screenshot).expanduser() if args.screenshot else None

    # 演示里：判断走 OpenRouter，起草走 DeepSeek 官网；全程就两把 key，都当「已配置」
    configured = "" if args.state == "setup" else "demo-key"
    demo_settings = {"relationship": "friends", "context": 10,
                     "jev_key": configured, "llm_key": configured,
                     "jev_provider": "openrouter", "jev_model": "typesafe/jev-1.13",
                     "draft_provider": "deepseek", "draft_model": "deepseek-flash",
                     "draft_base_url": "", "reply_target": True,
                     "style": "话少，基本不用标点，急了才发感叹号", "thinking": False,
                     "check_update": True, "debug_view": args.state == "debug"}

    def save_demo_settings(relationship_text=None, context_n=None, *, jev_provider_text=None,
                           jev_key_text=None, jev_model_text=None, draft_provider_text=None,
                           llm_key_text=None, draft_model_text=None, draft_base_url_text=None,
                           reply_target_on=None, style_text=None, thinking_on=None,
                           check_update_on=None, debug_view_on=None):
        if relationship_text:
            demo_settings["relationship"] = relationship_text
        if context_n is not None:
            demo_settings["context"] = context_n
        for name, value in (("jev_provider", jev_provider_text), ("jev_model", jev_model_text),
                            ("draft_provider", draft_provider_text), ("draft_model", draft_model_text),
                            ("draft_base_url", draft_base_url_text), ("style", style_text)):
            if value is not None:
                demo_settings[name] = value
        for name, key in (("jev_key", jev_key_text), ("llm_key", llm_key_text)):
            if key:
                demo_settings[name] = key
        for name, value in (("reply_target", reply_target_on), ("thinking", thinking_on),
                            ("check_update", check_update_on), ("debug_view", debug_view_on)):
            if value is not None:
                demo_settings[name] = bool(value)

    def fake_jev_models(provider, key, timeout=10):
        """演示不联网：给一小撮假模型，让「获取模型」按钮在本地也走得通。"""
        if provider == "openrouter":
            return ["typesafe/jev-1.13"]
        if provider == "opencode":
            return ["jev-1.13", "jev-1.13-free"]
        return ["jev-1.13.0", "jev-latest", "jev-preview"]

    def fake_llm_models(protocol, base_url, api_key, timeout=10):
        return {"anthropic": ["claude-demo-4", "claude-demo-4-mini"],
                "gemini": ["gemini-demo-pro", "gemini-demo-flash"]}.get(
            protocol, ["deepseek-flash", "deepseek-reasoner", "demo-model-a", "demo-model-b"])

    # 在创建 Overlay 前替换设置接口，整个事件循环期间都保持隔离。
    with patch("core.jev_client.list_models", fake_jev_models), patch(
            "core.llm.list_models", fake_llm_models), patch.multiple(
        settings,
        has_key=lambda: bool(demo_settings["jev_key"]),
        has_jev_key=lambda: bool(demo_settings["jev_key"]),
        has_llm_key=lambda: bool(demo_settings["llm_key"]),
        jev_key=lambda: demo_settings["jev_key"],
        llm_key=lambda: demo_settings["llm_key"],
        relationship=lambda: demo_settings["relationship"],
        context=lambda: demo_settings["context"],
        jev_provider=lambda: demo_settings["jev_provider"],
        jev_model=lambda: demo_settings["jev_model"],
        draft_provider=lambda: demo_settings["draft_provider"],
        draft_model=lambda: demo_settings["draft_model"],
        draft_base_url=lambda: demo_settings["draft_base_url"],
        reply_target=lambda: demo_settings["reply_target"],
        style=lambda: demo_settings["style"],
        thinking=lambda: demo_settings["thinking"],
        check_update=lambda: demo_settings["check_update"],
        debug_view=lambda: demo_settings["debug_view"],
        save=save_demo_settings,
    ):
        from PySide6.QtCore import QTimer
        from app.overlay import Overlay

        def simulate_fill(text):
            # 等 Overlay 自身的点击反馈结束后，再显示明确的演示提示。
            QTimer.singleShot(0, lambda: ov.set_status(
                f"演示模式：已模拟填入「{text}」；未操作微信。", kind="success"
            ))

        # 只有当前会话有结果，切到另一个会话就是空态——跟真实情况一致
        ov = Overlay(on_fill=simulate_fill, result_of=lambda t: _RESULT if t == _CHAT else None)
        ov.win.setWindowTitle("JevChat-Windows · 界面演示（合成数据）")
        shot = ov.win  # 截图截哪个窗口；调试预览截调试窗

        if args.state == "debug":
            from app.debugwin import DebugWindow

            dbg = DebugWindow(on_close=lambda: ov.set_debug_switch(False))
            dbg.setWindowTitle("识别调试 · 界面演示（合成数据）")
            dbg.show_packet(_debug_packet())
            dbg.show()
            shot = dbg
        elif args.state == "setup":
            ov.set_status("演示模式：请填写示例密钥，设置仅保存在本次预览内。", kind="warning")
            ov.open_settings()
        elif args.state == "waiting":
            ov.set_status("演示模式：等待对方的新消息；当前未连接微信。")
        else:
            for chat, who, text, name, timestamp in _MESSAGES:
                ov.log_message(who, text, name, timestamp=timestamp, chat=chat)
            ov.set_targets(_GROUP, _SENDERS, _SENDERS[0])  # 群聊才有回复对象这一行
            ov.set_chat(_CHAT)
            ov.show(_RESULT)
            ov.set_status("演示模式：已生成 3 条建议，点击填入仅模拟操作。", kind="success")
            ov.set_update("9.9.9", "https://github.com/jev-chat/jev-chat-windows/releases/latest")
            if args.state == "loading":
                ov.set_busy(True)
                ov.set_status("演示模式：正在为最新消息生成建议…", kind="busy")
            elif args.state == "error":
                ov.set_busy(True)
                ov.set_status("演示模式：分析失败，请检查网络和密钥，等待下一条消息后重试。", kind="error")
            elif args.state == "settings":
                ov.open_settings()
            elif args.state == "paused":
                ov.set_capture(False)

        exit_code = 0
        if target is not None:
            def save_screenshot():
                nonlocal exit_code
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not shot.grab().save(str(target), "PNG"):
                        raise OSError(f"无法保存截图：{target}")
                    print(f"已保存合成界面截图：{target}")
                except OSError as exc:
                    print(str(exc))
                    exit_code = 1
                finally:
                    ov.app.quit()

            QTimer.singleShot(500, save_screenshot)
        ov.run()
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
