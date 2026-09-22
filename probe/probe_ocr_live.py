# -*- coding: utf-8 -*-
"""
持续 OCR 探针：WGC 盯着微信窗口，消息区一变就 OCR，新冒出来的文字实时打到控制台。
在 IDE 里直接 Run，Ctrl-C / 停止按钮结束。

    pip install rapidocr-onnxruntime numpy windows-capture

只读，帧只在内存，不写盘不上传。
"""
import ctypes
import difflib
import os
import re
import time
import traceback

import numpy as np
from rapidocr_onnxruntime import RapidOCR
from windows_capture import WindowsCapture


def find_wechat_hwnd():
    """枚举顶层窗口，按进程名 Weixin.exe/WeChat.exe 找，取标题「微信」的那个（主窗口），没有就取第一个。"""
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    found = []

    def exe_of(pid):
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        return os.path.basename(buf.value).lower() if ok else ""

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if exe_of(pid.value) not in ("weixin.exe", "wechat.exe"):
            return True
        title = ctypes.create_unicode_buffer(256)
        u32.GetWindowTextW(hwnd, title, 256)
        found.append((hwnd, title.value))
        return True

    u32.EnumWindows(cb, 0)
    assert found, "没找到 Weixin.exe / WeChat.exe 的可见窗口。微信开着吗？"
    return next((h for h, t in found if t == "微信"), found[0][0])


