import http.server
import socketserver
import webbrowser
import threading
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "output"
HTML_FILE = OUTPUT_DIR / "index.html"

if not HTML_FILE.exists():
    print("未找到index.html文件！请先运行generate.py。")
else:
    PORT = 8080
    
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(OUTPUT_DIR), **kwargs)
    
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        url = f"http://localhost:{PORT}/index.html"
        print(f"本地服务器已启动: {url}")
        print("按 Ctrl+C 停止服务器")
        
        webbrowser.open(url)
        httpd.serve_forever()
