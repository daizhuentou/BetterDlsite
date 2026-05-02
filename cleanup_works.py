import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
WORKS_DIR = BASE_DIR / "works"
CRAWL_RESULTS_FILE = BASE_DIR / "crawl_results.json"
WORK_FILE_PATTERNS = ("RJ*.html", "VJ*.html")


def load_crawl_work_ids():
    if not CRAWL_RESULTS_FILE.exists():
        print(f"未找到 {CRAWL_RESULTS_FILE}")
        return set()

    with open(CRAWL_RESULTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    raw_categories = data.get("categories", []) if isinstance(data, dict) else data

    work_ids = set()
    for item in raw_categories:
        if not isinstance(item, dict):
            continue
        for wid in item.get("work_ids", []):
            work_ids.add(wid)

    return work_ids


def main():
    crawl_ids = load_crawl_work_ids()
    if not crawl_ids:
        print("crawl_results.json 中没有作品ID，退出")
        return

    print(f"crawl_results.json 中共有 {len(crawl_ids)} 个作品ID")

    all_html = []
    for pattern in WORK_FILE_PATTERNS:
        all_html.extend(WORKS_DIR.glob(pattern))

    print(f"works/ 目录中共有 {len(all_html)} 个HTML文件")

    to_delete = []
    for html_file in all_html:
        stem = html_file.stem
        if stem not in crawl_ids:
            to_delete.append(html_file)

    if not to_delete:
        print("没有需要删除的文件")
        return

    print(f"以下 {len(to_delete)} 个文件不在 crawl_results.json 中，将被删除：")
    for f in to_delete:
        print(f"  {f.name}")

    confirm = input("\n确认删除？(y/N): ").strip().lower()
    if confirm != "y":
        print("已取消")
        return

    deleted = 0
    for f in to_delete:
        f.unlink()
        deleted += 1

    print(f"已删除 {deleted} 个文件")


if __name__ == "__main__":
    main()
