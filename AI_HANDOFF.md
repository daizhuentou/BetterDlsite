# 项目架构与变更说明

这份文档用于把当前项目状态交接给另一个 AI 或开发者。目标是让接手者快速理解脚本职责、数据流、目录约定，以及最近完成的关键改动。

## 项目目标

这是一个 DLsite 作品爬取、解析、翻译整理和本地网页展示项目。

核心流程：

1. `crawler.py` 从 DLsite 搜索/分类页抓取作品 HTML。
2. `generate.py` 解析 `works/*.html`，下载图片，生成展示用 JSON、分类索引、待翻译 Markdown 和网页。
3. 人工或外部工具翻译项目根目录 `待翻译/RJxxxx.md` 或 `待翻译/VJxxxx.md`，保存为项目根目录 `翻译稿/RJxxxx.zh.md` 或 `翻译稿/VJxxxx.zh.md`。
4. `md_to_json.py` 将翻译稿合并回展示 JSON。
5. `open_page.py` 启动本地 HTTP 服务查看 `output/index.html`。

## 主要脚本职责

### `crawler.py`

负责爬取 DLsite 分类页和作品 HTML。

当前能力：

- 运行时可以连续输入多个 DLsite 搜索/分类 URL；每输入一个加入队列，直接回车开始按顺序爬取。
- 也支持命令行参数：

```powershell
python crawler.py "DLsite分类URL" 3
python crawler.py "DLsite分类URL1" "DLsite分类URL2" 3
```

命令行最后一个纯数字参数会被识别为最大爬取页数；多链接时该页数对每个链接分别生效。

- `MAX_PAGES = 0` 表示不限制页数。
- 自动把 URL 中的 `/page/N` 转成 `/page/{page}`。
- 自动从 URL 的 `genre_name[0]/...`、`work_type_category_name[0]/...` 等字段识别分类名。
- 如果 URL 只有 `genre[0]/数字`，会读取 `list.devtools` 里的 `genre id -> 分类名` 映射。
- 如果 `list.devtools` 没有该数字，会继续复用 `crawl_results.json` 中已出现过的 `genre[0] + genre_name[0]` 映射。
- 分类结果写入 `crawl_results.json`。
- 当前一次爬取队列的人气顺序会合并写入 `works_order.json`：本次队列排在前面，旧排序保留在后面，避免局部爬取覆盖完整排序。
- 支持 DLsite `RJ` 和 `VJ` 作品 ID；列表页会保存真实详情页链接，因此 `VJ` 会使用 `https://www.dlsite.com/pro/work/...`。
- 交互模式会询问是否只下载有字幕的音声 ASMR；命令行可加 `--subtitle-asmr-only`、`--only-subtitle-asmr` 或 `--asmr-subtitle-only`。开启后，列表页中识别为音声 ASMR 的作品会先查 asmr-200 字幕 API；无有效字幕结果就不下载该作品 HTML。HTTP 429 会按退避重试。
- `update_asmr_subtitles.py` 是字幕缓存维护脚本，启动后通过用户输入选择功能：补查未确认音声 ASMR，或复查已标为无字幕的音声 ASMR。脚本只更新 `asmr_subtitle_cache.json`，不会新增分类；HTTP 429 会按退避重试，且 `Retry-After` 为 0 或缺失时至少等待 10 秒；并发数输入 `auto` 会启用自动调整模式，收到 429 会立即降并发并进入冷却，连续稳定多个窗口后才逐步升并发；运行时会显示进度条和 429 次数，结束时输出处理数量、字幕/无字幕/失败统计、429 次数、耗时和速度总结。

重要改动：

- 分类保存改成“合并而不是覆盖”。
- 同一个作品 ID 可以同时属于多个分类。
- 重新爬某个分类的部分页时，不会把该分类旧作品删掉。
- 下载作品 HTML 后，会检查是否能解析到 `<h1 id="work_name">...</h1>`。
- 如果本地已有 HTML 但没有作品名，会重新下载。
- 如果普通作品页没有作品名，会等待 10 秒后改用预告页：

```text
https://www.dlsite.com/maniax/announce/=/product_id/RJxxxx.html
https://www.dlsite.com/pro/announce/=/product_id/VJxxxx.html
```

相关常量：

```python
MAX_CONCURRENT = 100
MAX_PAGES = 0
MAX_WORK_RETRIES = 3
WORK_RETRY_DELAY = 0
```

### `generate.py`

负责把 `works/*.html` 转成展示数据和网页。

主要输出：

