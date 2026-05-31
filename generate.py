import re
import sys
import json
import math
import hashlib
import asyncio
import aiohttp
import html as html_lib
import shutil
import time
import unicodedata
import os
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:
    Style = None
    lazy_pinyin = None


BASE_DIR = Path(__file__).parent  # 项目根目录
WORKS_DIR = BASE_DIR / "works"  # 作品HTML文件存储目录
ORDER_FILE = BASE_DIR / "works_order.json"  # 作品人气排序文件
CRAWL_RESULTS_FILE = BASE_DIR / "crawl_results.json"  # 爬取的分类和作品关系文件
OUTPUT_DIR = BASE_DIR / "output"  # 输出目录
IMAGES_DIR = OUTPUT_DIR / "images"  # 图片存储目录
DATA_DIR = OUTPUT_DIR / "data"  # 数据文件目录
JSON_DIR = DATA_DIR / "json"  # 作品JSON数据目录
TRANSLATE_DIR = DATA_DIR / "translate"  # 翻译相关数据目录
PENDING_TRANSLATE_DIR = BASE_DIR / "待翻译"  # 待翻译稿目录
DONE_TRANSLATE_DIR = TRANSLATE_DIR / "已翻译"  # 已归档的待翻译原稿目录
TRANSLATED_DRAFT_DIR = BASE_DIR / "翻译稿"  # 翻译好的稿件目录
ORIG_DIR = DATA_DIR / "orig"  # 原文对照文件目录
CATEGORIES_FILE = DATA_DIR / "categories.json"  # 分类配置文件
SEARCH_INDEX_FILE = DATA_DIR / "search_index.json"  # 搜索索引文件
FILTER_INDEX_DIR = DATA_DIR / "filter_index"  # 筛选索引目录
ASMR_SUBTITLE_CACHE_FILE = BASE_DIR / "asmr_subtitle_cache.json"  # ASMR字幕缓存文件
SLIDER_IMAGES_DIR = IMAGES_DIR / "slider"  # 轮播图存储目录
PARTS_IMAGES_DIR = IMAGES_DIR / "parts"  # 内容图存储目录
MAX_CONCURRENT_IMAGES = 300  # 最大并发下载图片数量
MAX_PARSE_WORKERS = min(12, max(4, (os.cpu_count() or 4)))  # HTML解析线程池大小
PARSE_PROGRESS_INTERVAL = 500  # 解析HTML进度显示间隔（每处理多少个作品显示一次）
PAGE_WRITE_LOG_INTERVAL = 100  # 分页JSON写入日志间隔（每写入多少个分页显示一次）
IMAGE_SCAN_PROGRESS_INTERVAL = 1000  # 扫描作品图片进度显示间隔
IMAGE_INDEX_PROGRESS_INTERVAL = 20000  # 图片索引进度显示间隔
ASMR_SUBTITLE_REFRESH = "--refresh-asmr-subtitles" in sys.argv  # 是否刷新ASMR字幕类型
ASMR_SUBTITLE_CONCURRENCY = 2  # ASMR字幕查询并发数
ASMR_SUBTITLE_MAX_RETRIES = 8  # ASMR字幕查询最大重试次数
ASMR_SUBTITLE_RETRY_DELAY = 3  # ASMR字幕查询重试间隔（秒）
ASMR_SUBTITLE_API_TEMPLATE = (
    "https://api.asmr-200.com/api/search/{work_id}"
    "?order=create_date&sort=desc&page=1&pageSize=20"
    "&subtitle=1&includeTranslationWorks=true"
)
WORK_FILE_PATTERNS = ("RJ*.html", "VJ*.html")
_GENRE_NAME_MAP = None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.dlsite.com/"
}

CJK_CHAR_RE = re.compile(r"[\u3400-\u9fff]")
CJK_SEGMENT_RE = re.compile(r"[\u3400-\u9fff]+")


class DownloadProgressBar:
    def __init__(self, total, desc="下载", bar_width=40):
        self.total = total
        self.desc = desc
        self.bar_width = bar_width
        self.count = 0
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.start_time = time.time()
        self.last_update = 0

    def update(self, n=1, success=True, skipped=False):
        if skipped:
            self.skipped += n
        elif success:
            self.success += n
        else:
            self.failed += n
        self.count += n
        self._display()

    def _display(self):
        elapsed = time.time() - self.start_time
        if self.count > 0:
            speed = self.count / elapsed if elapsed > 0 else 0
            eta = (self.total - self.count) / speed if speed > 0 else 0
        else:
            speed = 0
            eta = 0

        percent = self.count / self.total if self.total > 0 else 0
        filled = int(self.bar_width * percent)
        bar = "█" * filled + "░" * (self.bar_width - filled)

        status = f"✓{self.success}"
        if self.failed > 0:
            status += f" ✗{self.failed}"
        if self.skipped > 0:
            status += f" ⊘{self.skipped}"

        line = (
            f"\r{self.desc}: |{bar}| {self.count}/{self.total} "
            f"({percent*100:.1f}%) [{status}] "
            f"{speed:.1f}img/s ETA:{self._format_time(eta)}"
        )
        sys.stdout.write(line)
        sys.stdout.flush()

    def _format_time(self, seconds):
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            m, s = divmod(int(seconds), 60)
            return f"{m}m{s}s"
        else:
            h, m = divmod(int(seconds) // 60, 60)
            return f"{h}h{m}m"

    def close(self):
        elapsed = time.time() - self.start_time
        avg_speed = self.count / elapsed if elapsed > 0 else 0
        print(
            f"\n完成: {self.count}/{self.total} "
            f"[✓{self.success} ✗{self.failed} ⊘{self.skipped}] "
            f"耗时{elapsed:.1f}s 均速{avg_speed:.1f}img/s"
        )


async def download_image(session, url, save_path, max_retries=3):
    """异步下载单张图片，失败自动重试，返回 (local_path, success)"""
    if save_path.exists():
        return (str(save_path.relative_to(OUTPUT_DIR)).replace("\\", "/"), True)
    
    for attempt in range(max_retries + 1):
        try:
            async with session.get(url, timeout=30) as resp:
                data = await resp.read()
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(data)
            return (str(save_path.relative_to(OUTPUT_DIR)).replace("\\", "/"), True)
        except Exception as e:
            if attempt >= max_retries:
                return (url, False)


def get_image_filename(url, product_id, index=0, prefix=""):
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    ext = Path(url.split("?")[0]).suffix or ".jpg"
    if prefix:
        return f"{product_id}_{prefix}_{index}_{url_hash}{ext}"
    return f"{product_id}_{index}_{url_hash}{ext}"


def output_relative_path(path):
    return str(path.relative_to(OUTPUT_DIR)).replace("\\", "/")


def get_image_cache_key(url):
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    ext = (Path(url.split("?")[0]).suffix or ".jpg").lower()
    return url_hash, ext


def build_reusable_image_index(image_dir, prefix):
    image_index = {}
    if not image_dir.exists():
        return image_index

    pattern = re.compile(
        rf"_{re.escape(prefix)}_\d+_([0-9a-f]{{8}})(\.[^.]+)$",
        re.IGNORECASE,
    )
    scanned = 0
    for path in image_dir.iterdir():
        scanned += 1
        if not path.is_file():
            continue
        match = pattern.search(path.name)
        if not match:
            continue
        key = (match.group(1).lower(), match.group(2).lower())
        image_index.setdefault(key, output_relative_path(path))
        if scanned % IMAGE_INDEX_PROGRESS_INTERVAL == 0:
            sys.stdout.write(
                f"\r建立 {prefix} 图片索引: 已扫描 {scanned} 个文件，索引 {len(image_index)} 张"
            )
            sys.stdout.flush()
    if scanned:
        sys.stdout.write(
            f"\r建立 {prefix} 图片索引: 已扫描 {scanned} 个文件，索引 {len(image_index)} 张\n"
        )
        sys.stdout.flush()
    return image_index


def resolve_existing_image_path(url, product_id, index, prefix, image_dir, reusable_index):
    fname = get_image_filename(url, product_id, index, prefix)
    expected_path = image_dir / fname
    if expected_path.exists():
        return output_relative_path(expected_path)

    return reusable_index.get(get_image_cache_key(url), "")


def clean_html_text(value):
    value = re.sub(r'<br\s*/?>', '\n', value, flags=re.IGNORECASE)
    value = re.sub(r'<[^>]+>', '', value)
    value = html_lib.unescape(value)
    value = value.replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', value).strip()


def split_outline_value(value):
    value = clean_html_text(value).strip(" /")
    if not value:
        return []
    return [part.strip() for part in re.split(r'\s*/\s*', value) if part.strip()]


def unique_values(values):
    seen = set()
    result = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def extract_work_outline_rows(content):
    table_match = re.search(
        r'<table[^>]*id=["\']work_outline["\'][^>]*>(.*?)</table>',
        content,
        re.DOTALL | re.IGNORECASE,
    )
    if not table_match:
        return {}

    rows = {}
    for row_match in re.finditer(r'<tr[^>]*>(.*?)</tr>', table_match.group(1), re.DOTALL | re.IGNORECASE):
        row_html = row_match.group(1)
        header_match = re.search(r'<th[^>]*>(.*?)</th>', row_html, re.DOTALL | re.IGNORECASE)
        cell_match = re.search(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL | re.IGNORECASE)
        if not header_match or not cell_match:
            continue

        header = clean_html_text(header_match.group(1))
        if header:
            rows[header] = cell_match.group(1)

    return rows


def extract_outline_values(outline_rows, headers):
    values = []
    for header in headers:
        cell_html = outline_rows.get(header)
        if not cell_html:
            continue

        cell_values = []
        for span_match in re.finditer(r'<span\b([^>]*)>(.*?)</span>', cell_html, re.DOTALL | re.IGNORECASE):
            attrs = span_match.group(1)
            title_match = re.search(r'title=["\']([^"\']+)["\']', attrs, re.DOTALL | re.IGNORECASE)
            raw_value = title_match.group(1) if title_match else span_match.group(2)
            cell_values.extend(split_outline_value(raw_value))

        for info_match in re.finditer(
            r'<div[^>]*class=["\'][^"\']*additional_info[^"\']*["\'][^>]*>(.*?)</div>',
            cell_html,
            re.DOTALL | re.IGNORECASE,
        ):
            cell_values.extend(split_outline_value(info_match.group(1)))

        if not cell_values:
            cell_values.extend(split_outline_value(cell_html))

        values.extend(cell_values)

    return unique_values(values)


WORK_KIND_AUDIO_ASMR = "音声・ASMR"
WORK_KIND_SUBTITLED_ASMR = "有字幕ASMR"
WORK_KIND_UNSUBTITLED_ASMR = "无字幕ASMR"
WORK_KIND_MANGA = "漫画"
WORK_KIND_GAME = "游戏"
VERSION_PRIORITY = {
    "CHI_HANS": 0,
    "CHI_HANT": 1,
}
PLATFORM_VERSION_PRIORITY = {
    "PC": 0,
    "WINDOWS": 0,
    "MAC": 0,
    "ANDROID": 2,
    "IOS": 2,
}


def get_work_kind(work_types):
    for work_type in work_types:
        normalized = re.sub(r'\s+', '', work_type)
        if "ASMR" in normalized.upper() or normalized in ("ボイス", "音声", "音声・ASMR"):
            return WORK_KIND_AUDIO_ASMR
        if any(keyword in normalized for keyword in ("マンガ", "漫画", "コミック")):
            return WORK_KIND_MANGA
    return WORK_KIND_GAME


def is_audio_asmr_kind(work_kind):
    return work_kind in (WORK_KIND_AUDIO_ASMR, WORK_KIND_SUBTITLED_ASMR, WORK_KIND_UNSUBTITLED_ASMR)


def normalize_image_url(src):
    src = html_lib.unescape(str(src or "")).replace("\\/", "/").strip()
    if src.startswith("//"):
        return "https:" + src
    return src


def add_unique_image(images, src):
    src = normalize_image_url(src)
    if not src or "img.dlsite.jp/" not in src or src in images:
        return
    images.append(src)


def extract_language_editions(content):
    if "data-language-editions" not in content:
        return []

    match = re.search(r"data-language-editions='([^']*)'", content, re.DOTALL)
    if not match:
        return []

    raw = html_lib.unescape(match.group(1))
    try:
        editions = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return editions if isinstance(editions, list) else []


def get_current_language_edition(editions, product_id):
    product_id = product_id.upper()
    for edition in editions:
        if not isinstance(edition, dict):
            continue
        if str(edition.get("workno", "")).upper() == product_id:
            return edition
    return {}


def infer_language_from_name(work_name):
    if "簡体中文" in work_name or "简体中文" in work_name:
        return "CHI_HANS"
    if "繁体中文" in work_name or "繁體中文" in work_name:
        return "CHI_HANT"
    return ""


def infer_platform_from_label(label):
    normalized = clean_html_text(label).upper().replace(" ", "")
    if "ANDROID" in normalized:
        return "ANDROID"
    if "IOS" in normalized or "IPHONE" in normalized or "IPAD" in normalized:
        return "IOS"
    if "PC" in normalized or "WINDOWS" in normalized or "MAC" in normalized:
        return "PC"
    return ""


def infer_platform_from_name(work_name):
    normalized = clean_html_text(work_name).upper().replace(" ", "")
    if "ANDROID" in normalized:
        return "ANDROID"
    if "IOS" in normalized or "IPHONE" in normalized or "IPAD" in normalized:
        return "IOS"
    if "PC" in normalized or "WINDOWS" in normalized or "MAC" in normalized:
        return "PC"
    return ""


def extract_linked_version_entries(content):
    section_match = re.search(
        r'<ul\s+class="work_edition"[^>]*>(.*?)</ul>',
        content,
        re.DOTALL | re.IGNORECASE,
    )
    if not section_match:
        return []

    entries = []
    section_html = section_match.group(1)
    for match in re.finditer(
        r'<a[^>]+href="[^"]*/product_id/((?:RJ|VJ)\d+)\.html"[^>]*class="[^"]*work_edition_linklist_item[^"]*"[^>]*>.*?<dt>(.*?)</dt>',
        section_html,
        re.DOTALL | re.IGNORECASE,
    ):
        workno = match.group(1).upper()
        label = clean_html_text(match.group(2))
        entries.append({
            "workno": workno,
            "display_label": label,
            "platform": infer_platform_from_label(label),
        })
    return entries


def merge_version_entries(product_id, work_name, language_editions, linked_version_entries):
    merged = {}

    def get_or_create_entry(workno):
        workno = str(workno or "").upper()
        if not workno:
            return None
        if workno not in merged:
            merged[workno] = {
                "workno": workno,
                "edition_id": None,
                "lang": "",
                "display_label": "",
                "platform": "",
                "platform_label": "",
            }
        return merged[workno]

    for edition in language_editions:
        if not isinstance(edition, dict):
            continue
        entry = get_or_create_entry(edition.get("workno"))
        if not entry:
            continue
        entry["edition_id"] = edition.get("edition_id")
        entry["lang"] = str(edition.get("lang") or "").upper()
        entry["display_label"] = str(edition.get("display_label") or "")

    for linked_entry in linked_version_entries:
        if not isinstance(linked_entry, dict):
            continue
        entry = get_or_create_entry(linked_entry.get("workno"))
        if not entry:
            continue
        entry["platform"] = linked_entry.get("platform", "")
        entry["platform_label"] = linked_entry.get("display_label", "")

    current_entry = get_or_create_entry(product_id)
    current_entry["lang"] = current_entry.get("lang") or infer_language_from_name(work_name)
    current_entry["platform"] = current_entry.get("platform") or infer_platform_from_name(work_name)
    return list(merged.values())


def compute_version_rank(language, platform):
    language_rank = VERSION_PRIORITY.get(language, 2)
    platform_rank = PLATFORM_VERSION_PRIORITY.get(platform, 0)
    return language_rank * 10 + platform_rank


def build_version_info(product_id, work_name, editions, linked_version_entries):
    merged_entries = merge_version_entries(product_id, work_name, editions, linked_version_entries)
    current_edition = get_current_language_edition(merged_entries, product_id)
    version_ids = unique_values([
        str(entry.get("workno", "")).upper()
        for entry in merged_entries
        if str(entry.get("workno", "")).strip()
    ])

    edition_id = current_edition.get("edition_id")
    if edition_id is None:
        for edition in merged_entries:
            if isinstance(edition, dict) and edition.get("edition_id") is not None:
                edition_id = edition.get("edition_id")
                break

    if len(version_ids) > 1:
        version_group_id = f"edition:{edition_id}" if edition_id is not None else "versions:" + ",".join(sorted(version_ids))
    else:
        version_group_id = product_id.upper()

    language = str(current_edition.get("lang") or infer_language_from_name(work_name)).upper()
    platform = str(current_edition.get("platform") or infer_platform_from_name(work_name)).upper()
    return {
        "version_group_id": version_group_id,
        "version_lang": language,
        "version_label": current_edition.get("display_label", ""),
        "version_platform": platform,
        "version_platform_label": current_edition.get("platform_label", ""),
        "version_ids": version_ids,
        "version_rank": compute_version_rank(language, platform),
    }


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
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
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


async def query_asmr_subtitle(session, work_id, cache, refresh_missing=False):
    work_id = work_id.upper()
    if not work_id.startswith("RJ"):
        return False

    cached = get_cached_asmr_subtitle(cache, work_id)
    if cached is not None:
        return cached
    if not refresh_missing:
        return None

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
                    return None
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
            return None

    return None


async def refine_asmr_work_kinds(session, works, refresh_missing=False):
    audio_works = [work for work in works if work.get("work_kind") == WORK_KIND_AUDIO_ASMR]
    if not audio_works:
        return

    cache = load_asmr_subtitle_cache()
    semaphore = asyncio.Semaphore(ASMR_SUBTITLE_CONCURRENCY)
    completed = 0
    subtitled = 0
    unsubtitled = 0
    unknown = 0

    async def refine_one(work):
        nonlocal completed, subtitled, unsubtitled, unknown
        async with semaphore:
            has_subtitle = await query_asmr_subtitle(
                session,
                work["product_id"],
                cache,
                refresh_missing=refresh_missing,
            )
            if has_subtitle is True:
                work["work_kind"] = WORK_KIND_SUBTITLED_ASMR
                subtitled += 1
            elif has_subtitle is False:
                work["work_kind"] = WORK_KIND_UNSUBTITLED_ASMR
                unsubtitled += 1
            else:
                unknown += 1
            completed += 1
            if completed % 50 == 0 or completed == len(audio_works):
                save_asmr_subtitle_cache(cache)
                print(
                    f"ASMR 字幕类型处理: {completed}/{len(audio_works)} "
                    f"(有字幕 {subtitled}, 无字幕 {unsubtitled}, 未确认 {unknown})"
                )

    print(
        f"开始细化音声 ASMR 作品类型: {len(audio_works)} 个 "
        f"({'查询缺失缓存' if refresh_missing else '仅使用缓存'})"
    )
    await asyncio.gather(*(refine_one(work) for work in audio_works))
    save_asmr_subtitle_cache(cache)


def parse_html_file(filepath):
    product_id = Path(filepath).stem
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    work_name_match = re.search(r'<h1[^>]*id="work_name"[^>]*>(.*?)</h1>', content, re.DOTALL)
    work_name = work_name_match.group(1).strip() if work_name_match else ""

    maker_name_match = re.search(r'class="maker_name"[^>]*>\s*<a[^>]*>(.*?)</a>', content, re.DOTALL)
    maker_name = maker_name_match.group(1).strip() if maker_name_match else ""

    desc_match = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', content)
    description = desc_match.group(1) if desc_match else ""

    outline_rows = extract_work_outline_rows(content)
    work_types = extract_outline_values(outline_rows, ("作品形式",))
    work_kind = get_work_kind(work_types)
    language_editions = extract_language_editions(content)
    linked_version_entries = extract_linked_version_entries(content)
    version_info = build_version_info(product_id, work_name, language_editions, linked_version_entries)

    slider_images = []
    slider_block = re.search(r'class="product-slider-data">(.*?)</div>\s*<div\s+class="work_slider', content, re.DOTALL)
    if slider_block:
        for m in re.finditer(r'data-src="(//img\.dlsite\.jp/[^"]+)"', slider_block.group(1)):
            add_unique_image(slider_images, m.group(1))

    if not slider_images:
        fallback_patterns = (
            r'<translation-product-slider\b[^>]*\bsrc="([^"]+)"',
            r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
            r'<meta\s+name=["\']twitter:image:src["\']\s+content=["\']([^"\']+)["\']',
            r'<meta\s+itemprop=["\']image["\']\s+content=["\']([^"\']+)["\']',
            r'"image_main"\s*:\s*"([^"]+)"',
        )
        for pattern in fallback_patterns:
            image_match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
            if image_match:
                add_unique_image(slider_images, image_match.group(1))
                if slider_images:
                    break

    parts = []
    spec_pos = content.find('<!-- spec -->')
    if spec_pos == -1:
        spec_pos = content.find('<div id="intro-title"')
    parts_start = content.find('class="work_parts_container"')
    if parts_start != -1 and spec_pos != -1 and spec_pos > parts_start:
        parts_html = content[parts_start:spec_pos]

        for block_match in re.finditer(
            r'<div\s+class="work_parts\s+type_(text|image|multiimages)"[^>]*>(.*?)(?=<div\s+class="work_parts\s+type_|</div>\s*</div>\s*</div>\s*</div>|$)',
            parts_html, re.DOTALL
        ):
            part_type = block_match.group(1)
            block_content = block_match.group(2)

            heading_match = re.search(r'<h3[^>]*class="work_parts_heading"[^>]*>(.*?)</h3>', block_content, re.DOTALL)
            heading = heading_match.group(1).strip() if heading_match else ""

            if part_type == "text":
                text_area = re.search(r'<div\s+class="work_parts_area"[^>]*>(.*?)</div>', block_content, re.DOTALL)
                if text_area:
                    p_match = re.search(r'<p>(.*?)</p>', text_area.group(1), re.DOTALL)
                    if p_match:
                        text = p_match.group(1)
                        text = re.sub(r'<br\s*/?>', '\n', text)
                        text = re.sub(r'<[^>]+>', '', text)
                        text = re.sub(r'&lt;', '<', text)
                        text = re.sub(r'&gt;', '>', text)
                        text = re.sub(r'&amp;', '&', text)
                        text = re.sub(r'&nbsp;', ' ', text)
                        text = text.strip()
                        if text:
                            parts.append({
                                "type": "text",
                                "heading": heading,
                                "content": text
                            })
            elif part_type == "image":
                img_match = re.search(r'<img\s+src="([^"]+)"', block_content)
                if img_match:
                    img_src = img_match.group(1)
                    if img_src.startswith("//"):
                        img_src = "https:" + img_src
                    parts.append({
                        "type": "image",
                        "heading": heading,
                        "src": img_src
                    })
            elif part_type == "multiimages":
                img_matches = re.finditer(r'<img\s+src="([^"]+)"', block_content)
                for im in img_matches:
                    img_src = im.group(1)
                    if img_src.startswith("//"):
                        img_src = "https:" + img_src
                    parts.append({
                        "type": "image",
                        "heading": heading,
                        "src": img_src
                    })

    return {
        "product_id": product_id,
        "work_name": work_name,
        "maker_name": maker_name,
        "description": description,
        "work_types": work_types,
        "work_kind": work_kind,
        "slider_images": slider_images,
        "parts": parts,
        **version_info,
    }


def share_version_group_images(works):
    best_images_by_group = {}
    for work in works:
        group_id = work.get("version_group_id") or work["product_id"]
        images = work.get("slider_images") or []
        if not images:
            continue
        current_best = best_images_by_group.get(group_id, [])
        if len(images) > len(current_best):
            best_images_by_group[group_id] = images

    filled = 0
    for work in works:
        group_id = work.get("version_group_id") or work["product_id"]
        best_images = best_images_by_group.get(group_id)
        if best_images and len(work.get("slider_images") or []) < len(best_images):
            work["slider_images"] = list(best_images)
            filled += 1

    if filled:
        print(f"已为 {filled} 个多版本作品复用同组封面/样品图")


def format_seconds(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m"


def render_parse_progress(done, total, started_at, failed=0, force=False):
    if not force and done % PARSE_PROGRESS_INTERVAL != 0 and done != total:
        return

    elapsed = max(0.001, time.time() - started_at)
    speed = done / elapsed
    remaining = max(0, total - done)
    eta = remaining / speed if speed > 0 else 0
    failed_text = f" 失败:{failed}" if failed else ""
    line = (
        f"\r解析HTML: {done}/{total} "
        f"{done / max(1, total) * 100:5.1f}% "
        f"{speed:.1f}/s ETA:{format_seconds(eta)}{failed_text}"
    )
    sys.stdout.write(line)
    sys.stdout.flush()


def parse_html_files(html_files):
    total = len(html_files)
    workers = min(MAX_PARSE_WORKERS, max(1, total))
    print(f"开始解析 HTML: {total} 个，{workers} 个线程")

    works = [None] * total
    failed = 0
    started_at = time.time()
    render_parse_progress(0, total, started_at, force=True)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(parse_html_file, path): (idx, path)
            for idx, path in enumerate(html_files)
        }
        for done, future in enumerate(as_completed(future_map), start=1):
            idx, path = future_map[future]
            try:
                works[idx] = future.result()
            except Exception as e:
                failed += 1
                sys.stdout.write("\n")
                print(f"  跳过解析失败: {path.name} - {e}")
            render_parse_progress(done, total, started_at, failed)

    sys.stdout.write("\n")
    parsed_works = [work for work in works if work is not None]
    print(
        f"HTML 解析完成: {len(parsed_works)}/{total} 个，"
        f"耗时 {format_seconds(time.time() - started_at)}"
    )
    return parsed_works


def clean_description(text):
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'「DLsite[^」]*」[^「]*「DLsite[^」]*」！?', '', text)
    text = text.strip()
    return text


