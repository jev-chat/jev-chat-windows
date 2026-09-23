# -*- coding: utf-8 -*-
"""浅色置顶回复助手：回复建议和独立设置页。发送始终由用户在微信确认。"""
import ctypes
import ctypes.wintypes
import json
import threading
from datetime import datetime
from math import isfinite
from types import SimpleNamespace

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QSizeGrip, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CardWidget, CheckBox, ComboBox, DoubleSpinBox, EditableComboBox, FluentIcon as FIF,
    HyperlinkButton, IndeterminateProgressBar, LineEdit, PasswordLineEdit,
    PrimaryPushButton, PushButton, ScrollArea, SpinBox, SwitchButton, Theme, TransparentToolButton,
    setCustomStyleSheet, setFont, setTheme, setThemeColor,
)

from app import settings
from app.noise import is_system_noise
from app.version import VERSION
from core import jev_client, llm, providers
from core.questions import CHOICE_LABELS

_LOG_LINES = 300
_MUTED = "#68776f"
_GREEN = "#18794e"
_RELATIONSHIPS = [
    ("恋人", "romantic partners"), ("朋友", "friends"), ("同事", "colleagues"),
    ("家人", "family"), ("自定义", None),
]


def _choice(answers, name):
    return CHOICE_LABELS[name].get((answers.get(name) or {}).get("choice"), "暂未判断")


def judgment_lines(result):
    """一轮分析结果 → Jev 气泡的多行明细（微信内浮层用）。
    不带「推荐」行：三条候选就在下面的绿泡里（✓ 已标出），重复一遍反而像多出一条不相干的。"""
    answers = result.get("answers") or {}
    lines = [f"意图 · {_choice(answers, 'true_intent')}",
             f"建议 · {_choice(answers, 'best_action')}",
             f"需要 · {_choice(answers, 'she_needs')}"]
    score = (answers.get("danger_level") or {}).get("score")
    if isinstance(score, (int, float)) and isfinite(score) and 0 <= score <= 9:
        lines.append(f"紧张度 {score:.0f}/9")
    return lines


def judgment_text(result):
    """一轮分析结果 → 「Jev 判断」气泡的摘要文字：意图 / 建议 / 需要 / 紧张度一行，推荐候选一行。"""
    answers = result.get("answers") or {}
    head = (f"意图 {_choice(answers, 'true_intent')} · 建议 {_choice(answers, 'best_action')}"
            f" · 需要 {_choice(answers, 'she_needs')}")
    score = (answers.get("danger_level") or {}).get("score")
    if isinstance(score, (int, float)) and isfinite(score) and 0 <= score <= 9:
        head += f" · 紧张度 {score:.0f}/9"
    lines = [head]
    cands = result.get("candidates") or []
    best = result.get("best_index", 0)
    if cands and best in range(len(cands)):
        tip = f"推荐：「{cands[best]}」"
        raw = result.get("scores") or []
        p = raw[best] if best < len(raw) else None
        if isinstance(p, (int, float)) and p:
            tip += f" {round(p * 100)}%"
        lines.append(tip)
    return "\n".join(lines)


def _label(text="", size=14, color=None, bold=False, parent=None):
    label = BodyLabel(text, parent)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    setFont(label, size, QFont.DemiBold if bold else QFont.Normal)
    if color:
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(label, qss, qss)
    return label


def _tool(icon, title, callback, parent=None):
    button = TransparentToolButton(icon, parent)
    button.setFixedSize(32, 32)
    button.setToolTip(title)
    button.setAccessibleName(title)
    button.clicked.connect(callback)
    return button


class _Surface(CardWidget):
    def __init__(self, parent=None, accent=False):
        self.accent = accent
        super().__init__(parent)
        self.setBorderRadius(12)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def _normalBackgroundColor(self):
        return QColor("#edf7f0" if self.accent else "#ffffff")

    def _hoverBackgroundColor(self):
        return self._normalBackgroundColor()

    def _pressedBackgroundColor(self):
        return self._normalBackgroundColor()


class _Fetched(QObject):
    """取模型列表的后台线程 → 主线程：哪一组（SimpleNamespace）、取回来的模型 id、失败原因（成功是空串）。
    Qt 不让跨线程碰控件，信号是跨线程唯一干净的路。"""
    done = Signal(object, list, str)


class _Tested(QObject):
    """配置测试的后台线程 → 主线程：哪一组、成功否、给人看的说明。"""
    done = Signal(object, bool, str)


class _TitleBar(QWidget):
    """只有标题栏可拖动，选择正文或按按钮不会意外移动窗口。
    拖完回调一下：悬浮窗从「吸附跟随」切到「用户钉住了」，不再自动挪，直到重新保存设置。"""
    def __init__(self, parent, on_drag_end=None):
        super().__init__(parent)
        self._drag = None
        self._on_end = on_drag_end

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.window().pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag is not None and self._on_end:
            self._on_end()
        self._drag = None
        super().mouseReleaseEvent(event)


class _MainWindow(QWidget):
    """窗口大小变了就叫 Overlay 重新排布；断点没跨过时 _relayout 自己不做事，这里不用防抖。"""
    def __init__(self, relayout):
        super().__init__()
        self._relayout = relayout

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout(event.size().width(), event.size().height())


class _ReplyCard(_Surface):
    def __init__(self, owner, index, recommended=False, number=1, score=None):
        super().__init__(accent=recommended)
        box = QVBoxLayout(self)
        self.box = box
        box.setSpacing(10)
        top = QHBoxLayout()
        label = "推荐回复" if recommended else f"备选 {number}"
        if score is not None:
            label += f" · {round(score * 100)}%"
        top.addWidget(_label(label, 12, _GREEN if recommended else _MUTED, True))
        self.copyButton = _tool(FIF.COPY, "复制这条回复", lambda: owner._copy(index), self)
        self.copyButton.setFixedSize(24, 24)
        top.addWidget(self.copyButton)
        box.addLayout(top)
        self.text = _label(owner.cands[index], 15)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(self.text)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.fillButton = (PrimaryPushButton if recommended else PushButton)("填入微信", self)
        self.fillButton.setAccessibleName(f"填入{'推荐回复' if recommended else f'备选 {number}'}到微信")
        self.fillButton.clicked.connect(lambda: owner._fill(index))
        bottom.addWidget(self.fillButton)
        box.addLayout(bottom)
        self.set_compact(owner._compact)

    def set_available(self, enabled):
        self.fillButton.setEnabled(enabled)
        self.copyButton.setEnabled(enabled)

    def set_compact(self, compact):
        self.box.setContentsMargins(12, 8, 12, 8) if compact else self.box.setContentsMargins(16, 12, 16, 12)
        self.fillButton.setMinimumWidth(80 if compact else 100)


