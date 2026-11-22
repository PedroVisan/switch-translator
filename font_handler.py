from pathlib import Path
import struct
from cpk_handler import _decompress_crilayla, _parse_utf_table, repack_cpk

PT_GLYPH_SPECS = [
    # Minúsculas acentuadas
    ("á", "a", "acute"),
    ("à", "a", "grave"),
    ("â", "a", "circumflex"),
    ("ã", "a", "tilde"),
    ("é", "e", "acute"),
    ("ê", "e", "circumflex"),
    ("í", "i", "acute"),
    ("ó", "o", "acute"),
    ("ô", "o", "circumflex"),
    ("õ", "o", "tilde"),
    ("ú", "u", "acute"),
    ("ü", "u", "diaeresis"),
    ("ç", "c", "cedilla"),
    # Maiúsculas acentuadas
    ("Á", "A", "acute"),
    ("À", "A", "grave"),
    ("Â", "A", "circumflex"),
    ("Ã", "A", "tilde"),
    ("É", "E", "acute"),
    ("Ê", "E", "circumflex"),
    ("Í", "I", "acute"),
    ("Ó", "O", "acute"),
    ("Ô", "O", "circumflex"),
    ("Õ", "O", "tilde"),
    ("Ú", "U", "acute"),
    ("Ü", "U", "diaeresis"),
    ("Ç", "C", "cedilla"),
    # Ordinais
    ("º", "o", "ordinal"),
    ("ª", "a", "ordinal"),
]


def _utf8_key(ch: str) -> int:
    return int.from_bytes(ch.encode("utf-8"), "big")


