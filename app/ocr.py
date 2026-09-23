# -*- coding: utf-8 -*-
"""消息区截图 → 谁说了什么。RapidOCR 吃 numpy，不落盘。"""
import difflib
import re
import time

import numpy as np
from rapidocr_onnxruntime import RapidOCR


_ENGINE = None


def _engine():
    """OCR 引擎全进程共用：一个实例 ~40MB，每个会话一个 Reader，不能各带一个。
    det_limit_type 默认 'min' 会把小图放大到短边 736，裁小反而更慢；必须 'max'。"""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RapidOCR(intra_op_num_threads=4, det_limit_type="max", det_limit_side_len=4000)
    return _ENGINE


# OCR 幻觉过滤：图形/头像/表情包区域上引擎偶尔会"看"出不存在的短串（如 K<OY）。
# 1) 置信度低于 0.7 的框直接丢（实测真实文字 ≥ 0.84，留足余量）；
# 2) ≤8 字符、无汉字、且带 <>{}[]~^| 这类怪符号的也丢——正常中文聊天不会有这种串。
_MIN_OCR_SCORE = 0.7
_OCR_GHOST = re.compile(r"^[^\u4e00-\u9fff]{1,8}$")
_OCR_GHOST_CHARS = re.compile(r"[<>{}\[\]~^|]")


def _not_ghost(item):
    _box, text, score = item
    if float(score) < _MIN_OCR_SCORE:
        return False
    if _OCR_GHOST.match(text) and _OCR_GHOST_CHARS.search(text):
        return False
    return True


def read_header(band, x_boundary=None):
    """面板头部**整条**（从窗口左缘到消息区右缘）→ (会话名, 名字左缘 x, 分界 x)。

    x_boundary: chat_area 用底色找到的列表↔聊天区分界（物理 px，实测精准）。给了它，
    会话名 = 分界线**右侧**的文字框——搜索框、＋新建按钮都在分界线左侧，结构性排除，
    不需要按字符过滤（群名里带 + 也不受影响）。右上角的窗口控制按钮按比例（≥96% 宽度处）排除。
    没给 x_boundary（或分界右侧没 OCR 到字）就退回最大空隙法：最上一行文字框按 x 排，
    最大的空隙右侧是会话名。会话名里的成员数「(179)」去掉。"""
    res, _ = _engine()(band, use_cls=False)
    if not res:
        return "", None, None
    W_band = band.shape[1]
    res = [r for r in res if r[0][2][0] < W_band * 0.955]  # 右上角窗口控制按钮区
    if not res:
        return "", None, None
    first = min(res, key=lambda r: r[0][0][1])
    row = first[0][0][1] + (first[0][2][1] - first[0][0][1])  # 框底：顶在这之上的算同一行
    row_boxes = sorted((r for r in res if r[0][0][1] < row), key=lambda r: r[0][0][0])

    def _join(boxes):
        text = "".join(b[1] for b in boxes).strip()  # OCR 的框内空格已保留，框间不加空格
        return re.sub(r"\s*[（(]\d+[)）]\s*$", "", text)

    boundary = int(x_boundary) if x_boundary is not None else None
    title_boxes = []
    if x_boundary is not None:
        title_boxes = [b for b in row_boxes if b[0][0][0] >= x_boundary - 10]
    if not x_boundary or not title_boxes:  # 没分界 / 分界右侧没字：退回最大空隙法
        boundary = None
        if len(row_boxes) >= 2:
            gaps = [(row_boxes[i + 1][0][0][0] - row_boxes[i][0][2][0], i)
                    for i in range(len(row_boxes) - 1)]
            gap_w, gap_i = max(gaps)
            if gap_w >= 100:
                title_boxes = row_boxes[gap_i + 1:]
                boundary = int((row_boxes[gap_i][0][2][0] + title_boxes[0][0][0][0]) / 2)
        if not title_boxes:
            title_boxes = row_boxes  # 整行就一族：整行当会话名
    return _join(title_boxes), int(title_boxes[0][0][0][0]), boundary


