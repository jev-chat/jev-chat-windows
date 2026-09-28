# -*- coding: utf-8 -*-
"""Windows.Media.Ocr backend, for languages RapidOCR's bundled zh/en models cannot read (ko, ja, ...).

Offline like RapidOCR — the recognizer ships with the OS language pack, nothing leaves the machine —
and on a 760x1280 KakaoTalk window it is ~30x faster (60ms vs 2.0s).

Same call shape as RapidOCR so app/ocr.py can swap one for the other:
    engine(img, use_cls=False) -> ([[box, text, score], ...], None)
where box is 4 clockwise (x, y) points. Windows returns words, not bubbles, so words on one line are
glued into runs and split where the horizontal gap opens up (bubble edge, right-aligned own message).
"""
from __future__ import annotations

import asyncio

import numpy as np
from winsdk.windows.globalization import Language
from winsdk.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.security.cryptography import CryptographicBuffer

GAP = 0.8    # split a line into runs where the gap between words exceeds this * word height
SCALE = 2    # nearest-neighbour upscale before recognition: on a 760px-wide KakaoTalk window it
             # drops the stray one-character fragments ("I", "口(") for +30ms; 3x brings them back


def available(lang: str | None = None) -> bool:
    try:
        return _engine_for(lang) is not None
    except Exception:
        return False


def _engine_for(lang: str | None):
    if lang:
        return OcrEngine.try_create_from_language(Language(lang))
    return OcrEngine.try_create_from_user_profile_languages()


class WindowsOcr:
    """One engine per process; the event loop is kept alive because a frame arrives every 50ms."""

    def __init__(self, lang: str | None = None):
        self._engine = _engine_for(lang)
        if self._engine is None:
            raise RuntimeError(
                f"Windows OCR has no recognizer for {lang or 'the user profile languages'}. "
                "Add the language under Settings > Time & language > Language & region.")
        self._loop = asyncio.new_event_loop()

    @property
    def language(self) -> str:
        return self._engine.recognizer_language.language_tag

    def _scale_for(self, h: int, w: int) -> int:
        """Windows 拒绝超过 MaxImageDimension 的图，放大前先按它收一收。
        属性名在 winsdk 里是 snake_case；取不到就按官方下限 2600 兜底。"""
        limit = getattr(OcrEngine, "max_image_dimension", 0) or 2600
        return max(1, min(SCALE, limit // max(1, max(h, w))))

    def __call__(self, img: np.ndarray, use_cls: bool = False):
        scale = self._scale_for(*img.shape[:2])
        if scale > 1:
            img = np.repeat(np.repeat(img, scale, axis=0), scale, axis=1)
        h, w = img.shape[:2]
        bgra = np.dstack([img[:, :, ::-1], np.full((h, w, 1), 255, np.uint8)])
        buf = CryptographicBuffer.create_from_byte_array(bgra.tobytes())
        bitmap = SoftwareBitmap.create_copy_from_buffer(buf, BitmapPixelFormat.BGRA8, w, h,
                                                        BitmapAlphaMode.PREMULTIPLIED)
        result = self._loop.run_until_complete(_recognize(self._engine, bitmap))
        # boxes go back in source pixels: app/ocr.py indexes the original frame with them
        return [run for line in result.lines for run in _runs(line, scale)], None

    def close(self):
        self._loop.close()


async def _recognize(engine, bitmap):
    return await engine.recognize_async(bitmap)


def _runs(line, scale: int = 1):
    """Words of one OCR line -> [[box, text, score]] per run, split on wide horizontal gaps."""
    words = [(w.bounding_rect, w.text) for w in line.words]
    out, cur = [], []
    for rect, text in words:
        if cur and rect.x - (cur[-1][0].x + cur[-1][0].width) > GAP * rect.height:
            out.append(_box(cur, scale))
            cur = []
        cur.append((rect, text))
    if cur:
        out.append(_box(cur, scale))
    return out


def _box(run, scale: int = 1):
    x0 = min(r.x for r, _ in run) / scale
    y0 = min(r.y for r, _ in run) / scale
    x1 = max(r.x + r.width for r, _ in run) / scale
    y1 = max(r.y + r.height for r, _ in run) / scale
    text = " ".join(t for _, t in run)
    # Windows OCR reports no per-word confidence; 1.0 keeps the RapidOCR tuple shape usable.
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1]], text, 1.0]


if __name__ == "__main__":  # 自测：分段和坐标还原不碰引擎，没装语言包也能跑
    class _R:
        def __init__(self, x, w, h=20, y=0):
            self.x, self.y, self.width, self.height = x, y, w, h

    class _W:
        def __init__(self, rect, text):
            self.bounding_rect, self.text = rect, text

    class _L:
        def __init__(self, words):
            self.words = words

    engine = WindowsOcr.__new__(WindowsOcr)   # 不建引擎也能测放大上限
    cap = getattr(OcrEngine, "max_image_dimension", 0) or 2600
    assert engine._scale_for(100, 100) == SCALE           # 小图照常放大
    assert engine._scale_for(cap, cap) == 1               # 已经顶到上限，不能再放大
    assert engine._scale_for(cap // SCALE, 10) == SCALE   # 刚好放得下
    assert engine._scale_for(cap // SCALE + 1, 10) < SCALE
    line = _L([_W(_R(0, 40), "배포"), _W(_R(45, 40), "안"), _W(_R(400, 30), "오전")])
    runs = _runs(line)
    assert [t for _, t, _ in runs] == ["배포 안", "오전"], runs
    (box, _, score), = _runs(_L([_W(_R(10, 40, y=6), "하이")]), scale=2)
    assert box[0] == [5.0, 3.0] and box[2] == [25.0, 13.0] and score == 1.0, box
    print("ocr_windows ok")
