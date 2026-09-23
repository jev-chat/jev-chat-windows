# -*- coding: utf-8 -*-
"""父进程：只管界面。截图 + OCR 在 app/worker.py 的子进程里跑，队列里收新消息 →
冒出新的对方消息才调 engine → 悬浮窗给 3 条候选 → 人点「填入」。发送永远手动。静默期零调用。
上下文、结果、聊天记录都按会话名（子进程 OCR 头部标题得来）分开存，切会话不串味。

    pip install rapidocr-onnxruntime numpy windows-capture PySide6-Fluent-Widgets
两个模型（判断 Jev / 起草语言模型）的来源和 key 在独立设置页填写，不用改代码。IDE 里直接 Run。
"""
import ctypes
import ctypes.wintypes
import itertools
import multiprocessing
import os
import queue
import threading
import time
import traceback
from collections import deque

from PySide6.QtCore import QLockFile

from app import settings, update, worker
from app.capture import find_wechat_hwnd
from app.fill import fill
from app.noise import is_system_noise
from app.ocr import similar
from app.overlay import Overlay, judgment_text
from app.version import VERSION
from core.engine import analyze

# {会话名: {history, result, rev, target, senders}}：每个会话各自的上下文、上次结果和版本号，互不串味
# history 里是 [(who, text, name)]，engine 只认 her/me，name 是群里的发言人（单聊/自己说的是 None）；
# 只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定
# senders：这个群里发过言的人，去重、最近的排最前；target：用户挑的回复对象（None = 跟着最近那个走）
chats = {}
state = {"area": None, "busy": False, "rerun": None, "hwnd": None, "chat": "",
         "refresh_wait": None, "refresh_ts": 0.0, "vision": {}}  # vision: {会话: 最新消息区截图 JPEG}
results = queue.Queue()
update_result = queue.Queue()  # 独立小队列，别跟 results 的 (kind, r, title, revision) 形状搅在一起
cmd_q = multiprocessing.Queue()  # 发给采集子进程的指令（"refresh" = 强制重读当前画面）  # 独立小队列，别跟 results 的 (kind, r, title, revision) 形状搅在一起
_snap_count = 0  # 吸附跟随的节拍计数（tick 里 %5）
_find_count = 0  # 启动时没找到微信窗口的话，吸附这里隔几秒补找一次
_result_seq = itertools.count(1)  # 结果序号：浮层拿它判断内容变没变（id() 会被地址复用骗过）
u32 = ctypes.windll.user32
u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_uint]  # 64 位下句柄别截断


def chat_of(title):
    return chats.setdefault(title, {"history": deque(maxlen=60), "result": None, "rev": 0,
                                    "target": None, "senders": []})


def target_of(title):
    """这个会话现在的回复对象：用户挑过且人还在就用它，否则用最近说话的那个；单聊没有发言人 → None。"""
    chat = chat_of(title)
    if chat["target"] in chat["senders"]:
        return chat["target"]
    return chat["senders"][0] if chat["senders"] else None


def _finish_refresh(title):
    """拿到最新内容（或超时兜底）后，用当前会话历史再跑一轮分析。"""
    msgs = list(chat_of(title)["history"])
    if not any(m[0] == "her" for m in msgs):
        ov.set_status("当前会话还没有对方的消息，等对方说话后再试", "warning")
        return
    chat_of(title)["rev"] += 1  # 手动刷新也是新一轮：旧结果作废
    start_analyze(title, msgs)


def refresh_analysis():
    """手动重新生成：先让采集子进程对当前画面强制重跑一遍 OCR（拿到最新内容再生成），
    子进程回传后合并进历史再分析；4 秒没回传就按已有历史兜底。"""
    title = ov.current_chat()
    if not title:
        ov.set_status("还没有识别到会话，先在微信里点开一个聊天", "warning")
        return
    if state["busy"]:
        ov.set_status("正在生成中，稍等一下再试", "warning")
        return
    if not any(m[0] == "her" for m in chat_of(title)["history"]) and child is None:
        ov.set_status("当前会话还没有对方的消息，等对方说话后再试", "warning")
        return
    if child is not None:
        state["refresh_wait"] = title
        state["refresh_ts"] = time.time()
        cmd_q.put("refresh")
        ov.set_status("正在读取最新消息…", "busy")
    else:
        _finish_refresh(title)