def generate_translate_md(works):
    md = ""
    for work in works:
        md += f"## {work['product_id']}\n\n"
        md += f"### 作品名称\n\n"
        md += f"- **[译文]**: {work['work_name']}\n"
        md += f"- **社团**: {work['maker_name']}\n\n"
        md += f"### 简介\n\n"
        md += f"**[简介译文]**: {work['description_clean']}\n\n"
        
        part_idx = 0
        for pi, part in enumerate(work['parts']):
            if part['type'] == 'text':
                heading = part['heading'] if part['heading'] else f"段落{part_idx + 1}"
                md += f"### {heading}\n\n"
                md += f"**[译文]**: {part['content']}\n\n"
                part_idx += 1
    return md


def generate_orig_md(works):
    md = ""
    for work in works:
        md += f"## {work['product_id']}\n\n"
        md += f"### 作品名称\n\n"
        md += f"- **原文**: {work['work_name']}\n"
        md += f"- **社团**: {work['maker_name']}\n\n"
        md += f"### 简介\n\n"
        md += f"{work['description_clean']}\n\n"
        
        part_idx = 0
        for pi, part in enumerate(work['parts']):
            if part['type'] == 'text':
                heading = part['heading'] if part['heading'] else f"段落{part_idx + 1}"
                md += f"### {heading}\n\n"
                md += f"{part['content']}\n\n"
                part_idx += 1
    return md


def escape_html(text):
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    text = text.replace('"', '&quot;')
    text = text.replace("'", '&#39;')
    return text


ITEMS_PER_PAGE = 12

def get_style_variant(style_key):
    return STYLE_VARIANT_BY_KEY.get(style_key, STYLE_VARIANTS[0])


def get_style_extra_css(style_key):
    return STYLE_EXTRA_CSS.get(style_key, "")


def build_style_switcher_html(active_key):
    links = []
    for variant in STYLE_VARIANTS:
        classes = "style-link active" if variant["key"] == active_key else "style-link"
        links.append(
            f'<a class="{classes}" href="{escape_html(variant["file"])}">{escape_html(variant["name"])}</a>'
        )
    return "\n                ".join(links)


STYLE_VARIANTS = [
    {
        "key": "classic",
        "name": "经典卡片",
        "file": "index.html",
        "body_class": "ui-classic",
        "layout": "classic",
    },
    {
        "key": "gallery",
        "name": "沉浸封面墙",
        "file": "index_gallery.html",
        "body_class": "ui-gallery",
        "layout": "gallery",
    },
    {
        "key": "dashboard",
        "name": "数据工作台",
        "file": "index_dashboard.html",
        "body_class": "ui-dashboard",
        "layout": "dashboard",
    },
    {
        "key": "compact",
        "name": "紧凑片库",
        "file": "index_compact.html",
        "body_class": "ui-compact",
        "layout": "compact",
    },
    {
        "key": "magazine",
        "name": "杂志特写",
        "file": "index_magazine.html",
        "body_class": "ui-magazine",
        "layout": "magazine",
    },
]

STYLE_VARIANT_BY_KEY = {variant["key"]: variant for variant in STYLE_VARIANTS}

