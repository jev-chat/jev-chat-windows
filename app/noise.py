# -*- coding: utf-8 -*-
"""群聊系统通知识别：纯 stdlib，不 import 任何重模块——悬浮窗（overlay）也要用它做兜底过滤，
而 app.ocr 模块级就拉着 RapidOCR，从那儿 import 会把 OCR 引擎拖进主进程。"""
import re

# 群聊系统通知的形状。**整行匹配**（先剥掉开头的引用名）：
# 系统通知整行就是通知本身；真人聊天里"聊到"这些词（「他刚才撤回了一条消息」）不会整行命中，不误杀。
_SYSTEM_NOISE = re.compile(
    r"通过扫描.{0,24}分享的二维码(加入|进入)了?群聊"
    r"|(?:你)?邀请.{1,24}(加入|进入)了?群聊"
    r"|加入了群聊"
    r"|退出群聊"
    r"|被.{1,20}移出群聊"
    r"|移出了群聊"
    r"|撤回了一条消息"
    r"|以上是打招呼的内容"
    r"|以下为新消息"
    r"|开启了朋友圈权限"
    r"|拍了拍.{0,12}"
)
_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}$")  # 时间戳分隔行
_DATE_ONLY = re.compile(r"^\d{1,4}[-/.]\d{1,2}([-/.]\d{1,4})?$")  # 日期分隔行（09/08、2025-09-23）
_LEADING_QUOTE = re.compile(r'^(?:["“」】][^"“「【】]{1,24}["”」】]|[「【][^」】]{1,24}[」】])\s*')
# 通知动词：人名（带不带引号都有）后面紧跟这些词的，开头那截就是人名，不是消息内容
_NOISE_VERB = re.compile(r"^(?:通过扫描|邀请|撤回|退出|移出|被|加入|拍了拍)")
_LEADING_NAME = re.compile(r"^[^，。！？：；""'\s]{1,12}")


def is_system_noise(text: str) -> bool:
    """这行是不是群聊系统通知/时间戳（不是任何人说的话）。设置里的「过滤系统通知」开关用它。
    先去掉全部空白（OCR 会在字间插空格：『加 入群聊』『撤回 了一条消息』，不处理就漏），
    再剥掉开头的人名引用（"小白"退出群聊），最后要求**整行**命中通知短语——
    只用子串搜索会把聊到这些词的真人消息一起误杀。"""
    t = re.sub(r"\s+", "", str(text or ""))
    if not t:
        return False
    if _TIME_ONLY.match(t) or _DATE_ONLY.match(t):
        return True
    while True:  # 剥引号人名："小白"退出群聊
        stripped = _LEADING_QUOTE.sub("", t)
        if stripped == t:
            break
        t = stripped
    m = _LEADING_NAME.match(t)  # 剥裸人名：赵凝神"通过扫描"…（人名不一定带引号）
    if m and _NOISE_VERB.match(t[m.end():]):
        t = t[m.end():]
    return bool(_SYSTEM_NOISE.fullmatch(t))
