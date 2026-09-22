# -*- coding: utf-8 -*-
"""
OCR 速度探针：RapidOCR 在这台机器上一帧要多久，裁底部能快多少。
在 IDE 里直接 Run。改下面 SOURCE 切换图片来源。

    pip install rapidocr-onnxruntime pillow numpy windows-capture

只读，图片只在本地内存处理，不写盘不上传。

已知结论（Mac M 系列 CPU）：
  - det_limit_type 默认 'min' 会把小图放大到短边 736，裁小反而更慢；必须用 'max'。
  - 关 cls / 调 side_len 没用，瓶颈是 rec 每行一次。想快就少喂行（只 OCR 新消息区）。
"""
import ctypes
import os
import statistics
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from rapidocr_onnxruntime import RapidOCR

# "wechat" = 自动找微信窗口抓一帧（Windows）；"synth" = 合成图；或写一张截图的路径
SOURCE = "wechat"

FONTS = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "/System/Library/Fonts/STHeiti Medium.ttc"]
MSGS = [("her", "你今天怎么不回我消息呀"), ("me", "刚才在开会，没看手机"),
        ("her", "哦，那你现在忙完了吗"), ("me", "忙完了，怎么了"),
        ("her", "没什么，就是想问问你周末有没有空"), ("me", "周末应该有空，你想干嘛"),
        ("her", "我朋友说新开了家日料店，想去试试"), ("me", "行啊，那周六中午？"),
        ("her", "好呀好呀，那说定了"), ("her", "对了你上次说的那本书借我看看呗")]


def synth(w=1000, h=760, fs=22):
    font = next(f for f in FONTS if os.path.exists(f))
    img = Image.new("RGB", (w, h), (237, 237, 237))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(font, fs)
    y = 30
    for who, t in MSGS:
        bw, bh = d.textlength(t, font=f) + 30, fs + 24
        x = w - 80 - bw if who == "me" else 80
        d.rounded_rectangle([x, y, x + bw, y + bh], 8, fill=(149, 236, 105) if who == "me" else (255, 255, 255))
        d.text((x + 15, y + 12), t, font=f, fill=(0, 0, 0))
        y += bh + 22
    return np.asarray(img)


def find_wechat_hwnd():
    """枚举顶层窗口，按进程名 Weixin.exe/WeChat.exe 找。顺带把候选都打出来看看。
    实测同进程有好几个顶层窗口：主窗口 title='微信' class=Qt51514QWindowIcon，
    另有 'Weixin'（ToolSaveBits 工具窗）和 '图片和视频'（看图窗），面积都可能比主窗口大，
    所以不能按面积挑，按标题「微信」挑，没有就取第一个。"""
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
        title, cls, r = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256), (ctypes.c_long * 4)()
        u32.GetWindowTextW(hwnd, title, 256)
        u32.GetClassNameW(hwnd, cls, 256)
        u32.GetWindowRect(hwnd, r)
        found.append((hwnd, title.value, cls.value, (r[2] - r[0]) * (r[3] - r[1])))
        return True

    u32.EnumWindows(cb, 0)
    assert found, "没找到 Weixin.exe / WeChat.exe 的可见窗口。微信开着吗？"
    print("微信窗口候选：")
    for hwnd, title, cls, area in found:
        print(f"  hwnd={hwnd} title={title!r} class={cls} area={area}")
    return next((f[0] for f in found if f[1] == "微信"), found[0][0])


def grab_wechat():
    """Windows Graphics Capture 对着微信窗口抓一帧到内存：被遮挡/后台都行，只有最小化不行。
    正式 capture.py 就走这条路，这里顺带验证它在 Weixin.exe 上到底出不出图。"""
    from windows_capture import WindowsCapture

    ctypes.windll.user32.SetProcessDPIAware()  # 不然 HiDPI 下坐标是缩放过的
    hwnd = find_wechat_hwnd()
    got = []
    # ponytail: cursor_capture/draw_border 留默认 None，老版 Win10 不支持切换会直接抛异常；黄框无所谓
    cap = WindowsCapture(window_hwnd=hwnd)

    @cap.event
    def on_frame_arrived(frame, control):
        arr = frame.frame_buffer[:, :, :3][:, :, ::-1]  # BGRA → RGB，内存对象，绝不 .save()
        if arr.max() > 0:  # 头一两帧可能全黑，跳过
            got.append(np.ascontiguousarray(arr))
            control.stop()

    @cap.event
    def on_closed():
        pass

    cap.start()  # 阻塞到 control.stop()
    assert got, "WGC 没拿到帧。微信最小化了吗？"
    return got[0]