- `output/index.html`
- `output/data/categories.json`
- `output/data/search_index.json`
- `output/data/filter_index/*.json`
- `output/data/json/page_*.json`
- `output/data/json/<分类名>/page_*.json`
- `待翻译/RJxxxx.md` / `待翻译/VJxxxx.md`
- `output/data/orig/RJxxxx.md` / `output/data/orig/VJxxxx.md`
- `output/images/...`

分类来源：

- 优先读取 `crawl_results.json`。
- 全部作品排序优先读取 `works_order.json`；如果该文件只覆盖部分作品，会继续用 `crawl_results.json` 中各分类的 `work_ids` 顺序补齐，最后才按文件名兜底。
- 每个分类有独立 JSON 分页目录。
- 网页通过 `output/data/categories.json` 渲染顶部分类下拉框。
- `categories.json` 里的每个分类会带 `index_path`，指向对应的轻量筛选索引。
- 分类索引会为带 `genre[0]/...` 的来源 URL 写入 `genre_id`，用于排查相近分类；网页下拉框仍只显示分类名和数量，不显示编号。
- 网页右上角有全局搜索栏。`generate.py` 会生成轻量 `output/data/search_index.json`，搜索时先异步加载索引并在内存中过滤，再只加载当前搜索结果页需要的作品 JSON，避免每次搜索拉取全部分页。
- 分类切换和本地状态筛选使用 `output/data/filter_index/*.json`。有喜欢/不需要/玩过、隐藏已阅或作品类型筛选时，不再整类加载所有分页，而是先筛索引，再只读取当前结果页涉及的作品分页 JSON。
- 作品 JSON 会从 `#work_outline` 解析 `作品形式`。
- `作品形式` 命中 `音声・ASMR/ボイス・ASMR/ASMR` 时先识别为音声 ASMR，再根据 `asmr_subtitle_cache.json` 和 asmr-200 字幕 API 细化为 `有字幕ASMR` / `无字幕ASMR`；未确认的旧数据临时保留为 `音声・ASMR`。命中 `マンガ/漫画/コミック` 时归类为 `漫画`；两者都不命中时归类为 `游戏`。
- `python generate.py` 默认只使用字幕缓存细化作品类型；需要补查未知 RJ 时运行 `python generate.py --refresh-asmr-subtitles`。API 429 会按退避重试，并把确认结果写入 `asmr_subtitle_cache.json`。
- 更推荐用 `python update_asmr_subtitles.py` 维护缓存：它会让用户选择“补查未确认”或“复查无字幕”，然后再运行 `python generate.py` 刷新网页。
- `generate.py` 解析大量 `works/*.html` 时会使用线程池并显示 `解析HTML` 进度、速度和 ETA，避免在“找到 N 个HTML文件”后长时间无输出；分页 JSON 写入也改为定期报进度，避免几千页输出刷屏。
- `generate.py` 在 ASMR 字幕类型处理后会进入图片路径整理。这里会先为 `output/images/slider` 和 `output/images/parts` 建立一次性图片索引，并显示索引进度；之后再按作品扫描图片并显示进度，避免多版本同图复用时对每张图片都执行目录 `glob` 导致看起来卡住。
- 网页会按分类展示可选的 `work_kinds`，并提供“作品类型”多选筛选。
- 网页默认会把 DLsite 多语言版本折叠成一个作品展示，选择优先级是简体中文 > 繁体中文 > 其他；右下角“全部版本”按钮可以切换为展示同组所有版本。
- 多语言版本页如果没有旧版 `product-slider-data`，`generate.py` 会从 `translation-product-slider`、`og:image`、`twitter:image`、`image_main` 等字段补封面，并复用同组已有样品图，修复中文版封面空白的问题。
- 作品图片左上角的 `RJ/VJ` 编号徽标可点击复制，复制后短暂显示 `✓`。
- 网页使用浏览器 `localStorage` 保存每个作品的本地状态：`喜欢`、`不需要`、`玩过`、`已阅`。普通分类默认不显示已标记为喜欢/不需要/玩过的作品；分类下拉框会追加 `喜欢`、`不需要`、`玩过` 三个本地状态分类用于查看它们。右下角有“本页已阅”、“取消本页已阅”和“隐藏已阅”按钮，隐藏已阅默认关闭。

翻译稿生成规则：

- 只为缺译作品生成项目根目录 `待翻译/<作品ID>.md`。
- 如果存在对应的项目根目录 `翻译稿/<作品ID>.zh.md`，不会再次创建待翻译稿。
- `已翻译` 目录不再存放 `<作品ID>.zh.md`，只用于归档已经消费过的待翻译原稿。

