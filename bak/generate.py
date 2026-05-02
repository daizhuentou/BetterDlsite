import re
import sys
import json
import math
import hashlib
import asyncio
import aiohttp
from pathlib import Path


BASE_DIR = Path(__file__).parent
WORKS_DIR = BASE_DIR / "works"
ORDER_FILE = BASE_DIR / "works_order.json"
OUTPUT_DIR = BASE_DIR / "output"
IMAGES_DIR = OUTPUT_DIR / "images"
DATA_DIR = OUTPUT_DIR / "data"
JSON_DIR = DATA_DIR / "json"
TRANSLATE_DIR = DATA_DIR / "translate"
ORIG_DIR = DATA_DIR / "orig"
SLIDER_IMAGES_DIR = IMAGES_DIR / "slider"
PARTS_IMAGES_DIR = IMAGES_DIR / "parts"
MAX_CONCURRENT_IMAGES = 100  # 最大并发下载图片数量

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.dlsite.com/"
}


async def download_image(session, url, save_path):
    """异步下载单张图片"""
    if save_path.exists():
        return str(save_path.relative_to(OUTPUT_DIR)).replace("\\", "/")
    try:
        async with session.get(url, timeout=30) as resp:
            data = await resp.read()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(data)
        print(f"  ✓ 已下载: {save_path.name}")
        return str(save_path.relative_to(OUTPUT_DIR)).replace("\\", "/")
    except Exception as e:
        print(f"  ✗ 下载失败: {url} -> {e}")
        return url


def get_image_filename(url, product_id, index=0, prefix=""):
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    ext = Path(url.split("?")[0]).suffix or ".jpg"
    if prefix:
        return f"{product_id}_{prefix}_{index}_{url_hash}{ext}"
    return f"{product_id}_{index}_{url_hash}{ext}"


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

    slider_images = []
    slider_block = re.search(r'class="product-slider-data">(.*?)</div>\s*<div\s+class="work_slider', content, re.DOTALL)
    if slider_block:
        for m in re.finditer(r'data-src="(//img\.dlsite\.jp/[^"]+)"', slider_block.group(1)):
            src = m.group(1)
            if src.startswith("//"):
                src = "https:" + src
            slider_images.append(src)

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
        "slider_images": slider_images,
        "parts": parts
    }


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
            "slider_images": w["local_slider_images"],
            "parts": w["parts"]
        }
        works_json.append(work_data)
    return works_json


