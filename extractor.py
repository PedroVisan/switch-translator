from collections import defaultdict
import json
from pathlib import Path

from cpk_handler import repack_cpk, unpack_cpk
from font_handler import auto_patch_romfs_fonts
from stcm2l_handler import parse_stcm2l_strings, rebuild_stcm2l

TEXT_EXTENSIONS = {".txt", ".json", ".csv", ".xml", ".yaml", ".yml", ".tsv", ".ini"}


def _detect_engine(romfs_dir: str) -> str:
    romfs_path = Path(romfs_dir)
    for f in romfs_path.rglob("*"):
        if f.is_file() and f.suffix.lower() == ".cpk":
            return "cri_otomate"
    return "plain_text"


def _extract_cri_otomate(romfs_path: Path, work_path: Path) -> tuple[dict[str, str], dict[str, dict]]:
    cpk_extract_dir = work_path / "cpk_extraido"
    cpk_extract_dir.mkdir(parents=True, exist_ok=True)

    strings_map: dict[str, str] = {}
    manifest: dict[str, dict] = {}
    counter = 1

    for cpk_file in sorted(romfs_path.rglob("*")):
        if not cpk_file.is_file() or cpk_file.suffix.lower() != ".cpk":
            continue

        cpk_rel_path = cpk_file.relative_to(romfs_path).as_posix()
        unpacked_dir = cpk_extract_dir / cpk_file.stem
        unpack_cpk(str(cpk_file), str(unpacked_dir))

        for dat_file in sorted(unpacked_dir.rglob("*")):
            if not dat_file.is_file() or dat_file.suffix.lower() != ".dat":
                continue

            raw = dat_file.read_bytes()
            if not raw.startswith(b"STCM2L"):
                continue

            entries = parse_stcm2l_strings(raw)
            if not entries:
                continue

            internal_rel_path = dat_file.relative_to(unpacked_dir).as_posix()
            for ent in entries:
                sid = f"str_{counter:05d}"
                counter += 1
                strings_map[sid] = ent["text"]
                manifest[sid] = {
                    "type": "cri_cpk_stcm2l",
                    "cpk_rel_path": cpk_rel_path,
                    "unpacked_cpk_dir": str(unpacked_dir),
                    "internal_rel_path": internal_rel_path,
                    "str_offset": ent["str_offset"],
                }

    return strings_map, manifest