注意：

- 如果修改了 `generate.py` 或分类数据，需要重新运行：

```powershell
python generate.py
```

### `md_to_json.py`

负责把翻译稿合并进展示 JSON。

当前翻译目录约定：

```text
./
  待翻译/
    RJxxxx.md
    VJxxxx.md
  翻译稿/
    RJxxxx.zh.md
    VJxxxx.zh.md

output/data/translate/
  已翻译/
    RJxxxx.md
    VJxxxx.md
    legacy_pages/
      translate_page_*.md
      translate_page_*.zh.md
```

合并流程：

1. 读取项目根目录 `翻译稿/RJxxxx.zh.md` 和 `翻译稿/VJxxxx.zh.md`。
2. 兼容旧格式 `translate_page_*.zh.md`，并会拆分迁移。
3. 将翻译应用到所有 `output/data/json/**/*.json`。
4. 如果 `待翻译/<作品ID>.md` 已有对应译文并已用于合并，则移动到 `已翻译/<作品ID>.md`。
5. `<作品ID>.zh.md` 保持在 `翻译稿/`。

运行：

```powershell
python md_to_json.py
```

### `open_page.py`

负责启动本地网页服务。

当前改动：

- 使用 `ThreadingTCPServer`。
- 设置 `Cache-Control: no-store` 等响应头。
- 自动打开带时间戳参数的 URL，避免浏览器缓存旧 `index.html`：

```text
http://localhost:8080/index.html?v=...
```

运行：

```powershell
python open_page.py
```

如果网页仍显示旧内容，先停掉旧服务，再重新运行：

```powershell
Ctrl+C
python open_page.py
```

### `retry_failed.py`

负责重试下载 `failed_works.md` 中记录的失败作品。

主要功能：
- 解析 `failed_works.md` 中的失败作品列表，提取作品 ID 和 URL
- 并发重试下载（默认 10 个并发）
- 每个作品最多重试 3 次，自动切换 URL 格式（announce ↔ work）
- 成功下载的作品从列表中移除
- 全部成功时自动删除 `failed_works.md`

相关常量：
```python
MAX_CONCURRENT = 10
MAX_RETRIES = 3
```

运行：
```powershell
python retry_failed.py
```

## 数据文件说明

### `works/*.html`

每个作品一个原始 HTML 文件，例如：

```text
works/RJ01605618.html
works/VJ01004768.html
```

有效作品 HTML 必须能解析出：

```html
<h1 id="work_name">...</h1>
```

如果没有作品名，crawler 会认为该文件无效，下次爬到该作品会重试下载。

### `works_order.json`

保存全部作品页面的首选展示顺序。

注意：它不是所有分类的唯一来源。分类归属以 `crawl_results.json` 为准。`generate.py` 会先按 `works_order.json` 排序，缺失的作品再用 `crawl_results.json` 的分类顺序补齐。

### `crawl_results.json`

保存分类和作品关系。

结构示例：

```json
{
  "categories": [
    {
      "name": "寝取り",
      "slug": "寝取り",
      "source_url": "...",
      "updated_at": "2026-05-01T20:17:47",
      "work_ids": ["RJ01613299", "RJ01576032"]
    }
  ]
}
```

重要约定：

- 一个作品 ID（RJ 或 VJ）可以出现在多个分类的 `work_ids` 中。
- crawler 再次写入同分类时会做并集合并，不会减少旧分类。

### `output/data/categories.json`

网页分类下拉框读取这个文件。
同时也读取每个分类里的 `work_kinds`，用来生成作品类型多选筛选。

结构示例：

```json
[
  {
    "name": "全部作品",
    "slug": "__all__",
    "count": 1749,
    "pages": 146,
    "data_path": "data/json/page_",
    "work_kinds": ["有字幕ASMR", "无字幕ASMR", "音声・ASMR", "漫画", "游戏"]
  },
  {
    "name": "寝取り",
    "slug": "寝取り",
    "count": 1484,
    "pages": 124,
    "data_path": "data/json/寝取り/page_",
    "work_kinds": ["游戏"]
  }
]
```

如果网页没有显示分类下拉框：

1. 确认 `output/index.html` 包含 `categorySelect`。
2. 确认 `output/data/categories.json` 有多个分类。
3. 重启 `open_page.py` 并使用带 `?v=` 的新 URL。

### `output/data/search_index.json`

