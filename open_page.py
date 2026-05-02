import asyncio
import json
import socket
import time
import webbrowser
import aiofiles
from pathlib import Path
from aiohttp import web


OUTPUT_DIR = Path(__file__).parent / "output"
HTML_FILE = OUTPUT_DIR / "index.html"
WORK_STATES_FILE = OUTPUT_DIR / "data" / "work_states.json"
PORT = 8080


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


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


async def handle_get_work_states(request):
    if WORK_STATES_FILE.exists():
        async with aiofiles.open(WORK_STATES_FILE, 'r', encoding='utf-8') as f:
            data = await f.read()
        return web.Response(body=data, content_type='application/json')
    return web.Response(body='{}', content_type='application/json')


async def handle_save_work_states(request):
    try:
        body = await request.json()
        WORK_STATES_FILE.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(WORK_STATES_FILE, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(body, ensure_ascii=False))
        return web.Response(body='{"ok":true}', content_type='application/json')
    except Exception as e:
        return web.Response(status=400, body=json.dumps({"ok": False, "error": str(e)}), content_type='application/json')


async def main():
    if not HTML_FILE.exists():
        print("未找到 index.html 文件，请先运行 generate.py。")
        return

    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/api/work-states', handle_get_work_states)
    app.router.add_post('/api/work-states', handle_save_work_states)
    app.router.add_get('/{path:.*}', handle_static)

    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    local_ip = get_local_ip()
    local_url = f"http://localhost:{PORT}/index.html?v={int(time.time())}"
    lan_url = f"http://{local_ip}:{PORT}/index.html?v={int(time.time())}"
    print(f"服务器已启动:")
    print(f"  本机访问: {local_url}")
    print(f"  局域网访问: {lan_url}")
    print("按 Ctrl+C 停止服务器")
    webbrowser.open(local_url)

    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print("\n服务器已停止")
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
