# -*- coding: utf-8 -*-
"""浅色置顶回复助手：回复建议和独立设置页。发送始终由用户在微信确认。"""
import threading
from datetime import datetime
from math import isfinite
from types import SimpleNamespace

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QFrame, QHBoxLayout, QListWidget, QListWidgetItem,
    QMessageBox, QSizeGrip, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CardWidget, CheckBox, ComboBox, EditableComboBox, FluentIcon as FIF,
    HyperlinkButton, IndeterminateProgressBar, LineEdit, PasswordLineEdit, PlainTextEdit,
    PrimaryPushButton, PushButton, ScrollArea, SpinBox, SwitchButton, Theme, TransparentToolButton,
    setCustomStyleSheet, setFont, setTheme, setThemeColor,
)

from app import settings
from app.version import VERSION
from core import jev_client, llm, providers
from core.questions import CHOICE_LABELS

_LOG_LINES = 300
_MUTED = "#68776f"
_GREEN = "#18794e"
_RELATIONSHIPS = [
    ("女友 / 恋人", "romantic partners"), ("追求对象", "romantic interest"),
    ("暧昧对象", "ambiguous romantic interest"), ("朋友", "friends"),
    ("同事", "colleagues"),
    ("家人", "family"), ("自定义", None),
]


def _choice(answers, name):
    return CHOICE_LABELS[name].get((answers.get(name) or {}).get("choice"), "暂未判断")


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


class _TitleBar(QWidget):
    """只有标题栏可拖动，选择正文或按按钮不会意外移动窗口。"""
    def __init__(self, parent):
        super().__init__(parent)
        self._drag = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.window().pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
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
        self.fillButton = (PrimaryPushButton if recommended else PushButton)("填入当前对话", self)
        self.fillButton.setAccessibleName(
            f"填入{'推荐回复' if recommended else f'备选 {number}'}到当前对话")
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