class _BubbleLayer(QWidget):
    """透明浮层：铺在微信消息区上，照着聊天软件的样子画气泡——
    对方消息（白，左）→ Jev 判断（灰，左，多行明细）→ 三条候选（绿，右，可点即填入）。
    无任务栏图标、点击不抢焦点；Z 序由 main 钉在微信正上方，跟微信同视觉层。
    可整块拖拽：松手保存相对默认锚点的偏移（settings.bubble_offset），之后每拍定位都用它。"""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.SizeAllCursor)  # 提示整块可拖
        self._drag_off = None  # 拖拽中：按下的全局位置 - 窗口位置
        self._dragging = False  # 拖拽中 place() 不许抢位置
        self._anchor = None  # 默认锚点（拖拽偏移的参照，place 每次刷新）
        self.refresh_btn = None  # set_content 里创建
        self._spin_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spin_i = 0
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(120)
        self._spin_timer.timeout.connect(self._spin_tick)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(6, 4, 6, 6)
        self.box.setSpacing(8)
        self.box.addStretch(1)  # 气泡都沉底，像刚发生的对话

    def set_generating(self, busy):
        """生成中：按钮转菊花动画并禁用（点了有反应看得见），结束自动恢复。"""
        if self.refresh_btn is None:
            return
        if busy:
            self.refresh_btn.setEnabled(False)
            self._spin_i = 0
            self.refresh_btn.setText(f"{self._spin_frames[0]} 生成中")  # 立刻有反馈，不等第一拍
            self._spin_timer.start()
        else:
            self._spin_timer.stop()
            self.refresh_btn.setEnabled(True)
            self.refresh_btn.setText("↻ 重新生成")

    def _spin_tick(self):
        if self.refresh_btn is not None and not self.refresh_btn.isEnabled():
            self._spin_i = (self._spin_i + 1) % len(self._spin_frames)
            self.refresh_btn.setText(f"{self._spin_frames[self._spin_i]} 生成中")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_off = e.globalPosition().toPoint() - self.pos()
            self._dragging = True
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._dragging and self._drag_off is not None:
            self.move(e.globalPosition().toPoint() - self._drag_off)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._dragging:
            self._dragging = False
            anchor = self._anchor or (self.x(), self.y())
            self.owner.save_bubble_offset(self.x() - anchor[0], self.y() - anchor[1])
        super().mouseReleaseEvent(e)

    def _clear(self):
        """清空重建。注意 count()>1 的写法是错的：第一个 item 是 stretch 弹簧（没有 widget），
        先把它弹出去就会永远留下最后一轮的旧面板——上一轮结果残留就是这么来的。"""
        while self.box.count():
            item = self.box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.box.addStretch(1)  # 弹簧放回末尾：面板永远沉底

    @staticmethod
    def _bubble_card(bg, border, max_w):
        card = QFrame()
        card.setObjectName("bubbleCard")
        qss = f"QFrame#bubbleCard {{ background: {bg}; border-radius: 4px 12px 12px 12px;"
        if border:
            qss += f" border: 1px solid {border};"
        card.setStyleSheet(qss + " }")
        card.setMaximumWidth(max_w)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(9, 5, 9, 6)
        inner.setSpacing(1)
        return card, inner

    def _row(self, card, right=False):
        """一行气泡：默认靠左（对方/Jev），right=True 靠右（我的候选）。"""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if right:
            row.addStretch(1)
            row.addWidget(card)
        else:
            row.addWidget(card)
            row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        self.box.addWidget(wrap)

    @staticmethod
    def _norm(t):
        import re
        return re.sub(r"[\s\W_]+", "", t or "").lower()

    def set_content(self, her, lines, result):
        """全部内容合并成**一块**半透明面板（不再分散）：
        对方最新消息 → Jev 分析 → 三条候选（每行可点，✓ 为推荐），中间细分隔线。
        对方原文永远显示；复读对方消息的候选直接不画。"""
        self._clear()
        panel_w = max(240, (self.width() or 280) - 4)
        her_n = self._norm(her)

        panel = QFrame()
        panel.setObjectName("bubbleCard")
        panel.setStyleSheet(
            "QFrame#bubbleCard { background: rgba(248, 250, 248, 205);"
            " border: 1px solid rgba(0, 0, 0, 25); border-radius: 10px; }")
        panel.setMaximumWidth(panel_w)
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(7, 5, 7, 4)
        inner.setSpacing(1)

        if her:
            head = _label("对方", 10, "#8a9a90", True)
            inner.addWidget(head)
            # 原文只显示前 50 字：面板高度有限，塞下全文会把候选挤出可视区
            her_show = her if len(her) <= 50 else her[:50] + "…"
            her_label = _label(her_show, 12, "#233c2f")
            her_label.setToolTip(her)  # 完整内容悬停可看
            inner.addWidget(her_label)
            inner.addWidget(self._divider())

        # Jev 抬头行：左边标题，右边小「重新生成」按钮（点击不抢焦点，走首页同一条刷新链路）
        jrow = QHBoxLayout()
        jrow.setContentsMargins(0, 0, 0, 0)
        jrow.setSpacing(4)
        jrow.addWidget(_label("Jev:", 10, "#68776f", True))
        jrow.addStretch(1)
        refresh = PushButton("↻ 重新生成", self)
        refresh.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 6px;"
            " padding: 1px 6px; font-size: 10px; color: #68776f; }"
            "QPushButton:hover { background: rgba(149, 236, 105, 90); color: #18794e; }")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.setToolTip("用当前聊天记录再生成一轮建议")
        refresh.clicked.connect(lambda _=False: self.owner._refresh_clicked())
        jrow.addWidget(refresh)
        self.refresh_btn = refresh
        inner.addLayout(jrow)
        for text in lines:
            inner.addWidget(_label(text, 12, "#42574a"))
        if result.get("candidates"):
            inner.addWidget(self._divider())

        cands = result.get("candidates") or []
        best = result.get("best_index", 0)
        raw = result.get("scores") or []
        order = sorted(range(len(cands)), key=lambda i: (i != best, -(raw[i] if i < len(raw) else 0), i))
        shown = 0
        for index in order:
            if shown >= 3:
                break
            cand_n = self._norm(cands[index])
            if her_n and (cand_n in her_n or her_n in cand_n):
                continue  # 复读对方消息的候选直接不画
            score = raw[index] if index < len(raw) else None
            circ = "①②③④⑤"[shown]  # 圆序号按显示顺序
            mark = "✓" if index == best else ""
            pct = f"{round(score * 100)}%" if score else ""

            row = QFrame()
            row.setObjectName("candRow")
            row.setCursor(Qt.PointingHandCursor)
            row.setStyleSheet(
                "QFrame#candRow { background: transparent; border-radius: 6px; }"
                "QFrame#candRow:hover { background: rgba(149, 236, 105, 90); }")
            rbox = QHBoxLayout(row)
            rbox.setContentsMargins(5, 2, 5, 2)
            rbox.setSpacing(4)
            # 四列：对勾（固定宽、只有推荐行有）→ 圆序号（固定宽左对齐）→ 文字（伸缩）→ 百分比（右对齐）
            mark_label = _label(mark, 12, "#18794e", True)
            mark_label.setFixedWidth(12)
            mark_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            rbox.addWidget(mark_label)
            circ_label = _label(circ, 12, "#233c2f")
            circ_label.setFixedWidth(16)
            circ_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            rbox.addWidget(circ_label)
            # 候选超过 60 字截断（悬停看全文）：换行两行内保证三条候选都在可视区
            cand_show = cands[index] if len(cands[index]) <= 60 else cands[index][:60] + "…"
            text_label = _label(cand_show, 12, "#233c2f")
            text_label.setToolTip(cands[index])
            text_label.setCursor(Qt.PointingHandCursor)
            rbox.addWidget(text_label, 1)
            pct_label = _label(pct, 11, "#68776f")
            pct_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)  # 百分比右对齐
            pct_label.setCursor(Qt.PointingHandCursor)
            rbox.addWidget(pct_label)
            # 整行接收点击（行是 QFrame 不吃鼠标，手动转发到 _fill）
            row.mousePressEvent = lambda _e, i=index: self._fill(i)
            inner.addWidget(row)
            shown += 1
        if shown or True:  # 面板始终只有一块
            self.box.addWidget(panel)
        # 固定用 place 算好的尺寸：adjustSize 会把窗撑回内容高
        self.resize(*getattr(self, "_placed", (self.width(), self.height())))

    @staticmethod
    def _divider():
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: rgba(0, 0, 0, 22); border: none;")
        return line

    def _fill(self, index):
        """点候选气泡：直接走填入回调（点击不抢焦点，微信保持前台）。"""
        result = self.owner._bubble_result
        if not result:
            return
        cands = result.get("candidates") or []
        if index >= len(cands):
            return
        try:
            self.owner.on_fill(cands[index])
        except Exception:
            pass

    def place(self, area):
        """按微信消息区矩形（屏幕物理 px）把自己固定在聊天区**右上**：
        贴右缘 2px、完全贴住聊天区上边缘，高度不超过消息区高的一半——
        不遮下方自己刚发的消息。默认锚点之外叠加用户拖出的偏移
        （settings.bubble_offset，松手即存，重启不变）。拖拽进行中不抢位置。"""
        x0, y_top, x1, y_in = area
        dpr = QApplication.instance().primaryScreen().devicePixelRatio() or 1.0
        w = max(216, min(316, round((x1 - x0) / dpr) - 36))  # 比消息区窄 2 个中文字符
        # 高度 = 消息区高度 - 120：底部留出自己最新的消息和输入框，其余全给面板（原文不再被挤掉）
        avail_h = round((y_in - y_top) / dpr)
        h = min(290, max(160, avail_h - 120))
        self._placed = (w, h)
        x = round(x1 / dpr) - w - 2
        y = round(y_top / dpr)  # 完全贴住聊天区上边缘
        self._anchor = (x, y)
        off = settings.bubble_offset()
        if off:
            x, y = x + round(off[0]), y + round(off[1])
        screen = self.screen().availableGeometry()
        x = max(screen.left(), min(screen.right() + 1 - w, x))  # 偏移也不许拖出屏幕
        y = max(screen.top(), min(screen.bottom() + 1 - h, y))
        if not self._dragging:  # 拖拽中每 250ms 的自动定位闭嘴，松手后按新偏移接手
            self.resize(w, h)
            self.move(x, y)
        if not self.isVisible():
            self.show()