def chat_area(full, header_h=60):
    """算出消息列表区 (x0, y0, x1, y1)，全靠像素锚点，不靠固定坐标：
    - 面板背景色 = 右半边最常见的颜色（深浅主题通用）
    - 面板左/右边界 = 第一/最后一根「背景占比 > 30%」的列（联系人列表那边是另一种底色，占比 0）
    - 横向分隔线 = 整行单色且不是背景色的行；输入框顶 = 面板 45% 高度以下第一根；
      公告条下面那根（有的话）= 消息区顶，没有就用 header_h
    ponytail: 输入框拉到超过面板一半高就会认错；header_h 按 100% DPI 给的，缩放了按比例调。"""
    H, W = full.shape[:2]
    right = full[:, W // 2:].reshape(-1, 3)
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
    seps = [int(s) for i, s in enumerate(seps) if i == 0 or s - seps[i - 1] > 3]  # 相邻行合并
    below = [s for s in seps if s > y0 + 0.45 * (y1 - y0)]
    y_in = below[0] if below else y1
    above = [s for s in seps if y0 + header_h < s < y_in - 50]
    y_top = above[-1] if above else y0 + header_h
    return x0, y_top, x1, y_in


def bench(name, engine, arr, n=5):
    engine(arr, use_cls=False)  # 预热
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        res, _ = engine(arr, use_cls=False)
        ts.append((time.perf_counter() - t0) * 1000)
    print(f"{name:<28} median {statistics.median(ts):5.0f} ms  min {min(ts):5.0f} ms  识别 {len(res or [])} 段")
    return res


if SOURCE == "wechat":
    full = grab_wechat()
elif SOURCE == "synth":
    full = synth()
else:
    full = np.asarray(Image.open(SOURCE).convert("RGB"))
H, W = full.shape[:2]
x0, y0, x1, y1 = chat_area(full)
chat = full[y0:y1, x0:x1]
CW, CH = x1 - x0, y1 - y0
print(f"图 {W}x{H}，消息区 x{x0}-{x1} y{y0}-{y1} ({CW}x{CH})\n")

e1 = RapidOCR(intra_op_num_threads=4)  # det_limit_type 构造传参在 rapidocr 1.2.3 触发 KeyError，下面直接改预处理参数
for op in e1.text_detector.preprocess_op:
    if type(op).__name__ == "DetResizeForTest":
        op.limit_type = "max"
        op.limit_side_len = max(W, H)
res = bench("limit=max 消息区", e1, chat)
bench("limit=max 消息区底部 200px", e1, chat[-200:])
bench("limit=max 消息区底部 120px", e1, chat[-120:])
t0 = time.perf_counter()
np.array_equal(chat, chat.copy())
print(f"{'帧 diff (无变化跳过 OCR)':<28} {(time.perf_counter() - t0) * 1000:12.1f} ms")

print("\n消息区识别结果：")
for box, text, score in res or []:
    xc = (box[0][0] + box[2][0]) / 2 / CW
    who = "me" if xc > 0.55 else ("her" if xc < 0.45 else "?")
    print(f"  {who:>3} {score:.2f} {text[:40]}")

# 看一眼抓到的帧（tkinter 内存显示，不走 Image.show() —— 那个会写临时文件）
import tkinter as tk
from PIL import ImageTk

img = Image.fromarray(full)
d = ImageDraw.Draw(img)
d.rectangle([x0, y0, x1, y1], outline=(0, 160, 255), width=2)  # 蓝框=消息区
for box, _, _ in res or []:
    d.polygon([(p[0] + x0, p[1] + y0) for p in box], outline=(255, 0, 0), width=2)
img.thumbnail((1400, 900))
root = tk.Tk()
root.title(f"抓到的帧 {W}x{H}（蓝框=消息区，红框=OCR 行），关掉窗口结束")
photo = ImageTk.PhotoImage(img)
tk.Label(root, image=photo).pack()
root.mainloop()