def _decode_4bpp(raw: bytes, w: int, h: int) -> list[list[int]]:
    grid = [[0] * w for _ in range(h)]
    total = w * h
    for idx in range(total):
        b_idx = idx // 2
        if b_idx >= len(raw):
            break
        byte_val = raw[b_idx]
        nib = (byte_val >> 4) if (idx % 2 == 0) else (byte_val & 0x0F)
        grid[idx // w][idx % w] = nib
    return grid


def _encode_4bpp(grid: list[list[int]]) -> bytes:
    h = len(grid)
    w = len(grid[0]) if h else 0
    out = bytearray((w * h + 1) // 2)
    idx = 0
    for y in range(h):
        row = grid[y]
        for x in range(w):
            val = max(0, min(15, row[x]))
            if idx % 2 == 0:
                out[idx // 2] |= val << 4
            else:
                out[idx // 2] |= val
            idx += 1
    return bytes(out)


def _ink_bbox(grid: list[list[int]], threshold: int = 2) -> tuple[int, int, int, int] | None:
    h = len(grid)
    w = len(grid[0]) if h else 0
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            if grid[y][x] >= threshold:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _crop(grid: list[list[int]], bbox: tuple[int, int, int, int]) -> list[list[int]]:
    x0, y0, x1, y1 = bbox
    return [row[x0 : x1 + 1] for row in grid[y0 : y1 + 1]]


def _flip_h(grid: list[list[int]]) -> list[list[int]]:
    return [list(reversed(row)) for row in grid]


def _scale_grid(grid: list[list[int]], new_w: int, new_h: int) -> list[list[int]]:
    src_h = len(grid)
    src_w = len(grid[0]) if src_h else 0
    if src_w == 0 or src_h == 0 or new_w <= 0 or new_h <= 0:
        return [[0]]
    out = [[0] * new_w for _ in range(new_h)]
    for dy in range(new_h):
        sy = min(src_h - 1, int(round(dy * (src_h - 1) / max(1, new_h - 1))))
        for dx in range(new_w):
            sx = min(src_w - 1, int(round(dx * (src_w - 1) / max(1, new_w - 1))))
            out[dy][dx] = grid[sy][sx]
    return out


def _shift_y(grid: list[list[int]], dy: int) -> list[list[int]]:
    h = len(grid)
    w = len(grid[0]) if h else 0
    out = [[0] * w for _ in range(h)]
    for y in range(h):
        ny = y + dy
        if 0 <= ny < h:
            out[ny] = list(grid[y])
    return out


def _blit_max(dst: list[list[int]], src: list[list[int]], dst_x: int, dst_y: int):
    dh = len(dst)
    dw = len(dst[0]) if dh else 0
    for sy, row in enumerate(src):
        ty = dst_y + sy
        if 0 <= ty < dh:
            for sx, val in enumerate(row):
                tx = dst_x + sx
                if 0 <= tx < dw and val > dst[ty][tx]:
                    dst[ty][tx] = val


def patch_ffu_bytes(ffu_data: bytes) -> bytes:
    """
    Injeta os glifos acentuados do Português (á..ç, Á..Ç) e ordinais (º, ª)
    dentro de um arquivo de fonte .ffu (assinatura UF) da Otomate.
    """
    if not ffu_data.startswith(b"UF") or len(ffu_data) < 0x68:
        return ffu_data

    num_ranges, num_glyphs = struct.unpack_from("<HI", ffu_data, 2)
    t1_off, t2_off, px_off = struct.unpack_from("<III", ffu_data, 0x14)

    code_to_glyph: dict[int, int] = {}
    for i in range(num_ranges):
        start_c, end_c, g_idx = struct.unpack_from("<III", ffu_data, t1_off + i * 12)
        for offset, code in enumerate(range(start_c, end_c)):
            code_to_glyph[code] = g_idx + offset

    glyph_records: list[list[int]] = [
        list(struct.unpack_from("<BBHI", ffu_data, t2_off + i * 8))
        for i in range(num_glyphs)
    ]
    px_blob = bytearray(ffu_data[px_off:])

    def get_char_grid(ch: str) -> tuple[list[list[int]], int, int] | None:
        g_idx = code_to_glyph.get(_utf8_key(ch))
        if g_idx is None or g_idx >= len(glyph_records):
            return None
        w, h, sz, off = glyph_records[g_idx]
        raw = bytes(px_blob[off : off + sz])
        return _decode_4bpp(raw, w, h), w, h

    ref_A = get_char_grid("A")
    ref_a = get_char_grid("a")
    if not ref_A or not ref_a:
        return ffu_data

    bbox_A = _ink_bbox(ref_A[0]) or (0, 6, ref_A[1] - 1, ref_A[2] - 4)
    bbox_a = _ink_bbox(ref_a[0]) or (0, 12, ref_a[1] - 1, ref_a[2] - 4)
    cap_top = bbox_A[1]
    cap_h = max(10, bbox_A[3] - bbox_A[1])
    x_top = bbox_a[1]

    diacritics: dict[str, list[list[int]]] = {}

    def extract_diacritic(ch: str, max_h_ratio: float = 0.26) -> list[list[int]] | None:
        res = get_char_grid(ch)
        if not res:
            return None
        bb = _ink_bbox(res[0], threshold=3)
        if not bb:
            return None
        cropped = _crop(res[0], bb)
        max_h = max(4, int(round(cap_h * max_h_ratio)))
        if len(cropped) > max_h:
            new_w = max(3, int(round(len(cropped[0]) * (max_h / len(cropped)))))
            cropped = _scale_grid(cropped, new_w, max_h)
        return cropped

    acute = extract_diacritic("´", 0.25)
    if acute:
        diacritics["acute"] = acute
        diacritics["grave"] = _flip_h(acute)
    else:
        grave = extract_diacritic("`", 0.25)
        if grave:
            diacritics["grave"] = grave
            diacritics["acute"] = _flip_h(grave)

    for key, src_ch, ratio in [
        ("circumflex", "^", 0.24),
        ("tilde", "~", 0.21),
        ("diaeresis", "¨", 0.20),
        ("cedilla", ",", 0.26),
    ]:
        d_grid = extract_diacritic(src_ch, ratio)
        if d_grid:
            diacritics[key] = d_grid

    for target_ch, base_ch, mark_type in PT_GLYPH_SPECS:
        t_code = _utf8_key(target_ch)
        if t_code in code_to_glyph:
            continue

        base_res = get_char_grid(base_ch)
        if not base_res:
            continue
        base_grid, w, h = base_res
        canvas = [list(row) for row in base_grid]

        if base_ch == "i" and mark_type != "ordinal":
            cut_y = max(0, x_top - 2)
            for y in range(cut_y):
                canvas[y] = [0] * w

        b_box = _ink_bbox(canvas) or (2, x_top, w - 3, bbox_a[3])
        bx0, by0, bx1, by1 = b_box
        center_x = (bx0 + bx1 + 1) // 2

        if mark_type == "ordinal":
            letter_crop = _crop(canvas, b_box)
            target_h = max(8, int(round(cap_h * 0.44)))
            target_w = max(7, int(round(len(letter_crop[0]) * (target_h / max(1, len(letter_crop))))))
            mini = _scale_grid(letter_crop, target_w, target_h)

            ord_w = max(target_w + 4, int(round(w * 0.72)))
            canvas = [[0] * ord_w for _ in range(h)]
            ox = max(1, (ord_w - target_w) // 2)
            oy = max(1, cap_top)
            _blit_max(canvas, mini, ox, oy)

            bar_y = min(h - 2, oy + target_h + max(1, h // 36))
            for bx in range(ox, min(ord_w - 1, ox + target_w)):
                canvas[bar_y][bx] = 14
                if bar_y + 1 < h:
                    canvas[bar_y + 1][bx] = 5
            w = ord_w

        elif mark_type == "cedilla":
            ced = diacritics.get("cedilla")
            if ced:
                ch_h = len(ced)
                ch_w = len(ced[0])
                overflow = (by1 - 1 + ch_h) - (h - 1)
                if overflow > 0:
                    canvas = _shift_y(canvas, -min(overflow, max(1, by0 - 1)))
                    b_box = _ink_bbox(canvas) or b_box
                    bx0, by0, bx1, by1 = b_box
                dx = max(0, min(w - ch_w, center_x - (ch_w // 2)))
                dy = min(h - ch_h, by1 - 1)
                _blit_max(canvas, ced, dx, dy)

        else:
            mark = diacritics.get(mark_type)
            if mark:
                mh = len(mark)
                mw = len(mark[0])
                gap = max(1, h // 32)
                needed_top = mh + gap
                if by0 < needed_top + 1:
                    max_down = max(0, (h - 2) - by1)
                    shift_down = min(max_down, (needed_top + 1) - by0)
                    if shift_down > 0:
                        canvas = _shift_y(canvas, shift_down)
                        b_box = _ink_bbox(canvas) or b_box
                        bx0, by0, bx1, by1 = b_box

                dx = max(0, min(w - mw, center_x - (mw // 2)))
                dy = max(1, by0 - gap - mh)
                _blit_max(canvas, mark, dx, dy)

        encoded_px = _encode_4bpp(canvas)
        new_px_offset = len(px_blob)
        px_blob.extend(encoded_px)

        new_glyph_idx = len(glyph_records)
        glyph_records.append([w, h, len(encoded_px), new_px_offset])
        code_to_glyph[t_code] = new_glyph_idx

    sorted_items = sorted(code_to_glyph.items())
    merged_ranges: list[tuple[int, int, int]] = []
    for code, g_idx in sorted_items:
        if (
            merged_ranges
            and code == merged_ranges[-1][1]
            and g_idx == merged_ranges[-1][2] + (merged_ranges[-1][1] - merged_ranges[-1][0])
        ):
            s_c, _, first_g = merged_ranges[-1]
            merged_ranges[-1] = (s_c, code + 1, first_g)
        else:
            merged_ranges.append((code, code + 1, g_idx))

    new_t1 = bytearray()
    for start_c, end_c, g_idx in merged_ranges:
        new_t1 += struct.pack("<III", start_c, end_c, g_idx)

    new_t2 = bytearray()
    for w, h, sz, off in glyph_records:
        new_t2 += struct.pack("<BBHI", w, h, sz, off)

    new_t1_off = 0x68
    new_t2_off = new_t1_off + len(new_t1)
    new_px_off = new_t2_off + len(new_t2)

    new_hdr = bytearray(ffu_data[:0x68])
    struct.pack_into("<H", new_hdr, 0x02, len(merged_ranges))
    struct.pack_into("<I", new_hdr, 0x04, len(glyph_records))
    struct.pack_into("<I", new_hdr, 0x14, new_t1_off)
    struct.pack_into("<I", new_hdr, 0x18, new_t2_off)
    struct.pack_into("<I", new_hdr, 0x1C, new_px_off)

    return bytes(new_hdr) + bytes(new_t1) + bytes(new_t2) + bytes(px_blob)


def auto_patch_romfs_fonts(romfs_dir: str, patch_romfs_dir: str, log_fn=print) -> int:
    """
    Varre os arquivos .CPK do RomFS em busca de fontes UF (.ffu), injeta os glifos
    acentuados do PT-BR (á..ç, Á..Ç, º, ª) e gera o CPK atualizado no patch LayeredFS.
    Retorna a quantidade total de fontes atualizadas.
    """
    romfs_path = Path(romfs_dir)
    out_romfs = Path(patch_romfs_dir)
    out_romfs.mkdir(parents=True, exist_ok=True)
    patched_total = 0

    for cpk_path in sorted(romfs_path.rglob("*")):
        if not cpk_path.is_file() or cpk_path.suffix.lower() != ".cpk":
            continue

        modified_fonts: dict[str, bytes] = {}
        with open(cpk_path, "rb") as f:
            if f.read(4) != b"CPK ":
                continue
            f.seek(8)
            hdr_sz = struct.unpack("<Q", f.read(8))[0]
            info = _parse_utf_table(f.read(hdr_sz))[0]
            toc_off, content_off = info["TocOffset"], info["ContentOffset"]
            base = toc_off if (not content_off or toc_off < content_off) else content_off

            f.seek(toc_off + 8)
            toc_sz = struct.unpack("<Q", f.read(8))[0]
            entries = _parse_utf_table(f.read(toc_sz))

            for e in entries:
                fn = e.get("FileName", "")
                if fn.lower().endswith(".ffu"):
                    f.seek(base + e["FileOffset"])
                    raw_ffu = _decompress_crilayla(f.read(e["FileSize"]))
                    if raw_ffu.startswith(b"UF"):
                        rel_key = f"{e.get('DirName', '')}/{fn}".lstrip("/")
                        modified_fonts[rel_key] = patch_ffu_bytes(raw_ffu)

        if modified_fonts:
            target_cpk = out_romfs / cpk_path.name
            base_cpk = target_cpk if target_cpk.exists() else cpk_path
            repack_cpk(str(base_cpk), modified_fonts, str(target_cpk))
            patched_total += len(modified_fonts)
            log_fn(f"[Fonte] {len(modified_fonts)} fonte(s) .ffu acentuada(s) em {cpk_path.name}")

    return patched_total