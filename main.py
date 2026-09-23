# -*- coding: utf-8 -*-
"""父进程：只管界面。截图 + OCR 在 app/worker.py 的子进程里跑，队列里收新消息 →
冒出新的对方消息才调 engine → 悬浮窗给 3 条候选 → 人点「填入」。发送永远手动。静默期零调用。
上下文、结果、聊天记录都按会话名（子进程 OCR 头部标题得来）分开存，切会话不串味。

    pip install rapidocr-onnxruntime numpy windows-capture PySide6-Fluent-Widgets
两个模型（判断 Jev / 起草语言模型）的来源和 key 在独立设置页填写，不用改代码。IDE 里直接 Run。
"""
import ctypes
import multiprocessing
import queue
import threading
import traceback
from collections import deque

from app import settings, worker
from app.capture import find_chat_hwnd
from app.fill import fill
from app.overlay import Overlay
from core.engine import analyze
from core import history

# {会话名: {history, result, rev, target, senders, last_attempt}}：每个会话各自的上下文、上次结果和版本号，互不串味
# history 里是 [(who, text, name)]，engine 只认 her/me，name 是群里的发言人（单聊/自己说的是 None）；
# 只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定
# senders：这个群里发过言的人，去重、最近的排最前；target：用户挑的回复对象（None = 跟着最近那个走）
chats = {}
state = {"area": None, "busy": False, "rerun": None, "hwnd": None, "chat": "",
         "worker_app": None}
results = queue.Queue()


def chat_of(title):
    return chats.setdefault(title, {"history": deque(maxlen=200), "result": None, "rev": 0,
                                    "target": None, "senders": [], "last_attempt": None,
                                    "hydrated": False})


def history_key(title):
    """把聊天软件纳入 key，避免同名 QQ/微信会话串历史。"""
    return f"{settings.chat_app()}::{title}"


def hydrate_chat(title):
    """第一次看到会话时恢复本地文字历史；历史库异常不影响当前采集。"""
    chat = chat_of(title)
    if chat["hydrated"]:
        return
    chat["hydrated"] = True
    if not settings.history_enabled():
        return
    try:
        history.cleanup(settings.history_retention_days(), settings.history_max_mb())
        records = history.load_messages(history_key(title), settings.history_limit())
        for record in records:
            chat["history"].append((record["who"], record["text"], record["name"]))
        if records:
            ov.restore_messages(title, records)
    except Exception as exc:
        ov.log(f"[历史记录] 暂时无法读取：{type(exc).__name__}")


def target_of(title):
    """这个会话现在的回复对象：用户挑过且人还在就用它，否则用最近说话的那个；单聊没有发言人 → None。"""
    chat = chat_of(title)
    if chat["target"] in chat["senders"]:
        return chat["target"]
    return chat["senders"][0] if chat["senders"] else None


def fill_reply(text):
    if state["hwnd"] is None:  # 子进程重开过，hwnd 可能换了，用最新的
        raise RuntimeError(f"未找到{settings.chat_app_name()}窗口，请确认{settings.chat_app_name()}已打开")
    if state["area"] is None:
        raise RuntimeError(f"{settings.chat_app_name()}输入区域尚不可用，请确认聊天窗口可见（不要最小化）")
    if settings.reply_target() and ov.at_prefix_enabled():
        target = target_of(ov.current_chat())  # 填进去的是界面上正看着的那个会话的对象
        if target:
            text = f"@{target} " + text  # 纯文本，微信不认成真正的 @，只是让群里看得出在跟谁说
    fill(state["hwnd"], state["area"], text, settings.chat_app())


