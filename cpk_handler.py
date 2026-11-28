import os
from pathlib import Path
import struct

SKIP_MEDIA_EXTS = {
    ".bntx", ".png", ".jpg", ".jpeg", ".dds", ".tga", ".bmp", ".gxt", ".txp",
    ".hca", ".adx", ".at9", ".wav", ".ogg", ".mp3", ".usm", ".mp4", ".webm", ".bik",
    ".tid", ".acb", ".acf", ".ffu",
}


def _toggle_utf_crypt(data: bytearray) -> bytearray:
    """Aplica ou remove a ofuscação XOR padrão das tabelas @UTF da CRI Middleware."""
    m = 0x0000655F
    t = 0x00004115
    for i in range(len(data)):
        data[i] ^= m & 0xFF
        m = (m * t) & 0xFFFFFFFF
    return data


def _parse_utf_table(raw_utf: bytes, record_offsets: bool = False):
    """Lê uma tabela binária @UTF e opcionalmente registra os offsets de cada campo."""
    is_encrypted = raw_utf[:4] != b"@UTF"
    buf_full = _toggle_utf_crypt(bytearray(raw_utf)) if is_encrypted else bytearray(raw_utf)
    if buf_full[:4] != b"@UTF":
        raise ValueError("Cabeçalho @UTF inválido ou corrompido no arquivo CPK.")

    table_size = struct.unpack(">I", buf_full[4:8])[0]
    buf = buf_full[8 : 8 + table_size]

    rows_offset, strings_offset, data_offset, _ = struct.unpack(">IIII", buf[0:16])
    num_columns, row_length, num_rows = struct.unpack(">HHI", buf[16:24])

    def read_str(offset: int) -> str:
        end = buf.find(b"\x00", strings_offset + offset)
        return buf[strings_offset + offset : end].decode("utf-8", errors="replace")

    type_formats = {
        0: (">B", 1), 1: (">b", 1), 2: (">H", 2), 3: (">h", 2),
        4: (">I", 4), 5: (">i", 4), 6: (">Q", 8), 7: (">q", 8),
        8: (">f", 4), 9: (">d", 8),
    }

    def read_typed_value(col_type: int, src: bytearray, pos: int):
        if col_type in type_formats:
            fmt, size = type_formats[col_type]
            return struct.unpack(fmt, src[pos : pos + size])[0], size
        if col_type == 10:
            str_off = struct.unpack(">I", src[pos : pos + 4])[0]
            return read_str(str_off), 4
        if col_type == 11:
            blob_off, blob_size = struct.unpack(">II", src[pos : pos + 8])
            start = data_offset + blob_off
            return bytes(src[start : start + blob_size]), 8
        return None, 0

    schema = []
    cursor = 24
    for _ in range(num_columns):
        flags = buf[cursor]
        cursor += 1
        col_type = flags & 0x0F
        storage = flags & 0xF0

        col_name = ""
        if storage & 0x10:
            name_off = struct.unpack(">I", buf[cursor : cursor + 4])[0]
            col_name = read_str(name_off)
            cursor += 4

        const_val = None
        if storage & 0x20:
            const_val, consumed = read_typed_value(col_type, buf, cursor)
            cursor += consumed

        schema.append((col_name, col_type, storage, const_val))

    rows = []
    field_positions = []
    for r in range(num_rows):
        row_dict = {}
        pos_map = {}
        row_cursor = rows_offset + (r * row_length)
        for col_name, col_type, storage, const_val in schema:
            if storage & 0x40:
                val, consumed = read_typed_value(col_type, buf, row_cursor)
                pos_map[col_name] = (8 + row_cursor, col_type)
                row_cursor += consumed
                row_dict[col_name] = val
            elif storage & 0x20:
                row_dict[col_name] = const_val
            else:
                row_dict[col_name] = None
        rows.append(row_dict)
        field_positions.append(pos_map)

    if record_offsets:
        return rows, field_positions, buf_full, is_encrypted
    return rows


def _decompress_crilayla(data: bytes) -> bytes:
    """Descomprime arquivos internos marcados com assinatura CRILAYLA."""
    if data[:8] != b"CRILAYLA":
        return data

    uncomp_size, comp_size = struct.unpack("<II", data[8:16])
    raw_prefix = data[16 + comp_size : 16 + comp_size + 0x100]

    out = bytearray(0x100 + uncomp_size)
    out[0:0x100] = raw_prefix

    src_pos = 16 + comp_size - 1
    dst_pos = 0x100 + uncomp_size - 1
    bit_pool = 0
    bits_left = 0

    def get_bits(n: int) -> int:
        nonlocal src_pos, bit_pool, bits_left
        while bits_left < n:
            bit_pool = (bit_pool << 8) | data[src_pos]
            src_pos -= 1
            bits_left += 8
        bits_left -= n
        val = (bit_pool >> bits_left) & ((1 << n) - 1)
        bit_pool &= (1 << bits_left) - 1
        return val

    vle_steps = (2, 3, 5, 8)
    while dst_pos >= 0x100:
        if get_bits(1) > 0:
            offset = get_bits(13) + 3
            length = 3
            for bits in vle_steps:
                chunk = get_bits(bits)
                length += chunk
                if chunk != (1 << bits) - 1:
                    break
            else:
                while True:
                    chunk = get_bits(8)
                    length += chunk
                    if chunk != 255:
                        break

            for _ in range(length):
                if dst_pos >= 0x100:
                    out[dst_pos] = out[dst_pos + offset]
                    dst_pos -= 1
        else:
            out[dst_pos] = get_bits(8)
            dst_pos -= 1

    return bytes(out)