class Overlay:
    def __init__(self, on_fill, on_toggle_capture=None, on_target_change=None, result_of=None,
                 on_toggle_debug=None, on_settings_saved=None, on_retry=None,
                 on_history_generate=None, on_settings_reset=None):
        """result_of(会话名) → 那个会话上次的结果或 None；切着看别的会话时用它把旧结果放回来。
        on_target_change(会话名, 人名) → 用户在群里挑了回复对象。
        on_toggle_debug(开不开) → 开关调试视图那个独立窗口。"""
        self.app = QApplication.instance() or QApplication([])
        setTheme(Theme.LIGHT)
        setThemeColor(_GREEN, save=False)
        self.on_fill = on_fill
        self.on_toggle_capture = on_toggle_capture
        self.on_target_change = on_target_change
        self.on_toggle_debug = on_toggle_debug
        self.on_settings_saved = on_settings_saved
        self.on_retry = on_retry
        self.on_history_generate = on_history_generate
        self.on_settings_reset = on_settings_reset
        self.result_of = result_of
        self.cands = []
        self.cards = []
        self._busy = False
        self._current = False
        self._compact = None  # 断点模式：None 保证 _relayout 第一次调用必定生效
        self._pageLayouts = []
        self._hintLabels = []
        self.feeds = {}  # {会话名: [消息记录 dict]}
        self.counts = {}  # {会话名: 消息条数}
        self.hers = {}  # {会话名: 对方最近一句}
        self.targets = {}  # {会话名: ([发言人], 当前回复对象)}
        self._selected_history = None
        self._retry_chats = set()
        self._chat = ""  # 微信当前开着的会话
        self._shown = ""  # 界面上正在看的会话（浏览时和上面不一样）
        self.win = _MainWindow(self._relayout)
        self.win.setObjectName("assistantWindow")
        self.win.setWindowTitle(f"Jev · {settings.chat_app_name()}回复助手")
        self.win.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.win.setStyleSheet(
            "QWidget#assistantWindow { background: #f5f7f6; border: 1px solid #dce3de; border-radius: 14px; }"
        )
        self.win.setMinimumWidth(320)
        self.win.setMaximumWidth(640)
        outer = QVBoxLayout(self.win)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        header = _TitleBar(self.win)
        title = QHBoxLayout(header)
        title.setContentsMargins(18, 12, 10, 10)
        title.setSpacing(8)
        name = _label("Jev", 20, "#233c2f", True)
        name.setFixedWidth(40)
        name.setAttribute(Qt.WA_TransparentForMouseEvents)
        title.addWidget(name)
        self.subtitle = _label(f"{settings.chat_app_name()}回复助手", 12, _MUTED)
        self.subtitle.setAttribute(Qt.WA_TransparentForMouseEvents)
        title.addWidget(self.subtitle, 1)
        self.captureSwitch = SwitchButton(header)
        self.captureSwitch.setOnText("采集中")
        self.captureSwitch.setOffText("已暂停")
        self.captureSwitch.setToolTip(f"开启或暂停{settings.chat_app_name()}采集")
        self.captureSwitch.setAccessibleName(f"开启或暂停{settings.chat_app_name()}采集")
        self.captureSwitch.setChecked(False)
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
        self.set_capture(False, "会话内容不再读取")
        if not settings.has_key():
            self.set_status("请先在设置中配置模型", "warning")
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
        self.chatBox.setToolTip("聊天窗口切到哪个会话这里就跟到哪个；也可以自己选一个，只看它的记录和建议")
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
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.status = _label("", 12, _MUTED)
        status_row.addWidget(self.status, 1)
        self.retryButton = PushButton("重试")
        self.retryButton.setAccessibleName("重新生成回复")
        self.retryButton.setToolTip("上次生成失败时，使用相同的聊天内容再试一次")
        self.retryButton.clicked.connect(self._retry_clicked)
        self.retryButton.hide()
        status_row.addWidget(self.retryButton)
        body.addLayout(status_row)
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
        self.insight.setToolTip("根据当前聊天片段推测，可能理解有偏差。紧张度/重视度为 0–9 的参考评分：身体不适、情绪低落和关系冲突都会提高。")
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
        self.emptyHint = _label("保持聊天窗口打开。\n收到新消息后，回复建议会出现在这里。", 13, _MUTED)
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

        self.historyButton = PushButton(FIF.HISTORY, "聊天记录")
        self.historyButton.clicked.connect(self._toggle_history)
        self.historyButton.setAccessibleName("展开或收起聊天记录")
        body.addWidget(self.historyButton)
        self.feed = QListWidget()
        self.feed.setSelectionMode(QAbstractItemView.SingleSelection)
        self.feed.setWordWrap(True)
        self.feed.setTextElideMode(Qt.ElideNone)
        self.feed.setSpacing(2)
        self.feed.setFixedHeight(160)
        self.feed.itemClicked.connect(self._history_item_clicked)
        self.feed.hide()
        body.addWidget(self.feed)
        self.selectMessageButton = PushButton("按选中消息生成回复")
        self.selectMessageButton.setToolTip("点击聊天记录中的一条消息，再按这里；会按该消息之前的上下文重新生成")
        self.selectMessageButton.clicked.connect(self._generate_selected_history)
        self.selectMessageButton.hide()
        body.addWidget(self.selectMessageButton)
        self._history_title()
        body.addStretch(1)

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
        app_label = _label("聊天窗口", 13)
        box.addWidget(app_label)
        self.chatAppBox = ComboBox()
        self.chatAppBox.addItems(["微信", "QQ"])
        self.chatAppBox.setAccessibleName("聊天窗口")
        app_label.setBuddy(self.chatAppBox)
        box.addWidget(self.chatAppBox)
        box.addWidget(self._hint("选择要采集和填入的聊天软件；保存后会自动切换采集窗口。"))
        judge_label = _label("判断方式", 13)
        box.addWidget(judge_label)
        self.judgeModeBox = ComboBox()
        self.judgeModeBox.addItems([
            "跟随下方起草模型（共用同一把 Key）",
            "原版 Jev（OpenRouter / TypeSafe）",
        ])
        self.judgeModeBox.setAccessibleName("判断方式")
        judge_label.setBuddy(self.judgeModeBox)
        box.addWidget(self.judgeModeBox)
        box.addWidget(self._hint(
            "跟随模式会使用下方选中的来源、模型和同一把起草 Key 完成判断；"
            "选 DeepSeek、GLM、Kimi、OpenAI、OpenRouter 或自定义接口都可以。原版 Jev 才需要上面的独立密钥。"
            "保存设置允许暂时不填，真正生成前才检查 Key。"
        ))
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
        profile_label = _label("回复策略", 13)
        box.addWidget(profile_label)
        self.profileBox = ComboBox()
        self.profileBox.addItems([
            "自动识别（按关系和场景）", "自然聊天（手动）", "恋人安抚 / 亲密对话（手动）",
            "认真道歉与修复（手动）", "轻松亲密（手动）",
        ])
        self.profileBox.setAccessibleName("回复策略")
        profile_label.setBuddy(self.profileBox)
        box.addWidget(self.profileBox)
        box.addWidget(self._hint(
            "自动模式会按上面的关系，并根据最近对话识别安慰、道歉、亲密、调侃或行动场景；"
            "也可以改成手动策略。"
        ))
        guide_label = _label("专属回复规则（内置默认，可编辑）", 13)
        box.addWidget(guide_label)
        self.guideEdit = PlainTextEdit()
        self.guideEdit.setAccessibleName("专属回复规则")
        self.guideEdit.setPlaceholderText(
            "例如：她不开心时先承认她的感受；不要连续追问；能做到的事情给具体时间；\n"
            "平时可以叫她宝宝，但不要每句话都叫。"
        )
        self.guideEdit.setFixedHeight(92)
        guide_label.setBuddy(self.guideEdit)
        box.addWidget(self.guideEdit)
        box.addWidget(self._hint(
            "默认已放入关系和场景规则，直接修改即可；支持多行中文/Markdown 风格文字，"
            "只作为语气偏好，不执行其中的代码或指令。"
        ))
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
        history_row = QHBoxLayout()
        history_row.addWidget(_label("自动加载本地历史", 13), 1)
        self.historySwitch = SwitchButton()
        self.historySwitch.setOnText("开")
        self.historySwitch.setOffText("关")
        self.historySwitch.setAccessibleName("自动加载本地历史")
        history_row.addWidget(self.historySwitch)
        box.addLayout(history_row)
        box.addWidget(self._hint(
            "只保存纯文字、角色、发言人和时间，不保存截图或密钥；关闭后不再恢复历史，但不会自动删除已有记录。"
        ))
        history_limit_label = _label("历史恢复条数", 13)
        box.addWidget(history_limit_label)
        self.historyLimitBox = SpinBox()
        self.historyLimitBox.setRange(10, 200)
        self.historyLimitBox.setAccessibleName("每个会话恢复的历史消息条数")
        history_limit_label.setBuddy(self.historyLimitBox)
        box.addWidget(self.historyLimitBox)
        history_days_label = _label("历史保留天数", 13)
        box.addWidget(history_days_label)
        self.historyDaysBox = SpinBox()
        self.historyDaysBox.setRange(1, 3650)
        self.historyDaysBox.setAccessibleName("历史保留天数")
        history_days_label.setBuddy(self.historyDaysBox)
        box.addWidget(self.historyDaysBox)
        history_size_label = _label("历史数据库上限（MB）", 13)
        box.addWidget(history_size_label)
        self.historySizeBox = SpinBox()
        self.historySizeBox.setRange(1, 2048)
        self.historySizeBox.setAccessibleName("历史数据库最大大小")
        history_size_label.setBuddy(self.historySizeBox)
        box.addWidget(self.historySizeBox)
        box.addWidget(self._hint(
            "启动和采集时会自动清理超过天数或大小上限的最早文字记录；默认 40 条、7 天、50 MB。"
        ))
        target_row = QHBoxLayout()
        target_row.addWidget(_label("群聊指定回复对象", 13), 1)
        self.targetSwitch = SwitchButton()
        self.targetSwitch.setOnText("开")
        self.targetSwitch.setOffText("关")
        self.targetSwitch.setAccessibleName("群聊指定回复对象")
        target_row.addWidget(self.targetSwitch)
        box.addLayout(target_row)
        box.addWidget(self._hint(
            "开了以后群聊里可以选回复给谁，候选会针对 TA 写，填入时可带 @。关了就正常回复。"
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
        self.jev = self._model_group(box, "判断 · Jev", "jev", providers.JEV_PROVIDERS)
        box.addWidget(self._hint(
            "原版判断：识别意图、情绪、紧张度，并给三条候选排序。只有选择原版 Jev 时才需要这里的密钥。"
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
        think_row.addWidget(self.thinkingSwitch)
        box.addLayout(think_row)
        box.addWidget(self._hint(
            "关：秒回，够用。开：模型先想再写，更斟酌但慢好几倍、贵一些。"
            "只有 " + " / ".join(providers.THINKING) + " 认这个开关。"
        ))
        body.addWidget(models)
        self.settingsFeedback = _label("", 13, _GREEN)
        self.settingsFeedback.hide()
        body.addWidget(self.settingsFeedback)
        actions = QHBoxLayout()
        back = PushButton("返回")
        back.clicked.connect(self._back_home)
        actions.addWidget(back)
        reset = PushButton("重置设置")
        reset.setToolTip("清空本机保存的 API 密钥和个人配置，恢复分享用默认值")
        reset.setAccessibleName("重置设置并清空密钥")
        reset.clicked.connect(self._reset_settings)
        actions.addWidget(reset)
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
        if kind == "draft":  # 只有两个「自定义」来源要自己填地址，别的来源这一行藏着
            self.baseLabel = _label("Base URL", 13)
            box.addWidget(self.baseLabel)
            self.baseEdit = LineEdit()
            self.baseEdit.setPlaceholderText("https://你的服务/v1")
            self.baseEdit.setAccessibleName("自定义来源 Base URL")
            self.baseLabel.setBuddy(self.baseEdit)
            box.addWidget(self.baseEdit)
        key_label = _label("密钥", 13)
        box.addWidget(key_label)
        group.keyEdit = PasswordLineEdit()
        group.keyEdit.setAccessibleName(f"{title} API 密钥")
        key_label.setBuddy(group.keyEdit)
        group.keyEdit.returnPressed.connect(self._save)
        box.addWidget(group.keyEdit)
        box.addWidget(self._hint(
            "OpenRouter 的 key 或 TypeSafe 的 key，看上面选的来源。" if kind == "jev"
            else "上面选哪家就填哪家的 key；DeepSeek 判断模式也会复用这把起草 key；换来源重填一次，只存这一把。"))
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
        group.fetchButton.setAccessibleName(f"获取{title}的可用模型列表")
        group.fetchButton.clicked.connect(lambda: self._fetch_models(group))
        row.addWidget(group.fetchButton)
        box.addLayout(row)
        group.status = _label("", 12, _MUTED)
        box.addWidget(group.status)
        group.providerBox.currentIndexChanged.connect(lambda _: self._provider_changed(group))
        return group

    @staticmethod
    def _provider_of(group):
        return group.ids[max(0, group.providerBox.currentIndex())]

    def _provider_changed(self, group):
        """换来源：模型框回到这家该有的值（存的就是这家才用存的，否则用它的默认），状态清掉。"""
        provider = self._provider_of(group)
        saved = settings.jev_provider() if group.kind == "jev" else settings.draft_provider()
        stored = settings.jev_model() if group.kind == "jev" else settings.draft_model()
        group.modelBox.clear()
        group.modelBox.setText(stored if provider == saved else group.table[provider].default)
        group.status.setText("")
        self._sync_model_fields()

    def _sync_model_fields(self):
        """两组共用：密钥已配置/未配置、占位文案、自定义 Base URL 行的显隐，
        外加紧凑模式下把来源按钮上的文字省略——ComboBox 是 QPushButton，
        minimumSizeHint 按整段文字算，不会自动换行/省略，长名字会把设置页撑宽。"""
        # 0=跟随起草，1=原版 Jev。
        # 只有原版 Jev 需要启用上面的独立来源/密钥。
        unused_jev = self.judgeModeBox.currentIndex() != 1
        for group in (self.jev, self.draft):
            provider = self._provider_of(group)
            name = group.table[provider].name
            configured = bool(group.stored_key())
            unused = group.kind == "jev" and unused_jev
            group.keyState.setText("本模式不用" if unused else ("已配置" if configured else "未配置"))
            group.keyEdit.setPlaceholderText(
                "跟随/DeepSeek 模式下无需填写" if unused
                else ("已配置，留空保留" if configured else f"输入 {name} API 密钥"))
            group.keyEdit.setEnabled(not unused)
            group.providerBox.setEnabled(not unused)
            group.modelBox.setEnabled(not unused)
            # 原来的按钮允许先点再提示“先填密钥”；只有 DeepSeek 模式下不用的 Jev 组才禁用。
            group.fetchButton.setEnabled(not unused)
            if self._compact:
                name = group.providerBox.fontMetrics().elidedText(name, Qt.ElideRight, 180)
            group.providerBox.setText(name)
        custom = self._provider_of(self.draft) in providers.CUSTOM
        self.baseLabel.setVisible(custom)
        self.baseEdit.setVisible(custom)

    def _fetch_models(self, group):
        """「获取模型」：拿填的 key（没填就拿存的）去问接口，网络调用丢后台线程。"""
        provider = self._provider_of(group)
        custom = group.kind == "draft" and provider in providers.CUSTOM
        base = self.baseEdit.text().strip() if custom else None
        key = group.keyEdit.text().strip() or group.stored_key()
        if not key:
            group.status.setText("先填密钥")
            return
        if custom and not base:
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
                models = jev_client.list_models(provider, key)
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
        """把存下来的来源和模型放回一组控件里；填充不算用户操作，别触发换来源的重置。"""
        group.providerBox.blockSignals(True)
        group.providerBox.setCurrentIndex(group.ids.index(provider))
        group.providerBox.blockSignals(False)
        group.keyEdit.clear()
        group.modelBox.clear()
        group.modelBox.setText(model)
        group.status.setText("")

    def _load_settings(self):
        relationship = settings.relationship()
        index = next((i for i, (_, value) in enumerate(_RELATIONSHIPS) if value == relationship),
                     len(_RELATIONSHIPS) - 1)
        self.relationshipBox.setCurrentIndex(index)
        self.relEdit.setText(relationship if _RELATIONSHIPS[index][1] is None else "")
        self.relEdit.setVisible(_RELATIONSHIPS[index][1] is None)
        self.chatAppBox.setCurrentIndex(1 if settings.chat_app() == "qq" else 0)
        self.judgeModeBox.setCurrentIndex({"follow": 0, "jev": 1}.get(
            settings.judge_mode(), 0))
        self.styleEdit.setText(settings.style())
        self.profileBox.setCurrentIndex(
            {"auto": 0, "natural": 1, "girlfriend": 2, "repair": 3, "playful": 4}.get(
                settings.reply_profile(), 0))
        self.guideEdit.setPlainText(settings.reply_guide())
        self.contextBox.setValue(settings.context())
        self.historySwitch.setChecked(settings.history_enabled())
        self.historyLimitBox.setValue(settings.history_limit())
        self.historyDaysBox.setValue(settings.history_retention_days())
        self.historySizeBox.setValue(settings.history_max_mb())
        self.targetSwitch.setChecked(settings.reply_target())
        self._set_group(self.jev, settings.jev_provider(), settings.jev_model())
        self._set_group(self.draft, settings.draft_provider(), settings.draft_model())
        self.baseEdit.setText(settings.draft_base_url())
        self.thinkingSwitch.setChecked(settings.thinking())
        self.set_debug_switch(settings.debug_view())  # 屏蔽信号地拨，别在加载时开关一遍窗口
        self._sync_model_fields()  # 上面屏蔽了信号，这里补一次
        self.settingsFeedback.hide()

    def _save(self):
        relationship = _RELATIONSHIPS[self.relationshipBox.currentIndex()][1]
        relationship = relationship or self.relEdit.text().strip()
        chat_app = "qq" if self.chatAppBox.currentIndex() == 1 else "wechat"
        judge_mode = ("follow", "jev")[self.judgeModeBox.currentIndex()]
        jev_provider = self._provider_of(self.jev)
        draft_provider = self._provider_of(self.draft)
        base = self.baseEdit.text().strip()
        profile = ("auto", "natural", "girlfriend", "repair", "playful")[self.profileBox.currentIndex()]
        if not relationship:
            self._settings_feedback("请填写关系背景，或选择一个已有选项。", error=True)
            self.relEdit.setFocus()
            return
        if draft_provider in providers.CUSTOM and not base:
            self._settings_feedback("自定义来源要填 Base URL。", error=True)
            self.baseEdit.setFocus()
            return
        groups = [(self.draft, draft_provider)]
        if judge_mode == "jev":
            groups.insert(0, (self.jev, jev_provider))
        for group, provider in groups:
            name = group.table[provider].name
            if not group.modelBox.text().strip():
                self._settings_feedback(f"{name} 请先获取并选择一个模型。", error=True)
                group.modelBox.setFocus()
                return
        try:
            settings.save(relationship, self.contextBox.value(),
                          jev_provider_text=jev_provider,
                          jev_key_text=self.jev.keyEdit.text().strip() or None,
                          jev_model_text=self.jev.modelBox.text().strip(),
                          draft_provider_text=draft_provider,
                          llm_key_text=self.draft.keyEdit.text().strip() or None,
                          draft_model_text=self.draft.modelBox.text().strip(),
                          draft_base_url_text=base,
                          reply_target_on=self.targetSwitch.isChecked(),
                          style_text=self.styleEdit.text().strip(),
                          reply_profile_text=profile,
                          reply_guide_text=self.guideEdit.toPlainText().strip(),
                          thinking_on=self.thinkingSwitch.isChecked(),
                          chat_app_text=chat_app,
                          judge_mode_text=judge_mode,
                          history_enabled_on=self.historySwitch.isChecked(),
                          history_limit_n=self.historyLimitBox.value(),
                          history_days_n=self.historyDaysBox.value(),
                          history_max_mb_n=self.historySizeBox.value())
        except Exception:
            self._settings_feedback("保存失败，请检查配置文件是否可写后重试。", error=True)
            return
        self._load_settings()
        self.win.setWindowTitle(f"Jev · {settings.chat_app_name()}回复助手")
        self.subtitle.setText(f"{settings.chat_app_name()}回复助手")
        self.captureSwitch.setToolTip(f"开启或暂停{settings.chat_app_name()}采集")
        self.captureSwitch.setAccessibleName(f"开启或暂停{settings.chat_app_name()}采集")
        if self.on_settings_saved:
            self.on_settings_saved()
        self._render_targets()  # 开关刚改过，回到首页时这一行该显该藏得重算一次
        self._settings_feedback("设置已保存，将用于下一次回复。")
        self.setupButton.hide()
        if not self.cands and not self._busy:
            self._empty_text()
            self.set_status("设置已就绪，等待新消息", "idle")

    def _reset_settings(self):
        """清空本机 key 和个人配置，方便把当前程序目录分享出去。"""
        answer = QMessageBox.question(
            self.win, "重置设置",
            "将清空本机保存的全部 API 密钥、个人设置和聊天历史，恢复为分享用默认值。\n确定继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            settings.reset()
        except Exception:
            self._settings_feedback("重置失败，请检查配置文件是否可写。", error=True)
            return
        self._load_settings()
        self.win.setWindowTitle(f"Jev · {settings.chat_app_name()}回复助手")
        self.subtitle.setText(f"{settings.chat_app_name()}回复助手")
        self.captureSwitch.setToolTip(f"开启或暂停{settings.chat_app_name()}采集")
        self.captureSwitch.setAccessibleName(f"开启或暂停{settings.chat_app_name()}采集")
        self.captureSwitch.blockSignals(True)
        self.captureSwitch.setChecked(False)
        self.captureSwitch.blockSignals(False)
        if self.on_settings_saved:
            self.on_settings_saved()
        if self.on_settings_reset:
            self.on_settings_reset()
        self._clear_cards()
        self.cands = []
        self._current = False
        self.insight.hide()
        self.referenceNote.hide()
        self._settings_feedback("已重置：密钥、设置和本地聊天历史已清空，聊天窗口恢复为微信，采集默认暂停。")

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
        self.jev.keyEdit.clear()
        self.draft.keyEdit.clear()
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

    def _retry_clicked(self):
        """把最近一次失败的同一会话、同一批消息再交给后台生成。"""
        if self._busy or not self._shown or self._shown not in self._retry_chats:
            return
        self.retryButton.setEnabled(False)
        if self.on_retry:
            self.on_retry(self._shown)
        self._sync_retry()

    def _history_item_clicked(self, item):
        """聊天记录里的消息可选中；状态行没有 UserRole 数据，不参与生成。"""
        data = item.data(Qt.UserRole)
        if not data:
            self._selected_history = None
            self.selectMessageButton.hide()
            return
        self._selected_history = data
        _, _, who, text, name, timestamp = data
        speaker = (name or "对方") if who == "her" else "我"
        self.selectMessageButton.setText("按这条消息生成回复")
        self.selectMessageButton.setToolTip("会使用这条消息之前的聊天上下文，忽略之后的表情包或已发送内容")
        self.selectMessageButton.show()
        self.set_status(f"已选中 {timestamp} 的「{speaker}」消息，点击下方重新生成", "idle")

    def _generate_selected_history(self):
        if self._busy or not self._selected_history or not self.on_history_generate:
            return
        self.selectMessageButton.setEnabled(False)
        self.on_history_generate(self._selected_history)

    def _sync_retry(self):
        visible = self._shown in self._retry_chats and not self._busy
        self.retryButton.setVisible(visible)
        self.retryButton.setEnabled(visible)

    def mark_retry(self, title):
        self._retry_chats.add(title)
        if title == self._shown:
            self._sync_retry()

    def clear_retry(self, title):
        self._retry_chats.discard(title)
        if title == self._shown:
            self._sync_retry()

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
            self.set_status(reason or "会话内容不再读取", "warning")
        elif configured:
            self.set_status("等待新消息", "idle")
        else:
            self.set_status("请先在设置中配置模型", "warning")
        if self._busy or self.cands:  # 正在生成或已有候选时，空态卡片本来就看不见
            return
        if not on:
            self.emptyTitle.setText("采集已暂停")
            self.emptyHint.setText("会话内容暂时不再读取。\n打开标题栏的开关，继续接收新消息。")
            self.setupButton.setVisible(not configured)
        else:
            self._empty_text()

    def set_busy(self, busy):
        self._busy = busy
        self.progress.setVisible(busy)
        self.selectMessageButton.setEnabled(not busy)
        if busy:
            self.retryButton.hide()
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
        if not busy:
            self._sync_retry()
            if self._selected_history and not self.feed.isHidden():
                self.selectMessageButton.show()

    def _empty_text(self):
        """空态卡片的默认文案，配好没配好两套说法。"""
        configured = settings.has_key()
        self.emptyTitle.setText("等待对方的新消息" if configured else "先设置，再开始")
        self.emptyHint.setText("保持聊天窗口打开。\n收到新消息后，回复建议会出现在这里。"
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

    def _toggle_history(self):
        self.feed.setVisible(self.feed.isHidden())
        if self.feed.isHidden():
            self.selectMessageButton.hide()
        elif self._selected_history:
            self.selectMessageButton.show()
        self._history_title()

    def _history_title(self):
        action = "展开" if self.feed.isHidden() else "收起"
        count = self.counts.get(self._shown, 0)
        self.historyButton.setText(f"{action}聊天记录" + (f" · {count}" if count else ""))

    def log(self, line):
        """采集状态行：只进正在看的那个会话，不按会话存。"""
        item = QListWidgetItem(line)
        item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
        self.feed.addItem(item)
        while self.feed.count() > _LOG_LINES:
            self.feed.takeItem(0)
        self.feed.scrollToBottom()

    def _add_message_item(self, chat, index, record):
        who, text, name, timestamp = (record["who"], record["text"],
                                      record["name"], record["timestamp"])
        speaker = (name or "对方") if who == "her" else "我"
        item = QListWidgetItem(f"{timestamp}  {speaker}\n{text}")
        item.setToolTip("点击选中这条消息，再按下方按钮生成回复")
        item.setData(Qt.UserRole, (chat, index, who, text, name, timestamp))
        self.feed.addItem(item)
        self.feed.scrollToBottom()
        return item

    def log_message(self, who, text, name="", timestamp=None, chat=None):
        """按会话存一份；只有正在看的那个会往显示区里写。"""
        chat = chat or self._shown
        speaker = (name or "对方") if who == "her" else "我"
        timestamp = timestamp or datetime.now().strftime("%H:%M")
        self.counts[chat] = self.counts.get(chat, 0) + 1
        record = {"who": who, "text": text, "name": name, "timestamp": timestamp}
        records = self.feeds.setdefault(chat, [])
        records.append(record)
        del records[:-_LOG_LINES]
        if who == "her":
            self.hers[chat] = text
        self._add_chat(chat)
        if chat != self._shown:
            return
        self._add_message_item(chat, len(records) - 1, record)
        if who == "her":
            self._show_latest(text)
        self._history_title()

    def restore_messages(self, chat, records):
        """把本地历史恢复到现有聊天记录视图；只更新内存和 UI，不再次写库。"""
        for record in records or []:
            self.log_message(record.get("who", "her"), record.get("text", ""),
                             record.get("name", ""), record.get("timestamp") or "", chat=chat)

    def clear_history(self):
        """重置设置后的界面清空；磁盘清理由 app.settings.reset() 完成。"""
        self.feeds.clear()
        self.counts.clear()
        self.hers.clear()
        self.targets.clear()
        self._selected_history = None
        self._chat = ""
        self._shown = ""
        self.feed.clear()
        self.chatBox.blockSignals(True)
        self.chatBox.clear()
        self.chatBox.blockSignals(False)
        self.selectMessageButton.hide()
        self._history_title()

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
        self.feed.clear()
        self._selected_history = None
        self.selectMessageButton.hide()
        for index, record in enumerate(self.feeds.get(title, [])):
            self._add_message_item(title, index, record)
        her = self.hers.get(title)
        if her:
            self._show_latest(her)
        else:
            self.context.hide()
        self._history_title()
        self._follow_text()
        self._render_targets()
        self.show_cached(self.result_of(title) if self.result_of else None)
        self._sync_retry()

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
        self.chatFollow.setText(("跟随对话" if self._shown == self._chat else "浏览中") if self._chat else "")

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
                            "\n当前场景 · " + _choice(answers, "reply_scene") +
                            "\n可能需要 · " + _choice(answers, "she_needs"))
        score = (answers.get("danger_level") or {}).get("score")
        valid_score = isinstance(score, (int, float)) and isfinite(score) and 0 <= score <= 9
        self.tension.setText(f"紧张度/重视度 {score:.0f}/9" if valid_score else "紧张度/重视度待判断")
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
