import asyncio
import time
import webbrowser
import aiofiles
from pathlib import Path
from aiohttp import web


OUTPUT_DIR = Path(__file__).parent / "output"
HTML_FILE = OUTPUT_DIR / "index.html"
PORT = 8080


async def handle_static(request):
    file_path = OUTPUT_DIR / request.match_info.get('path', 'index.html')
    
    if not file_path.exists():
        return web.Response(status=404, text="文件不存在")
    
    content_type = get_content_type(file_path.suffix)
    
    async with aiofiles.open(file_path, 'rb') as f:
        data = await f.read()
    
    response = web.Response(
        body=data,
        content_type=content_type,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )
    return response


def get_content_type(suffix):
    types = {
        '.html': 'text/html',
        '.css': 'text/css',
        '.js': 'application/javascript',
        '.json': 'application/json',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.svg': 'image/svg+xml',
        '.ico': 'image/x-icon',
        '.md': 'text/markdown',
    }
    return types.get(suffix.lower(), 'application/octet-stream')


async def handle_index(request):
    return await handle_static(type('Request', (), {'match_info': {'path': 'index.html'}})())


async def main():
    if not HTML_FILE.exists():
        print("未找到 index.html 文件，请先运行 generate.py。")
        return

    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/{path:.*}', handle_static)

    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, 'localhost', PORT)
    await site.start()

    url = f"http://localhost:{PORT}/index.html?v={int(time.time())}"
    print(f"异步服务器已启动: {url}")
    print("按 Ctrl+C 停止服务器")
    webbrowser.open(url)

    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print("\n服务器已停止")
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