def unpack_cpk(cpk_path: str, output_dir: str) -> int:
    """Extrai apenas arquivos de texto/dados de um .cpk para output_dir."""
    os.makedirs(output_dir, exist_ok=True)
    with open(cpk_path, "rb") as f:
        if f.read(4) != b"CPK ":
            raise ValueError(f"{cpk_path} não é um arquivo CPK válido.")
        f.read(4)
        utf_size = struct.unpack("<Q", f.read(8))[0]
        header_rows = _parse_utf_table(f.read(utf_size))
        if not header_rows:
            return 0

        info = header_rows[0]
        toc_offset = info.get("TocOffset") or 0
        content_offset = info.get("ContentOffset") or 0
        if not toc_offset:
            return 0

        f.seek(toc_offset)
        if f.read(4) != b"TOC ":
            raise ValueError("Tabela TOC não encontrada dentro do CPK.")
        f.read(4)
        toc_utf_size = struct.unpack("<Q", f.read(8))[0]
        entries = _parse_utf_table(f.read(toc_utf_size))

        target_entries = [
            e for e in entries
            if e.get("FileName")
            and Path(e["FileName"]).suffix.lower() not in SKIP_MEDIA_EXTS
        ]

        base_offset = (
            toc_offset
            if (not content_offset or toc_offset < content_offset)
            else content_offset
        )

        extracted = 0
        for entry in target_entries:
            file_name = entry["FileName"]
            dir_name = (entry.get("DirName") or "").strip("/\\")
            rel_offset = entry.get("FileOffset") or 0
            comp_size = entry.get("FileSize") or 0

            f.seek(base_offset + rel_offset)
            raw_bytes = f.read(comp_size)
            final_bytes = _decompress_crilayla(raw_bytes)

            dest_path = (
                Path(output_dir) / dir_name / file_name
                if dir_name
                else Path(output_dir) / file_name
            )
            os.makedirs(dest_path.parent, exist_ok=True)
            with open(dest_path, "wb") as out_f:
                out_f.write(final_bytes)
            extracted += 1

        return extracted


def _write_utf_int(buf: bytearray, pos_info: tuple[int, int], value: int):
    pos, col_type = pos_info
    if col_type == 4:
        struct.pack_into(">I", buf, pos, value)
    elif col_type == 6:
        struct.pack_into(">Q", buf, pos, value)


