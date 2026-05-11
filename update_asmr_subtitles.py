import asyncio
import sys
import time

import aiohttp

from generate import (
    ASMR_SUBTITLE_MAX_RETRIES,
    ASMR_SUBTITLE_RETRY_DELAY,
    HEADERS,
    WORKS_DIR,
    WORK_KIND_AUDIO_ASMR,
    build_asmr_subtitle_api_url,
    get_cached_asmr_subtitle,
    has_valid_asmr_subtitle_result,
    load_asmr_subtitle_cache,
    parse_html_file,
    save_asmr_subtitle_cache,
    set_cached_asmr_subtitle,
)


DEFAULT_CONCURRENCY = 3
AUTO_CONCURRENCY = "auto"
AUTO_MIN_CONCURRENCY = 1
AUTO_MAX_CONCURRENCY = 8
AUTO_START_CONCURRENCY = 2
AUTO_WINDOW_SIZE = 20
RATE_LIMIT_MIN_WAIT = 10
AUTO_RATE_LIMIT_COOLDOWN_WINDOWS = 3
AUTO_RAMP_UP_CLEAN_WINDOWS = 3


class ProgressBar:
    def __init__(self, total, label, width=32):
        self.total = max(1, total)
        self.label = label
        self.width = width
        self.current = 0
        self.start_time = time.time()
        self.last_line_length = 0
        self.fill_char, self.empty_char = self.get_bar_chars()

    @staticmethod
    def get_bar_chars():
        encoding = sys.stdout.encoding or "utf-8"
        try:
            "█░".encode(encoding)
            return "█", "░"
        except UnicodeEncodeError:
            return "#", "-"

    def update(self, current, subtitled, unsubtitled, failed, rate_limited=0):
        self.current = current
        elapsed = max(0.001, time.time() - self.start_time)
        speed = current / elapsed
        remaining = max(0, self.total - current)
        eta = remaining / speed if speed > 0 else 0
        ratio = min(1, current / self.total)
        filled = int(self.width * ratio)
        bar = self.fill_char * filled + self.empty_char * (self.width - filled)
        line = (
            f"\r{self.label}: |{bar}| {current}/{self.total} "
            f"{ratio * 100:5.1f}% "
            f"有字幕:{subtitled} 无字幕:{unsubtitled} 失败:{failed} 429:{rate_limited} "
            f"{speed:.2f}/s ETA:{format_duration(eta)}"
        )
        padding = " " * max(0, self.last_line_length - len(line))
        print(line + padding, end="", flush=True)
        self.last_line_length = len(line)

    def message(self, text):
        if self.last_line_length:
            print("\r" + " " * self.last_line_length + "\r", end="", flush=True)
            self.last_line_length = 0
        print(text, flush=True)

    def close(self):
        print()


def format_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m"