def _extract_plain_text(romfs_path: Path) -> tuple[dict[str, str], dict[str, dict]]:
    strings_map: dict[str, str] = {}
    manifest: dict[str, dict] = {}
    counter = 1

    for f in sorted(romfs_path.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        rel_path = f.relative_to(romfs_path).as_posix()
        for idx, line in enumerate(content.splitlines()):
            stripped = line.strip()
            if len(stripped) >= 2 and any(c.isalpha() for c in stripped):
                sid = f"str_{counter:05d}"
                counter += 1
                strings_map[sid] = stripped
                manifest[sid] = {
                    "type": "plain_text",
                    "rel_path": rel_path,
                    "line_idx": idx,
                    "original": stripped,
                }

    return strings_map, manifest


def extract_texts(romfs_dir: str, work_dir: str) -> dict:
    """
    Detecta a engine do jogo e extrai todas as strings traduzíveis para JSON,
    gerando também o manifest.json necessário para reconstruir o LayeredFS.
    """
    romfs_path = Path(romfs_dir)
    work_path = Path(work_dir)
    work_path.mkdir(parents=True, exist_ok=True)

    engine = _detect_engine(romfs_dir)
    if engine == "cri_otomate":
        strings_map, manifest = _extract_cri_otomate(romfs_path, work_path)
    else:
        strings_map, manifest = _extract_plain_text(romfs_path)

    strings_path = work_path / "strings_para_traduzir.json"
    manifest_path = work_path / "manifest.json"

    strings_path.write_text(
        json.dumps(strings_map, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "engine": engine,
        "total_strings": len(strings_map),
        "strings_path": str(strings_path),
        "manifest_path": str(manifest_path),
    }


def build_layeredfs_patch(
    title_id: str,
    romfs_dir: str,
    translated_json_path: str,
    manifest_path: str,
    log_fn=print,
) -> str:
    """
    Reconstrói os arquivos traduzidos a partir do manifest.json, injeta glifos
    acentuados nas fontes .ffu e salva a estrutura pronta em
    patches_prontos/atmosphere/contents/<TITLE_ID>/romfs.
    """
    romfs_path = Path(romfs_dir)
    translated_map: dict[str, str] = json.loads(
        Path(translated_json_path).read_text(encoding="utf-8")
    )
    manifest: dict[str, dict] = json.loads(
        Path(manifest_path).read_text(encoding="utf-8")
    )

    atmosphere_root = Path("patches_prontos") / "atmosphere"
    patch_romfs = atmosphere_root / "contents" / title_id / "romfs"
    patch_romfs.mkdir(parents=True, exist_ok=True)

    # Agrupa traduções por (cpk_rel_path, unpacked_cpk_dir, internal_rel_path) -> {str_offset: texto}
    stcm2l_groups: dict[tuple[str, str, str], dict[int, str]] = defaultdict(dict)
    plain_groups: dict[str, list[dict]] = defaultdict(list)

    for sid, meta in manifest.items():
        if sid not in translated_map:
            continue
        tr_text = translated_map[sid]
        entry_type = meta.get("type")

        if entry_type == "cri_cpk_stcm2l":
            key = (
                meta["cpk_rel_path"],
                meta["unpacked_cpk_dir"],
                meta["internal_rel_path"],
            )
            stcm2l_groups[key][int(meta["str_offset"])] = tr_text

        elif entry_type == "plain_text":
            plain_groups[meta["rel_path"]].append(
                {
                    "line_idx": meta["line_idx"],
                    "original": meta["original"],
                    "translated": tr_text,
                }
            )

    # Reconstrói cada script STCM2L (.DAT) modificado
    cpk_modifications: dict[str, dict[str, bytes]] = defaultdict(dict)
    for (cpk_rel_path, unpacked_cpk_dir, internal_rel_path), offsets_map in stcm2l_groups.items():
        dat_path = Path(unpacked_cpk_dir) / Path(internal_rel_path)
        if not dat_path.is_file():
            continue
        raw_dat = dat_path.read_bytes()
        rebuilt_dat = rebuild_stcm2l(raw_dat, offsets_map, strip_accents=True)
        if rebuilt_dat != raw_dat:
            cpk_modifications[cpk_rel_path][internal_rel_path] = rebuilt_dat

    # Reempacota cada .CPK modificado
    for cpk_rel_path, files_dict in cpk_modifications.items():
        orig_cpk = romfs_path / Path(cpk_rel_path)
        if orig_cpk.is_file():
            out_cpk = patch_romfs / Path(cpk_rel_path)
            out_cpk.parent.mkdir(parents=True, exist_ok=True)
            repack_cpk(str(orig_cpk), files_dict, str(out_cpk))

    # Reconstrói arquivos de texto plano (caso a engine não use CPK)
    for rel_path, items in plain_groups.items():
        src_file = romfs_path / Path(rel_path)
        if not src_file.is_file():
            continue
        lines = src_file.read_text(encoding="utf-8").splitlines()
        for item in items:
            idx = item["line_idx"]
            if 0 <= idx < len(lines):
                lines[idx] = lines[idx].replace(item["original"], item["translated"], 1)
        out_file = patch_romfs / Path(rel_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("\n".join(lines), encoding="utf-8")

    # Injeta automaticamente os acentos PT-BR nas fontes .ffu (ex: SYSTEM.CPK)
    auto_patch_romfs_fonts(str(romfs_path), str(patch_romfs), log_fn=log_fn)

    return str(atmosphere_root)