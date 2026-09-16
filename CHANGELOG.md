# Changelog

## Unreleased

### Fixes
- **高并发下 AstrBot / NapCat 卡死（并发治理全面加固）**: 修掉一组会互相放大的资源失控点——
  - **Chromium 进程炸弹**: `_get_shared_browser()` 的「检查 → 启动 → 赋值」存在竞态，并发首次渲染时多个协程各自 `launch` 一个 Chromium，瞬间拉起 N 个浏览器进程。现在整个流程用互斥锁串行化，并给 chromium 加上 `--disable-dev-shm-usage` 与堆上限；热重载更换事件循环后自动丢弃旧实例
  - **卡片 HTML 内存炸弹**: 缩略图改用降采样后的 data URI（最长边 640，JPEG），不再把数 MB 原图整张 base64 内联进模板，多图推文的 HTML 体积下降一个数量级
  - **browser context 泄漏**: `_render_card` 的 `ctx.close()` 原先只在成功路径执行，任何异常都会漏掉 context；改为 `finally` 中必定关闭，并新增 `render_concurrency`（默认 2）限制并发渲染数、`max_card_height`（默认 6000）限制截图高度
  - **NapCat 发送队列打爆**: 所有出站发送统一走 `_send()`，受 `send_concurrency`（默认 3）限制，同一会话的消息串行发出，不再出现多条推文的卡片/图片/视频互相穿插
  - **下载内存失控**: `_download_file` 改流式写盘并限制单文件大小（`max_download_mb`，默认 64），大视频不再被整个读进内存
  - **磁盘只增不减**: 卡片 PNG、下载的媒体与渲染 HTML 原先从不删除；现在发送完成后即回收，启动与每轮轮询都会清扫陈旧临时文件，terminate 精确回收本进程文件
  - **CPU 密集操作阻塞事件循环**: 取色量化（MCU 128 色）、配色求解、Jinja2 渲染、缩略图编码、图片下载与 JSON 落盘全部移入线程池
  - **twikit 调用无超时**: 全部 twikit 调用加 30s 超时与并发闸门（`twitter_concurrency`，默认 4）；文章抓取改用共享连接池并真正遵循 `proxy` 配置，不再每次新建 `httpx.AsyncClient`
  - **数据文件被截断**: 订阅/历史/token 统计改用「临时文件 + `os.replace`」原子写，消除并发读到的半截 JSON；高频写入合并为去抖落盘（在事件循环内快照、线程池中写盘）
  - **无界增长**: `_seed_cache` 加 FIFO 上限、推送历史上限新增 `history_max_entries`（默认 2000，永久保留时同样封顶）、历史清理加时间戳节流
  - **合并转发内联原图**: 卡片缩略图已降采样，但合并转发（`Node`）路径仍直接发送下载的 orig 原图。经核对 AstrBot 源码，`Node.to_dict()` 会把图片 `convert_to_base64()` 内联进单条 OneBot 报文，4 张大图可达数百 MB 并经 websocket 推给 NapCat——这正是「大 payload 打爆内存」的另一条通道。现在发送用图片统一降采样（`send_image_max_side`，默认 1280）并受单条消息内联总量上限约束（`send_image_max_total_mb`，默认 12MB）。实测（4096×4096 近不可压缩照片）：4 张原图的单条报文约 100MB → 压缩后约 3MB，降幅约 38 倍
  - **数据文件被空态覆盖（可能导致订阅永久丢失）**: `_load_data` 失败时会把内存订阅清空，而 terminate 又无条件全量落盘，一次瞬时 IO 抖动就能用空模板覆盖磁盘上完好的订阅（实测可复现）。现在加载失败时保留内存现状，并用 `_data_loaded_ok` / `_history_loaded_ok` 守卫：未成功加载过就拒绝覆盖写。同时区分两类失败以避免可用性死角——**内容损坏**（JSON 解析失败/结构非法）会把坏文件改名为 `.broken` 备份后放行，让用户能继续使用插件；**瞬时 IO 错误**（占用/权限）才保守拒绝写盘，下次启动重试
  - **发送结果被忽略导致推文永久丢失**: `context.send_message` 在找不到匹配平台时返回 `False` 且不抛异常，插件却仍记为推送成功并推进基线，该推文再也不会重推。现在 `_send` 的返回值决定会话成功与否，未投递时不推进基线、下轮补推
  - **`max_card_height` 未真正封顶**: `full_page=True` 会让 Playwright 重新测量文档真实高度，仅设置 viewport 高度并不能限制输出位图大小。改用 `clip` 明确指定截取区域，超长卡片不再生成巨幅位图
  - **`terminate` 越过宽限期删文件**: 退出时的强制清扫会无视 360s 宽限期直接删除媒体文件，而 NapCat 是独立进程、仍在令牌窗口内异步回拉，导致刚发出的视频发送失败。现在 `_sweep_own_temp_files` 不再强制删除宽限期内的文件
  - **proxy 热更新不同步**: 媒体下载/取色用的共享 `httpx` client 只在事件循环变化时重建，改代理后 Twitter 请求走新代理而下载仍走旧代理。现在把 proxy 纳入重建 key，并在 twikit 与 GraphQL/媒体下载三处统一显式 `trust_env=False`（「留空即直连」不再被 `HTTP_PROXY` 等环境变量悄悄接管，避免"Twitter 走环境代理、图片走直连"这类不一致）
  - **凭证轮换后文章抓取持续 403**: 共享 GraphQL client 把 `auth_token`/`ct0` 固化在 cookie 与 header 中，`set_credentials` 未重置它。现在凭证变更时一并重建
  - **OCR 阻塞事件循环**: `easyocr` 的 `Reader` 每次调用都重新加载数百 MB 模型，且加载与推理都是同步 CPU 操作、无法被 `wait_for` 取消。现在复用 Reader 单例并移入线程池；OCR 图片走统一下载路径（受大小上限与临时文件回收约束）
  - **ffmpeg 转换无超时**: 损坏/超长视频可让 ffmpeg 长时间不退出，而该协程持有推送锁，会卡住后续全部推送。新增 `gif_convert_timeout`（默认 120s）并在超时后终止进程
  - **退出时取消在途发送可能导致重复推送**: terminate 改为**两阶段收尾**——阶段1 只等待不取消（`asyncio.wait` 不取消任务），给在途 `send_message` 最多 3s 自然结束的窗口；阶段2 窗口用尽才 `cancel()` 并短暂等其真正退出，**之后**才关闭共享资源（避免任务在已关闭的 client/browser 上继续跑）
  - **孤儿 Chromium 进程**: `_close_shared_browser()` 此前零调用，热重载会遗留浏览器进程；现已在 terminate 中关闭
  - **同一条推文按会话重复建卡**: 监控循环原先逐会话调用 `_process_and_push(data, [sess_umo])`，同一推文有几个会话就要完整走几遍"下载媒体 → 翻译 → 取色 → 渲染卡片"，长文分块时开销再翻 N 倍——这是渲染风暴的主要来源之一。现在改为按**推文**维度聚合（`_push_pending_tweets`），同一条推文只建卡一次再复用给所有需要它的会话。实测 2 条推文 × 3 会话：建卡 **6 次 → 2 次**，发送次数与各会话基线推进语义完全不变
  - **发送图片压缩重复执行**: `_prepare_send_images` 原先在 `for session_umo` 循环内，多会话推送时同一批图被重复解码/重编码 N 次（N 倍 CPU + N 倍临时文件）。现移到循环外只压缩一次供所有会话复用
  - **压缩失败留下脱管临时文件**: `_shrink_image_file` 原先在 `im.save()` **之后**才登记临时文件，save 抛异常（磁盘满/编码失败）时该文件"两表都不在"，只能靠 6 小时后的陈旧清扫兜底。现在改为**先登记再写**，并在异常路径立即清理半截文件
  - **ffprobe 探测无超时**: ffmpeg 转换已加超时，但同函数更早的 ffprobe 帧率探测没有，损坏视频会让持有 `_push_lock` 的协程长时间卡住。现加 15s 超时并终止进程
  - **首次 OCR 仍阻塞事件循环**: `asyncio.to_thread(self._get_ocr_reader().readtext, ...)` 的参数会在调用前于事件循环线程求值，导致首次 OCR 仍同步加载数百 MB 模型。现改为分两步，模型构造与推理都在 worker 线程
  - **连接池泄漏**: 热改 proxy / 轮换凭证 / 更换事件循环时，被替换掉的 `httpx` 连接池此前直接丢弃而从不 `aclose()`，每次改配置泄漏一个连接池（FD/socket）。现在被替换的连接池排入待关闭队列，由 `close()` 统一回收
