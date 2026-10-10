from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from typing import Iterable

U8_MAGIC = 0x55AA382D
TPL_MAGIC = 0x0020AF30

# GX texture formats used by TPL.
GX_TF_I4 = 0
GX_TF_I8 = 1
GX_TF_IA4 = 2
GX_TF_IA8 = 3
GX_TF_RGB565 = 4
GX_TF_RGB5A3 = 5
GX_TF_RGBA8 = 6
GX_TF_C4 = 8
GX_TF_C8 = 9
GX_TF_C14X2 = 10
GX_TF_CMPR = 14

_FORMAT_NAMES = {
    GX_TF_I4: "I4",
    GX_TF_I8: "I8",
    GX_TF_IA4: "IA4",
    GX_TF_IA8: "IA8",
    GX_TF_RGB565: "RGB565",
    GX_TF_RGB5A3: "RGB5A3",
    GX_TF_RGBA8: "RGBA8",
    GX_TF_C4: "C4",
    GX_TF_C8: "C8",
    GX_TF_C14X2: "C14X2",
    GX_TF_CMPR: "CMPR",
}


@dataclass(frozen=True)
class U8File:
    name: str
    offset: int
    size: int


@dataclass(frozen=True)
class TPLInfo:
    file_offset: int
    file_size: int
    width: int
    height: int
    fmt: int
    image_data_offset: int
    image_data_size: int
    palette_header_offset: int = 0
    palette_data_offset: int = 0
    palette_entries: int = 0
    palette_format: int = -1

    @property
    def format_name(self) -> str:
        return _FORMAT_NAMES.get(self.fmt, f"fmt_{self.fmt}")


# Tiny deterministic bitmap font. The game's name plate is itself a texture,
# so this keeps the runtime self-contained and avoids a PIL/font dependency.
_FONT_5X7: dict[str, tuple[str, ...]] = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    " ": ("00000",) * 7,
}


def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from(">H", data, off)[0]


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from(">I", data, off)[0]


def _cstring(data: bytes, off: int, limit: int = 256) -> str:
    if off < 0 or off >= len(data):
        return ""
    end = data.find(b"\x00", off, min(len(data), off + limit))
    if end < 0:
        end = min(len(data), off + limit)
    return data[off:end].decode("ascii", "ignore")


def find_u8_file(blob: bytes, filename: str) -> U8File | None:
    """Find one file by basename in a Wii U8 archive contained in *blob*.

    blob should begin at the archive base. Both exact path and basename matches
    are accepted because UI archives often store files under arc/timg/.
    """
    if len(blob) < 0x20 or _u32(blob, 0) != U8_MAGIC:
        return None
    root_off = _u32(blob, 4)
    if root_off < 0x10 or root_off + 12 > len(blob):
        return None
    root_type_name = _u32(blob, root_off)
    if (root_type_name >> 24) != 1:
        return None
    node_count = _u32(blob, root_off + 8)
    if node_count <= 0 or node_count > 0x10000:
        return None
    nodes_end = root_off + node_count * 12
    if nodes_end > len(blob):
        return None
    strings_off = nodes_end
    wanted = str(filename).replace("\\", "/").lower()
    wanted_base = wanted.rsplit("/", 1)[-1]
    for i in range(1, node_count):
        noff = root_off + i * 12
        type_name = _u32(blob, noff)
        if (type_name >> 24) != 0:
            continue
        name_off = type_name & 0x00FFFFFF
        name = _cstring(blob, strings_off + name_off)
        if not name:
            continue
        norm = name.replace("\\", "/").lower()
        base = norm.rsplit("/", 1)[-1]
        if norm != wanted and base != wanted_base:
            continue
        file_off = _u32(blob, noff + 4)
        file_size = _u32(blob, noff + 8)
        if file_off < 0 or file_size <= 0 or file_off + file_size > len(blob):
            continue
        return U8File(name=name, offset=file_off, size=file_size)
    return None


def find_embedded_u8(blob: bytes, filename: str) -> tuple[int, U8File] | None:
    """Locate a U8 archive inside a larger memory blob and return base + file."""
    magic = struct.pack(">I", U8_MAGIC)
    start = 0
    while True:
        pos = blob.find(magic, start)
        if pos < 0:
            return None
        found = find_u8_file(blob[pos:], filename)
        if found is not None:
            return pos, found
        start = pos + 4


def _texture_size(width: int, height: int, fmt: int) -> int:
    specs = {
        GX_TF_I4: (8, 8, 32),
        GX_TF_I8: (8, 4, 32),
        GX_TF_IA4: (8, 4, 32),
        GX_TF_IA8: (4, 4, 32),
        GX_TF_RGB565: (4, 4, 32),
        GX_TF_RGB5A3: (4, 4, 32),
        GX_TF_RGBA8: (4, 4, 64),
        GX_TF_C4: (8, 8, 32),
        GX_TF_C8: (8, 4, 32),
        GX_TF_C14X2: (4, 4, 32),
        GX_TF_CMPR: (8, 8, 32),
    }
    spec = specs.get(int(fmt))
    if spec is None:
        return 0
    bw, bh, bsize = spec
    return math.ceil(width / bw) * math.ceil(height / bh) * bsize