def fill_reply(text):
    if state["hwnd"] is None:  # 子进程重开过，hwnd 可能换了，用最新的
        raise RuntimeError("未找到微信窗口，请确认微信已打开")
    if state["area"] is None:
        raise RuntimeError("微信输入区域尚不可用，请确认微信聊天窗口可见（不要最小化）")
    if settings.reply_target() and ov.at_prefix_enabled():
        target = target_of(ov.current_chat())  # 填进去的是界面上正看着的那个会话的对象
        if target:
            text = f"@{target} " + text  # 纯文本，微信不认成真正的 @，只是让群里看得出在跟谁说
    fill(state["hwnd"], state["area"], text)


def spawn_worker():
    """开一个采集子进程，它跟着 capture_on 走：置位=采集，清掉=暂停。"""
    p = multiprocessing.Process(target=worker.run,
                                args=(q, state["hwnd"], capture_on, debug_on, cmd_q), daemon=True)
    p.start()
    return p


def set_debug(on):
    """调试视图开关：开 → 开窗 + 置位（子进程这才开始送帧，一帧 2~3MB）；关 → 清掉 + 收窗。"""
    global dbg
    if not on:
        debug_on.clear()
        if dbg is not None:
            dbg.hide()
        return
    if dbg is None:
        from app.debugwin import DebugWindow

        dbg = DebugWindow(on_close=on_debug_closed)
    dbg.show()
    debug_on.set()


def on_debug_closed():
    """用户直接关了调试窗 = 把开关也关了，否则设置页显示开着但没窗。"""
    debug_on.clear()
    ov.set_debug_switch(False)
    settings.save(debug_view_on=False)


def on_toggle_capture(on):
    """标题栏开关。启动时没找到微信就没有子进程，这会儿再找一次，找到了才真开得起来。"""
    global child
    if not on:
        capture_on.clear()
        return
    if child is None:
        try:
            state["hwnd"] = find_wechat_hwnd()
        except RuntimeError:
            ov.set_capture(False, "未找到微信窗口，打开微信后再开启采集")
            return
        child = spawn_worker()
    capture_on.set()


def analyze_bg(msgs, title, revision, reply_to=None):
    """后台线程只跑网络调用，结果丢队列；UI 只在主线程的 tick 里动（Qt 不能跨线程碰）。"""
    try:
        results.put(("ok", analyze(msgs, settings.relationship(), context=settings.context(),
                                   model=settings.draft_model() or None,
                                   provider=settings.draft_provider(),
                                   base_url=settings.draft_base_url() or None,
                                   reply_to=reply_to, style=settings.style(),
                                   thinking=settings.thinking(),
                                   draft_extra=settings.draft_extra(),
                                   filter_noise=settings.filter_system_msgs(),
                                   vision_image=state["vision"].get(title),
                                   jev_provider=settings.jev_provider(),
                                   jev_model=settings.jev_model() or None,
                                   jev_base_url=settings.jev_base_url() or None),
                     title, revision))
    except Exception as e:
        results.put(("err", f"分析失败: {e}", title, revision))


def check_update_bg():
    """启动时后台查一次新版本，跟 analyze_bg 一个套路：网络调用在线程里，UI 只在 tick() 里动。"""
    r = update.check_latest(VERSION)
    if r:
        update_result.put(r)


