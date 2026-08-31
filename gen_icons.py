#!/usr/bin/env python3
"""生成飞牛应用图标（64x64 + 256x256），仅用标准库。
设计：蓝色渐变圆角背景 + 两个白色“NAS 服务器”方块 + 中间双向迁移箭头。
"""
import struct
import zlib

TOP = (79, 156, 255)     # #4f9cff
BOTTOM = (30, 58, 138)   # #1e3a8a
WHITE = (245, 248, 252)
SLOT = (59, 130, 246)    # 蓝色插槽
AMBER = (251, 191, 36)   # #fbbf24


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def write_png(path, w, h, pixels):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            r, g, b = pixels[y][x]
            raw += bytes((r, g, b))

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    with open(path, "wb") as f:
        f.write(sig)
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", idat))
        f.write(chunk(b"IEND", b""))


def render(S):
    """返回 S x S 的像素矩阵。坐标用 [0,1) 相对值。"""
    px = [[lerp(TOP, BOTTOM, y / S) for _ in range(S)] for y in range(S)]

    def sp(x, y, c):
        xi, yi = int(x * S), int(y * S)
        if 0 <= xi < S and 0 <= yi < S:
            px[yi][xi] = c

    def in_rounded(rx0, ry0, rx1, ry1, rr):
        r = rr * S
        x0, y0, x1, y1 = rx0 * S, ry0 * S, rx1 * S, ry1 * S
        res = [[False] * S for _ in range(S)]
        for yy in range(S):
            for xx in range(S):
                if x0 <= xx < x1 and y0 <= yy < y1:
                    # 圆角裁剪
                    cx = min(max(xx, x0 + r), x1 - r)
                    cy = min(max(yy, y0 + r), y1 - r)
                    dx, dy = xx - cx, yy - cy
                    if dx * dx + dy * dy <= r * r or (xx >= x0 + r and xx < x1 - r) or (yy >= y0 + r and yy < y1 - r):
                        res[yy][xx] = True
        return res

    def fill_rounded(rx0, ry0, rx1, ry1, rr, c):
        mask = in_rounded(rx0, ry0, rx1, ry1, rr)
        for yy in range(S):
            for xx in range(S):
                if mask[yy][xx]:
                    px[yy][xx] = c
        return mask

    def hline_in(mask, ry, rx0, rx1, thick, c):
        y = int(ry * S)
        t = max(1, int(thick * S))
        for yy in range(y, y + t):
            for xx in range(int(rx0 * S), int(rx1 * S)):
                if 0 <= yy < S and 0 <= xx < S and mask[yy][xx]:
                    px[yy][xx] = c

    def fill_tri(ax, ay, bx, by, cx, cy, c):
        # 包围盒内逐像素重心坐标判断
        axp, ayp = ax * S, ay * S
        bxp, byp = bx * S, by * S
        cxp, cyp = cx * S, cy * S
        minx = max(0, int(min(axp, bxp, cxp)))
        maxx = min(S - 1, int(max(axp, bxp, cxp)))
        miny = max(0, int(min(ayp, byp, cyp)))
        maxy = min(S - 1, int(max(ayp, byp, cyp)))
        for yy in range(miny, maxy + 1):
            for xx in range(minx, maxx + 1):
                px_, py_ = xx + 0.5, yy + 0.5
                d1 = (px_ - bxp) * (ayp - byp) - (axp - bxp) * (py_ - byp)
                d2 = (px_ - cxp) * (byp - cyp) - (bxp - cxp) * (py_ - cyp)
                d3 = (px_ - axp) * (cyp - ayp) - (cxp - axp) * (py_ - ayp)
                neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
                pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
                if not (neg and pos):
                    px[yy][xx] = c

    def thick_hline(ry, rx0, rx1, thick, c):
        y = int(ry * S)
        t = max(1, int(thick * S))
        for yy in range(y, y + t):
            for xx in range(int(rx0 * S), int(rx1 * S)):
                if 0 <= yy < S and 0 <= xx < S:
                    px[yy][xx] = c

    # 背景：圆角渐变已存在，这里把圆角外的像素设为透明背景灰（避免锯齿黑边）
    bg_mask = in_rounded(0.0, 0.0, 1.0, 1.0, 0.20)
    corner = (15, 17, 21)
    for yy in range(S):
        for xx in range(S):
            if not bg_mask[yy][xx]:
                px[yy][xx] = corner

    # 左 NAS 盒
    lb = fill_rounded(0.13, 0.24, 0.40, 0.76, 0.05, WHITE)
    for sy in (0.34, 0.46, 0.58):
        hline_in(lb, sy, 0.17, 0.36, 0.025, SLOT)

    # 右 NAS 盒
    rb = fill_rounded(0.60, 0.24, 0.87, 0.76, 0.05, WHITE)
    for sy in (0.34, 0.46, 0.58):
        hline_in(rb, sy, 0.64, 0.83, 0.025, SLOT)

    # 上箭头 →
    thick_hline(0.385, 0.43, 0.545, 0.05, AMBER)
    fill_tri(0.545, 0.345, 0.545, 0.435, 0.585, 0.39, AMBER)
    # 下箭头 ←
    thick_hline(0.615, 0.455, 0.57, 0.05, AMBER)
    fill_tri(0.455, 0.565, 0.455, 0.655, 0.415, 0.61, AMBER)

    return px


def downsample(p256, S=256, D=64):
    f = S // D
    out = [[(0, 0, 0)] * D for _ in range(D)]
    for yy in range(D):
        for xx in range(D):
            r = g = b = 0
            for dy in range(f):
                for dx in range(f):
                    pr, pg, pb = p256[yy * f + dy][xx * f + dx]
                    r += pr
                    g += pg
                    b += pb
            n = f * f
            out[yy][xx] = (r // n, g // n, b // n)
    return out


def main():
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    img_dir = os.path.join(here, "app", "ui", "images")
    os.makedirs(img_dir, exist_ok=True)

    p256 = render(256)
    p64 = downsample(p256)

    write_png(os.path.join(img_dir, "icon_256.png"), 256, 256, p256)
    write_png(os.path.join(img_dir, "icon_64.png"), 64, 64, p64)
    # 应用中心图标（根目录）
    write_png(os.path.join(here, "ICON_256.PNG"), 256, 256, p256)
    write_png(os.path.join(here, "ICON.PNG"), 64, 64, p64)
    print("icons generated:", os.listdir(img_dir), "+ ICON.PNG, ICON_256.PNG")


if __name__ == "__main__":
    main()
