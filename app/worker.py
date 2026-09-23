# -*- coding: utf-8 -*-
"""子进程：截图 → 定位消息区 → OCR 头部会话名和消息 → 按会话去重，全在这边跑。
一次 OCR 250~800ms，放父进程的 Qt 主线程界面就僵了。
只往队列里丢纯 tuple/str（底色 bg 是 numpy，留在这边不过队列）。帧全程内存，绝不落盘。"""
import ctypes
import queue
import time
import traceback

import numpy as np

from app.capture import Capture, chat_area, unminimize
from app.noise import is_system_noise
from app.ocr import Reader, read_header, similar
from app import settings


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


def run(q, hwnd, enabled, debug_on, cmd_q=None):
    """enabled 置位=采集，清掉=暂停。暂停时停掉 WGC 会话（Windows 那圈黄色采集边框也跟着没了），
    恢复时重开一个；readers 一直留着，去重状态不丢，恢复后不会把屏幕上的旧消息再报一遍。
    debug_on 置位才往队列里送整帧（一帧 2~3MB），关着一点额外活都不干。"""
    ctypes.windll.user32.SetProcessDPIAware()
    cap = None
    cap_tries = 0  # WGC 偶发起不来（微信刚启动窗口还没就绪时 GraphicsCaptureItem 转换失败），重试再报死
    readers = {}  # {会话名: Reader}，一个会话一套去重状态
    title, head = "", None  # 当前会话名 / 上一帧的头部像素
    last_area = None  # 上次发给父进程的 4 元组，变了才再发一次
    warned = False  # 消息区识别失败是否已经报过，拖窗口时别每帧刷一条
    while True:
        if not enabled.is_set():
            if cap is not None:
                cap.stop()
                cap = None
                q.put(("paused",))
            enabled.wait()
            continue
        want_refresh = False
        if cmd_q is not None:  # 「重新生成」先取最新内容：对当前画面强制重跑一遍 OCR
            try:
                while True:
                    if cmd_q.get_nowait() == "refresh":
                        want_refresh = True
            except queue.Empty:
                pass
        if want_refresh and cap is not None and cap.latest is not None:
            try:
                full = cap.latest
                area = chat_area(full)
                if area:
                    x0, y0, x1, y1, bg, y_pane = area
                    name, title_left, boundary = read_header(full[y_pane:y0, 0:x1],
                                                             x_boundary=x0)
                    if title_left:
                        x0 = max(x0, min(title_left - 12, x1 - 300))
                    if x1 - x0 < 300:
                        x0 = max(8, x1 - 300)
                    area = (x0, y0, x1, y1, bg, y_pane)
                    cap.area = area
                    name = name or title or "当前会话"
                    name = next((k for k in readers if similar(k, name)), name)
                    reader = readers.setdefault(name, Reader())
                    rows = [(w, n, t) for w, n, t, _ in reader.read(full[y0:y1, x0:x1], bg)]
                    if settings.filter_system_msgs():
                        rows = [m for m in rows if not is_system_noise(m[2])]
                    q.put(("refresh_lines", name, rows, (x0, y0, x1, y1)))
            except Exception:
                _err(q)
        if cap is None:
            try:
                cap = Capture(hwnd)
                cap_tries = 0
            except Exception as e:
                cap_tries += 1
                if cap_tries >= 10:  # 十次（约十秒）都不成才算真死，别一秒重试几十次
                    q.put(("dead", "无法开始采集：" + (" ".join(str(e).split())[:120] or type(e).__name__)))
                    enabled.clear()  # 自己清掉，下一圈就去等着
                else:
                    time.sleep(1)
                continue
            q.put(("resumed",))
        if not cap.alive():
            break
        try:
            unminimize(hwnd)
            full = cap.settled()
            if full is not None:
                reader, lines = None, []  # 调试视图要用，消息区没认出来时就是空的
                area = chat_area(full)  # 每次停稳都重算：拖完窗口微信布局会晚一拍才铺好，只按尺寸变化算一次会锁死
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
                    # 会话名 = 底色分界线右侧的文字框（搜索框、＋按钮都在左侧，结构性排除）；
                    # 消息区左边界取「名字左缘 - 12」和底色分界中更靠右的，保证不圈进列表
                    name, title_left, boundary = read_header(full[y_pane:y0, 0:x1],
                                                             x_boundary=x0)
                    if title_left:
                        x0 = max(x0, min(title_left - 12, x1 - 300))
                    if x1 - x0 < 300:
                        x0 = max(8, x1 - 300)
                    area = (x0, y0, x1, y1, bg, y_pane)
                    cap.area = area  # 采集线程的 diff 和裁剪都用修正后的区域
                    rect = (x0, y0, x1, y1)
                    crop = full[y_pane:y0, x0:x1]  # 头部：会话名在这里（已对准）
                    if head is None or not np.array_equal(crop, head):  # 名字没动就别白跑一次 OCR
                        head = crop
                        # OCR 抖一下（「小分队」↔「小分认」）不能分裂出一个新会话
                        name = next((k for k in readers if similar(k, name)), name) if name else ""
                        # ponytail: 认不出就沿用上次；开头就认不出给个占位名，总比把消息全丢了强
                        name = name or title or "当前会话"
                        if name != title:
                            title = name
                            q.put(("chat", title))
                    reader = readers.setdefault(title, Reader())
                    # 文字模型模式：先把左右头像列涂掉再 OCR（头像上会幻觉出 K<OY 这类鬼字）；
                    # 视觉模式保留头像（模型靠它分清谁发的）
                    dpr = ctypes.windll.user32.GetDpiForSystem() / 96
                    lines = reader.read(full[y0:y1, x0:x1], bg,
                                        keep_images=settings.vision_draft(),
                                        mask_avatar_px=0 if settings.vision_draft() else round(46 * dpr))
                    new = reader.new_lines(lines)
                    if settings.filter_system_msgs():  # 开关：系统通知/时间戳挡在上下文外
                        new = [m for m in new if not is_system_noise(m[2])]
                    if new:
                        q.put(("lines", title, new, rect))
                        if settings.vision_draft():  # 视觉开关：附带消息区截图给起草模型
                            try:
                                import io as _io
                                from PIL import Image
                                im = Image.fromarray(np.ascontiguousarray(full[y0:y1, x0:x1]))
                                if im.width > 1400:
                                    im = im.resize((1400, round(im.height * 1400 / im.width)))
                                buf = _io.BytesIO()
                                im.save(buf, "JPEG", quality=80)
                                q.put(("vision", title, buf.getvalue()))
                            except Exception:
                                _err(q)
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
