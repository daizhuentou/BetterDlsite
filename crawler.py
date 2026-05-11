import re
import sys
import json
import asyncio
import aiohttp
import html as html_lib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


BASE_DIR = Path(__file__).parent
WORKS_DIR = BASE_DIR / "works"
FAILED_LOG = BASE_DIR / "failed_works.md"
ORDER_FILE = BASE_DIR / "works_order.json"
CRAWL_RESULTS_FILE = BASE_DIR / "crawl_results.json"
GENRE_LIST_FILE = BASE_DIR / "list.devtools"
URL_HISTORY_FILE = BASE_DIR / "url_history.json"
ASMR_SUBTITLE_CACHE_FILE = BASE_DIR / "asmr_subtitle_cache.json"
MAX_CONCURRENT = 100
MAX_PAGES = 0  # 0 表示不限制；大于 0 表示本次最多爬取多少个搜索结果页
MAX_WORK_RETRIES = 3
WORK_RETRY_DELAY = 0
WORK_ID_PATTERN = r"(?:RJ|VJ)\d+"
_GENRE_NAME_MAP = None
ASMR_SUBTITLE_FILTER_FLAGS = {
    "--subtitle-asmr-only",
    "--only-subtitle-asmr",
    "--asmr-subtitle-only",
}
ASMR_SUBTITLE_API_TEMPLATE = (
    "https://api.asmr-200.com/api/search/{work_id}"
    "?order=create_date&sort=desc&page=1&pageSize=20"
    "&subtitle=1&includeTranslationWorks=true"
)
ASMR_SUBTITLE_CONCURRENCY = 2
ASMR_SUBTITLE_MAX_RETRIES = 8
ASMR_SUBTITLE_RETRY_DELAY = 3

DEFAULT_URL = (
    "https://www.dlsite.com/maniax/fsr/=/language/jp/sex_category%5B0%5D/male/"
    "order%5B0%5D/trend/work_type_category%5B0%5D/game/"
    "work_type_category_name%5B0%5D/%E6%B8%B8%E6%88%8F/genre%5B0%5D/302/"
    "genre_name%5B0%5D/%E5%AF%9D%E5%8F%96%E3%82%8A/options_and_or/and/"
    "options%5B0%5D/JPN/options%5B1%5D/CHI_HANS/options%5B2%5D/CHI_HANT/"
    "options%5B3%5D/NM/options_name%5B0%5D/%E6%97%A5%E8%AF%AD%E4%BD%9C%E5%93%81/"
    "options_name%5B1%5D/%E7%AE%80%E4%BD%9C%E4%B8%AD%E6%96%87%E4%BD%9C%E5%93%81/"
    "options_name%5B2%5D/%E7%B9%81%E4%BD%9C%E4%B8%AD%E6%96%87%E4%BD%9C%E5%93%81/"
    "options_name%5B3%5D/%E6%97%A5%E8%AF%AD%E8%A8%80%E9%99%90%E5%88%B6/"
    "per_page/100/page/1/show_type/3/lang_options%5B0%5D/%E6%97%A5%E8%AF%AD/"
    "lang_options%5B1%5D/%E4%B8%AD%E6%96%87%28%E7%AE%80%E4%BD%9C%E5%AD%97%29/"
    "lang_options%5B2%5D/%E4%B8%AD%E6%96%87%28%E7%B9%81%E4%BD%9C%E5%AD%97%29/"
    "lang_options%5B3%5D/%E6%97%A5%E8%AF%AD%E8%A8%80%E9%99%90%E5%88%B6"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36, "
        "like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.dlsite.com/",
    "Cookie": "adultchecked=1",
}


async def download_page(session, url):
    """Download one HTML page."""
    try:
        async with session.get(url, timeout=30) as resp:
            return await resp.text("utf-8", errors="ignore")
    except Exception as e:
        print(f"  下载失败: {url} - {e}")
        return None