网页搜索栏读取这个文件。每条索引只保存作品 ID、所在全部作品分页、作品类型、所属分类 slug 和预处理后的搜索文本；真正渲染搜索结果时再按分页读取 `output/data/json/page_*.json` 中的完整作品数据。

### `output/data/filter_index/*.json`

网页分类切换和本地状态筛选读取这些文件。每个分类一个索引，索引只保存作品 ID、该分类分页位置和作品类型；前端用它快速计算筛选后的分页，保持分类内原有顺序，并避免一次性拉取所有分类 JSON。

## 当前已知分类状态

最近修复后的分类关系：

- `全部作品`: 34468 个。
- `寝取り`: 2350 个。
- `屈辱`: 1785 个。

这些分类可以有交集，交集作品会同时显示在多个分类中。

## 推荐使用流程

### 爬取新分类

```powershell
python crawler.py "DLsite分类URL" 0
python generate.py
python md_to_json.py
python open_page.py
```

如果只想测试前几页：

```powershell
python crawler.py "DLsite分类URL" 2
```

如果要按顺序爬多个分类，可以直接运行：

```powershell
python crawler.py
```

然后逐行输入 URL；空回车表示队列输入结束并开始爬取。也可以用命令行一次传入多个 URL：

```powershell
python crawler.py "DLsite分类URL1" "DLsite分类URL2" 0
```

### 翻译新增作品

1. 运行 `generate.py` 后查看：

```text
待翻译/
```

2. 翻译 `RJxxxx.md` 或 `VJxxxx.md`。
3. 保存为：

```text
翻译稿/RJxxxx.zh.md
翻译稿/VJxxxx.zh.md
```

4. 合并翻译：

```powershell
python md_to_json.py
```

5. 刷新网页。

## 接手时最需要注意的坑

- 不要用 `works_order.json` 推断完整分类归属，它只代表全部作品页面的首选展示顺序。
- 分类归属应读取和维护 `crawl_results.json`。
- 不要把某个作品 ID 从其他分类里移除；一个 RJ 或 VJ 可以属于多个分类。
- `genre[0]/数字` 的分类名优先从 `list.devtools` 反查；如果旧数据里有同一 genre 的 `genre_name[0]`，也会作为备用映射。
- `已翻译/` 不是译文目录，译文目录是 `翻译稿/`。
- `translate_page_*.md` 是旧结构，已经归档到 `已翻译/legacy_pages/`。
- 作品页没有 `work_name` 时，需要尝试 `announce` URL。
- 修改生成逻辑后必须重新运行 `generate.py`，否则 `open_page.py` 打开的仍是旧 HTML。

## 最近变更

