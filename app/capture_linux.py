# -*- coding: utf-8 -*-
"""Linux：AT-SPI 找官方微信窗口，采集子进程用系统 Python 跑 Mutter ScreenCast。"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import threading
import time

import numpy as np

from app.linux_helper import FRAME_MAGIC, STATUS_MAGIC

_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "linux_helper.py")


def gi_python():
    """RapidOCR 卡在 3.12，PyGObject 在 Ubuntu 系统 Python 上。采集/填入走这一份。"""
    env = os.environ.get("JEV_GI_PYTHON")
    for cand in (env, "/bin/python3.14", "/usr/bin/python3", "/bin/python3"):
        if cand and os.path.isfile(cand):
            return cand
    raise RuntimeError("找不到带 PyGObject 的系统 Python（/bin/python3）")


def _run_helper(args, **kw):
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    try:
        return subprocess.run(
            [gi_python(), _HELPER, *args], env=env,
            stdout=kw.get("stdout", subprocess.PIPE),
            stderr=kw.get("stderr", subprocess.PIPE),
            input=kw.get("input"), timeout=kw.get("timeout", 8), check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("操作超时：" + " ".join(args))


def find_wechat_hwnd():
    r = _run_helper(["find"])
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    if r.returncode != 0:
        raise RuntimeError(err or "没找到微信窗口，请确认官方 Linux 微信已打开")
    info = json.loads(r.stdout.decode("utf-8"))
    if not info.get("w") or not info.get("h"):
        raise RuntimeError("微信窗口不可见（最小化了？），请把微信窗口显示出来")
    return info


def unminimize(hwnd):
    """Wayland 上最小化窗口没有画面。唤起 wechat.desktop，单例进程会自己把窗口拉回来。"""
    if isinstance(hwnd, dict) and hwnd.get("w", 1) > 50 and hwnd.get("h", 1) > 50:
        return False
    _run_helper(["unminimize"], timeout=5)
    return True


def fill(hwnd, area, text):
    """area 是采集帧里的消息区像素；AT-SPI 窗口坐标是逻辑像素，HiDPI 要除 scale。"""
    geom = find_wechat_hwnd()
    unminimize(geom)
    scale = float((hwnd or {}).get("scale") or geom.get("scale") or 1) or 1.0
    x0, _, _, y1 = area
    cx = int(geom["x"] + (x0 + 60) / scale)
    cy = int(geom["y"] + (y1 + 40) / scale)
    data = text.encode("utf-8") if isinstance(text, str) else text
    r = _run_helper(["fill", str(cx), str(cy)], input=data, timeout=8)
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    if r.returncode != 0:
        raise RuntimeError(err or "填入失败")


class Capture:
    """Mutter RecordArea + PipeWire。接口跟 Windows 那份 Capture 对齐：settled / alive / stop / wait。
    截的是屏幕上微信那块矩形，被别的窗口挡住就会拍到挡着的东西——悬浮窗别叠在微信上。"""

    def __init__(self, hwnd, settle=0.25, max_wait=1.0):
        from app.capture import chat_area

        self.settle, self.max_wait = settle, max_wait
        self.shape = self.area = self.last = self.pending = None
        self.t = self.t0 = 0.0
        self.scale = float((hwnd or {}).get("scale") or 1) if isinstance(hwnd, dict) else 1.0
        self.geom = dict(hwnd) if isinstance(hwnd, dict) else {}
        self._chat_area = chat_area
        self._err = None
        self._lock = threading.Lock()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [gi_python(), _HELPER, "capture"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._err_thread = threading.Thread(target=self._drain_err, daemon=True)
        self._thread.start()
        self._err_thread.start()

    def _drain_err(self):
        buf = b""
        try:
            while True:
                chunk = self.proc.stderr.read(256)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 4000:
                    buf = buf[-2000:]
        except Exception:
            pass
        if buf and self._err is None:
            self._err = buf.decode("utf-8", "replace").strip()[-200:]

    def _read(self):
        out = self.proc.stdout

        def exact(n):
            buf = b""
            while len(buf) < n:
                chunk = out.read(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            return buf

        try:
            while True:
                head = exact(4)
                if not head:
                    break
                if head == FRAME_MAGIC:
                    rest = exact(12)
                    if rest is None:
                        break
                    w, h, size = struct.unpack("<III", rest)
                    rgb = exact(size)
                    if rgb is None:
                        break
                    full = np.frombuffer(rgb, dtype=np.uint8).reshape(h, w, 3).copy()
                    self._on_frame(full)
                elif head == STATUS_MAGIC:
                    rawn = exact(4)
                    if rawn is None:
                        break
                    n = struct.unpack("<I", rawn)[0]
                    payload = exact(n)
                    if payload is None:
                        break
                    try:
                        self.geom = json.loads(payload.decode("utf-8"))
                    except Exception:
                        pass
                else:
                    # 协议乱了，丢掉这个字节继续找魔数没有意义，停掉让上层重开
                    self._err = self._err or "采集流协议错乱"
                    break
        except Exception as e:
            self._err = str(e)
        if self.proc.poll() not in (None, 0) and not self._err:
            self._err = "采集进程退出 %s" % self.proc.returncode

    def _on_frame(self, full):
        if full.max() == 0:
            return
        if self.geom.get("w"):
            self.scale = full.shape[1] / self.geom["w"]
            self.geom["scale"] = round(self.scale, 3)
        with self._lock:
            if self.area is None or full.shape != self.shape:
                self.shape, self.area = full.shape, self._chat_area(full, scale=self.scale)
            if self.area is None:
                return
            x0, y0, x1, y1 = self.area[:4]
            chat = full[y0:y1, x0:x1]
            if self.last is not None and np.array_equal(chat, self.last):
                return
            self.last = chat
            if self.pending is None:
                self.t0 = time.perf_counter()
            self.pending, self.t = full, time.perf_counter()

    def settled(self):
        with self._lock:
            if self.pending is None:
                return None
            now = time.perf_counter()
            if now - self.t < self.settle and now - self.t0 < self.max_wait:
                return None
            full, self.pending = self.pending, None
            return full

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def wait(self):
        self.proc.wait()
        if self.proc.returncode not in (0, None, -15, 15) and self._err:
            raise RuntimeError(self._err)
        if self._err and self.proc.returncode not in (0, None, -15, 15):
            raise RuntimeError(self._err)