def load_asmr_subtitle_cache():
    if not ASMR_SUBTITLE_CACHE_FILE.exists():
        return {}

    try:
        with open(ASMR_SUBTITLE_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    return data if isinstance(data, dict) else {}


def save_asmr_subtitle_cache(cache):
    with open(ASMR_SUBTITLE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def get_cached_asmr_subtitle(cache, work_id):
    entry = cache.get(work_id.upper())
    if isinstance(entry, dict) and entry.get("status") == "ok":
        return bool(entry.get("has_subtitle"))
    if isinstance(entry, bool):
        return entry
    return None


def set_cached_asmr_subtitle(cache, work_id, has_subtitle):
    cache[work_id.upper()] = {
        "has_subtitle": bool(has_subtitle),
        "status": "ok",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


def build_asmr_subtitle_api_url(work_id):
    return ASMR_SUBTITLE_API_TEMPLATE.format(work_id=quote(work_id.upper(), safe=""))


def has_valid_asmr_subtitle_result(data, work_id):
    if not isinstance(data, dict):
        return False

    works = data.get("works")
    if not isinstance(works, list) or not works:
        return False

    normalized_id = work_id.upper()
    for work in works:
        if not isinstance(work, dict):
            continue
        for key in ("source_id", "workno", "work_id", "product_id", "dlsite_id"):
            value = work.get(key)
            if isinstance(value, str) and value.upper() == normalized_id:
                return True
    return True


async def query_asmr_subtitle(session, work_id, cache):
    work_id = work_id.upper()
    if not work_id.startswith("RJ"):
        return False

    cached = get_cached_asmr_subtitle(cache, work_id)
    if cached is not None:
        return cached

    url = build_asmr_subtitle_api_url(work_id)
    for attempt in range(1, ASMR_SUBTITLE_MAX_RETRIES + 1):
        try:
            async with session.get(url, timeout=20) as resp:
                if resp.status == 429 and attempt < ASMR_SUBTITLE_MAX_RETRIES:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait_seconds = float(retry_after) if retry_after else ASMR_SUBTITLE_RETRY_DELAY * attempt
                    except ValueError:
                        wait_seconds = ASMR_SUBTITLE_RETRY_DELAY * attempt
                    print(f"  字幕 API 限流: {work_id}，{wait_seconds:.1f} 秒后重试")
                    await asyncio.sleep(wait_seconds)
                    continue
                if resp.status != 200:
                    print(f"  字幕 API 状态异常: {work_id} HTTP {resp.status}")
                    return False
                data = await resp.json(content_type=None)
                has_subtitle = has_valid_asmr_subtitle_result(data, work_id)
                set_cached_asmr_subtitle(cache, work_id, has_subtitle)
                return has_subtitle
        except Exception as e:
            if attempt < ASMR_SUBTITLE_MAX_RETRIES:
                wait_seconds = ASMR_SUBTITLE_RETRY_DELAY * attempt
                print(f"  字幕 API 查询失败: {work_id} - {e}，{wait_seconds:.1f} 秒后重试")
                await asyncio.sleep(wait_seconds)
                continue
            print(f"  字幕 API 查询失败: {work_id} - {e}")
            return False

    return False


def clean_listing_text(html):
    text = re.sub(r"<[^>]+>", " ", html)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def is_audio_search_url(url):
    return "work_type_category[0]/audio" in unquote(url)


def is_audio_asmr_listing_block(block, source_url=""):
    text = clean_listing_text(block)
    haystack = html_lib.unescape(block) + " " + text
    normalized = re.sub(r"\s+", "", haystack).upper()
    if "ASMR" in normalized:
        return True
    if any(keyword in haystack for keyword in ("ボイス・ASMR", "ボイス", "音声")):
        return True
    return is_audio_search_url(source_url)


def extract_work_links(html, source_url=""):
    """Extract product links from a DLsite search/listing page."""
    work_blocks = re.split(r'<li\s+[^>]*data-list_item_product_id=', html)[1:]
    work_links = []
    seen_ids = set()

    for block in work_blocks:
        id_match = re.search(rf'["\']({WORK_ID_PATTERN})["\']', block)
        if not id_match:
            continue
        work_id = id_match.group(1)

        if work_id in seen_ids:
            continue
        seen_ids.add(work_id)

        link_match = re.search(
            r'<a\s+[^>]*href="([^"]*/work/[^"]*)"[^>]*title="([^"]*)"',
            block,
        )
        work_url = ""
        work_name = ""
        if link_match:
            work_url = normalize_dlsite_url(html_lib.unescape(link_match.group(1)))
            work_name = html_lib.unescape(link_match.group(2))

        if not work_url:
            site_area = "pro" if work_id.startswith("VJ") else "maniax"
            work_url = f"https://www.dlsite.com/{site_area}/work/=/product_id/{work_id}.html"

        work_links.append({
            "id": work_id,
            "name": work_name,
            "url": work_url,
            "is_audio_asmr": is_audio_asmr_listing_block(block, source_url),
        })

    return work_links


def normalize_dlsite_url(url):
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.dlsite.com" + url
    return url


def has_next_page(html, current_page):
    """Check whether the listing has a next page."""
    next_pattern = rf'rel="next"|page/{current_page + 1}'
    return bool(re.search(next_pattern, html))


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


def is_valid_saved_work_file(save_path):
    if not save_path.exists():
        return False

    try:
        with open(save_path, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return False

    valid, _ = is_valid_work_html(html)
    return valid


def build_announce_url(work_id, work_url=""):
    site_area = "pro" if work_id.startswith("VJ") else "maniax"
    if work_url:
        path_parts = [part for part in urlsplit(work_url).path.split("/") if part]
        if path_parts:
            site_area = path_parts[0]
    return f"https://www.dlsite.com/{site_area}/announce/=/product_id/{work_id}.html"


def parse_cli_queue_args():
    raw_args = [arg.strip() for arg in sys.argv[1:] if arg.strip()]
    if not raw_args:
        return None, None, None

    subtitle_asmr_only = False
    args = []
    for arg in raw_args:
        if arg in ASMR_SUBTITLE_FILTER_FLAGS:
            subtitle_asmr_only = True
            continue
        args.append(arg)

    max_pages = None
    if len(args) >= 2 and args[-1].isdigit():
        max_pages = int(args[-1])
        args = args[:-1]

    if not args:
        return None, max_pages, subtitle_asmr_only

    return args, max_pages, subtitle_asmr_only


def load_url_history():
    if not URL_HISTORY_FILE.exists():
        return []
    try:
        with open(URL_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_url_history(urls):
    history = load_url_history()
    existing = set(history)
    for url in urls:
        if url not in existing:
            history.append(url)
            existing.add(url)
    with open(URL_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def prompt_search_urls():
    history = load_url_history()

    if history:
        print("历史 URL 记录：")
        for i, url in enumerate(history, 1):
            print(f"  [{i}] {url}")
        print()
        print("输入序号选择历史 URL（多个用逗号分隔，如 1,3,5），或直接输入新 URL：")
    else:
        print("请输入 DLsite 搜索/分类页 URL。每输入一个链接会加入队列，直接回车开始爬取。")

    urls = []

    while True:
        user_input = input(f"URL {len(urls) + 1}: ").strip()

        if not user_input:
            break

        if history and user_input.lower() == 'all':
            print(f"  已选择全部 {len(history)} 个历史 URL")
            urls.extend(history)
            continue

        if history and re.match(r'^[\d,\s]+$', user_input):
            selected = []
            for part in user_input.split(','):
                part = part.strip()
                if not part:
                    continue
                try:
                    idx = int(part) - 1
                    if 0 <= idx < len(history):
                        selected.append(history[idx])
                    else:
                        print(f"  序号 {part} 超出范围")
                except ValueError:
                    pass
            if selected:
                print(f"  已选择 {len(selected)} 个历史 URL")
                urls.extend(selected)
            continue

        if 'genre[0]' not in user_input:
            print("  URL 无效：必须包含 genre[0] 参数（DLsite 分类页 URL）")
            continue

        urls.append(user_input)
        print(f"  已加入队列 ({len(urls)}): {user_input}")

    if not urls:
        if history:
            print("  未输入 URL，使用全部历史 URL。")
            return history
        print("  未输入 URL，使用脚本内置的默认 URL。")
        return [DEFAULT_URL]

    return urls


def prompt_max_pages():
    value = input(f"每个链接最大爬取页数(0=不限制，留空={MAX_PAGES}): ").strip()

    if not value:
        return MAX_PAGES

    try:
        max_pages = int(value)
    except ValueError:
        print(f"最大页数输入无效，使用默认值 {MAX_PAGES}")
        return MAX_PAGES

    if max_pages < 0:
        print(f"最大页数不能小于 0，使用默认值 {MAX_PAGES}")
        return MAX_PAGES

    return max_pages


def prompt_subtitle_asmr_only():
    value = input("是否只下载有字幕的音声 ASMR？(y/N): ").strip().lower()
    if not value:
        return True
    return value in ("y", "yes", "1", "true", "是")


def split_decoded_path(url):
    path = urlsplit(url).path
    return [unquote(part) for part in path.split("/") if part]


def extract_value_after_path_key(url, key):
    parts = split_decoded_path(url)
    for idx, part in enumerate(parts[:-1]):
        if part == key:
            return parts[idx + 1]
    return ""


def clean_genre_label(label_html):
    label_html = re.sub(
        r'<span[^>]*class=["\'][^"\']*number[^"\']*["\'][^>]*>.*?</span>',
        '',
        label_html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    label = re.sub(r'<[^>]+>', '', label_html)
    label = html_lib.unescape(label)
    return re.sub(r'\s+', ' ', label).strip()


def load_genre_name_map():
    global _GENRE_NAME_MAP
    if _GENRE_NAME_MAP is not None:
        return _GENRE_NAME_MAP

    genre_map = {}
    if GENRE_LIST_FILE.exists():
        try:
            with open(GENRE_LIST_FILE, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            content = ""

        for match in re.finditer(
            r'<a\s+[^>]*href=["\'][^"\']*/genre/(\d+)[^"\']*["\'][^>]*>(.*?)</a>',
            content,
            re.DOTALL | re.IGNORECASE,
        ):
            genre_id = match.group(1)
            label = clean_genre_label(match.group(2))
            if label and genre_id not in genre_map:
                genre_map[genre_id] = label

    if CRAWL_RESULTS_FILE.exists():
        try:
            with open(CRAWL_RESULTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

        categories = data.get("categories", []) if isinstance(data, dict) else data
        for item in (categories if isinstance(categories, list) else []):
            if not isinstance(item, dict):
                continue
            source_url = item.get("source_url", "")
            genre_id = extract_value_after_path_key(source_url, "genre[0]")
            genre_name = extract_value_after_path_key(source_url, "genre_name[0]")
            if genre_id and genre_name and genre_id not in genre_map:
                genre_map[genre_id] = genre_name
            elif (
                genre_id
                and item.get("name")
                and item.get("name") != "未分类"
                and item.get("name") != f"genre_{genre_id}"
                and genre_id not in genre_map
            ):
                genre_map[genre_id] = item["name"]

    _GENRE_NAME_MAP = genre_map
    return _GENRE_NAME_MAP


def extract_category_name(url):
    genre_name = extract_value_after_path_key(url, "genre_name[0]")
    if genre_name:
        return genre_name

    genre_id = extract_value_after_path_key(url, "genre[0]")
    if genre_id:
        if genre_id.isdigit():
            return load_genre_name_map().get(genre_id, f"genre_{genre_id}")
        return genre_id

    for key in ("work_type_category_name[0]", "keyword"):
        value = extract_value_after_path_key(url, key)
        if value:
            return value
    return "未分类"


def make_safe_slug(name):
    slug = re.sub(r'[<>:"/\\|?*#%&+\x00-\x1f]', "_", name).strip()
    slug = re.sub(r"\s+", "_", slug)
    slug = slug.strip(" ._")
    return slug or "uncategorized"


def build_page_template(url):
    """Return a page-format URL and detected start page.

    DLsite search URLs encode filters in path segments. We replace an existing
    /page/N segment with /page/{page}; if it is missing, we insert it before
    /show_type/ when possible, otherwise append it to the end.
    """
    url = url.strip()
    if "{page}" in url:
        return url, 1

    page_match = re.search(r"(/page/)(\d+)(?=/|$)", url)
    if page_match:
        start_page = int(page_match.group(2))
        template = url[:page_match.start(2)] + "{page}" + url[page_match.end(2):]
        return template, start_page

    start_page = 1
    show_type_pos = url.find("/show_type/")
    if show_type_pos != -1:
        return url[:show_type_pos] + "/page/{page}" + url[show_type_pos:], start_page

    sep = "" if url.endswith("/") else "/"
    return f"{url}{sep}page/{{page}}", start_page


def load_crawl_results():
    if not CRAWL_RESULTS_FILE.exists():
        return {"categories": []}

    try:
        with open(CRAWL_RESULTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"categories": []}

    if isinstance(data, dict) and isinstance(data.get("categories"), list):
        return data
    if isinstance(data, list):
        return {"categories": data}
    return {"categories": []}


def unique_ids(ids):
    seen = set()
    result = []
    for work_id in ids:
        if work_id not in seen:
            result.append(work_id)
            seen.add(work_id)
    return result


def load_work_order():
    if not ORDER_FILE.exists():
        return []

    try:
        with open(ORDER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []
    return [work_id for work_id in data if isinstance(work_id, str)]


def save_work_order(new_work_ids):
    previous_work_ids = load_work_order()
    merged_work_ids = unique_ids(new_work_ids + previous_work_ids)

    with open(ORDER_FILE, "w", encoding="utf-8") as f:
        json.dump(merged_work_ids, f, ensure_ascii=False)

    return merged_work_ids, len(previous_work_ids)


def save_crawl_result(category_name, category_slug, source_url, work_ids):
    data = load_crawl_results()
    categories = data.setdefault("categories", [])

    new_work_ids = unique_ids(work_ids)

    entry = {
        "name": category_name,
        "slug": category_slug,
        "source_url": source_url,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "work_ids": new_work_ids,
    }

    for idx, existing in enumerate(categories):
        if existing.get("slug") == category_slug:
            # Keep category membership additive. A work can belong to multiple
            # categories, and partial crawls must not shrink older results.
            merged_work_ids = unique_ids(new_work_ids + existing.get("work_ids", []))
            existing.update(entry)
            existing["work_ids"] = merged_work_ids
            categories[idx] = existing
            break
    else:
        categories.append(entry)

    with open(CRAWL_RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def log_failed_work(work_id, work_name, work_url):
    """Append failed downloads to a markdown log."""
    if not FAILED_LOG.exists():
        with open(FAILED_LOG, "w", encoding="utf-8") as f:
            f.write("# 下载失败的作品列表\n\n")
            f.write("| 作品ID | 作品名称 | 链接 |\n")
            f.write("|------|----------|------|\n")

    with open(FAILED_LOG, "a", encoding="utf-8") as f:
        f.write(f"| {work_id} | {work_name} | {work_url} |\n")


async def download_work(session, work_id, work_name, work_url):
    """Download a single work HTML file."""
    save_path = WORKS_DIR / f"{work_id}.html"
    announce_url = build_announce_url(work_id, work_url)
    use_announce_url = False
    last_url = work_url

    for attempt in range(1, MAX_WORK_RETRIES + 1):
        current_url = announce_url if use_announce_url else work_url
        last_url = current_url

        if use_announce_url:
            print(f"  使用预告页重试: {work_id}")

        html = await download_page(session, current_url)
        valid, reason = is_valid_work_html(html)

        if valid:
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"  已保存: {work_id}.html")
            return True

        print(f"  抓取无效: {work_id} - {reason} ({attempt}/{MAX_WORK_RETRIES})")
        if reason == "没有解析到作品名" and current_url != announce_url:
            use_announce_url = True

        if attempt < MAX_WORK_RETRIES:
            print(f"  {WORK_RETRY_DELAY} 秒后重试: {work_id}")
            await asyncio.sleep(WORK_RETRY_DELAY)

    log_failed_work(work_id, work_name, last_url)
    return False


async def download_works_from_page(session, work_links, page_num):
    """Download all new works from one listing page."""
    works_to_download = []

    for work in work_links:
        save_path = WORKS_DIR / f"{work['id']}.html"
        if is_valid_saved_work_file(save_path):
            print(f"  跳过已存在且有效: {work['id']}.html")
        else:
            if save_path.exists():
                print(f"  已存在但没有作品名，重新下载: {work['id']}.html")
            works_to_download.append(work)

    if not works_to_download:
        print(f"  第 {page_num} 页的作品都已下载过")
        return 0, 0

    print(f"\n  开始下载第 {page_num} 页的 {len(works_to_download)} 个新作品...")
    print(f"  使用 {MAX_CONCURRENT} 个并发连接\n")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def bounded_download(work):
        async with semaphore:
            print(f"  下载中: {work['id']}")
            success = await download_work(session, work["id"], work["name"], work["url"])
            await asyncio.sleep(0.1)
            return success

    results = await asyncio.gather(*[bounded_download(work) for work in works_to_download])
    total_downloaded = sum(results)
    total_failed = len(results) - total_downloaded
    return total_downloaded, total_failed


async def filter_subtitled_audio_asmr_links(session, work_links, subtitle_cache):
    if not work_links:
        return [], 0

    kept = []
    skipped = 0
    semaphore = asyncio.Semaphore(ASMR_SUBTITLE_CONCURRENCY)

    async def check_work(work):
        if not work.get("is_audio_asmr"):
            return work, True
        async with semaphore:
            has_subtitle = await query_asmr_subtitle(session, work["id"], subtitle_cache)
            return work, has_subtitle

    results = await asyncio.gather(*(check_work(work) for work in work_links))
    for work, keep in results:
        if keep:
            kept.append(work)
        else:
            skipped += 1
            print(f"  跳过无字幕音声 ASMR: {work['id']} {work.get('name', '')}")

    if skipped:
        save_asmr_subtitle_cache(subtitle_cache)
    return kept, skipped


async def crawl_search_url(
    session,
    search_url,
    max_pages,
    queue_index=1,
    queue_total=1,
    subtitle_asmr_only=False,
    subtitle_cache=None,
):
    page_template, start_page = build_page_template(search_url)
    category_name = extract_category_name(search_url)
    category_slug = make_safe_slug(category_name)

    print("\n" + "=" * 60)
    if queue_total > 1:
        print(f"队列 {queue_index}/{queue_total}")
    print(f"分类: {category_name}")
    print(f"起始页: {start_page}")
    print(f"最大页数: {'不限制' if max_pages == 0 else max_pages}")
    print(f"只下载有字幕音声 ASMR: {'是' if subtitle_asmr_only else '否'}")
    print(f"URL 模板: {page_template}")

    all_work_ids = []
    page = start_page
    total_downloaded = 0
    total_failed = 0
    total_skipped_no_subtitle = 0

    while True:
        print(f"\n正在处理第 {page} 页...")
        search_page_url = page_template.format(page=page)

        search_html = await download_page(session, search_page_url)
        if not search_html:
            print(f"  无法下载第 {page} 页，停止")
            break

        work_links = extract_work_links(search_html, search_page_url)
        if not work_links:
            print(f"  第 {page} 页没有找到作品链接，停止")
            break

        print(f"  第 {page} 页找到 {len(work_links)} 个作品")

        if subtitle_asmr_only:
            work_links, skipped_no_subtitle = await filter_subtitled_audio_asmr_links(
                session,
                work_links,
                subtitle_cache if subtitle_cache is not None else {},
            )
            total_skipped_no_subtitle += skipped_no_subtitle
            print(f"  字幕筛选后保留 {len(work_links)} 个作品")

        for work in work_links:
            all_work_ids.append(work["id"])

        if work_links:
            downloaded, failed = await download_works_from_page(session, work_links, page)
            total_downloaded += downloaded
            total_failed += failed
        else:
            print(f"  第 {page} 页没有符合字幕筛选条件的作品")

        pages_done = page - start_page + 1
        if max_pages > 0 and pages_done >= max_pages:
            print(f"\n已达到最大爬取页数限制: {max_pages}")
            break

        if not has_next_page(search_html, page):
            print("\n没有更多页面了")
            break

        page += 1
        print("\n  等待 0.1 秒后继续下一页...")
        await asyncio.sleep(0.1)

    print("\n分类爬取完成:")
    print(f"  - 分类: {category_name}")
    print(f"  - 新下载: {total_downloaded} 个作品")
    if total_skipped_no_subtitle > 0:
        print(f"  - 跳过无字幕音声 ASMR: {total_skipped_no_subtitle} 个作品")
    if total_failed > 0:
        print(f"  - 下载失败: {total_failed} 个作品 (详情查看 failed_works.md)")

    if all_work_ids:
        save_crawl_result(category_name, category_slug, search_url, all_work_ids)
        print(f"  - 已记录分类结果: {CRAWL_RESULTS_FILE} -> {category_name} ({len(unique_ids(all_work_ids))} 个作品)")
    else:
        print("  - 没有抓到作品 ID，跳过分类记录")

    return {
        "category_name": category_name,
        "work_ids": all_work_ids,
        "downloaded": total_downloaded,
        "failed": total_failed,
        "skipped_no_subtitle": total_skipped_no_subtitle,
    }


async def main():
    cli_urls, cli_max_pages, cli_subtitle_asmr_only = parse_cli_queue_args()
    search_urls = cli_urls if cli_urls is not None else prompt_search_urls()
    max_pages = cli_max_pages if cli_max_pages is not None else prompt_max_pages()
    subtitle_asmr_only = (
        cli_subtitle_asmr_only
        if cli_subtitle_asmr_only is not None
        else prompt_subtitle_asmr_only()
    )

    save_url_history(search_urls)

    WORKS_DIR.mkdir(exist_ok=True)

    print("开始爬取 DLsite 作品...")
    print("=" * 60)
    print(f"队列链接数: {len(search_urls)}")
    print(f"每个链接最大页数: {'不限制' if max_pages == 0 else max_pages}")
    print(f"只下载有字幕音声 ASMR: {'是' if subtitle_asmr_only else '否'}")

    queue_work_ids = []
    total_downloaded = 0
    total_failed = 0
    total_skipped_no_subtitle = 0
    subtitle_cache = load_asmr_subtitle_cache() if subtitle_asmr_only else {}

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        for index, search_url in enumerate(search_urls, start=1):
            result = await crawl_search_url(
                session,
                search_url,
                max_pages,
                index,
                len(search_urls),
                subtitle_asmr_only=subtitle_asmr_only,
                subtitle_cache=subtitle_cache,
            )
            queue_work_ids.extend(result["work_ids"])
            total_downloaded += result["downloaded"]
            total_failed += result["failed"]
            total_skipped_no_subtitle += result.get("skipped_no_subtitle", 0)

    if subtitle_asmr_only:
        save_asmr_subtitle_cache(subtitle_cache)

    print("\n" + "=" * 60)
    print("队列爬取完成:")
    print(f"  - 新下载: {total_downloaded} 个作品")
    if total_skipped_no_subtitle > 0:
        print(f"  - 跳过无字幕音声 ASMR: {total_skipped_no_subtitle} 个作品")
    if total_failed > 0:
        print(f"  - 下载失败: {total_failed} 个作品 (详情查看 failed_works.md)")

    ordered_work_ids = unique_ids(queue_work_ids)
    if not ordered_work_ids:
        print("  - 没有抓到作品 ID，保留现有排序和分类记录")
        return

    merged_work_ids, previous_count = save_work_order(ordered_work_ids)
    print(
        f"  - 已合并保存队列排序: {ORDER_FILE} "
        f"(本次 {len(ordered_work_ids)} 个，原有 {previous_count} 个，合并后 {len(merged_work_ids)} 个)"
    )


if __name__ == "__main__":
    try:
        import aiohttp
    except ImportError:
        print("需要安装 aiohttp，请运行: pip install aiohttp")
        sys.exit(1)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())