def spawn_worker():
    """开一个采集子进程，它跟着 capture_on 走：置位=采集，清掉=暂停。"""
    app = settings.chat_app()
    p = multiprocessing.Process(target=worker.run,
                                args=(q, state["hwnd"], capture_on, debug_on, app), daemon=True)
    p.start()
    state["worker_app"] = app
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
    """标题栏开关：默认暂停；用户主动打开后才找聊天窗口并启动采集。"""
    global child
    if not on:
        capture_on.clear()
        return
    if child is None:
        try:
            state["hwnd"] = find_chat_hwnd(settings.chat_app())
        except RuntimeError:
            ov.set_capture(False, f"未找到{settings.chat_app_name()}窗口，打开后再开启采集")
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
                                   profile=settings.reply_profile(),
                                   reply_guide=settings.custom_reply_guide(),
                                   thinking=settings.thinking(),
                                   jev_provider=settings.jev_provider(),
                                   jev_model=(settings.jev_model() if settings.judge_mode() == "jev"
                                              else settings.judge_model()) or None,
                                   judge_mode=settings.judge_mode(),
                                   judge_base_url=settings.judge_base_url() or None),
                     title, revision))
    except Exception as e:
        results.put(("err", f"分析失败: {e}", title, revision))


def start_analyze(title, msgs):
    if not settings.has_judge_key():
        ov.set_status("请先在设置中配置模型", "warning")
        return
    if not settings.has_llm_key():
        ov.set_status(f"起草来源 {settings.draft_provider_name()} 没填密钥，去设置里补上", "warning")
        return
    chat_of(title)["last_attempt"] = list(msgs)
    state["busy"] = True
    ov.set_busy(True)
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    threading.Thread(target=analyze_bg, args=(msgs, title, chat_of(title)["rev"], reply_to),
                     daemon=True).start()


def retry_analyze(title):
    """重试上一次失败的同一批消息；没有记录时才退回当前上下文。"""
    chat = chat_of(title)
    msgs = chat.get("last_attempt") or list(chat["history"])
    if not msgs:
        ov.set_status("这个会话还没有可用于生成的聊天记录", "warning")
        ov.clear_retry(title)
        return
    start_analyze(title, list(msgs))


def generate_from_history(selection):
    """按用户点中的消息截断上下文，允许忽略后面发出的表情包/图片等内容。"""
    title, _, who, text, name, _ = selection
    history = list(chat_of(title)["history"])
    target = (who, text, name or None)
    index = next((i for i in range(len(history) - 1, -1, -1)
                  if (history[i][0], history[i][1], history[i][2] or None) == target), None)
    if index is None:
        ov.set_status("这条消息已经不在当前上下文里，请先等采集更新后再选", "warning")
        return
    msgs = history[:index + 1]
    if not any(item[0] == "her" for item in msgs):
        ov.set_status("选中的内容里没有对方消息，换一条对方消息再生成", "warning")
        return
    if state["busy"]:
        state["rerun"] = (title, msgs)
        ov.set_status("当前生成结束后，会按选中的消息重新生成", "busy")
        return
    start_analyze(title, msgs)


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


def _find_capture_after_switch(app):
    """后台找切换后的窗口，避免保存设置时让 Qt 主线程等待 Windows API。"""
    try:
        hwnd = find_chat_hwnd(app)
        q.put(("reconfigure", app, hwnd, ""))
    except RuntimeError as exc:
        q.put(("reconfigure", app, None, str(exc)))


def on_settings_saved():
    """保存后只在聊天软件真的改变时切换采集；查窗口和停旧进程不阻塞设置页。"""
    global child
    new_app = settings.chat_app()
    if child is None:
        state["worker_app"] = None
        return
    if state.get("worker_app") == new_app:
        return
    # QQ/微信切换时不能把旧软件的内存上下文继续带过去；磁盘历史按 history_key 隔离。
    chats.clear()
    was_on = capture_on.is_set()
    capture_on.clear()
    child.terminate()
    child = None
    state["area"] = None
    state["hwnd"] = None
    state["worker_app"] = None
    if not was_on:
        ov.set_capture(False, "会话内容不再读取")
        return
    ov.set_capture(False, f"正在切换到{settings.chat_app_name()}…")
    threading.Thread(target=_find_capture_after_switch, args=(new_app,), daemon=True).start()