- **文章抓取的调试日志刷屏**: `extract_tweet_data` 里每条推文都会打的 `logger.warning`（文本长度、覆盖告警、取色告警、配色成功日志）降级为 `debug` 或移除
- **历史卡片每次重启重复拉取**: 启动重建任务的完整性判断改为按 `avatar_url`/`thumbnail_urls` 键是否存在（而非值真值），纯文字推文的空缩略图列表不再被误判为缺失；启动时仅当存在真正缺键的旧条目才触发重建，已完整的卡片不再每次重启重新请求 Twitter
- **渲染调试文件默认不再落盘**: `_dump_render_debug` 原先每次渲染都写 HTML + JSON 到 `data/config/debug_render`，改为由 `debug_render_dump`（默认关闭）控制

### Features
- **回退模型列表** (`text_translate_fallback_providers` / `image_translate_fallback_providers`): 主翻译模型请求失败、超时或返回空结果时，按配置顺序自动切换到备用模型；图片回退列表留空则沿用文字回退列表；token 统计覆盖失败尝试；dashboard 配置页提供 tag 式编辑器（回车/逗号添加、点击 × 移除、Backspace 删除末项），AstrBot 插件配置页使用多选 provider 列表
- **静默处理模式** (`silent_mode` 配置项): 收到推文链接后仅回复一次确认请求，直接发送解析结果（卡片/图片/视频），不再让 LLM 输出额外对话文本
- **并发与资源上限可配置**: 新增 `render_concurrency` / `send_concurrency` / `twitter_concurrency` / `max_download_mb` / `max_card_height` / `history_max_entries` / `debug_render_dump` / `send_image_max_side` / `send_image_max_total_mb` / `gif_convert_timeout` 配置项，默认值开箱即用，无需老用户改配置