def parse_tpl(blob: bytes, file_offset: int = 0, file_size: int | None = None) -> TPLInfo | None:
    base = int(file_offset)
    size = int(len(blob) - base if file_size is None else file_size)
    if base < 0 or size < 0x20 or base + size > len(blob):
        return None
    if _u32(blob, base) != TPL_MAGIC:
        return None
    count = _u32(blob, base + 4)
    table_off = _u32(blob, base + 8)
    if count < 1 or count > 256 or table_off + 8 > size:
        return None
    desc = base + table_off
    image_hdr_rel = _u32(blob, desc)
    palette_hdr_rel = _u32(blob, desc + 4)
    image_hdr = base + image_hdr_rel
    if image_hdr_rel <= 0 or image_hdr + 0x24 > base + size:
        return None
    height = _u16(blob, image_hdr)
    width = _u16(blob, image_hdr + 2)
    fmt = _u32(blob, image_hdr + 4)
    data_rel = _u32(blob, image_hdr + 8)
    data_abs = base + data_rel
    data_size = _texture_size(width, height, fmt)
    if width <= 0 or height <= 0 or data_size <= 0:
        return None
    if data_rel <= 0 or data_abs + data_size > base + size:
        return None

    pal_data_rel = 0
    pal_entries = 0
    pal_fmt = -1
    if palette_hdr_rel:
        ph = base + palette_hdr_rel
        if ph + 0x0C <= base + size:
            pal_entries = _u16(blob, ph)
            pal_fmt = _u32(blob, ph + 4)
            pal_data_rel = _u32(blob, ph + 8)
            if pal_data_rel and base + pal_data_rel + pal_entries * 2 > base + size:
                pal_data_rel = 0
                pal_entries = 0
                pal_fmt = -1

    return TPLInfo(
        file_offset=base,
        file_size=size,
        width=width,
        height=height,
        fmt=fmt,
        image_data_offset=data_rel,
        image_data_size=data_size,
        palette_header_offset=palette_hdr_rel,
        palette_data_offset=pal_data_rel,
        palette_entries=pal_entries,
        palette_format=pal_fmt,
    )


def find_name_tpl(blob: bytes, filename: str = "Name_ryu.tpl") -> tuple[int, TPLInfo] | None:
    """Find Name_ryu.tpl in a loaded UI archive, with a safe heuristic fallback."""
    embedded = find_embedded_u8(blob, filename)
    if embedded is not None:
        archive_base, file = embedded
        info = parse_tpl(blob, archive_base + file.offset, file.size)
        if info is not None:
            return archive_base, info

    # Fallback for already-unpacked/archive-fragment captures: locate TPLs and
    # choose the most name-plate-like wide texture. This never writes by itself.
    magic = struct.pack(">I", TPL_MAGIC)
    candidates: list[tuple[float, int, TPLInfo]] = []
    start = 0
    while True:
        pos = blob.find(magic, start)
        if pos < 0:
            break
        info = parse_tpl(blob, pos, len(blob) - pos)
        if info is not None:
            ratio = info.width / max(1, info.height)
            score = 0.0
            if ratio >= 2.0:
                score += 10.0
            if info.height <= 128:
                score += 4.0
            if info.width >= 64:
                score += 2.0
            score += min(4.0, ratio)
            candidates.append((score, pos, info))
        start = pos + 4
    if not candidates:
        return None
    candidates.sort(key=lambda row: row[0], reverse=True)
    _score, pos, info = candidates[0]
    return pos, info


