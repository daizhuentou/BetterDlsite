import re
import asyncio
import aiohttp
from pathlib import Path


BASE_DIR = Path(__file__).parent
WORKS_DIR = BASE_DIR / "works"
FAILED_LOG = BASE_DIR / "failed_works.md"
MAX_CONCURRENT = 10
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.dlsite.com/",
    "Cookie": "adultchecked=1",
}


def parse_failed_works():
    if not FAILED_LOG.exists():
        return []

    with open(FAILED_LOG, "r", encoding="utf-8") as f:
        content = f.read()

    works = []
    lines = content.split("\n")
    for line in lines:
        if line.startswith("|") and "RJ" in line or "VJ" in line:
            if line.startswith("| RJ号") or line.startswith("|------"):
                continue

            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4:
                work_id = parts[1]
                work_name = parts[2]
                work_url = parts[3]

                if re.match(r"^(RJ|VJ)\d+$", work_id):
                    works.append({
                        "id": work_id,
                        "name": work_name,
                        "url": work_url,
                    })

    return works


async def download_page(session, url):
    try:
        async with session.get(url, timeout=30) as resp:
            return await resp.text("utf-8", errors="ignore")
    except Exception as e:
        print(f"    下载失败: {e}")
        return None


def extract_work_name_from_html(html):
    if not html:
        return ""

    name_match = re.search(r'<h1[^>]*id=["\']work_name["\'][^>]*>(.*?)</h1>', html, re.DOTALL)
    if not name_match:
        return ""

    name = re.sub(r'<[^>]+>', '', name_match.group(1))
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def is_valid_work_html(html):
    if not html:
        return False, "没有下载到内容"
    if len(html) <= 500:
        return False, f"内容过短 ({len(html)} 字节)"
    if not extract_work_name_from_html(html):
        return False, "没有解析到作品名"
    return True, ""


def build_work_url(work_id, work_url=""):
    from urllib.parse import urlsplit
    site_area = "pro" if work_id.startswith("VJ") else "maniax"
    if work_url:
        path_parts = [part for part in urlsplit(work_url).path.split("/") if part]
        if path_parts:
            site_area = path_parts[0]
    return f"https://www.dlsite.com/{site_area}/work/=/product_id/{work_id}.html"


def build_announce_url(work_id, work_url=""):
    from urllib.parse import urlsplit
    site_area = "pro" if work_id.startswith("VJ") else "maniax"
    if work_url:
        path_parts = [part for part in urlsplit(work_url).path.split("/") if part]
        if path_parts:
            site_area = path_parts[0]
    return f"https://www.dlsite.com/{site_area}/announce/=/product_id/{work_id}.html"


async def retry_download_work(session, work):
    work_id = work["id"]
    work_name = work["name"]
    work_url = work["url"]
    save_path = WORKS_DIR / f"{work_id}.html"

    urls_to_try = [work_url]

    if "announce" in work_url:
        work_format_url = build_work_url(work_id, work_url)
        urls_to_try.append(work_format_url)
    else:
        announce_url = build_announce_url(work_id, work_url)
        urls_to_try.append(announce_url)

    for url_idx, current_url in enumerate(urls_to_try):
        for attempt in range(1, MAX_RETRIES + 1):
            if url_idx > 0:
                print(f"    尝试 {'work' if 'work' in current_url else 'announce'} 格式")

            html = await download_page(session, current_url)
            valid, reason = is_valid_work_html(html)

            if valid:
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"  [OK] {work_id} - {work_name}")
                return True

            print(f"    无效: {reason} (尝试 {attempt}/{MAX_RETRIES})")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1)

    print(f"  [FAIL] {work_id} - {work_name}")
    return False


def update_failed_log(remaining_works):
    if not remaining_works:
        FAILED_LOG.unlink(missing_ok=True)
        print("\n所有失败作品已成功下载，已删除 failed_works.md")
        return

    with open(FAILED_LOG, "w", encoding="utf-8") as f:
        f.write("# 下载失败的作品列表\n\n")
        f.write("| RJ号 | 作品名称 | 链接 |\n")
        f.write("|------|----------|------|\n")
        for work in remaining_works:
            f.write(f"| {work['id']} | {work['name']} | {work['url']} |\n")

    print(f"\n已更新 failed_works.md，剩余 {len(remaining_works)} 个失败作品")


async def main():
    WORKS_DIR.mkdir(parents=True, exist_ok=True)

    works = parse_failed_works()
    if not works:
        print("没有找到失败的作品记录")
        return

    print(f"找到 {len(works)} 个失败作品，开始重试下载...\n")

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)

        async def bounded_download(work):
            async with semaphore:
                return await retry_download_work(session, work)

        results = await asyncio.gather(*[bounded_download(w) for w in works])

    success_count = sum(results)
    fail_count = len(results) - success_count

    print(f"\n下载完成: 成功 {success_count}，失败 {fail_count}")

    remaining_works = [w for w, success in zip(works, results) if not success]
    update_failed_log(remaining_works)


if __name__ == "__main__":
    asyncio.run(main())