def on_settings_reset():
    """重置设置后同步清掉当前进程里的会话缓存，避免旧消息继续参与下一次判断。"""
    chats.clear()
    ov.clear_history()


def drain():
    """把子进程队列里攒的东西全收掉。"""
    global child
    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            return
        kind = msg[0]
        if kind == "reconfigure":
            _, app, hwnd, reason = msg
            if app != settings.chat_app() or child is not None:
                continue
            if hwnd is None:
                ov.set_capture(False, f"未找到{settings.chat_app_name()}窗口，请打开后再开启采集")
                ov.log(reason)
                continue
            state["hwnd"] = hwnd
            child = spawn_worker()
            capture_on.set()
            ov.set_capture(True)
            continue
        if kind == "area":  # 只是窗口挪了位置，坐标跟着更新，别的什么都不用动
            state["area"] = msg[1]
            continue
        if kind == "chat":  # 微信切了会话，界面跟过去（用户正浏览别的会话时也跟，微信是准的）
            state["chat"] = msg[1]
            hydrate_chat(msg[1])
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
        if kind == "paused":  # 子进程确认已暂停
            ov.set_capture(False)
            continue
        if kind == "resumed":  # 子进程重新开始采集
            ov.set_capture(True)
            continue
        if kind == "dead":  # 采集彻底停了（微信关了之类），这才是真的要清状态
            state["area"] = None
            for c in chats.values():  # 在跑的分析作废，回来的结果不再往界面上贴
                c["rev"] += 1
            state["rerun"] = None
            ov.invalidate_replies()
            ov.set_busy(False)
            ov.set_capture(False, msg[1])
            ov.log(msg[1])
            if child is not None:  # 子进程已经不干活了，收掉引用，下次打开开关重开一个
                child.terminate()
                child.join()
                child = None
            continue
        _, title, new, area = msg
        state["area"] = area
        hydrate_chat(title)
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
        if settings.history_enabled():
            history.save_messages(history_key(title), new,
                                  settings.history_retention_days(), settings.history_max_mb())
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


def tick():
    try:
        drain()
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
                chat_of(title)["result"] = r  # 先存着；正看着这个会话才立刻贴上去
                if settings.history_enabled():
                    history.save_judgment(history_key(title), r)
                ov.clear_retry(title)
                if title == ov.current_chat():
                    ov.show(r)
                else:
                    ov.set_busy(False)
            else:
                ov.set_busy(False)
                ov.mark_retry(title)
                ov.set_status("生成失败，可以点右侧「重试」；也可以从聊天记录选一条重新生成。", "error")
                ov.log(r)
    except Exception:
        traceback.print_exc()  # 一帧出错不退出
    ov.after(50, tick)


if __name__ == "__main__":  # Windows 的 spawn 会让子进程重新执行本文件，没这行就无限套娃开进程
    multiprocessing.freeze_support()  # 打包成 exe 后 spawn 出来的子进程会重跑一遍 exe，没这行就无限弹界面
    ctypes.windll.user32.SetProcessDPIAware()
    q = multiprocessing.Queue()
    capture_on = multiprocessing.Event()  # 父子进程共用的开关，置位=采集
    debug_on = multiprocessing.Event()  # 同上，置位=子进程往队列里送整帧给调试窗
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change, on_toggle_debug=set_debug,
                 on_retry=retry_analyze, on_history_generate=generate_from_history,
                  result_of=lambda t: chats.get(t, {}).get("result"),
                  on_settings_saved=on_settings_saved,
                  on_settings_reset=on_settings_reset)
    child = dbg = None
    state["worker_app"] = None
    ov.set_capture(False, "会话内容不再读取")
    if settings.debug_view():  # 上次开着就直接开回来
        set_debug(True)
    if not settings.has_judge_key():
        ov.set_status("请先在设置中配置模型", "warning")
        ov.after(0, ov.open_settings)
    ov.after(50, tick)
    try:
        ov.run()
    finally:
        if child is not None:
            child.terminate()
