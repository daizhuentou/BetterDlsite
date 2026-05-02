import re
import json
import shutil
from pathlib import Path


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "output" / "data"
JSON_DIR = DATA_DIR / "json"
TRANSLATE_DIR = DATA_DIR / "translate"
PENDING_TRANSLATE_DIR = BASE_DIR / "待翻译"
DONE_TRANSLATE_DIR = TRANSLATE_DIR / "已翻译"
TRANSLATED_DRAFT_DIR = BASE_DIR / "翻译稿"
LEGACY_TRANSLATE_DIR = DONE_TRANSLATE_DIR / "legacy_pages"
WORK_ID_PATTERN = r"(?:RJ|VJ)\d+"
WORK_ID_FILE_PATTERNS = ("RJ*.zh.md", "VJ*.zh.md")


def parse_translate_md(md_text, default_work_id=None):
    blocks = re.split(rf'^##\s+({WORK_ID_PATTERN})\s*$', md_text, flags=re.MULTILINE)
    if len(blocks) == 1 and default_work_id:
        blocks = ["", default_work_id, md_text]

    results = []
    for i in range(1, len(blocks), 2):
        work_id = blocks[i]
        block = blocks[i + 1]
        work_trans = {"product_id": work_id}

        name_match = re.search(r'-\s*\*\*\[[^\n]*?\]\*\*:\s*(.+)', block)
        if name_match and name_match.group(1).strip():
            work_trans["work_name_trans"] = name_match.group(1).strip()

        part_translations = []
        all_sections = re.split(r'^### ', block, flags=re.MULTILINE)
        for section in all_sections:
            first_newline = section.find('\n')
            if first_newline == -1:
                continue

            heading = section[:first_newline].strip()
            content = section[first_newline + 1:]
            if any(token in heading for token in ("简介", "绠")):
                desc_match = re.search(
                    r'\*\*\[[^\n]*?\]\*\*:\s*(.+?)(?=\n\n###|\n##|\Z)',
                    content,
                    re.DOTALL,
                )
                if desc_match and desc_match.group(1).strip():
                    work_trans["description_trans"] = desc_match.group(1).strip()
                continue

            if any(token in heading for token in ("作品名称", "浣滃搧")):
                continue

            trans_match = re.search(
                r'\*\*\[[^\n]*?\]\*\*:\s*(.+?)(?=\n\n###|\n##|\Z)',
                content,
                re.DOTALL,
            )
            if trans_match and trans_match.group(1).strip():
                part_translations.append({
                    "heading": heading,
                    "content_trans": trans_match.group(1).strip(),
                })

        work_trans["parts_trans"] = part_translations
        results.append(work_trans)

    return results


def split_translate_blocks(md_text):
    parts = re.split(rf'(^##\s+{WORK_ID_PATTERN}\s*$)', md_text, flags=re.MULTILINE)
    blocks = {}

    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        body = parts[i + 1]
        match = re.match(rf'^##\s+({WORK_ID_PATTERN})\s*$', heading)
        if not match:
            continue

        work_id = match.group(1)
        block = f"{heading}\n{body}".strip() + "\n"
        blocks[work_id] = block

    return blocks


def unique_move_path(target_path):
    if not target_path.exists():
        return target_path

    stem = target_path.stem
    suffix = target_path.suffix
    parent = target_path.parent
    index = 1
    while True:
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def move_file_no_overwrite(source_path, target_path):
    target_path.parent.mkdir(parents=True, exist_ok=True)
    final_path = unique_move_path(target_path)
    shutil.move(str(source_path), str(final_path))
    return final_path


def migrate_legacy_page_translations():
    if not TRANSLATE_DIR.exists():
        return 0, 0

    PENDING_TRANSLATE_DIR.mkdir(parents=True, exist_ok=True)
    DONE_TRANSLATE_DIR.mkdir(parents=True, exist_ok=True)
    TRANSLATED_DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    LEGACY_TRANSLATE_DIR.mkdir(parents=True, exist_ok=True)

    migrated = 0
    skipped = 0

    for pattern in WORK_ID_FILE_PATTERNS:
        for root_zh in sorted(TRANSLATE_DIR.glob(pattern)):
            target_path = TRANSLATED_DRAFT_DIR / root_zh.name
            if target_path.exists():
                skipped += 1
                continue
            move_file_no_overwrite(root_zh, target_path)
            migrated += 1

    for pattern in WORK_ID_FILE_PATTERNS:
        for done_zh in sorted(DONE_TRANSLATE_DIR.glob(pattern)):
            target_path = TRANSLATED_DRAFT_DIR / done_zh.name
            if target_path.exists():
                skipped += 1
                continue
            move_file_no_overwrite(done_zh, target_path)
            migrated += 1

    for page_file in sorted(TRANSLATE_DIR.glob("translate_page_*.zh.md")):
        with open(page_file, "r", encoding="utf-8") as f:
            blocks = split_translate_blocks(f.read())

        for work_id, block in blocks.items():
            target_path = TRANSLATED_DRAFT_DIR / f"{work_id}.zh.md"
            if target_path.exists():
                skipped += 1
                continue
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(block)
            migrated += 1

        archive_path = LEGACY_TRANSLATE_DIR / page_file.name
        move_file_no_overwrite(page_file, archive_path)

    for page_md in sorted(TRANSLATE_DIR.glob("translate_page_*.md")):
        if page_md.name.endswith(".zh.md"):
            continue
        archive_path = LEGACY_TRANSLATE_DIR / page_md.name
        move_file_no_overwrite(page_md, archive_path)

    return migrated, skipped


