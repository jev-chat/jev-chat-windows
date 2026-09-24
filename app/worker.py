# -*- coding: utf-8 -*-
"""子进程：截图 → 定位消息区 → OCR 头部会话名和消息 → 按会话去重，全在这边跑。
一次 OCR 250~800ms，放父进程的 Qt 主线程界面就僵了。
只往队列里丢纯 tuple/str（底色 bg 是 numpy，留在这边不过队列）。帧全程内存，绝不落盘。"""
import sys
import time
import traceback

import numpy as np

from app.capture import LOCKED_STATUS, Capture, chat_area, unminimize
from app.ocr import Reader, read_title, similar


def _err(q):
    """异常压成一行发给父进程，子进程的 stderr 一般没人看得见。"""
    q.put(("status", " ".join(traceback.format_exc().split())[-200:]))


def _packet(full, area, title, reader, lines):
    """调试视图的一帧：整帧缩到长边 ≤1100 再走队列（原帧 2560 宽裸传要 20MB），只在内存里传，不落盘。
    整数步长切片够用，不引新依赖；框和坐标照发原始值，画的那边按 scale 折算。"""
    k = max(1, -(-max(full.shape[:2]) // 1100))
    small = np.ascontiguousarray(full[::k, ::k])
    return {"w": small.shape[1], "h": small.shape[0], "rgb": small.tobytes(), "scale": k,
            "area": tuple(int(v) for v in area[:4]) if area else None,
            "pane_top": int(area[5]) if area else 0, "title": title,
            "boxes": reader.last_boxes if reader else [],
            "lines": [(w, n, t) for w, n, t, _ in lines],
            "ocr_ms": reader.last_ms if reader else 0, "ts": time.time()}


def run(q, hwnd, enabled, debug_on):
    """enabled 置位=采集，清掉=暂停。暂停时停掉 WGC 会话（Windows 那圈黄色采集边框也跟着没了），
    恢复时重开一个；readers 一直留着，去重状态不丢，恢复后不会把屏幕上的旧消息再报一遍。
    debug_on 置位才往队列里送整帧（一帧 2~3MB），关着一点额外活都不干。"""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    cap = None
    readers = {}  # {会话名: Reader}，一个会话一套去重状态
    title, head = "", None  # 当前会话名 / 上一帧的头部像素
    last_area = None  # 上次发给父进程的 4 元组，变了才再发一次
    last_geom = None
    warned = False  # 消息区识别失败是否已经报过，拖窗口时别每帧刷一条
    lock_warned = False
    while True:
        if not enabled.is_set():
            if cap is not None:
                cap.stop()
                cap = None
                q.put(("paused",))
            enabled.wait()
            continue
        if cap is None:
            try:
                cap = Capture(hwnd)
            except Exception as e:
                q.put(("dead", "无法开始采集：" + (" ".join(str(e).split())[:120] or type(e).__name__)))
                enabled.clear()  # 自己清掉，下一圈就去等着，别一秒重试几十次
                continue
            q.put(("resumed",))
        if not cap.alive():
            break
        try:
            unminimize(hwnd)
            if isinstance(getattr(cap, "geom", None), dict):
                geom = {k: cap.geom.get(k) for k in ("pid", "x", "y", "w", "h", "title", "locked", "scale")}
                geom["scale"] = getattr(cap, "scale", geom.get("scale") or 1)
                if geom != last_geom:
                    q.put(("geom", geom))
                    last_geom = geom
                    hwnd = geom
                if geom.get("locked") and not lock_warned:
                    q.put(("status", LOCKED_STATUS))
                    lock_warned = True
                if not geom.get("locked"):
                    lock_warned = False
            full = cap.settled()
            locked = isinstance(getattr(cap, "geom", None), dict) and cap.geom.get("locked")
            if full is not None:
                reader, lines = None, []  # 调试视图要用，消息区没认出来时就是空的
                if locked:
                    area = None
                    if debug_on.is_set():
                        q.put(("debug", _packet(full, None, title, None, [])))
                else:
                    scale = getattr(cap, "scale", 1) or 1
                    area = chat_area(full, scale=scale)  # 每次停稳都重算：拖完窗口微信布局会晚一拍才铺好，只按尺寸变化算一次会锁死
                    if area is None:
                        if not warned:
                            q.put(("status", "消息区认不出来（窗口太小？）"))
                            warned = True
                    else:
                        warned = False
                        cap.area = area  # 采集线程拿它做 diff
                        x0, y0, x1, y1, bg, y_pane = area
                        rect = (x0, y0, x1, y1)
                        if rect != last_area:
                            q.put(("area", rect))
                            last_area = rect
                        crop = full[y_pane:y0, x0:x1]  # 头部：会话名在这里
                        if head is None or not np.array_equal(crop, head):  # 名字没动就别白跑一次 OCR
                            head = crop
                            name = read_title(crop)
                            # OCR 抖一下（「小分队」↔「小分认」）不能分裂出一个新会话
                            name = next((k for k in readers if similar(k, name)), name) if name else ""
                            # ponytail: 认不出就沿用上次；开头就认不出给个占位名，总比把消息全丢了强
                            name = name or title or "当前会话"
                            if name != title:
                                title = name
                                q.put(("chat", title))
                        reader = readers.setdefault(title, Reader())
                        lines = reader.read(full[y0:y1, x0:x1], bg)
                        new = reader.new_lines(lines)
                        if new:
                            q.put(("lines", title, new, rect))
                    if debug_on.is_set():
                        q.put(("debug", _packet(full, area, title, reader, lines)))
        except Exception:
            _err(q)  # 一帧出错不退出
        time.sleep(0.05)
    q.put(("dead", "采集停了（微信关了？）"))
    try:
        cap.wait()  # 采集线程若是报错死的，这里把错抛出来
    except Exception:
        _err(q)
