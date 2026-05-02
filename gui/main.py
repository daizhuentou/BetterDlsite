import sys
import asyncio
import subprocess
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QSpinBox, QGroupBox,
    QMessageBox, QProgressBar
)
from PyQt5.QtCore import QThread, pyqtSignal


if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent.parent


class FullPipelineThread(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)
    finished_signal = pyqtSignal(bool)
    
    def __init__(self, url, max_pages):
        super().__init__()
        self.url = url
        self.max_pages = max_pages
    
    def run(self):
        try:
            import aiohttp
        except ImportError:
            self.log_signal.emit("错误: 需要安装 aiohttp，请运行: pip install aiohttp")
            self.finished_signal.emit(False)
            return
        
        sys.path.insert(0, str(BASE_DIR))
        
        try:
            if not self.run_crawler():
                self.finished_signal.emit(False)
                return
            
            if not self.run_generate():
                self.finished_signal.emit(False)
                return
            
            self.run_translate()
            
            self.log_signal.emit("\n" + "=" * 50)
            self.log_signal.emit("全部完成！")
            self.finished_signal.emit(True)
            
        except Exception as e:
            import traceback
            self.log_signal.emit(f"错误: {e}")
            self.log_signal.emit(traceback.format_exc())
            self.finished_signal.emit(False)
    
    def run_crawler(self):
        from crawler import (
            build_page_template, extract_category_name, make_safe_slug,
            download_page, extract_work_links, download_works_from_page,
            has_next_page, save_crawl_result, WORKS_DIR, ORDER_FILE,
            CRAWL_RESULTS_FILE, HEADERS, MAX_CONCURRENT
        )
        import aiohttp
        import json
        
        self.log_signal.emit("=" * 50)
        self.log_signal.emit("【步骤 1/3】开始爬取 DLsite 作品...")
        self.log_signal.emit("=" * 50)
        
        try:
            page_template, start_page = build_page_template(self.url)
            category_name = extract_category_name(self.url)
            category_slug = make_safe_slug(category_name)
            
            WORKS_DIR.mkdir(exist_ok=True)
            
            self.log_signal.emit(f"分类: {category_name}")
            self.log_signal.emit(f"起始页: {start_page}")
            self.log_signal.emit(f"最大页数: {'不限制' if self.max_pages == 0 else self.max_pages}")
            
            all_work_ids = []
            
            async def run_crawler():
                nonlocal all_work_ids
                connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT)
                async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
                    page = start_page
                    total_downloaded = 0
                    total_failed = 0
                    
                    while True:
                        self.log_signal.emit(f"\n正在处理第 {page} 页...")
                        search_page_url = page_template.format(page=page)
                        
                        search_html = await download_page(session, search_page_url)
                        if not search_html:
                            self.log_signal.emit(f"  无法下载第 {page} 页，停止")
                            break
                        
                        work_links = extract_work_links(search_html)
                        if not work_links:
                            self.log_signal.emit(f"  第 {page} 页没有找到作品链接，停止")
                            break
                        
                        self.log_signal.emit(f"  第 {page} 页找到 {len(work_links)} 个作品")
                        
                        for work in work_links:
                            all_work_ids.append(work["id"])
                        
                        downloaded, failed = await download_works_from_page(session, work_links, page)
                        total_downloaded += downloaded
                        total_failed += failed
                        
                        pages_done = page - start_page + 1
                        if self.max_pages > 0 and pages_done >= self.max_pages:
                            self.log_signal.emit(f"\n已达到最大爬取页数限制: {self.max_pages}")
                            break
                        
                        if not has_next_page(search_html, page):
                            self.log_signal.emit("\n没有更多页面了")
                            break
                        
                        page += 1
                        await asyncio.sleep(0.1)
                    
                    return total_downloaded, total_failed
            
            total_downloaded, total_failed = asyncio.run(run_crawler())
            
            self.log_signal.emit("\n爬取完成:")
            self.log_signal.emit(f"  - 新下载: {total_downloaded} 个作品")
            if total_failed > 0:
                self.log_signal.emit(f"  - 下载失败: {total_failed} 个作品")
            
            if all_work_ids:
                with open(ORDER_FILE, "w", encoding="utf-8") as f:
                    json.dump(all_work_ids, f, ensure_ascii=False)
                self.log_signal.emit(f"  - 已保存排序: {len(all_work_ids)} 个作品")
                
                save_crawl_result(category_name, category_slug, self.url, all_work_ids)
                self.log_signal.emit(f"  - 已记录分类: {category_name}")
            
            return True
            
        except Exception as e:
            import traceback
            self.log_signal.emit(f"爬取错误: {e}")
            self.log_signal.emit(traceback.format_exc())
            return False
    
    def run_generate(self):
        from generate import (
            parse_html_file, download_all_images, write_work_markdown_files,
            write_paged_outputs, JSON_DIR, WORKS_DIR, ITEMS_PER_PAGE,
            load_crawl_categories, cleanup_stale_category_dirs,
            build_manifest_entry, collect_work_kinds, CATEGORIES_FILE,
            OUTPUT_DIR, generate_html, HEADERS, MAX_CONCURRENT_IMAGES,
            find_work_html_files
        )
        import aiohttp
        import math
        import json
        
        self.log_signal.emit("\n" + "=" * 50)
        self.log_signal.emit("【步骤 2/3】开始生成网页数据...")
        self.log_signal.emit("=" * 50)
        
        try:
            html_files = find_work_html_files()
            if not html_files:
                self.log_signal.emit("错误: 未找到HTML文件！")
                return False
            
            self.log_signal.emit(f"找到 {len(html_files)} 个HTML文件")
            
            works = []
            for i, f in enumerate(html_files):
                work = parse_html_file(f)
                works.append(work)
                self.progress_signal.emit(i + 1, len(html_files))
            
            self.log_signal.emit("下载图片中...")
            
            async def download_images():
                connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_IMAGES)
                async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
                    return await download_all_images(session, works)
            
            works = asyncio.run(download_images())
            
            total_pages = math.ceil(len(works) / ITEMS_PER_PAGE)
            self.log_signal.emit(f"共 {len(works)} 个作品，分 {total_pages} 页")
            
            self.log_signal.emit("生成Markdown文件...")
            write_work_markdown_files(works)
            
            self.log_signal.emit("生成JSON数据...")
            write_paged_outputs(works, JSON_DIR, "全部作品")
            
            works_by_id = {work["product_id"]: work for work in works}
            manifest = [
                build_manifest_entry(
                    "全部作品", "__all__", len(works),
                    "data/json/page_", "",
                    work_kinds=collect_work_kinds(works)
                )
            ]
            
            crawl_categories = load_crawl_categories(works_by_id)
            cleanup_stale_category_dirs({c["slug"] for c in crawl_categories})
            
            for category in crawl_categories:
                category_works = [works_by_id[wid] for wid in category["work_ids"]]
                json_dir = JSON_DIR / category["slug"]
                write_paged_outputs(category_works, json_dir, category["name"])
                manifest.append(build_manifest_entry(
                    category["name"], category["slug"], len(category_works),
                    f"data/json/{category['slug']}/page_", "",
                    category.get("source_url", ""),
                    category.get("updated_at", ""),
                    collect_work_kinds(category_works)
                ))
            
            with open(CATEGORIES_FILE, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            
            self.log_signal.emit("生成HTML...")
            html = generate_html(len(works))
            with open(OUTPUT_DIR / "index.html", "w", encoding="utf-8") as f:
                f.write(html)
            
            self.log_signal.emit("生成完成！")
            return True
            
        except Exception as e:
            import traceback
            self.log_signal.emit(f"生成错误: {e}")
            self.log_signal.emit(traceback.format_exc())
            return False
    
    def run_translate(self):
        from md_to_json import (
            migrate_legacy_page_translations, load_translations,
            apply_translations_to_json, archive_completed_pending_files,
            JSON_DIR, DATA_DIR
        )
        import json
        
        self.log_signal.emit("\n" + "=" * 50)
        self.log_signal.emit("【步骤 3/3】导入翻译...")
        self.log_signal.emit("=" * 50)
        
        try:
            migrated, skipped = migrate_legacy_page_translations()
            if migrated or skipped:
                self.log_signal.emit(f"整理译文稿: 移入/拆出 {migrated} 个，跳过 {skipped} 个")
            
            translations, sources, md_files = load_translations()
            if not md_files:
                self.log_signal.emit("未找到翻译文件，跳过此步骤")
                return
            
            if not translations:
                self.log_signal.emit("没有解析到可用的翻译，跳过此步骤")
                return
            
            json_files = sorted(JSON_DIR.rglob("page_*.json"))
            total_translated = 0
            total_works = 0
            applied_work_ids = set()
            
            for json_path in json_files:
                with open(json_path, "r", encoding="utf-8") as f:
                    json_data = json.load(f)
                
                json_data, translated_count, page_applied_ids = apply_translations_to_json(json_data, translations)
                applied_work_ids.update(page_applied_ids)
                
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(json_data, f, ensure_ascii=False, indent=2)
                
                total_works += len(json_data)
                total_translated += translated_count
            
            moved_pending, moved_zh = archive_completed_pending_files(applied_work_ids, sources)
            
            self.log_signal.emit(f"导入完成: {total_translated}/{total_works} 个作品命中翻译")
            if moved_pending > 0:
                self.log_signal.emit(f"已归档 {moved_pending} 个待翻译稿")
            
        except Exception as e:
            import traceback
            self.log_signal.emit(f"翻译导入错误: {e}")
            self.log_signal.emit(traceback.format_exc())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DLsite 爬虫工具")
        self.setMinimumSize(700, 500)
        self.open_page_process = None
        
        self.init_ui()
    
    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout(central_widget)
        
        crawler_group = QGroupBox("爬虫设置")
        crawler_layout = QVBoxLayout(crawler_group)
        
        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel("搜索URL:"))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("输入 DLsite 搜索/分类页 URL")
        url_layout.addWidget(self.url_input)
        crawler_layout.addLayout(url_layout)
        
        btn_layout = QHBoxLayout()
        btn_layout.addWidget(QLabel("最大页数:"))
        self.pages_spin = QSpinBox()
        self.pages_spin.setRange(0, 1000)
        self.pages_spin.setValue(0)
        self.pages_spin.setSpecialValueText("不限制")
        btn_layout.addWidget(self.pages_spin)
        btn_layout.addStretch()
        
        self.start_btn = QPushButton("开始爬取")
        self.start_btn.setMinimumHeight(40)
        self.start_btn.clicked.connect(self.start_pipeline)
        btn_layout.addWidget(self.start_btn)
        
        self.open_btn = QPushButton("打开网页")
        self.open_btn.setMinimumHeight(40)
        self.open_btn.clicked.connect(self.open_page)
        btn_layout.addWidget(self.open_btn)
        
        crawler_layout.addLayout(btn_layout)
        layout.addWidget(crawler_group)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        log_group = QGroupBox("日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        layout.addWidget(log_group)
    
    def log(self, message):
        self.log_text.append(message)
    
    def start_pipeline(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "警告", "请输入搜索URL")
            return
        
        if "dlsite.com" not in url:
            QMessageBox.warning(self, "警告", "请输入有效的 DLsite URL")
            return
        
        self.start_btn.setEnabled(False)
        self.log_text.clear()
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        
        self.pipeline_thread = FullPipelineThread(url, self.pages_spin.value())
        self.pipeline_thread.log_signal.connect(self.log)
        self.pipeline_thread.progress_signal.connect(self.on_progress)
        self.pipeline_thread.finished_signal.connect(self.on_finished)
        self.pipeline_thread.start()
    
    def on_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
    
    def on_finished(self, success):
        self.start_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        if success:
            QMessageBox.information(self, "完成", "全部流程已完成！")
        else:
            QMessageBox.warning(self, "失败", "流程执行失败，请查看日志")
    
    def open_page(self):
        open_page_path = BASE_DIR / "open_page.py"
        if not open_page_path.exists():
            QMessageBox.warning(self, "警告", "未找到 open_page.py")
            return
        
        if self.open_page_process and self.open_page_process.poll() is None:
            QMessageBox.information(self, "提示", "服务器已在运行中")
            return
        
        self.log("启动本地服务器...")
        self.open_page_process = subprocess.Popen(
            [sys.executable, str(open_page_path)],
            cwd=str(BASE_DIR)
        )
    
    def closeEvent(self, event):
        if self.open_page_process and self.open_page_process.poll() is None:
            self.open_page_process.terminate()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