### Tests
- 本次并发与资源治理在本地以 208 项断言做了回归验证，覆盖并发上限执行、临时文件生命周期、数据落盘保护（含损坏/瞬时错误/首次运行三类路径）、配置脏值健壮性、`terminate` 收尾顺序，以及用真实 Chromium 验证截图封顶确实生效（旧写法 20000px 页面产出 40000px，新写法正确封顶）；测试脚本未纳入版本库

## v2.0.0 (2026-08-03)

### Features
- **Cumulative token consumption**: Token stats now persist across restarts (`denpa_push_token_stats.json`) instead of resetting each run; overview subtitle updated accordingly
- **Overview cache size**: Status overview now shows the total on-disk size of the plugin's persistent data files (subscriptions, push-history cache, UI config/backgrounds, debug_render)
- **Sidebar ECG Logo — three-line sweep style**: Final redesign with dark base trace + theme-colored comet tail + white leading tip with glow, drawn in native 22×22 viewBox
- **Activity-driven logo speed**: Logo animation switches between three gears by daily push count (0 → 2.4s slow / 10 → 1.6s normal / 20 → 0.8s fast), with hover acceleration
- **Dynamic color integration**: Logo colors now follow the plugin's Material 3 dynamic palette (wallpaper accent extraction / custom brand color), auto-adapting to light & dark themes
- **ECG waveform generator** (`ecg-generator.js`): Gaussian pulse synthesis of physiological PQRST waveforms — PR / QRS / ST / QT intervals verified within normal ranges (75bpm), asymmetric T wave, baseline drift; parameterized SVG path & animated icon output; Node CLI + browser dual mode
- **Hero ECG waveform**: Waveform diversity with 15s rotation, speed/intensity driven by today's push count (capped at 20), rendering performance optimization
- **Hero ECG waveform — auto/manual mode**: Settings panel toggle between auto (waveform speed & complexity driven by today's push count) and manual (custom speed 20–120% + complexity 0–10 sliders); manual controls collapse/hide with animation in auto mode
- **Settings panel — conditional parameter blocks**: Parameter controls now collapse/hide when their governing option is off or switched — glow/shadow intensity (when their toggle is off), material opacity/blur (when material off or Mica), background-image scrim & upload (when bg mode ≠ image), theme color (when color mode ≠ static), custom backgrounds (when bg mode ≠ custom); unified `.collapse-block` animated collapse
- **Parallax wallpaper interactions**: Click / long-press / drag-select three-state interactions, configurable parallax mode, responsive sidebar
- **MD3 card rendering**: Layered card layout with full palette roles, per-avatar accent extraction, light/dark auto switching

### Fixes
- **Hero waveform ignores custom brand color in static color mode**: `applyPalette()` now dispatches a `palette-changed` event to invalidate the waveform's cached brand color, so static custom theme colors apply immediately
- **Tracked tweet cards stuck in dark theme**: Card MD3 palette is now re-derived per current theme (light included) instead of reusing backend-precomputed palettes that may be dark-only
- **Card theme switching & tab double-glass**: Fixed card theme toggle bug and duplicated glass layers inside tab content
- **ECG Logo white tip color**: Leading tip forced to pure white (Material `on-primary` can carry a hue tint); bumped cached resource version to force refresh
- **Resource cache invalidation**: Versioned `?v=` query strings for dashboard CSS/JS so updates always take effect

### Chores
- Dashboard asset cache versioning (`?v=` bump per release)
- Ruff formatting & metadata sync

## v1.1.0 (2026-05-22)

### Features
- **Plugin rename**: Renamed from `astrbot_plugin_twitter_monitor` to `astrbot_plugin_denpa_push`
- **Dynamic MD3 color palette**: Replaced 39 preset matching (CIELAB) with `material_color_utilities.theme_from_color()` — generates proper Material Design 3 light/dark schemes from any seed color using Google's Hct color science
- **Color extraction via QuantizerCelebi+Score**: Replaced 1×1 average pixel with `prominent_colors_from_image()`, matching the same algorithm used by the Material Design reference project
- **Recursive retweet handling**: Pure retweets are now resolved and displayed in the quote tweet card style, with full text, media, and NoteTweet/Article content extracted from `tweet.retweeted_tweet`
- **Layered card template**: New MD3 card layout — `background` full card → `surface_container` text pad → `on_surface`/`on_surface_variant` text hierarchy
- **Full MD3 palette roles**: Added `background`, `surface_container`, `on_surface_variant` to complement the existing 6 core roles
- **Parallel LLM translation**: Long article texts are now split and translated concurrently via `asyncio.gather`
- **Quoted article translation**: Quoted tweet article text (NoteTweet/Note) is now included in the translation prompt

### Fixes
- **NoteTweet truncated text**: Fixed order of text source priority — `note_tweet.text` (full 1599 chars) now checked before `legacy.full_text` (301-char preview), restoring full bio tweets from accounts like @MimikuWo
- **Avatar CDN fallback**: Added `User-Agent`, `Accept`, `Referer` headers to avatar download; fallback through `_400x400` → `_bigger` → `_normal` size; gray seed fallback using `user_id` hash for per-user variation
- **Hex color extraction**: Fixed crash when `prominent_colors_from_image` returns 6-char hex (`#RRGGBB`) instead of 8-char ARGB — code now indexes `h[0:2]`, `h[2:4]`, `h[4:6]` correctly
- **pure retweet text**: Resolved "RT @user: https://t.co/..." placeholder by recursively extracting the retweeted tweet's full content via `tweet.retweeted_tweet`
- **Fallback article fetch**: When twikit's `Article` detection misses, falls back to fetching via the Article/Longform endpoint
- **created_at_datetime strptime**: Wrapped twikit's datetime parsing in try/except — older Python versions without `%z` support in `strptime` now fall back to the raw time string
- **Full text override**: Ensured raw GraphQL `full_text` overrides twikit's truncated `tweet.text` in all code paths

### Chores
- Ruff formatted `main.py` and `twitter_client.py`
- README and metadata synced from master