def who_said(chat, box):
    """按 OCR 框里的颜色分类，不看 x 坐标。返回 (谁, 底色, 墨高)：
    先看底色平不平：框里众数颜色占比 <45% 就是图片（头像/照片/表情包）里的字 → None 丢掉。
    绿底 → me；非绿且文字对底色对比度 ≥150 → her；其余（引用块、群里的发言人名、时间戳、系统提示、
    链接卡片描述——都是灰字，对比度 80~95）→ "gray"。
    实测：气泡正文对比度 178~208，me 绿泡 142~150，灰字 ≤ 93。深浅主题都靠这套。
    墨高 = 框里最长一段连续有字的行数（OCR 框对小字有固定 padding、还会蹭到上下行，不能拿框高比大小）。"""
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    reg = chat[int(min(ys)):int(max(ys)), int(min(xs)):int(max(xs))].astype(int)
    if reg.size == 0:
        return None, None, 0
    vals, cnt = np.unique(reg.reshape(-1, 3), axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    if cnt.max() / reg.shape[0] / reg.shape[1] < 0.45:
        # 文字必须落在平底色上：WGC 帧是精确像素，气泡/面板里众数颜色占 0.56~0.82，
        # 头像/照片/表情包里只有 0.1~0.3——那是图片里的字（头像上的「借仲夏夜之梦」之类），不是消息。
        # ponytail: 只对精确像素的帧成立；缩放/压缩过的截图（比如拿预览窗再截一次的图）底色会糊成几百种颜色，全会被当图片。
        return None, bg, 0
    diff = np.abs(reg @ [0.299, 0.587, 0.114] - bg @ [0.299, 0.587, 0.114])
    ink_h = best = 0
    for r in (diff > 60).any(axis=1):
        best = best + 1 if r else 0
        ink_h = max(ink_h, best)
    if bg[1] > bg[0] + 40 and bg[1] > bg[2] + 40:
        return "me", bg, ink_h
    return ("her" if diff.max() >= 150 else "gray"), bg, ink_h


def similar(a, b):
    """同一段像素挪个位置 OCR 会抖（「傻逼了」↔「傻逼」、「不好意思」↔「不好竟思」），按相似度判同一条。"""
    if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
        return True
    return len(a) == len(b) >= 3 and sum(x != y for x, y in zip(a, b)) <= 1  # 短句错一个字


class Reader:
    """一个会话一个 Reader：lh/seen 各自算各自的，切走再切回来不会把旧消息当新的重报一遍。"""

    def __init__(self):
        self.ocr = _engine()
        self.lh = None  # 正常气泡字高，头一帧定
        self.seen = []  # [(who, name, text)]，累计，封顶 500
        self.last_boxes = []  # 调试视图用：[(x0,y0,x1,y1,kind,text)]，消息区裁剪坐标
        self.last_ms = 0  # 上一帧 OCR 耗时

    def read(self, chat, pane_bg, keep_images=False, mask_avatar_px=0):
        """→ [(who, name, text, y)]，同一气泡的多行已合并。who ∈ me/her；name 群聊里是发言人，单聊 None。
        顺带把每个框的分类记进 self.last_boxes（调试视图画框用，几十个 tuple，不开也不亏）。
        keep_images: 视觉模式用——图片消息（头像/表情包/截图）原本直接丢弃，开了就记成一条
        her 的「[图片]」占位：OCR 读不出它的内容，但视觉模型看得见，且它得能触发分析。
        mask_avatar_px: 文字模型模式用——先把左右两侧的头像列涂成面板底色再 OCR，
        引擎看不到头像，就不会在头像上幻觉出 K<OY 这类不存在的字符串。"""
        if mask_avatar_px:
            chat = chat.copy()
            chat[:, :mask_avatar_px] = pane_bg
            chat[:, chat.shape[1] - mask_avatar_px:] = pane_bg
        t0 = time.perf_counter()
        res, _ = self.ocr(chat, use_cls=False)
        res = [r for r in (res or []) if _not_ghost(r)]  # 低分/符号幻觉框直接丢
        self.last_ms = int((time.perf_counter() - t0) * 1000)
        self.last_boxes = []
        W = chat.shape[1]
        # 群聊：每条 her 气泡上方一行灰色发言人名（靠左、短、不带冒号、印在面板底色上），从上往下扫，名字带给后面的气泡。
        # 引用块/时间戳/公告带冒号，链接卡片灰字印在气泡底色上，都不会被当成名字。
        # ponytail: 名字行被 OCR 漏掉时会挂到上一个人头上。
        name, raw = None, []
        for box, text, _ in sorted(res or [], key=lambda r: r[0][0][1]):
            kind, bg, h = who_said(chat, box)
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            rect = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
            if kind == "gray":
                on_pane = np.abs(bg - pane_bg).sum() <= 6
                taken = bool(on_pane and box[0][0] < 0.25 * W and len(text) <= 16
                             and not re.search("[:：]", text))
                if taken:
                    name = text
                self.last_boxes.append(rect + ("name" if taken else "gray", text))
                continue
            if kind is None or (self.lh and h < 0.6 * self.lh):
                # 字比正常气泡小得多 = 图片消息（截图/表情包）里的字，不是气泡
                self.last_boxes.append(rect + ("image" if kind is None else "tiny", text))
                if keep_images and kind is None:
                    # 视觉模式：图片消息记成 her 的「[图片]」占位（内容丢给视觉模型看）；
                    # OCR 幻觉出来的小字不要，固定占位文本。表情包/截图/头像块都算。
                    raw.append(("her", name, "[图片]", box[0][1], box[2][1],
                                max(h, 0.8 * (self.lh or h))))
                continue
            self.last_boxes.append(rect + (kind, text))
            raw.append((kind, name if kind == "her" else None, text, box[0][1], box[2][1], h))
        if not self.lh and len(raw) >= 3:
            self.lh = float(np.median([r[5] for r in raw]))
        # 同一气泡的多行合并：同人、上一行底到这一行顶的间距不到半个字高（不同气泡之间至少隔一个字高）
        lines = []
        for who, nm, text, top, bottom, h in raw:
            if lines and lines[-1][0] == who and lines[-1][1] == nm and top - lines[-1][4] < 0.6 * (self.lh or h):
                lines[-1][2] += text
                lines[-1][4] = bottom
            else:
                lines.append([who, nm, text, top, bottom])
        # 语音输入的固定提示文案不是消息（双保险：输入框区本不该圈进消息区）
        lines = [l for l in lines if "语音输入" not in l[2] and "按住鼠标" not in l[2]]
        return [(w, n, t, y) for w, n, t, y, _ in lines]

    def new_lines(self, lines):
        """去重（滚动不重复）→ 这一帧里真正新出现的 [(who, name, text)]。
        本帧有已知行时只要已知行下方的：往上滚翻出来的旧消息在已知行上方，不算。
        本帧一行已知的都没有（大图把旧文字全顶出去了、切了聊天、滚远了）：全算，宁可多算不能漏。
        ponytail: 同一人连发两句一模一样的会吞一句——对触发分析无害。"""
        known_y = [y for w, n, t, y in lines if self._seen(w, n, t)]
        floor = max(known_y) if known_y else -1
        new = [(w, n, t) for w, n, t, y in lines if y > floor and not self._seen(w, n, t)]
        self.seen.extend((w, n, t) for w, n, t, _ in lines if not self._seen(w, n, t))
        del self.seen[:-500]
        return new

    def _seen(self, who, name, text):
        # 名字不参与判重：名字行滚出画面后同一条消息会从 her(LO) 变成 her，不能算新消息
        return any(w == who and similar(t, text) for w, _, t in self.seen)


from app.noise import is_system_noise  # noqa: F401  —— 识别规则在 app/noise.py（轻量，主进程也能用）
