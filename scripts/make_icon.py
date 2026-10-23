# -*- coding: utf-8 -*-
"""生成 py-tran 应用图标（assets/pytran.png + assets/pytran.ico）。

设计：深蓝→青渐变圆角底 + 白色对话气泡，气泡内为「A → 译」，
     表达「英文对话 → 中文」这一本工具的核心动作。

排版不靠猜字体度量：每个元素（A / 箭头 / 译）先单独渲染成透明图层，
按 getbbox() 拿到真实包围盒，再从左到右按固定间距排布、整体居中，
最后按 bbox 垂直中心对齐。换字体/换字号都不会跑偏或溢出气泡。

按尺寸分三档细节，保证 16px 到 256px 都干净：
  * >= 64  ：完整（A + 箭头 + 译）
  * 32/48  ：只保留放大版箭头（小字会糊成一团）
  * <= 24  ：只有底色 + 气泡（16/20 时省略尾巴）

本脚本是「一次性设计工具」，不属于运行时依赖：
运行时不需要 Pillow / numpy，产物（png/ico）直接入库。
用法（需装有 Pillow 的解释器）：
    python scripts/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- 常量

# 设计基准分辨率：所有几何坐标都按 512 画布给出，渲染时整体等比缩放。
DESIGN = 512

BG_TOP = (37, 99, 235)      # #2563EB 蓝（AI / 科技）
BG_BOTTOM = (6, 182, 212)   # #06B6D4 青
BG_MID = (21, 141, 224)     # 对角线中点，避免 2x2 渐变插值发灰
BUBBLE = (255, 255, 255)
SHADOW = (0, 0, 0, 70)
INK = (17, 40, 94)          # 气泡内文字/箭头的深蓝

# 气泡（含尾巴）几何
BUB_X0, BUB_Y0, BUB_X1, BUB_Y1 = 96, 116, 416, 348
BUB_R = 56
TAIL = [(168, 314), (240, 314), (190, 378)]
TAIL_R = 14                  # 尾巴根部与气泡的融合圆角

# 气泡内元素（尺寸为字号/长度；具体位置由自动排版决定）
A_SIZE = 100                  # "A" 字号
CN_SIZE = 90                  # "译" 字号
ARR_LEN = 38                  # 箭头主干长度
ARR_W = 11                    # 箭头主干线宽
GAP = 24                      # 元素间距
MARGIN = 26                   # 气泡内左右最小留白（自检依据）
SOLO_ARROW_SCALE = 1.7        # 仅箭头档（32/48）的放大倍数

# ICO 内嵌尺寸：Windows 资源管理器/任务栏会挑最合适的一档
ICO_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
PNG_SIZE = 512


# ---------------------------------------------------------------- 字体

def _candidates() -> tuple[tuple[str, str], ...]:
    """(拉丁字体, 中文字体) 候选，按优先级排列；命中第一个存在的组合。"""
    win = Path("C:/Windows/Fonts")
    return (
        (str(win / "segoeuib.ttf"), str(win / "msyhbd.ttc")),
        (str(win / "segoeui.ttf"), str(win / "msyh.ttc")),
        (str(win / "arialbd.ttf"), str(win / "simhei.ttf")),
    )


def _load_fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    """加载 A 与「译」的字体；全部失败时抛出，让调用方明确知道产物不可用。"""
    last: Exception | None = None
    for latin, cjk in _candidates():
        try:
            fa = ImageFont.truetype(latin, 64)
            fc = ImageFont.truetype(cjk, 64)
            # 真检一次：ttc 索引错误等在 truetype() 阶段未必暴露
            scratch = ImageDraw.Draw(Image.new("RGB", (8, 8)))
            scratch.text((0, 0), "A", font=fa)
            scratch.text((0, 0), "译", font=fc)
            return fa, fc
        except Exception as exc:  # noqa: BLE001 - 字体环境各异，逐个试
            last = exc
    raise RuntimeError(f"找不到可用字体（Latin + CJK）：{last}")


# ---------------------------------------------------------------- 底层绘制

def _gradient(size: int) -> Image.Image:
    """2x2 线性渐变放大到目标尺寸：比逐像素循环快几个数量级。"""
    small = Image.new("RGB", (2, 2))
    small.putpixel((0, 0), BG_TOP)
    small.putpixel((1, 1), BG_BOTTOM)
    small.putpixel((1, 0), BG_MID)
    small.putpixel((0, 1), BG_MID)
    return small.resize((size, size), Image.BICUBIC)


def _round_mask(size: int, radius: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=radius, fill=255
    )
    return mask


def _bubble_mask(size: int, k: float, offset: float = 0.0, tail: bool = True) -> Image.Image:
    """气泡（+尾巴）的 L 遮罩；offset 为向下阴影偏移（设计像素）。"""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle(
        (BUB_X0 * k, (BUB_Y0 + offset) * k, BUB_X1 * k, (BUB_Y1 + offset) * k),
        radius=BUB_R * k, fill=255,
    )
    if tail:
        d.polygon([(x * k, (y + offset) * k) for x, y in TAIL], fill=255)
        tx, ty = TAIL[0]
        d.ellipse(  # 尾巴根部加宽，与气泡自然衔接
            ((tx - TAIL_R) * k, (ty - TAIL_R + offset) * k,
             (tx + TAIL_R) * k, (ty + TAIL_R + offset) * k), fill=255,
        )
    return m


def _center_text(d, x: float, y: float, text: str, font, color) -> None:
    """以 (x, y) 为包围盒中心绘制文本（用 bbox 抵消字体内部留白）。"""
    box = d.textbbox((0, 0), text, font=font)
    d.text((x - (box[2] - box[0]) / 2 - box[0],
            y - (box[3] - box[1]) / 2 - box[1]), text, font=font, fill=color)


def _draw_arrow(d, x0: float, y: float, length: float, k: float,
                color: tuple[int, int, int]) -> None:
    w = max(1, round(ARR_W * k))
    x1 = x0 + length
    d.line((x0, y, x1, y), fill=color, width=w)
    head = max(2, round(19 * k))
    d.polygon([(x1, y - head), (x1 + head, y), (x1, y + head)], fill=color)


# ---------------------------------------------------------------- 元素图层

def _element(size: int, paint) -> tuple[Image.Image, int, int]:
    """把单个元素画到透明画布上，返回 (裁剪后的图层, 宽, 高)。

    paint(draw, k) 只需把元素画在画布中部，具体位置由排版阶段决定。
    """
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    paint(ImageDraw.Draw(layer), size / DESIGN, size)
    box = layer.getbbox()
    if box is None:  # 极端小尺寸下可能什么都画不出来
        return layer, 0, 0
    cropped = layer.crop(box)
    return cropped, cropped.width, cropped.height


def _glyph_layers(size: int, fa, fc, with_cjk: bool, arrow_scale: float = 1.0):
    k = size / DESIGN
    items = []

    if with_cjk:
        f_a = fa.font_variant(size=max(8, round(A_SIZE * k)))
        f_c = fc.font_variant(size=max(8, round(CN_SIZE * k)))
        items.append(_element(
            size, lambda d, kk, s, f=f_a: _center_text(d, s / 2, s / 2, "A", f, INK)))
        items.append(_element(
            size, lambda d, kk, s, kk2=k: _draw_arrow(
                d, s / 2 - ARR_LEN * kk2 / 2, s / 2, ARR_LEN * kk2, kk2, INK)))
        items.append(_element(
            size, lambda d, kk, s, f=f_c: _center_text(d, s / 2, s / 2, "译", f, INK)))
    elif arrow_scale:
        length = ARR_LEN * arrow_scale
        items.append(_element(
            size, lambda d, kk, s: _draw_arrow(
                d, s / 2 - length * kk / 2, s / 2, length * kk,
                kk * arrow_scale, INK)))
    return items


def _place(canvas: Image.Image, items, cx: float, cy: float, gap: float) -> None:
    """把元素图层从左到右等距排布、整体居中、按 bbox 垂直中心对齐。"""
    usable = [it for it in items if it[1] > 0]
    if not usable:
        return
    total = sum(it[1] for it in usable) + gap * (len(usable) - 1)
    x = cx - total / 2
    for layer, w, h in usable:
        canvas.alpha_composite(layer, (round(x), round(cy - h / 2)))
        x += w + gap


# ---------------------------------------------------------------- 组装

def render(size: int, fa, fc, with_cjk: bool, arrow_scale: float = 0.0,
           tail: bool = True) -> Image.Image:
    k = size / DESIGN
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(_gradient(size), (0, 0))

    # 圆角裁剪
    canvas.putalpha(_round_mask(size, radius=96 * k))

    transparent = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # 气泡阴影（整体下移 10 设计像素，压在圆角底内）
    canvas.alpha_composite(Image.composite(
        Image.new("RGBA", (size, size), SHADOW), transparent,
        _bubble_mask(size, k, offset=10, tail=tail)))
    # 白色气泡本体
    canvas.alpha_composite(Image.composite(
        Image.new("RGBA", (size, size), BUBBLE + (255,)), transparent,
        _bubble_mask(size, k, tail=tail)))

    if with_cjk or arrow_scale:
        cx = (BUB_X0 + BUB_X1) / 2 * k
        cy = (BUB_Y0 + BUB_Y1) / 2 * k
        _place(canvas, _glyph_layers(size, fa, fc, with_cjk, arrow_scale),
               cx, cy, GAP * k)
    return canvas


def build(size: int, fa, fc) -> Image.Image:
    if size >= 64:
        return render(size, fa, fc, with_cjk=True)
    if size >= 32:
        return render(size, fa, fc, with_cjk=False, arrow_scale=SOLO_ARROW_SCALE)
    # 16/20：尾巴在这么小的尺寸会糊成一坨，省略
    return render(size, fa, fc, with_cjk=False, arrow_scale=0.0, tail=size > 20)


# ---------------------------------------------------------------- 入口

def main() -> int:
    root = Path(__file__).resolve().parent.parent
    assets = root / "assets"
    assets.mkdir(exist_ok=True)

    try:
        fa, fc = _load_fonts()
    except RuntimeError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1

    png_path = assets / "pytran.png"
    build(PNG_SIZE, fa, fc).save(png_path, format="PNG", optimize=True)

    # 注意：Pillow 的 ICO 保存器会以「主图尺寸」为上界过滤 sizes
    # （IcoImagePlugin._save 中 `size[0] > width` 直接 continue），
    # 因此必须把最大的一帧作为主图，其余用 append_images 传入 ——
    # 否则最终 ICO 只会剩下一个 16x16 帧。
    frames = {s: build(s, fa, fc) for s in ICO_SIZES}
    main_size = max(ICO_SIZES)
    ico_path = assets / "pytran.ico"
    frames[main_size].save(
        ico_path, format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=[frames[s] for s in ICO_SIZES if s != main_size],
    )

    # 自检：元素不得溢出气泡（MARGIN 为最小留白）
    for s in ICO_SIZES:
        if s < 32:
            continue
        img = frames[s].convert("RGBA")
        k = s / DESIGN
        l_pad = (BUB_X0 + MARGIN) * k
        r_pad = (BUB_X1 - MARGIN) * k
        arr = img.crop((0, round(BUB_Y0 * k), s, round(BUB_Y1 * k)))
        px = arr.load()
        ok = True
        for y in range(arr.height):
            for x in range(arr.width):
                r, g, b, a = px[x, y]
                if a > 200 and abs(r - INK[0]) + abs(g - INK[1]) + abs(b - INK[2]) < 120:
                    if x < l_pad or x > r_pad:
                        ok = False
                        break
            if not ok:
                break
        if not ok:
            print(f"[警告] {s}x{s}：墨迹溢出气泡安全区", file=sys.stderr)

    for p in (png_path, ico_path):
        print(f"[OK] {p}  ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
