# -*- coding: utf-8 -*-
"""把选中的候选填进微信/QQ输入框：写剪贴板 → 点输入框 → Ctrl+V。绝不发回车、绝不点发送。"""
import ctypes
import ctypes.wintypes as w
import time

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32

# 64 位下 ctypes.windll 默认 restype 是 32 位 c_int，而 GlobalAlloc 返回 64 位 HGLOBAL——
# 不声明类型句柄会被截断成垃圾值，GlobalLock(垃圾) 返回 NULL，memmove(NULL,…) 就是
# "access violation writing 0x0"。所有带句柄/指针的函数必须显式声明。
k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
k32.GlobalFree.argtypes = [ctypes.c_void_p]
u32.SetClipboardData.restype = ctypes.c_void_p
u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]


def set_clipboard(text):
    """写剪贴板。剪贴板可能被别的程序占着（剪贴板管理器、截图工具），重试几次。"""
    data = text.encode("utf-16-le") + b"\0\0"
    for attempt in range(10):
        if not u32.OpenClipboard(None):
            time.sleep(0.05)
            continue
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(0x2, len(data))  # GMEM_MOVEABLE
            if not h:
                raise RuntimeError("GlobalAlloc 失败")
            p = k32.GlobalLock(h)
            if not p:
                k32.GlobalFree(h)
                raise RuntimeError("GlobalLock 失败")
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            if not u32.SetClipboardData(13, h):  # CF_UNICODETEXT；成功后句柄归系统，不能 Free
                k32.GlobalFree(h)
                raise RuntimeError(f"SetClipboardData 失败 (attempt {attempt})")
            return
        finally:
            u32.CloseClipboard()
    raise RuntimeError("OpenClipboard 连续失败，剪贴板被其他程序占用")


def input_point(rect, area, chat_app="wechat"):
    """按窗口矩形和消息区计算一个落在编辑区的安全点击点（纯计算，便于测试）。"""
    x0, _, x1, y1 = area
    if chat_app == "qq":
        # QQ 的 y1 是消息区和编辑区的分隔线，y1 后面先是工具栏，再是输入文字区；
        # x0 左边也有表情、剪刀、图片等工具按钮。原来的 x0+70 / y1+48
        # 正好会落在表情按钮上，导致点击「填入」反而打开表情面板。
        # 取编辑区的中部，并给左右两侧的工具/发送按钮留出安全边距。
        input_left = rect.left + x0 + 110
        input_right = rect.right - 180
        input_top = rect.top + y1 + 70
        input_bottom = rect.bottom - 85
        if input_right <= input_left:
            input_left = rect.left + x0 + 30
            input_right = rect.left + x1 - 30
        if input_bottom <= input_top:
            input_top = rect.top + y1 + 35
            input_bottom = rect.bottom - 45
        return (input_left + input_right) // 2, (input_top + input_bottom) // 2
    return rect.left + x0 + 60, rect.top + y1 + 40


def fill(hwnd, area, text, chat_app="wechat"):
    """area = 消息区 (x0, y0, x1, y1)；输入框就在底线 y1 下面。"""
    from app.capture import unminimize

    set_clipboard(text)
    r = w.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # 扩展边界，跟 WGC 帧对齐
        u32.GetWindowRect(hwnd, ctypes.byref(r))
    cx, cy = input_point(r, area, chat_app)
    unminimize(hwnd)

    # SetForegroundWindow 有前台窗口保护，普通后台进程会被拒；AttachThreadInput 绕过
    fg = u32.GetForegroundWindow()
    if fg != hwnd:
        fg_tid = u32.GetWindowThreadProcessId(fg, None)
        our_tid = k32.GetCurrentThreadId()
        u32.AttachThreadInput(our_tid, fg_tid, True)
        u32.SetForegroundWindow(hwnd)
        u32.AttachThreadInput(our_tid, fg_tid, False)
        time.sleep(0.15)  # 给微信一点时间响应前台切换

    old = w.POINT()
    u32.GetCursorPos(ctypes.byref(old))
    u32.SetCursorPos(cx, cy)
    time.sleep(0.05)
    u32.mouse_event(0x2, 0, 0, 0, 0)  # 左键按下
    u32.mouse_event(0x4, 0, 0, 0, 0)  # 抬起
    time.sleep(0.05)
    u32.SetCursorPos(old.x, old.y)
    time.sleep(0.05)
    # 光标移到已有文本的绝对末尾：点击落在文字中间时 caret 会插在中间，
    # 连续多次填入就串行错乱；Ctrl+End 保证新内容永远追加在最后
    u32.keybd_event(0x11, 0, 0, 0)  # Ctrl 按下
    u32.keybd_event(0x23, 0, 0, 0)  # End 按下（VK_END）
    u32.keybd_event(0x23, 0, 2, 0)  # End 抬起
    u32.keybd_event(0x11, 0, 2, 0)  # Ctrl 抬起
    time.sleep(0.05)
    u32.keybd_event(0x11, 0, 0, 0)  # Ctrl
    u32.keybd_event(0x56, 0, 0, 0)  # V
    u32.keybd_event(0x56, 0, 2, 0)
    u32.keybd_event(0x11, 0, 2, 0)
    # 到此为止。发不发、改不改，人来。