def start_analyze(title, msgs):
    if not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        return
    if (settings.jev_provider() == "custom"
            and (not settings.jev_base_url() or not settings.jev_model())):
        ov.set_status("自定义判断来源要填 Base URL 并选一个模型，去设置里补上", "warning")
        return
    if not settings.has_llm_key():
        ov.set_status(f"起草来源 {settings.draft_provider_name()} 没填密钥，去设置里补上", "warning")
        return
    state["busy"] = True
    ov.set_busy(True)
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    threading.Thread(target=analyze_bg, args=(msgs, title, chat_of(title)["rev"], reply_to),
                     daemon=True).start()


def on_target_change(title, name):
    """用户挑了回复对象：记下来，这个会话里有对方的话就照新对象重跑一次。"""
    chat = chat_of(title)
    chat["target"] = name
    msgs = list(chat["history"])
    if not any(m[0] == "her" for m in msgs):
        return
    if state["busy"]:
        state["rerun"] = (title, msgs)
        ov.set_busy(True)
    else:
        start_analyze(title, msgs)


def drain():
    """把子进程队列里攒的东西全收掉。"""
    global child
    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            return
        kind = msg[0]
        if kind == "area":  # 只是窗口挪了位置，坐标跟着更新，别的什么都不用动
            state["area"] = msg[1]
            continue
        if kind == "chat":  # 微信切了会话，界面跟过去（用户正浏览别的会话时也跟，微信是准的）
            state["chat"] = msg[1]
            ov.set_chat(msg[1])
            continue
        if kind == "debug":  # 调试视图的一帧；窗口不在就直接丢掉
            if dbg is not None:
                dbg.show_packet(msg[1])
            continue
        if kind == "status":  # 单帧识别失败/报错，提示一下就好，别把正在跑的分析和已知坐标清掉
            ov.set_status(msg[1], "warning")
            ov.log(msg[1])
            continue
        if kind == "vision":  # 视觉开关开时的消息区截图：每个会话只留最新一张
            state["vision"][msg[1]] = msg[2]
            continue
        if kind == "refresh_lines":  # 「重新生成」的强制重读结果：合并进历史（按相似度去重）再生成
            _, title, rows, rect = msg
            state["area"] = rect
            chat = chat_of(title)
            for who, name, text in rows:
                if settings.filter_system_msgs() and is_system_noise(text):
                    continue
                if any(w == who and similar(t, text) for w, t, _ in chat["history"]):
                    continue  # 已经在历史里的不重复记
                chat["history"].append((who, text, name))
                ov.log_message(who, text, name, chat=title)
                if who == "her" and name:
                    if name in chat["senders"]:
                        chat["senders"].remove(name)
                    chat["senders"].insert(0, name)
            ov.set_targets(title, chat["senders"], target_of(title))
            if state["refresh_wait"] == title:
                state["refresh_wait"] = None
                _finish_refresh(title)
            continue
        if kind == "paused":  # 子进程确认已暂停
            ov.set_capture(False)
            ov.sync_bubble(None, None)  # 不采了，聊天区浮层也收起来
            continue
        if kind == "resumed":  # 子进程重新开始采集
            ov.set_capture(True)
            continue
        if kind == "dead":  # 采集彻底停了（微信关了之类），这才是真的要清状态
            state["area"] = None
            ov.sync_bubble(None, None)
            for c in chats.values():  # 在跑的分析作废，回来的结果不再往界面上贴
                c["rev"] += 1
            state["rerun"] = None
            ov.invalidate_replies()
            ov.set_busy(False)
            ov.set_capture(False, msg[1])
            ov.log(msg[1])
            state["refresh_wait"] = None  # 采集都没了，刷新等待作废
            if child is not None:  # 子进程已经不干活了，收掉引用，下次打开开关重开一个
                child.terminate()
                child.join()
                child = None
            continue
        _, title, new, area = msg
        state["area"] = area
        chat = chat_of(title)
        chat["rev"] += 1  # 这个会话有新消息了，它在跑的分析作废
        if title == ov.current_chat():  # 看的是别的会话就别把人家的候选划掉
            ov.invalidate_replies()
        for who, name, text in new:
            chat["history"].append((who, text, name))
            ov.log_message(who, text, name, chat=title)
            if who == "her" and name:  # 群里发过言的人，去重后最近的排最前
                if name in chat["senders"]:
                    chat["senders"].remove(name)
                chat["senders"].insert(0, name)
        ov.set_targets(title, chat["senders"], target_of(title))  # 显不显示这一行由悬浮窗按开关决定
        if new[-1][0] == "her":  # 只有对方最新说话才值得分析
            msgs = list(chat["history"])
            if state["busy"]:
                state["rerun"] = (title, msgs)
                ov.set_busy(True)
            else:
                start_analyze(title, msgs)
        else:
            state["rerun"] = None
            ov.set_busy(False)
            ov.set_status("你已回复，等待对方的新消息")