- 2026-05-02：项目链路支持 `VJ` 作品。`crawler.py` 现在从列表页提取 `RJ/VJ`，并保留真实 `work` 链接；`generate.py` 和 GUI 会读取 `RJ*.html` 与 `VJ*.html`；`md_to_json.py` 支持 `VJxxxx.zh.md` 和旧分页中的 `## VJxxxx` 翻译块。
- 2026-05-02：`crawler.py` 支持多链接队列。交互模式下逐行输入 URL，空回车结束输入并按顺序爬取；命令行也支持多个 URL 加最后一个页数参数。每个分类独立写入 `crawl_results.json`，本次队列的合并去重顺序会合并到 `works_order.json` 前面，旧排序保留在后面。
- 2026-05-02：修复全部作品页人气排序断层。`crawler.py` 不再用本次队列覆盖整个 `works_order.json`；`generate.py` 在 `works_order.json` 只覆盖部分作品时，会用 `crawl_results.json` 的分类顺序继续补齐。
- 2026-05-02：分类索引增加 `genre_id` 字段。`generate.py` 从分类 `source_url` 提取 `genre[0]` 写入 `output/data/categories.json`，但网页下拉框只显示分类名和数量，避免编号影响浏览。
- 2026-05-02：网页新增作品本地状态管理。每张作品卡有“喜欢 / 不需要 / 玩过”按钮，普通分类会过滤这三类作品；分类下拉框追加“喜欢 / 不需要 / 玩过”虚拟分类；右下角新增“本页已阅”、“取消本页已阅”和“隐藏已阅”按钮。状态保存在浏览器 `localStorage`，重新运行 `generate.py` 不会清空浏览器里的标记。
- 2026-05-02：作品卡左上角的 `RJ/VJ` 编号改为可点击复制的徽标，沿用现有复制反馈逻辑。
- 2026-05-02：作品类型增加 `漫画`。`generate.py` 从 `作品形式` 中识别 `マンガ/漫画/コミック` 为漫画；音声和漫画都不匹配时才归类为 `游戏`。
- 2026-05-03：网页新增右上角全局搜索栏。`generate.py` 输出 `output/data/search_index.json`，前端异步加载轻量索引并按当前分类、作品类型和本地状态过滤搜索结果；展示时只读取当前结果页需要的作品分页 JSON。
- 2026-05-03：修复搜索框左侧图标偶尔显示成“、”的问题。搜索图标从 CSS 伪元素改为内嵌 SVG，避免伪元素手柄偏移造成视觉杂点。
- 2026-05-03：改进 crawler 的 URL 历史记录功能。支持记忆所有用过的 URL，交互模式下显示历史 URL 列表并支持多选（用逗号分隔序号），新 URL 自动追加到历史中不重复。
- 2026-05-03：新增 retry_failed.py 脚本。解析 failed_works.md 并自动重试下载失败作品，支持 announce 和 work 格式 URL 自动切换，成功的作品从列表移除，全部成功则删除文件。
- 2026-05-08：优化网页初始加载和分类切换速度。`generate.py` 新增 `output/data/filter_index/*.json` 并在 `categories.json` 写入 `index_path`；前端有本地状态/作品类型/隐藏已阅筛选时改为索引分页加载，不再一次性拉取整个分类的所有 JSON。分页 JSON 和搜索索引改为紧凑 JSON 输出，轮播图也改为首图先加载、其余图片切换到时再加载。
- 2026-05-09：ASMR 字幕能力改为“作品类型”细化，而不是新增分类。`generate.py` 会把音声 ASMR 细化为 `有字幕ASMR` / `无字幕ASMR`，结果缓存到 `asmr_subtitle_cache.json`；`crawler.py` 增加只下载有字幕音声 ASMR 的选项并对 API 429 做重试。
- 2026-05-09：新增 `update_asmr_subtitles.py`，用于维护 ASMR 字幕缓存。脚本启动后由用户选择补查未确认作品或复查已标无字幕作品，429 会自动重试，确认结果写入 `asmr_subtitle_cache.json`；脚本带终端进度条和结束总结，并支持并发输入 `auto` 自动调整并发数。
- 2026-05-09：修正 `update_asmr_subtitles.py` 的自动并发策略。429 的 `Retry-After` 为 0 或缺失时至少等待 10 秒；auto 模式收到 429 会立即降并发并进入冷却，连续稳定多个窗口后才允许升并发，避免在限流附近反复震荡。进度条会根据终端编码自动选择方块或 ASCII 字符，避免管道/GBK 输出时编码失败。
- 2026-05-11：修复 DLsite 多语言版本的封面与重复展示问题。`generate.py` 会从翻译作品页的新结构补封面，并让同组版本复用已有样品图；默认只展示一个版本，优先简体中文，其次繁体中文，再其次其他语言，右下角“全部版本”按钮可切换全展示。HTML 解析阶段改为线程池并带进度/ETA，分页 JSON 写入改为定期报进度，避免 3 万多个 HTML 文件生成时看起来卡住或刷屏。
- 2026-05-11：继续优化 `generate.py` 的图片路径整理阶段。多版本同图复用从“每张图执行一次目录 glob”改为“启动时一次性建立图片 hash 索引”，并新增图片索引与 `扫描作品图片` 进度输出，修复 ASMR 字幕类型处理完成后静默很久的问题。

## GUI 应用 (`gui/`)

提供了一个基于 PyQt5 的图形界面，整合了核心流程。

### 文件结构

```text
gui/
  main.py      # GUI 主程序
  run.bat      # Windows 启动脚本
```

### 运行方式

```powershell
# 方式1：双击 gui/run.bat
# 方式2：命令行
python gui/main.py
```

### 功能

| 按钮 | 功能 |
|------|------|
| **开始爬取** | 输入URL和页数后，自动执行：爬取 → 生成网页数据 → 导入翻译 |
| **打开网页** | 启动 `open_page.py` 本地服务器 |

### 技术要点

- 使用 `QThread` 在后台执行耗时操作，避免阻塞 UI。
- `FullPipelineThread` 类整合了 crawler、generate、md_to_json 三个流程。
- `aiohttp.TCPConnector` 必须在 `async def` 内部创建，否则会报 `RuntimeError: no running event loop`。
- 启动 `open_page.py` 使用 `subprocess.Popen`，关闭 GUI 时会自动终止服务器进程。

### 依赖

```powershell
pip install PyQt5 aiohttp
```
