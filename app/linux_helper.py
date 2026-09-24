# -*- coding: utf-8 -*-
"""Linux 采集/填入助手。必须用带 PyGObject 的系统 Python 跑（Ubuntu 上是 /bin/python3）。

本进程只做三件事，截图不落盘：
  find        用 AT-SPI 找官方 Linux 微信主窗口，JSON 打到 stdout
  capture     Mutter ScreenCast RecordArea + PipeWire 帧，二进制打到 stdout
  fill x y    stdin 读文本 → wl-copy → 点输入框 → Ctrl+End → Ctrl+V，不按发送

父进程是 3.12 venv（RapidOCR 封顶 <3.13），系统 Python 才有 gi，所以拆开。
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time

FRAME_MAGIC = b"JEVF"
STATUS_MAGIC = b"JEVS"


def _gi():
    import gi

    gi.require_version("Gio", "2.0")
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, Gio, GLib

    return Atspi, Gio, GLib


def ensure_atspi(Atspi, Gio, GLib):
    """AT-SPI 走独立 bus，没设 AT_SPI_BUS_ADDRESS 时 gi 连不上。"""
    if os.environ.get("AT_SPI_BUS_ADDRESS"):
        Atspi.init()
        return
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    proxy = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.NONE, None,
        "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", None,
    )
    addr = proxy.call_sync("GetAddress", None, Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
    os.environ["AT_SPI_BUS_ADDRESS"] = addr
    Atspi.init()


def _showing(acc, Atspi):
    """无障碍树上锁屏文案解锁后还在，只是被藏起来（0×0、没有 SHOWING）。只认真正画出来的。"""
    try:
        st = acc.get_state_set()
        if not st.contains(Atspi.StateType.SHOWING):
            return False
    except Exception:
        return False
    try:
        ext = acc.get_extents(Atspi.CoordType.SCREEN)
        return ext.width > 8 and ext.height > 8
    except Exception:
        return True


def _locked_in(acc, Atspi, depth=0):
    try:
        name = acc.get_name() or ""
    except Exception:
        return False
    if "已被锁定" in name and _showing(acc, Atspi):
        return True
    if depth >= 6:
        return False
    try:
        n = acc.get_child_count()
    except Exception:
        return False
    for i in range(min(n, 24)):
        ch = acc.get_child_at_index(i)
        if ch and _locked_in(ch, Atspi, depth + 1):
            return True
    return False


def find_wechat(Atspi):
    """官方 Linux 微信主窗口：AT-SPI 应用名 wechat、frame 标题「微信」。"""
    desktop = Atspi.get_desktop(0)
    found = []
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        name = (app.get_name() or "").lower()
        if name not in ("wechat", "weixin") and "wechat" not in name:
            continue
        if app.get_child_count() < 1:
            continue
        frame = app.get_child_at_index(0)
        title = frame.get_name() or ""
        ext = frame.get_extents(Atspi.CoordType.SCREEN)
        found.append({
            "pid": int(app.get_process_id() or 0),
            "x": int(ext.x), "y": int(ext.y),
            "w": int(ext.width), "h": int(ext.height),
            "title": title,
            "locked": bool(_locked_in(frame, Atspi)),
        })
    if not found:
        raise RuntimeError("没找到微信窗口。请先打开官方 Linux 微信（/usr/bin/wechat）。")
    return next((w for w in found if w["title"] == "微信"), found[0])


def cmd_find():
    Atspi, Gio, GLib = _gi()
    ensure_atspi(Atspi, Gio, GLib)
    info = find_wechat(Atspi)
    json.dump(info, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def _proxy(bus, Gio, dest, path, iface):
    return Gio.DBusProxy.new_sync(bus, Gio.DBusProxyFlags.NONE, None, dest, path, iface, None)


def _write_status(info):
    payload = json.dumps(info, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(STATUS_MAGIC + struct.pack("<I", len(payload)) + payload)
    sys.stdout.buffer.flush()


def _write_frame(rgb, w, h):
    sys.stdout.buffer.write(FRAME_MAGIC + struct.pack("<III", w, h, len(rgb)) + rgb)
    sys.stdout.buffer.flush()


def _start_stream(Gio, GLib, bus, x, y, w, h):
    sc = _proxy(bus, Gio, "org.gnome.Mutter.ScreenCast",
                "/org/gnome/Mutter/ScreenCast", "org.gnome.Mutter.ScreenCast")
    sess_path = sc.call_sync(
        "CreateSession", GLib.Variant("(a{sv})", ({},)),
        Gio.DBusCallFlags.NONE, 5000, None,
    ).unpack()[0]
    sess = _proxy(bus, Gio, "org.gnome.Mutter.ScreenCast", sess_path,
                  "org.gnome.Mutter.ScreenCast.Session")
    props = {"cursor-mode": GLib.Variant("u", 0)}  # hidden，别把光标画进帧
    stream_path = sess.call_sync(
        "RecordArea",
        GLib.Variant("(iiiia{sv})", (int(x), int(y), int(w), int(h), props)),
        Gio.DBusCallFlags.NONE, 5000, None,
    ).unpack()[0]
    stream = _proxy(bus, Gio, "org.gnome.Mutter.ScreenCast", stream_path,
                    "org.gnome.Mutter.ScreenCast.Stream")
    node = {"id": None}

    def on_signal(_p, _sender, signal, params):
        if signal == "PipeWireStreamAdded":
            node["id"] = params.unpack()[0]

    stream.connect("g-signal", on_signal)
    sess.call_sync("Start", None, Gio.DBusCallFlags.NONE, 8000, None)
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + 5
    while node["id"] is None and time.monotonic() < deadline:
        ctx.iteration(True)
    if node["id"] is None:
        try:
            sess.call_sync("Stop", None, Gio.DBusCallFlags.NONE, 2000, None)
        except Exception:
            pass
        raise RuntimeError("Mutter ScreenCast 没有给出 PipeWire 节点")
    return sess, node["id"]


def _stop_session(sess, Gio, GLib):
    try:
        sess.call_sync("Stop", None, Gio.DBusCallFlags.NONE, 2000, None)
    except Exception:
        pass


def cmd_capture():
    """持续往 stdout 写帧。RecordArea 截的是屏幕上那块矩形（被挡住就会拍到挡着的窗口）。"""
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Atspi, Gio, GLib = _gi()
    ensure_atspi(Atspi, Gio, GLib)
    Gst.init(None)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    ctx = GLib.MainContext.default()

    sess = pipeline = sink = None
    last_rect = None
    last_lock = None
    try:
        while True:
            try:
                info = find_wechat(Atspi)
            except RuntimeError as e:
                raise RuntimeError(str(e)) from e
            rect = (info["x"], info["y"], info["w"], info["h"])
            if info["w"] < 50 or info["h"] < 50:
                time.sleep(0.3)
                continue
            if last_lock is not info["locked"]:
                last_lock = info["locked"]
                _write_status(info)
            if sess is None or rect != last_rect:
                if sess is not None:
                    if pipeline is not None:
                        pipeline.set_state(Gst.State.NULL)
                    _stop_session(sess, Gio, GLib)
                    sess = pipeline = sink = None
                if rect[2] < 50 or rect[3] < 50:
                    time.sleep(0.3)
                    continue
                sess, node = _start_stream(Gio, GLib, bus, *rect)
                last_rect = rect
                desc = (
                    f"pipewiresrc path={node} ! videoconvert ! "
                    "video/x-raw,format=RGB ! appsink name=sink max-buffers=2 drop=true"
                )
                pipeline = Gst.parse_launch(desc)
                sink = pipeline.get_by_name("sink")
                pipeline.set_state(Gst.State.PLAYING)
                _write_status(info)

            while ctx.pending():
                ctx.iteration(False)
            sample = sink.emit("try-pull-sample", 50 * Gst.MSECOND)
            if not sample:
                time.sleep(0.01)
                continue
            buf = sample.get_buffer()
            caps = sample.get_caps()
            s = caps.get_structure(0)
            w, h = s.get_value("width"), s.get_value("height")
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                raw = bytes(mapinfo.data)
            finally:
                buf.unmap(mapinfo)
            expected = w * h * 3
            if len(raw) != expected:
                if h <= 0 or len(raw) % h != 0:
                    continue
                stride = len(raw) // h
                row = w * 3
                if stride < row:
                    continue
                tight = bytearray(expected)
                for y in range(h):
                    o, s = y * row, y * stride
                    tight[o:o + row] = raw[s:s + row]
                raw = bytes(tight)
            if info["w"]:
                info = dict(info)
                info["scale"] = round(w / info["w"], 3)
            _write_frame(raw, w, h)
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        if sess is not None:
            _stop_session(sess, Gio, GLib)


def _combo(Atspi, mod, keyval, keystr):
    Atspi.generate_keyboard_event(mod, None, Atspi.KeySynthType.PRESS)
    time.sleep(0.02)
    Atspi.generate_keyboard_event(keyval, keystr, Atspi.KeySynthType.PRESSRELEASE)
    time.sleep(0.02)
    Atspi.generate_keyboard_event(mod, None, Atspi.KeySynthType.RELEASE)


def cmd_fill(cx, cy, text):
    """点输入框、把已有光标挪到末尾、粘贴。绝不发回车。"""
    Atspi, Gio, GLib = _gi()
    ensure_atspi(Atspi, Gio, GLib)
    if not text:
        raise RuntimeError("没有要填入的文字")
    try:
        subprocess.run(
            ["wl-copy", "--type", "text/plain"],
            input=text.encode("utf-8"), check=True, timeout=3,
        )
    except FileNotFoundError as e:
        raise RuntimeError("需要 wl-copy（包 wl-clipboard）才能填入") from e
    time.sleep(0.05)
    # 先点微信标题栏把键盘焦点从助手抢走，否则 Ctrl+V 会粘进 Jev，
    # 而助手主线程若还在等本进程，合成键会把两边卡死。
    try:
        w = find_wechat(Atspi)
        tx = int(w["x"] + min(80, max(w["w"] // 4, 24)))
        ty = int(w["y"] + 12)
        Atspi.generate_mouse_event(tx, ty, "b1c")
        time.sleep(0.12)
    except Exception:
        pass
    Atspi.generate_mouse_event(int(cx), int(cy), "abs")
    time.sleep(0.03)
    Atspi.generate_mouse_event(int(cx), int(cy), "b1c")
    time.sleep(0.08)
    _combo(Atspi, 0xFFE3, 0xFF57, None)  # Ctrl+End：Control_L + End
    time.sleep(0.05)
    _combo(Atspi, 0xFFE3, 0x0076, "v")  # Ctrl+V


def cmd_unminimize():
    """GNOME 上最小化的 Wayland 窗口没法靠坐标还原，唤起 desktop 让它自己单例拉起来。"""
    Atspi, Gio, GLib = _gi()
    try:
        info = Gio.DesktopAppInfo.new("wechat.desktop")
        if info is not None:
            info.launch([], None)
            return
    except Exception:
        pass
    subprocess.Popen(["gtk-launch", "wechat"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print("usage: linux_helper.py find|capture|fill X Y|unminimize", file=sys.stderr)
        return 2
    cmd = argv[1]
    try:
        if cmd == "find":
            cmd_find()
        elif cmd == "capture":
            cmd_capture()
        elif cmd == "fill":
            if len(argv) < 4:
                raise RuntimeError("fill 需要屏幕坐标 x y，文本走 stdin")
            cmd_fill(int(argv[2]), int(argv[3]), sys.stdin.read())
        elif cmd == "unminimize":
            cmd_unminimize()
        else:
            raise RuntimeError("未知命令: " + cmd)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