def render_text_rgba(width: int, height: int, text: str) -> list[tuple[int, int, int, int]]:
    """Render centered name-plate text.

    Prefer pygame's antialiased system font because TvCGUI already ships with
    pygame. Fall back to the deterministic 5x7 font if font rendering is not
    available in a stripped/headless environment.
    """
    w = max(1, int(width))
    h = max(1, int(height))
    text = str(text or "").upper()

    try:
        import pygame
        if not pygame.font.get_init():
            pygame.font.init()
        # Arial Black/Impact-like weight reads much closer to the game's large
        # name plate than a regular UI font. SysFont falls back safely on hosts
        # where the exact family is unavailable.
        size = max(8, int(h * 0.82))
        font = None
        while size >= 8:
            font = pygame.font.SysFont("Arial Black", size, bold=True)
            tw, th = font.size(text)
            if tw <= max(1, w - 4) and th <= max(1, h - 2):
                break
            size -= 1
        if font is not None:
            surf = pygame.Surface((w, h), flags=pygame.SRCALPHA, depth=32)
            surf.fill((0, 0, 0, 0))
            shadow = font.render(text, True, (0, 0, 0, 170))
            main = font.render(text, True, (255, 255, 255, 255))
            x = max(0, (w - main.get_width()) // 2)
            y = max(0, (h - main.get_height()) // 2)
            surf.blit(shadow, (x + 1, y + 1))
            surf.blit(main, (x, y))
            raw = pygame.image.tostring(surf, "RGBA")
            return [tuple(raw[i:i + 4]) for i in range(0, len(raw), 4)]  # type: ignore[list-item]
    except Exception:
        pass

    glyphs = [_FONT_5X7.get(ch, _FONT_5X7[" "]) for ch in text]
    units_w = max(1, len(glyphs) * 6 - 1)
    units_h = 7
    scale = max(1, min(max(1, (w - 4) // units_w), max(1, (h - 4) // units_h)))
    draw_w = units_w * scale
    draw_h = units_h * scale
    x0 = max(0, (w - draw_w) // 2)
    y0 = max(0, (h - draw_h) // 2)

    px = [(255, 255, 255, 0) for _ in range(w * h)]

    def put(x: int, y: int, rgba: tuple[int, int, int, int]) -> None:
        if 0 <= x < w and 0 <= y < h:
            idx = y * w + x
            old = px[idx]
            if rgba[3] >= old[3]:
                px[idx] = rgba

    shadow = max(1, scale // 3)
    cursor = x0
    for glyph in glyphs:
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit != "1":
                    continue
                for yy in range(scale):
                    for xx in range(scale):
                        put(cursor + gx * scale + xx + shadow, y0 + gy * scale + yy + shadow, (0, 0, 0, 150))
        cursor += 6 * scale

    cursor = x0
    for glyph in glyphs:
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit != "1":
                    continue
                for yy in range(scale):
                    for xx in range(scale):
                        put(cursor + gx * scale + xx, y0 + gy * scale + yy, (255, 255, 255, 255))
        cursor += 6 * scale
    return px


def _lum(r: int, g: int, b: int) -> int:
    return max(0, min(255, int(round(0.299 * r + 0.587 * g + 0.114 * b))))


def _rgb565(r: int, g: int, b: int) -> int:
    return ((r * 31 + 127) // 255 << 11) | ((g * 63 + 127) // 255 << 5) | ((b * 31 + 127) // 255)


def _decode_rgb565(v: int) -> tuple[int, int, int, int]:
    r = ((v >> 11) & 31) * 255 // 31
    g = ((v >> 5) & 63) * 255 // 63
    b = (v & 31) * 255 // 31
    return r, g, b, 255


def _rgb5a3(r: int, g: int, b: int, a: int) -> int:
    if a >= 224:
        return 0x8000 | ((r * 31 + 127) // 255 << 10) | ((g * 31 + 127) // 255 << 5) | ((b * 31 + 127) // 255)
    return ((a * 7 + 127) // 255 << 12) | ((r * 15 + 127) // 255 << 8) | ((g * 15 + 127) // 255 << 4) | ((b * 15 + 127) // 255)


def _decode_rgb5a3(v: int) -> tuple[int, int, int, int]:
    if v & 0x8000:
        r = ((v >> 10) & 31) * 255 // 31
        g = ((v >> 5) & 31) * 255 // 31
        b = (v & 31) * 255 // 31
        return r, g, b, 255
    a = ((v >> 12) & 7) * 255 // 7
    r = ((v >> 8) & 15) * 255 // 15
    g = ((v >> 4) & 15) * 255 // 15
    b = (v & 15) * 255 // 15
    return r, g, b, a


def _decode_palette_entry(v: int, fmt: int) -> tuple[int, int, int, int]:
    if fmt == 0:  # IA8 palette: high intensity, low alpha in common TPL tools.
        i = (v >> 8) & 0xFF
        a = v & 0xFF
        return i, i, i, a
    if fmt == 1:
        return _decode_rgb565(v)
    if fmt == 2:
        return _decode_rgb5a3(v)
    return 255, 255, 255, 255


def _nearest_palette_index(rgba: tuple[int, int, int, int], palette: list[tuple[int, int, int, int]]) -> int:
    r, g, b, a = rgba
    best_i = 0
    best_d = 1 << 62
    for i, (pr, pg, pb, pa) in enumerate(palette):
        # Alpha matters strongly for text plates.
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2 + 3 * (a - pa) ** 2
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def encode_texture(
    pixels: list[tuple[int, int, int, int]],
    width: int,
    height: int,
    fmt: int,
    *,
    palette_bytes: bytes = b"",
    palette_entries: int = 0,
    palette_format: int = -1,
) -> bytes:
    """Encode RGBA pixels into the existing TPL image format and tiled layout."""
    w, h = int(width), int(height)
    if len(pixels) != w * h:
        raise ValueError("pixel count does not match texture dimensions")

    def get(x: int, y: int) -> tuple[int, int, int, int]:
        if 0 <= x < w and 0 <= y < h:
            return pixels[y * w + x]
        return 255, 255, 255, 0

    out = bytearray()

    if fmt == GX_TF_I4:
        for by in range(0, h, 8):
            for bx in range(0, w, 8):
                vals: list[int] = []
                for y in range(8):
                    for x in range(8):
                        r, g, b, a = get(bx + x, by + y)
                        vals.append((_lum(r, g, b) * a // 255) >> 4)
                for i in range(0, 64, 2):
                    out.append((vals[i] << 4) | vals[i + 1])
    elif fmt in (GX_TF_I8, GX_TF_IA4):
        for by in range(0, h, 4):
            for bx in range(0, w, 8):
                for y in range(4):
                    for x in range(8):
                        r, g, b, a = get(bx + x, by + y)
                        i = _lum(r, g, b)
                        if fmt == GX_TF_I8:
                            out.append(i * a // 255)
                        else:
                            out.append(((a >> 4) << 4) | (i >> 4))
    elif fmt in (GX_TF_IA8, GX_TF_RGB565, GX_TF_RGB5A3):
        for by in range(0, h, 4):
            for bx in range(0, w, 4):
                for y in range(4):
                    for x in range(4):
                        r, g, b, a = get(bx + x, by + y)
                        if fmt == GX_TF_IA8:
                            i = _lum(r, g, b)
                            out.extend(bytes((i, a)))
                        elif fmt == GX_TF_RGB565:
                            out.extend(struct.pack(">H", _rgb565(r * a // 255, g * a // 255, b * a // 255)))
                        else:
                            out.extend(struct.pack(">H", _rgb5a3(r, g, b, a)))
    elif fmt == GX_TF_RGBA8:
        for by in range(0, h, 4):
            for bx in range(0, w, 4):
                block = [get(bx + x, by + y) for y in range(4) for x in range(4)]
                for r, g, b, a in block:
                    out.extend(bytes((a, r)))
                for r, g, b, a in block:
                    out.extend(bytes((g, b)))
    elif fmt in (GX_TF_C4, GX_TF_C8, GX_TF_C14X2):
        if palette_entries <= 0 or len(palette_bytes) < palette_entries * 2:
            raise ValueError("paletted TPL has no usable palette")
        palette = [
            _decode_palette_entry(struct.unpack_from(">H", palette_bytes, i * 2)[0], palette_format)
            for i in range(palette_entries)
        ]
        if fmt == GX_TF_C4:
            for by in range(0, h, 8):
                for bx in range(0, w, 8):
                    vals = [_nearest_palette_index(get(bx + x, by + y), palette) & 0xF for y in range(8) for x in range(8)]
                    for i in range(0, 64, 2):
                        out.append((vals[i] << 4) | vals[i + 1])
        elif fmt == GX_TF_C8:
            for by in range(0, h, 4):
                for bx in range(0, w, 8):
                    for y in range(4):
                        for x in range(8):
                            out.append(_nearest_palette_index(get(bx + x, by + y), palette) & 0xFF)
        else:
            for by in range(0, h, 4):
                for bx in range(0, w, 4):
                    for y in range(4):
                        for x in range(4):
                            idx = _nearest_palette_index(get(bx + x, by + y), palette) & 0x3FFF
                            out.extend(struct.pack(">H", idx))
    elif fmt == GX_TF_CMPR:
        # Wii CMPR is four DXT1 4x4 blocks inside each tiled 8x8 block.
        for by in range(0, h, 8):
            for bx in range(0, w, 8):
                for sy in (0, 4):
                    for sx in (0, 4):
                        block = [get(bx + sx + x, by + sy + y) for y in range(4) for x in range(4)]
                        # Use transparent DXT1 mode: c0 <= c1. Black, white,
                        # grey, transparent gives excellent binary text plates.
                        c0, c1 = 0x0000, 0xFFFF
                        out.extend(struct.pack(">HH", c0, c1))
                        for y in range(4):
                            bits = 0
                            for x in range(4):
                                r, g, b, a = block[y * 4 + x]
                                if a < 96:
                                    idx = 3
                                else:
                                    lum = _lum(r, g, b)
                                    idx = 1 if lum >= 170 else (2 if lum >= 64 else 0)
                                bits |= (idx & 3) << (6 - x * 2)
                            out.append(bits)
    else:
        raise ValueError(f"unsupported TPL texture format {fmt}")

    expected = _texture_size(w, h, fmt)
    if len(out) != expected:
        raise ValueError(f"encoded size mismatch: got {len(out)} expected {expected}")
    return bytes(out)
