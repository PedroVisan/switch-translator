from bisect import bisect_right
import re
import struct

ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_./\\-]+$")
LOWER_KEYWORD_RE = re.compile(r"^[a-z]+$")
JP_CHAR_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uff00-\uffef]")

EXTRA_CHAR_MAP = str.maketrans(
    {
        "—": "-",
        "–": "-",
        "…": "...",
        "«": '"',
        "»": '"',
    }
)


def normalize_for_otomate_font(text: str) -> str:
    """Mantém todos os acentos e º/ª do português, trocando apenas travessões/reticências."""
    return text.translate(EXTRA_CHAR_MAP)


def _is_translatable_text(text: str) -> bool:
    """Mantém apenas diálogos, escolhas e avisos reais, protegendo IDs e tags."""
    cleaned = text.strip()
    if len(cleaned) < 2:
        return False
    if "\ufffd" in cleaned or "\x00" in cleaned:
        return False
    if any(ord(ch) < 0x20 and ch not in ("\n", "\r", "\t") for ch in cleaned):
        return False
    if not any(ch.isalpha() for ch in cleaned):
        return False
    if cleaned.startswith("#Name["):
        return False
    if JP_CHAR_RE.search(cleaned):
        return False
    if LOWER_KEYWORD_RE.match(cleaned):
        return False
    if ASSET_ID_RE.match(cleaned) and (
        "_" in cleaned or "/" in cleaned or "\\" in cleaned or any(c.isdigit() for c in cleaned)
    ):
        return False
    return True