class Overlay:
    def __init__(self, on_fill, on_toggle_capture=None, on_target_change=None, result_of=None,
                 on_toggle_debug=None, on_refresh=None):
        """result_of(会话名) → 那个会话上次的结果或 None；切着看别的会话时用它把旧结果放回来。
        on_target_change(会话名, 人名) → 用户在群里挑了回复对象。
        on_toggle_debug(开不开) → 开关调试视图那个独立窗口。
        on_refresh() → 首页「重新生成」按钮：用当前会话已有记录再跑一轮分析。"""
        self.app = QApplication.instance() or QApplication([])
        setTheme(Theme.LIGHT)
        setThemeColor(_GREEN, save=False)
        self.on_fill = on_fill
        self.on_toggle_capture = on_toggle_capture
        self.on_target_change = on_target_change
        self.on_toggle_debug = on_toggle_debug
        self.on_refresh = on_refresh
        self.result_of = result_of
        self.cands = []
        self.cards = []
        self._busy = False
        self._current = False
        self._compact = None  # 断点模式：None 保证 _relayout 第一次调用必定生效
        self._pageLayouts = []
        self._hintLabels = []
        self.feeds = {}  # {会话名: [排好版的记录]}
        self.counts = {}  # {会话名: 消息条数}
        self.hers = {}  # {会话名: 对方最近一句}
        self.targets = {}  # {会话名: ([发言人], 当前回复对象)}
        self._chat = ""  # 微信当前开着的会话
        self._shown = ""  # 界面上正在看的会话（浏览时和上面不一样）
        self._pinned = False  # 用户拖过悬浮窗就停在原地，微信窗口一动自动恢复跟随
        self._pin_rect = None  # 拖走时记下微信矩形，变了就解钉
        self._last_rect = None  # 最近一次见到的微信矩形（磁吸判断用）
        self._border = None  # 悬浮窗自己的不可见边框（物理 px，首次吸附时量）
        self._bubble = None  # 微信聊天区的气泡浮层（懒创建）
        self._bubble_key = None  # 浮层当前内容对应的 (会话, 结果)，变了才重画
        self._bubble_result = None  # 浮层里候选对应的结果（点击填入用）
        self._bubble_errors = {}  # {会话名: 失败原因}——生成失败时浮层显示它，而不是留着旧建议
        self._loading = False  # 回显设置中：开关 setChecked 会触发即时保存，装载期间必须拦住，
        # 不然回显到一半的默认值会把已经存好的设置冲掉（「设置有时候未持久化」的真凶）
        self.win = _MainWindow(self._relayout)
        self.win.setObjectName("assistantWindow")
        self.win.setWindowTitle("Jev · 微信回复助手")
        self.win.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)  # 不置顶：Z 序钉在微信正下方（main.py）
        self.win.setStyleSheet(
            "QWidget#assistantWindow { background: #f5f7f6; border: 1px solid #dce3de; border-radius: 14px; }"
        )
        self.win.setMinimumWidth(320)
        self.win.setMaximumWidth(640)
        outer = QVBoxLayout(self.win)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        header = _TitleBar(self.win, lambda: self._on_drag_end())
        title = QHBoxLayout(header)
        title.setContentsMargins(18, 12, 10, 10)
        title.setSpacing(8)
        name = _label("Jev", 20, "#233c2f", True)
        name.setFixedWidth(40)
        name.setAttribute(Qt.WA_TransparentForMouseEvents)
        title.addWidget(name)
        self.subtitle = _label("微信回复助手", 12, _MUTED)
        self.subtitle.setAttribute(Qt.WA_TransparentForMouseEvents)
        title.addWidget(self.subtitle, 1)
        self.captureSwitch = SwitchButton(header)
        self.captureSwitch.setOnText("采集中")
        self.captureSwitch.setOffText("已暂停")
        self.captureSwitch.setToolTip("开启或暂停微信采集")
        self.captureSwitch.setAccessibleName("开启或暂停微信采集")
        self.captureSwitch.setChecked(True)
        self.captureSwitch.checkedChanged.connect(self._capture_toggled)
        title.addWidget(self.captureSwitch)
        self.settingsButton = _tool(FIF.SETTING, "设置", self.open_settings, header)
        title.addWidget(self.settingsButton)
        title.addWidget(_tool(FIF.REMOVE, "最小化", self.win.showMinimized, header))
        title.addWidget(_tool(FIF.CLOSE, "关闭助手", self.win.close, header))
        outer.addWidget(header)
        self.updateBar = QWidget(self.win)
        update_row = QHBoxLayout(self.updateBar)
        update_row.setContentsMargins(18, 4, 8, 4)
        update_row.setSpacing(8)
        self.updateLabel = _label("", 12, _GREEN, True)
        update_row.addWidget(self.updateLabel, 1)
        self.updateLink = HyperlinkButton("", "去下载", self.updateBar)
        self.updateLink.setFixedHeight(24)
        update_row.addWidget(self.updateLink)
        closeUpdate = TransparentToolButton(FIF.CLOSE, self.updateBar)
        closeUpdate.setFixedSize(20, 20)
        closeUpdate.setToolTip("关闭更新提示")
        closeUpdate.setAccessibleName("关闭更新提示")
        closeUpdate.clicked.connect(lambda: self.updateBar.hide())
        update_row.addWidget(closeUpdate)
        self.updateBar.setFixedHeight(32)
        self.updateBar.hide()
        outer.addWidget(self.updateBar)
        self.pages = QStackedWidget(self.win)
        outer.addWidget(self.pages, 1)
        self._build_home()
        self._build_settings()
        footer = QHBoxLayout()
        footer.setContentsMargins(20, 9, 8, 8)
        footer.addWidget(_label(f"仅填入输入框 · 发送由你确认 · v{VERSION}", 11, _MUTED), 1)
        grip = QSizeGrip(self.win)
        grip.setFixedSize(16, 16)
        footer.addWidget(grip, 0, Qt.AlignBottom)
        outer.addLayout(footer)
        screen = self.app.primaryScreen().availableGeometry()
        self.win.setMinimumHeight(min(360, screen.height() - 32))
        self.win.resize(min(440, screen.width() - 32), min(820, screen.height() - 48))
        self.win.move(screen.right() - self.win.width() - 20, screen.top() + 24)
        self._relayout(self.win.width(), self.win.height())  # resizeEvent 补不到构造时这一次
        self.set_status("等待新消息" if settings.has_key() else "需要配置模型",
                        "idle" if settings.has_key() else "warning")
        self.win.show()

    def _scroll_page(self):
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName("pageContent")
        content.setStyleSheet("QWidget#pageContent { background: transparent; }")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 8, 20, 12)
        layout.setSpacing(14)
        scroll.setWidget(content)
        self.pages.addWidget(scroll)
        self._pageLayouts.append(layout)
        return scroll, layout

    def _relayout(self, w, h):
        """宽度跨过断点才重新摆布局（省事）；高度每次都重算，反正只是设个定高。"""
        compact = w < 400
        if compact != self._compact:
            self._compact = compact
            self._apply_compact(compact)
        self.feed.setFixedHeight(max(100, min(240, int(h * 0.25))))
        self._elide_chat_name()

    def _elide_chat_name(self):
        """会话名过长时显示省略号（内部键仍是全名，切换/存储不受影响）。"""
        if not self._chat:
            return
        width = max(60, self.chatBox.width() - 30)
        self.chatBox.setText(self.chatBox.fontMetrics().elidedText(self._chat, Qt.ElideRight, width))

    def _apply_compact(self, compact):
        """紧凑/常规两套间距和可见性；断点没变时不会被调用。"""
        self.subtitle.setVisible(not compact)
        self.captureSwitch.setOnText("" if compact else "采集中")
        self.captureSwitch.setOffText("" if compact else "已暂停")
        for label in self._hintLabels:
            label.setVisible(not compact)
        self.referenceNote.setVisible(bool(self.cands) and not compact)
        self._sync_model_fields()
        margins = (12, 8, 12, 12) if compact else (20, 8, 20, 12)
        for layout in self._pageLayouts:
            layout.setContentsMargins(*margins)
        for card in self.cards:
            card.set_compact(compact)

    def _build_home(self):
        self.home, body = self._scroll_page()
        heading = QHBoxLayout()
        heading.addWidget(_label("回复建议", 23, "#24382d", True), 1)
        self.updated = _label("", 11, _MUTED)
        self.updated.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        heading.addWidget(self.updated)
        body.addLayout(heading)
        chat_row = QHBoxLayout()
        chat_row.setSpacing(8)
        prefix = _label("当前会话", 12, _MUTED)
        prefix.setFixedWidth(56)
        chat_row.addWidget(prefix)
        self.chatBox = ComboBox()
        self.chatBox.setMinimumWidth(0)  # 别让会话名的长度撑开整行，宽度交给 stretch
        self.chatBox.setPlaceholderText("尚未识别到会话")
        self.chatBox.setAccessibleName("当前会话")
        self.chatBox.setToolTip("微信切到哪个会话这里就跟到哪个；也可以自己选一个，只看它的记录和建议")
        self.chatBox.currentIndexChanged.connect(self._on_chat_selected)
        chat_row.addWidget(self.chatBox, 1)
        self.chatFollow = _label("", 11, _MUTED)
        self.chatFollow.setFixedWidth(52)
        self.chatFollow.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        chat_row.addWidget(self.chatFollow)
        body.addLayout(chat_row)
        self.targetRow = QWidget()  # 只有开了「群聊指定回复对象」且这个会话是群聊才露出来
        target_row = QHBoxLayout(self.targetRow)
        target_row.setContentsMargins(0, 0, 0, 0)
        target_row.setSpacing(8)
        target_prefix = _label("回复对象", 12, _MUTED)
        target_prefix.setFixedWidth(56)
        target_row.addWidget(target_prefix)
        self.targetBox = ComboBox()
        self.targetBox.setMinimumWidth(0)  # 人名长度不定，别让它撑开整行
        self.targetBox.setAccessibleName("回复对象")
        self.targetBox.setToolTip("三条候选都按这个人来写；不选就跟着最近说话的那位")
        self.targetBox.currentIndexChanged.connect(self._on_target_selected)
        target_row.addWidget(self.targetBox, 1)
        self.atCheck = CheckBox("填入时带 @")
        self.atCheck.setChecked(True)
        self.atCheck.setToolTip("填入时在开头加「@名字 」。只是普通文字，微信不会认成真正的 @")
        target_row.addWidget(self.atCheck)
        self.targetRow.hide()
        body.addWidget(self.targetRow)
        self.status = _label("", 12, _MUTED)
        body.addWidget(self.status)
        self.progress = IndeterminateProgressBar()
        self.progress.setFixedHeight(3)
        self.progress.hide()
        body.addWidget(self.progress)
        self.context = QWidget()
        context_box = QVBoxLayout(self.context)
        context_box.setContentsMargins(0, 0, 0, 0)
        context_box.setSpacing(5)
        context_box.addWidget(_label("对方最近说", 11, _MUTED))
        self.latest = _label("", 14, "#42574a")
        self.latest.setTextInteractionFlags(Qt.TextSelectableByMouse)
        context_box.addWidget(self.latest)
        self.context.hide()
        body.addWidget(self.context)

        self.insight = _Surface()
        insight_box = QVBoxLayout(self.insight)
        insight_box.setContentsMargins(14, 12, 14, 12)
        insight_box.setSpacing(7)
        row = QHBoxLayout()
        self.insightTitle = _label("对话参考", 12, _MUTED)
        row.addWidget(self.insightTitle, 1)
        self.tension = _label("", 11)
        self.tension.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.tension)
        insight_box.addLayout(row)
        self.summary = _label("", 14, "#304c3c", True)
        insight_box.addWidget(self.summary)
        self.intent = _label("", 12, _MUTED)
        insight_box.addWidget(self.intent)
        self.insight.setToolTip("根据当前聊天片段推测，可能理解有偏差。紧张度为 0–9 的参考评分。")
        self.insight.hide()
        body.addWidget(self.insight)

        self.empty = _Surface()
        empty_box = QVBoxLayout(self.empty)
        empty_box.setContentsMargins(24, 36, 24, 36)
        empty_box.setSpacing(14)
        symbol = _label("…", 30, _GREEN, True)
        symbol.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(symbol)
        self.emptyTitle = _label("等待对方的新消息", 17, "#304c3c", True)
        self.emptyTitle.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(self.emptyTitle)
        self.emptyHint = _label("保持微信聊天窗口打开。\n收到新消息后，回复建议会出现在这里。", 13, _MUTED)
        self.emptyHint.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(self.emptyHint)
        self.setupButton = PrimaryPushButton("前往设置")
        self.setupButton.clicked.connect(self.open_settings)
        self.setupButton.setVisible(not settings.has_key())
        empty_box.addWidget(self.setupButton, 0, Qt.AlignHCenter)
        if not settings.has_key():
            self.emptyTitle.setText("先设置，再开始")
            self.emptyHint.setText("配置模型和关系背景，\n让建议更贴近你们的对话。")
        body.addWidget(self.empty)
        self.replyBox = QVBoxLayout()
        self.replyBox.setSpacing(10)
        body.addLayout(self.replyBox)
        self.referenceNote = _label("AI 建议仅供参考，按你的语气调整后再发送。", 11, _MUTED)
        self.referenceNote.hide()
        body.addWidget(self.referenceNote)

        actions_row = QHBoxLayout()
        self.historyButton = PushButton(FIF.HISTORY, "聊天记录")
        self.historyButton.setMinimumWidth(0)
        self.historyButton.clicked.connect(self._toggle_history)
        self.historyButton.setAccessibleName("展开或收起聊天记录")
        actions_row.addWidget(self.historyButton, 1)
        self.refreshButton = PushButton(FIF.SYNC, "重新生成")
        self.refreshButton.setMinimumWidth(0)
        self.refreshButton.setToolTip("用当前会话已有的聊天记录再生成一轮建议（生成失败或想换个写法时用）")
        self.refreshButton.setAccessibleName("重新生成回复建议")
        self.refreshButton.clicked.connect(self._refresh_clicked)
        actions_row.addWidget(self.refreshButton)
        body.addLayout(actions_row)
        self.feed = self._build_feed()
        body.addWidget(self.feed)
        self._history_title()
        body.addStretch(1)

    def _build_feed(self):
        """聊天记录区：气泡流。对方消息后跟一条灰色「Jev 判断」泡，像微信聊天那样读。
        滚动条隐藏（首页自己有一根，嵌套出两根很蠢；滚轮照样能滚）。"""
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # 只留首页一根滚动条
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background: #fbfcfb; border: 1px solid #e2e7e3; border-radius: 10px; }")
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        self.feedBox = QVBoxLayout(content)
        self.feedBox.setContentsMargins(10, 10, 10, 10)
        self.feedBox.setSpacing(6)
        self.feedBox.addStretch(1)  # 永远在末尾：气泡少的时候全顶到上面
        scroll.setWidget(content)
        scroll.setFixedHeight(160)
        scroll.hide()
        return scroll

    def _bubble_card(self, bg, border, text, color, size):
        """一个圆角气泡。QLabel 继承 QFrame，样式选择器必须用 objectName 圈死卡片本身，
        不然会把泡里每一个文字标签都画上边框和底色。"""
        card = QFrame()
        card.setObjectName("bubbleCard")
        qss = f"QFrame#bubbleCard {{ background: {bg}; border-radius: 10px;"
        if border:
            qss += f" border: 1px solid {border};"
        card.setStyleSheet(qss + " }")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(10, 7, 10, 8)
        inner.setSpacing(3)
        inner.addWidget(_label(text, size, color))
        return card

    def _append_bubble(self, entry):
        """一条记录 → 气泡控件贴进记录区。对方靠左白底、我靠右绿底、Jev 判断靠左灰底带抬头。"""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        kind = entry["kind"]
        if kind == "jev":
            card = QFrame()
            card.setObjectName("bubbleCard")
            card.setStyleSheet("QFrame#bubbleCard { background: #ecefee; border-radius: 10px; }")
            inner = QVBoxLayout(card)
            inner.setContentsMargins(10, 6, 10, 8)
            inner.setSpacing(3)
            inner.addWidget(_label(f"Jev · {entry['ts']}", 10, _MUTED, True))
            inner.addWidget(_label(entry["text"], 12, "#42574a"))
            row.addWidget(card, 4)
            row.addStretch(1)
        elif kind == "note":
            note = _label(entry["text"], 11, _MUTED)
            note.setAlignment(Qt.AlignCenter)
            row.addStretch(1)
            row.addWidget(note, 4)
            row.addStretch(1)
        else:
            mine = entry["who"] == "me"
            who = "我" if mine else (entry["name"] or "对方")
            head = _label(f"{who} · {entry['ts']}", 10, _MUTED, True)
            card = QFrame()
            card.setObjectName("bubbleCard")
            bg = "#dcf2cd" if mine else "#ffffff"
            border = "" if mine else "1px solid #e2e7e3"
            card.setStyleSheet(
                f"QFrame#bubbleCard {{ background: {bg}; border-radius: 10px; border: {border}; }}")
            inner = QVBoxLayout(card)
            inner.setContentsMargins(10, 6, 10, 8)
            inner.setSpacing(3)
            inner.addWidget(head)
            inner.addWidget(_label(entry["text"], 13, "#233c2f"))
            if mine:
                row.addStretch(1)
                row.addWidget(card, 4)
            else:
                row.addWidget(card, 4)
                row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        self.feedBox.insertWidget(self.feedBox.count() - 1, wrap)
        return wrap

    def _clear_feed(self):
        while self.feedBox.count() > 1:  # 留着末尾那个 stretch
            item = self.feedBox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _scroll_feed_bottom(self):
        bar = self.feed.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _build_settings(self):
        self.settingsPage, body = self._scroll_page()
        heading = QHBoxLayout()
        heading.addWidget(_tool(FIF.RETURN, "返回回复建议", self._back_home))
        heading.addWidget(_label("设置", 23, "#24382d", True), 1)
        body.addLayout(heading)
        body.addWidget(_label("调整关系背景，配置判断和起草用的两个模型。", 13, _MUTED))
        preference = _Surface()
        box = QVBoxLayout(preference)
        box.setContentsMargins(16, 16, 16, 18)
        box.setSpacing(12)
        box.addWidget(_label("回复偏好", 16, "#304c3c", True))
        relation_label = _label("你们的关系", 13)
        box.addWidget(relation_label)
        self.relationshipBox = ComboBox()
        self.relationshipBox.setMinimumWidth(0)
        self.relationshipBox.addItems([name for name, value in _RELATIONSHIPS])
        self.relationshipBox.setAccessibleName("你们的关系")
        relation_label.setBuddy(self.relationshipBox)
        box.addWidget(self.relationshipBox)
        self.relEdit = LineEdit()
        self.relEdit.setPlaceholderText("例如：刚认识的朋友，正在慢慢熟悉")
        self.relEdit.setAccessibleName("自定义关系背景")
        box.addWidget(self.relEdit)
        self.relationshipBox.currentIndexChanged.connect(
            lambda index: self.relEdit.setVisible(_RELATIONSHIPS[index][1] is None)
        )
        box.addWidget(self._hint("帮助助手把握称呼、语气和回应分寸。"))
        style_label = _label("说话风格（可选）", 13)
        box.addWidget(style_label)
        self.styleEdit = LineEdit()
        self.styleEdit.setPlaceholderText("例如：话少、不用标点、偶尔用 doge、不说客套话")
        self.styleEdit.setAccessibleName("说话风格")
        style_label.setBuddy(self.styleEdit)
        box.addWidget(self.styleEdit)
        box.addWidget(self._hint("候选本来就照着你最近发的消息模仿；这里可以再补一句你自己的口吻。"))
        context_label = _label("参考上下文", 13)
        box.addWidget(context_label)
        self.contextBox = SpinBox()
        self.contextBox.setRange(3, 30)
        self.contextBox.setAccessibleName("参考的最近消息条数")
        context_label.setBuddy(self.contextBox)
        box.addWidget(self.contextBox)
        box.addWidget(self._hint(
            "生成和判断时看最近这么多条消息。太少会丢上下文，太多会稀释重点，建议 6–12。"
        ))
        target_row = QHBoxLayout()
        target_row.addWidget(_label("群聊指定回复对象", 13), 1)
        self.targetSwitch = SwitchButton()
        self.targetSwitch.setOnText("开")
        self.targetSwitch.setOffText("关")
        self.targetSwitch.setAccessibleName("群聊指定回复对象")
        # 开关即时保存：拨了就落盘，不用再找「保存设置」（不然拨完关页就丢，看起来像没保存）
        self.targetSwitch.checkedChanged.connect(self._instant_save)
        target_row.addWidget(self.targetSwitch)
        box.addLayout(target_row)
        box.addWidget(self._hint(
            "开了以后群聊里可以选回复给谁，候选会针对 TA 写，填入时可带 @。关了就正常回复。"
        ))
        snap_row = QHBoxLayout()
        snap_row.addWidget(_label("吸附跟随微信窗口", 13), 1)
        self.snapSwitch = SwitchButton()
        self.snapSwitch.setOnText("开")
        self.snapSwitch.setOffText("关")
        self.snapSwitch.setAccessibleName("吸附跟随微信窗口")
        self.snapSwitch.checkedChanged.connect(self._instant_save)
        snap_row.addWidget(self.snapSwitch)
        box.addLayout(snap_row)
        box.addWidget(self._hint(
            "悬浮窗自动贴在微信窗口旁边（等高、零间隙），微信挪它也跟着挪。"
            "关了就停在你拖到的地方。"
        ))
        bubble_row = QHBoxLayout()
        bubble_row.addWidget(_label("微信内气泡浮层", 13), 1)
        self.bubbleSwitch = SwitchButton()
        self.bubbleSwitch.setOnText("开")
        self.bubbleSwitch.setOffText("关")
        self.bubbleSwitch.setAccessibleName("微信内气泡浮层")
        self.bubbleSwitch.checkedChanged.connect(self._instant_save)
        bubble_row.addWidget(self.bubbleSwitch)
        box.addLayout(bubble_row)
        box.addWidget(self._hint(
            "把对方最新消息、Jev 判断和三条候选直接画在微信聊天区上，"
            "点候选气泡即填入。跟着微信同层显示，微信被盖住它也跟着被盖。"
        ))
        vision_row = QHBoxLayout()
        vision_row.addWidget(_label("起草用视觉模型读屏", 13), 1)
        self.visionSwitch = SwitchButton()
        self.visionSwitch.setOnText("开")
        self.visionSwitch.setOffText("关")
        self.visionSwitch.setAccessibleName("起草用视觉模型读屏")
        self.visionSwitch.checkedChanged.connect(self._instant_save)
        vision_row.addWidget(self.visionSwitch)
        box.addLayout(vision_row)
        box.addWidget(self._hint(
            "开了以后起草时把聊天区截图直接发给模型（仅聊天对话区，不含侧边栏），"
            "图片/表情等文字读不出的内容也能看见。模型就是上面「起草」组里选的那个——"
            "需要支持图片输入的型号（如 GLM-4V、GPT-4o、Qwen-VL 系列），不支持的会报错，关掉即可；"
            "会话名识别和触发检测仍用本地 OCR。"
        ))
        noise_row = QHBoxLayout()
        noise_row.addWidget(_label("过滤群聊系统通知", 13), 1)
        self.noiseSwitch = SwitchButton()
        self.noiseSwitch.setOnText("开")
        self.noiseSwitch.setOffText("关")
        self.noiseSwitch.setAccessibleName("过滤群聊系统通知")
        self.noiseSwitch.checkedChanged.connect(self._instant_save)
        noise_row.addWidget(self.noiseSwitch)
        box.addLayout(noise_row)
        box.addWidget(self._hint(
            "进群/退群/撤回/时间戳这类系统通知不是人说的话：开了就在读取时直接滤掉、"
            "不再混进分析，同时给起草模型注入「忽略系统通知」的提示词兜底。"
        ))
        update_row = QHBoxLayout()
        update_row.addWidget(_label("启动时检查更新", 13), 1)
        self.updateSwitch = SwitchButton()
        self.updateSwitch.setOnText("开")
        self.updateSwitch.setOffText("关")
        self.updateSwitch.setAccessibleName("启动时检查更新")
        self.updateSwitch.checkedChanged.connect(self._instant_save)
        update_row.addWidget(self.updateSwitch)
        box.addLayout(update_row)
        box.addWidget(self._hint(
            "只向 GitHub 查最新版本号，不发送任何数据。国内访问 GitHub 慢的话关掉也行。"
        ))
        debug_row = QHBoxLayout()
        debug_row.addWidget(_label("调试视图", 13), 1)
        self.debugSwitch = SwitchButton()
        self.debugSwitch.setOnText("开")
        self.debugSwitch.setOffText("关")
        self.debugSwitch.setAccessibleName("调试视图")
        self.debugSwitch.checkedChanged.connect(self._debug_toggled)  # 这个开关立刻生效，不等「保存设置」
        debug_row.addWidget(self.debugSwitch)
        box.addLayout(debug_row)
        box.addWidget(self._hint(
            "另开一个窗口实时显示截到的画面和识别框：绿 = 我、蓝 = 对方、灰 = 过滤掉的灰字、"
            "红 = 当成图片丢掉、黄 = 小字丢掉。只在内存里画，不存图。"
        ))
        body.addWidget(preference)

        models = _Surface()
        box = QVBoxLayout(models)
        box.setContentsMargins(16, 16, 16, 18)
        box.setSpacing(12)
        box.addWidget(_label("模型", 16, "#304c3c", True))
        self._fetched = _Fetched()
        self._fetched.done.connect(self._models_fetched)
        self._tested = _Tested()
        self._tested.done.connect(self._test_finished)
        self.jev = self._model_group(box, "判断 · Jev", "jev", providers.JEV_PROVIDERS)
        box.addWidget(self._hint(
            "判断意图、紧张度，并给三条候选排序。OpenRouter / TypeSafe 是同一个 Jev；"
            "「自定义」填同协议（/api/alpha/decisions）的代理或自建地址。"
        ))
        self.draft = self._model_group(box, "起草 · 语言模型", "draft", providers.DRAFT_PROVIDERS)
        box.addWidget(self._hint(
            "写那三条候选。OpenAI / Anthropic / Gemini 三种接口都走各自官方 SDK。"
            "默认 DeepSeek 官网直连，国内最快。"
        ))
        think_row = QHBoxLayout()
        think_row.addWidget(_label("起草时开启思考模式", 13), 1)
        self.thinkingSwitch = SwitchButton()
        self.thinkingSwitch.setOnText("开")
        self.thinkingSwitch.setOffText("关")
        self.thinkingSwitch.setAccessibleName("起草时开启思考模式")
        self.thinkingSwitch.checkedChanged.connect(self._instant_save)
        think_row.addWidget(self.thinkingSwitch)
        box.addLayout(think_row)
        box.addWidget(self._hint(
            "关：秒回，够用。开：模型先想再写，更斟酌但慢好几倍、贵一些。"
            "只有 " + " / ".join(providers.THINKING) + " 认这个开关。"
        ))
        extra_label = _label("起草高级参数", 13)
        box.addWidget(extra_label)
        # 不懂 JSON 也能调：三个常用参数勾选即生效，其余进阶字段才用 JSON
        param_row = QHBoxLayout()
        self.tempCheck = CheckBox("温度")
        param_row.addWidget(self.tempCheck)
        self.tempSpin = DoubleSpinBox()
        self.tempSpin.setRange(0.0, 2.0)
        self.tempSpin.setDecimals(1)
        self.tempSpin.setSingleStep(0.1)
        self.tempSpin.setValue(1.2)  # 起草的默认温度就是 1.2（draft.py）
        self.tempSpin.setEnabled(False)
        self.tempSpin.setAccessibleName("温度")
        self.tempCheck.toggled.connect(self.tempSpin.setEnabled)
        param_row.addWidget(self.tempSpin)
        param_row.addStretch(1)
        box.addLayout(param_row)
        topp_row = QHBoxLayout()
        self.topPCheck = CheckBox("Top P")
        topp_row.addWidget(self.topPCheck)
        self.topPSpin = DoubleSpinBox()
        self.topPSpin.setRange(0.0, 1.0)
        self.topPSpin.setDecimals(2)
        self.topPSpin.setSingleStep(0.05)
        self.topPSpin.setValue(0.9)
        self.topPSpin.setEnabled(False)
        self.topPSpin.setAccessibleName("Top P")
        self.topPCheck.toggled.connect(self.topPSpin.setEnabled)
        topp_row.addWidget(self.topPSpin)
        topp_row.addStretch(1)
        box.addLayout(topp_row)
        tok_row = QHBoxLayout()
        self.maxTokCheck = CheckBox("最大输出 tokens")
        tok_row.addWidget(self.maxTokCheck)
        self.maxTokSpin = SpinBox()
        self.maxTokSpin.setRange(16, 8192)
        self.maxTokSpin.setSingleStep(16)
        self.maxTokSpin.setValue(400)
        self.maxTokSpin.setEnabled(False)
        self.maxTokSpin.setAccessibleName("最大输出 tokens")
        self.maxTokCheck.toggled.connect(self.maxTokSpin.setEnabled)
        tok_row.addWidget(self.maxTokSpin)
        tok_row.addStretch(1)
        box.addLayout(tok_row)
        self.extraEdit = LineEdit()
        self.extraEdit.setPlaceholderText('其它参数（JSON，可选），例如 {"frequency_penalty": 0.5}')
        self.extraEdit.setAccessibleName("其它起草参数 JSON")
        box.addWidget(self.extraEdit)
        box.addWidget(self._hint(
            "勾了就生效：温度越高越随性，Top P 越低越稳，tokens 限回复长度。"
            "「其它参数」是给进阶用户的 JSON，会浅合并进请求体（盖过上面几项），"
            "OpenAI / Anthropic 协议生效，Gemini 协议忽略。"
        ))
        body.addWidget(models)
        self.settingsFeedback = _label("", 13, _GREEN)
        self.settingsFeedback.hide()
        body.addWidget(self.settingsFeedback)
        actions = QHBoxLayout()
        back = PushButton("返回")
        back.clicked.connect(self._back_home)
        actions.addWidget(back)
        actions.addStretch(1)
        self.saveButton = PrimaryPushButton("保存设置")
        self.saveButton.clicked.connect(self._save)
        actions.addWidget(self.saveButton)
        body.addLayout(actions)
        body.addWidget(self._hint("保存后用于下一次生成的回复。"))
        body.addStretch(1)
        self._load_settings()

    def _hint(self, text):
        """设置页字段下面的灰字说明：记下来，紧凑模式一起隐藏。"""
        label = _label(text, 12, _MUTED)
        self._hintLabels.append(label)
        return label

    def _instant_save(self, _on=False):
        """偏好开关即时落盘：拨了就存，不等「保存设置」——不然拨完关页就丢，
        看起来就是「设置没保存」。开关一起存，幂等且便宜。
        装载（回显）期间不存：setChecked 触发的 toggled 带着的是半初始化状态。"""
        if self._loading:
            return
        try:
            settings.save(reply_target_on=self.targetSwitch.isChecked(),
                          snap_follow_on=self.snapSwitch.isChecked(),
                          check_update_on=self.updateSwitch.isChecked(),
                          thinking_on=self.thinkingSwitch.isChecked(),
                          bubble_layer_on=self.bubbleSwitch.isChecked(),
                          filter_system_msgs_on=self.noiseSwitch.isChecked(),
                          vision_draft_on=self.visionSwitch.isChecked())
        except Exception:
            pass  # 存不进去（磁盘只读之类）别把界面搞崩，下次点「保存设置」还能兜住

    def _model_group(self, box, title, kind, table):
        """一组「来源 / 密钥 / 模型」控件，判断和起草各一份。table 是 core/providers.py 里那张表。"""
        group = SimpleNamespace(kind=kind, table=table, ids=list(table),
                                keyTitle="判断" if kind == "jev" else "起草",
                                stored_key=lambda k=kind: (settings.jev_key() if k == "jev"
                                                           else settings.llm_key()))
        heading = QHBoxLayout()
        heading.addWidget(_label(title, 14, "#304c3c", True), 1)
        group.keyState = _label("", 12, _GREEN)
        group.keyState.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        heading.addWidget(group.keyState)
        box.addLayout(heading)
        source_label = _label("来源", 13)
        box.addWidget(source_label)
        group.providerBox = ComboBox()
        group.providerBox.setMinimumWidth(0)  # 选项文字长短不一，别让它撑开设置页
        group.providerBox.addItems([table[i].name for i in group.ids])
        group.providerBox.setAccessibleName(f"{title} 来源")
        source_label.setBuddy(group.providerBox)
        box.addWidget(group.providerBox)
        group.baseLabel = _label("Base URL", 13)
        box.addWidget(group.baseLabel)
        group.baseEdit = LineEdit()
        group.baseEdit.setPlaceholderText("切来源会自动填它的默认地址，可手动改成镜像/代理")
        group.baseEdit.setAccessibleName(f"{title} Base URL")
        group.baseLabel.setBuddy(group.baseEdit)
        box.addWidget(group.baseEdit)
        key_label = _label("密钥", 13)
        box.addWidget(key_label)
        group.keyEdit = PasswordLineEdit()
        group.keyEdit.setAccessibleName(f"{title} API 密钥")
        key_label.setBuddy(group.keyEdit)
        group.keyEdit.returnPressed.connect(self._save)
        box.addWidget(group.keyEdit)
        box.addWidget(self._hint(
            "看上面选的来源：OpenRouter、TypeSafe 或你自定义接口的 key。" if kind == "jev"
            else "上面选哪家就填哪家的 key；换来源重填一次，只存这一把。"))
        model_label = _label("模型", 13)
        box.addWidget(model_label)
        row = QHBoxLayout()
        row.setSpacing(8)
        group.modelBox = EditableComboBox()  # 能选也能手打，接口新出的模型不用等我改代码
        group.modelBox.setMinimumWidth(0)
        group.modelBox.setAccessibleName(f"{title} 模型")
        model_label.setBuddy(group.modelBox)
        row.addWidget(group.modelBox, 1)
        group.fetchButton = PushButton("获取模型")
        group.fetchButton.setMinimumWidth(0)  # 窄窗时允许压缩，别把页面撑出可视区
        group.fetchButton.setAccessibleName(f"获取{title}的可用模型列表")
        group.fetchButton.clicked.connect(lambda: self._fetch_models(group))
        row.addWidget(group.fetchButton)
        group.testButton = PushButton("测试")
        group.testButton.setMinimumWidth(0)
        group.testButton.setToolTip("用当前配置发一次最小请求，验证地址、密钥和模型是否可用")
        group.testButton.setAccessibleName(f"测试{title}配置")
        group.testButton.clicked.connect(lambda: self._test_models(group))
        row.addWidget(group.testButton)
        box.addLayout(row)
        group.status = _label("", 12, _MUTED)
        box.addWidget(group.status)
        group.providerBox.currentIndexChanged.connect(lambda _: self._provider_changed(group))
        return group

    @staticmethod
    def _provider_of(group):
        return group.ids[max(0, group.providerBox.currentIndex())]

    def _provider_changed(self, group):
        """换来源：模型框和 Base URL 行回到这家该有的值（存的就是这家才用存的，否则用它的默认），
        状态清掉。地址自动填进去，用户仍可手动改（比如把 OpenRouter 换成镜像）。"""
        provider = self._provider_of(group)
        saved = settings.jev_provider() if group.kind == "jev" else settings.draft_provider()
        stored = settings.jev_model() if group.kind == "jev" else settings.draft_model()
        saved_base = settings.jev_base_url() if group.kind == "jev" else settings.draft_base_url()
        group.modelBox.clear()
        group.modelBox.setText(stored if provider == saved else group.table[provider].default)
        # 地址：存的值优先，没存（或来源换过）就填该家的默认
        group.baseEdit.setText(
            saved_base if (provider == saved and saved_base) else group.table[provider].base)
        group.status.setText("")
        self._sync_model_fields()

    def _sync_model_fields(self):
        """两组共用：密钥已配置/未配置、占位文案、Base URL 行（常驻：切来源自动填该家默认地址，
        可手改），外加紧凑模式下把来源按钮上的文字省略——ComboBox 是 QPushButton，
        minimumSizeHint 按整段文字算，不会自动换行/省略，长名字会把设置页撑宽。"""
        for group in (self.jev, self.draft):
            provider = self._provider_of(group)
            name = group.table[provider].name
            configured = bool(group.stored_key())
            group.keyState.setText("已配置" if configured else "未配置")
            group.keyEdit.setPlaceholderText(
                "已配置，留空保留" if configured else f"输入 {name} API 密钥")
            group.baseLabel.show()
            group.baseEdit.show()
            if self._compact:
                name = group.providerBox.fontMetrics().elidedText(name, Qt.ElideRight, 180)
            group.providerBox.setText(name)

    def _fetch_models(self, group):
        """「获取模型」：拿填的 key（没填就拿存的）和 Base URL 行里的地址去问接口，
        网络调用丢后台线程。"""
        provider = self._provider_of(group)
        base = group.baseEdit.text().strip() or None
        key = group.keyEdit.text().strip() or group.stored_key()
        if not key:
            group.status.setText("先填密钥")
            return
        if provider == "custom" and not base:
            group.status.setText("先填 Base URL")
            return
        group.status.setText("获取中…")
        group.fetchButton.setEnabled(False)
        threading.Thread(target=lambda: self._list_models(group, provider, key, base),
                         daemon=True).start()

    def _list_models(self, group, provider, key, base):
        """后台线程：判断走 jev_client，起草按协议走 llm；失败把原因一起送回主线程。"""
        try:
            if group.kind == "jev":
                models = jev_client.list_models(provider, key, base_url=base)
            else:
                spec = providers.DRAFT_PROVIDERS[provider]
                models = llm.list_models(spec.protocol, base or spec.base, key)
            reason = "" if models else "这个来源没返回任何模型"
        except Exception as exc:  # 线程里漏异常会静默吞掉，按钮就永远停在禁用态
            models, reason = [], str(exc)[:120]
        self._fetched.done.emit(group, models, reason)

    def _models_fetched(self, group, models, reason):
        """回到主线程：填进下拉框，原来选中的还在列表里就留着。"""
        group.fetchButton.setEnabled(True)
        if not models:
            group.status.setText(reason or "获取失败，检查密钥、网络或 Base URL")
            return
        current = group.modelBox.text().strip()
        group.modelBox.clear()
        group.modelBox.addItems(models)
        if current in models:
            group.modelBox.setCurrentIndex(models.index(current))
        else:
            group.modelBox.setText(current)  # 手打的没在列表里也不清掉
        group.status.setText(f"共 {len(models)} 个")

    def _set_group(self, group, provider, model):
        """把存下来的来源和模型放回一组控件里；填充不算用户操作，别触发换来源的重置。
        密钥直接填入（PasswordLineEdit 自带眼睛图标切换明文），免得每次重输。"""
        group.providerBox.blockSignals(True)
        group.providerBox.setCurrentIndex(group.ids.index(provider))
        group.providerBox.blockSignals(False)
        group.keyEdit.setText(group.stored_key())
        group.modelBox.clear()
        group.modelBox.setText(model)
        group.status.setText("")

    def _load_settings(self):
        self._loading = True  # 回显期间拦住即时保存（见 _instant_save）
        relationship = settings.relationship()
        index = next((i for i, (_, value) in enumerate(_RELATIONSHIPS) if value == relationship),
                     len(_RELATIONSHIPS) - 1)
        self.relationshipBox.setCurrentIndex(index)
        self.relEdit.setText(relationship if _RELATIONSHIPS[index][1] is None else "")
        self.relEdit.setVisible(_RELATIONSHIPS[index][1] is None)
        self.styleEdit.setText(settings.style())
        self.contextBox.setValue(settings.context())
        self.targetSwitch.setChecked(settings.reply_target())
        self._set_group(self.jev, settings.jev_provider(), settings.jev_model())
        self._set_group(self.draft, settings.draft_provider(), settings.draft_model())
        # Base URL 行常驻：存过就用存的，没存把该来源的默认地址填进去（和切换来源时的行为一致）
        self.jev.baseEdit.setText(
            settings.jev_base_url() or providers.JEV_PROVIDERS[settings.jev_provider()].base)
        self.draft.baseEdit.setText(
            settings.draft_base_url() or providers.DRAFT_PROVIDERS[settings.draft_provider()].base)
        params = settings.draft_extra() or {}
        self.tempCheck.setChecked("temperature" in params)
        self.tempSpin.setValue(float(params.get("temperature") or 1.2))
        self.tempSpin.setEnabled(self.tempCheck.isChecked())
        self.topPCheck.setChecked("top_p" in params)
        self.topPSpin.setValue(float(params.get("top_p") or 0.9))
        self.topPSpin.setEnabled(self.topPCheck.isChecked())
        self.maxTokCheck.setChecked("max_tokens" in params)
        self.maxTokSpin.setValue(int(params.get("max_tokens") or 400))
        self.maxTokSpin.setEnabled(self.maxTokCheck.isChecked())
        rest = {k: v for k, v in params.items()
                if k not in ("temperature", "top_p", "max_tokens")}
        self.extraEdit.setText(json.dumps(rest, ensure_ascii=False) if rest else "")
        self.thinkingSwitch.setChecked(settings.thinking())
        self.updateSwitch.setChecked(settings.check_update())
        self.snapSwitch.setChecked(settings.snap_follow())
        self.bubbleSwitch.setChecked(settings.bubble_layer())
        self.noiseSwitch.setChecked(settings.filter_system_msgs())
        self.visionSwitch.setChecked(settings.vision_draft())
        self.set_debug_switch(settings.debug_view())  # 屏蔽信号地拨，别在加载时开关一遍窗口
        self._sync_model_fields()  # 上面屏蔽了信号，这里补一次
        self.settingsFeedback.hide()
        self._loading = False

    def _save(self):
        relationship = _RELATIONSHIPS[self.relationshipBox.currentIndex()][1]
        relationship = relationship or self.relEdit.text().strip()
        jev_provider = self._provider_of(self.jev)
        draft_provider = self._provider_of(self.draft)
        jev_base = self.jev.baseEdit.text().strip()
        draft_base = self.draft.baseEdit.text().strip()
        extra_text = self.extraEdit.text().strip()
        params = {}
        if self.tempCheck.isChecked():  # 勾选即生效，没勾用起草默认
            params["temperature"] = round(self.tempSpin.value(), 2)
        if self.topPCheck.isChecked():
            params["top_p"] = round(self.topPSpin.value(), 2)
        if self.maxTokCheck.isChecked():
            params["max_tokens"] = self.maxTokSpin.value()
        if extra_text:  # 「其它参数」进阶 JSON，可选项；非法拦截
            try:
                parsed_extra = json.loads(extra_text)
                assert isinstance(parsed_extra, dict) and parsed_extra
            except (ValueError, AssertionError):
                self._settings_feedback('「其它参数」得是 JSON 对象，例如 {"frequency_penalty": 0.5}。', error=True)
                self.extraEdit.setFocus()
                return
            params.update(parsed_extra)
        extra_text = json.dumps(params, ensure_ascii=False) if params else ""
        if not relationship:
            self._settings_feedback("请填写关系背景，或选择一个已有选项。", error=True)
            self.relEdit.setFocus()
            return
        if draft_provider in providers.CUSTOM and not draft_base:
            self._settings_feedback("自定义来源要填 Base URL。", error=True)
            self.draft.baseEdit.setFocus()
            return
        if jev_provider == "custom" and not jev_base:
            self._settings_feedback("自定义判断来源要填 Base URL。", error=True)
            self.jev.baseEdit.setFocus()
            return
        for group, provider in ((self.jev, jev_provider), (self.draft, draft_provider)):
            name = group.table[provider].name
            if not group.keyEdit.text().strip() and not group.stored_key():
                self._settings_feedback(f"请先填写 {group.keyTitle} 的 API 密钥。", error=True)
                group.keyEdit.setFocus()
                return
            if not group.modelBox.text().strip():
                self._settings_feedback(f"{name} 请先获取并选择一个模型。", error=True)
                group.modelBox.setFocus()
                return
        try:
            settings.save(relationship, self.contextBox.value(),
                          jev_provider_text=jev_provider,
                          jev_key_text=self.jev.keyEdit.text().strip() or None,
                          jev_model_text=self.jev.modelBox.text().strip(),
                          jev_base_url_text=jev_base,
                          draft_provider_text=draft_provider,
                          llm_key_text=self.draft.keyEdit.text().strip() or None,
                          draft_model_text=self.draft.modelBox.text().strip(),
                          draft_base_url_text=draft_base,
                          draft_extra_text=extra_text,
                          reply_target_on=self.targetSwitch.isChecked(),
                          style_text=self.styleEdit.text().strip(),
                          thinking_on=self.thinkingSwitch.isChecked(),
                          check_update_on=self.updateSwitch.isChecked(),
                          snap_follow_on=self.snapSwitch.isChecked())
        except Exception:
            self._settings_feedback("保存失败，请检查配置文件是否可写后重试。", error=True)
            return
        self._load_settings()
        self._pinned = False  # 重新保存过设置，吸附跟随立刻恢复
        self._pin_rect = None
        self._render_targets()  # 开关刚改过，回到首页时这一行该显该藏得重算一次
        self._settings_feedback("设置已保存，将用于下一次回复。")
        self.setupButton.hide()
        if not self.cands and not self._busy:
            self._empty_text()
            self.set_status("设置已就绪，等待新消息", "idle")

    def _debug_toggled(self, on):
        """调试视图独立于「保存设置」：拨一下就开窗/收窗，顺手落盘，重启还在。"""
        settings.save(debug_view_on=on)
        if self.on_toggle_debug:
            self.on_toggle_debug(on)

    def set_debug_switch(self, on):
        """调试窗被用户直接关掉时把开关拨回去；屏蔽信号，免得又回调一圈。"""
        self.debugSwitch.blockSignals(True)
        self.debugSwitch.setChecked(on)
        self.debugSwitch.blockSignals(False)

    def _settings_feedback(self, text, error=False):
        color = "#b44832" if error else _GREEN
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(self.settingsFeedback, qss, qss)
        self.settingsFeedback.setText(text)
        self.settingsFeedback.show()

    def open_settings(self):
        if self.pages.currentWidget() != self.settingsPage:
            self._load_settings()
        self.pages.setCurrentWidget(self.settingsPage)
        self.settingsButton.setEnabled(False)
        (self.relationshipBox if settings.has_key() else self.jev.keyEdit).setFocus()

    def _back_home(self):
        self.pages.setCurrentWidget(self.home)
        self.settingsButton.setEnabled(True)

    def _fill(self, index):
        if self._busy or not self._current or index >= len(self.cands):
            return
        try:
            self.on_fill(self.cands[index])
        except Exception as e:
            # 状态栏保持友好文案；真实原因和压缩堆栈进聊天记录，认得出是哪一步炸的
            import traceback
            self.set_status("未能填入，请确认微信窗口可用后重试，或复制回复。", "error")
            self.log(f"[填入失败] {type(e).__name__}: {e}")
            self.log(f"[填入失败堆栈] {' '.join(traceback.format_exc().split())[:300]}")
            return
        self.set_status("已尝试填入，请在微信确认内容后发送。", "success")

    def _copy(self, index):
        if self._busy or not self._current or index >= len(self.cands):
            return
        self.app.clipboard().setText(self.cands[index])
        self.set_status("回复已复制，可在微信中粘贴并修改。", "success")

    def snap_to(self, rect):
        """微信窗口矩形（Win32 GetWindowRect 的**物理像素**）→ 悬浮窗完全贴边吸附：
        高度与微信窗口一致、顶边对齐、0 间距；微信宽 + 悬浮窗宽超出屏幕时自动收窄悬浮窗
        （最小 320），选剩余空间更宽的一边贴外缘；两侧都放不下才内叠（靠左缘，盖会话列表
        不盖聊天内容）。Qt 的 move() 吃逻辑坐标，高 DPI 下先按 devicePixelRatio 换算。
        用户拖走（_pinned）后停在原地，但微信窗口一动就恢复跟随。"""
        self._last_rect = rect  # 拖拽松手时算磁吸要用最新的微信矩形
        if not settings.snap_follow() or self.win.isMinimized():
            return
        if QApplication.mouseButtons() & Qt.LeftButton:  # 拖动中，别抢
            return
        if self._pinned:
            # 拖走后停在原地；微信矩形一变（挪了/缩放了）就当用户想让它继续跟
            if self._pin_rect is None:
                self._pin_rect = rect
                return
            if rect != self._pin_rect:
                self._pinned = False
                self._pin_rect = None
            else:
                return
        target = self._dock_target(rect)
        if target is None:
            return
        x, y, new_w, new_h = target
        if (self.win.width(), self.win.height()) != (new_w, new_h):
            self.win.resize(new_w, new_h)
        if (x, y) != (self.win.x(), self.win.y()):
            self.win.move(x, y)

    def _on_drag_end(self):
        """拖悬浮窗松手：离贴边位 60 逻辑像素内当手滑，磁吸回贴边位；拖得远才算手动摆放
        （钉住，微信一动恢复跟随）。"""
        rect = self._last_rect
        if not settings.snap_follow() or rect is None:
            self._pinned = True  # 没有微信矩形可参照（没开微信/关了跟随），按手动摆放算
            self._pin_rect = None
            return
        target = self._dock_target(rect)
        if target is None:
            self._pinned = True
            self._pin_rect = rect
            return
        x, y, new_w, new_h = target
        near = ((self.win.x() - x) ** 2 + (self.win.y() - y) ** 2) ** 0.5 <= 60
        if near:
            self._pinned = False
            self._pin_rect = None
            if (self.win.width(), self.win.height()) != (new_w, new_h):
                self.win.resize(new_w, new_h)
            self.win.move(x, y)
        else:
            self._pinned = True
            self._pin_rect = rect

    def _border_offsets(self):
        """悬浮窗自己的不可见边框（物理 px）：Win32 矩形和 Qt 逻辑几何的差。
        Qt 的无框窗口在 Win32 里仍留着缩放边，不补偿的话贴边永远差几个像素。缓存一次。"""
        if self._border is None:
            try:
                dpr = self.win.screen().devicePixelRatio() or 1.0
                g = ctypes.wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(int(self.win.winId()), ctypes.byref(g))
                ql = round(self.win.x() * dpr)
                qt = round(self.win.y() * dpr)
                qr = round((self.win.x() + self.win.width()) * dpr)
                qb = round((self.win.y() + self.win.height()) * dpr)
                self._border = (g.left - ql, g.top - qt, g.right - qr, g.bottom - qb)
            except Exception:
                self._border = (0, 0, 0, 0)
        return self._border

    def _dock_target(self, rect):
        """给定微信**可见边界**矩形（Win32 物理像素），算贴边吸附的落点和尺寸
        (x, y, w, h)（逻辑坐标）；矩形无效返回 None。纯计算不动窗口，吸附和拖拽磁吸共用。
        全程物理像素计算，最后再除回逻辑：悬浮窗自己的不可见边框（_border_offsets）
        一并补偿——可见边缘贴可见边缘，才是用户眼里的「完全贴边、高度一致」。"""
        dpr = self.win.screen().devicePixelRatio() or 1.0
        left, top, right, bottom = rect
        screen = self.win.screen().availableGeometry()
        sl, st = round(screen.left() * dpr), round(screen.top() * dpr)
        sr = round((screen.right() + 1) * dpr)  # 右/下边界是「第一个不存在的像素」
        sb = round((screen.bottom() + 1) * dpr)
        # 矩形出了屏幕（最小化的窗口在 -16000 这类地方、或显示器换了）就没法贴
        if right <= left or bottom <= top or right < sl or left > sr:
            return None
        bl, bt, br, bb = self._border_offsets()
        # 高度跟微信可见高一致（含自身上下不可见边），不小于最小高、不超出屏幕
        h = max(round(self.win.minimumHeight() * dpr), min(bottom - top + bt + bb, sb - st))
        # 宽度：微信可见宽 + 悬浮窗可见宽不许溢出屏幕 → 收窄到贴的那边剩的空间（最小 320 逻辑）
        free_r = sr - right
        free_l = left - sl
        dock_right = free_r >= free_l  # 哪边剩的空间多贴哪边
        avail = free_r if dock_right else free_l
        w = round(self.win.width() * dpr)
        min_w = round(max(320, self.win.minimumWidth()) * dpr)
        if avail < w:
            w = max(min_w, min(w, avail))
        if dock_right:  # 可见左缘贴微信可见右缘：Qt 左 = 微信右 + 自身左边框
            x = min(right + bl, sr - w)
        else:  # 可见右缘贴微信可见左缘：Qt 右 = 微信左 - 自身右边框
            x = max(sl, left - w + br)
        y = min(top + bt, sb - h)  # 可见顶缘与微信对齐
        x = max(sl, min(sr - w, x))  # 兜底：不许落屏幕外
        y = max(st, y)
        return x / dpr, y / dpr, w / dpr, h / dpr

    def set_bubble_error(self, chat, reason):
        """记录某会话的生成失败原因：浮层面板显示它（而不是留着一轮旧建议误导人）。"""
        self._bubble_errors[chat] = reason.strip()[:200]

    def sync_bubble(self, chat, area):
        """微信聊天区气泡浮层：把当前微信会话的对方消息、Jev 判断、三条候选画到消息区上。
        chat/area 为空、没结果或开关关 → 隐藏。main 每 250ms 调一次（位置跟微信走）。"""
        on = settings.bubble_layer() and bool(chat) and bool(area)
        result = self.result_of(chat) if (on and self.result_of) else None
        err = self._bubble_errors.get(chat)
        if not on or (not result and not err):
            if self._bubble is not None and self._bubble.isVisible():
                self._bubble.hide()
            return
        if self._bubble is None:
            self._bubble = _BubbleLayer(self)
        if result is None:  # 生成失败：面板显示原因 + 重试提示（新结果到达时自动清掉）
            key = ("err", chat, err)
            if key != self._bubble_key:
                self._bubble_key = key
                self._bubble_result = None
                self._bubble.place(area)
                self._bubble.set_content(
                    self.hers.get(chat) or "",  # 原文留着，失败也知道自己在回什么
                    ["生成失败：" + err, "点「↻ 重新生成」可重试"],
                    {"candidates": []})
            else:
                self._bubble.place(area)
            return
        self._bubble_errors.pop(chat, None)  # 成功了就清掉错误态
        self._bubble.place(area)
        key = (chat, result.get("seq") or id(result))
        if key != self._bubble_key:
            self._bubble_key = key
            self._bubble_result = result
            self._bubble.set_content(self.hers.get(chat) or "",
                                     judgment_lines(result), result)

    def bubble_hwnd(self):
        """浮层的 Win32 句柄（没建或没显示返回 0）：main 拿它把浮层钉在微信正上方。"""
        if self._bubble is not None and self._bubble.isVisible():
            return int(self._bubble.winId())
        return 0

    def save_bubble_offset(self, dx, dy):
        """浮层拖拽松手：把相对默认锚点的偏移写进配置（重启后固定在最后位置）。"""
        try:
            settings.save(bubble_offset_pair=[int(dx), int(dy)])
        except Exception:
            pass

    def _capture_toggled(self, on):
        """用户自己拨的开关：界面先改，再通知父进程去开/停采集。"""
        self._capture_text(on)
        if self.on_toggle_capture:
            self.on_toggle_capture(on)

    def set_update(self, latest, url):
        """main.py 后台线程查到比当前新的版本才会调这个。只显示版本号和 Release 链接，别的什么都没有。"""
        self.updateLabel.setText(f"有新版本 v{latest}")
        self.updateLink.setUrl(url)
        self.updateBar.show()

    def set_capture(self, on, reason=""):
        """父进程回报的状态：只改界面，不回调（不然和父进程来回打架）。reason 为空用默认说明。"""
        self.captureSwitch.blockSignals(True)
        self.captureSwitch.setChecked(on)
        self.captureSwitch.blockSignals(False)
        self._capture_text(on, reason)

    def _capture_text(self, on, reason=""):
        """开关状态对应的状态行和空态文案。已有的候选不受影响，暂停了照样能填入/复制。"""
        configured = settings.has_key()
        if not on:
            self.set_status(reason or "采集已暂停，微信内容不再读取", "warning")
        elif configured:
            self.set_status("等待新消息", "idle")
        else:
            self.set_status("请先在设置中配置模型", "warning")
        if self._busy or self.cands:  # 正在生成或已有候选时，空态卡片本来就看不见
            return
        if not on:
            self.emptyTitle.setText("采集已暂停")
            self.emptyHint.setText("微信里的内容暂时不再读取。\n打开标题栏的开关，继续接收新消息。")
            self.setupButton.setVisible(not configured)
        else:
            self._empty_text()

    def set_busy(self, busy):
        self._busy = busy
        if self._bubble is not None:  # 浮层上的重新生成按钮跟着转动画
            self._bubble.set_generating(busy)
        self.progress.setVisible(busy)
        if busy:
            self.invalidate_replies()
            self.progress.start()
            self.set_status("正在根据新消息整理回复…", "busy")
            if not self.cands:
                self.emptyTitle.setText("正在想一句合适的回复")
                self.emptyHint.setText("正在结合上下文生成建议，稍等一下。")
                self.setupButton.hide()
        else:
            self.progress.stop()
            if not self.cands:
                self._empty_text()
        for card in self.cards:
            card.set_available(self._current and not busy)

    def _empty_text(self):
        """空态卡片的默认文案，配好没配好两套说法。"""
        configured = settings.has_key()
        self.emptyTitle.setText("等待对方的新消息" if configured else "先设置，再开始")
        self.emptyHint.setText("保持微信聊天窗口打开。\n收到新消息后，回复建议会出现在这里。"
                               if configured else "配置模型和关系背景，\n让建议更贴近你们的对话。")
        self.setupButton.setVisible(not configured)

    def invalidate_replies(self):
        self._current = False
        if self.cands:
            self.updated.setText("上次建议")
        for card in self.cards:
            card.set_available(False)

    def set_status(self, text, kind="idle"):
        colors = {"idle": _MUTED, "busy": _GREEN, "success": _GREEN,
                  "warning": "#93611d", "error": "#b44832"}
        markers = {"idle": "●", "busy": "●", "success": "✓", "warning": "!", "error": "!"}
        qss = f"BodyLabel {{ color: {colors.get(kind, _MUTED)}; background: transparent; }}"
        setCustomStyleSheet(self.status, qss, qss)
        self.status.setText(f"{markers.get(kind, '●')}  {text}")
        if kind == "error" and self._busy:
            self.set_busy(False)
        if kind == "error" and not self.cands:
            self.emptyTitle.setText("暂时没有可用的回复")
            self.emptyHint.setText("请按上方提示处理。收到新的对方消息后会再次尝试。")
            self.setupButton.setVisible(not settings.has_key())

    def _refresh_clicked(self):
        """首页「重新生成」：忙时提示，闲了交给 main 用当前会话记录再跑一轮。"""
        if self._busy:
            self.set_status("正在生成中，稍等一下再试", "warning")
            return
        if self.on_refresh:
            self.on_refresh()

    def _test_models(self, group):
        """「测试」：用当前配置发一次最小请求，验证地址、密钥、模型是否真的能用。
        起草发一轮 10 tokens 的小对话；判断拉一次模型列表（decisions 接口没有更便宜的探测）。"""
        provider = self._provider_of(group)
        base = group.baseEdit.text().strip() or None
        key = group.keyEdit.text().strip() or group.stored_key()
        model = group.modelBox.text().strip() or None
        if not key:
            group.status.setText("测试：先填密钥")
            return
        if provider in providers.CUSTOM and not base:
            group.status.setText("测试：先填 Base URL")
            return
        group.status.setText("测试中…")
        group.testButton.setEnabled(False)
        threading.Thread(target=lambda: self._run_test(group, provider, key, base, model),
                         daemon=True).start()

    def _run_test(self, group, provider, key, base, model):
        try:
            if group.kind == "jev":
                models = jev_client.list_models(provider, key, base_url=base)
                ok, msg = bool(models), f"✓ 连通正常，{len(models)} 个模型"
            else:
                spec = providers.DRAFT_PROVIDERS[provider]
                reply = llm.chat(spec.protocol, base or spec.base, key,
                                 model or spec.default, "你是连通性测试。", ["只回复：正常"],
                                 temperature=0, max_tokens=10, timeout=20)
                ok = bool(reply and reply.strip())
                msg = f"✓ 连通正常（回复：{reply.strip()[:20]}）" if ok else "✗ 模型返回为空"
        except Exception as exc:
            ok, msg = False, "✗ " + str(exc)[:100]
        self._tested.done.emit(group, ok, msg)

    def _test_finished(self, group, ok, msg):
        group.testButton.setEnabled(True)
        group.status.setText(msg)
        from PySide6.QtGui import QColor as _C
        qss = ("BodyLabel { color: " + (_GREEN if ok else "#b44832") + "; background: transparent; }")
        setCustomStyleSheet(group.status, qss, qss)

    def _toggle_history(self):
        self.feed.setVisible(self.feed.isHidden())
        self._history_title()

    def _history_title(self):
        action = "展开" if self.feed.isHidden() else "收起"
        count = self.counts.get(self._shown, 0)
        self.historyButton.setText(f"{action}聊天记录" + (f" · {count}" if count else ""))

    def log(self, line):
        """采集状态行：只贴到正在看的那个会话的记录区（居中小字），不按会话存。"""
        if self.feed.isHidden():
            return
        wrap = self._append_bubble({"kind": "note", "text": line})
        wrap.setMaximumHeight(24)
        self._scroll_feed_bottom()

    def log_message(self, who, text, name="", timestamp=None, chat=None):
        """按会话存一份；只有正在看的那个会往显示区里写。
        开了「过滤系统通知」时，漏网的系统通知不更新「对方最近说」——浮层显示的
        永远是过滤后的最新真人消息原文。"""
        chat = chat or self._shown
        timestamp = timestamp or datetime.now().strftime("%H:%M")
        self.counts[chat] = self.counts.get(chat, 0) + 1
        entries = self.feeds.setdefault(chat, [])
        entries.append({"kind": "msg", "who": who, "name": name, "text": text, "ts": timestamp})
        del entries[:-_LOG_LINES]
        noise = who == "her" and settings.filter_system_msgs() and is_system_noise(text)
        if who == "her" and not noise:
            self.hers[chat] = text
        self._add_chat(chat)
        if chat != self._shown:
            return
        self._append_bubble(entries[-1])
        self._scroll_feed_bottom()
        if who == "her" and not noise:
            self._show_latest(text)
        self._history_title()

    def show_judgment(self, chat, text):
        """一轮分析完成：判断摘要当「Jev 气泡」存进那个会话的记录（跟在对方消息后面）。"""
        entry = {"kind": "jev", "text": text, "ts": datetime.now().strftime("%H:%M")}
        entries = self.feeds.setdefault(chat, [])
        entries.append(entry)
        del entries[:-_LOG_LINES]
        self._add_chat(chat)
        if chat == self._shown:
            self._append_bubble(entry)
            self._scroll_feed_bottom()

    def _show_latest(self, text):
        self.latest.setText(text if len(text) <= 120 else text[:120] + "…")
        self.latest.setToolTip(text)
        self.context.show()

    def current_chat(self):
        """界面上正在看的会话（不一定是微信当前开着的那个）。"""
        return self._shown

    def set_chat(self, title):
        """微信切到了哪个会话：登记进下拉框并自动跟过去，不触发用户选择的回调。"""
        if not title:
            return
        browsing = self._shown != self._chat  # 正看着的就是它、但之前是「浏览中」：也得重画，把填入放开
        self._chat = title
        self._add_chat(title)
        if title != self._shown or browsing:
            self.chatBox.blockSignals(True)
            self.chatBox.setCurrentIndex(self.chatBox.findText(title))
            self.chatBox.blockSignals(False)
            self._switch_to(title)
        self._follow_text()

    def _add_chat(self, title):
        """新会话自动进下拉框；addItem 添第一条时会自己选中，别让它触发切换。"""
        if not title or self.chatBox.findText(title) >= 0:
            return
        self.chatBox.blockSignals(True)
        self.chatBox.addItem(title)
        self.chatBox.blockSignals(False)

    def _on_chat_selected(self, index):
        """用户自己挑了一个会话：只换看的内容，微信那边不动。"""
        title = self.chatBox.itemText(index)
        if title and title != self._shown:
            self._switch_to(title)

    def _switch_to(self, title):
        """换正在看的会话：记录、对方最近说、条数、上次的建议一起换过去。"""
        self._shown = title
        self._clear_feed()
        for entry in self.feeds.get(title, []):
            self._append_bubble(entry)
        self._scroll_feed_bottom()
        her = self.hers.get(title)
        if her:
            self._show_latest(her)
        else:
            self.context.hide()
        self._history_title()
        self._follow_text()
        self._render_targets()
        self._elide_chat_name()
        self.show_cached(self.result_of(title) if self.result_of else None)

    def set_targets(self, chat, senders, current):
        """某个会话的发言人名单（最近的在前）和当前回复对象；正看着它才重画。"""
        self.targets[chat] = (list(senders), current)
        if chat == self._shown:
            self._render_targets()

    def _render_targets(self):
        """开关关着、或这个会话没有发言人（单聊），这一行就不出现。
        重填下拉框时屏蔽信号，别把自己的填充当成用户挑的。"""
        senders, current = self.targets.get(self._shown, ([], None))
        visible = bool(senders) and settings.reply_target()
        self.targetRow.setVisible(visible)
        if not visible:
            return
        self.targetBox.blockSignals(True)
        self.targetBox.clear()
        self.targetBox.addItems(senders)
        self.targetBox.setCurrentIndex(senders.index(current) if current in senders else 0)
        self.targetBox.blockSignals(False)

    def _on_target_selected(self, index):
        """用户挑了回复对象。浏览别的会话时改的就是那个会话的对象——记录、候选也都按会话走，口径一致。"""
        name = self.targetBox.itemText(index)
        if not name:
            return
        senders, _ = self.targets.get(self._shown, ([], None))
        self.targets[self._shown] = (senders, name)
        self.set_status(f"按「{name}」重新生成…", "busy")
        if self.on_target_change:
            self.on_target_change(self._shown, name)

    def at_prefix_enabled(self):
        """填入时要不要带「@名字 」前缀（只记在界面上，不落盘）。"""
        return self.atCheck.isChecked()

    def _follow_text(self):
        self.chatFollow.setText(("跟随微信" if self._shown == self._chat else "浏览中") if self._chat else "")

    def show_cached(self, result):
        """把某个会话上次的结果放回界面；没有就回到空态。浏览别的会话时只给看不给填——
        微信当前开着的不是它，填进去就串会话了。"""
        if result:
            self.show(result)
        else:
            self.cands = []
            self._clear_cards()
            self.insight.hide()
            self.referenceNote.hide()
            self.empty.show()
            self.updated.setText("")
            self._empty_text()
        if self._shown != self._chat:
            self.invalidate_replies()
            self.set_status(f"正在浏览「{self._shown}」，只看不填；微信切回它才能用。")

    def show(self, result):
        """按推荐顺序展示，按钮始终绑定 candidates 的原始索引。"""
        self.cands = result["candidates"]
        self.set_busy(False)
        self._current = bool(self.cands)
        self._clear_cards()
        best = result.get("best_index", 0)
        if best not in range(len(self.cands)):
            best = 0
        raw_scores = result.get("scores") or []
        scores = [raw_scores[i] if i < len(raw_scores) else None for i in range(len(self.cands))]
        if not any(scores):  # 全 0/None（旧结果或接口未返回）就不展示百分比
            scores = [None] * len(self.cands)
        # 按概率降序排，推荐位（API 给的 choice）强制第一，同分按原索引
        order = sorted(range(len(self.cands)), key=lambda i: (i != best, -(scores[i] or 0), i))
        for position, index in enumerate(order):
            card = _ReplyCard(self, index, recommended=index == best, number=position, score=scores[index])
            self.replyBox.addWidget(card)
            self.cards.append(card)
        reply_to = result.get("reply_to")
        self.insightTitle.setText(f"对话参考 · 回复给 {reply_to}" if reply_to else "对话参考")
        answers = result.get("answers") or {}
        self.summary.setText("建议：" + _choice(answers, "best_action"))
        self.intent.setText("可能意图 · " + _choice(answers, "true_intent") +
                            "\n可能需要 · " + _choice(answers, "she_needs"))
        score = (answers.get("danger_level") or {}).get("score")
        valid_score = isinstance(score, (int, float)) and isfinite(score) and 0 <= score <= 9
        self.tension.setText(f"紧张度 {score:.0f}/9" if valid_score else "紧张度待判断")
        color = "#996819" if valid_score and score >= 3 else _MUTED
        if valid_score and score >= 6:
            color = "#b44832"
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(self.tension, qss, qss)
        self.empty.setVisible(not self.cands)
        self.insight.setVisible(bool(self.cands))
        self.referenceNote.setVisible(bool(self.cands) and not self._compact)
        self.updated.setText(datetime.now().strftime("%H:%M") + " 更新")
        if self.cands:
            self.set_status("建议已更新，选一句适合你的回复", "success")
        else:
            self.set_status("未生成可用回复，请等待下一条新消息。", "error")

    def _clear_cards(self):
        for card in self.cards:
            self.replyBox.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []

    def after(self, ms, fn):
        QTimer.singleShot(ms, fn)

    def run(self):
        self.app.exec()
