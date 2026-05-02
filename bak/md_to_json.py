import re
import json
from pathlib import Path


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "output" / "data"
JSON_DIR = DATA_DIR / "json"
TRANSLATE_DIR = DATA_DIR / "translate"


def parse_translate_md(md_text):
    results = []
    work_blocks = re.split(r'^## (RJ\d+)$', md_text, flags=re.MULTILINE)
    
    for i in range(1, len(work_blocks), 2):
        work_id = work_blocks[i]
        block = work_blocks[i + 1]
        
        work_trans = {"product_id": work_id}
        
        name_match = re.search(r'-\s*\*\*\[译文\]\*\*:\s*(.+)', block)
        if name_match and name_match.group(1).strip():
            work_trans["work_name_trans"] = name_match.group(1).strip()
        
        desc_match = re.search(r'\*\*\[简介译文\]\*\*:\s*(.+?)(?=\n\n###|\n##|\Z)', block, re.DOTALL)
        if desc_match and desc_match.group(1).strip():
            work_trans["description_trans"] = desc_match.group(1).strip()
        
        part_translations = []
        all_sections = re.split(r'^### ', block, flags=re.MULTILINE)
        
        for section in all_sections:
            first_newline = section.find('\n')
            if first_newline == -1:
                continue
            
            heading = section[:first_newline].strip()
            content = section[first_newline + 1:]
            
            if heading == "作品名称" or heading == "简介":
                continue
            
            trans_match = re.search(r'\*\*\[译文\]\*\*:\s*(.+?)(?=\n\n###|\n##|\Z)', content, re.DOTALL)
            if trans_match and trans_match.group(1).strip():
                part_translations.append({
                    "heading": heading,
                    "content_trans": trans_match.group(1).strip()
                })
        
        work_trans["parts_trans"] = part_translations
        results.append(work_trans)
    
    return results


def apply_translations_to_json(json_data, translations):
    trans_dict = {t["product_id"]: t for t in translations}
    
    for work in json_data:
        if work["product_id"] not in trans_dict:
            continue
        
        trans = trans_dict[work["product_id"]]
        
        if trans.get("work_name_trans"):
            work["work_name_trans"] = trans["work_name_trans"]
        
        if trans.get("description_trans"):
            work["description"] = trans["description_trans"]
            work["description_clean"] = trans["description_trans"]
        
        text_parts_trans = trans.get("parts_trans", [])
        text_part_idx = 0
        
        for part in work["parts"]:
            if part["type"] != "text":
                continue
            
            if text_part_idx < len(text_parts_trans):
                part_trans = text_parts_trans[text_part_idx]
                if part_trans.get("content_trans"):
                    part["content"] = part_trans["content_trans"]
            
            text_part_idx += 1
    
    return json_data


def main():
    if not JSON_DIR.exists():
        print("未找到 data/json 目录！请先运行 generate.py。")
        return
    
    md_files = sorted(TRANSLATE_DIR.glob("translate_page_*.zh.md"))
    if not md_files:
        print("未找到翻译文件 translate/translate_page_*.zh.md！")
        print("请将翻译后的文件命名为 translate_page_X.zh.md 并放入 translate/ 目录")
        return
    
    total_updated = 0
    total_translated = 0
    
    for md_file in md_files:
        page_match = re.search(r'translate_page_(\d+)\.zh', md_file.name)
        if not page_match:
            continue
        page_num = page_match.group(1)
        
        json_path = JSON_DIR / f"page_{page_num}.json"
        if not json_path.exists():
            print(f"  跳过: {md_file.name} (对应的 json/page_{page_num}.json 不存在)")
            continue
        
        with open(md_file, "r", encoding="utf-8") as f:
            md_text = f.read()
        
        with open(json_path, "r", encoding="utf-8") as f:
            json_data = json.load(f)
        
        translations = parse_translate_md(md_text)
        
        translated_count = 0
        for t in translations:
            if t.get("work_name_trans") or t.get("description_trans") or t.get("parts_trans"):
                translated_count += 1
        
        json_data = apply_translations_to_json(json_data, translations)
        
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
        
        total_updated += len(json_data)
        total_translated += translated_count
        print(f"  ✓ 第 {page_num} 页: {translated_count}/{len(json_data)} 个作品有翻译 -> page_{page_num}.json")
    
    print(f"\n导入完成！共更新 {total_updated} 个作品，其中 {total_translated} 个有翻译。")


if __name__ == "__main__":
    main()