def _parse_inline_string_block(data: bytes, cursor: int, inline_end: int) -> tuple[bool, int, str]:
    """
    Valida o bloco inline STCM2L: [word_count: u32, unit=1: u32, byte_len: u32, utf8 + zero_pad].
    """
    if cursor + 12 > inline_end:
        return False, 0, ""

    word_count, str_unit, str_byte_len = struct.unpack_from("<III", data, cursor)
    if (
        str_unit != 1
        or str_byte_len <= 4
        or str_byte_len % 4 != 0
        or word_count != (str_byte_len // 4)
        or cursor + 12 + str_byte_len > inline_end
    ):
        return False, 0, ""

    raw_slice = data[cursor + 12 : cursor + 12 + str_byte_len]
    if b"\x00" not in raw_slice:
        return False, 0, ""

    raw_text_bytes, padding = raw_slice.split(b"\x00", 1)
    if not raw_text_bytes or any(b != 0 for b in padding):
        return False, 0, ""

    try:
        decoded = raw_text_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False, 0, ""

    return True, str_byte_len, decoded


def _get_code_bounds(data: bytes) -> tuple[int, int, int, int]:
    """Retorna (code_start, export_offset, export_count, collection_link_offset)."""
    if not data.startswith(b"STCM2L") or len(data) < 0x50:
        return 0, 0, 0, 0

    export_offset, export_count, _, collection_link_offset = struct.unpack_from("<IIII", data, 0x20)
    if (
        export_offset <= 0x50
        or collection_link_offset <= export_offset
        or collection_link_offset > len(data)
    ):
        return 0, 0, 0, 0

    targets = []
    for i in range(export_count):
        eoff = export_offset + (i * 40)
        if eoff + 40 <= collection_link_offset:
            t = struct.unpack_from("<I", data, eoff + 36)[0]
            if 0x50 <= t < export_offset:
                targets.append(t)

    if not targets:
        return 0, 0, 0, 0

    return min(targets), export_offset, export_count, collection_link_offset


def parse_stcm2l_strings(data: bytes) -> list[dict]:
    """Extrai todas as strings traduzíveis com seus offsets exatos."""
    code_start, export_offset, _, _ = _get_code_bounds(data)
    if not code_start:
        return []

    pos = code_start
    entries = []

    while pos + 16 <= export_offset:
        is_export, opcode, param_count, inst_size = struct.unpack_from("<IIII", data, pos)
        if inst_size < 16 + (param_count * 12) or pos + inst_size > export_offset:
            return []

        inline_start = pos + 16 + (param_count * 12)
        inline_end = pos + inst_size
        cursor = inline_start

        while cursor + 12 <= inline_end:
            is_str, str_byte_len, decoded = _parse_inline_string_block(data, cursor, inline_end)
            if is_str:
                if _is_translatable_text(decoded):
                    entries.append(
                        {
                            "inst_offset": pos,
                            "str_offset": cursor,
                            "old_byte_len": str_byte_len,
                            "text": decoded,
                        }
                    )
                cursor += 12 + str_byte_len
            else:
                cursor += 4

        pos += inst_size

    return entries


def rebuild_stcm2l(
    data: bytes,
    translations_by_str_offset: dict[int, str],
    strip_accents: bool = True,
) -> bytes:
    """
    Reconstrói o arquivo STCM2L (.DAT) atualizando word_count, byte_len,
    ponteiros em p[0] e p[1], EXPORT_DATA e rodapé.
    """
    code_start, export_offset, export_count, collection_link_offset = _get_code_bounds(data)
    if not code_start:
        return data

    instructions = []
    pos = code_start
    while pos + 16 <= export_offset:
        is_export, opcode, param_count, inst_size = struct.unpack_from("<IIII", data, pos)
        if inst_size < 16 + (param_count * 12) or pos + inst_size > export_offset:
            return data
        instructions.append(
            {
                "old_offset": pos,
                "is_export": is_export,
                "opcode": opcode,
                "param_count": param_count,
                "old_size": inst_size,
            }
        )
        pos += inst_size

    tail_before_export = data[pos:export_offset]

    old_to_new_inst = {}
    inline_offset_map = {}
    new_cursor = code_start

    for inst in instructions:
        old_pos = inst["old_offset"]
        old_to_new_inst[old_pos] = new_cursor
        param_count = inst["param_count"]
        old_size = inst["old_size"]

        params_start = old_pos + 16
        inline_start = params_start + (param_count * 12)
        inline_end = old_pos + old_size

        params = [
            list(struct.unpack_from("<III", data, params_start + i * 12))
            for i in range(param_count)
        ]

        new_inline = bytearray()
        cursor = inline_start

        while cursor + 12 <= inline_end:
            inline_offset_map[cursor] = new_cursor + 16 + (param_count * 12) + len(new_inline)
            is_str, str_byte_len, _ = _parse_inline_string_block(data, cursor, inline_end)

            if is_str:
                _, str_unit, _ = struct.unpack_from("<III", data, cursor)
                if cursor in translations_by_str_offset:
                    new_text = translations_by_str_offset[cursor]
                    if strip_accents:
                        new_text = normalize_for_otomate_font(new_text)

                    encoded = new_text.encode("utf-8") + b"\x00"
                    pad_len = (4 - (len(encoded) % 4)) % 4
                    padded_bytes = encoded + (b"\x00" * pad_len)
                    new_byte_len = len(padded_bytes)
                    new_word_count = new_byte_len // 4

                    new_inline += struct.pack("<III", new_word_count, str_unit, new_byte_len)
                    new_inline += padded_bytes
                else:
                    new_inline += data[cursor : cursor + 12 + str_byte_len]

                cursor += 12 + str_byte_len
            else:
                new_inline += data[cursor : cursor + 4]
                cursor += 4

        while cursor < inline_end:
            inline_offset_map[cursor] = new_cursor + 16 + (param_count * 12) + len(new_inline)
            new_inline.append(data[cursor])
            cursor += 1

        new_inst_size = 16 + (param_count * 12) + len(new_inline)
        inst["new_offset"] = new_cursor
        inst["new_size"] = new_inst_size
        inst["params"] = params
        inst["new_inline"] = bytes(new_inline)

        new_cursor += new_inst_size

    new_export_offset = new_cursor + len(tail_before_export)
    total_delta = new_export_offset - export_offset
    old_to_new_inst[export_offset] = new_export_offset
    sorted_old_offsets = [inst["old_offset"] for inst in instructions]

    def remap_pointer(addr: int) -> int:
        if addr in old_to_new_inst:
            return old_to_new_inst[addr]
        if addr in inline_offset_map:
            return inline_offset_map[addr]
        if pos <= addr <= export_offset:
            return addr + total_delta
        if code_start <= addr < pos:
            idx = bisect_right(sorted_old_offsets, addr) - 1
            if idx >= 0:
                inst_ref = instructions[idx]
                rel = addr - inst_ref["old_offset"]
                if rel < 16 + (inst_ref["param_count"] * 12):
                    return inst_ref["new_offset"] + rel
        return addr

    rebuilt_inst_blobs = []
    for inst in instructions:
        header_bytes = struct.pack(
            "<IIII",
            inst["is_export"],
            inst["opcode"],
            inst["param_count"],
            inst["new_size"],
        )
        param_bytes = bytearray()
        for p in inst["params"]:
            p[0] = remap_pointer(p[0])
            p[1] = remap_pointer(p[1])
            param_bytes += struct.pack("<III", *p)

        rebuilt_inst_blobs.append(header_bytes + bytes(param_bytes) + inst["new_inline"])

    export_section = bytearray(data[export_offset:collection_link_offset])
    for i in range(export_count):
        entry_off = i * 40
        if entry_off + 40 <= len(export_section):
            target_off = struct.unpack_from("<I", export_section, entry_off + 36)[0]
            struct.pack_into("<I", export_section, entry_off + 36, remap_pointer(target_off))

    footer_section = bytearray(data[collection_link_offset:])
    new_collection_link_offset = collection_link_offset + total_delta

    if len(footer_section) >= 8:
        old_eof = struct.unpack_from("<I", footer_section, 4)[0]
        if old_eof == len(data):
            struct.pack_into("<I", footer_section, 4, len(data) + total_delta)

    new_header = bytearray(data[:0x50])
    struct.pack_into("<I", new_header, 0x20, new_export_offset)
    struct.pack_into("<I", new_header, 0x2C, new_collection_link_offset)

    return (
        bytes(new_header)
        + data[0x50:code_start]
        + b"".join(rebuilt_inst_blobs)
        + tail_before_export
        + bytes(export_section)
        + bytes(footer_section)
    )