def generate_html(total_works):
    total_pages = math.ceil(total_works / ITEMS_PER_PAGE)

    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>作品展示</title>
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
            margin-bottom: 30px;
            font-size: 2.5em;
            text-shadow: 0 0 20px rgba(102, 126, 234, 0.5);
            letter-spacing: 2px;
        }
        .pagination {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 10px;
            margin-bottom: 30px;
            flex-wrap: wrap;
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
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
            z-index: 5;
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
        @media (max-width: 768px) {
            .works-grid { grid-template-columns: 1fr; }
            h1 { font-size: 1.8em; }
            .image-carousel { height: 250px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>作品展示</h1>
        <div class="pagination" id="pagination"></div>
        <div class="works-grid" id="worksGrid"></div>
        <div class="pagination" id="paginationBottom"></div>
    </div>
    <div class="modal" id="imageModal" onclick="closeModal()">
        <span class="modal-close">&times;</span>
        <img src="" id="modalImage">
    </div>
    <script>
        const TOTAL_WORKS = ''' + str(total_works) + ''';
        const TOTAL_PAGES = ''' + str(total_pages) + ''';
        const ITEMS_PER_PAGE = ''' + str(ITEMS_PER_PAGE) + ''';
        let currentPage = 1;
        let currentData = null;

        async function loadPageData(page) {
            const resp = await fetch('data/json/page_' + page + '.json');
            if (!resp.ok) {
                console.error('加载第 ' + page + ' 页数据失败');
                return null;
            }
            return await resp.json();
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
            if (e.key === 'Escape') closeModal();
        });

        function slideImages(card, direction) {
            const track = card.querySelector('.carousel-track');
            const slides = track.querySelectorAll('.carousel-slide');
            const total = slides.length;
            let current = parseInt(track.dataset.current || '0');
            current += direction;
            if (current < 0) current = total - 1;
            if (current >= total) current = 0;
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

        function renderWorks() {
            if (!currentData) return;
            const grid = document.getElementById('worksGrid');
            grid.innerHTML = '';
            
            for (let idx = 0; idx < currentData.length; idx++) {
                const work = currentData[idx];
                const globalIdx = (currentPage - 1) * ITEMS_PER_PAGE + idx;
                const card = document.createElement('div');
                card.className = 'work-card';

                let imagesHtml = '';
                work.slider_images.forEach((img) => {
                    imagesHtml += '<div class="carousel-slide"><img src="' + img + '" alt="' + escapeHtml(work.work_name) + '" onclick="openModal(this.src)"></div>';
                });

                let counterHtml = work.slider_images.length > 1
                    ? '<span class="carousel-counter">1 / ' + work.slider_images.length + '</span>'
                    : '';

                let buttonsHtml = work.slider_images.length > 1
                    ? '<button class="carousel-btn prev" onclick="slideImages(this.closest(\\'.work-card\\'), -1)">&#9664;</button><button class="carousel-btn next" onclick="slideImages(this.closest(\\'.work-card\\'), 1)">&#9654;</button>'
                    : '';

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
                            partsHtml += '<img class="parts-image" src="' + part.local_path + '" alt="' + escapeHtml(part.alt || part.heading || '') + '" onclick="openModal(this.src)" style="max-height:400px;">';
                        }
                    }
                });

                card.innerHTML =
                    '<div class="image-carousel">' +
                        '<span class="work-id-badge">' + work.product_id + '</span>' +
                        '<div class="carousel-track" data-current="0">' + imagesHtml + '</div>' +
                        buttonsHtml +
                        counterHtml +
                    '</div>' +
                    '<div class="work-content">' +
                        '<div class="name-section">' +
                            '<div class="maker-name">社团: ' + escapeHtml(work.maker_name) + '</div>' +
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
                        '<div class="description-section">' +
                            '<div class="section-title">简介</div>' +
                            '<div class="description-text">' + escapeHtml(work.description) + '</div>' +
                        '</div>' +
                        '<div class="intro-section">' +
                            '<div class="section-title">详细介绍</div>' + partsHtml +
                        '</div>' +
                    '</div>';

                grid.appendChild(card);
            }
            
            renderPagination();
        }

        function renderPagination() {
            const paginationTop = document.getElementById('pagination');
            const paginationBottom = document.getElementById('paginationBottom');
            
            let html = '';
            
            html += '<button class="page-btn" onclick="goToPage(' + (currentPage - 1) + ')" ' + (currentPage === 1 ? 'disabled' : '') + '>上一页</button>';
            
            const maxButtons = 9;
            let startPage = Math.max(1, currentPage - Math.floor(maxButtons / 2));
            let endPage = Math.min(TOTAL_PAGES, startPage + maxButtons - 1);
            
            if (endPage - startPage < maxButtons - 1) {
                startPage = Math.max(1, endPage - maxButtons + 1);
            }
            
            if (startPage > 1) {
                html += '<button class="page-btn" onclick="goToPage(1)">1</button>';
                if (startPage > 2) {
                    html += '<span class="page-info">...</span>';
                }
            }
            
            for (let i = startPage; i <= endPage; i++) {
                html += '<button class="page-btn ' + (i === currentPage ? 'active' : '') + '" onclick="goToPage(' + i + ')">' + i + '</button>';
            }
            
            if (endPage < TOTAL_PAGES) {
                if (endPage < TOTAL_PAGES - 1) {
                    html += '<span class="page-info">...</span>';
                }
                html += '<button class="page-btn" onclick="goToPage(' + TOTAL_PAGES + ')">' + TOTAL_PAGES + '</button>';
            }
            
            html += '<button class="page-btn" onclick="goToPage(' + (currentPage + 1) + ')" ' + (currentPage === TOTAL_PAGES ? 'disabled' : '') + '>下一页</button>';
            html += '<span class="page-info">' + TOTAL_WORKS + ' 个作品 / 共 ' + TOTAL_PAGES + ' 页</span>';
            
            paginationTop.innerHTML = html;
            paginationBottom.innerHTML = html;
        }

        async function goToPage(page) {
            if (page < 1 || page > TOTAL_PAGES) return;
            
            currentPage = page;
            currentData = await loadPageData(page);
            renderWorks();
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        goToPage(1);
    </script>
</body>
</html>'''


async def download_all_images(session, works):
    """异步下载所有图片"""
    all_download_tasks = []
    skipped_count = 0
    
    for work in works:
        work["description_clean"] = clean_description(work["description"])
        
        # 准备轮播图下载
        for i, img_url in enumerate(work["slider_images"]):
            fname = get_image_filename(img_url, work["product_id"], i, "slider")
            save_path = SLIDER_IMAGES_DIR / fname
            if save_path.exists():
                skipped_count += 1
            else:
                all_download_tasks.append({
                    "type": "slider",
                    "work_id": work["product_id"],
                    "index": i,
                    "url": img_url,
                    "save_path": save_path
                })
        
        # 准备内容块图片下载
        for pi, part in enumerate(work["parts"]):
            if part["type"] == "image" and part.get("src"):
                fname = get_image_filename(part["src"], work["product_id"], pi, "parts")
                save_path = PARTS_IMAGES_DIR / fname
                if save_path.exists():
                    skipped_count += 1
                else:
                    all_download_tasks.append({
                        "type": "part",
                        "work_id": work["product_id"],
                        "index": pi,
                        "url": part["src"],
                        "save_path": save_path,
                        "part": part
                    })
    
    total_images = len(all_download_tasks) + skipped_count
    print(f"\n共 {total_images} 张图片，跳过已下载 {skipped_count} 张，需下载 {len(all_download_tasks)} 张")
    
    if not all_download_tasks:
        print("所有图片都已下载完毕！")
        # 填充本地路径
        for work in works:
            local_slider = []
            for i, img_url in enumerate(work["slider_images"]):
                fname = get_image_filename(img_url, work["product_id"], i, "slider")
                save_path = SLIDER_IMAGES_DIR / fname
                local_slider.append(str(save_path.relative_to(OUTPUT_DIR)).replace("\\", "/"))
            work["local_slider_images"] = local_slider
            for pi, part in enumerate(work["parts"]):
                if part["type"] == "image" and part.get("src"):
                    fname = get_image_filename(part["src"], work["product_id"], pi, "parts")
                    save_path = PARTS_IMAGES_DIR / fname
                    part["local_path"] = str(save_path.relative_to(OUTPUT_DIR)).replace("\\", "/")
        return works
    
    # 开始异步下载
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_IMAGES)
    downloaded_count = 0
    failed_count = 0
    
    async def bounded_download(task):
        nonlocal downloaded_count, failed_count
        async with semaphore:
            local_path = await download_image(session, task["url"], task["save_path"])
            if local_path != task["url"]:
                downloaded_count += 1
            else:
                failed_count += 1
            return {
                "task": task,
                "local_path": local_path
            }
    
    download_results = await asyncio.gather(*[bounded_download(task) for task in all_download_tasks])
    
    print(f"\n下载完成: 成功 {downloaded_count} 张，失败 {failed_count} 张，跳过 {skipped_count} 张")
    
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
                else:
                    fname = get_image_filename(img_url, work["product_id"], i, "slider")
                    save_path = SLIDER_IMAGES_DIR / fname
                    local_slider.append(str(save_path.relative_to(OUTPUT_DIR)).replace("\\", "/"))
            work["local_slider_images"] = local_slider
        else:
            # 全部已跳过
            local_slider = []
            for i, img_url in enumerate(work["slider_images"]):
                fname = get_image_filename(img_url, work["product_id"], i, "slider")
                save_path = SLIDER_IMAGES_DIR / fname
                local_slider.append(str(save_path.relative_to(OUTPUT_DIR)).replace("\\", "/"))
            work["local_slider_images"] = local_slider
        
        # 填充已跳过的 parts 图片路径
        for pi, part in enumerate(work["parts"]):
            if part["type"] == "image" and part.get("src") and "local_path" not in part:
                fname = get_image_filename(part["src"], work["product_id"], pi, "parts")
                save_path = PARTS_IMAGES_DIR / fname
                part["local_path"] = str(save_path.relative_to(OUTPUT_DIR)).replace("\\", "/")
    
    return works


async def main():
    SLIDER_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    PARTS_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)
    TRANSLATE_DIR.mkdir(parents=True, exist_ok=True)
    ORIG_DIR.mkdir(parents=True, exist_ok=True)

    html_files = sorted(WORKS_DIR.glob("RJ*.html"))
    if not html_files:
        print("未找到RJ*.html文件！")
        return

    # 按人气排序读取
    if ORDER_FILE.exists():
        with open(ORDER_FILE, "r", encoding="utf-8") as f:
            work_order = json.load(f)
        ordered_files = []
        file_dict = {Path(f).stem: f for f in html_files}
        for work_id in work_order:
            if work_id in file_dict:
                ordered_files.append(file_dict[work_id])
        for f in html_files:
            if f not in ordered_files:
                ordered_files.append(f)
        html_files = ordered_files
        print(f"按人气排序加载 (来自 works_order.json)")
    else:
        print(f"未找到 works_order.json，按文件名排序")

    print(f"找到 {len(html_files)} 个HTML文件\n")

    works = []
    for f in html_files:
        work = parse_html_file(f)
        works.append(work)
    
    # 异步下载所有图片
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_IMAGES)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        works = await download_all_images(session, works)

    total_pages = math.ceil(len(works) / ITEMS_PER_PAGE)
    print(f"\n共 {len(works)} 个作品，分 {total_pages} 页生成文件")

    # 分页生成 MD 和 JSON 文件
    for page in range(1, total_pages + 1):
        start = (page - 1) * ITEMS_PER_PAGE
        end = min(start + ITEMS_PER_PAGE, len(works))
        page_works = works[start:end]
        
        # 生成分页翻译 MD（译文栏填原文，可直接扔翻译软件）
        translate_md = generate_translate_md(page_works)
        translate_path = TRANSLATE_DIR / f"translate_page_{page}.md"
        with open(translate_path, "w", encoding="utf-8") as f:
            f.write(translate_md)
        
        # 生成分页原文 MD（供对照参考）
        orig_md = generate_orig_md(page_works)
        orig_path = ORIG_DIR / f"orig_page_{page}.md"
        with open(orig_path, "w", encoding="utf-8") as f:
            f.write(orig_md)
        
        # 生成分页 JSON 数据
        page_json = generate_page_json(works, page)
        json_path = JSON_DIR / f"page_{page}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(page_json, f, ensure_ascii=False, indent=2)
        
        print(f"  第 {page}/{total_pages} 页: {len(page_works)} 个作品 -> translate/translate_page_{page}.md, orig/orig_page_{page}.md, json/page_{page}.json")

    # 生成 HTML
    html = generate_html(len(works))
    with open(OUTPUT_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n已生成: {OUTPUT_DIR / 'index.html'}")

    print("\n完成！运行 open_page.py 查看结果。")


if __name__ == "__main__":
    # 修复Windows上的Event loop is closed错误
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())
