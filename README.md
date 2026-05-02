# DLsite 作品爬取与展示系统

一个用于爬取 DLsite 同人作品信息、生成翻译稿件、并本地展示的工具集。

> **翻译软件推荐**：只推荐使用 [LinguaGacha](https://github.com/neavo/LinguaGacha)。
>
> 如果使用 DeepSeek 翻译，**请务必关闭思考模式**，否则会产生高额费用。
>
> 本项目已附带可用的翻译稿（`翻译稿.rar`），解压后可直接使用。

## 功能概览

- **爬取作品信息** — 从 DLsite 搜索页批量抓取作品 HTML，自动下载图片
- **生成翻译稿件** — 解析 HTML 生成待翻译 Markdown 文件
- **导入翻译结果** — 将翻译好的 Markdown 应用到 JSON 数据
- **本地网页展示** — 启动本地服务器，以网页形式浏览作品

## 目录结构

```
├── crawler.py          # 爬虫脚本
├── generate.py         # 生成 JSON 和翻译稿
├── md_to_json.py       # 导入翻译结果
├── open_page.py        # 启动本地展示页
├── cleanup_works.py    # 清理不在 crawl_results 中的 HTML
├── split_to_ai.py      # 分割待翻译文件到 ai 文件夹
├── retry_failed.py     # 重试下载失败的作品
├── works/              # 爬取的 HTML 文件
├── 待翻译/             # 生成的待翻译稿件
├── 翻译稿/             # 翻译完成的稿件
├── output/             # 生成的展示数据
│   ├── index.html      # 展示页入口
│   ├── images/         # 下载的图片
│   └── data/
│       ├── json/       # 作品 JSON 数据
│       ├── orig/       # 原文对照
│       └── translate/  # 翻译数据
├── crawl_results.json  # 爬取分类记录
├── works_order.json    # 作品人气排序
└── url_history.json    # URL 历史记录
```

## 使用流程

### 1. 爬取作品

```bash
python crawler.py
```

运行后：
1. 输入 DLsite 搜索/分类页 URL（可输入多个）
2. 输入 `1` 可复用上次使用的 URL
3. 设置每个链接最大爬取页数（0 = 不限制）
4. 爬虫自动下载作品 HTML 到 `works/` 目录

### 2. 生成数据

```bash
python generate.py
```

功能：
- 解析 `works/` 下的 HTML 文件
- 下载作品图片（轮播图、内容图）
- 生成 JSON 数据到 `output/data/json/`
- 生成待翻译稿件到 `待翻译/`
- 生成原文对照到 `output/data/orig/`

### 3. 翻译作品

将 `待翻译/` 中的 `.md` 文件翻译后：
- 保存为 `翻译稿/RJxxxxxx.zh.md`
- 或直接覆盖 `待翻译/RJxxxxxx.md` 并重命名为 `.zh.md`

### 4. 导入翻译

```bash
python md_to_json.py
```

功能：
- 读取 `翻译稿/` 下的翻译文件
- 将翻译应用到 JSON 数据
- 归档已翻译的待翻译文件

### 5. 查看展示页

```bash
python open_page.py
```

或直接运行：
```bash
启动网页.bat
```

自动打开浏览器访问 `http://localhost:8080`

## 辅助脚本

### cleanup_works.py

删除 `works/` 中不存在于 `crawl_results.json` 的 HTML 文件：

```bash
python cleanup_works.py
```

### split_to_ai.py

将 `待翻译/` 中的文件分割复制到 `ai/` 文件夹（每 1000 个一组）：

```bash
python split_to_ai.py
```

## 依赖

- Python 3.10+
- aiohttp
- aiofiles

安装依赖：
```bash
pip install aiohttp aiofiles
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `crawl_results.json` | 记录每次爬取的分类信息、作品 ID 列表 |
| `works_order.json` | 作品按人气排序的 ID 列表 |
| `url_history.json` | 爬虫使用过的 URL 历史 |
| `failed_works.md` | 下载失败的作品记录 |

## 注意事项

- 爬虫需要网络访问 DLsite
- 图片下载支持断点续传（已下载的会跳过）
- 翻译稿命名格式：`RJxxxxxx.zh.md` 或 `VJxxxxxx.zh.md`