STYLE_EXTRA_CSS = {
    "classic": """
        body.ui-classic .style-link[href="index.html"] {
            background: linear-gradient(135deg, #11998e, #38ef7d);
            color: #fff;
        }
    """,
    "gallery": """
        body.ui-gallery {
            background: #11131a;
            padding: 22px;
        }
        body.ui-gallery .container { max-width: 1720px; }
        body.ui-gallery .top-bar {
            min-height: 88px;
            align-items: flex-start;
            justify-content: flex-start;
            padding: 16px 460px 14px 18px;
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 22px;
            background: rgba(255,255,255,0.06);
            box-shadow: 0 24px 70px rgba(0,0,0,0.32);
            backdrop-filter: blur(18px);
        }
        body.ui-gallery h1 {
            text-align: left;
            font-size: 2.2em;
            letter-spacing: 0;
            text-shadow: none;
        }
        body.ui-gallery .style-switcher {
            position: relative;
            left: auto;
            top: auto;
            transform: none;
            max-width: 740px;
            margin-left: 24px;
            margin-top: 4px;
        }
        body.ui-gallery .search-box {
            width: min(420px, 28vw);
            top: 22px;
            transform: none;
        }
        body.ui-gallery .category-bar {
            justify-content: flex-start;
            margin: 18px 0 22px;
        }
        body.ui-gallery .works-grid {
            grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
            grid-auto-flow: dense;
            gap: 18px;
        }
        body.ui-gallery .work-card {
            position: relative;
            border-radius: 18px;
            background: #161a22;
            color: #f4f7fb;
            overflow: hidden;
            min-height: 520px;
            box-shadow: 0 22px 58px rgba(0,0,0,0.34);
        }
        body.ui-gallery .work-card:nth-child(5n + 1) {
            grid-row: span 2;
            min-height: 680px;
        }
        body.ui-gallery .work-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 30px 78px rgba(0,0,0,0.48);
        }
        body.ui-gallery .image-carousel {
            height: 360px;
            background: #0b0d12;
        }
        body.ui-gallery .work-card:nth-child(5n + 1) .image-carousel { height: 500px; }
        body.ui-gallery .carousel-slide img {
            width: 100%;
            height: 100%;
            max-width: none;
            max-height: none;
            object-fit: contain;
            object-position: center;
        }
        body.ui-gallery .work-content {
            position: absolute;
            left: 0;
            right: 0;
            bottom: 0;
            padding: 76px 18px 18px;
            color: #fff;
            background: linear-gradient(180deg, transparent 0%, rgba(10,12,18,0.84) 28%, rgba(10,12,18,0.96) 100%);
        }
        body.ui-gallery .name-section,
        body.ui-gallery .description-section,
        body.ui-gallery .intro-section {
            border: 0;
            margin: 0;
            padding: 0;
            background: transparent;
            max-height: none;
        }
        body.ui-gallery .maker-name,
        body.ui-gallery .work-kind-row,
        body.ui-gallery .description-text,
        body.ui-gallery .intro-content,
        body.ui-gallery .name-translated-text {
            color: rgba(255,255,255,0.78);
        }
        body.ui-gallery .name-row {
            display: block;
            margin-bottom: 10px;
        }
        body.ui-gallery .name-label { display: none; }
        body.ui-gallery .name-text {
            display: block;
            color: #fff;
            font-size: 1.16em;
            line-height: 1.35;
        }
        body.ui-gallery .name-translated-text {
            display: block;
            border: 0;
            font-size: 0.96em;
            margin-top: 4px;
        }
        body.ui-gallery .copy-btn { display: none; }
        body.ui-gallery .section-title { display: none; }
        body.ui-gallery .description-text {
            display: block;
            overflow: visible;
            font-size: 0.88em;
            line-height: 1.6;
        }
        body.ui-gallery .intro-section { display: block; }
        body.ui-gallery .scroll-reveal {
            max-height: 232px;
            overflow: auto;
            padding-right: 8px;
            margin-top: 10px;
            scrollbar-width: thin;
            scrollbar-color: rgba(255,255,255,0.4) rgba(255,255,255,0.08);
        }
        body.ui-gallery .scroll-reveal .description-section,
        body.ui-gallery .scroll-reveal .intro-section {
            display: block;
            background: transparent;
        }
        body.ui-gallery .scroll-reveal .description-text {
            display: block;
            overflow: visible;
        }
        body.ui-gallery .kind-pill {
            color: #03110d;
            background: #9af4c6;
            border: 0;
        }
        body.ui-gallery .state-btn,
        body.ui-gallery .asmr-link-btn {
            color: #fff;
            background: rgba(255,255,255,0.12);
            border-color: rgba(255,255,255,0.2);
            box-shadow: none;
        }
        body.ui-gallery .scroll-reveal {
            max-height: 210px;
            overflow: auto;
            padding-right: 8px;
            margin-top: 10px;
            scrollbar-width: thin;
            scrollbar-color: rgba(255,255,255,0.4) rgba(255,255,255,0.08);
        }
        body.ui-gallery .scroll-reveal::-webkit-scrollbar { width: 6px; }
        body.ui-gallery .scroll-reveal::-webkit-scrollbar-thumb {
            background: rgba(255,255,255,0.4);
            border-radius: 999px;
        }
        body.ui-gallery .scroll-reveal::-webkit-scrollbar-track {
            background: rgba(255,255,255,0.08);
        }
    """,
    "dashboard": """
        body.ui-dashboard {
            background:
                linear-gradient(135deg, #0d1117 0%, #171717 48%, #0d1512 100%);
            padding: 18px;
            color: #e8edf3;
        }
        body.ui-dashboard * {
            scrollbar-color: #465461 #111820;
        }
        body.ui-dashboard ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        body.ui-dashboard ::-webkit-scrollbar-track {
            background: #111820;
        }
        body.ui-dashboard ::-webkit-scrollbar-thumb {
            background: #465461;
            border-radius: 999px;
        }
        body.ui-dashboard ::-webkit-scrollbar-thumb:hover {
            background: #5a6875;
        }
        body.ui-dashboard .container {
            max-width: 1840px;
            display: grid;
            grid-template-columns: 320px minmax(0, 1fr);
            grid-template-areas:
                "head head"
                "tools main"
                "top main"
                ". bottom";
            gap: 18px;
        }
        body.ui-dashboard .top-bar {
            grid-area: head;
            min-height: 74px;
            margin: 0;
            padding: 14px 18px;
            justify-content: flex-start;
            border: 1px solid #2a343d;
            border-radius: 14px;
            background: #171c22;
            box-shadow: 0 18px 42px rgba(0,0,0,0.28);
        }
        body.ui-dashboard h1 {
            color: #f4f7fb;
            font-size: 1.8em;
            text-align: left;
            text-shadow: none;
            letter-spacing: 0;
        }
        body.ui-dashboard .style-switcher {
            position: relative;
            left: auto;
            top: auto;
            transform: none;
            max-width: none;
            margin-left: 26px;
        }
        body.ui-dashboard .style-link {
            color: #b8c4d0;
            background: #20262d;
            border-color: #303a44;
        }
        body.ui-dashboard .style-link.active {
            color: #07130e;
            background: #7ee2a8;
        }
        body.ui-dashboard .search-box {
            right: 18px;
            width: min(420px, 28vw);
        }
        body.ui-dashboard .search-input {
            color: #edf3f8;
            background: #111820;
            border-color: #303b45;
            box-shadow: 0 10px 26px rgba(0,0,0,0.2);
        }
        body.ui-dashboard .search-input::placeholder { color: #778897; }
        body.ui-dashboard .search-icon { color: #9aa9b8; }
        body.ui-dashboard .search-clear {
            color: #dce6ef;
            background: rgba(255,255,255,0.08);
        }
        body.ui-dashboard .category-bar {
            grid-area: tools;
            display: block;
            margin: 0;
            padding: 16px;
            border: 1px solid #2a343d;
            border-radius: 14px;
            background: #171c22;
            box-shadow: 0 18px 42px rgba(0,0,0,0.24);
        }
        body.ui-dashboard .control-group {
            display: block;
            margin-bottom: 16px;
        }
        body.ui-dashboard .category-label {
            display: block;
            color: #a6b4c2;
            margin-bottom: 8px;
            font-weight: 700;
        }
        body.ui-dashboard .category-select {
            width: 100%;
            min-width: 0;
            color: #e9eff5;
            background: #111820;
            border: 1px solid #303b45;
            box-shadow: none;
        }
        body.ui-dashboard .category-picker {
            width: 100%;
            min-width: 0;
        }
        body.ui-dashboard .category-select-arrow {
            border-color: #9aa9b8;
        }
        body.ui-dashboard .category-dropdown {
            background: #171c22;
            border: 1px solid #303b45;
            box-shadow: 0 24px 58px rgba(0,0,0,0.42);
        }
        body.ui-dashboard .category-search-wrap {
            background: #12181f;
            border-bottom-color: #2a343d;
        }
        body.ui-dashboard .category-search-input {
            color: #edf3f8;
            background: #0f151c;
            border-color: #303b45;
        }
        body.ui-dashboard .category-search-input:focus {
            border-color: #7ee2a8;
            box-shadow: 0 0 0 4px rgba(126,226,168,0.12);
        }
        body.ui-dashboard .category-option-list {
            background: #171c22;
        }
        body.ui-dashboard .category-option {
            color: #dce6ef;
        }
        body.ui-dashboard .category-option:hover {
            background: rgba(126,226,168,0.1);
        }
        body.ui-dashboard .category-option.active {
            color: #07130e;
            background: #7ee2a8;
        }
        body.ui-dashboard .category-option-count,
        body.ui-dashboard .category-option-empty {
            color: #8fa0af;
        }
        body.ui-dashboard .category-option.active .category-option-count {
            color: #123821;
        }
        body.ui-dashboard .work-type-buttons {
            display: grid;
            grid-template-columns: 1fr;
        }
        body.ui-dashboard .work-type-btn {
            width: 100%;
            color: #b8c4d0;
            background: #111820;
            border-color: #303b45;
            border-radius: 8px;
            text-align: left;
            padding: 9px 11px;
        }
        body.ui-dashboard .work-type-btn.active {
            color: #07130e;
            background: #7ee2a8;
        }
        body.ui-dashboard .category-meta {
            display: block;
            color: #8fa0af;
            margin-top: 8px;
        }
        body.ui-dashboard .page-info {
            color: #a6b4c2;
        }
        body.ui-dashboard .page-btn {
            color: #dce6ef;
            background: #20262d;
            border: 1px solid #303b45;
        }
        body.ui-dashboard .page-btn.active {
            color: #07130e;
            background: #7ee2a8;
            border-color: transparent;
        }
        body.ui-dashboard .page-btn:disabled {
            background: #14191f;
            color: #64717e;
        }
        body.ui-dashboard #pagination {
            grid-area: top;
            margin: 0;
            align-self: start;
            justify-content: flex-start;
        }
        body.ui-dashboard #paginationBottom {
            grid-area: bottom;
            margin: 0;
            justify-content: center;
        }
        body.ui-dashboard .works-grid {
            grid-area: main;
            display: block;
        }
        body.ui-dashboard .work-card {
            display: grid;
            grid-template-columns: 180px minmax(0, 1fr) minmax(260px, 0.82fr);
            gap: 16px;
            align-items: stretch;
            border-radius: 12px;
            border: 1px solid #2a343d;
            background: #171c22;
            box-shadow: 0 14px 34px rgba(0,0,0,0.22);
            margin-bottom: 12px;
        }
        body.ui-dashboard .work-card:hover {
            transform: translateX(3px);
            box-shadow: 0 18px 44px rgba(0,0,0,0.32);
        }
        body.ui-dashboard .image-carousel {
            height: 100%;
            min-height: 100%;
            border-radius: 12px 0 0 12px;
            background: #0c1117;
            overflow: hidden;
        }
        body.ui-dashboard .carousel-slide {
            display: flex;
            align-items: center;
            justify-content: center;
        }
        body.ui-dashboard .carousel-slide img {
            width: 100%;
            height: 100%;
            max-width: none;
            max-height: none;
            object-fit: contain;
            padding: 10px;
            background: linear-gradient(135deg, #0e141b 0%, #161d24 100%);
        }
        body.ui-dashboard .work-content {
            grid-column: 2 / span 2;
            display: grid;
            grid-template-columns: minmax(0, 1fr) minmax(220px, 0.72fr);
            gap: 14px;
            padding: 14px 16px 14px 0;
            min-width: 0;
        }
        body.ui-dashboard .name-section,
        body.ui-dashboard .description-section,
        body.ui-dashboard .intro-section {
            margin: 0;
            padding: 0;
            border: 0;
            background: transparent;
        }
        body.ui-dashboard .name-section { grid-column: 1; }
        body.ui-dashboard .description-section { grid-column: 1; }
        body.ui-dashboard .intro-section {
            grid-column: 2;
            grid-row: 1 / span 2;
            max-height: 170px;
            overflow: auto;
            background: #111820;
            border: 1px solid #26313a;
            border-radius: 10px;
            padding: 12px;
        }
        body.ui-dashboard .description-text {
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            font-size: 0.88em;
            line-height: 1.55;
        }
        body.ui-dashboard .name-label,
        body.ui-dashboard .maker-name,
        body.ui-dashboard .work-kind-row,
        body.ui-dashboard .description-text,
        body.ui-dashboard .intro-content,
        body.ui-dashboard .name-translated-text {
            color: #a6b4c2;
        }
        body.ui-dashboard .name-text {
            color: #f2f6fb;
            font-size: 1.05em;
        }
        body.ui-dashboard .name-translated-text {
            border-bottom-color: #34404a;
        }
        body.ui-dashboard .name-row {
            gap: 8px;
            margin-bottom: 6px;
        }
        body.ui-dashboard .copy-btn {
            padding: 5px 10px;
            color: #07130e;
            background: #7ee2a8;
        }
        body.ui-dashboard .section-title {
            color: #7ee2a8;
            font-size: 0.9em;
            margin-bottom: 6px;
        }
        body.ui-dashboard .section-title::before {
            background: #7ee2a8;
        }
        body.ui-dashboard .kind-pill {
            color: #07130e;
            background: #7ee2a8;
            border: 0;
        }
        body.ui-dashboard .state-btn,
        body.ui-dashboard .asmr-link-btn {
            color: #dce6ef;
            background: #20262d;
            border-color: #303b45;
            box-shadow: none;
        }
        body.ui-dashboard .state-btn.active-like {
            color: #8ef0b5;
            background: rgba(33, 197, 94, 0.14);
            border-color: rgba(126,226,168,0.4);
        }
        body.ui-dashboard .state-btn.active-dislike {
            color: #ff9a9a;
            background: rgba(244, 63, 94, 0.14);
            border-color: rgba(255,154,154,0.35);
        }
        body.ui-dashboard .state-btn.active-played {
            color: #9fc3ff;
            background: rgba(96, 165, 250, 0.14);
            border-color: rgba(159,195,255,0.35);
        }
        body.ui-dashboard .state-pill,
        body.ui-dashboard .state-pill.read {
            color: #b8c4d0;
            background: #20262d;
        }
        body.ui-dashboard .parts-heading {
            color: #7ee2a8;
        }
        body.ui-dashboard .floating-btn.secondary {
            color: #dce6ef;
            background: #20262d;
            border: 1px solid #303b45;
        }
        body.ui-dashboard .floating-btn.active {
            background: #9f3434;
        }
        body.ui-dashboard .floating-actions {
            left: 18px;
            right: auto;
            bottom: 18px;
            width: 320px;
            justify-content: flex-start;
        }
        @media (max-width: 1100px) {
            body.ui-dashboard .container {
                display: block;
            }
            body.ui-dashboard .top-bar,
            body.ui-dashboard .category-bar,
            body.ui-dashboard #pagination,
            body.ui-dashboard #paginationBottom {
                margin-bottom: 16px;
            }
            body.ui-dashboard .work-card,
            body.ui-dashboard .work-content {
                display: block;
            }
            body.ui-dashboard .image-carousel {
                height: 260px;
                border-radius: 12px 12px 0 0;
            }
            body.ui-dashboard .work-content {
                padding: 16px;
            }
            body.ui-dashboard .intro-section {
                margin-top: 12px;
            }
            body.ui-dashboard .floating-actions {
                left: 12px;
                right: 12px;
                width: auto;
            }
        }
    """,
    "magazine": """
        body.ui-magazine {
            background:
                radial-gradient(circle at top right, rgba(116,75,162,0.28), transparent 26%),
                radial-gradient(circle at 15% 10%, rgba(17,153,142,0.22), transparent 20%),
                linear-gradient(135deg, #11131a 0%, #1c1e2a 52%, #0f141d 100%);
            color: #f5f7fb;
        }
        body.ui-magazine .container { max-width: 1580px; }
        body.ui-magazine .top-bar {
            min-height: 88px;
            justify-content: flex-start;
            padding: 18px 18px 18px 0;
            margin-bottom: 16px;
        }
        body.ui-magazine h1 {
            text-align: left;
            font-family: Georgia, 'Times New Roman', 'Microsoft YaHei', serif;
            letter-spacing: 0;
            font-size: 2.55em;
            text-shadow: 0 16px 30px rgba(0,0,0,0.4);
        }
        body.ui-magazine .style-switcher {
            position: relative;
            left: auto;
            top: auto;
            transform: none;
            max-width: 760px;
            margin-left: 24px;
        }
        body.ui-magazine .style-link {
            background: rgba(255,255,255,0.08);
            color: rgba(255,255,255,0.8);
            border-color: rgba(255,255,255,0.12);
        }
        body.ui-magazine .style-link.active {
            background: linear-gradient(135deg, #f3dfb3 0%, #4cc3aa 100%);
            color: #13161a;
        }
        body.ui-magazine .search-box {
            position: relative;
            right: auto;
            top: auto;
            transform: none;
            width: min(420px, 30vw);
            margin-left: auto;
        }
        body.ui-magazine .search-input {
            color: #fff7ea;
            background: rgba(255,255,255,0.08);
            border-color: rgba(255,255,255,0.16);
            box-shadow: none;
        }
        body.ui-magazine .category-bar {
            justify-content: flex-start;
            margin-bottom: 18px;
        }
        body.ui-magazine .works-grid {
            grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
            gap: 18px;
        }
        body.ui-magazine .work-card {
            border-radius: 10px;
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.08);
            backdrop-filter: blur(14px);
            box-shadow: 0 24px 56px rgba(0,0,0,0.28);
            overflow: hidden;
        }
        body.ui-magazine .work-card:hover {
            transform: translateY(-5px) scale(1.01);
            box-shadow: 0 28px 72px rgba(0,0,0,0.4);
        }
        body.ui-magazine .image-carousel {
            height: 280px;
            background: #0a0f17;
        }
        body.ui-magazine .work-content {
            padding: 18px;
            color: #f4f7fb;
        }
        body.ui-magazine .name-section,
        body.ui-magazine .description-section,
        body.ui-magazine .intro-section {
            background: transparent;
            border: 0;
        }
        body.ui-magazine .name-text { font-size: 1.22em; color: #fff; }
        body.ui-magazine .description-text { color: rgba(255,255,255,0.76); }
        body.ui-magazine .intro-section {
            max-height: 260px;
            overflow: auto;
            padding-right: 4px;
        }
        body.ui-magazine .kind-pill {
            background: rgba(255,255,255,0.1);
            color: #f3dfb3;
            border-color: rgba(255,255,255,0.14);
        }
        body.ui-magazine .work-id-badge,
        body.ui-magazine .copy-btn,
        body.ui-magazine .state-btn {
            border-radius: 999px;
        }
        body.ui-magazine .state-btn.active-like { background: rgba(33, 197, 94, 0.14); color: #7ee2a8; }
        body.ui-magazine .state-btn.active-dislike { background: rgba(244, 63, 94, 0.14); color: #ff9090; }
        body.ui-magazine .state-btn.active-played { background: rgba(59, 130, 246, 0.14); color: #8cb7ff; }
    """,
    "poster": """
        body.ui-poster {
            background:
                linear-gradient(135deg, rgba(255,255,255,0.02) 0 25%, transparent 25% 50%, rgba(255,255,255,0.02) 50% 75%, transparent 75% 100%),
                linear-gradient(135deg, #0b1020 0%, #10182a 52%, #0b1020 100%);
        }
        body.ui-poster .container { max-width: 1700px; }
        body.ui-poster .top-bar { justify-content: flex-start; margin-bottom: 18px; }
        body.ui-poster h1 { text-align: left; text-shadow: 0 14px 30px rgba(0,0,0,0.35); }
        body.ui-poster .works-grid {
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 14px;
        }
        body.ui-poster .work-card {
            border-radius: 16px;
            overflow: hidden;
            border: 1px solid rgba(255,255,255,0.08);
            box-shadow: 0 18px 40px rgba(0,0,0,0.25);
        }
        body.ui-poster .work-card:hover {
            transform: translateY(-4px) rotate(-0.2deg);
        }
        body.ui-poster .image-carousel { height: 420px; }
        body.ui-poster .work-content {
            position: absolute;
            inset: auto 0 0 0;
            padding: 72px 16px 16px;
            background: linear-gradient(180deg, transparent 0%, rgba(0,0,0,0.76) 30%, rgba(0,0,0,0.92) 100%);
            color: #fff;
        }
        body.ui-poster .description-section,
        body.ui-poster .intro-section { display: none; }
        body.ui-poster .name-section { border: 0; margin: 0; padding: 0; }
        body.ui-poster .name-text { color: #fff; font-size: 1.08em; }
        body.ui-poster .name-translated-text { color: rgba(255,255,255,0.8); border: 0; }
        body.ui-poster .copy-btn { display: none; }
        body.ui-poster .maker-name { color: rgba(255,255,255,0.75); }
    """,
    "mosaic": """
        body.ui-mosaic {
            background: #0e1520;
            padding: 18px;
        }
        body.ui-mosaic .container { max-width: 1800px; }
        body.ui-mosaic .works-grid {
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 10px;
        }
        body.ui-mosaic .work-card {
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid rgba(255,255,255,0.08);
            box-shadow: 0 14px 32px rgba(0,0,0,0.24);
        }
        body.ui-mosaic .work-card:nth-child(4n + 1) { grid-column: span 2; }
        body.ui-mosaic .work-card:nth-child(6n + 2) { grid-row: span 2; }
        body.ui-mosaic .image-carousel { height: 100%; min-height: 220px; }
        body.ui-mosaic .work-content { padding: 12px; }
        body.ui-mosaic .description-section,
        body.ui-mosaic .intro-section { display: none; }
        body.ui-mosaic .name-text { font-size: 1em; }
        body.ui-mosaic .name-row { display: block; }
        body.ui-mosaic .copy-btn { display: none; }
    """,
    "split": """
        body.ui-split {
            background: #f0f3f7;
            padding: 18px;
            color: #1c2733;
        }
        body.ui-split .container { max-width: 1760px; }
        body.ui-split .works-grid {
            grid-template-columns: 360px minmax(0, 1fr);
            gap: 16px;
        }
        body.ui-split .work-card {
            display: block;
            border-radius: 14px;
            overflow: hidden;
            background: #fff;
            border: 1px solid #dbe4ee;
        }
        body.ui-split .work-card:nth-child(1) {
            grid-row: 1 / span 4;
        }
        body.ui-split .image-carousel { height: 240px; }
        body.ui-split .work-content { padding: 16px; }
        body.ui-split .description-text,
        body.ui-split .intro-section { max-height: 180px; overflow: auto; }
        body.ui-split .description-section { margin-bottom: 12px; }
    """,
    "timeline": """
        body.ui-timeline {
            background: #111827;
            padding: 22px;
        }
        body.ui-timeline .container { max-width: 1500px; }
        body.ui-timeline .works-grid {
            grid-template-columns: 1fr;
            gap: 16px;
        }
        body.ui-timeline .work-card {
            display: grid;
            grid-template-columns: 220px minmax(0, 1fr);
            border-radius: 12px;
            overflow: hidden;
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.08);
            backdrop-filter: blur(14px);
        }
        body.ui-timeline .image-carousel { height: 100%; min-height: 260px; }
        body.ui-timeline .work-content { padding: 16px; color: #edf4fb; }
        body.ui-timeline .name-text { color: #fff; }
        body.ui-timeline .description-text { color: rgba(255,255,255,0.76); }
        body.ui-timeline .work-card:nth-child(odd) {
            margin-left: 0;
        }
    """,
    "glass": """
        body.ui-glass {
            background:
                radial-gradient(circle at top left, rgba(115,103,240,0.24), transparent 24%),
                radial-gradient(circle at bottom right, rgba(56,189,248,0.2), transparent 26%),
                linear-gradient(135deg, #0f172a 0%, #111827 100%);
            padding: 18px;
        }
        body.ui-glass .container { max-width: 1700px; }
        body.ui-glass .work-card,
        body.ui-glass .top-bar,
        body.ui-glass .category-bar {
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.14);
            backdrop-filter: blur(18px);
            box-shadow: 0 20px 56px rgba(0,0,0,0.26);
        }
        body.ui-glass .works-grid {
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 16px;
        }
        body.ui-glass .work-card {
            border-radius: 18px;
            overflow: hidden;
        }
        body.ui-glass .image-carousel { height: 260px; }
        body.ui-glass .work-content { padding: 16px; color: #eef6ff; }
        body.ui-glass .kind-pill { background: rgba(255,255,255,0.1); color: #d6f3ff; }
    """,
    "cinema": """
        body.ui-cinema {
            background: #050608;
            padding: 16px 22px;
        }
        body.ui-cinema .container { max-width: 1800px; }
        body.ui-cinema .top-bar { justify-content: flex-start; margin-bottom: 14px; }
        body.ui-cinema .works-grid {
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 12px;
        }
        body.ui-cinema .work-card {
            border-radius: 8px;
            overflow: hidden;
            background: #0c0d12;
            border: 1px solid rgba(255,255,255,0.08);
        }
        body.ui-cinema .image-carousel { height: 360px; }
        body.ui-cinema .work-content { padding: 12px; color: #f2f5f9; }
        body.ui-cinema .description-section,
        body.ui-cinema .intro-section { display: none; }
    """,
    "archive": """
        body.ui-archive {
            background: #f7f3eb;
            color: #2b251e;
            padding: 20px;
        }
        body.ui-archive .container { max-width: 1500px; }
        body.ui-archive .works-grid {
            grid-template-columns: 1fr;
            gap: 14px;
        }
        body.ui-archive .work-card {
            display: grid;
            grid-template-columns: 160px minmax(0, 1fr);
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid #d8ccb8;
            background: #fffdf8;
        }
        body.ui-archive .image-carousel { height: 100%; min-height: 180px; }
        body.ui-archive .work-content { padding: 14px; }
        body.ui-archive .description-text {
            max-height: 120px;
            overflow: auto;
        }
    """,
    "prism": """
        body.ui-prism {
            background:
                linear-gradient(135deg, rgba(34,211,238,0.08), transparent 35%),
                linear-gradient(225deg, rgba(168,85,247,0.12), transparent 30%),
                #0b1020;
            padding: 18px;
        }
        body.ui-prism .works-grid {
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 14px;
        }
        body.ui-prism .work-card {
            border-radius: 16px;
            overflow: hidden;
            border: 1px solid rgba(255,255,255,0.08);
            box-shadow: 0 20px 48px rgba(0,0,0,0.28);
        }
        body.ui-prism .work-card:hover {
            transform: translateY(-5px) skewX(-0.15deg);
        }
        body.ui-prism .image-carousel { height: 300px; }
        body.ui-prism .work-content { padding: 14px; }
    """,
    "spotlight": """
        body.ui-spotlight {
            background:
                radial-gradient(circle at 50% 0, rgba(250,204,21,0.18), transparent 26%),
                #050505;
            padding: 18px;
        }
        body.ui-spotlight .works-grid {
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 18px;
        }
        body.ui-spotlight .work-card {
            border-radius: 18px;
            overflow: hidden;
            border: 1px solid rgba(255,255,255,0.08);
            background: rgba(255,255,255,0.04);
            box-shadow: 0 18px 46px rgba(0,0,0,0.34);
        }
        body.ui-spotlight .work-card:hover {
            transform: translateY(-6px) scale(1.01);
            box-shadow: 0 26px 68px rgba(0,0,0,0.44);
        }
        body.ui-spotlight .image-carousel { height: 340px; }
        body.ui-spotlight .work-content { padding: 16px; color: #f7f7f7; }
        body.ui-spotlight .description-section,
        body.ui-spotlight .intro-section { max-height: 140px; overflow: auto; }
    """,
    "compact": """
        body.ui-compact {
            background: #0f1720;
            padding: 16px;
        }
        body.ui-compact .container { max-width: 1900px; }
        body.ui-compact h1 {
            text-align: left;
            font-size: 1.7em;
            color: #e6edf3;
            letter-spacing: 0;
            text-shadow: none;
        }
        body.ui-compact .top-bar {
            justify-content: flex-start;
            margin-bottom: 14px;
            padding-right: 430px;
        }
        body.ui-compact .style-switcher {
            left: 180px;
            max-width: 760px;
        }
        body.ui-compact .style-link {
            min-height: 30px;
            padding: 5px 10px;
            color: #b7c4d3;
            background: rgba(255,255,255,0.06);
            border-color: rgba(255,255,255,0.12);
        }
        body.ui-compact .style-link.active {
            color: #07130e;
            background: #7ee2a8;
        }
        body.ui-compact .search-input {
            height: 38px;
            color: #e6edf3;
            background: rgba(255,255,255,0.08);
            border-color: rgba(255,255,255,0.16);
            box-shadow: none;
        }
        body.ui-compact .category-bar {
            justify-content: flex-start;
            margin-bottom: 14px;
        }
        body.ui-compact .category-label,
        body.ui-compact .category-meta,
        body.ui-compact .page-info {
            color: #b7c4d3;
        }
        body.ui-compact .category-select {
            color: #e6edf3;
            background: #182331;
            border: 1px solid rgba(255,255,255,0.12);
            box-shadow: none;
        }
        body.ui-compact .category-dropdown,
        body.ui-compact .category-search-wrap,
        body.ui-compact .category-option-list {
            background: #182331;
        }
        body.ui-compact .category-option,
        body.ui-compact .category-option-count {
            color: #e6edf3;
        }
        body.ui-compact .work-type-btn {
            color: #b7c4d3;
            background: rgba(255,255,255,0.06);
            border-color: rgba(255,255,255,0.12);
            padding: 3px 9px;
        }
        body.ui-compact .work-type-btn.active,
        body.ui-compact .page-btn,
        body.ui-compact .floating-btn.secondary {
            color: #07130e;
            background: #7ee2a8;
        }
        body.ui-compact .works-grid {
            grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
            gap: 12px;
        }
        body.ui-compact .work-card {
            border-radius: 10px;
            background: #182331;
            color: #e6edf3;
            box-shadow: 0 12px 28px rgba(0,0,0,0.28);
        }
        body.ui-compact .work-card:hover {
            transform: translateY(-3px);
            box-shadow: 0 16px 34px rgba(0,0,0,0.34);
        }
        body.ui-compact .image-carousel {
            height: 190px;
            background: #0a111a;
        }
        body.ui-compact .work-content { padding: 12px; }
        body.ui-compact .name-section {
            margin-bottom: 10px;
            padding-bottom: 10px;
            border-bottom-color: rgba(255,255,255,0.1);
        }
        body.ui-compact .maker-name,
        body.ui-compact .work-kind-row,
        body.ui-compact .description-text,
        body.ui-compact .intro-content,
        body.ui-compact .name-translated-text {
            color: #9fb0c3;
        }
        body.ui-compact .name-row {
            display: block;
            margin-bottom: 8px;
        }
        body.ui-compact .name-label { display: none; }
        body.ui-compact .name-text {
            display: block;
            color: #e6edf3;
            font-size: 0.98em;
            line-height: 1.35;
        }
        body.ui-compact .name-translated-text {
            display: block;
            border: 0;
            font-size: 0.88em;
            min-height: 0;
        }
        body.ui-compact .copy-btn {
            margin-top: 6px;
            padding: 4px 8px;
            border-radius: 7px;
            background: #26364a;
        }
        body.ui-compact .description-text {
            display: block;
            overflow: visible;
            font-size: 0.84em;
            line-height: 1.5;
        }
        body.ui-compact .intro-section { display: block; }
        body.ui-compact .scroll-reveal {
            max-height: 224px;
            overflow: auto;
            padding-right: 6px;
            margin-top: 10px;
            scrollbar-width: thin;
            scrollbar-color: rgba(126,226,168,0.48) rgba(255,255,255,0.08);
        }
        body.ui-compact .scroll-reveal .description-section,
        body.ui-compact .scroll-reveal .intro-section {
            display: block;
        }
        body.ui-compact .scroll-reveal .description-text {
            display: block;
            overflow: visible;
        }
        body.ui-compact .section-title {
            font-size: 0.9em;
            color: #7ee2a8;
        }
        body.ui-compact .section-title::before { background: #7ee2a8; }
        body.ui-compact .kind-pill {
            color: #07130e;
            background: #7ee2a8;
            border: 0;
        }
        body.ui-compact .state-btn,
        body.ui-compact .asmr-link-btn {
            color: #dce7f3;
            background: #26364a;
            border-color: rgba(255,255,255,0.08);
            padding: 5px 9px;
        }
        @media (max-width: 900px) {
            body.ui-gallery .top-bar,
            body.ui-compact .top-bar {
                display: block;
                padding-right: 0;
            }
            body.ui-gallery .style-switcher,
            body.ui-compact .style-switcher {
                position: relative;
                left: auto;
                top: auto;
                transform: none;
                margin: 14px 0 0;
            }
            body.ui-gallery .search-box,
            body.ui-compact .search-box {
                position: relative;
                right: auto;
                top: auto;
                transform: none;
                width: 100%;
                min-width: 0;
                margin-top: 14px;
            }
            body.ui-gallery .work-card:nth-child(n),
            body.ui-gallery .work-card.hero-card {
                grid-row: auto;
                min-height: 520px;
            }
            body.ui-gallery .work-card:nth-child(n) .image-carousel,
            body.ui-gallery .work-card.hero-card .image-carousel {
                height: 360px;
            }
        }
    """,
}

