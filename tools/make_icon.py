# -*- coding: utf-8 -*-
"""生成 docs/icon.ico、docs/icon.png 和 Linux hicolor PNG。
圆角绿底 + 白色 J。图标进仓库，换颜色/字母时才需要重跑：python tools/make_icon.py"""
import os

from PIL import Image, ImageDraw, ImageFont

GREEN = "#18794e"
ICO_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICO_OUT = os.path.join(ROOT, "docs", "icon.ico")
PNG_OUT = os.path.join(ROOT, "docs", "icon.png")
HICOLOR = os.path.join(ROOT, "packaging", "linux", "icons", "hicolor")
FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
)


def font(px):
    path = next((p for p in FONTS if os.path.exists(p)), None)
    return ImageFont.truetype(path, px) if path else ImageFont.load_default(px)


def _draw_j(d, size):
    """字体不可用时画一个粗 J，小尺寸图标也能认。"""
    s = float(size)
    w = max(2, round(s * 0.14))
    x0, x1 = s * 0.30, s * 0.70
    y0, y1 = s * 0.18, s * 0.78
    stem_l = x1 - w
    d.rounded_rectangle((x0, y0, x1, y0 + w), radius=w // 2, fill="white")
    d.rounded_rectangle((stem_l, y0, x1, y1 - w * 0.9), radius=w // 2, fill="white")
    box = (x0, y1 - w * 3.2, x1, y1)
    d.arc(box, start=0, end=190, fill="white", width=w)


def render(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=max(1, round(56 * size / 256)), fill=GREEN)
    fpx = max(8, round(168 * size / 256))
    try:
        d.text((size / 2, size * 120 / 256), "J", font=font(fpx), fill="white", anchor="mm")
    except Exception:
        _draw_j(d, size)
    return img


def main():
    os.makedirs(os.path.dirname(ICO_OUT), exist_ok=True)
    master = render(256)
    master.save(ICO_OUT, sizes=ICO_SIZES)
    master.save(PNG_OUT)
    assert os.path.getsize(ICO_OUT) > 1024, ICO_OUT
    for size in PNG_SIZES:
        dest_dir = os.path.join(HICOLOR, f"{size}x{size}", "apps")
        os.makedirs(dest_dir, exist_ok=True)
        render(size).save(os.path.join(dest_dir, "jev-chat-ubuntu.png"))
    print(f"{ICO_OUT}  {os.path.getsize(ICO_OUT)} bytes  {len(ICO_SIZES)} sizes")
    print(f"{PNG_OUT}  {os.path.getsize(PNG_OUT)} bytes")
    print(f"{HICOLOR}  {len(PNG_SIZES)} png sizes")


if __name__ == "__main__":
    main()