def window_rect(hwnd):
    """窗口的**可见**边界。GetWindowRect 带着不可见的缩放边框（Win10/11 左右下各 ~7 物理像素，
    200% 缩放下就是 14 逻辑像素），拿它贴边会悬空一截、高度也对不上肉眼看到的边缘；
    DWMWA_EXTENDED_FRAME_BOUNDS 才是用户眼睛看到的窗口边缘。取不到（老系统）退回 GetWindowRect。"""
    rect = ctypes.wintypes.RECT()
    res = ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
    if res != 0 or rect.right <= rect.left or rect.bottom <= rect.top:
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def follow_wechat():
    """吸附跟随：每 ~0.5s 读一次微信窗口矩形，把悬浮窗贴到它边上（设置里能关）。
    启动时没找到微信的，这里隔 ~2s 补找一次，找到就开始跟。
    微信最小化时矩形是 (-16000,-16000) 这种鬼位置，不查会把悬浮窗算到屏幕外——必须跳过。"""
    global _find_count
    if not settings.snap_follow():
        return
    if state["hwnd"] is None:
        _find_count += 1
        if _find_count % 8:  # snap 每 5 拍才进来一次，8 次就是 ~2s
            return
        try:
            state["hwnd"] = find_wechat_hwnd()
        except RuntimeError:
            return
    if ctypes.windll.user32.IsIconic(state["hwnd"]):  # 最小化：不动，微信还原后接着跟
        ov.sync_bubble(None, None)  # 微信都没显示，浮层也收起来
        return
    ov.snap_to(window_rect(state["hwnd"]))
    # 同视觉层：悬浮窗不置顶，Z 序钉在微信正下方——微信被别的窗口盖住它也跟着被盖，
    # 微信在前它就在前。用户正操作悬浮窗（焦点在它）时不压它，松手后下一拍自动归位。
    me = int(ov.win.winId())
    if u32.GetForegroundWindow() != me:
        # NOSIZE|NOMOVE|NOACTIVATE：只调 Z 序，把悬浮窗插到微信（hWndInsertAfter）的下面
        u32.SetWindowPos(me, state["hwnd"], 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    # 微信聊天区的气泡浮层：位置/内容/Z 序每拍跟着微信走
    # state["area"] 是截图帧里的相对坐标，浮层要铺在屏幕上，得加上微信窗口原点；
    # 右缘直接锚定微信窗口可见右缘（消息区右缘在窗口里还差一截，贴上去不贴边）
    if state["area"]:
        wl, wt_, wtr, _ = window_rect(state["hwnd"])
        abs_area = (wl + state["area"][0], wt_ + state["area"][1], wtr, wt_ + state["area"][3])
    else:
        abs_area = None
    ov.sync_bubble(state["chat"], abs_area)
    bh = ov.bubble_hwnd()
    if bh:
        prev = u32.GetWindow(state["hwnd"], 3)  # GW_HWNDPREV：微信正上方那扇窗
        # 浮层插到「微信上方那扇」的下面 = 正好压在微信内容上，又不挡别的应用
        u32.SetWindowPos(bh, prev if prev else 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)


def tick():
    global _snap_count
    try:
        drain()
        if state["refresh_wait"] and time.time() - state["refresh_ts"] > 4:
            title, state["refresh_wait"] = state["refresh_wait"], None  # 子进程没回话：按已有历史兜底
            _finish_refresh(title)
        _snap_count += 1
        if _snap_count % 5 == 0:  # tick 是 50ms 一拍，5 拍（250ms）跟一次：够跟手也不费电
            follow_wechat()
        while not update_result.empty():
            latest, url = update_result.get()
            ov.set_update(latest, url)
        while not results.empty():
            kind, r, title, revision = results.get()
            state["busy"] = False
            if state["rerun"]:  # 分析期间又来了新消息，接着跑最新的
                (t, msgs), state["rerun"] = state["rerun"], None
                start_analyze(t, msgs)
                continue
            if revision != chat_of(title)["rev"]:  # 这个会话后来又说话了，这份结果过期了
                ov.set_busy(False)
                continue
            if kind == "ok":
                r["seq"] = next(_result_seq)  # 浮层/界面判断内容新旧用，别拿 id()（地址会复用）
                chat_of(title)["result"] = r  # 先存着；正看着这个会话才立刻贴上去
                ov.show_judgment(title, judgment_text(r))  # 判断摘要进那个会话的记录（Jev 气泡）
                if title == ov.current_chat():
                    ov.show(r)
                else:
                    ov.set_busy(False)
            else:
                reason = str(r).replace("分析失败：", "").replace("分析失败: ", "").strip()
                chat_of(title)["result"] = None  # 失败：作废旧结果，浮层才肯显示错误面板
                state["vision"].pop(title, None)
                ov.show_judgment(title, f"本轮判断失败：{reason[:160]}")  # 详情进聊天记录
                ov.set_bubble_error(title, reason)  # 浮层面板显示失败原因（不再留旧建议）
                ov.set_busy(False)
                ov.set_status(f"生成失败 · {reason[:70]}", "error")  # 状态栏直接给具体原因
                ov.log(r)
    except Exception:
        traceback.print_exc()  # 一帧出错不退出
    ov.after(50, tick)


if __name__ == "__main__":  # Windows 的 spawn 会让子进程重新执行本文件，没这行就无限套娃开进程
    multiprocessing.freeze_support()  # 打包成 exe 后 spawn 出来的子进程会重跑一遍 exe，没这行就无限弹界面
    ctypes.windll.user32.SetProcessDPIAware()
    # 单实例锁：开两个助手 = 微信上浮着两块气泡层（旧实例画的还是旧结果），看起来像灵异事件
    _lock = QLockFile(os.path.join(os.environ.get("TEMP", os.path.dirname(os.path.abspath(__file__))),
                                   "jev-chat-windows.lock"))
    if not _lock.tryLock(0):
        print("助手已经在运行了（任务栏/托盘找一下），别再开第二个。")
        raise SystemExit(0)
    q = multiprocessing.Queue()
    capture_on = multiprocessing.Event()  # 父子进程共用的开关，置位=采集
    debug_on = multiprocessing.Event()  # 同上，置位=子进程往队列里送整帧给调试窗
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change, on_toggle_debug=set_debug,
                 on_refresh=refresh_analysis,
                 result_of=lambda t: chats.get(t, {}).get("result"))
    child = dbg = None
    try:
        state["hwnd"] = find_wechat_hwnd()
    except RuntimeError:
        ov.set_capture(False, "未找到微信窗口，打开微信后再开启采集")
    else:
        capture_on.set()
        child = spawn_worker()
    if settings.debug_view():  # 上次开着就直接开回来
        set_debug(True)
    if not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        ov.after(0, ov.open_settings)
    if settings.check_update() and update.parse_version(VERSION):  # 开发版没有版本号，不查也不烦源码用户
        threading.Thread(target=check_update_bg, daemon=True).start()
    ov.after(50, tick)
    try:
        ov.run()
    finally:
        if child is not None:
            child.terminate()
