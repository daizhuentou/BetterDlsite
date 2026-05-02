import re
import sys
import json
import asyncio
import aiohttp
from pathlib import Path


BASE_DIR = Path(__file__).parent
WORKS_DIR = BASE_DIR / "works"
FAILED_LOG = BASE_DIR / "failed_works.md"
ORDER_FILE = BASE_DIR / "works_order.json"
MAX_CONCURRENT = 5  # 最大并发数
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.dlsite.com/"
}


async def download_page(session, url):
    """异步下载页面内容"""
    try:
        async with session.get(url, timeout=30) as resp:
            return await resp.text('utf-8', errors='ignore')
    except Exception as e:
        print(f"  下载失败: {url} - {e}")
        return None


def extract_work_links(html):
    """从搜索页面提取作品链接和名称"""
    work_blocks = re.split(r'<li\s+[^>]*data-list_item_product_id=', html)[1:]
    work_links = []
    seen_ids = set()
    
    for block in work_blocks:
        id_match = re.search(r'"(RJ\d+)"', block)
        if not id_match:
            continue
        work_id = id_match.group(1)
        
        if work_id in seen_ids:
            continue
        seen_ids.add(work_id)
        
        name_match = re.search(r'<a\s+[^>]*href="[^"]*/work/[^"]*"[^>]*title="([^"]*)"', block)
        work_name = name_match.group(1) if name_match else ""
        
        work_links.append({
            "id": work_id,
            "name": work_name,
            "url": f"https://www.dlsite.com/maniax/work/=/product_id/{work_id}.html"
        })
    
    return work_links


def has_next_page(html, current_page):
    """检查是否有下一页"""
    next_pattern = rf'rel="next"|page/{current_page + 1}'
    return bool(re.search(next_pattern, html))


def log_failed_work(work_id, work_name, work_url):
    """记录下载失败的作品到md文件"""
    if not FAILED_LOG.exists():
        with open(FAILED_LOG, "w", encoding="utf-8") as f:
            f.write("# 下载失败的作品列表\n\n")
            f.write("| RJ号码 | 作品名称 | 链接 |\n")
            f.write("|--------|----------|------|\n")
    
    with open(FAILED_LOG, "a", encoding="utf-8") as f:
        f.write(f"| {work_id} | {work_name} | {work_url} |\n")


async def download_work(session, work_id, work_name, work_url):
    """异步下载单个作品的HTML"""
    html = await download_page(session, work_url)
    if html and len(html) > 500:
        save_path = WORKS_DIR / f"{work_id}.html"
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  ✓ 已保存: {work_id}.html")
        return True
    else:
        if html:
            print(f"  ✗ 内容为空或过短: {work_id} (仅 {len(html) if html else 0} 字节)")
        log_failed_work(work_id, work_name, work_url)
        return False


async def download_works_from_page(session, work_links, page_num):
    """下载单页的所有作品"""
    works_to_download = []
    skipped_count = 0
    
    for work in work_links:
        save_path = WORKS_DIR / f"{work['id']}.html"
        if save_path.exists():
            skipped_count += 1
            print(f"  跳过已存在: {work['id']}.html")
        else:
            works_to_download.append(work)
    
    if not works_to_download:
        print(f"  第 {page_num} 页的作品都已下载完毕")
        return 0, 0
    
    print(f"\n  开始下载第 {page_num} 页的 {len(works_to_download)} 个新作品...")
    print(f"  使用 {MAX_CONCURRENT} 个并发连接\n")
    
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    
    async def bounded_download(work):
        async with semaphore:
            print(f"  下载中: {work['id']}")
            success = await download_work(session, work['id'], work['name'], work['url'])
            await asyncio.sleep(0.1)
            return success
    
    tasks = [bounded_download(work) for work in works_to_download]
    results = await asyncio.gather(*tasks)
    
    total_downloaded = sum(results)
    total_failed = len(results) - total_downloaded
    
    return total_downloaded, total_failed


async def main():
    base_url = "https://www.dlsite.com/maniax/fsr/=/language/jp/sex_category%5B0%5D/male/order%5B0%5D/trend/work_type_category%5B0%5D/game/work_type_category_name%5B0%5D/%E6%B8%B8%E6%88%8F/genre%5B0%5D/302/genre_name%5B0%5D/%E5%AF%9D%E5%8F%96%E3%82%8A/options_and_or/and/options%5B0%5D/JPN/options%5B1%5D/CHI_HANS/options%5B2%5D/CHI_HANT/options%5B3%5D/NM/options_name%5B0%5D/%E6%97%A5%E8%AF%AD%E4%BD%9C%E5%93%81/options_name%5B1%5D/%E7%AE%80%E4%BD%9C%E4%B8%AD%E6%96%87%E4%BD%9C%E5%93%81/options_name%5B2%5D/%E7%B9%81%E4%BD%9C%E4%B8%AD%E6%96%87%E4%BD%9C%E5%93%81/options_name%5B3%5D/%E6%97%A5%E8%AF%AD%E8%A8%80%E9%99%90%E5%88%B6%E4%BD%9C%E5%93%81/per_page/100/page/{page}/show_type/3/lang_options%5B0%5D/%E6%97%A5%E8%AF%AD/lang_options%5B1%5D/%E4%B8%AD%E6%96%87%28%E7%AE%80%E4%BD%9C%E5%AD%97%29/lang_options%5B2%5D/%E4%B8%AD%E6%96%87%28%E7%B9%81%E4%BD%9C%E5%AD%97%29/lang_options%5B3%5D/%E6%97%A5%E8%AF%AD%E8%A8%80%E9%99%90%E5%88%B6"
    
    WORKS_DIR.mkdir(exist_ok=True)
    
    print("开始爬取DLsite作品...")
    print("=" * 60)
    
    all_work_ids = []
    
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        page = 1
        total_downloaded = 0
        total_skipped = 0
        total_failed = 0
        
        while True:
            print(f"\n正在处理第 {page} 页...")
            search_url = base_url.format(page=page)
            
            search_html = await download_page(session, search_url)
            if not search_html:
                print(f"  无法下载第 {page} 页，跳过")
                break
            
            work_links = extract_work_links(search_html)
            if not work_links:
                print(f"  第 {page} 页没有找到作品链接")
                break
            
            print(f"  第 {page} 页找到 {len(work_links)} 个作品")
            
            for work in work_links:
                all_work_ids.append(work["id"])
            
            downloaded, failed = await download_works_from_page(session, work_links, page)
            total_downloaded += downloaded
            total_failed += failed
            
            has_next = has_next_page(search_html, page)
            if not has_next:
                print(f"\n没有更多页面了")
                break
            
            page += 1
            print(f"\n  等待 1 秒后继续下一页...")
            await asyncio.sleep(1)
        
        print("\n" + "=" * 60)
        print(f"爬取完成！")
        print(f"  - 共下载: {total_downloaded} 个新作品")
        if total_failed > 0:
            print(f"  - 下载失败: {total_failed} 个作品 (详情查看 failed_works.md)")
    
    # 保存人气排序顺序
    with open(ORDER_FILE, "w", encoding="utf-8") as f:
        json.dump(all_work_ids, f, ensure_ascii=False)
    print(f"  - 已保存人气排序: {ORDER_FILE} ({len(all_work_ids)} 个作品)")


if __name__ == "__main__":
    try:
        import aiohttp
    except ImportError:
        print("需要安装aiohttp库，请运行: pip install aiohttp")
        import sys
        sys.exit(1)
    
    # 修复Windows上的Event loop is closed错误
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())