def generate_page_json(works, page_num):
    start = (page_num - 1) * ITEMS_PER_PAGE
    end = min(start + ITEMS_PER_PAGE, len(works))
    page_works = works[start:end]
    
    works_json = []
    for w in page_works:
        work_data = {
            "product_id": w["product_id"],
            "work_name": w["work_name"],
            "maker_name": w["maker_name"],
            "description": w["description_clean"],
            "work_types": w.get("work_types", []),
            "work_kind": w.get("work_kind", WORK_KIND_GAME),
            "version_group_id": w.get("version_group_id", w["product_id"]),
            "version_lang": w.get("version_lang", ""),
            "version_label": w.get("version_label", ""),
            "version_rank": w.get("version_rank", 2),
            "version_ids": w.get("version_ids", []),
            "slider_images": w["local_slider_images"],
            "parts": deepcopy(w["parts"])
        }
        works_json.append(work_data)
    return works_json


def make_safe_slug(name):
    slug = re.sub(r'[<>:"/\\|?*#%&+\x00-\x1f]', "_", name).strip()
    slug = re.sub(r"\s+", "_", slug)
    slug = slug.strip(" ._")
    return slug or "uncategorized"


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
    genre_list_file = BASE_DIR / "list.devtools"
    if genre_list_file.exists():
        try:
            with open(genre_list_file, "r", encoding="utf-8") as f:
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

    _GENRE_NAME_MAP = genre_map
    return _GENRE_NAME_MAP


def unique_existing_work_ids(work_ids, works_by_id):
    seen = set()
    result = []
    for work_id in work_ids:
        if work_id in seen or work_id not in works_by_id:
            continue
        result.append(work_id)
        seen.add(work_id)
    return result


def load_work_order_ids():
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