def find_translation_files():
    files = []
    seen = set()
    search_roots = [TRANSLATED_DRAFT_DIR, TRANSLATE_DIR]

    for root in search_roots:
        if not root.exists():
            continue

        for pattern in ("translate_page_*.zh.md", *WORK_ID_FILE_PATTERNS):
            for md_file in sorted(root.rglob(pattern), key=lambda p: p.stat().st_mtime):
                if md_file in seen:
                    continue
                if md_file.parent == LEGACY_TRANSLATE_DIR:
                    continue
                seen.add(md_file)
                files.append(md_file)

    return files


def load_translations():
    files = find_translation_files()
    translations = {}
    sources = {}

    for md_file in files:
        default_work_id = None
        work_match = re.match(rf'^({WORK_ID_PATTERN})\.zh\.md$', md_file.name)
        if work_match:
            default_work_id = work_match.group(1)

        with open(md_file, "r", encoding="utf-8") as f:
            for trans in parse_translate_md(f.read(), default_work_id):
                if trans.get("product_id"):
                    work_id = trans["product_id"]
                    existing_source = sources.get(work_id)
                    if existing_source and existing_source.parent == TRANSLATED_DRAFT_DIR:
                        continue
                    translations[work_id] = trans
                    sources[work_id] = md_file

    return translations, sources, files


def apply_translations_to_json(json_data, translations):
    translated_count = 0
    applied_work_ids = set()

    for work in json_data:
        trans = translations.get(work.get("product_id"))
        if not trans:
            continue

        changed = False

        if trans.get("work_name_trans"):
            work["work_name_trans"] = trans["work_name_trans"]
            changed = True

        if trans.get("description_trans"):
            work["description"] = trans["description_trans"]
            work["description_clean"] = trans["description_trans"]
            changed = True

        text_parts_trans = trans.get("parts_trans", [])
        text_part_idx = 0

        for part in work.get("parts", []):
            if part.get("type") != "text":
                continue

            if text_part_idx < len(text_parts_trans):
                part_trans = text_parts_trans[text_part_idx]
                if part_trans.get("content_trans"):
                    part["content"] = part_trans["content_trans"]
                    changed = True

            text_part_idx += 1

        if changed:
            translated_count += 1
            applied_work_ids.add(work.get("product_id"))

    return json_data, translated_count, applied_work_ids


def archive_completed_pending_files(applied_work_ids, sources):
    moved_pending = 0
    moved_zh = 0

    DONE_TRANSLATE_DIR.mkdir(parents=True, exist_ok=True)
    TRANSLATED_DRAFT_DIR.mkdir(parents=True, exist_ok=True)

    for work_id in sorted(applied_work_ids):
        pending_path = PENDING_TRANSLATE_DIR / f"{work_id}.md"
        if pending_path.exists():
            move_file_no_overwrite(pending_path, DONE_TRANSLATE_DIR / pending_path.name)
            moved_pending += 1

        source_path = sources.get(work_id)
        if (
            source_path
            and source_path.exists()
            and source_path.name == f"{work_id}.zh.md"
            and source_path.parent != TRANSLATED_DRAFT_DIR
        ):
            move_file_no_overwrite(source_path, TRANSLATED_DRAFT_DIR / source_path.name)
            moved_zh += 1

    return moved_pending, moved_zh


def main():
    if not JSON_DIR.exists():
        print("未找到 data/json 目录，请先运行 generate.py。")
        return

    if not TRANSLATE_DIR.exists():
        print("未找到 translate 目录，请先运行 generate.py。")
        return

    migrated, skipped = migrate_legacy_page_translations()
    if migrated or skipped:
        print(f"已整理译文稿: 移入/拆出 {migrated} 个作品译文，跳过 {skipped} 个已存在译文。")

    translations, sources, md_files = load_translations()
    if not md_files:
        print("未找到翻译文件。请将翻译后的文件命名为 RJxxxx.zh.md 或 VJxxxx.zh.md 并放入项目根目录的 翻译稿 文件夹。")
        print("旧格式 translate_page_*.zh.md 仍然兼容。")
        return

    if not translations:
        print("找到了翻译文件，但没有解析到可用的作品翻译块。")
        return

    json_files = sorted(JSON_DIR.rglob("page_*.json"))
    total_pages = 0
    total_works = 0
    total_translated = 0
    applied_work_ids = set()

    for json_path in json_files:
        with open(json_path, "r", encoding="utf-8") as f:
            json_data = json.load(f)

        json_data, translated_count, page_applied_ids = apply_translations_to_json(json_data, translations)
        applied_work_ids.update(page_applied_ids)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

        total_pages += 1
        total_works += len(json_data)
        total_translated += translated_count
        rel_path = json_path.relative_to(DATA_DIR)
        print(f"  已更新: {rel_path} ({translated_count}/{len(json_data)} 个作品命中翻译)")

    moved_pending, moved_zh = archive_completed_pending_files(applied_work_ids, sources)

    print(
        f"\n导入完成：读取 {len(md_files)} 个翻译文件，"
        f"解析 {len(translations)} 个作品；更新 {total_pages} 个 JSON 页，"
        f"{total_translated}/{total_works} 个展示条目命中翻译。"
    )
    print(f"已归档 {moved_pending} 个待翻译稿到 {DONE_TRANSLATE_DIR}。")
    print(f"已整理 {moved_zh} 个作品译文到 {TRANSLATED_DRAFT_DIR}。")


if __name__ == "__main__":
    main()
