# -*- coding: utf-8 -*-
"""生成应用图标 app.ico（纯标准库，不依赖 Pillow）。

图形：圆角方块蓝底 + 白色对话气泡 + 三个点。超采样做抗锯齿。
"""
import struct
import zlib

SIZE = 256
SS = 4                    # 每个像素 4x4 采样
BLUE = (37, 99, 235)      # #2563eb
DARK = (29, 78, 216)      # 稍深，做气泡外轮廓用不上，留作阴影备选
WHITE = (255, 255, 255)


def in_rrect(x, y, x0, y0, x1, y1, r):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    if x < x0 + r:
        cx = x0 + r
    elif x > x1 - r:
        cx = x1 - r
    else:
        return True
    if y < y0 + r:
        cy = y0 + r
    elif y > y1 - r:
        cy = y1 - r
    else:
        return True
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_tri(px, py, ax, ay, bx, by, cx, cy):
    d1 = (px - bx) * (ay - by) - (ax - bx) * (py - by)
    d2 = (px - cx) * (by - cy) - (bx - cx) * (py - cy)
    d3 = (px - ax) * (cy - ay) - (cx - ax) * (py - ay)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def in_disc(x, y, cx, cy, r):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def sample(u, v):
    """返回该点的颜色或 None（透明）。u/v 是 0..256 浮点坐标。"""
    # 底板：留 6 像素边距，圆角 56
    if not in_rrect(u, v, 6, 6, 250, 250, 52):
        return None
    # 对话气泡
    if in_rrect(u, v, 50, 62, 206, 166, 26):
        return WHITE
    # 气泡尾巴
    if in_tri(u, v, 74, 150, 74, 210, 128, 160):
        return WHITE
    return BLUE


def render(size):
    """渲染 size×size 的 RGBA 像素列表。"""
    scale = SIZE / float(size)
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc = [0, 0, 0]
            cov = 0
            n = 0
            for sy in range(SS):
                for sx in range(SS):
                    u = (px + (sx + 0.5) / SS) * scale
                    v = (py + (sy + 0.5) / SS) * scale
                    c = sample(u, v)
                    n += 1
                    if c is None:
                        continue
                    cov += 1
                    acc[0] += c[0]
                    acc[1] += c[1]
                    acc[2] += c[2]
            if cov == 0:
                row += b"\x00\x00\x00\x00"
                continue
            r = acc[0] // cov
            g = acc[1] // cov
            b = acc[2] // cov
            a = int(round(255 * cov / float(n)))
            row += bytes((r, g, b, a))
        rows.append(bytes(row))
    return rows


def to_png(rows, w, h):
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main():
    sizes = [256, 128, 64, 48, 32, 16]
    blobs = []
    for s in sizes:
        png = to_png(render(s), s, s)
        blobs.append((s, png))
        if s == 256:
            with open("icon_preview.png", "wb") as f:
                f.write(png)

    count = len(blobs)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = b""
    data = b""
    for s, png in blobs:
        w = 0 if s == 256 else s        # ICO 里 0 表示 256
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        data += png
    with open("app.ico", "wb") as f:
        f.write(header + entries + data)
    print("app.ico 已生成，%d 个尺寸，共 %d 字节" % (count, 6 + 16 * count + len(data)))


if __name__ == "__main__":
    main()