def load_crawl_order_ids():
    if not CRAWL_RESULTS_FILE.exists():
        return []

    try:
        with open(CRAWL_RESULTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    raw_categories = data.get("categories", []) if isinstance(data, dict) else data
    if not isinstance(raw_categories, list):
        return []

    work_ids = []
    for item in raw_categories:
        if not isinstance(item, dict):
            continue
        item_work_ids = item.get("work_ids", [])
        if isinstance(item_work_ids, list):
            work_ids.extend(work_id for work_id in item_work_ids if isinstance(work_id, str))
    return work_ids


def order_html_files_by_popularity(html_files):
    file_dict = {Path(f).stem: f for f in html_files}
    ordered_files = []
    seen_files = set()
    source_counts = {
        "works_order": 0,
        "crawl_results": 0,
        "filename": 0,
    }

    def append_by_work_ids(work_ids, source_key):
        for work_id in work_ids:
            path = file_dict.get(work_id)
            if not path or path in seen_files:
                continue
            ordered_files.append(path)
            seen_files.add(path)
            source_counts[source_key] += 1

    append_by_work_ids(load_work_order_ids(), "works_order")
    append_by_work_ids(load_crawl_order_ids(), "crawl_results")

    for path in html_files:
        if path in seen_files:
            continue
        ordered_files.append(path)
        seen_files.add(path)
        source_counts["filename"] += 1

    return ordered_files, source_counts


def load_crawl_categories(works_by_id):
    if not CRAWL_RESULTS_FILE.exists():
        return []

    try:
        with open(CRAWL_RESULTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    raw_categories = data.get("categories", []) if isinstance(data, dict) else data
    merged_categories = []
    merged_by_key = {}
    genre_name_map = load_genre_name_map()

    for item in raw_categories:
        if not isinstance(item, dict):
            continue

        source_url = item.get("source_url", "")
        old_name = item.get("name") or "未分类"
        genre_id = extract_value_after_path_key(source_url, "genre[0]")
        name = genre_name_map.get(genre_id, old_name) if genre_id else old_name

        work_ids = unique_existing_work_ids(item.get("work_ids", []), works_by_id)
        if not work_ids:
            continue

        old_slug = item.get("slug") or ""
        default_old_slug = make_safe_slug(old_name)
        slug = make_safe_slug(name if genre_id and (not old_slug or old_slug == default_old_slug or old_name != name) else (old_slug or name))
        merge_key = f"genre:{genre_id}" if genre_id else f"slug:{slug}"
        existing_idx = merged_by_key.get(merge_key)
        if existing_idx is not None:
            existing = merged_categories[existing_idx]
            existing["work_ids"] = unique_existing_work_ids(existing["work_ids"] + work_ids, works_by_id)
            if item.get("updated_at", "") > existing.get("updated_at", ""):
                existing["updated_at"] = item.get("updated_at", "")
            continue

        merged_by_key[merge_key] = len(merged_categories)
        merged_categories.append({
            "name": name,
            "slug": slug,
            "source_url": source_url,
            "updated_at": item.get("updated_at", ""),
            "work_ids": work_ids,
        })

    categories = []
    used_slugs = set()
    for entry in merged_categories:
        slug = entry["slug"]
        base_slug = slug
        suffix = 2
        while slug in used_slugs:
            slug = f"{base_slug}_{suffix}"
            suffix += 1
        used_slugs.add(slug)

        normalized_entry = dict(entry)
        normalized_entry["slug"] = slug
        categories.append(normalized_entry)

    return categories


def collect_work_kinds(works):
    kinds = {work.get("work_kind", WORK_KIND_GAME) for work in works}
    order = (
        WORK_KIND_SUBTITLED_ASMR,
        WORK_KIND_UNSUBTITLED_ASMR,
        WORK_KIND_AUDIO_ASMR,
        WORK_KIND_MANGA,
        WORK_KIND_GAME,
    )
    return [kind for kind in order if kind in kinds]


def build_manifest_entry(name, slug, count, data_path, translate_path="", source_url="", updated_at="", work_kinds=None, index_path="", search_text=""):
    return {
        "name": name,
        "slug": slug,
        "count": count,
        "pages": max(1, math.ceil(count / ITEMS_PER_PAGE)),
        "data_path": data_path,
        "index_path": index_path,
        "translate_path": translate_path,
        "source_url": source_url,
        "updated_at": updated_at,
        "genre_id": extract_value_after_path_key(source_url, "genre[0]") if source_url else "",
        "work_kinds": work_kinds or [],
        "search_text": normalize_search_text(search_text or build_pinyin_search_text(name)),
    }


def write_paged_outputs(works, json_dir, label):
    json_dir.mkdir(parents=True, exist_ok=True)

    total_pages = max(1, math.ceil(len(works) / ITEMS_PER_PAGE))
    for page in range(1, total_pages + 1):
        start = (page - 1) * ITEMS_PER_PAGE
        end = min(start + ITEMS_PER_PAGE, len(works))
        page_works = works[start:end]

        page_json = generate_page_json(works, page)
        json_path = json_dir / f"page_{page}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(page_json, f, ensure_ascii=False, separators=(",", ":"))

        if page == 1 or page == total_pages or page % PAGE_WRITE_LOG_INTERVAL == 0:
            print(
                f"  {label} 第 {page}/{total_pages} 页: {len(page_works)} 个作品 "
                f"-> {json_path.relative_to(OUTPUT_DIR)}"
            )

    return total_pages


def write_filter_index(works, index_path, label):
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index = []
    for idx, work in enumerate(works):
        index.append({
            "product_id": work["product_id"],
            "page": idx // ITEMS_PER_PAGE + 1,
            "index": idx % ITEMS_PER_PAGE,
            "kind": work.get("work_kind", WORK_KIND_GAME),
            "version_group_id": work.get("version_group_id", work["product_id"]),
            "version_rank": work.get("version_rank", 2),
        })

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    print(
        f"  {label} 筛选索引: {len(index)} 个作品 "
        f"-> {index_path.relative_to(OUTPUT_DIR)}"
    )


def normalize_search_text(value):
    value = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"\s+", " ", value).strip()


def dedupe_text_tokens(tokens):
    seen = set()
    result = []
    for token in tokens:
        token = normalize_search_text(token).replace(" ", "")
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def compact_search_token(value):
    return normalize_search_text(value).replace(" ", "")


@lru_cache(maxsize=32768)
def build_pinyin_search_text(value):
    text = str(value or "").strip()
    if not text or lazy_pinyin is None or not CJK_CHAR_RE.search(text):
        return ""

    full_parts = []
    initials = []
    for segment in CJK_SEGMENT_RE.findall(text):
        segment_full = [
            compact_search_token(part)
            for part in lazy_pinyin(segment, strict=False, errors="ignore")
        ]
        segment_full = [part for part in segment_full if part]
        if not segment_full:
            continue
        full_parts.extend(segment_full)
        if Style is not None:
            segment_initials = [
                compact_search_token(part)
                for part in lazy_pinyin(
                    segment,
                    style=Style.FIRST_LETTER,
                    strict=False,
                    errors="ignore",
                )
            ]
            initials.extend(part for part in segment_initials if part)

    tokens = list(full_parts)
    if full_parts:
        tokens.append("".join(full_parts))
        tokens.append(" ".join(full_parts))
    if initials:
        tokens.append("".join(initials))
        tokens.append(" ".join(initials))
    return " ".join(dedupe_text_tokens(tokens))


def build_search_text(work, category_names=None):
    values = [
        work.get("product_id", ""),
        work.get("work_name", ""),
        work.get("maker_name", ""),
        work.get("description_clean", ""),
        work.get("work_kind", WORK_KIND_GAME),
        work.get("version_lang", ""),
        work.get("version_label", ""),
    ]
    values.extend(work.get("work_types", []))
    values.extend(category_names or [])
    return normalize_search_text(" ".join(values))


def build_search_alias_text(work, category_names=None):
    values = [
        work.get("work_name", ""),
        work.get("maker_name", ""),
        work.get("work_kind", WORK_KIND_GAME),
        work.get("version_lang", ""),
        work.get("version_label", ""),
    ]
    values.extend(work.get("work_types", []))
    values.extend(category_names or [])
    return normalize_search_text(
        " ".join(
            alias
            for alias in (build_pinyin_search_text(value) for value in values)
            if alias
        )
    )


def write_search_index(works, crawl_categories):
    categories_by_work_id = {}
    category_names_by_work_id = {}
    for category in crawl_categories:
        slug = category.get("slug")
        name = category.get("name", "")
        if not slug:
            continue
        for work_id in category.get("work_ids", []):
            categories_by_work_id.setdefault(work_id, set()).add(slug)
            if name:
                category_names_by_work_id.setdefault(work_id, set()).add(name)

    index = []
    for idx, work in enumerate(works):
        work_id = work["product_id"]
        category_names = sorted(category_names_by_work_id.get(work_id, set()))
        index.append({
            "product_id": work_id,
            "page": idx // ITEMS_PER_PAGE + 1,
            "index": idx % ITEMS_PER_PAGE,
            "kind": work.get("work_kind", WORK_KIND_GAME),
            "version_group_id": work.get("version_group_id", work_id),
            "version_rank": work.get("version_rank", 2),
            "categories": sorted(categories_by_work_id.get(work_id, set())),
            "text": build_search_text(work, category_names),
            "aliases": build_search_alias_text(work, category_names),
        })

    with open(SEARCH_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    print(f"搜索索引已生成: {SEARCH_INDEX_FILE} ({len(index)} 个作品)")


def cleanup_stale_category_dirs(valid_slugs):
    if not JSON_DIR.exists():
        return

    for path in JSON_DIR.iterdir():
        if not path.is_dir() or path.name in valid_slugs:
            continue

        shutil.rmtree(path)
        print(f"  已移除旧分类数据: {path.relative_to(OUTPUT_DIR)}")


def cleanup_stale_filter_indexes(valid_slugs):
    if not FILTER_INDEX_DIR.exists():
        return

    valid_files = {"__all__.json"} | {f"{slug}.json" for slug in valid_slugs}
    for path in FILTER_INDEX_DIR.glob("*.json"):
        if path.name in valid_files:
            continue
        path.unlink()
        print(f"  已移除旧筛选索引: {path.relative_to(OUTPUT_DIR)}")


def cleanup_stale_style_pages(valid_files):
    if not OUTPUT_DIR.exists():
        return

    valid_names = set(valid_files)
    for path in OUTPUT_DIR.glob("index*.html"):
        if path.name in valid_names:
            continue
        path.unlink()
        print(f"  已移除旧风格页: {path.relative_to(OUTPUT_DIR)}")


def find_work_html_files():
    html_files = []
    seen = set()
    for pattern in WORK_FILE_PATTERNS:
        for path in WORKS_DIR.glob(pattern):
            if path in seen:
                continue
            html_files.append(path)
            seen.add(path)
    return sorted(html_files)


def get_work_version_ids(work):
    work_ids = [work["product_id"]]
    work_ids.extend(work.get("version_ids") or [])
    return unique_values([work_id.upper() for work_id in work_ids if work_id])


def choose_preferred_version_work(group_works):
    return min(
        enumerate(group_works),
        key=lambda item: (item[1].get("version_rank", 2), item[0]),
    )[1]


def collect_version_groups(works):
    groups = []
    groups_by_id = {}
    for work in works:
        group_id = work.get("version_group_id") or work["product_id"]
        if group_id not in groups_by_id:
            groups_by_id[group_id] = []
            groups.append(groups_by_id[group_id])
        groups_by_id[group_id].append(work)
    return groups


def has_any_translation_file(work_ids):
    for work_id in work_ids:
        translated_paths = [
            TRANSLATED_DRAFT_DIR / f"{work_id}.zh.md",
            TRANSLATE_DIR / f"{work_id}.zh.md",
            DONE_TRANSLATE_DIR / f"{work_id}.zh.md",
            PENDING_TRANSLATE_DIR / f"{work_id}.zh.md",
        ]
        if any(path.exists() for path in translated_paths):
            return True
    return False


def has_any_pending_translate_file(work_ids):
    return any((PENDING_TRANSLATE_DIR / f"{work_id}.md").exists() for work_id in work_ids)


def write_work_markdown_files(works):
    TRANSLATE_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_TRANSLATE_DIR.mkdir(parents=True, exist_ok=True)
    DONE_TRANSLATE_DIR.mkdir(parents=True, exist_ok=True)
    TRANSLATED_DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    ORIG_DIR.mkdir(parents=True, exist_ok=True)

    seen = set()
    written = 0
    skipped_translated = 0
    skipped_pending = 0
    skipped_duplicate_versions = 0
    for group_works in collect_version_groups(works):
        work = choose_preferred_version_work(group_works)
        work_id = work["product_id"]
        if work_id in seen:
            continue
        seen.add(work_id)

        group_work_ids = []
        for group_work in group_works:
            group_work_ids.extend(get_work_version_ids(group_work))
        group_work_ids = unique_values(group_work_ids)
        pending_path = PENDING_TRANSLATE_DIR / f"{work_id}.md"

        if has_any_translation_file(group_work_ids):
            skipped_translated += 1
        elif has_any_pending_translate_file(group_work_ids):
            skipped_pending += 1
        else:
            with open(pending_path, "w", encoding="utf-8") as f:
                f.write(generate_translate_md([work]))
            written += 1

        skipped_duplicate_versions += max(0, len(group_works) - 1)

    for work in works:
        work_id = work["product_id"]
        orig_path = ORIG_DIR / f"{work_id}.md"
        with open(orig_path, "w", encoding="utf-8") as f:
            f.write(generate_orig_md([work]))

    print(f"\n已生成 {written} 个待翻译作品文件 -> {PENDING_TRANSLATE_DIR}")
    print(f"已跳过 {skipped_translated} 个已有译文、{skipped_pending} 个已有待翻译稿")
    print(f"多版本作品已按组去重，避免重复生成 {skipped_duplicate_versions} 个待翻译稿")
    print(f"原文对照文件已生成 -> {ORIG_DIR}")


def generate_html(total_works, style_key="classic"):
    total_pages = math.ceil(total_works / ITEMS_PER_PAGE)
    variant = get_style_variant(style_key)
    active_style_key = variant["key"]
    style_name = variant["name"]
    body_class = variant["body_class"]
    layout_mode = variant.get("layout", "classic")
    style_switcher_html = build_style_switcher_html(active_style_key)
    style_extra_css = get_style_extra_css(active_style_key)

    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>作品展示 - ''' + escape_html(style_name) + '''</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', 'Microsoft YaHei', 'Noto Sans SC', sans-serif;
            background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%);
            min-height: 100vh;
            padding: 30px 20px;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
        }
        h1 {
            text-align: center;
            color: #fff;
            font-size: 2.5em;
            text-shadow: 0 0 20px rgba(102, 126, 234, 0.5);
            letter-spacing: 2px;
        }
        .style-switcher {
            position: absolute;
            left: 0;
            top: 50%;
            transform: translateY(-50%);
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
            max-width: min(560px, 42vw);
        }
        .style-link {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 34px;
            padding: 7px 12px;
            border: 1px solid rgba(255,255,255,0.22);
            border-radius: 999px;
            background: rgba(255,255,255,0.12);
            color: rgba(255,255,255,0.82);
            font-size: 0.84em;
            font-weight: 600;
            text-decoration: none;
            white-space: nowrap;
            backdrop-filter: blur(12px);
            transition: transform 0.2s ease, background 0.2s ease, color 0.2s ease, box-shadow 0.2s ease;
        }
        .style-link:hover {
            transform: translateY(-1px);
            background: rgba(255,255,255,0.2);
            color: #fff;
        }
        .style-link.active {
            color: #fff;
            background: linear-gradient(135deg, #11998e, #38ef7d);
            border-color: transparent;
            box-shadow: 0 8px 18px rgba(17, 153, 142, 0.24);
        }
        .top-bar {
            position: relative;
            min-height: 88px;
            margin-bottom: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .search-box {
            position: absolute;
            right: 0;
            top: 50%;
            transform: translateY(-50%);
            width: min(380px, 32vw);
            min-width: 260px;
        }
        .search-input {
            width: 100%;
            height: 44px;
            border: 1px solid rgba(255,255,255,0.28);
            border-radius: 999px;
            padding: 0 46px 0 44px;
            color: #fff;
            background: rgba(255,255,255,0.12);
            backdrop-filter: blur(18px);
            box-shadow: 0 14px 34px rgba(0,0,0,0.22);
            font-size: 0.95em;
            outline: none;
            transition: border-color 0.2s ease, background 0.2s ease, box-shadow 0.2s ease;
        }
        .search-input::placeholder {
            color: rgba(255,255,255,0.62);
        }
        .search-input:focus {
            border-color: rgba(255,255,255,0.58);
            background: rgba(255,255,255,0.18);
            box-shadow: 0 16px 38px rgba(102,126,234,0.28);
        }
        .search-icon {
            position: absolute;
            left: 17px;
            top: 50%;
            width: 18px;
            height: 18px;
            color: rgba(255,255,255,0.68);
            transform: translateY(-50%);
            pointer-events: none;
        }
        .search-icon circle,
        .search-icon path {
            fill: none;
            stroke: currentColor;
            stroke-width: 2.2;
            stroke-linecap: round;
        }
        .search-clear {
            position: absolute;
            right: 7px;
            top: 50%;
            width: 30px;
            height: 30px;
            border: none;
            border-radius: 50%;
            background: rgba(255,255,255,0.16);
            color: #fff;
            cursor: pointer;
            transform: translateY(-50%);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.2s ease, background 0.2s ease;
        }
        .search-clear.visible {
            opacity: 1;
            pointer-events: auto;
        }
        .search-clear:hover {
            background: rgba(255,255,255,0.28);
        }
        .category-bar {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 12px;
            margin: 0 auto 26px;
            flex-wrap: wrap;
        }
        .toolbar {
            align-items: center;
            row-gap: 14px;
        }
        .control-group {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }
        .category-label {
            color: rgba(255,255,255,0.9);
            font-size: 0.95em;
        }
        .category-select {
            min-width: 220px;
            max-width: min(520px, 100%);
            border: none;
            border-radius: 8px;
            padding: 10px 14px;
            color: #24243e;
            background: rgba(255,255,255,0.96);
            font-size: 0.95em;
            box-shadow: 0 10px 28px rgba(0,0,0,0.22);
        }
        .category-picker {
            position: relative;
            min-width: 220px;
            max-width: min(520px, 100%);
        }
        .category-select-button {
            width: 100%;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            cursor: pointer;
            transition: box-shadow 0.2s ease, transform 0.2s ease;
            text-align: left;
        }
        .category-select-button:hover {
            transform: translateY(-1px);
            box-shadow: 0 14px 34px rgba(0,0,0,0.24);
        }
        .category-select-button.open {
            box-shadow: 0 16px 38px rgba(0,0,0,0.28);
        }
        .category-select-label {
            min-width: 0;
            flex: 1;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .category-select-arrow {
            width: 10px;
            height: 10px;
            border-right: 2px solid rgba(36,36,62,0.72);
            border-bottom: 2px solid rgba(36,36,62,0.72);
            transform: rotate(45deg) translateY(-1px);
            transition: transform 0.2s ease;
            flex-shrink: 0;
            margin-right: 2px;
        }
        .category-select-button.open .category-select-arrow {
            transform: rotate(-135deg) translateY(-1px);
        }
        .category-dropdown {
            position: absolute;
            top: calc(100% + 10px);
            left: 0;
            width: min(420px, calc(100vw - 32px));
            background: rgba(255,255,255,0.98);
            border-radius: 14px;
            box-shadow: 0 22px 58px rgba(10,12,38,0.32);
            overflow: hidden;
            z-index: 40;
            backdrop-filter: blur(12px);
        }
        .category-dropdown[hidden] {
            display: none;
        }
        .category-search-wrap {
            padding: 14px;
            border-bottom: 1px solid rgba(91,104,173,0.12);
            background: rgba(245,247,255,0.9);
        }
        .category-search-input {
            width: 100%;
            border: 1px solid rgba(102,126,234,0.2);
            border-radius: 10px;
            padding: 10px 14px;
            font-size: 0.95em;
            color: #24243e;
            background: white;
            outline: none;
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }
        .category-search-input:focus {
            border-color: rgba(102,126,234,0.58);
            box-shadow: 0 0 0 4px rgba(102,126,234,0.14);
        }
        .category-option-list {
            max-height: min(420px, 55vh);
            overflow-y: auto;
            padding: 10px;
        }
        .category-option {
            width: 100%;
            border: none;
            background: transparent;
            color: #24243e;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            padding: 11px 12px;
            border-radius: 10px;
            cursor: pointer;
            text-align: left;
            transition: background 0.18s ease, color 0.18s ease, transform 0.18s ease;
        }
        .category-option:hover {
            background: rgba(102,126,234,0.1);
            transform: translateX(2px);
        }
        .category-option.active {
            background: linear-gradient(135deg, rgba(102,126,234,0.16), rgba(118,75,162,0.2));
            color: #352b66;
            font-weight: 600;
        }
        .category-option-name {
            min-width: 0;
            flex: 1;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .category-option-count {
            color: rgba(54,58,102,0.7);
            font-size: 0.84em;
            flex-shrink: 0;
        }
        .category-option-empty {
            color: rgba(54,58,102,0.7);
            padding: 18px 12px;
            text-align: center;
            font-size: 0.92em;
        }
        .work-type-buttons {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }
        .work-type-btn {
            background: rgba(255,255,255,0.15);
            border: 1px solid rgba(255,255,255,0.3);
            color: white;
            padding: 2px 10px;
            border-radius: 12px;
            cursor: pointer;
            font-size: 0.95em;
            transition: all 0.2s ease;
            user-select: none;
        }
        .work-type-btn:hover {
            background: rgba(255,255,255,0.25);
            border-color: rgba(255,255,255,0.5);
        }
        .work-type-btn.active {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-color: transparent;
            box-shadow: 0 2px 8px rgba(102,126,234,0.4);
        }
        .work-type-btn.disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }
        .category-meta {
            color: rgba(255,255,255,0.78);
            font-size: 0.9em;
        }
        .pagination {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 10px;
            margin-bottom: 30px;
            flex-wrap: wrap;
        }
        .infinite-summary {
            justify-content: flex-start;
            margin-bottom: 18px;
        }
        .infinite-status {
            min-height: 70px;
            margin: 28px 0 78px;
        }
        .infinite-status .page-info {
            padding: 12px 18px;
            border: 1px solid rgba(255,255,255,0.16);
            border-radius: 999px;
            background: rgba(255,255,255,0.08);
            backdrop-filter: blur(12px);
        }
        .loading-dots::after {
            content: '';
            display: inline-block;
            width: 1.4em;
            text-align: left;
            animation: loadingDots 1.1s steps(4, end) infinite;
        }
        @keyframes loadingDots {
            0% { content: ''; }
            25% { content: '.'; }
            50% { content: '..'; }
            75%, 100% { content: '...'; }
        }
        .page-btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 1em;
            transition: all 0.3s;
            min-width: 44px;
        }
        .page-btn:hover {
            transform: scale(1.05);
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
        }
        .page-btn:disabled {
            background: #555;
            cursor: not-allowed;
            opacity: 0.5;
        }
        .page-btn.active {
            background: linear-gradient(135deg, #11998e, #38ef7d);
        }
        .page-info {
            color: white;
            font-size: 1em;
            padding: 0 15px;
        }
        .works-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
            gap: 30px;
        }
        .work-card {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }
        .work-card:hover {
            transform: translateY(-8px);
            box-shadow: 0 30px 80px rgba(0, 0, 0, 0.4);
        }
        .image-carousel {
            position: relative;
            width: 100%;
            height: 350px;
            overflow: hidden;
            background: #1a1a2e;
        }
        .carousel-track {
            display: flex;
            height: 100%;
            transition: transform 0.4s ease;
        }
        .carousel-slide {
            min-width: 100%;
            height: 100%;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .carousel-slide img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            cursor: pointer;
        }
        .carousel-btn {
            position: absolute;
            top: 50%;
            transform: translateY(-50%);
            width: 44px;
            height: 44px;
            border-radius: 50%;
            border: none;
            background: rgba(0, 0, 0, 0.5);
            color: white;
            font-size: 20px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.3s;
            z-index: 10;
        }
        .carousel-btn:hover {
            background: rgba(102, 126, 234, 0.8);
        }
        .carousel-btn.prev { left: 12px; }
        .carousel-btn.next { right: 12px; }
        .carousel-counter {
            position: absolute;
            bottom: 12px;
            right: 12px;
            background: rgba(0, 0, 0, 0.6);
            color: white;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 0.85em;
        }
        .work-id-badge {
            position: absolute;
            top: 12px;
            left: 12px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            padding: 6px 16px;
            border: none;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
            cursor: pointer;
            z-index: 5;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }
        .work-id-badge:hover {
            transform: translateY(-1px);
            box-shadow: 0 8px 18px rgba(102, 126, 234, 0.35);
        }
        .work-content {
            padding: 25px;
        }
        .name-section {
            margin-bottom: 20px;
            padding-bottom: 18px;
            border-bottom: 2px solid #f0f0f5;
        }
        .name-row {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 8px;
        }
        .name-label {
            font-size: 0.8em;
            color: #999;
            min-width: 50px;
        }
        .name-text {
            font-size: 1.3em;
            color: #333;
            font-weight: bold;
            line-height: 1.4;
            flex: 1;
        }
        .name-translated-text {
            font-size: 1.15em;
            color: #555;
            line-height: 1.4;
            flex: 1;
            border-bottom: 1px dashed #ccc;
            min-height: 1.5em;
            outline: none;
        }
        .copy-btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 6px 14px;
            border-radius: 16px;
            cursor: pointer;
            font-size: 0.8em;
            transition: all 0.3s;
            white-space: nowrap;
        }
        .copy-btn:hover {
            transform: scale(1.05);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
        }
        .copy-btn.copied {
            background: linear-gradient(135deg, #11998e, #38ef7d);
        }
        .maker-name {
            font-size: 0.95em;
            color: #888;
            margin-bottom: 5px;
        }
        .work-kind-row {
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 6px;
            color: #777;
            font-size: 0.82em;
            margin: 8px 0 2px;
        }
        .kind-pill {
            background: #eef0ff;
            color: #5a4fcf;
            border: 1px solid #d8dcff;
            border-radius: 999px;
            padding: 3px 8px;
            line-height: 1.2;
        }
        .work-action-row {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
            margin: 10px 0 14px;
        }
        .state-btn {
            border: 1px solid #d8dcff;
            background: #f5f6ff;
            color: #5a4fcf;
            border-radius: 999px;
            padding: 6px 12px;
            font-size: 0.82em;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .state-btn:hover {
            background: #eef0ff;
            transform: translateY(-1px);
        }
        .asmr-link-btn {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            border: 1px solid rgba(17, 153, 142, 0.32);
            background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
            color: #fff;
            text-decoration: none;
            border-radius: 999px;
            padding: 6px 12px;
            font-size: 0.82em;
            font-weight: 600;
            box-shadow: 0 8px 18px rgba(17, 153, 142, 0.18);
            transition: transform 0.2s ease, box-shadow 0.2s ease, filter 0.2s ease;
        }
        .asmr-link-btn::after {
            content: '↗';
            font-size: 0.9em;
            line-height: 1;
            opacity: 0.86;
        }
        .asmr-link-btn:hover {
            transform: translateY(-1px);
            box-shadow: 0 10px 22px rgba(17, 153, 142, 0.28);
            filter: saturate(1.05);
        }
        .state-btn.active-like {
            background: #e7fff3;
            border-color: #71d8a1;
            color: #087a3f;
        }
        .state-btn.active-dislike {
            background: #fff0f0;
            border-color: #efa3a3;
            color: #a33434;
        }
        .state-btn.active-played {
            background: #eef7ff;
            border-color: #8fc8f2;
            color: #1b6294;
        }
        .state-pill {
            border-radius: 999px;
            padding: 5px 10px;
            font-size: 0.78em;
            background: #f0f0f5;
            color: #666;
        }
        .state-pill.read {
            background: #eef7ff;
            color: #2d6594;
        }
        .empty-state {
            grid-column: 1 / -1;
            color: rgba(255,255,255,0.9);
            text-align: center;
            padding: 70px 20px;
            font-size: 1.1em;
        }
        .section-title {
            font-size: 1.05em;
            color: #5a4fcf;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            font-weight: 600;
        }
        .section-title::before {
            content: '';
            display: inline-block;
            width: 4px;
            height: 18px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            margin-right: 10px;
            border-radius: 2px;
        }
        .description-section {
            margin-bottom: 18px;
        }
        .description-text {
            color: #555;
            line-height: 1.8;
            font-size: 0.95em;
        }
        .intro-section {
            background: #f8f9ff;
            padding: 18px;
            border-radius: 12px;
            max-height: 400px;
            overflow-y: auto;
        }
        .intro-content {
            color: #444;
            line-height: 1.8;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .intro-content::-webkit-scrollbar { width: 6px; }
        .intro-content::-webkit-scrollbar-track { background: #f1f1f1; border-radius: 3px; }
        .intro-content::-webkit-scrollbar-thumb { background: #c5c5e0; border-radius: 3px; }
        .parts-heading {
            color: #5a4fcf;
            font-weight: 600;
            margin-top: 14px;
            margin-bottom: 6px;
            font-size: 1em;
        }
        .parts-image {
            max-width: 100%;
            border-radius: 8px;
            margin: 8px 0;
            cursor: pointer;
            transition: transform 0.2s;
        }
        .parts-image:hover {
            transform: scale(1.02);
        }
        .modal {
            display: none;
            position: fixed;
            top: 0; left: 0;
            width: 100%; height: 100%;
            background: rgba(0, 0, 0, 0.92);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal.active { display: flex; }
        .modal img {
            max-width: 92%;
            max-height: 92%;
            object-fit: contain;
            border-radius: 8px;
        }
        .modal-close {
            position: absolute;
            top: 20px; right: 30px;
            color: white;
            font-size: 40px;
            cursor: pointer;
            z-index: 1001;
        }
        .floating-actions {
            position: fixed;
            right: 22px;
            bottom: 22px;
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            justify-content: flex-end;
            z-index: 900;
            max-width: min(420px, calc(100vw - 44px));
        }
        .floating-btn {
            border: none;
            border-radius: 999px;
            padding: 12px 16px;
            background: linear-gradient(135deg, #11998e, #38ef7d);
            color: #fff;
            font-weight: 600;
            cursor: pointer;
            box-shadow: 0 10px 28px rgba(0,0,0,0.26);
            transition: transform 0.2s ease, box-shadow 0.2s ease, opacity 0.2s ease;
        }
        .floating-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 14px 34px rgba(0,0,0,0.3);
        }
        .floating-btn.secondary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }
        .floating-btn.active {
            background: linear-gradient(135deg, #e25555 0%, #a33434 100%);
        }
        @media (max-width: 768px) {
            .works-grid { grid-template-columns: 1fr; }
            h1 { font-size: 1.8em; }
            .top-bar {
                display: block;
                min-height: 0;
            }
            .style-switcher {
                position: relative;
                left: auto;
                top: auto;
                transform: none;
                justify-content: center;
                max-width: none;
                margin-top: 14px;
            }
            .search-box {
                position: relative;
                right: auto;
                top: auto;
                transform: none;
                width: 100%;
                min-width: 0;
                margin-top: 18px;
            }
            .image-carousel { height: 250px; }
            .category-bar { justify-content: flex-start; }
            .floating-actions { left: 12px; right: 12px; bottom: 12px; }
        }
        ''' + style_extra_css + '''
    </style>
</head>
<body class="''' + escape_html(body_class) + '''">
    <div class="container">
        <div class="top-bar">
            <h1>作品展示</h1>
            <nav class="style-switcher" aria-label="切换 UI">
                ''' + style_switcher_html + '''
            </nav>
            <div class="search-box" role="search">
                <svg class="search-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                    <circle cx="11" cy="11" r="7"></circle>
                    <path d="m16.5 16.5 4.5 4.5"></path>
                </svg>
                <input class="search-input" id="searchInput" type="search" placeholder="搜索 RJ / 标题 / 社团" autocomplete="off" oninput="queueSearch(this.value)">
                <button class="search-clear" id="searchClear" type="button" onclick="clearSearch()" aria-label="清空搜索">&times;</button>
            </div>
        </div>
        <div class="category-bar toolbar">
            <div class="control-group">
                <span class="category-label">分类</span>
                <div class="category-picker" id="categoryPicker">
                    <button class="category-select category-select-button" id="categorySelectButton" type="button" onclick="toggleCategoryPicker()" aria-haspopup="listbox" aria-expanded="false">
                        <span class="category-select-label" id="categorySelectLabel">全部作品</span>
                        <span class="category-select-arrow" aria-hidden="true"></span>
                    </button>
                    <div class="category-dropdown" id="categoryDropdown" hidden>
                        <div class="category-search-wrap">
                            <input class="category-search-input" id="categorySearchInput" type="search" placeholder="搜索分类" autocomplete="off" oninput="updateCategorySearch(this.value)">
                        </div>
                        <div class="category-option-list" id="categoryOptionList" role="listbox"></div>
                    </div>
                </div>
            </div>
            <div class="control-group">
                <span class="category-label">作品类型</span>
                <div class="work-type-buttons" id="workTypeButtons"></div>
            </div>
            <span class="category-meta" id="categoryMeta"></span>
            <span class="category-meta" id="filterMeta"></span>
        </div>
        <div class="pagination" id="pagination"></div>
        <div class="works-grid" id="worksGrid"></div>
        <div class="pagination" id="paginationBottom"></div>
    </div>
    <div class="floating-actions">
        <button class="floating-btn" onclick="markCurrentPageRead()">已加载内容已阅</button>
        <button class="floating-btn secondary" onclick="unmarkCurrentPageRead()">取消已加载已阅</button>
        <button class="floating-btn secondary" id="hideReadToggle" onclick="toggleHideRead()">隐藏已阅：关</button>
        <button class="floating-btn secondary" id="showAllVersionsToggle" onclick="toggleShowAllVersions()">全部版本：关</button>
    </div>
    <div class="modal" id="imageModal" onclick="closeModal()">
        <span class="modal-close">&times;</span>
        <img src="" id="modalImage">
    </div>
    <script>
        const FALLBACK_TOTAL_WORKS = ''' + str(total_works) + ''';
        const FALLBACK_TOTAL_PAGES = ''' + str(total_pages) + ''';
        const ITEMS_PER_PAGE = ''' + str(ITEMS_PER_PAGE) + ''';
        const LAYOUT_MODE = ''' + json.dumps(layout_mode, ensure_ascii=False) + ''';
        let categories = [];
        let currentCategory = null;
        let currentPage = 0;
        let currentData = [];
        let isLoadingMore = false;
        let infiniteScrollObserver = null;
        let loadSequence = 0;
        let indexedResultEntries = null;
        let searchIndex = null;
        let searchIndexPromise = null;
        let searchQuery = '';
        let searchTerms = [];
        let searchDebounceTimer = null;
        let searchResultEntries = null;
        let categorySearchQuery = '';
        const pageDataCache = new Map();
        const filterIndexCache = new Map();
        let currentTotalWorks = FALLBACK_TOTAL_WORKS;
        let currentTotalPages = FALLBACK_TOTAL_PAGES;
        let activeWorkTypes = [];
        let hideReadWorks = false;
        let showAllVersions = localStorage.getItem('dlsiteShowAllVersions.v1') === '1';
        const STATUS_CATEGORY_LIKED = '__liked__';
        const STATUS_CATEGORY_DISLIKED = '__disliked__';
        const STATUS_CATEGORY_PLAYED = '__played__';
        const STATUS_CATEGORIES = {
            [STATUS_CATEGORY_LIKED]: { name: '喜欢', preference: 'liked', searchText: 'xihuan xh' },
            [STATUS_CATEGORY_DISLIKED]: { name: '不需要', preference: 'disliked', searchText: 'buxuyao bxy' },
            [STATUS_CATEGORY_PLAYED]: { name: '玩过', preference: 'played', searchText: 'wanguo wg' }
        };
        const HIDDEN_PREFERENCES = ['liked', 'disliked', 'played'];
        let workStates = {};

        async function initWorkStates() {
            workStates = await loadWorkStates();
            if (Object.keys(workStates).length === 0) {
                const legacy = loadLegacyWorkStates();
                if (Object.keys(legacy).length > 0) {
                    workStates = legacy;
                    saveWorkStates();
                }
            }
        }

        function loadLegacyWorkStates() {
            try {
                const saved = localStorage.getItem('dlsiteWorkStates.v1');
                const parsed = saved ? JSON.parse(saved) : {};
                return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
            } catch (e) {
                return {};
            }
        }

        async function loadCategories() {
            try {
                const resp = await fetch('data/categories.json');
                if (resp.ok) {
                    categories = await resp.json();
                }
            } catch (e) {
                categories = [];
            }

            if (!Array.isArray(categories) || categories.length === 0) {
                categories = [{
                    name: '全部作品',
                    slug: '__all__',
                    count: FALLBACK_TOTAL_WORKS,
                    pages: FALLBACK_TOTAL_PAGES,
                    data_path: 'data/json/page_',
                    index_path: 'data/filter_index/__all__.json',
                    work_kinds: ['有字幕ASMR', '无字幕ASMR', '音声・ASMR', '漫画', '游戏']
                }];
            }

            renderCategorySelect();
            await changeCategory(categories[0].slug);
        }

        function renderCategorySelect() {
            updateCategoryPickerLabel();
            renderCategoryOptionList();
        }

        function getCategoryOptions() {
            const categoryOptions = categories.map((category) => ({
                slug: category.slug,
                name: category.name,
                count: Number(category.count) || 0,
                title: category.source_url || '',
                searchText: normalizeSearchText(category.search_text || '')
            }));
            const statusOptions = Object.entries(STATUS_CATEGORIES).map(([slug, config]) => ({
                slug,
                name: config.name,
                count: countPreference(config.preference),
                title: '',
                searchText: config.searchText || ''
            }));
            return categoryOptions.concat(statusOptions);
        }

        function updateCategoryPickerLabel() {
            const label = document.getElementById('categorySelectLabel');
            if (!label) return;
            if (currentCategory) {
                const count = STATUS_CATEGORIES[currentCategory.slug]
                    ? countPreference(STATUS_CATEGORIES[currentCategory.slug].preference)
                    : (Number(currentCategory.count) || 0);
                label.textContent = currentCategory.name + ' (' + count + ')';
                return;
            }
            const fallback = categories[0];
            label.textContent = fallback
                ? (fallback.name + ' (' + (Number(fallback.count) || 0) + ')')
                : '全部作品';
        }

        function getFilteredCategoryOptions() {
            const options = getCategoryOptions();
            const terms = categorySearchQuery ? categorySearchQuery.split(' ').filter(Boolean) : [];
            if (terms.length === 0) return options;
            return options.filter((option) => {
                const haystack = normalizeSearchText(option.name + ' ' + option.slug + ' ' + (option.searchText || ''));
                return terms.every((term) => haystack.includes(term));
            });
        }

        function renderCategoryOptionList() {
            const list = document.getElementById('categoryOptionList');
            if (!list) return;

            const selected = currentCategory ? currentCategory.slug : '';
            const options = getFilteredCategoryOptions();
            list.innerHTML = '';

            if (options.length === 0) {
                const empty = document.createElement('div');
                empty.className = 'category-option-empty';
                empty.textContent = '没有匹配的分类';
                list.appendChild(empty);
                return;
            }

            options.forEach((option) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'category-option' + (option.slug === selected ? ' active' : '');
                button.setAttribute('role', 'option');
                button.setAttribute('aria-selected', option.slug === selected ? 'true' : 'false');
                if (option.title) {
                    button.title = option.title;
                }

                const name = document.createElement('span');
                name.className = 'category-option-name';
                name.textContent = option.name;

                const count = document.createElement('span');
                count.className = 'category-option-count';
                count.textContent = option.count;

                button.appendChild(name);
                button.appendChild(count);
                button.onclick = async () => {
                    closeCategoryPicker();
                    await changeCategory(option.slug);
                };
                list.appendChild(button);
            });
        }

        function updateCategorySearch(value) {
            categorySearchQuery = normalizeSearchText(value);
            renderCategoryOptionList();
        }

        function openCategoryPicker() {
            const picker = document.getElementById('categoryPicker');
            const button = document.getElementById('categorySelectButton');
            const dropdown = document.getElementById('categoryDropdown');
            const input = document.getElementById('categorySearchInput');
            if (!picker || !button || !dropdown || !input) return;
            picker.classList.add('open');
            button.classList.add('open');
            button.setAttribute('aria-expanded', 'true');
            dropdown.hidden = false;
            renderCategoryOptionList();
            requestAnimationFrame(() => input.focus());
        }

        function closeCategoryPicker() {
            const picker = document.getElementById('categoryPicker');
            const button = document.getElementById('categorySelectButton');
            const dropdown = document.getElementById('categoryDropdown');
            const input = document.getElementById('categorySearchInput');
            if (!picker || !button || !dropdown || !input) return;
            picker.classList.remove('open');
            button.classList.remove('open');
            button.setAttribute('aria-expanded', 'false');
            dropdown.hidden = true;
            if (categorySearchQuery || input.value) {
                categorySearchQuery = '';
                input.value = '';
                renderCategoryOptionList();
            }
        }

        function toggleCategoryPicker() {
            const dropdown = document.getElementById('categoryDropdown');
            if (!dropdown || dropdown.hidden) {
                openCategoryPicker();
                return;
            }
            closeCategoryPicker();
        }

        function normalizeSearchText(value) {
            return String(value || '').normalize('NFKC').toLowerCase().replace(/\\s+/g, ' ').trim();
        }

        function isSearchActive() {
            return searchTerms.length > 0;
        }

        function invalidateSearchResults() {
            searchResultEntries = null;
            indexedResultEntries = null;
        }

        function updateSearchClearButton() {
            const btn = document.getElementById('searchClear');
            if (!btn) return;
            btn.classList.toggle('visible', Boolean(document.getElementById('searchInput').value.trim()));
        }

        function queueSearch(value) {
            updateSearchClearButton();
            clearTimeout(searchDebounceTimer);
            searchDebounceTimer = setTimeout(() => applySearch(value), 140);
        }

        async function clearSearch() {
            const input = document.getElementById('searchInput');
            input.value = '';
            updateSearchClearButton();
            await applySearch('');
        }

        async function applySearch(value) {
            const nextQuery = normalizeSearchText(value);
            if (nextQuery === searchQuery) return;

            searchQuery = nextQuery;
            searchTerms = searchQuery ? searchQuery.split(' ').filter(Boolean) : [];
            invalidateSearchResults();
            await resetInfiniteList(true);
        }

        async function loadSearchIndex() {
            if (searchIndex) return searchIndex;
            if (searchIndexPromise) return searchIndexPromise;

            searchIndexPromise = fetch('data/search_index.json')
                .then((resp) => resp.ok ? resp.json() : [])
                .then((data) => Array.isArray(data) ? data : [])
                .catch(() => []);

            searchIndex = await searchIndexPromise;
            return searchIndex;
        }

        async function loadDataPage(dataPath, page) {
            const key = dataPath + page;
            if (!pageDataCache.has(key)) {
                pageDataCache.set(
                    key,
                    fetch(dataPath + page + '.json')
                        .then((resp) => resp.ok ? resp.json() : [])
                        .then((data) => Array.isArray(data) ? data : [])
                        .catch(() => [])
                );
            }
            return pageDataCache.get(key);
        }

        async function loadCurrentFilterIndex() {
            const dataCategory = getDataCategory();
            const indexPath = dataCategory && dataCategory.index_path ? dataCategory.index_path : '';
            if (!indexPath) return [];

            if (!filterIndexCache.has(indexPath)) {
                filterIndexCache.set(
                    indexPath,
                    fetch(indexPath)
                        .then((resp) => resp.ok ? resp.json() : [])
                        .then((data) => Array.isArray(data) ? data : [])
                        .catch(() => [])
                );
            }
            return filterIndexCache.get(indexPath);
        }

        function entryMatchesIndexedContext(entry) {
            if (hasActiveWorkTypeFilters() && !activeWorkTypes.includes(entry.kind || '游戏')) {
                return false;
            }

            const state = getWorkState(entry.product_id);
            if (isStatusCategory()) {
                if (state.preference !== currentCategory.status_filter) return false;
            } else if (HIDDEN_PREFERENCES.includes(state.preference)) {
                return false;
            }

            if (hideReadWorks && state.read) return false;
            return true;
        }

        function compareVersionEntries(a, b) {
            const rankA = Number.isFinite(Number(a.version_rank)) ? Number(a.version_rank) : 2;
            const rankB = Number.isFinite(Number(b.version_rank)) ? Number(b.version_rank) : 2;
            if (rankA !== rankB) return rankA - rankB;
            if (a.page !== b.page) return a.page - b.page;
            return a.index - b.index;
        }

        function dedupeVersionEntries(entries) {
            if (showAllVersions) return entries;

            const grouped = new Map();
            entries.forEach((entry, order) => {
                const key = entry.version_group_id || entry.product_id;
                const current = grouped.get(key);
                if (!current) {
                    grouped.set(key, { firstOrder: order, entry });
                    return;
                }
                if (compareVersionEntries(entry, current.entry) < 0) {
                    current.entry = entry;
                }
            });

            return Array.from(grouped.values())
                .sort((a, b) => a.firstOrder - b.firstOrder)
                .map((item) => item.entry);
        }

        function entryMatchesSearchContext(entry) {
            if (!entryMatchesIndexedContext(entry)) return false;
            if (!isStatusCategory()) {
                if (currentCategory && currentCategory.slug !== '__all__') {
                    const entryCategories = Array.isArray(entry.categories) ? entry.categories : [];
                    if (!entryCategories.includes(currentCategory.slug)) return false;
                }
            }
            return true;
        }

        async function getSearchResults() {
            if (searchResultEntries) return searchResultEntries;
            if (!isSearchActive()) {
                searchResultEntries = [];
                return searchResultEntries;
            }

            const index = await loadSearchIndex();
            searchResultEntries = dedupeVersionEntries(index.filter((entry) => {
                if (!entryMatchesSearchContext(entry)) return false;
                const text = entry.text || '';
                const aliases = entry.aliases || '';
                return searchTerms.every((term) => text.includes(term) || aliases.includes(term));
            }));
            return searchResultEntries;
        }

        async function loadSearchSourcePage(page) {
            return loadDataPage('data/json/page_', page);
        }

        async function loadSearchPageData(page) {
            const results = await getSearchResults();
            currentTotalWorks = results.length;
            currentTotalPages = Math.max(1, Math.ceil(results.length / ITEMS_PER_PAGE));

            const start = (page - 1) * ITEMS_PER_PAGE;
            const entries = results.slice(start, start + ITEMS_PER_PAGE);
            const works = new Array(entries.length);
            const grouped = new Map();

            entries.forEach((entry, resultIndex) => {
                if (!grouped.has(entry.page)) grouped.set(entry.page, []);
                grouped.get(entry.page).push({ entry, resultIndex });
            });

            await Promise.all(Array.from(grouped.entries()).map(async ([sourcePage, items]) => {
                const pageData = await loadSearchSourcePage(sourcePage);
                items.forEach(({ entry, resultIndex }) => {
                    works[resultIndex] = pageData[entry.index];
                });
            }));

            return works.filter(Boolean);
        }

        async function getIndexedResults() {
            if (indexedResultEntries) return indexedResultEntries;
            const index = await loadCurrentFilterIndex();
            indexedResultEntries = dedupeVersionEntries(index.filter(entryMatchesIndexedContext));
            return indexedResultEntries;
        }

        async function loadIndexedPageData(page) {
            const results = await getIndexedResults();
            currentTotalWorks = results.length;
            currentTotalPages = Math.max(1, Math.ceil(results.length / ITEMS_PER_PAGE));

            const dataCategory = getDataCategory();
            const dataPath = dataCategory ? dataCategory.data_path : 'data/json/page_';
            const start = (page - 1) * ITEMS_PER_PAGE;
            const entries = results.slice(start, start + ITEMS_PER_PAGE);
            const works = new Array(entries.length);
            const grouped = new Map();

            entries.forEach((entry, resultIndex) => {
                if (!grouped.has(entry.page)) grouped.set(entry.page, []);
                grouped.get(entry.page).push({ entry, resultIndex });
            });

            await Promise.all(Array.from(grouped.entries()).map(async ([sourcePage, items]) => {
                const pageData = await loadDataPage(dataPath, sourcePage);
                items.forEach(({ entry, resultIndex }) => {
                    works[resultIndex] = pageData[entry.index];
                });
            }));

            return works.filter(Boolean);
        }

        function loadWorkStates() {
            return fetch('/api/work-states')
                .then(r => r.json())
                .then(data => data && typeof data === 'object' && !Array.isArray(data) ? data : {})
                .catch(() => ({}));
        }

        function saveWorkStates() {
            fetch('/api/work-states', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(workStates)
            }).catch(() => {});
        }

        function getWorkState(workId) {
            return workStates[workId] || {};
        }

        function setWorkState(workId, nextState) {
            if (!nextState.preference) delete nextState.preference;
            if (!nextState.read) delete nextState.read;
            if (!nextState.preference && !nextState.read) {
                delete workStates[workId];
            } else {
                workStates[workId] = nextState;
            }
        }

        function hasPreferenceStates() {
            return Object.values(workStates).some((state) => HIDDEN_PREFERENCES.includes(state.preference));
        }

        function countPreference(preference) {
            return Object.values(workStates).filter((state) => state.preference === preference).length;
        }

        function getBaseCategory() {
            return categories.find((category) => category.slug === '__all__') || categories[0];
        }

        function isStatusCategory() {
            return currentCategory && Boolean(currentCategory.status_filter);
        }

        function createStatusCategory(slug) {
            const config = STATUS_CATEGORIES[slug];
            const base = getBaseCategory();
            return {
                name: config.name,
                slug,
                count: countPreference(config.preference),
                pages: Math.max(1, Math.ceil(countPreference(config.preference) / ITEMS_PER_PAGE)),
                data_path: base.data_path,
                work_kinds: base.work_kinds || [],
                status_filter: config.preference
            };
        }

        function getDataCategory() {
            return isStatusCategory() ? getBaseCategory() : (currentCategory || getBaseCategory());
        }

        function renderWorkTypeButtons() {
            const container = document.getElementById('workTypeButtons');
            const meta = document.getElementById('filterMeta');
            const workTypes = currentCategory && Array.isArray(currentCategory.work_kinds)
                ? currentCategory.work_kinds
                : [];

            container.innerHTML = '';
            workTypes.forEach((workType) => {
                const btn = document.createElement('button');
                btn.className = 'work-type-btn' + (activeWorkTypes.includes(workType) ? ' active' : '');
                btn.textContent = workType;
                btn.onclick = () => toggleWorkType(workType);
                container.appendChild(btn);
            });

            if (meta) {
                if (workTypes.length === 0) {
                    meta.textContent = '没有作品类型数据';
                } else if (activeWorkTypes.length > 0) {
                    meta.textContent = '已选 ' + activeWorkTypes.length + ' 项';
                } else {
                    meta.textContent = '';
                }
            }
        }

        function toggleWorkType(workType) {
            const idx = activeWorkTypes.indexOf(workType);
            if (idx >= 0) {
                activeWorkTypes.splice(idx, 1);
            } else {
                activeWorkTypes.push(workType);
            }
            renderWorkTypeButtons();
            invalidateSearchResults();
            resetInfiniteList(true);
        }

        async function changeCategory(slug) {
            currentCategory = STATUS_CATEGORIES[slug]
                ? createStatusCategory(slug)
                : (categories.find((category) => category.slug === slug) || categories[0]);
            activeWorkTypes = [];
            invalidateSearchResults();
            renderCategorySelect();
            renderWorkTypeButtons();
            await resetInfiniteList(true);
        }

        async function loadPageData(page) {
            if (isSearchActive()) {
                return await loadSearchPageData(page);
            }

            const dataCategory = getDataCategory();
            const dataPath = dataCategory ? dataCategory.data_path : 'data/json/page_';
            if (requiresFullDataFiltering()) {
                return await loadIndexedPageData(page);
            }

            currentTotalWorks = dataCategory ? dataCategory.count : FALLBACK_TOTAL_WORKS;
            currentTotalPages = dataCategory ? dataCategory.pages : FALLBACK_TOTAL_PAGES;
            const pageData = await loadDataPage(dataPath, page);
            if (!Array.isArray(pageData)) {
                console.error('加载第 ' + page + ' 页数据失败');
                return null;
            }
            return pageData;
        }

        function hasActiveWorkTypeFilters() {
            return activeWorkTypes.length > 0;
        }

        function requiresFullDataFiltering() {
            return !showAllVersions || hasActiveWorkTypeFilters() || isStatusCategory() || hasPreferenceStates() || hideReadWorks;
        }

        function copyText(text, btn) {
            navigator.clipboard.writeText(text).then(() => {
                const orig = btn.innerHTML;
                btn.innerHTML = '✓';
                btn.classList.add('copied');
                setTimeout(() => { btn.innerHTML = orig; btn.classList.remove('copied'); }, 1200);
            }).catch(() => {
                const ta = document.createElement('textarea');
                ta.value = text;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                const orig = btn.innerHTML;
                btn.innerHTML = '✓';
                btn.classList.add('copied');
                setTimeout(() => { btn.innerHTML = orig; btn.classList.remove('copied'); }, 1200);
            });
        }

        function openModal(src) {
            document.getElementById('modalImage').src = src;
            document.getElementById('imageModal').classList.add('active');
        }

        function closeModal() {
            document.getElementById('imageModal').classList.remove('active');
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                closeModal();
                closeCategoryPicker();
            }
        });

        document.addEventListener('click', (e) => {
            const picker = document.getElementById('categoryPicker');
            if (!picker || picker.contains(e.target)) return;
            closeCategoryPicker();
        });

        function hydrateCarouselSlide(slide) {
            if (!slide) return;
            const img = slide.querySelector('img[data-src]');
            if (!img) return;
            img.src = img.dataset.src;
            img.removeAttribute('data-src');
        }

        function slideImages(card, direction) {
            const track = card.querySelector('.carousel-track');
            const slides = track.querySelectorAll('.carousel-slide');
            const total = slides.length;
            let current = parseInt(track.dataset.current || '0');
            current += direction;
            if (current < 0) current = total - 1;
            if (current >= total) current = 0;
            hydrateCarouselSlide(slides[current]);
            track.dataset.current = current;
            track.style.transform = 'translateX(-' + (current * 100) + '%)';
            const counter = card.querySelector('.carousel-counter');
            if (counter) counter.textContent = (current + 1) + ' / ' + total;
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function formatText(text) {
            let html = escapeHtml(text);
            html = html.replace(/\\n/g, '<br>');
            const urlRegex = /https?:\\/\\/[^\\s<]+/g;
            html = html.replace(urlRegex, function(url) {
                return '<a href="' + url + '" target="_blank" rel="noopener" style="color:#667eea;word-break:break-all;">' + url + '</a>';
            });
            return html;
        }

        function getWorkCardClass(idx) {
            const classes = ['work-card', 'layout-' + LAYOUT_MODE];
            if (LAYOUT_MODE === 'gallery' && idx % 7 === 0) {
                classes.push('hero-card');
            }
            return classes.join(' ');
        }

        function isAudioWork(work) {
            const kind = String((work && work.work_kind) || '');
            return kind.includes('ASMR') || kind.includes('音声') || kind.includes('ボイス');
        }

        function buildAsmrOneUrl(work) {
            const productId = String((work && work.product_id) || '').trim().toUpperCase();
            if (!productId) return '';
            return 'https://www.asmr.one/work/' + encodeURIComponent(productId);
        }

        async function setWorkPreference(workId, preference) {
            const state = { ...getWorkState(workId) };
            state.preference = state.preference === preference ? '' : preference;
            setWorkState(workId, state);
            saveWorkStates();
            renderCategorySelect();
            invalidateSearchResults();
            await resetInfiniteList(true);
        }

        async function markCurrentPageRead() {
            if (!Array.isArray(currentData) || currentData.length === 0) return;
            currentData.forEach((work) => {
                const state = { ...getWorkState(work.product_id), read: true };
                setWorkState(work.product_id, state);
            });
            saveWorkStates();
            renderCategorySelect();
            invalidateSearchResults();
            await resetInfiniteList(true);
        }

        async function unmarkCurrentPageRead() {
            if (!Array.isArray(currentData) || currentData.length === 0) return;
            currentData.forEach((work) => {
                const state = { ...getWorkState(work.product_id), read: false };
                setWorkState(work.product_id, state);
            });
            saveWorkStates();
            renderCategorySelect();
            invalidateSearchResults();
            await resetInfiniteList(true);
        }

        async function toggleHideRead() {
            hideReadWorks = !hideReadWorks;
            updateFloatingControls();
            invalidateSearchResults();
            await resetInfiniteList(true);
        }

        async function toggleShowAllVersions() {
            showAllVersions = !showAllVersions;
            localStorage.setItem('dlsiteShowAllVersions.v1', showAllVersions ? '1' : '0');
            updateFloatingControls();
            invalidateSearchResults();
            await resetInfiniteList(true);
        }

        function updateFloatingControls() {
            const hideBtn = document.getElementById('hideReadToggle');
            if (hideBtn) {
                hideBtn.textContent = hideReadWorks ? '隐藏已阅：开' : '隐藏已阅：关';
                hideBtn.classList.toggle('active', hideReadWorks);
            }
            const versionBtn = document.getElementById('showAllVersionsToggle');
            if (versionBtn) {
                versionBtn.textContent = showAllVersions ? '全部版本：开' : '全部版本：关';
                versionBtn.classList.toggle('active', showAllVersions);
            }
        }

        function renderEmptyState() {
            const grid = document.getElementById('worksGrid');
            grid.innerHTML = '<div class="empty-state">没有符合筛选条件的作品</div>';
            renderPagination();
            updateFloatingControls();
        }

        function appendWorks(pageData, page) {
            const grid = document.getElementById('worksGrid');
            const startIndex = currentData.length;

            for (let idx = 0; idx < pageData.length; idx++) {
                const work = pageData[idx];
                const globalIdx = (page - 1) * ITEMS_PER_PAGE + idx;
                const layoutIdx = startIndex + idx;
                const card = document.createElement('div');
                card.className = getWorkCardClass(layoutIdx);

                let imagesHtml = '';
                work.slider_images.forEach((img, imageIndex) => {
                    const imageAttr = imageIndex === 0
                        ? 'src="' + escapeHtml(img) + '"'
                        : 'data-src="' + escapeHtml(img) + '"';
                    imagesHtml += '<div class="carousel-slide"><img loading="lazy" decoding="async" ' + imageAttr + ' alt="' + escapeHtml(work.work_name) + '" onclick="openModal(this.src)"></div>';
                });

                let counterHtml = work.slider_images.length > 1
                    ? '<span class="carousel-counter">1 / ' + work.slider_images.length + '</span>'
                    : '';

                let buttonsHtml = work.slider_images.length > 1
                    ? '<button class="carousel-btn prev" onclick="slideImages(this.closest(\\'.work-card\\'), -1)">&#9664;</button><button class="carousel-btn next" onclick="slideImages(this.closest(\\'.work-card\\'), 1)">&#9654;</button>'
                    : '';

                const state = getWorkState(work.product_id);
                const likeActive = state.preference === 'liked';
                const dislikeActive = state.preference === 'disliked';
                const playedActive = state.preference === 'played';
                const kindHtml = '<div class="work-kind-row"><span>作品类型</span><span class="kind-pill">' + escapeHtml(work.work_kind || '游戏') + '</span></div>';
                const asmrOneUrl = isAudioWork(work) ? buildAsmrOneUrl(work) : '';
                const asmrButtonHtml = asmrOneUrl
                    ? '<a class="asmr-link-btn" href="' + escapeHtml(asmrOneUrl) + '" target="_blank" rel="noopener noreferrer" title="在 ASMR One 打开">ASMR One</a>'
                    : '';
                const actionHtml =
                    '<div class="work-action-row">' +
                        '<button class="state-btn ' + (likeActive ? 'active-like' : '') + '" onclick="setWorkPreference(\\'' + work.product_id + '\\', \\'liked\\')">喜欢</button>' +
                        '<button class="state-btn ' + (dislikeActive ? 'active-dislike' : '') + '" onclick="setWorkPreference(\\'' + work.product_id + '\\', \\'disliked\\')">不需要</button>' +
                        '<button class="state-btn ' + (playedActive ? 'active-played' : '') + '" onclick="setWorkPreference(\\'' + work.product_id + '\\', \\'played\\')">玩过</button>' +
                        asmrButtonHtml +
                        (state.read ? '<span class="state-pill read">已阅</span>' : '') +
                    '</div>';

                let partsHtml = '';
                work.parts.forEach(part => {
                    if (part.type === 'text') {
                        if (part.heading) {
                            partsHtml += '<div class="parts-heading">' + escapeHtml(part.heading) + '</div>';
                        }
                        partsHtml += '<div class="intro-content">' + formatText(part.content) + '</div>';
                    } else if (part.type === 'image') {
                        if (part.heading) {
                            partsHtml += '<div class="parts-heading">' + escapeHtml(part.heading) + '</div>';
                        }
                        if (part.local_path) {
                            partsHtml += '<img loading="lazy" class="parts-image" src="' + part.local_path + '" alt="' + escapeHtml(part.alt || part.heading || '') + '" onclick="openModal(this.src)" style="max-height:400px;">';
                        }
                    }
                });

                const useScrollReveal = LAYOUT_MODE === 'gallery' || LAYOUT_MODE === 'compact';
                const detailWrapStart = useScrollReveal ? '<div class="scroll-reveal">' : '';
                const detailWrapEnd = useScrollReveal ? '</div>' : '';

                card.innerHTML =
                    '<div class="image-carousel">' +
                        '<button class="work-id-badge" type="button" onclick="copyText(\\'' + work.product_id + '\\', this)" title="点击复制">' + work.product_id + '</button>' +
                        '<div class="carousel-track" data-current="0">' + imagesHtml + '</div>' +
                        buttonsHtml +
                        counterHtml +
                    '</div>' +
                    '<div class="work-content">' +
                        '<div class="name-section">' +
                            '<div class="maker-name">社团: ' + escapeHtml(work.maker_name) + '</div>' +
                            kindHtml +
                            actionHtml +
                            '<div class="name-row">' +
                                '<span class="name-label">原文</span>' +
                                '<span class="name-text" id="name-orig-' + globalIdx + '">' + escapeHtml(work.work_name) + '</span>' +
                                '<button class="copy-btn" onclick="copyText(document.getElementById(\\'name-orig-' + globalIdx + '\\').textContent, this)">复制</button>' +
                            '</div>' +
                            '<div class="name-row">' +
                                '<span class="name-label">译文</span>' +
                                '<span class="name-translated-text" id="name-trans-' + globalIdx + '">' + escapeHtml(work.work_name_trans || '') + '</span>' +
                                '<button class="copy-btn" onclick="copyText(document.getElementById(\\'name-trans-' + globalIdx + '\\').textContent, this)">复制</button>' +
                            '</div>' +
                        '</div>' +
                        detailWrapStart +
                            '<div class="description-section">' +
                                '<div class="section-title">简介</div>' +
                                '<div class="description-text">' + escapeHtml(work.description) + '</div>' +
                            '</div>' +
                            '<div class="intro-section">' +
                                '<div class="section-title">详细介绍</div>' + partsHtml +
                            '</div>' +
                        detailWrapEnd +
                    '</div>';

                grid.appendChild(card);
            }

            currentData = currentData.concat(pageData);
            renderPagination();
            updateFloatingControls();
        }

        function renderPagination() {
            const paginationTop = document.getElementById('pagination');
            const paginationBottom = document.getElementById('paginationBottom');
            const totalPages = currentTotalPages;
            const totalWorks = currentTotalWorks;
            const loadedWorks = currentData.length;
            const loadedText = loadedWorks + ' / ' + totalWorks + ' 个作品';
            const pageText = currentPage > 0 ? ('已加载 ' + currentPage + ' / ' + totalPages + ' 页') : '准备加载';
            const topHtml = '<span class="page-info">' + loadedText + '，' + pageText + '</span>';

            let bottomHtml = '';
            if (isLoadingMore) {
                bottomHtml = '<span class="page-info loading-dots">正在加载下一页</span>';
            } else if (loadedWorks === 0) {
                bottomHtml = '<span class="page-info">没有符合筛选条件的作品</span>';
            } else if (currentPage >= totalPages) {
                bottomHtml = '<span class="page-info">已经加载全部 ' + loadedWorks + ' 个作品</span>';
            } else {
                bottomHtml = '<span class="page-info">继续向下滚动，自动加载第 ' + (currentPage + 1) + ' 页</span>';
            }

            paginationTop.classList.add('infinite-summary');
            paginationBottom.classList.add('infinite-status');
            paginationTop.innerHTML = topHtml;
            paginationBottom.innerHTML = bottomHtml;
        }

        function updateCategoryMeta() {
            const meta = document.getElementById('categoryMeta');
            if (currentCategory && meta) {
                if (isSearchActive()) {
                    meta.textContent = currentTotalWorks + ' 个搜索结果 / 共 ' + currentTotalPages + ' 页';
                } else {
                    const filterText = requiresFullDataFiltering() ? '（筛选后）' : '';
                    meta.textContent = currentTotalWorks + ' 个作品 / 共 ' + currentTotalPages + ' 页' + filterText;
                }
            }
        }

        function setupInfiniteScroll() {
            const sentinel = document.getElementById('paginationBottom');
            if (!sentinel) return;
            if (infiniteScrollObserver) {
                infiniteScrollObserver.disconnect();
            }

            infiniteScrollObserver = new IntersectionObserver((entries) => {
                if (entries.some((entry) => entry.isIntersecting)) {
                    loadNextPage();
                }
            }, { rootMargin: '900px 0px 1200px', threshold: 0 });

            infiniteScrollObserver.observe(sentinel);
        }

        async function resetInfiniteList(keepScroll) {
            const sequence = ++loadSequence;
            currentPage = 0;
            currentData = [];
            isLoadingMore = false;
            document.getElementById('worksGrid').innerHTML = '';
            renderPagination();
            if (!keepScroll) {
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }
            await loadNextPage(sequence);
        }

        async function loadNextPage(expectedSequence) {
            const sequence = expectedSequence ?? loadSequence;
            if (sequence !== loadSequence || isLoadingMore) return;
            if (currentPage > 0 && currentPage >= currentTotalPages) {
                renderPagination();
                return;
            }
            await goToPage(currentPage + 1, true, sequence);
        }

        async function goToPage(page, keepScroll, expectedSequence) {
            const sequence = expectedSequence ?? ++loadSequence;
            const totalPages = (isSearchActive() || requiresFullDataFiltering())
                ? Number.MAX_SAFE_INTEGER
                : (currentCategory ? currentCategory.pages : FALLBACK_TOTAL_PAGES);
            if (page < 1 || page > totalPages) return;

            isLoadingMore = true;
            renderPagination();
            const pageData = await loadPageData(page);
            if (sequence !== loadSequence) return;

            if (page > currentTotalPages) {
                isLoadingMore = false;
                renderPagination();
                return;
            }

            currentPage = page;
            isLoadingMore = false;
            updateCategoryMeta();

            if (!Array.isArray(pageData) || pageData.length === 0) {
                if (currentData.length === 0) {
                    renderEmptyState();
                } else {
                    renderPagination();
                    updateFloatingControls();
                }
                return;
            }

            appendWorks(pageData, page);
            if (!keepScroll) {
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }
        }

        (async () => {
            await initWorkStates();
            await loadCategories();
            setupInfiniteScroll();
        })();
    </script>
</body>
</html>'''


async def download_all_images(session, works):
    """异步下载所有图片"""
    all_download_tasks = []
    reused_slider_paths = {}
    reused_part_paths = {}
    skipped_count = 0

    print("\n建立已有图片索引...")
    slider_image_index = build_reusable_image_index(SLIDER_IMAGES_DIR, "slider")
    parts_image_index = build_reusable_image_index(PARTS_IMAGES_DIR, "parts")
    print(f"已有图片索引: slider {len(slider_image_index)} 张，parts {len(parts_image_index)} 张")

    scan_started_at = time.time()
    total_works = len(works)
    
    for scan_idx, work in enumerate(works, start=1):
        work["description_clean"] = clean_description(work["description"])
        
        for i, img_url in enumerate(work["slider_images"]):
            fname = get_image_filename(img_url, work["product_id"], i, "slider")
            save_path = SLIDER_IMAGES_DIR / fname
            existing_path = resolve_existing_image_path(
                img_url,
                work["product_id"],
                i,
                "slider",
                SLIDER_IMAGES_DIR,
                slider_image_index,
            )
            if existing_path:
                skipped_count += 1
                reused_slider_paths[(work["product_id"], i)] = existing_path
            else:
                all_download_tasks.append({
                    "type": "slider",
                    "work_id": work["product_id"],
                    "index": i,
                    "url": img_url,
                    "save_path": save_path
                })
        
        for pi, part in enumerate(work["parts"]):
            if part["type"] == "image" and part.get("src"):
                fname = get_image_filename(part["src"], work["product_id"], pi, "parts")
                save_path = PARTS_IMAGES_DIR / fname
                existing_path = resolve_existing_image_path(
                    part["src"],
                    work["product_id"],
                    pi,
                    "parts",
                    PARTS_IMAGES_DIR,
                    parts_image_index,
                )
                if existing_path:
                    skipped_count += 1
                    reused_part_paths[(work["product_id"], pi)] = existing_path
                else:
                    all_download_tasks.append({
                        "type": "part",
                        "work_id": work["product_id"],
                        "index": pi,
                        "url": part["src"],
                        "save_path": save_path,
                        "part": part
                    })

        if scan_idx % IMAGE_SCAN_PROGRESS_INTERVAL == 0 or scan_idx == total_works:
            elapsed = max(0.001, time.time() - scan_started_at)
            speed = scan_idx / elapsed
            remaining = max(0, total_works - scan_idx)
            eta = remaining / speed if speed > 0 else 0
            sys.stdout.write(
                f"\r扫描作品图片: {scan_idx}/{total_works} "
                f"{scan_idx / max(1, total_works) * 100:5.1f}% "
                f"{speed:.1f}/s ETA:{format_seconds(eta)} "
                f"跳过:{skipped_count} 待下:{len(all_download_tasks)}"
            )
            sys.stdout.flush()

    sys.stdout.write("\n")
    
    total_images = len(all_download_tasks) + skipped_count
    print(f"\n共 {total_images} 张图片，跳过已下载 {skipped_count} 张，需下载 {len(all_download_tasks)} 张")
    
    if not all_download_tasks:
        print("所有图片都已下载完毕！")
        for work in works:
            local_slider = []
            for i, img_url in enumerate(work["slider_images"]):
                existing_path = reused_slider_paths.get((work["product_id"], i))
                if not existing_path:
                    fname = get_image_filename(img_url, work["product_id"], i, "slider")
                    save_path = SLIDER_IMAGES_DIR / fname
                    existing_path = output_relative_path(save_path)
                local_slider.append(existing_path)
            work["local_slider_images"] = local_slider
            for pi, part in enumerate(work["parts"]):
                if part["type"] == "image" and part.get("src"):
                    existing_path = reused_part_paths.get((work["product_id"], pi))
                    if not existing_path:
                        fname = get_image_filename(part["src"], work["product_id"], pi, "parts")
                        save_path = PARTS_IMAGES_DIR / fname
                        existing_path = output_relative_path(save_path)
                    part["local_path"] = existing_path
        return works
    
    progress_bar = DownloadProgressBar(len(all_download_tasks), desc="下载图片")
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_IMAGES)
    
    async def bounded_download(task):
        async with semaphore:
            local_path, success = await download_image(session, task["url"], task["save_path"])
            progress_bar.update(1, success=success, skipped=False)
            return {
                "task": task,
                "local_path": local_path,
                "success": success
            }
    
    download_results = await asyncio.gather(*[bounded_download(task) for task in all_download_tasks])
    progress_bar.close()
    
    # 把下载结果填回去
    slider_dict = {}
    
    for res in download_results:
        task = res["task"]
        if task["type"] == "slider":
            if task["work_id"] not in slider_dict:
                slider_dict[task["work_id"]] = []
            slider_dict[task["work_id"]].append({
                "index": task["index"],
                "path": res["local_path"]
            })
        elif task["type"] == "part":
            task["part"]["local_path"] = res["local_path"]
    
    # 填充轮播图路径（包括已跳过的）
    for work in works:
        if work["product_id"] in slider_dict:
            # 合并已下载和已跳过的
            existing_paths = {}
            for item in slider_dict[work["product_id"]]:
                existing_paths[item["index"]] = item["path"]
            local_slider = []
            for i, img_url in enumerate(work["slider_images"]):
                if i in existing_paths:
                    local_slider.append(existing_paths[i])
                elif (work["product_id"], i) in reused_slider_paths:
                    local_slider.append(reused_slider_paths[(work["product_id"], i)])
                else:
                    fname = get_image_filename(img_url, work["product_id"], i, "slider")
                    save_path = SLIDER_IMAGES_DIR / fname
                    local_slider.append(output_relative_path(save_path))
            work["local_slider_images"] = local_slider
        else:
            # 全部已跳过
            local_slider = []
            for i, img_url in enumerate(work["slider_images"]):
                existing_path = reused_slider_paths.get((work["product_id"], i))
                if not existing_path:
                    fname = get_image_filename(img_url, work["product_id"], i, "slider")
                    save_path = SLIDER_IMAGES_DIR / fname
                    existing_path = output_relative_path(save_path)
                local_slider.append(existing_path)
            work["local_slider_images"] = local_slider
        
        # 填充已跳过的 parts 图片路径
        for pi, part in enumerate(work["parts"]):
            if part["type"] == "image" and part.get("src") and "local_path" not in part:
                existing_path = reused_part_paths.get((work["product_id"], pi))
                if not existing_path:
                    fname = get_image_filename(part["src"], work["product_id"], pi, "parts")
                    save_path = PARTS_IMAGES_DIR / fname
                    existing_path = output_relative_path(save_path)
                part["local_path"] = existing_path
    
    return works


async def main():
    SLIDER_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    PARTS_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)
    FILTER_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    TRANSLATE_DIR.mkdir(parents=True, exist_ok=True)
    ORIG_DIR.mkdir(parents=True, exist_ok=True)

    html_files = find_work_html_files()
    if not html_files:
        print("未找到 RJ/VJ HTML 文件！")
        return

    # 按人气排序读取；works_order 可能只覆盖最近一次队列，crawl_results 可补齐旧分类顺序。
    html_files, order_counts = order_html_files_by_popularity(html_files)
    popularity_count = order_counts["works_order"] + order_counts["crawl_results"]
    if popularity_count:
        print(
            "按人气排序加载 "
            f"(works_order.json {order_counts['works_order']} 个，"
            f"crawl_results.json 兜底 {order_counts['crawl_results']} 个，"
            f"文件名兜底 {order_counts['filename']} 个)"
        )
    else:
        print("未找到人气排序数据，按文件名排序")

    print(f"找到 {len(html_files)} 个HTML文件\n")

    works = parse_html_files(html_files)
    share_version_group_images(works)
    
    # 异步下载所有图片
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_IMAGES)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        await refine_asmr_work_kinds(session, works, refresh_missing=ASMR_SUBTITLE_REFRESH)
        works = await download_all_images(session, works)

    total_pages = math.ceil(len(works) / ITEMS_PER_PAGE)
    print(f"\n共 {len(works)} 个作品，分 {total_pages} 页生成文件")

    write_work_markdown_files(works)
    write_paged_outputs(works, JSON_DIR, "全部作品")
    write_filter_index(works, FILTER_INDEX_DIR / "__all__.json", "全部作品")

    works_by_id = {work["product_id"]: work for work in works}
    manifest = [
        build_manifest_entry(
            "全部作品",
            "__all__",
            len(works),
            "data/json/page_",
            "data/translate/",
            work_kinds=collect_work_kinds(works),
            index_path="data/filter_index/__all__.json",
        )
    ]

    crawl_categories = load_crawl_categories(works_by_id)
    write_search_index(works, crawl_categories)
    cleanup_stale_category_dirs({category["slug"] for category in crawl_categories})
    cleanup_stale_filter_indexes({category["slug"] for category in crawl_categories})

    if crawl_categories:
        print(f"\n检测到 {len(crawl_categories)} 个爬取分类，开始生成分类数据")

    for category in crawl_categories:
        category_works = [works_by_id[work_id] for work_id in category["work_ids"]]
        json_dir = JSON_DIR / category["slug"]

        write_paged_outputs(
            category_works,
            json_dir,
            category["name"],
        )
        write_filter_index(
            category_works,
            FILTER_INDEX_DIR / f"{category['slug']}.json",
            category["name"],
        )

        manifest.append(build_manifest_entry(
            category["name"],
            category["slug"],
            len(category_works),
            f"data/json/{category['slug']}/page_",
            "data/translate/",
            category.get("source_url", ""),
            category.get("updated_at", ""),
            collect_work_kinds(category_works),
            index_path=f"data/filter_index/{category['slug']}.json",
        ))

    with open(CATEGORIES_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\n已生成分类索引: {CATEGORIES_FILE}")

    # 生成多套同功能展示页风格
    generated_pages = []
    for variant in STYLE_VARIANTS:
        html = generate_html(len(works), variant["key"])
        html_path = OUTPUT_DIR / variant["file"]
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        generated_pages.append((variant["name"], html_path))
    cleanup_stale_style_pages({variant["file"] for variant in STYLE_VARIANTS})

    print("\n已生成网页风格:")
    for name, html_path in generated_pages:
        print(f"  - {name}: {html_path}")

    print("\n完成！运行 open_page.py 查看结果")


if __name__ == "__main__":
    # 修复Windows上的Event loop is closed错误
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())