def repack_cpk(orig_cpk_path: str, modified_files: dict[str, bytes], out_cpk_path: str):
    """
    Cria um novo .CPK substituindo os arquivos em modified_files e atualizando
    TOC, ContentSize, EnabledPackedSize, EnabledDataSize e ETOC.
    """
    os.makedirs(Path(out_cpk_path).parent, exist_ok=True)

    with open(orig_cpk_path, "rb") as f:
        if f.read(4) != b"CPK ":
            raise ValueError(f"{orig_cpk_path} não é um arquivo CPK válido.")
        cpk_flags = f.read(4)
        utf_size = struct.unpack("<Q", f.read(8))[0]
        header_rows, header_pos, header_utf_buf, header_enc = _parse_utf_table(
            f.read(utf_size), record_offsets=True
        )
        info = header_rows[0]
        toc_offset = info.get("TocOffset") or 0
        content_offset = info.get("ContentOffset") or 0
        etoc_offset = info.get("EtocOffset") or 0
        etoc_size = info.get("EtocSize") or 0
        align = info.get("Align") or 0x10

        f.seek(toc_offset)
        if f.read(4) != b"TOC ":
            raise ValueError("Tabela TOC não encontrada dentro do CPK.")
        toc_flags = f.read(4)
        toc_utf_size = struct.unpack("<Q", f.read(8))[0]
        entries, toc_pos_list, toc_utf_buf, toc_enc = _parse_utf_table(
            f.read(toc_utf_size), record_offsets=True
        )

        # Lê o bloco ETOC original se existir
        etoc_blob = b""
        if etoc_offset and etoc_size:
            f.seek(etoc_offset)
            etoc_blob = f.read(16 + etoc_size)

        toc_before_content = toc_offset < content_offset
        old_base_offset = toc_offset if toc_before_content else content_offset

        indexed_entries = sorted(
            enumerate(entries), key=lambda item: item[1].get("FileOffset") or 0
        )

        first_data_abs = (
            content_offset
            if toc_before_content
            else min(
                content_offset,
                old_base_offset + (indexed_entries[0][1].get("FileOffset") or 0),
            )
        )
        f.seek(0)
        prefix_bytes = f.read(first_data_abs)

        total_packed_size = 0
        total_extract_size = 0

        with open(out_cpk_path, "wb") as out_f:
            out_f.write(prefix_bytes)

            for idx, entry in indexed_entries:
                file_name = entry.get("FileName") or ""
                dir_name = (entry.get("DirName") or "").strip("/\\")
                rel_key = f"{dir_name}/{file_name}" if dir_name else file_name
                rel_key_norm = rel_key.replace("\\", "/")

                cur_abs = out_f.tell()
                pad = (align - (cur_abs % align)) % align
                if pad:
                    out_f.write(b"\x00" * pad)
                file_abs_start = out_f.tell()

                pos_map = toc_pos_list[idx]
                if rel_key_norm in modified_files:
                    payload = modified_files[rel_key_norm]
                    out_f.write(payload)
                    fsize = len(payload)
                    esize = len(payload)
                    if "FileSize" in pos_map:
                        _write_utf_int(toc_utf_buf, pos_map["FileSize"], fsize)
                    if "ExtractSize" in pos_map:
                        _write_utf_int(toc_utf_buf, pos_map["ExtractSize"], esize)
                else:
                    old_rel = entry.get("FileOffset") or 0
                    fsize = entry.get("FileSize") or 0
                    esize = entry.get("ExtractSize") or fsize
                    f.seek(old_base_offset + old_rel)
                    out_f.write(f.read(fsize))

                total_packed_size += fsize
                total_extract_size += esize

                new_rel_offset = file_abs_start - old_base_offset
                if "FileOffset" in pos_map:
                    _write_utf_int(toc_utf_buf, pos_map["FileOffset"], new_rel_offset)

            # Alinha o fim da área de conteúdo para gravar ETOC / TOC
            cur_abs = out_f.tell()
            pad = (align - (cur_abs % align)) % align
            if pad:
                out_f.write(b"\x00" * pad)
            end_of_content = out_f.tell()
            new_content_size = end_of_content - content_offset

            final_toc_bytes = bytes(_toggle_utf_crypt(toc_utf_buf) if toc_enc else toc_utf_buf)

            if toc_before_content:
                out_f.seek(end_of_content)
                new_etoc_offset = 0
                if etoc_blob:
                    new_etoc_offset = out_f.tell()
                    out_f.write(etoc_blob)

                out_f.seek(toc_offset)
                out_f.write(b"TOC " + toc_flags + struct.pack("<Q", len(final_toc_bytes)))
                out_f.write(final_toc_bytes)
            else:
                new_toc_offset = out_f.tell()
                out_f.write(b"TOC " + toc_flags + struct.pack("<Q", len(final_toc_bytes)))
                out_f.write(final_toc_bytes)

                cur_abs = out_f.tell()
                pad = (align - (cur_abs % align)) % align
                if pad:
                    out_f.write(b"\x00" * pad)

                new_etoc_offset = 0
                if etoc_blob:
                    new_etoc_offset = out_f.tell()
                    out_f.write(etoc_blob)

                if "TocOffset" in header_pos[0]:
                    _write_utf_int(header_utf_buf, header_pos[0]["TocOffset"], new_toc_offset)

            hdr_map = header_pos[0]
            if "ContentSize" in hdr_map:
                _write_utf_int(header_utf_buf, hdr_map["ContentSize"], new_content_size)
            if "EtocOffset" in hdr_map and new_etoc_offset:
                _write_utf_int(header_utf_buf, hdr_map["EtocOffset"], new_etoc_offset)
            if "EnabledPackedSize" in hdr_map:
                _write_utf_int(header_utf_buf, hdr_map["EnabledPackedSize"], total_packed_size)
            if "EnabledDataSize" in hdr_map:
                _write_utf_int(header_utf_buf, hdr_map["EnabledDataSize"], total_extract_size)

            final_hdr_bytes = bytes(
                _toggle_utf_crypt(header_utf_buf) if header_enc else header_utf_buf
            )
            out_f.seek(0)
            out_f.write(b"CPK " + cpk_flags + struct.pack("<Q", len(final_hdr_bytes)))
            out_f.write(final_hdr_bytes)
            
def cpk_has_extensions(cpk_path: str, exts: tuple[str, ...]) -> bool:
    """Lê apenas o índice TOC do CPK (em milissegundos) e verifica se contém alguma extensão desejada."""
    try:
        with open(cpk_path, "rb") as f:
            if f.read(4) != b"CPK ":
                return False
            f.seek(8)
            info_size = struct.unpack("<Q", f.read(8))[0]
            info = _parse_utf_table(f.read(info_size))[0]
            toc_off = info.get("TocOffset", 0)
            if not toc_off:
                return False
            f.seek(toc_off)
            if f.read(4) != b"TOC ":
                return False
            f.seek(toc_off + 8)
            toc_size = struct.unpack("<Q", f.read(8))[0]
            entries = _parse_utf_table(f.read(toc_size))
            return any(
                (e.get("FileName", "") or "").lower().endswith(exts)
                for e in entries
            )
    except Exception:
        return False