def chat_area(full, header_h=60):
    """消息列表区 (x0, y0, x1, y1)，像素锚点，见 probe/probe_ocr_speed.py 里的说明。"""
    H, W = full.shape[:2]
    right = full[::8, W // 2::8].reshape(-1, 3)  # 抽样够用，全量 np.unique 在 2560 宽的图上要半秒
    vals, cnt = np.unique(right, axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    isbg = np.abs(full.astype(int) - bg).sum(-1) <= 6
    col = isbg[H // 4: H * 3 // 4].mean(0)
    x0 = int(np.argmax(col > 0.3))
    x1 = W - int(np.argmax(col[::-1] > 0.3))
    row = isbg[:, x0:x1].mean(1)
    y0 = int(np.argmax(row > 0.9))
    y1 = H - int(np.argmax(row[::-1] > 0.9))
    band = full[y0:y1, x0:x1].astype(int)
    seps = y0 + np.where((band.std(axis=(1, 2)) < 4) & (row[y0:y1] < 0.1))[0]
    seps = [int(s) for i, s in enumerate(seps) if i == 0 or s - seps[i - 1] > 3]
    below = [s for s in seps if s > y0 + 0.45 * (y1 - y0)]
    y_in = below[0] if below else y1
    above = [s for s in seps if y0 + header_h < s < y_in - 50]
    y_top = above[-1] if above else y0 + header_h
    if x1 - x0 < 100 or y_in - y_top < 40:
        return None  # 拖窗口拖到一半、布局没铺好的帧，认不出消息区
    return x0, y_top, x1, y_in, bg


def who_said(chat, box):
    """按 OCR 框里的颜色分类，不看 x 坐标。返回 (谁, 底色, 墨高)：
    绿底 → me；非绿且文字对底色对比度 ≥150 → her；其余（引用块、群里的发言人名、时间戳、系统提示、
    链接卡片描述——都是灰字，对比度 80~95）→ "gray"。
    实测：气泡正文对比度 178~208，me 绿泡 142~150，灰字 ≤ 93。深浅主题都靠这套。
    墨高 = 框里最长一段连续有字的行数（OCR 框对小字有固定 padding、还会蹭到上下行，不能拿框高比大小）。"""
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    reg = chat[int(min(ys)):int(max(ys)), int(min(xs)):int(max(xs))].astype(int)
    if reg.size == 0:
        return None, None, 0
    flat = reg.reshape(-1, 3)
    vals, cnt = np.unique(flat, axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    lum = reg @ [0.299, 0.587, 0.114]
    diff = np.abs(lum - bg @ [0.299, 0.587, 0.114])
    ink_h = best = 0  # 最长的连续有墨行段 = 一行字的实际高度（框里若混进上下行的边，也不会被算进去）
    for r in (diff > 60).any(axis=1):
        best = best + 1 if r else 0
        ink_h = max(ink_h, best)
    if bg[1] > bg[0] + 40 and bg[1] > bg[2] + 40:
        return "me", bg, ink_h
    return ("her" if diff.max() >= 150 else "gray"), bg, ink_h


ctypes.windll.user32.SetProcessDPIAware()
ocr = RapidOCR(intra_op_num_threads=4)  # det_limit_type 构造传参在 rapidocr 1.2.3 触发 KeyError，下面直接改预处理参数
for op in ocr.text_detector.preprocess_op:
    if type(op).__name__ == "DetResizeForTest":
        op.limit_type = "max"
        op.limit_side_len = 4000
state = {"shape": None, "area": None, "bg": None, "last": None, "seen": [], "pending": None, "t": 0, "t0": 0, "lh": None, "shown": None}
SETTLE = 0.25  # 秒：画面停稳这么久才 OCR，跳过滚动/新消息滑入的中间帧（半截气泡会认错、会重复）
MAX_WAIT = 1.0  # 秒：画面一直在变（动图表情包）就永远停不稳，最多等这么久照样 OCR
hwnd = find_wechat_hwnd()
cap = WindowsCapture(window_hwnd=hwnd)


def unminimize(hwnd):
    """Windows 不渲染最小化的窗口，什么截图法都拿不到画面。发现被最小化就无激活还原，再压到所有窗口最底下——
    看着跟收起来一样，但 DWM 继续画，能截。不抢焦点、不动大小位置。"""
    u32 = ctypes.windll.user32
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        u32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x13)  # HWND_BOTTOM, SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE
        print("微信被最小化了（系统不渲染最小化窗口，截不到）→ 已还原并压到最底层，别最小化，用别的窗口盖住就行")


@cap.event
def on_frame_arrived(frame, control):
    """采集线程：只做跟上一帧比，变了就把整帧挂成 pending，消息区定位 + OCR 交给主线程去抖后再做。"""
    full = np.ascontiguousarray(frame.frame_buffer[:, :, :3][:, :, ::-1])  # BGRA → RGB，纯内存
    if full.max() == 0:
        return
    if state["area"] is None or full.shape != state["shape"]:
        state["shape"], state["area"] = full.shape, chat_area(full)
    if state["area"] is None:
        return
    x0, y0, x1, y1, _ = state["area"]
    chat = full[y0:y1, x0:x1]
    if state["last"] is not None and np.array_equal(chat, state["last"]):
        return  # 画面没变，0 开销
    state["last"] = chat
    if state["pending"] is None:
        state["t0"] = time.perf_counter()  # 这轮变化开始的时刻
    state["pending"], state["t"] = full, time.perf_counter()


def process(full):
    area = chat_area(full)  # 每次停稳都重算：拖完窗口微信布局会晚一拍才铺好，只按尺寸变化算一次会锁死在半成品上
    if area is None:
        print("消息区认不出来（窗口太小/布局没铺好），等下一帧")
        return
    if area[:4] != state["shown"]:  # 末位是 numpy 底色，只比前四个
        state["shown"] = area[:4]
        print(f"消息区 x{area[0]}-{area[2]} y{area[1]}-{area[3]}")
    state["area"] = area
    x0, y0, x1, y1, state["bg"] = area
    chat = full[y0:y1, x0:x1]
    t0 = time.perf_counter()
    res, _ = ocr(chat, use_cls=False)
    ms = (time.perf_counter() - t0) * 1000
    # 群聊：每条 her 气泡上方有一行灰色发言人名（靠左、短、不带冒号、直接印在面板底色上），
    # 从上往下扫，名字带给后面的气泡。引用块/时间戳/公告带冒号，链接卡片的灰字印在气泡底色上，都不会被当成名字。
    # 单聊没有名字行，就是裸 her。
    # ponytail: 名字行被 OCR 漏掉时会挂到上一个人头上。
    name, lines = None, []
    for box, text, _ in sorted(res or [], key=lambda r: r[0][0][1]):
        kind, bg, h = who_said(chat, box)
        if kind == "gray":
            on_pane = bg is not None and np.abs(bg - state["bg"]).sum() <= 6
            if on_pane and box[0][0] < 0.25 * (x1 - x0) and len(text) <= 16 and not re.search("[:：]", text):
                name = text
            continue
        if kind is None:
            continue
        if state["lh"] and h < 0.6 * state["lh"]:
            continue  # 字比正常气泡小得多 = 图片消息（截图/表情包）里的字，不是气泡
        lines.append((f"her({name})" if kind == "her" and name else kind, text, box[0][1], h))
    if not state["lh"] and len(lines) >= 3:
        state["lh"] = float(np.median([h for *_, h in lines]))  # 用头一帧定下正常字高
    # 去重（滚动不重复）：
    #  - 同一段像素挪个位置 OCR 结果会抖（「傻逼了」↔「傻逼」、「不好意思」↔「不好竟思」），所以按相似度判已见，不按全等
    #  - 本帧有已知行时，只打已知行下方的新行：往上滚翻出来的旧消息在已知行上方，不打
    #  - 本帧一行已知的都没有（大图/表情包把旧文字全顶出去了、切了聊天、滚远了）：全打。宁可多打也不能漏新消息。
    # ponytail: 同一人连发两句一模一样的会被吞一句；切聊天/滚远会把当前可见的历史打一遍。
    #           真要分「新来的」还是「翻出来的」，拿相邻两帧行均值做互相关算滚动方向，此处不做。
    seen = state["seen"]
    known_y = [y for w, t, y, _ in lines if is_seen(seen, w, t)]
    floor = max(known_y) if known_y else -1
    new = [(w, t) for w, t, y, _ in lines if y > floor and not is_seen(seen, w, t)]
    seen.extend((w, t) for w, t, *_ in lines if not is_seen(seen, w, t))
    del seen[:-500]
    for who, text in new:
        print(f"{time.strftime('%H:%M:%S')} [{ms:4.0f}ms] {who}: {text}", flush=True)


def is_seen(seen, who, text):
    for w, t in seen:
        if w != who:
            continue
        if t == text or difflib.SequenceMatcher(None, t, text).ratio() >= 0.75:
            return True
        if len(t) == len(text) >= 3 and sum(a != b for a, b in zip(t, text)) <= 1:  # 短句错一个字
            return True
    return False


@cap.event
def on_closed():
    print("微信窗口关了，结束")


ctl = cap.start_free_threaded()
print("盯着微信中… Ctrl-C 结束")
try:
    while not ctl.is_finished():
        time.sleep(0.05)
        unminimize(hwnd)
        now = time.perf_counter()
        if state["pending"] is not None and (now - state["t"] > SETTLE or now - state["t0"] > MAX_WAIT):
            full, state["pending"] = state["pending"], None
            try:
                process(full)
            except Exception:
                traceback.print_exc()  # 一帧出错不退出
    ctl.wait()  # 采集线程若是报错死的，这里把错误抛出来，别静默结束
except KeyboardInterrupt:
    ctl.stop()