def read_input(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def prompt_choice():
    print("请选择要执行的功能：")
    print("  1. 补查未确认的音声 ASMR")
    print("  2. 复查已标为无字幕的音声 ASMR")
    while True:
        value = read_input("输入 1 或 2: ").strip()
        if value in ("1", "2"):
            return value
        if not value:
            print("未选择功能，已取消。")
            return ""
        print("输入无效，请输入 1 或 2。")


def prompt_int(label, default, minimum=1, allow_auto=False):
    auto_hint = "，输入 auto=自动调整" if allow_auto else ""
    value = read_input(f"{label}(留空={default}{auto_hint}): ").strip()
    if allow_auto and value.lower() == AUTO_CONCURRENCY:
        return AUTO_CONCURRENCY
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        print(f"输入无效，使用默认值 {default}")
        return default
    if number < minimum:
        print(f"不能小于 {minimum}，使用默认值 {default}")
        return default
    return number


def get_retry_wait_seconds(retry_after, attempt):
    fallback = ASMR_SUBTITLE_RETRY_DELAY * attempt
    try:
        value = float(retry_after) if retry_after else fallback
    except ValueError:
        value = fallback
    return max(RATE_LIMIT_MIN_WAIT, value)


def collect_audio_asmr_ids():
    work_ids = []
    html_files = sorted(WORKS_DIR.glob("RJ*.html"))
    total = len(html_files)

    for idx, path in enumerate(html_files, start=1):
        if idx % 1000 == 0:
            print(f"扫描作品 HTML: {idx}/{total}")
        try:
            work = parse_html_file(path)
        except Exception as e:
            print(f"  跳过解析失败: {path.name} - {e}")
            continue
        if work.get("work_kind") == WORK_KIND_AUDIO_ASMR:
            work_ids.append(work["product_id"])

    return work_ids


async def query_subtitle_api(session, work_id, log=print, on_rate_limit=None):
    url = build_asmr_subtitle_api_url(work_id)
    saw_rate_limit = False
    notified_rate_limit = False
    for attempt in range(1, ASMR_SUBTITLE_MAX_RETRIES + 1):
        try:
            async with session.get(url, timeout=20) as resp:
                if resp.status == 429 and attempt < ASMR_SUBTITLE_MAX_RETRIES:
                    saw_rate_limit = True
                    if on_rate_limit and not notified_rate_limit:
                        on_rate_limit()
                        notified_rate_limit = True
                    retry_after = resp.headers.get("Retry-After")
                    wait_seconds = get_retry_wait_seconds(retry_after, attempt)
                    log(f"  字幕 API 限流: {work_id}，{wait_seconds:.1f} 秒后重试")
                    await asyncio.sleep(wait_seconds)
                    continue

                if resp.status != 200:
                    log(f"  字幕 API 状态异常: {work_id} HTTP {resp.status}")
                    return None, saw_rate_limit

                data = await resp.json(content_type=None)
                return has_valid_asmr_subtitle_result(data, work_id), saw_rate_limit
        except Exception as e:
            if attempt < ASMR_SUBTITLE_MAX_RETRIES:
                wait_seconds = ASMR_SUBTITLE_RETRY_DELAY * attempt
                log(f"  字幕 API 查询失败: {work_id} - {e}，{wait_seconds:.1f} 秒后重试")
                await asyncio.sleep(wait_seconds)
                continue
            log(f"  字幕 API 查询失败: {work_id} - {e}")
            return None, saw_rate_limit

    return None, saw_rate_limit


async def update_targets(target_ids, label, concurrency):
    cache = load_asmr_subtitle_cache()
    auto_mode = concurrency == AUTO_CONCURRENCY
    target_concurrency = AUTO_START_CONCURRENCY if auto_mode else concurrency
    target_concurrency = max(AUTO_MIN_CONCURRENCY, min(AUTO_MAX_CONCURRENCY, target_concurrency))
    active_tasks = set()
    pending_ids = list(target_ids)
    completed = 0
    changed_to_subtitled = 0
    confirmed_unsubtitled = 0
    failed = 0
    rate_limited = 0
    window_completed = 0
    window_rate_limited = 0
    window_failed = 0
    cooldown_windows = 0
    clean_windows = 0
    progress = ProgressBar(len(target_ids), label)
    started_at = time.time()

    def log_message(message):
        progress.message(message)

    def adjust_for_rate_limit():
        nonlocal target_concurrency, cooldown_windows, clean_windows
        if not auto_mode:
            return
        old = target_concurrency
        target_concurrency = max(AUTO_MIN_CONCURRENCY, target_concurrency - 1)
        cooldown_windows = AUTO_RATE_LIMIT_COOLDOWN_WINDOWS
        clean_windows = 0
        if target_concurrency != old:
            log_message(f"  自动并发调整: {old} -> {target_concurrency}（检测到 429，进入冷却）")

    def tune_auto_concurrency():
        nonlocal target_concurrency, window_completed, window_rate_limited, window_failed
        nonlocal cooldown_windows, clean_windows
        if not auto_mode or window_completed < AUTO_WINDOW_SIZE:
            return

        old = target_concurrency
        if window_rate_limited > 0:
            cooldown_windows = AUTO_RATE_LIMIT_COOLDOWN_WINDOWS
            clean_windows = 0
        elif window_failed >= max(3, AUTO_WINDOW_SIZE // 4):
            target_concurrency = max(AUTO_MIN_CONCURRENCY, target_concurrency - 1)
            cooldown_windows = max(cooldown_windows, 1)
            clean_windows = 0
        elif window_failed == 0 and window_rate_limited == 0:
            if cooldown_windows > 0:
                cooldown_windows -= 1
            else:
                clean_windows += 1
                if clean_windows >= AUTO_RAMP_UP_CLEAN_WINDOWS:
                    target_concurrency = min(AUTO_MAX_CONCURRENCY, target_concurrency + 1)
                    clean_windows = 0
        else:
            clean_windows = 0

        if target_concurrency != old:
            log_message(f"  自动并发调整: {old} -> {target_concurrency}")

        window_completed = 0
        window_rate_limited = 0
        window_failed = 0

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        async def worker(work_id):
            on_rate_limit = adjust_for_rate_limit if auto_mode else None
            return work_id, await query_subtitle_api(session, work_id, log_message, on_rate_limit)

        while pending_ids or active_tasks:
            while pending_ids and len(active_tasks) < target_concurrency:
                active_tasks.add(asyncio.create_task(worker(pending_ids.pop(0))))

            done, active_tasks = await asyncio.wait(active_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                work_id, (result, saw_rate_limit) = await task
                completed += 1
                window_completed += 1

                if saw_rate_limit:
                    rate_limited += 1
                    window_rate_limited += 1

                if result is None:
                    failed += 1
                    window_failed += 1
                else:
                    set_cached_asmr_subtitle(cache, work_id, result)
                    if result:
                        changed_to_subtitled += 1
                    else:
                        confirmed_unsubtitled += 1

                if completed % 20 == 0 or completed == len(target_ids):
                    save_asmr_subtitle_cache(cache)
                progress.update(completed, changed_to_subtitled, confirmed_unsubtitled, failed, rate_limited)
                tune_auto_concurrency()

    save_asmr_subtitle_cache(cache)
    progress.close()
    elapsed = time.time() - started_at
    return {
        "processed": completed,
        "subtitled": changed_to_subtitled,
        "unsubtitled": confirmed_unsubtitled,
        "failed": failed,
        "rate_limited": rate_limited,
        "elapsed": elapsed,
        "speed": completed / elapsed if elapsed > 0 else 0,
        "auto_mode": auto_mode,
        "final_concurrency": target_concurrency,
    }


async def main():
    choice = prompt_choice()
    if not choice:
        return 0
    concurrency = prompt_int("并发查询数", DEFAULT_CONCURRENCY, allow_auto=True)
    limit = prompt_int("最多处理多少个作品，0=全部", 0, minimum=0)

    cache = load_asmr_subtitle_cache()
    audio_ids = collect_audio_asmr_ids()
    print(f"本地音声 ASMR: {len(audio_ids)} 个")

    if choice == "1":
        target_ids = [work_id for work_id in audio_ids if get_cached_asmr_subtitle(cache, work_id) is None]
        label = "补查未确认"
    else:
        target_ids = [work_id for work_id in audio_ids if get_cached_asmr_subtitle(cache, work_id) is False]
        label = "复查无字幕"

    if limit > 0:
        target_ids = target_ids[:limit]

    print(f"{label}目标: {len(target_ids)} 个")
    if not target_ids:
        print("没有需要处理的作品。")
        return 0



    summary = await update_targets(
        target_ids,
        label,
        concurrency,
    )
    print("\n总结：")
    print(f"  - 执行功能: {label}")
    print(f"  - 本次处理: {summary['processed']} / {len(target_ids)}")
    print(f"  - 更新为有字幕: {summary['subtitled']}")
    print(f"  - 确认为无字幕: {summary['unsubtitled']}")
    print(f"  - 查询失败/仍被限流: {summary['failed']}")
    print(f"  - 遇到 429 限流: {summary['rate_limited']}")
    if summary["auto_mode"]:
        print(f"  - 自动并发最终值: {summary['final_concurrency']}")
    print(f"  - 耗时: {format_duration(summary['elapsed'])}")
    print(f"  - 平均速度: {summary['speed']:.2f} 个/秒")
    print("下一步运行 python generate.py 重新生成网页，作品类型会根据缓存更新。")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
