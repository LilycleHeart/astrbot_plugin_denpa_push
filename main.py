import asyncio
import itertools
import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image as CompImage, Video as CompVideo, Plain
from astrbot.api.star import Context, Star, register

from .twitter_client import TwitterClient

DATA_DIR = "data/config"
DATA_FILE = "astrbot_plugin_denpa_push_data.json"

# 同一条推文连续多少轮推送失败后强制跳过(防止会话基线永久卡死在某条上)
PUSH_MAX_FAIL_ROUNDS = 5


def _plain(text: str) -> MessageChain:
    """Create a plain text MessageChain."""
    chain = MessageChain()
    chain.chain.append(Plain(text))
    return chain


def _img(url: str) -> MessageChain:
    """Create an image MessageChain from URL or file path."""
    chain = MessageChain()
    if url.startswith("http"):
        chain.chain.append(CompImage.fromURL(url))
    else:
        chain.chain.append(CompImage.fromFileSystem(url))
    return chain


def _chain(components: list) -> MessageChain:
    """Create a MessageChain from a list of components."""
    chain = MessageChain()
    for c in components:
        chain.chain.append(c)
    return chain


def _unwrap_event(event) -> AstrMessageEvent:
    """兼容 AstrBot v4.26.0: event 可能是 ContextWrapper 或 AstrMessageEvent。"""
    if hasattr(event, "context") and hasattr(event.context, "event"):
        return event.context.event
    return event

# ═══════════════════════════════════════════════════════════
# 并发基础设施
# ═══════════════════════════════════════════════════════════
# 插件热重载会换事件循环, 绑定在已关闭 loop 上的 Lock/Semaphore 一旦复用会抛
# RuntimeError, 因此统一按 "当前 loop + 当前配置" 惰性重建。


class _LoopBoundSemaphore:
    """跨事件循环安全的惰性信号量, 并发上限变化时自动重建。"""

    def __init__(self, size_getter):
        self._get_size = size_getter
        self._sem = None
        self._key = None

    def get(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        size = max(1, int(self._get_size()))
        key = (loop, size)
        if self._sem is None or self._key != key:
            self._sem = asyncio.Semaphore(size)
            self._key = key
        return self._sem


class _LoopBoundLock:
    """跨事件循环安全的惰性互斥锁。"""

    def __init__(self):
        self._lock = None
        self._loop = None

    def get(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop
        return self._lock


# ═══════════════════════════════════════════════════════════
# 共享 Playwright (module-level, survives plugin reload)
# ═══════════════════════════════════════════════════════════
_pw_instance = None
_pw_browser = None
_pw_browser_loop = None
_pw_lock = _LoopBoundLock()

_TEMP_PREFIX = "astrbot_twitter_"
_tmp_counter = itertools.count()
# 每个目标路径一把进程内锁: Windows 上 os.replace 若目标正被另一线程替换/占用,
# 会抛 WinError 5(拒绝访问), 必须把 "写临时文件 + 替换" 串行化。
_atomic_locks = {}
_atomic_locks_guard = threading.Lock()


def _atomic_lock_for(path: str) -> threading.Lock:
    with _atomic_locks_guard:
        lock = _atomic_locks.get(path)
        if lock is None:
            lock = _atomic_locks[path] = threading.Lock()
        return lock


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _shrink_image_file(path: str, max_side: int) -> str:
    """把图片压成"发送用"副本, 返回新路径; 失败返回原路径。

    用于合并转发/图片消息: 这些内容会被 AstrBot 转成 base64 内联进 OneBot 报文,
    直接发 orig 原图会让单条消息膨胀到几百 MB。压缩后再发可把体积降一个数量级。
    """
    if not path or max_side <= 0:
        return path
    name = None
    try:
        import tempfile

        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            if max(im.size) <= max_side:
                return path  # 已经足够小, 不重复编码
            im.thumbnail((max_side, max_side), Image.LANCZOS)
            out = tempfile.NamedTemporaryFile(
                suffix=".jpg", delete=False, prefix=_TEMP_PREFIX
            )
            name = out.name
            out.close()
            # 必须先登记再写: 若 save 抛异常(磁盘满/编码失败), 这份临时文件
            # 否则会"两表都不在"而脱管, 只能等 6 小时后的陈旧清扫兜底
            _register_temp(name)
            im.save(name, format="JPEG", quality=82, optimize=True)
        return name
    except Exception as e:
        logger.warning(f"[DenpaPush] shrink image failed: {e}")
        # 写失败的半截文件立即清掉, 不留在临时目录
        _remove_temp(name)
        return path


def _atomic_write_json(path: str, payload) -> None:
    """原子写 JSON: 先写临时文件再 os.replace。

    原实现直接 open(path, "w") 截断后就地写, 与并发读取方交叠时会读到空/半截
    文件(表现为订阅或历史莫名丢失); os.replace 在同一文件系统上是原子的。
    同一路径的并发写用进程内锁串行化, 规避 Windows 的 WinError 5。
    """
    tmp = f"{path}.{os.getpid()}.{next(_tmp_counter)}.tmp"
    try:
        with _atomic_lock_for(path):
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            for attempt in range(3):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    # 目标可能被杀毒/索引器短暂占用, 退避重试
                    if attempt == 2:
                        raise
                    time.sleep(0.05 * (attempt + 1))
    finally:
        _remove_temp(tmp)


_TRACKED_TEMP = set()
_TRACKED_TEMP_GUARD = threading.Lock()


def _register_temp(path):
    """登记本进程产生的临时文件, 供退出时精确回收。"""
    if path:
        with _TRACKED_TEMP_GUARD:
            _TRACKED_TEMP.add(path)
    return path


def _forget_temp(path) -> None:
    if path:
        with _TRACKED_TEMP_GUARD:
            _TRACKED_TEMP.discard(path)


def _remove_temp(path) -> None:
    _forget_temp(path)
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# 媒体文件回收宽限期(秒)。必须覆盖 NapCat 回拉文件的窗口:
# AstrBot 的 Video.to_dict() 只是把本地路径注册成一次性令牌(默认 300s),
# 真正的文件传输是 NapCat 之后异步通过 HTTP 回拉完成的。因此发送调用返回后
# 立刻删文件会与之竞态, 导致视频消息发送失败。宽限期取 300s 令牌有效期再多留余量。
_TEMP_GRACE_SECONDS = 360.0
# {path: 可删除的时间戳} —— 待回收的临时文件
_PENDING_REMOVE = {}
_PENDING_GUARD = threading.Lock()


def _release_temp(paths) -> None:
    """把临时文件标记为「宽限期后可删除」, 并顺手回收已到期的文件。

    刻意不用 asyncio.sleep 长任务: 每条推文一个 6 分钟休眠任务会随推送量堆积,
    这里只登记时间戳, 由下一次释放/清扫机会式回收, 无任务、有界。
    """
    now = time.time()
    with _PENDING_GUARD:
        for p in paths:
            if p:
                _PENDING_REMOVE.setdefault(p, now + _TEMP_GRACE_SECONDS)
    # 移出"使用中"登记表: 从此这些文件由待回收表负责, 避免两表同时持有
    # 导致 terminate 的宽限期判断把它们当成仍在使用的文件
    for p in paths:
        _forget_temp(p)
    with _PENDING_GUARD:
        # 机会式回收已到期的文件
        due = [p for p, ts in _PENDING_REMOVE.items() if ts <= now]
        for p in due:
            _PENDING_REMOVE.pop(p, None)
    for p in due:
        _remove_temp(p)


def _flush_released_temp(force: bool = False) -> int:
    """回收已过宽限期的临时文件; force=True 时全部回收(退出时用)。"""
    now = time.time()
    with _PENDING_GUARD:
        due = [
            p
            for p, ts in _PENDING_REMOVE.items()
            if force or ts <= now
        ]
        for p in due:
            _PENDING_REMOVE.pop(p, None)
        remaining = list(_PENDING_REMOVE)
    for p in due:
        _remove_temp(p)
    # 已被其他途径(如陈旧清扫)删掉的路径也摘除, 否则待回收表会慢慢长大。
    # 存在性检查放在锁外做, 避免持锁期间做文件 IO。
    for p in remaining:
        if not os.path.exists(p):
            with _PENDING_GUARD:
                _PENDING_REMOVE.pop(p, None)
    return len(due)


def _sweep_own_temp_files() -> int:
    """退出时回收本进程的临时文件 —— 但绝不越过宽限期。

    关键约束: NapCat 是独立进程, 并不随 AstrBot 退出而停止回拉文件, 而插件
    reload/停用会频繁触发 terminate。若在这里强制删掉仍在 NapCat 令牌窗口
    (300s) 内的文件, 刚发出的视频/图片就会回拉失败。
    因此这里只删已过宽限期的文件; 宽限期内的留在磁盘上, 仅取消登记,
    交给下次启动的 _sweep_stale_temp_files 按 mtime 回收(成为可被收拾的孤儿)。
    """
    removed = _flush_released_temp()  # 注意: 不带 force
    with _TRACKED_TEMP_GUARD:
        # 仍在宽限期内(或尚未释放)的文件不删除, 只从登记表摘除,
        # 使其不再被"跳过清理"逻辑保护, 从而能被后续的陈旧清扫回收
        _TRACKED_TEMP.clear()
    return removed


def _sweep_stale_temp_files(max_age_seconds: float = 6 * 3600) -> int:
    """清理上次运行残留的卡片 HTML/PNG 与媒体临时文件。

    原实现从不删除已发送的卡片 PNG 与下载的媒体, 磁盘只增不减; 这里按
    修改时间清理陈旧文件(默认 6 小时), 既回收空间又不会误删在途文件。
    """
    import tempfile
    import time as _time

    removed = 0
    try:
        tmp_dir = tempfile.gettempdir()
        now = _time.time()
        with _TRACKED_TEMP_GUARD:
            tracked = set(_TRACKED_TEMP)
        for name in os.listdir(tmp_dir):
            if not name.startswith(_TEMP_PREFIX):
                continue
            p = os.path.join(tmp_dir, name)
            # 本进程仍在使用的文件不清理
            if p in tracked:
                continue
            try:
                if os.path.isfile(p) and now - os.path.getmtime(p) > max_age_seconds:
                    os.remove(p)
                    removed += 1
            except OSError:
                continue
        # 顺带丢弃已不存在的登记项(渲染失败时 PNG 路径已登记但从未生成),
        # 保证登记表本身不会随运行时长增长
        with _TRACKED_TEMP_GUARD:
            for p in list(_TRACKED_TEMP):
                if not os.path.exists(p):
                    _TRACKED_TEMP.discard(p)
    except OSError:
        pass
    return removed


async def _get_shared_browser():
    """Lazy-init and return a persistent Chromium browser shared across all instances.

    并发安全: 整个 "检查 → 启动 → 赋值" 过程串行化。原实现在并发首次渲染时
    会有多个协程同时看到 _pw_browser 为 None, 各自 launch 一个 Chromium,
    短时间内拉起 N 个浏览器进程吃光内存, 导致 AstrBot 与 NapCat 一起卡死。
    """
    global _pw_instance, _pw_browser, _pw_browser_loop

    loop = asyncio.get_running_loop()
    # 热重载/换事件循环后旧实例绑在已关闭的 loop 上, 必须丢弃重建
    if _pw_browser_loop is not None and _pw_browser_loop is not loop:
        _pw_browser = None
        _pw_instance = None
        _pw_browser_loop = None

    if _pw_browser is not None:
        try:
            if _pw_browser.is_connected():
                return _pw_browser
        except Exception:
            pass
        _pw_browser = None

    async with _pw_lock.get():
        if _pw_browser is not None:
            try:
                if _pw_browser.is_connected():
                    return _pw_browser
            except Exception:
                pass
            _pw_browser = None
        if _pw_instance is None:
            from playwright.async_api import async_playwright

            _pw_instance = await async_playwright().start()
        _pw_browser = await _pw_instance.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--mute-audio",
                "--no-first-run",
                # 单页渲染用不到大堆内存, 压低上限避免多开时 OOM
                "--js-flags=--max-old-space-size=256",
            ],
        )
        _pw_browser_loop = loop
        logger.info("[DenpaPush] Chromium launched for card rendering")
        return _pw_browser


async def _close_shared_browser():
    """Close shared Playwright and browser."""
    global _pw_instance, _pw_browser, _pw_browser_loop
    browser, instance = _pw_browser, _pw_instance
    _pw_browser = None
    _pw_instance = None
    _pw_browser_loop = None
    if browser:
        try:
            await browser.close()
        except Exception:
            pass
    if instance:
        try:
            await instance.stop()
        except Exception:
            pass


def _twitter_media_url(url: str, size: str = "orig") -> str:
    """Set Twitter media size suffix, handling both :name and ?name= formats."""
    if not url or "pbs.twimg.com" not in url:
        return url
    # strip existing :name suffix
    for s in (":thumb", ":small", ":medium", ":large", ":orig"):
        if url.endswith(s):
            url = url[: -len(s)]
            break
    # strip existing ?name= query param
    url = re.sub(r"\?format=\w+&name=\w+", "", url)
    return f"{url}:{size}"


def _file_to_data_uri(path: str, max_side: int = 0) -> str:
    """Read a local image file and return a base64 data URI.

    max_side > 0 时先用 PIL 等比降采样到最长边不超过该值再编码。卡片里只显示
    缩略图, 内联原图(单张可达数 MB)会按 base64 放大约 1.33 倍, 一张多图推文的
    HTML 就能涨到几十 MB, 多路并发渲染时直接把内存打满。
    """
    import base64
    import mimetypes

    if max_side > 0:
        try:
            import io

            from PIL import Image

            with Image.open(path) as im:
                im = im.convert("RGB")
                if max(im.size) > max_side:
                    im.thumbnail((max_side, max_side), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=82, optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode()
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            logger.warning(f"[DenpaPush] Thumbnail downscale failed, using original: {e}")

    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"


@register(
    "astrbot_plugin_denpa_push",
    "astrbot_user",
    "Twitter/X 推文监控、翻译与推送插件",
    "1.0.0",
)
class DenpaPushPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.twitter = TwitterClient()
        self.subscriptions = {}  # {session_umo: {username: {info}}}
        self.monitored_sessions = set()
        self.monitor_task = None
        self._rebuild_task = None
        self._running = False
        self._rebuild_running = False
        self._data_path = self._get_data_path()
        self._seed_cache = {}  # {image_url: rgb_tuple}
        self._push_logs = deque(maxlen=100)  # dashboard 信号日志
        self._push_history = deque()  # dashboard 推送历史(含卡片详情)，按保留天数清理
        self._total_pushes = 0
        self._push_fail_counts = {}  # {(session_umo, username, tweet_id): 连续失败轮数}
        self._last_prune_at = None  # 历史清理节流时间戳
        # 加载成功标志: 为 False 时禁止覆盖写磁盘, 防止一次读取失败把
        # 磁盘上完好的订阅/历史按空模板覆盖掉(数据永久丢失)
        self._data_loaded_ok = False
        self._history_loaded_ok = False
        self._token_stats = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
        # 共享连接池 + 并发信号量: 全局限制 LLM 与 HTTP 下载并发, 防止积压时连接风暴
        self._http_client = None
        self._http_client_key = None
        self._register_dashboard_apis(context)
        # 注册并发闸门(按当前事件循环 + 配置惰性重建)
        self._http_semaphore = _LoopBoundSemaphore(self._http_concurrency)
        self._llm_semaphore = _LoopBoundSemaphore(self._llm_concurrency)
        self._render_semaphore = _LoopBoundSemaphore(self._render_concurrency)
        self._send_semaphore = _LoopBoundSemaphore(self._send_concurrency)
        self._push_lock = _LoopBoundLock()
        self._pending_saves = {}

    @staticmethod
    def _as_int(value, default: int, minimum: int = 1, maximum: int = 64) -> int:
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default

    def _http_concurrency(self) -> int:
        return self._as_int(self.config.get("http_concurrency", 8), 8)

    def _llm_concurrency(self) -> int:
        return self._as_int(self.config.get("llm_concurrency", 3), 3)

    def _render_concurrency(self) -> int:
        """同时进行的 Playwright 渲染数。"""
        return self._as_int(self.config.get("render_concurrency", 2), 2, 1, 8)

    def _send_concurrency(self) -> int:
        """同时进行的平台出站发送数, 保护 NapCat/OneBot 不被消息洪水打爆。"""
        return self._as_int(self.config.get("send_concurrency", 3), 3, 1, 16)

    def _llm_timeout(self) -> float:
        try:
            return max(5.0, float(self.config.get("llm_timeout", 120)))
        except (TypeError, ValueError):
            return 120.0

    def _get_http_semaphore(self) -> asyncio.Semaphore:
        return self._http_semaphore.get()

    def _get_llm_semaphore(self) -> asyncio.Semaphore:
        return self._llm_semaphore.get()

    def _get_render_semaphore(self) -> asyncio.Semaphore:
        return self._render_semaphore.get()

    def _get_send_semaphore(self) -> asyncio.Semaphore:
        return self._send_semaphore.get()

    async def _send(self, session_umo: str, chain: MessageChain) -> bool:
        """受全局信号量保护的出站发送。

        并发推送多条推文时, 无节制的 send_message 会把大量(尤其是含大图/视频的)
        消息同时压进 NapCat 的发送队列, NapCat 内存暴涨后与 AstrBot 一起卡死。
        这里限制在途发送数, 并让单次发送失败只影响当前会话。
        """
        async with self._get_send_semaphore():
            return bool(await self.context.send_message(session_umo, chain))

    def _get_http_client(self):
        """懒加载一个全局共享的 httpx.AsyncClient(连接池)。

        复用同一个 client(而非每个文件新建)让 TCP+TLS 连接在多张图片下载间保活,
        避免积压时几十个裸连接同时握手。proxy 由配置决定。
        重建条件: 事件循环更换(插件热重载后旧 loop 已关闭) 或 proxy 配置变更
        —— 后者若不重建, 热改代理后媒体下载与取色仍走旧代理, 与 twikit 侧不一致。
        """
        import httpx

        loop = asyncio.get_running_loop()
        proxy = (self.config.get("proxy", "") or "").strip() or None
        key = (loop, proxy)
        if self._http_client_key != key:
            # 旧 client 绑定的 loop/proxy 已失效, 丢弃重建
            if self._http_client is not None and not self._http_client.is_closed:
                try:
                    asyncio.get_running_loop().create_task(
                        self._http_client.aclose()
                    )
                except Exception:
                    pass
            self._http_client = None
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                proxy=proxy,
                timeout=httpx.Timeout(60.0, connect=15.0),
                limits=httpx.Limits(
                    max_connections=20, max_keepalive_connections=10
                ),
                follow_redirects=True,
                # trust_env=False: "proxy 留空" 就是直连, 不应被 HTTP_PROXY 等
                # 环境变量悄悄接管, 否则行为依赖运行环境难以排查
                trust_env=False,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            self._http_client_key = key
        return self._http_client

    def _max_download_bytes(self) -> int:
        """单文件下载上限(MB → 字节), 防止超大视频把内存吃满。"""
        return self._as_int(self.config.get("max_download_mb", 64), 64, 1, 1024) * 1024 * 1024

    async def _download_file(self, url, suffix=".jpg", timeout=60.0):
        """用共享连接池流式下载到临时文件, 受 http 信号量限流且限制单文件大小。

        原实现 r = await client.get(url) 会把整个响应体读进内存再写盘, 一个几百 MB
        的视频就能顶爆内存; 改流式写盘并边下边累计字节数, 超限立即中止。
        """
        if not url:
            return None
        try:
            import tempfile

            client = self._get_http_client()
            max_bytes = self._max_download_bytes()
            async with self._get_http_semaphore():
                ext = suffix
                for s in [".mp4", ".gif", ".jpg", ".jpeg", ".png", ".webp"]:
                    if s in url.lower():
                        ext = s
                        break
                tmp = tempfile.NamedTemporaryFile(
                    suffix=ext, delete=False, prefix=_TEMP_PREFIX
                )
                name = _register_temp(tmp.name)
                total = 0
                try:
                    async with client.stream("GET", url, timeout=timeout) as r:
                        r.raise_for_status()
                        declared = r.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > max_bytes:
                            logger.warning(
                                f"[DenpaPush] Skip oversized media "
                                f"({int(declared) // 1048576}MB): {url[:60]}"
                            )
                            tmp.close()
                            _remove_temp(name)
                            return None
                        async for chunk in r.aiter_bytes(65536):
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError(
                                    f"download exceeds {max_bytes // 1048576}MB limit"
                                )
                            tmp.write(chunk)
                except Exception:
                    tmp.close()
                    _remove_temp(name)
                    raise
                tmp.close()
                return name
        except Exception as e:
            logger.warning(f"Media download failed: {url[:60]} - {e}")
            return None

    async def _llm_generate(self, provider_id, prompt, image_urls=None, timeout=None):
        """经全局信号量限流的 LLM 调用, 可选超时。

        文字分块翻译与图片翻译共享同一个信号量, 从源头限制并发 LLM 请求数,
        并在挂起时通过 wait_for 超时切断, 避免卡死整条处理链。
        """
        async with self._get_llm_semaphore():
            kwargs = {"chat_provider_id": provider_id, "prompt": prompt}
            if image_urls:
                kwargs["image_urls"] = image_urls
            coro = self.context.llm_generate(**kwargs)
            if timeout:
                return await asyncio.wait_for(coro, timeout=timeout)
            return await coro

    async def _llm_generate_with_fallback(
        self, provider_ids, prompt, image_urls=None, timeout=None
    ):
        """按顺序尝试 provider 链, 单次调用内完成回退(不做跨请求记忆)。

        回退触发条件(与 AstrBot 核心 fallback_chat_models 一致):
          - 调用抛异常(超时 / 网络 / 鉴权 / 限流等)
          - 返回 None 或 completion_text 为空白(视为本次请求失败)
        每个拿到的响应都会计入 token 统计 —— 失败的尝试同样消耗了 token。
        全部候选都失败时返回最后一次的响应(可能是 None), 由调用方决定兜底文案。
        """
        chain = [p for p in (provider_ids or []) if p]
        if not chain:
            return None

        last_resp = None
        last_error = None
        for idx, pid in enumerate(chain):
            try:
                resp = await self._llm_generate(
                    provider_id=pid,
                    prompt=prompt,
                    image_urls=image_urls,
                    timeout=timeout,
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    f"[DenpaPush] LLM provider `{pid}` 调用失败: "
                    f"{type(e).__name__}: {e}"
                )
                if idx < len(chain) - 1:
                    logger.info(f"[DenpaPush] 切换到回退模型 `{chain[idx + 1]}`")
                continue

            if resp is not None:
                self._track_token_usage(resp)
            text = getattr(resp, "completion_text", None)
            if text and text.strip():
                if idx > 0:
                    logger.info(
                        f"[DenpaPush] 回退模型 `{pid}` 返回成功(主模型: `{chain[0]}`)"
                    )
                return resp

            # 空响应同样视为失败, 继续尝试下一个候选
            last_resp = resp
            if idx < len(chain) - 1:
                logger.warning(
                    f"[DenpaPush] LLM provider `{pid}` 返回空结果, "
                    f"切换到回退模型 `{chain[idx + 1]}`"
                )

        if last_error and last_resp is None:
            logger.warning(
                f"[DenpaPush] 全部 {len(chain)} 个模型均失败, "
                f"最后错误: {type(last_error).__name__}: {last_error}"
            )
        return last_resp

    def _get_data_path(self):
        root = getattr(self.context, "astrbot_root", os.getcwd())
        return os.path.join(root, DATA_DIR, DATA_FILE)

    @property
    def _push_history_path(self):
        return os.path.join(os.path.dirname(self._data_path), "denpa_push_history.json")

    @property
    def _token_stats_path(self):
        return os.path.join(os.path.dirname(self._data_path), "denpa_push_token_stats.json")

    def _is_transient_failure(self, e: Exception) -> bool:
        """判断推送失败是否属于暂时性故障(掉线/网络/超时/平台抖动)。

        暂时性失败不累计跳过轮数, 恢复后自动补推; 只有持久性失败
        (推文内容/渲染本身有问题)才累计并触发 PUSH_MAX_FAIL_ROUNDS 强制跳过。

        优先按异常类型识别(部分异常无错误信息, 如 aiocqhttp.ApiNotAvailable):
        - aiocqhttp.NetworkError / ApiNotAvailable → 掉线/超时, 临时
        - aiocqhttp.ActionFailed → OneBot 执行失败(禁言/非好友/目标不存在), 持久
        - websockets.ConnectionClosed → 连接断开, 临时
        """
        try:
            from aiocqhttp.exceptions import (
                ActionFailed,
                ApiNotAvailable,
                NetworkError,
            )

            if isinstance(e, ActionFailed):
                return False
            if isinstance(e, (NetworkError, ApiNotAvailable)):
                return True
        except ImportError:
            pass
        try:
            from websockets.exceptions import ConnectionClosed

            if isinstance(e, ConnectionClosed):
                return True
        except ImportError:
            pass

        import httpx as _httpx

        msg = str(e)
        if isinstance(e, (_httpx.ConnectError, _httpx.TimeoutException)):
            return True
        transient = (
            "timeout",
            "timed out",
            "timedout",
            "connection",
            "connect",
            "network",
            "socket",
            "ssl",
            "handshake",
            "dns",
            "name or service",
            "certificate",
            "reset",
            "aborted",
            "closed",
            "refused",
            "broken pipe",
            "websocket",
            "api not available",
            "http request failed",
            "offline",
            "disconnected",
            "连接失败",
            "连接超时",
            "网络",
            "掉线",
            "离线",
            "超时",
        )
        return any(k in msg.lower() for k in transient)

    def _load_push_history(self):
        """加载推送历史; 读取失败时保留内存现状并不允许覆盖写。

        与订阅同理: 一次读取抖动不该让整个历史被空列表覆盖掉。
        内容损坏时备份后放行(历史属可再生成的次要数据, 不该因此锁死写入)。
        """
        self._history_loaded_ok = False
        if not os.path.exists(self._push_history_path):
            self._history_loaded_ok = True
            self._prune_push_history(quiet=True)
            return
        try:
            with open(self._push_history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, UnicodeDecodeError) as e:
            self._quarantine_broken_file(self._push_history_path, "push history", e)
            self._history_loaded_ok = True
            self._prune_push_history(quiet=True)
            return
        except Exception as e:
            logger.error(
                f"[DenpaPush] Failed to read push history (transient), "
                f"refusing to overwrite: {e}"
            )
            self._prune_push_history(quiet=True)
            return

        try:
            # 向后兼容：旧格式是 list，新格式是 {"history": [...], "total_pushes": N}
            if isinstance(data, list):
                self._push_history = deque(data)
                self._total_pushes = len(self._push_history)
            elif isinstance(data, dict):
                hist = data.get("history", [])
                self._push_history = deque(hist)
                self._total_pushes = data.get("total_pushes", len(self._push_history))
            else:
                raise ValueError(f"unexpected history root: {type(data).__name__}")
            self._history_loaded_ok = True
        except Exception as e:
            self._quarantine_broken_file(self._push_history_path, "push history", e)
            self._history_loaded_ok = True
        # 按保留天数清理过期卡片
        self._prune_push_history(quiet=True)

    def _history_retention_days(self) -> int:
        """追踪卡片保留天数（0 = 永久保留）。"""
        try:
            return int(self.config.get("history_retention_days", 30) or 0)
        except (TypeError, ValueError):
            return 30

    def _prune_push_history(self, quiet: bool = False):
        """按保留天数清理过期追踪卡片。

        自动清除关闭或保留天数为 0 时不做任何事（永久保留）。
        无有效时间戳的条目一律保留，避免误删。
        高频推送时用时间戳节流, 避免每条推文都全量扫描一次历史。
        """
        auto_clean = self.config.get("history_auto_clean", True)
        if isinstance(auto_clean, str):
            auto_clean = auto_clean.strip().lower() not in ("0", "false", "no", "")
        if not auto_clean:
            return
        days = self._history_retention_days()
        if days <= 0:
            return
        now = datetime.now(timezone.utc)
        last = getattr(self, "_last_prune_at", None)
        if quiet and last is not None and (now - last).total_seconds() < 60:
            return
        self._last_prune_at = now
        cutoff = now - timedelta(days=days)
        kept = []
        dropped = 0
        for entry in self._push_history:
            raw = entry.get("time", "") if isinstance(entry, dict) else ""
            try:
                t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
            except Exception:
                kept.append(entry)  # 无有效时间戳的条目保留
                continue
            if t >= cutoff:
                kept.append(entry)
            else:
                dropped += 1
        if dropped:
            self._push_history.clear()
            self._push_history.extend(kept)
            self._schedule_save("push_history", self._write_history_payload, self._history_payload)
            if not quiet:
                self._log_push(
                    f"已自动清理 {dropped} 条超过 {days} 天的追踪卡片", "info"
                )

    def _save_push_history(self):
        # 历史从未成功加载过时拒绝写入, 防止用空历史覆盖磁盘上的既有记录
        if not getattr(self, "_history_loaded_ok", False):
            logger.warning(
                "[DenpaPush] Skip saving push history: initial load never "
                "succeeded, refusing to overwrite existing history file"
            )
            return
        try:
            os.makedirs(os.path.dirname(self._push_history_path), exist_ok=True)
            _atomic_write_json(self._push_history_path, self._history_payload())
        except Exception as e:
            logger.warning(f"[DenpaPush] Failed to save push history: {e}")

    def _load_token_stats(self):
        """从磁盘加载累计 token 统计（跨重启累计）。"""
        try:
            if os.path.exists(self._token_stats_path):
                with open(self._token_stats_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                stats = {
                    k: int(data.get(k, 0) or 0)
                    for k in ("prompt", "completion", "total", "calls")
                }
                if not stats.get("total"):
                    stats["total"] = stats["prompt"] + stats["completion"]
                self._token_stats.update(stats)
        except Exception as e:
            logger.warning(f"[DenpaPush] Failed to load token stats: {e}")

    def _save_token_stats(self):
        try:
            os.makedirs(os.path.dirname(self._token_stats_path), exist_ok=True)
            _atomic_write_json(self._token_stats_path, self._token_stats)
        except Exception as e:
            logger.warning(f"[DenpaPush] Failed to save token stats: {e}")

    async def initialize(self):
        try:
            import twikit  # noqa: F401

        except ImportError:
            logger.error("twikit 未安装，请确保 requirements.txt 中的依赖已被安装")
        self._apply_twitter_credentials()
        self._load_data()
        self._load_push_history()
        self._load_token_stats()
        # 清理上次运行残留的卡片/媒体临时文件, 避免磁盘与内存长期累积
        try:
            swept = await asyncio.to_thread(_sweep_stale_temp_files)
            if swept:
                logger.info(f"[DenpaPush] Swept {swept} stale temp files")
        except Exception as e:
            logger.warning(f"[DenpaPush] temp sweep failed: {e}")
        auto_monitor = True
        if self.subscriptions and auto_monitor:
            self._start_monitor()
        logger.info("Twitter Monitor plugin initialized")
        # 自动后台重建历史卡片（补齐缺失头像/媒体/配色）
        # 仅当存在缺少 avatar_url/thumbnail_urls 键的旧条目时才重建，
        # 避免每次重启都对已完整的卡片重新拉取 Twitter
        if self._push_history and any(
            not isinstance(e, dict)
            or "thumbnail_urls" not in e
            or "avatar_url" not in e
            for e in self._push_history
        ):
            self._rebuild_task = asyncio.create_task(self._rebuild_history_async())

    def _apply_twitter_credentials(self):
        auth_token = self.config.get("twitter_auth_token", "")
        ct0 = self.config.get("twitter_ct0", "")
        # 代理与 API 并发同样热更新: twikit 底层连接池在 set_proxy 时重建
        try:
            self.twitter.set_proxy(self.config.get("proxy", ""))
        except Exception as e:
            logger.warning(f"[DenpaPush] Failed to apply proxy: {e}")
        self.twitter.set_concurrency(self._twitter_concurrency())
        if auth_token:
            self.twitter.set_credentials(auth_token, ct0)

    def _twitter_concurrency(self) -> int:
        """同时进行的 Twitter API 请求数。"""
        return self._as_int(self.config.get("twitter_concurrency", 4), 4, 1, 16)

    async def terminate(self):
        self._running = False
        # 先落盘所有去抖写入, 再取消后台任务, 避免丢数据
        self._flush_pending_saves()
        if self.monitor_task:
            task = self.monitor_task
            self.monitor_task = None
            # 两阶段收尾, 目标是"既保留在途发送的自然收尾机会, 又不永久挂住":
            #   阶段1: 只等待、不取消(asyncio.wait 本身不会取消任务)。若此刻正卡在
            #          send_message 的 await 上(消息已发出、等响应), 它能在窗口内
            #          自然返回; 否则基线不推进会导致下轮重复推送。
            #   阶段2: 窗口用尽仍未结束, 才强制取消, 并短暂等一下让它真正退出,
            #          之后才关闭共享资源(避免任务在已关闭的 client/browser 上跑)。
            done, pending = await asyncio.wait({task}, timeout=3.0)
            if pending:
                logger.warning(
                    "[DenpaPush] Monitor task exceeded grace period, cancelling"
                )
                task.cancel()
                # 给它一点时间响应取消, 争取在关资源前真正停止
                await asyncio.wait({task}, timeout=2.0)
            if task.done():
                # 消费异常结果, 避免 "exception was never retrieved" 告警
                try:
                    task.exception()
                except (asyncio.CancelledError, Exception):
                    pass
            else:
                logger.warning(
                    "[DenpaPush] Monitor task still running after cancel; "
                    "closing shared resources anyway"
                )
        rebuild_task = getattr(self, "_rebuild_task", None)
        if rebuild_task and not rebuild_task.done():
            rebuild_task.cancel()
        self._rebuild_task = None
        if self._http_client is not None and not self._http_client.is_closed:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
        self._http_client = None
        self._http_client_key = None
        self._seed_cache.clear()
        # 关闭 twikit 与 GraphQL 的连接池, 避免热重载时连接泄漏
        try:
            await self.twitter.close()
        except Exception as e:
            logger.warning(f"[DenpaPush] Failed to close twitter client: {e}")
        # 关闭共享 Chromium(否则热重载会遗留孤儿浏览器进程)
        try:
            await _close_shared_browser()
        except Exception as e:
            logger.warning(f"[DenpaPush] Failed to close shared browser: {e}")
        # 本次运行产生的临时文件回收(宽限期内的保留给 NapCat 回拉, 见函数注释)
        try:
            await asyncio.to_thread(_sweep_own_temp_files)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════
    # Dashboard API (Signal Observatory)
    # ═══════════════════════════════════════════════════════════

    def _register_dashboard_apis(self, context: Context) -> None:
        """注册 Dashboard 后端 API。"""
        apis = [
            ("dashboard/status", self._api_dashboard_status, ["GET"], "面板状态"),
            ("dashboard/subscriptions", self._api_dashboard_subscriptions, ["GET"], "订阅列表"),
            ("dashboard/subscribe", self._api_dashboard_subscribe, ["POST"], "添加订阅"),
            ("dashboard/unsubscribe", self._api_dashboard_unsubscribe, ["POST"], "移除订阅"),
            ("dashboard/logs", self._api_dashboard_logs, ["GET"], "推送日志"),
            ("dashboard/ui_config", self._api_dashboard_ui_config, ["GET", "POST"], "界面设置持久化"),
            ("dashboard/config", self._api_dashboard_config, ["GET", "POST"], "插件配置读写"),
            ("dashboard/toggle_monitor", self._api_dashboard_toggle_monitor, ["POST"], "会话监控开关"),
            ("dashboard/history", self._api_dashboard_history, ["GET"], "推送历史详情"),
            ("bg/upload", self._api_bg_upload, ["POST"], "上传背景图"),
            ("bg/remove", self._api_bg_remove, ["POST"], "移除背景图"),
        ]
        for route, handler, methods, desc in apis:
            context.register_web_api(
                f"/astrbot_plugin_denpa_push/{route}", handler, methods, desc
            )

    def _log_push(self, msg: str, log_type: str = "push"):
        """记录一条 dashboard 信号日志。"""
        self._push_logs.appendleft({
            "time": datetime.now(timezone.utc).isoformat(),
            "type": log_type,
            "message": msg,
        })

    def _record_push_history(self, info: dict, session_umo: str, source: str = "auto"):
        """记录一条推送历史（自动/手动共用），含时间、会话与卡片详情。

        info: _build_card_data 返回的富结构 dict
        session_umo: 推送目标会话标识
        source: "auto" | "manual"
        """
        hist_media = (
            (info.get("images") or [])
            + (info.get("gifs") or [])
            + (info.get("videos") or [])
        )
        hist_thumbs = []
        for m in hist_media[:4]:
            mu = m.get("media_url", "")
            if mu:
                hist_thumbs.append(_twitter_media_url(mu, "medium"))
        self._push_history.appendleft({
            "time": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "session": session_umo,
            "tweet_id": info.get("tweet_id", ""),
            "screen_name": info.get("screen_name", ""),
            "user_name": info.get("user_name", ""),
            "avatar_url": info.get("avatar_url", ""),
            "text": (info.get("original_text") or "")[:300],
            "translated_text": (info.get("translated_text") or "")[:300],
            "tweet_url": info.get("tweet_url", ""),
            "seed_color": info.get("seed_color", ""),
            "palette": info.get("palette", []),
            "thumbnail_urls": hist_thumbs,
            "image_count": info.get("image_count", 0),
            "gif_count": info.get("gif_count", 0),
            "video_count": info.get("video_count", 0),
            "has_media": bool(hist_thumbs),
            "created_at_str": info.get("created_at_str", ""),
            "quoted_screen_name": info.get("quoted_screen_name", ""),
            "quoted_text": (info.get("quoted_text") or "")[:200],
        })
        # 硬上限: 保留天数设为 0(永久) 或关闭自动清理时也要封顶, 否则长跑必爆内存
        max_entries = self._as_int(
            self.config.get("history_max_entries", 2000), 2000, 50, 100000
        )
        while len(self._push_history) > max_entries:
            self._push_history.pop()
        # 去抖落盘: 原实现每条推送都全量 json.dump + 全量扫描清理, 高频推送时
        # 同步写盘会明显阻塞事件循环。历史上限可达 2000 条, 每条推送都重新序列化
        # 一遍整个历史是 O(n²) 量级, 故用更长的合并窗口(历史展示对实时性不敏感)。
        self._schedule_save(
            "push_history",
            self._write_history_payload,
            self._history_payload,
            delay=8.0,
        )
        self._prune_push_history(quiet=True)

    def _data_cache_size_bytes(self) -> int:
        """统计插件在数据目录下持久化文件的总大小（字节）。

        涵盖: 订阅数据、推送历史缓存、UI 配置（含背景图）、
        token 累计统计、backgrounds 临时目录与 debug_render 调试目录。
        data/config 下其他插件的文件不会被计入。
        """
        data_dir = os.path.dirname(self._data_path)
        total = 0
        try:
            if not os.path.isdir(data_dir):
                return 0
            for name in os.listdir(data_dir):
                if name.startswith(("denpa_", "astrbot_plugin_denpa")) or name in (
                    "backgrounds",
                    "debug_render",
                ):
                    path = os.path.join(data_dir, name)
                    if os.path.isfile(path):
                        total += os.path.getsize(path)
                    elif os.path.isdir(path):
                        for root, _dirs, files in os.walk(path):
                            for f in files:
                                try:
                                    total += os.path.getsize(os.path.join(root, f))
                                except OSError:
                                    pass
        except OSError as e:
            logger.warning(f"[DenpaPush] Failed to measure data cache size: {e}")
        return total

    async def _api_dashboard_status(self):
        """Dashboard 总览状态。"""
        from astrbot.api.web import request, json_response

        total_tracked = sum(len(users) for users in self.subscriptions.values())
        # 计算今日推送数（本地时区 00:00 起）
        today_pushes = 0
        try:
            from datetime import datetime as _dt
            now_local = _dt.now()
            today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            for item in self._push_history:
                t = item.get("time", "")
                if t:
                    try:
                        dt = _dt.fromisoformat(t.replace("Z", "+00:00"))
                        if dt.astimezone().replace(tzinfo=None) >= today_start:
                            today_pushes += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return json_response({
            "monitor_running": self._running,
            "total_tracked": total_tracked,
            "total_pushes": self._total_pushes,
            "today_pushes": today_pushes,
            "poll_interval": int(self.config.get("poll_interval", 5)),
            "session_count": len(self.monitored_sessions),
            "monitored_sessions": list(self.monitored_sessions),
            "auth_configured": bool(self.config.get("twitter_auth_token", "")),
            "playwright_ready": _pw_browser is not None and _pw_browser.is_connected(),
            "translation_language": self.config.get("translation_language", "中文"),
            "color_source": self.config.get("color_source", "avatar"),
            "proxy": self.config.get("proxy", ""),
            "token_stats": dict(self._token_stats),
            "cache_size_bytes": self._data_cache_size_bytes(),
        })

    async def _api_dashboard_subscriptions(self):
        """返回全部订阅 {session: {username: info}}。"""
        from astrbot.api.web import json_response

        return json_response(self.subscriptions)

    def _normalize_session(self, session: str, message_type: str = "") -> str:
        """把前端传入的会话统一为 unified_msg_origin 格式。

        支持两种输入:
        - 完整会话 "平台:消息类型:ID"(如 小赤羽:FriendMessage:2728007259)
        - 纯 QQ 号 "2728007259": 若该 QQ 号已存在于现有会话则复用原会话(类型以其为准);
          否则按 message_type(FriendMessage 私聊 / GroupMessage 群聊, 默认私聊)
          用现有会话的平台前缀拼成 "平台:类型:QQ号"。
        无可参照会话时返回空串(由调用方回退到下拉会话或报错)。
        """
        session = (session or "").strip()
        if not session:
            return ""
        if ":" in session:
            return session
        existing = list(self.subscriptions) or list(self.monitored_sessions)
        # 该 QQ 号已是某个会话: 复用(保留其原始类型, 群/私聊都对得上)
        for s in existing:
            if s and ":" in s and s.rsplit(":", 1)[-1] == session:
                return s
        # 新 QQ 号: 用前端指定类型, 未指定或非法则默认私聊
        mtype = (message_type or "").strip()
        if mtype not in ("FriendMessage", "GroupMessage"):
            mtype = "FriendMessage"
        if existing:
            platform_id = existing[0].split(":", 1)[0]
            return f"{platform_id}:{mtype}:{session}"
        return ""

    async def _api_dashboard_subscribe(self):
        """Dashboard 添加订阅（可指定 session，否则自动选择）。"""
        from astrbot.api.web import request, json_response, error_response

        payload = await request.json()
        username = (payload.get("username") or "").strip().lstrip("@")
        if not username:
            return error_response("缺少 username", status_code=400)

        # 选择目标 session: 优先用前端指定的（纯 QQ 号按所选类型补全前缀），否则自动选
        target_session = self._normalize_session(
            payload.get("session", ""), payload.get("session_type", "")
        )
        if not target_session:
            if self.monitored_sessions:
                target_session = next(iter(self.monitored_sessions))
            elif self.subscriptions:
                target_session = next(iter(self.subscriptions))
        if not target_session:
            return error_response("无可用会话，请先在群聊中使用 /twitter add 初始化", status_code=400)

        # 已追踪该账号则直接返回(用 get 避免提前创建会话键)
        if username in self.subscriptions.get(target_session, {}):
            return json_response({"ok": True, "message": f"@{username} 已在追踪中"})

        try:
            await self.twitter.ensure_ready()
            user = await self.twitter.get_user_by_screen_name(username)
            tweets = await self.twitter.get_user_tweets(user.id, count=1)
            last_id = tweets[0].id if tweets else "0"
            avatar_url = getattr(user, "profile_image_url", "") or ""
            if avatar_url:
                avatar_url = avatar_url.replace("_normal.", "_400x400.")
            # 拉取成功后才落会话键，失败时不残留内存幻影会话
            session_users = self.subscriptions.setdefault(target_session, {})
            if username in session_users:
                return json_response({"ok": True, "message": f"@{username} 已在追踪中"})
            session_users[username] = {
                "user_id": user.id,
                "name": getattr(user, "name", "") or username,
                "avatar_url": avatar_url,
                "last_tweet_id": last_id,
                "last_checked_at": datetime.now(timezone.utc).isoformat(),
            }
            self.monitored_sessions.add(target_session)
            self._save_data()
            self._start_monitor()
            self._log_push(f"开始追踪 @{username} ({user.name})", "info")
            return json_response({"ok": True, "message": f"已追踪 @{username}"})
        except Exception as e:
            logger.error(f"[Dashboard] subscribe failed: {e}")
            return error_response(f"添加失败: {str(e)[:120]}", status_code=500)

    async def _api_dashboard_unsubscribe(self):
        """Dashboard 移除订阅（可指定 session，否则从所有 session 移除）。"""
        from astrbot.api.web import request, json_response, error_response

        payload = await request.json()
        username = (payload.get("username") or "").strip().lstrip("@")
        if not username:
            return error_response("缺少 username", status_code=400)

        target_session = payload.get("session", "")
        removed = False

        if target_session:
            # 按会话级别移除
            session_users = self.subscriptions.get(target_session, {})
            if username in session_users:
                del session_users[username]
                removed = True
                if not session_users:
                    del self.subscriptions[target_session]
        else:
            # 从所有 session 移除
            for sess_umo in list(self.subscriptions.keys()):
                session_users = self.subscriptions.get(sess_umo, {})
                if username in session_users:
                    del session_users[username]
                    removed = True
                    if not session_users:
                        del self.subscriptions[sess_umo]

        if not removed:
            return error_response(f"未找到 @{username}", status_code=404)

        self._save_data()
        if not self.subscriptions:
            self._stop_monitor()
        self._log_push(f"取消追踪 @{username}", "info")
        return json_response({"ok": True, "message": f"已取消追踪 @{username}"})

    async def _api_dashboard_logs(self):
        """返回推送日志。"""
        from astrbot.api.web import json_response

        return json_response({"logs": list(self._push_logs)})

    async def _api_dashboard_ui_config(self):
        """界面设置持久化（独立 JSON 文件）。"""
        from astrbot.api.web import request, json_response, error_response

        path = os.path.join(
            os.path.dirname(self._data_path), "denpa_push_ui_config.json"
        )
        if request.method == "GET":
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json_response(json.load(f))
            except (FileNotFoundError, json.JSONDecodeError):
                return json_response({})
        else:
            payload = await request.json()
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                return json_response({"saved": True})
            except Exception as e:
                return error_response(f"保存失败: {e}", status_code=500)

    async def _api_dashboard_config(self):
        """插件配置读写（对应 _conf_schema.json 声明的 key）。"""
        from astrbot.api.web import request, json_response, error_response

        SCHEMA_KEYS = [
            "twitter_auth_token", "twitter_ct0", "poll_interval",
            "text_translate_provider", "image_translate_provider",
            "text_translate_fallback_providers", "image_translate_fallback_providers",
            "image_translate_mode", "translation_language",
            "text_translate_prompt", "image_translate_prompt",
            "color_source",
            "history_retention_days", "history_auto_clean", "proxy",
            "llm_concurrency", "http_concurrency", "backlog_budget",
            "render_concurrency", "send_concurrency", "twitter_concurrency",
            "max_download_mb", "max_card_height", "history_max_entries",
            "debug_render_dump", "send_image_max_side",
            "send_image_max_total_mb", "gif_convert_timeout",
        ]
        FALLBACK_KEYS = (
            "text_translate_fallback_providers",
            "image_translate_fallback_providers",
        )
        if request.method == "GET":
            data = {k: self.config.get(k, "") for k in SCHEMA_KEYS}
            # 回退模型列表统一输出数组，前端 tag 编辑器可直接使用
            for k in FALLBACK_KEYS:
                data[k] = self._normalize_fallback_list(self.config.get(k, []))
            # 未保存过的新选项回退到 schema 默认值，避免界面显示空
            if data.get("history_retention_days") in ("", None):
                data["history_retention_days"] = 30
            if data.get("history_auto_clean") in ("", None):
                data["history_auto_clean"] = True
            # 脱敏: token 只显示前6位
            for key in ("twitter_auth_token", "twitter_ct0"):
                v = data.get(key, "")
                if v and len(v) > 6:
                    data[key + "_masked"] = v[:6] + "…" + v[-4:]
            return json_response(data)
        else:
            payload = await request.json()
            try:
                for k in SCHEMA_KEYS:
                    if k in payload:
                        v = payload[k]
                        # 布尔开关统一归一化为真布尔，避免 "false" 字符串被当 truthy
                        if k == "history_auto_clean":
                            if isinstance(v, str):
                                v = v.strip().lower() not in ("0", "false", "no", "")
                            else:
                                v = bool(v)
                        elif k in FALLBACK_KEYS:
                            # 回退列表归一化为数组存储(兼容前端传字符串的情况)
                            v = self._normalize_fallback_list(v)
                        self.config[k] = v
                self.config.save_config()
                # 热更新凭据
                self._apply_twitter_credentials()
                return json_response({"saved": True})
            except Exception as e:
                return error_response(f"保存失败: {e}", status_code=500)

    async def _api_dashboard_history(self):
        """推送历史详情（含推文内容、翻译、色板）。"""
        from astrbot.api.web import json_response

        auto_clean = self.config.get("history_auto_clean", True)
        if isinstance(auto_clean, str):
            auto_clean = auto_clean.strip().lower() not in ("0", "false", "no", "")
        return json_response({
            "history": list(self._push_history),
            "rebuilding": self._rebuild_running,
            "retention_days": self._history_retention_days(),
            "auto_clean": bool(auto_clean),
            "count": len(self._push_history),
        })

    async def _rebuild_history_async(self):
        """后台重新构建历史 timeline 卡片。

        扫描 _push_history 中缺失 avatar_url/thumbnail_urls 的条目，
        按 tweet_url 解析 tweet_id 并重新拉取推文，补齐头像、媒体缩略图、
        媒体计数、引用推文、发布时间与配色。
        """
        if self._rebuild_running:
            return
        self._rebuild_running = True
        self._log_push("开始后台重建历史卡片", "info")
        try:
            await self.twitter.ensure_ready()
            rebuilt = 0
            skipped = 0
            for entry in list(self._push_history):
                # 已具备头像与媒体缩略图键的条目跳过。
                # 按"键是否存在"判断而非值真值：持久化的空列表表示推文本就无媒体，
                # 并非缺失——否则纯文字推文会在每次重启时被重复拉取
                if (
                    isinstance(entry, dict)
                    and "avatar_url" in entry
                    and "thumbnail_urls" in entry
                ):
                    skipped += 1
                    continue
                # 解析 tweet_id：优先字段，其次从 tweet_url 提取
                tweet_id = str(entry.get("tweet_id") or "")
                if not tweet_id and entry.get("tweet_url"):
                    parts = entry["tweet_url"].split("/status/")
                    if len(parts) == 2 and parts[1]:
                        tweet_id = parts[1].split("?")[0].strip()
                if not tweet_id:
                    continue
                try:
                    tweet = await self.twitter.get_tweet_by_id(tweet_id)
                    data = TwitterClient.extract_tweet_data(tweet)
                    raw_av = (data.get("user") or {}).get("avatar_url", "") or ""
                    # 无论是否取到头像都写键，保证重建成功后该条目被标记为完整，
                    # 下次重启不会因空值被误判为缺失而重复拉取
                    entry["avatar_url"] = (
                        raw_av.replace("_normal.", "_400x400.") if raw_av else ""
                    )
                    images, gifs, videos = TwitterClient.extract_tweet_media(data)
                    thumbs = []
                    for m in (images + gifs + videos)[:4]:
                        mu = m.get("media_url", "")
                        if mu:
                            thumbs.append(_twitter_media_url(mu, "medium"))
                    entry["thumbnail_urls"] = thumbs
                    entry["image_count"] = len(images)
                    entry["gif_count"] = len(gifs)
                    entry["video_count"] = len(videos)
                    entry["has_media"] = bool(thumbs)
                    entry["tweet_id"] = str(data.get("id", tweet_id))
                    q = data.get("quoted_tweet") or {}
                    if q:
                        entry["quoted_screen_name"] = (
                            q.get("user", {}).get("screen_name", "") if q.get("user") else ""
                        )
                        entry["quoted_text"] = (q.get("text", "") or "")[:200]
                    dt = data.get("created_at_datetime")
                    if dt and hasattr(dt, "astimezone"):
                        try:
                            entry["created_at_str"] = dt.astimezone(
                                timezone(timedelta(hours=8))
                            ).strftime("%m月%d日 %H:%M")
                        except Exception:
                            pass
                    # 补齐原文/译文
                    if not entry.get("text"):
                        entry["text"] = (data.get("text", "") or "")[:300]
                    # 缺失配色则重新取色
                    if not entry.get("seed_color"):
                        color_source = self.config.get("color_source", "avatar")
                        seed_url = entry.get("avatar_url", "")
                        if color_source == "first_image" and images:
                            first_url = images[0].get("media_url", "")
                            if first_url:
                                seed_url = _twitter_media_url(first_url, "orig")
                        if seed_url:
                            seed_rgb = await self._extract_seed_color(seed_url)
                            pal, _is_dark = await self._build_palette_async(seed_rgb)
                            entry["seed_color"] = (
                                "#%02x%02x%02x" % seed_rgb if seed_rgb else ""
                            )
                            entry["palette"] = pal
                    rebuilt += 1
                    self._schedule_save(
                        "push_history",
                        self._write_history_payload,
                        self._history_payload,
                    )
                    await asyncio.sleep(2)  # 规避 Twitter API 速率限制
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(
                        f"[Rebuild] Failed for tweet {tweet_id}: {e}"
                    )
                    continue
            self._log_push(
                f"历史卡片重建完成，补全 {rebuilt} 条，跳过 {skipped} 条", "info"
            )
        except Exception as e:
            logger.error(f"[Rebuild] History rebuild failed: {e}")
            self._log_push(f"历史卡片重建失败: {str(e)[:60]}", "error")
        finally:
            self._rebuild_running = False

    async def _api_dashboard_toggle_monitor(self):
        """按会话开启/关闭监控推送。"""
        from astrbot.api.web import request, json_response, error_response

        payload = await request.json()
        session = payload.get("session", "")
        enabled = payload.get("enabled", True)
        if not session:
            return error_response("缺少 session", status_code=400)

        if enabled:
            self.monitored_sessions.add(session)
            if any(self.subscriptions.get(s) for s in self.monitored_sessions):
                self._start_monitor()
        else:
            self.monitored_sessions.discard(session)
            if not self.monitored_sessions:
                self._stop_monitor()

        self._save_data()
        self._log_push(
            f"会话 {'开启' if enabled else '关闭'}监控: {session[:20]}…",
            "info",
        )
        return json_response({"ok": True, "enabled": enabled})

    async def _api_bg_upload(self):
        """上传 UI 背景图，以 base64 data URI 内联存储。"""
        import base64
        import time as _time
        from astrbot.api.web import request, json_response, error_response, PluginUploadFile

        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("缺少上传文件（字段名应为 file）", status_code=400)

        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            return error_response("仅支持 jpg/png/webp/gif 图片", status_code=400)

        # 取文件字节：优先用 body 属性，否则临时落盘读取后删除
        body = getattr(upload, "body", None)
        if body is None:
            bg_dir = os.path.join(os.path.dirname(self._data_path), "backgrounds")
            os.makedirs(bg_dir, exist_ok=True)
            tmp_path = os.path.join(bg_dir, f"tmp_{int(_time.time())}{ext}")
            await upload.save(tmp_path)
            try:
                with open(tmp_path, "rb") as f:
                    body = f.read()
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        if len(body) > 20 * 1024 * 1024:
            return error_response("图片不能超过 20MB", status_code=400)

        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(ext, "application/octet-stream")
        data_uri = f"data:{mime};base64," + base64.b64encode(body).decode("ascii")

        # 持久化到 ui_config JSON
        path = os.path.join(
            os.path.dirname(self._data_path), "denpa_push_ui_config.json"
        )
        try:
            ui = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    ui = json.load(f)
            ui["background_image"] = data_uri
            ui["background_mode"] = "image"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(ui, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[DenpaPush] 保存背景图配置失败: {e}")

        logger.info(f"[DenpaPush] 背景图已上传（base64 内联，{len(body)} 字节）")
        return json_response({
            "saved": True,
            "data": data_uri,
            "filename": upload.filename,
        })

    async def _api_bg_remove(self):
        """移除背景图。"""
        from astrbot.api.web import json_response

        path = os.path.join(
            os.path.dirname(self._data_path), "denpa_push_ui_config.json"
        )
        try:
            ui = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    ui = json.load(f)
            ui["background_image"] = ""
            ui["background_accent"] = ""
            ui["background_mode"] = "theme"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(ui, f, ensure_ascii=False)
        except Exception:
            pass
        return json_response({"removed": True})

    def _load_data(self):
        """从磁盘加载订阅数据。

        安全约束: 只有"确认读到了内容"或"确认文件不存在"才算加载成功。
        读取/解析失败时**绝不直接覆盖**磁盘 —— 否则一次瞬时 IO 抖动就会让内存落到
        空态, 随后 terminate 的无条件落盘会把完好的订阅按空模板覆盖掉(实测可复现)。

        但要区分两类失败, 否则内容已损坏的用户会永久无法保存(可用性死角):
          - 瞬时 IO 错误(占用/权限/IO): 保守处理, 本次运行拒绝写盘, 下次重试即可;
          - 内容损坏(JSON 解析失败/结构非法): 先把坏文件备份, 再放行写盘,
            让用户能继续使用插件, 同时保留现场供排查。
        """
        self._data_loaded_ok = False
        if not os.path.exists(self._data_path):
            # 首次运行: 磁盘上本就没有数据, 空态是正确状态
            self._data_loaded_ok = True
            return
        try:
            with open(self._data_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, UnicodeDecodeError) as e:
            # json.JSONDecodeError 继承自 ValueError; 内容坏了, 备份后放行
            self._quarantine_broken_file(self._data_path, "subscriptions", e)
            self._data_loaded_ok = True
            return
        except Exception as e:
            # 瞬时 IO 问题: 保留磁盘现状, 本次运行不写盘
            logger.error(
                f"[DenpaPush] Failed to read subscription data (transient), "
                f"refusing to overwrite to avoid data loss: {e}"
            )
            return

        try:
            if not isinstance(data, dict):
                raise ValueError(f"unexpected data root: {type(data).__name__}")
            tracked = data.get("tracked_users")
            if tracked is not None:
                sessions = data.get("monitored_sessions", [])
                self.subscriptions = {}
                for s in sessions:
                    self.subscriptions[s] = dict(tracked)
                self.monitored_sessions = set(sessions)
            else:
                self.subscriptions = data.get("subscriptions", {})
                self.monitored_sessions = set(data.get("monitored_sessions", []))
            total = sum(len(users) for users in self.subscriptions.values())
            logger.info(
                f"Loaded {len(self.subscriptions)} sessions with {total} tracked users"
            )
            self._data_loaded_ok = True
        except Exception as e:
            self._quarantine_broken_file(self._data_path, "subscriptions", e)
            self._data_loaded_ok = True

    @staticmethod
    def _quarantine_broken_file(path: str, label: str, err: Exception) -> None:
        """把无法解析的数据文件改名备份, 让插件能继续启动而不是永久拒绝写盘。"""
        backup = f"{path}.broken"
        try:
            if os.path.exists(backup):
                _remove_temp(backup)
            os.replace(path, backup)
            logger.error(
                f"[DenpaPush] {label} file is corrupted ({err}); "
                f"moved it to {os.path.basename(backup)} and starting with empty state"
            )
        except Exception as e:
            logger.error(
                f"[DenpaPush] {label} file is corrupted ({err}) and could not be "
                f"backed up ({e}); starting with empty state"
            )

    def _data_payload(self) -> dict:
        """在事件循环线程里快照订阅数据(深一层拷贝), 供线程池写盘使用。"""
        return {
            "subscriptions": {s: dict(u) for s, u in self.subscriptions.items()},
            "monitored_sessions": list(self.monitored_sessions),
        }

    def _save_data(self):
        """原子写订阅数据。

        推送循环在每条推文后都会调用, 原实现直接截断重写, 与 Dashboard 并发读
        时可能读到半截 JSON; 改为原子替换。
        未成功加载过数据时拒绝写入, 防止用空态覆盖磁盘上完好的订阅。
        """
        if not getattr(self, "_data_loaded_ok", False):
            logger.warning(
                "[DenpaPush] Skip saving subscriptions: initial load never "
                "succeeded, refusing to overwrite existing data file"
            )
            return
        try:
            os.makedirs(os.path.dirname(self._data_path), exist_ok=True)
            _atomic_write_json(self._data_path, self._data_payload())
        except Exception as e:
            logger.error(f"Failed to save data: {e}")

    def _history_payload(self) -> dict:
        return {
            "history": list(self._push_history),
            "total_pushes": self._total_pushes,
        }

    def _schedule_save(self, key: str, writer, payload_fn, delay: float = 1.5) -> None:
        """去抖落盘: 合并高频写入, 并在线程池里执行真正的写盘。

        payload_fn 在事件循环线程内先把数据快照出来(避免线程遍历时容器被并发
        修改), 随后 json.dump 与文件替换都放到线程池, 不阻塞 AstrBot 的事件循环。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无运行中的 loop(如 terminate 收尾) 直接同步写
            try:
                writer(payload_fn())
            except Exception as e:
                logger.warning(f"[DenpaPush] sync save failed: {e}")
            return

        old = self._pending_saves.pop(key, None)
        if old is not None:
            old.cancel()

        async def _runner():
            try:
                await asyncio.sleep(delay)
                # 快照在事件循环内完成
                payload = payload_fn()
                await asyncio.to_thread(writer, payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[DenpaPush] deferred save failed: {e}")
            finally:
                # 只清理自己: 期间可能已有新的写入挂到同一 key 上,
                # 无条件 pop 会把新任务从待写表中抹掉, terminate 时就会丢数据
                if self._pending_saves.get(key) is task:
                    self._pending_saves.pop(key, None)

        task = loop.create_task(_runner())
        self._pending_saves[key] = task

    def _flush_pending_saves(self) -> None:
        """立即落盘所有挂起的去抖写入(terminate 时调用)。

        注意: 这里用带守卫的 _save_* 而非 _write_*_payload, 因为"从未成功加载"
        时写入空态会覆盖磁盘上完好的数据(实测可复现的丢数据路径)。
        """
        for key, task in list(self._pending_saves.items()):
            try:
                task.cancel()
            except Exception:
                pass
            self._pending_saves.pop(key, None)
        for fn in (self._save_data, self._save_push_history, self._save_token_stats):
            try:
                fn()
            except Exception as e:
                logger.warning(f"[DenpaPush] flush save failed: {e}")

    def _write_data_payload(self, payload) -> None:
        if not getattr(self, "_data_loaded_ok", False):
            logger.warning(
                "[DenpaPush] Skip writing subscriptions: initial load never succeeded"
            )
            return
        os.makedirs(os.path.dirname(self._data_path), exist_ok=True)
        _atomic_write_json(self._data_path, payload)

    def _write_history_payload(self, payload) -> None:
        if not getattr(self, "_history_loaded_ok", False):
            logger.warning(
                "[DenpaPush] Skip writing push history: initial load never succeeded"
            )
            return
        os.makedirs(os.path.dirname(self._push_history_path), exist_ok=True)
        _atomic_write_json(self._push_history_path, payload)

    def _write_token_payload(self, payload) -> None:
        os.makedirs(os.path.dirname(self._token_stats_path), exist_ok=True)
        _atomic_write_json(self._token_stats_path, payload)

    def _start_monitor(self):
        if self.monitor_task and not self.monitor_task.done():
            return
        self._running = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Monitor loop started")

    def _stop_monitor(self):
        self._running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = None
        logger.info("Monitor loop stopped")

    @filter.command("twitter")
    async def twitter_cmd(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield _plain(
                "用法:\n"
                "/twitter add <username>  - 关注用户\n"
                "/twitter remove <username>  - 取消关注\n"
                "/twitter list  - 关注列表\n"
                "/twitter push <url>  - 手动推送单条推文\n"
                "/twitter monitor  - 切换本会话的推送开关"
            )
            return

        sub = parts[1].lower()

        auth_token = self.config.get("twitter_auth_token", "")
        if not auth_token:
            yield _plain(
                "请先在插件配置中设置 twitter_auth_token 和 twitter_ct0"
            )
            return
        self.twitter.set_credentials(auth_token, self.config.get("twitter_ct0", ""))

        if sub == "add" and len(parts) >= 3:
            yield await self._cmd_add(event, parts[2])
        elif sub == "remove" and len(parts) >= 3:
            yield await self._cmd_remove(event, parts[2])
        elif sub == "list":
            yield await self._cmd_list(event)
        elif sub == "push" and len(parts) >= 3:
            tmps = []
            try:
                for result in await self._cmd_push(event, parts[2], temp_sink=tmps):
                    yield result
            finally:
                # 发送完成后标记待回收(宽限期覆盖 NapCat 的异步回拉窗口)
                _release_temp(tmps)
        elif sub == "monitor":
            yield await self._cmd_monitor(event)
        else:
            yield _plain(f"未知子指令: {sub}")

    @filter.llm_tool(name="twitter_add")
    async def twitter_add(self, event: AstrMessageEvent, usernames: list):
        """当用户说「关注」「订阅」「跟踪」某个推特账号时使用此工具。会开始监控该用户的推文并自动推送新内容。

        Args:
            usernames(array[string]): 要关注的用户名，如 ["ApexLiveComms"]
        """
        event = _unwrap_event(event)
        if isinstance(usernames, str):
            usernames = [usernames]
        for name in usernames:
            result = await self._cmd_add(event, name)
            yield result

    @filter.llm_tool(name="twitter_remove")
    async def twitter_remove(self, event: AstrMessageEvent, usernames: list):
        """当用户说「取消关注」「取关」「删除订阅」某个推特账号时使用此工具。支持模糊匹配。
        Args:
            usernames(array[string]): 要取消关注的用户名，如 ["apexlive"]
        """
        event = _unwrap_event(event)
        if isinstance(usernames, str):
            usernames = [usernames]
        umo = event.unified_msg_origin
        session_users = self.subscriptions.get(umo, {})
        for raw in usernames:
            matched = [n for n in session_users if raw.lower() in n.lower()]
            if not matched:
                result = await self._cmd_remove(event, raw)
                yield result
            else:
                for name in matched:
                    result = await self._cmd_remove(event, name)
                    yield result

    @filter.llm_tool(name="twitter_push")
    async def twitter_push(self, event: AstrMessageEvent, url: str):
        """当用户发来一个推特链接并要求「推送」「翻译」「看看」「读取」「解析」时使用此工具。会获取推文内容、翻译、生成卡片并发送图片/视频。不要自己解释推文内容，交给此工具处理。

        Args:
            url(string): 推特链接，如 https://x.com/username/status/123456
        """
        event = _unwrap_event(event)
        umo = event.unified_msg_origin
        m = __import__("re").search(
            r"(?:twitter\.com|x\.com)/(\w+)/status/(\d+)", url
        )
        username = m.group(1) if m else "unknown"
        tweet_id = m.group(2) if m else ""
        silent = bool(self.config.get("silent_mode", False))
        tmps = []
        try:
            for chain in await self._cmd_push(
                event, url, silent=silent, temp_sink=tmps
            ):
                await self._send(umo, chain)
                await asyncio.sleep(0.3)
        finally:
            # 发送完成后再回收媒体/卡片临时文件(宽限期覆盖 NapCat 异步回拉)
            _release_temp(tmps)
        if not silent:
            yield f"已推送 @{username} 的推文 {tweet_id}"

    @filter.llm_tool(name="twitter_list")
    async def twitter_list(self, event: AstrMessageEvent):
        """列出当前群聊已关注的 Twitter 用户。"""
        event = _unwrap_event(event)
        umo = event.unified_msg_origin
        session_users = self.subscriptions.get(umo, {})
        lines = ["已关注用户:"]
        for name in session_users:
            lines.append(f"  @{name}")
        yield _plain("\n".join(lines) if len(lines) > 1 else "暂无关注用户")

    @filter.llm_tool(name="denpa_push")
    async def denpa_push(self, event: AstrMessageEvent):
        """开启或关闭当前会话的自动推送。"""
        event = _unwrap_event(event)
        umo = event.unified_msg_origin
        if umo in self.monitored_sessions:
            self.monitored_sessions.discard(umo)
            self._save_data()
            yield _plain("已关闭本会话的自动推送")
        else:
            self.monitored_sessions.add(umo)
            self._save_data()
            if any(self.subscriptions.get(s) for s in self.monitored_sessions):
                self._start_monitor()
            yield _plain("已开启本会话的自动推送")

    async def _cmd_add(self, event: AstrMessageEvent, username: str):
        username = username.lstrip("@")
        umo = event.unified_msg_origin
        session_users = self.subscriptions.setdefault(umo, {})
        if username in session_users:
            yield _plain(f"本群已关注 @{username}")
        try:
            user = await self.twitter.get_user_by_screen_name(username)
            tweets = await self.twitter.get_user_tweets(user.id, count=1)
            last_id = tweets[0].id if tweets else "0"
            avatar_url = getattr(user, "profile_image_url", "") or ""
            if avatar_url:
                avatar_url = avatar_url.replace("_normal.", "_400x400.")
            session_users[username] = {
                "user_id": user.id,
                "name": getattr(user, "name", "") or username,
                "avatar_url": avatar_url,
                "last_tweet_id": last_id,
                "last_checked_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_data()
            self._start_monitor()
            yield _plain(
                f"本群已关注 @{username}（{user.name}），开始跟踪"
            )
        except Exception as e:
            logger.error(f"Failed to add user {username}: {e}")
            yield _plain(f"添加失败: {str(e)[:100]}")

    async def _cmd_remove(self, event: AstrMessageEvent, username: str):
        username = username.lstrip("@")
        umo = event.unified_msg_origin
        session_users = self.subscriptions.get(umo, {})
        if username not in session_users:
            return _plain(f"本群未关注 @{username}")
        del session_users[username]
        if not session_users:
            del self.subscriptions[umo]
        self._save_data()
        if not self.subscriptions:
            self._stop_monitor()
        return _plain(f"本群已取消关注 @{username}")

    async def _cmd_list(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        session_users = self.subscriptions.get(umo, {})
        if not session_users:
            return _plain("本群暂无关注用户")
        lines = ["本群关注用户:"]
        for name, info in session_users.items():
            lines.append(
                f"  @{name}  (最后ID: {info.get('last_tweet_id', 'N/A')[:12]}...)"
            )
        return _plain("\n".join(lines))

    async def _cmd_push(
        self,
        event: AstrMessageEvent,
        url: str,
        silent: bool = False,
        temp_sink: list = None,
    ):
        results = []
        m = re.search(r"(?:twitter\.com|x\.com)/(\w+)/status/(\d+)", url)
        if not m:
            results.append(
                _plain(
                    "无效的推文链接，格式: https://x.com/username/status/123456"
                )
            )
            return results
        username, tweet_id = m.group(1), m.group(2)
        try:
            if not silent:
                results.append(_plain("正在获取推文..."))
            tweet = await self.twitter.get_tweet_by_id(tweet_id)
            data = TwitterClient.extract_tweet_data(tweet)

            # 2. 图片提前下载到临时文件发送（避免发送时 pbs.twimg.com 直连超时）
            from astrbot.api.message_components import Node, Plain
            import subprocess, os

            async def _convert_to_gif(mp4_path):
                try:
                    import json, shutil

                    _ffmpeg = shutil.which("ffmpeg")
                    _ffprobe = shutil.which("ffprobe")
                    if not _ffmpeg:
                        logger.warning(
                            "ffmpeg not found, GIF conversion unavailable. "
                            "Install: apt install ffmpeg"
                        )
                        return None

                    gif_path = mp4_path.rsplit(".", 1)[0] + ".gif"
                    # detect original fps via ffprobe
                    fps = 15
                    if _ffprobe:
                        # 探测同样要有超时: 损坏/超长 mp4 会让 ffprobe 长时间不退出,
                        # 而本协程持有 _push_lock, 无超时会把后续所有推送一起卡住
                        # (与下面 ffmpeg 转换超时同构)
                        probe = None
                        try:
                            probe = await asyncio.create_subprocess_exec(
                                _ffprobe,
                                "-v", "error",
                                "-select_streams", "v:0",
                                "-show_entries", "stream=r_frame_rate",
                                "-of", "json",
                                mp4_path,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                            )
                            out, _ = await asyncio.wait_for(
                                probe.communicate(), timeout=15
                            )
                            info = json.loads(out.decode())
                            num, den = map(
                                int, info["streams"][0]["r_frame_rate"].split("/")
                            )
                            fps = num / den if den else 15
                        except asyncio.TimeoutError:
                            logger.warning("ffprobe timed out, fallback to 15fps")
                            if probe is not None:
                                try:
                                    probe.kill()
                                    await probe.wait()
                                except Exception:
                                    pass
                        except Exception:
                            # 探测失败不影响主流程, 用默认帧率继续
                            pass

                    # ffmpeg palettegen+paletteuse
                    palette_filter = (
                        f"fps={fps:.2f},"
                        f"split[s0][s1];"
                        f"[s0]palettegen=stats_mode=diff[p];"
                        f"[s1][p]paletteuse=dither=none"
                    )
                    proc = await asyncio.create_subprocess_exec(
                        _ffmpeg,
                        "-i", mp4_path,
                        "-vf", palette_filter,
                        "-y", gif_path,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    # 加超时: 损坏/超长视频会让 ffmpeg 长时间不退出, 而这条协程
                    # 正持有 _push_lock, 无超时就会把后续所有推送一起卡住
                    gif_timeout = self._as_int(
                        self.config.get("gif_convert_timeout", 120), 120, 10, 1800
                    )
                    try:
                        rc = await asyncio.wait_for(proc.wait(), timeout=gif_timeout)
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"GIF conversion timed out after {gif_timeout}s, skipping"
                        )
                        try:
                            proc.kill()
                            await proc.wait()
                        except Exception:
                            pass
                        return None
                    if rc == 0 and os.path.exists(gif_path):
                        return gif_path
                except Exception as e:
                    logger.warning(f"GIF conversion failed: {e}")
                return None

            # 提前下载图片，同时传给卡片渲染复用
            images, gifs, videos = TwitterClient.extract_tweet_media(data)
            media_url_to_path = {}
            img_files = await asyncio.gather(
                *[
                    self._download_file(
                        _twitter_media_url(img.get("media_url", ""), "orig")
                    )
                    for img in images
                    if img.get("media_url", "")
                ]
            )
            for img, path in zip(
                [img for img in images if img.get("media_url", "")], img_files
            ):
                if path:
                    media_url_to_path[img["media_url"]] = path

            info = await self._build_card_data(data, media_url_to_path)

            # 把本次产生的临时文件交给调用方回收(命令返回后立即删除)
            if temp_sink is not None:
                temp_sink.extend(p for p in img_files if p)
                temp_sink.extend(
                    p
                    for p in info.get("card_img_urls", [])
                    if p and not str(p).startswith("http")
                )

            # 1. 卡片 PNG 直接发送（多张长文章分块）
            for url in info.get("card_img_urls", [info.get("card_img_url", "")]):
                if url:
                    results.append(_img(url))
            results.append(
                _plain(
                    f"📢 @{info['screen_name']}\n{info.get('tweet_url', '')}"
                )
            )

            uname = info.get("user_name", info["screen_name"])
            # 发送用图片先压缩: 合并转发会把图片 base64 内联进 OneBot 报文,
            # 直接用 orig 原图会撑爆单条消息
            send_images = await self._prepare_send_images(
                [p for p in img_files if p]
            )
            if temp_sink is not None:
                temp_sink.extend(send_images)
            img_contents = [Plain(f"📸 @{info['screen_name']} 的图片")]
            for f in send_images:
                img_contents.append(CompImage.fromFileSystem(f))
            if len(img_contents) > 1:
                node = Node(uin="0", name=uname, content=img_contents)
                results.append(_chain([node]))
            for gif in info.get("gifs", []):
                vurl = gif.get("video_url", gif.get("media_url", ""))
                if vurl:
                    f = await self._download_file(vurl, suffix=".mp4")
                    if f:
                        if temp_sink is not None:
                            temp_sink.append(f)
                        gif_path = await _convert_to_gif(f)
                        if gif_path:
                            if temp_sink is not None:
                                temp_sink.append(gif_path)
                            results.append(
                                _chain([CompImage.fromFileSystem(gif_path)])
                            )
                        else:
                            results.append(
                                _chain([CompVideo.fromFileSystem(f)])
                            )
            for vid in info.get("videos", []):
                vurl = vid.get("video_url", vid.get("media_url", ""))
                if vurl:
                    f = await self._download_file(vurl)
                    if f:
                        if temp_sink is not None:
                            temp_sink.append(f)
                        results.append(
                            _chain([CompVideo.fromFileSystem(f)])
                        )

            # 记录到推送历史（手动推送），溯源时间与会话
            self._total_pushes += 1
            self._log_push(
                f"@{info['screen_name']} [手动] → {event.unified_msg_origin[:20]}…",
                "push",
            )
            self._record_push_history(info, event.unified_msg_origin, source="manual")
        except Exception as e:
            logger.error(f"Failed to push tweet {tweet_id}: {e}")
            results.append(_plain(f"推送失败: {str(e)[:100]}"))
        return results

    async def _cmd_monitor(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        if umo in self.monitored_sessions:
            self.monitored_sessions.discard(umo)
            self._save_data()
            return _plain("已关闭本会话的自动推送")
        else:
            self.monitored_sessions.add(umo)
            self._save_data()
            if any(self.subscriptions.get(s) for s in self.monitored_sessions):
                self._start_monitor()
            return _plain("已开启本会话的自动推送")

    async def _monitor_loop(self):
        interval = max(1, int(self.config.get("poll_interval", 5))) * 60
        total_subs = (
            sum(len(users) for users in self.subscriptions.values())
            if self.subscriptions
            else 0
        )
        logger.info(
            f"[Monitor] Loop started, interval={interval}s, subscriptions={len(self.subscriptions)}, tracked_users={total_subs}, monitored_sessions={len(self.monitored_sessions)}"
        )
        while self._running:
            try:
                await self.twitter.ensure_ready()
            except Exception as e:
                logger.warning(f"Twitter client not ready: {e}")
                await asyncio.sleep(60)
                continue

            # Build unique user set across all sessions
            unique_users = {}
            user_sessions = {}
            for session_umo, session_users in list(self.subscriptions.items()):
                for username, info in session_users.items():
                    if username not in unique_users:
                        unique_users[username] = info
                    user_sessions.setdefault(username, []).append(session_umo)

            for username in list(unique_users.keys()):
                try:
                    info = unique_users[username]
                    user_id = info["user_id"]
                    tweets = await self.twitter.get_user_tweets(user_id, count=20)

                    # Backfill avatar/display name for legacy subscriptions missing them
                    if tweets and not info.get("avatar_url"):
                        tu = getattr(tweets[0], "user", None)
                        av = (getattr(tu, "profile_image_url", "") or "") if tu else ""
                        if av:
                            av = av.replace("_normal.", "_400x400.")
                        disp_name = (getattr(tu, "name", "") or "") if tu else ""
                        for sess_umo in user_sessions.get(username, []):
                            sess_users = self.subscriptions.get(sess_umo)
                            if sess_users and username in sess_users:
                                if av:
                                    sess_users[username]["avatar_url"] = av
                                if disp_name:
                                    sess_users[username]["name"] = disp_name
                        self._schedule_save("data", self._write_data_payload, self._data_payload)
                        logger.info(f"[Monitor] Backfilled avatar for @{username}")

                    # Highest last_tweet_id across sessions tracking this user
                    last_id = "0"
                    for sess_umo in user_sessions.get(username, []):
                        sess_users = self.subscriptions.get(sess_umo, {})
                        uid = sess_users.get(username, {}).get("last_tweet_id", "0")
                        if uid > last_id:
                            last_id = uid

                    new_tweets = [t for t in tweets if t.id > last_id]

                    if new_tweets:
                        # 各会话基线独立, 先算出每个会话自己的待推队列(旧→新)。
                        # 随后按"推文"维度批量推送: 同一条推文只建卡一次(翻译/取色/
                        # 渲染各一次), 再复用给所有需要它的会话 —— 原实现是
                        # _process_and_push(data, [sess_umo]) 逐会话各建一次卡,
                        # N 个会话就要渲染/翻译 N 次, 长文分块还会再翻倍。
                        try:
                            backlog_budget = max(
                                1, int(self.config.get("backlog_budget", 10) or 10)
                            )
                        except (TypeError, ValueError):
                            backlog_budget = 10
                        queues = {}
                        for sess_umo in user_sessions.get(username, []):
                            sess_users = self.subscriptions.get(sess_umo)
                            sess_info = (sess_users or {}).get(username)
                            if not sess_info:
                                continue
                            # 未监控的会话: 丢弃本轮新推文并推进基线, 防开启监控后补推轰炸
                            if sess_umo not in self.monitored_sessions:
                                sess_info["last_tweet_id"] = new_tweets[0].id
                                sess_info["last_checked_at"] = datetime.now(
                                    timezone.utc
                                ).isoformat()
                                continue
                            sess_new = [
                                t
                                for t in tweets
                                if t.id > sess_info.get("last_tweet_id", "0")
                            ]
                            if not sess_new:
                                continue
                            # 积压预算: 掉线期间可能积压大量推文, 每轮只补推最旧的 N 条
                            if len(sess_new) > backlog_budget:
                                logger.warning(
                                    f"[Monitor] {username} → {sess_umo[:20]}…: "
                                    f"backlog {len(sess_new)} > budget {backlog_budget}, "
                                    f"staggering into multiple rounds"
                                )
                                sess_new = sess_new[-backlog_budget:]
                            logger.info(
                                f"[Monitor] {username} → {sess_umo[:20]}…: "
                                f"{len(sess_new)} new tweets "
                                f"(last={sess_info.get('last_tweet_id', '0')[:15]}..)"
                            )
                            queues[sess_umo] = list(reversed(sess_new))  # 旧→新
                        pushed_any = await self._push_pending_tweets(
                            username, queues, tweets
                        )
                        self._schedule_save("data", self._write_data_payload, self._data_payload)
                        if pushed_any:
                            logger.info(
                                f"[Monitor] {username}: pushed, baseline advanced per session"
                            )
                    else:
                        logger.debug(f"[Monitor] {username}: no new tweets")
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    estr = str(e)
                    if "429" in estr or "Rate limit" in estr:
                        logger.warning(
                            f"[Monitor] Rate limited for {username}, aborting this round"
                        )
                        self._log_push(f"@{username} 触发速率限制，本轮中止", "error")
                        break
                    logger.error(f"[Monitor] Error for {username}: {e}")
                    self._log_push(f"@{username} 检查异常: {estr[:60]}", "error")
                    # 单个账号异常不影响其余账号: 原实现在非限流异常时也会继续
                    # 下一个账号, 这里显式 sleep 让出事件循环避免密集重试
                    await asyncio.sleep(1)

            # 每轮回收: 已过宽限期的媒体文件 + 陈旧残留下载/卡片文件,
            # 保证长时间运行磁盘不会只增不减
            try:
                await asyncio.to_thread(_flush_released_temp)
                await asyncio.to_thread(_sweep_stale_temp_files)
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def _extract_seed_color(self, image_url: str):
        if image_url in self._seed_cache:
            return self._seed_cache[image_url]
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": "https://x.com/",
            }
            img_bytes = None
            used_url = image_url
            _c = self._get_http_client()
            async with self._get_http_semaphore():
                if "_normal." in image_url:
                    sizes = ["_400x400.", "_bigger.", "_normal."]
                    base = image_url.replace("_normal.", "{}")
                    for s in sizes:
                        try:
                            u = base.format(s)
                            _r = await _c.get(u, headers=headers, timeout=15)
                            _r.raise_for_status()
                            img_bytes = _r.content
                            used_url = u
                            break
                        except Exception:
                            continue
                else:
                    _r = await _c.get(image_url, headers=headers, timeout=15)
                    _r.raise_for_status()
                    img_bytes = _r.content
            if not img_bytes:
                return (103, 80, 164)
            # 解码 + 量化是纯 CPU 的同步操作(最高 128 色的 MCU 量化可达上百毫秒),
            # 放在事件循环里会直接卡住 AstrBot/NapCat 的心跳与收发
            rgb = await asyncio.to_thread(
                self._quantize_seed_color, img_bytes
            )
            if rgb is None:
                return (103, 80, 164)
            logger.debug(f"Seed extracted: RGB={rgb} from {used_url.split('/')[-1][:40]}")
            # 简单的 FIFO 上限: 长跑下头像 URL 会不断累积, 不封顶就是内存泄漏
            if len(self._seed_cache) >= 512:
                for k in list(self._seed_cache)[:128]:
                    self._seed_cache.pop(k, None)
            self._seed_cache[image_url] = rgb
            return rgb
        except Exception as e:
            logger.warning(f"Seed color extraction failed: {type(e).__name__}: {e}")
            self._seed_cache[image_url] = (103, 80, 164)
            return (103, 80, 164)

    async def _file_to_data_uri_async(self, path: str, max_side: int = 640) -> str:
        """在线程池里做缩略图编码(PIL 解码+缩放+JPEG 编码是同步 CPU 操作)。"""
        try:
            return await asyncio.to_thread(_file_to_data_uri, path, max_side)
        except Exception as e:
            logger.warning(f"[DenpaPush] thumbnail encode failed: {e}")
            return ""

    @staticmethod
    def _quantize_seed_color(img_bytes: bytes):
        """同步的取色计算(供线程池调用)。"""
        import io

        from PIL import Image
        from material_color_utilities import prominent_colors_from_image

        with Image.open(io.BytesIO(img_bytes)) as im:
            im = im.convert("RGBA")
            # align with MCU: downsample to 48×48 before quantizing
            im = im.resize((48, 48), Image.LANCZOS)
        colors = prominent_colors_from_image(im, max_colors=128)
        if not colors:
            return None
        # colors 是 RRGGBB hex 格式 #rrggbb
        h = colors[0].lstrip("#")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    async def _build_palette_async(self, seed_rgb):
        """在线程池里算 Material 配色(MCU 主题求解是纯 CPU 密集计算)。"""
        return await asyncio.to_thread(self._generate_palette, seed_rgb)

    def _generate_palette(self, seed_rgb):
        h = int(
            __import__("datetime")
            .datetime.now(
                __import__("datetime").timezone(
                    __import__("datetime").timedelta(hours=8)
                )
            )
            .strftime("%H")
        )
        is_dark = h >= 18 or h < 6

        try:
            from material_color_utilities import theme_from_color

            def rgb_str(c):
                return f"{int(c[1:3], 16)}, {int(c[3:5], 16)}, {int(c[5:7], 16)}"

            hex_color = f"#{seed_rgb[0]:02x}{seed_rgb[1]:02x}{seed_rgb[2]:02x}"
            theme = theme_from_color(hex_color)
            scheme = theme.schemes.dark if is_dark else theme.schemes.light
            palette = {
                "primary": scheme.primary,
                "primary_rgb": rgb_str(scheme.primary),
                "on_primary": scheme.on_primary,
                "on_primary_rgb": rgb_str(scheme.on_primary),
                "secondary": scheme.secondary,
                "secondary_rgb": rgb_str(scheme.secondary),
                "surface": scheme.surface,
                "surface_rgb": rgb_str(scheme.surface),
                "surface_variant": scheme.surface_variant,
                "surface_variant_rgb": rgb_str(scheme.surface_variant),
                "on_surface": scheme.on_surface,
                "on_surface_rgb": rgb_str(scheme.on_surface),
                "on_surface_variant": scheme.on_surface_variant,
                "on_surface_variant_rgb": rgb_str(scheme.on_surface_variant),
                "background": scheme.background,
                "background_rgb": rgb_str(scheme.background),
                "surface_container": scheme.surface_container,
                "surface_container_rgb": rgb_str(scheme.surface_container),
            }
            logger.debug(
                f"Dynamic palette from seed={hex_color} (dark={is_dark}): primary={scheme.primary}"
            )
            return palette, is_dark
        except Exception as e:
            logger.debug(f"Dynamic palette failed: {e}")

        # Fallback hardcoded palettes
        if is_dark:
            return {
                "primary": "#d0bcff",
                "primary_rgb": "208, 188, 255",
                "on_primary": "#381e72",
                "on_primary_rgb": "56, 30, 114",
                "secondary": "#cac4d0",
                "secondary_rgb": "202, 196, 208",
                "surface": "#1c1b1f",
                "surface_rgb": "28, 27, 31",
                "surface_variant": "#141318",
                "surface_variant_rgb": "20, 19, 24",
                "on_surface": "#e6e1e5",
                "on_surface_rgb": "230, 225, 229",
                "on_surface_variant": "#c9c5d0",
                "on_surface_variant_rgb": "201, 197, 208",
                "background": "#141318",
                "background_rgb": "20, 19, 24",
                "surface_container": "#1c1b1f",
                "surface_container_rgb": "28, 27, 31",
            }, is_dark
        return {
            "primary": "#5700d2",
            "primary_rgb": "87, 0, 210",
            "on_primary": "#ffffff",
            "on_primary_rgb": "255, 255, 255",
            "secondary": "#554262",
            "secondary_rgb": "85, 66, 98",
            "surface": "#fdf7ff",
            "surface_rgb": "253, 247, 255",
            "surface_variant": "#efe5ff",
            "surface_variant_rgb": "239, 229, 255",
            "on_surface": "#1d1a24",
            "on_surface_rgb": "29, 26, 36",
            "on_surface_variant": "#49454f",
            "on_surface_variant_rgb": "73, 69, 79",
            "background": "#fdf7ff",
            "background_rgb": "253, 247, 255",
            "surface_container": "#f0eaf8",
            "surface_container_rgb": "240, 234, 248",
        }, is_dark

    async def _prepare_send_images(self, paths: list) -> list:
        """把待发送的原图压成"发送用"副本, 并限制单条消息的内联总量。

        为什么必须做: AstrBot 的 `Node.to_dict()` / `_from_segment_to_dict` 会把
        图片转成 base64 内联进 OneBot 报文(components.py:590-598)。合并转发里若塞
        入 4 张 orig 原图, 按 max_download_mb=64 计, 单条消息的 JSON 可达数百 MB,
        经 websocket 发给 NapCat 时直接把两边内存打爆 —— 这正是"高并发下卡死"的
        主要通道之一。这里按最长边与总量双重限制, 超出的图片直接不发送。
        """
        if not paths:
            return []
        max_side = self._as_int(
            self.config.get("send_image_max_side", 1280), 1280, 320, 4096
        )
        total_budget = (
            self._as_int(self.config.get("send_image_max_total_mb", 12), 12, 1, 128)
            * 1024
            * 1024
        )

        out = []
        used = 0
        for p in paths:
            if not p:
                continue
            try:
                shrunk = await asyncio.to_thread(_shrink_image_file, p, max_side)
            except Exception as e:
                logger.warning(f"[Push] shrink image failed, skipping: {e}")
                continue
            if not shrunk:
                continue
            try:
                size = os.path.getsize(shrunk)
            except OSError:
                continue
            # base64 会放大约 4/3, 这里按编码后的量估算预算
            if used + size * 4 // 3 > total_budget:
                _release_temp([shrunk])
                logger.warning(
                    f"[Push] inline budget exceeded "
                    f"({used // 1024}KB used), dropping remaining images"
                )
                break
            used += size * 4 // 3
            out.append(shrunk)
        return out

    async def _build_card_data(self, data: dict, media_url_to_path: dict = None) -> dict:
        import re as _re

        article = data.get("article")
        if article and article.get("rest_id"):
            try:
                full_text = await self.twitter.get_full_article_text(data["id"])
                if full_text:
                    article["full_text"] = full_text
                    data["article_full_text"] = full_text
            except Exception as e:
                logger.warning(f"Full article fetch failed: {e}")
        elif not article:
            try:
                full_text = await self.twitter.get_full_article_text(data["id"])
                if full_text:
                    data["text"] = full_text
                    data["article_full_text"] = full_text
                    title_m = _re.search(
                        r"<h1[^>]*>(.*?)</h1>", full_text, _re.I | _re.S
                    )
                    title = _re.sub(
                        r"<[^>]+>", "", title_m.group(1) if title_m else ""
                    ).strip()
                    data["article"] = {
                        "title": title,
                        "full_text": full_text,
                        "preview_text": "",
                        "rest_id": data["id"],
                    }
            except Exception as e:
                logger.warning(f"Fallback article fetch failed: {e}")

        # 如果推文是引用推文但未提取到数据（twikit get_tweet_by_id 不返回 quoted_status_result），单独获取
        if data.get("is_quote") and not data.get("quoted_tweet"):
            try:
                q = await self.twitter.fetch_quoted_tweet_data(data["id"])
                if q:
                    data["quoted_tweet"] = q
            except Exception as e:
                logger.warning(f"Failed to fetch quoted tweet data: {e}")

        # 如果引推也有文章（NoteTweet），获取引推文章全文
        q_data = data.get("quoted_tweet", {})
        q_article = q_data.get("article", {}) if q_data else {}
        if q_article and q_article.get("rest_id") and q_data.get("id"):
            try:
                q_art_full = await asyncio.wait_for(
                    self.twitter.get_full_article_text(q_data["id"]), timeout=20
                )
                if q_art_full:
                    q_article["full_text"] = q_art_full
            except Exception as e:
                logger.warning(f"Quoted article fetch failed: {e}")

        translated_text = data.get("text", "")
        try:
            translated_text = await self._translate_text(data)
        except Exception as e:
            logger.warning(f"Translation failed: {e}")
            translated_text = data.get("text", "(翻译失败)")

        # 提取引用推文数据用于卡片
        quoted = data.get("quoted_tweet", {})
        quoted_user = quoted.get("user", {}) if quoted else {}
        quoted_media = quoted.get("media", []) if quoted else []
        quoted_thumbnails = []
        for m in quoted_media[:2]:
            mu = m.get("media_url", "")
            if not mu:
                continue
            if media_url_to_path and mu in media_url_to_path:
                quoted_thumbnails.append(
                    await self._file_to_data_uri_async(media_url_to_path[mu])
                )
            else:
                quoted_thumbnails.append(_twitter_media_url(mu, "medium"))
        q_user_name = quoted_user.get("name", "")
        q_screen_name = quoted_user.get("screen_name", "")
        q_avatar = quoted_user.get("avatar_url", "")

        # 引用推文的文章数据
        q_article_data = quoted.get("article", {}) if quoted else {}
        q_article_title = q_article_data.get("title", "") if q_article_data else ""
        q_article_text = q_article_data.get("full_text", "") if q_article_data else ""
        q_article_preview = (
            q_article_data.get("preview_text", "") if q_article_data else ""
        )

        image_translations = None
        images, gifs, videos = TwitterClient.extract_tweet_media(data)
        # 合并引用推文的媒体到待推送列表
        if quoted:
            q_imgs, q_gifs, q_vids = TwitterClient.extract_tweet_media(quoted)
            images.extend(q_imgs)
            gifs.extend(q_gifs)
            videos.extend(q_vids)
        # 翻译时跳过链接预览图：推文全文只有链接时，图片都是预览图
        raw_text = data.get("text", "").strip()
        urls = data.get("urls", [])
        text_no_urls = raw_text
        for u in urls:
            text_no_urls = (
                text_no_urls.replace(u.get("url", ""), "")
                .replace(u.get("expanded_url", ""), "")
                .strip()
            )
        is_link_only = len(text_no_urls.strip()) < 5
        if is_link_only:
            images_for_translate = []
        else:
            images_for_translate = images
        if images_for_translate:
            try:
                image_translations = await asyncio.wait_for(
                    self._translate_images(images_for_translate), timeout=120
                )
            except asyncio.TimeoutError:
                logger.warning("Image translation timed out, skipping")
                image_translations = None
            except Exception as e:
                logger.warning(f"Image translation failed: {e}")

        article = data.get("article")
        thumbnail_urls = []
        all_media = images + gifs + videos
        for m in all_media[:4]:
            poster = m.get("media_url", "")
            if poster:
                if media_url_to_path and poster in media_url_to_path:
                    thumbnail_urls.append(
                        await self._file_to_data_uri_async(media_url_to_path[poster])
                    )
                else:
                    thumbnail_urls.append(_twitter_media_url(poster, "medium"))

        import os as _os

        tmpl_path = _os.path.join(
            _os.path.dirname(__file__), "templates", "tweet_card.html"
        )
        # 模板读取是阻塞 IO, 放线程池避免偶发卡顿
        template = await asyncio.to_thread(
            lambda: open(tmpl_path, "r", encoding="utf-8").read()
        )

        # 图片译文合并到文字译文末尾
        if image_translations:
            translated_text = f"{translated_text}\n\n{image_translations}"

        raw_avatar = data["user"]["avatar_url"]
        avatar_url = raw_avatar.replace("_normal.", "_400x400.")
        logger.debug(f"Avatar URL: {raw_avatar} -> {avatar_url}")

        color_source = self.config.get("color_source", "avatar")
        seed_url = avatar_url
        if color_source == "first_image":
            # 从原始推文的第一张媒体取色（图片/GIF/视频缩略图）
            orig_all = data.get("media", [])
            if orig_all:
                first_url = orig_all[0].get("media_url", "")
                if first_url:
                    seed_url = _twitter_media_url(first_url, "orig")
                    logger.debug(f"Seed from first media: {seed_url[:80]}...")

        seed_rgb = await self._extract_seed_color(seed_url)
        palette, is_dark = await self._build_palette_async(seed_rgb)
        card_data = {
            "user_name": data["user"]["name"],
            "screen_name": data["user"]["screen_name"],
            "user_id": data["user"]["id"],
            "avatar_url": data["user"]["avatar_url"],
            "created_at_str": (
                data["created_at_datetime"]
                .astimezone(
                    __import__("datetime").timezone(
                        __import__("datetime").timedelta(hours=8)
                    )
                )
                .strftime("%m月%d日 %H:%M")
            )
            if data.get("created_at_datetime")
            and hasattr(data["created_at_datetime"], "strftime")
            else str(data.get("created_at", "")),
            "article_title": article.get("title", "") if article else "",
            "article_cover_url": article.get("cover_url", "") if article else "",
            "article_text": data.get("article_full_text")
            or (article.get("full_text", "") if article else ""),
            "article_preview": article.get("preview_text", "") if article else "",
            "original_text": data.get("text", ""),
            "translated_text": translated_text,
            "image_count": len(images),
            "gif_count": len(gifs),
            "video_count": len(videos),
            "thumbnail_urls": thumbnail_urls,
            "quoted_user_name": q_user_name,
            "quoted_screen_name": q_screen_name,
            "quoted_avatar_url": q_avatar,
            "quoted_text": quoted.get("text", ""),
            "quoted_thumbnail_urls": quoted_thumbnails,
            "has_quoted_tweet": bool(quoted and quoted.get("text")),
            "q_article_title": q_article_title,
            "q_article_text": q_article_text,
            "q_article_preview": q_article_preview,
            "has_q_article": bool(q_article_title or q_article_text),
            "palette": palette,
            "is_dark": is_dark,
            "seed_color": "#%02x%02x%02x" % seed_rgb if seed_rgb else "",
            "tweet_url": f"https://x.com/{data['user']['screen_name']}/status/{data.get('id', '')}",
            "text": data.get("text", ""),
        }

        # 长文章分块渲染
        import re as _re

        article_raw = data.get("article_full_text") or (
            article.get("full_text", "") if article else ""
        )
        article_text = (
            _re.sub(r"<[^>]+>", "", article_raw).strip() if article_raw else ""
        )
        MAX_CHUNK = 2000

        def split_into_chunks(text):
            """Split text into ~MAX_CHUNK chunks at paragraph boundaries."""
            paras = text.split("\n\n")
            chunks, cur, cl = [], [], 0
            for p in paras:
                plen = len(p)
                if cl + plen > MAX_CHUNK and cur:
                    chunks.append("\n\n".join(cur))
                    cur, cl = [], 0
                # if a single paragraph exceeds MAX_CHUNK, force-split at sentence
                if plen > MAX_CHUNK:
                    import re as _re

                    sentences = _re.split(r"(?<=[。！？.!?])", p)
                    s_chunk, s_cl = [], 0
                    for s in sentences:
                        if s_cl + len(s) > MAX_CHUNK and s_chunk:
                            cur.append("".join(s_chunk))
                            cl += len(cur[-1]) + 2
                            s_chunk, s_cl = [s], len(s)
                        else:
                            s_chunk.append(s)
                            s_cl += len(s)
                    if s_chunk:
                        cur.append("".join(s_chunk))
                        cl += len(cur[-1]) + 2
                else:
                    cur.append(p)
                    cl += plen + 2
            if cur:
                chunks.append("\n\n".join(cur))
            return chunks

        if len(article_text) > MAX_CHUNK:
            t_chunks = split_into_chunks(translated_text)

            # 译文块数决定卡片数，原文只用在第一张
            card_img_urls = []
            for i, t_chunk in enumerate(t_chunks):
                sub = dict(card_data)
                sub["article_title"] = (
                    card_data["article_title"]
                    if i == 0
                    else f"(续 {i + 1}/{len(t_chunks)})"
                )
                sub["article_text"] = ""
                if i > 0:
                    sub["article_preview"] = ""
                sub["translated_text"] = t_chunk
                img_url = await self._render_card(template, sub)
                if img_url:
                    card_img_urls.append(img_url)
            if not card_img_urls:
                card_img_urls = [await self._render_card(template, card_data)]
        else:
            card_img_urls = [await self._render_card(template, card_data)]

        return {
            "card_img_urls": card_img_urls,
            "card_img_url": card_img_urls[0] if card_img_urls else "",
            "translated_text": translated_text,
            "images": images,
            "gifs": gifs,
            "videos": videos,
            "screen_name": data["user"]["screen_name"],
            "user_name": data["user"]["name"],
            "user_id": data["user"]["id"],
            "avatar_url": avatar_url,
            "tweet_url": f"https://x.com/{data['user']['screen_name']}/status/{data['id']}",
            "tweet_id": data.get("id", ""),
            "original_text": data.get("text", ""),
            "seed_color": card_data["seed_color"],
            "palette": card_data["palette"],
            "is_dark": card_data["is_dark"],
            "created_at_str": card_data["created_at_str"],
            "image_count": card_data["image_count"],
            "gif_count": card_data["gif_count"],
            "video_count": card_data["video_count"],
            "quoted_screen_name": card_data["quoted_screen_name"],
            "quoted_text": card_data["quoted_text"],
        }

    async def _dump_render_debug(self, html: str, card_data: dict, png_path: str):
        debug_dir = os.path.join(
            getattr(self.context, "astrbot_root", os.getcwd()),
            "data",
            "config",
            "debug_render",
        )
        os.makedirs(debug_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        tweet_id = card_data.get("id", "unknown")
        base = os.path.join(debug_dir, f"{ts}_{tweet_id}")
        try:
            with open(f"{base}.html", "w", encoding="utf-8") as f:
                f.write(html)
            meta = {
                "tweet_id": tweet_id,
                "card_data_keys": list(card_data.keys()),
                "screenshot_png": png_path,
                "rendered_at": ts,
            }
            with open(f"{base}.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            logger.debug(f"Render debug dumped to {base}.*")
        except Exception as e:
            logger.warning(f"Failed to dump render debug: {e}")

    async def _render_card(self, template: str, card_data: dict) -> str:
        """本地 Playwright 渲染 HTML → PNG，返回文件路径。

        并发保护: 渲染受 _render_concurrency 限制。原实现没有任何上限, 高并发时
        会同时开出大量 Chromium page 并各自截图(每张 620×N × deviceScaleFactor=2
        的位图), CPU/内存瞬间打满, AstrBot 事件循环被拖住, NapCat 也随之无响应。
        """
        import tempfile
        import os as _os
        import time as _time

        _tag = f"{id(self)}_{int(_time.time() * 1000000) % 1000000}"
        html_path = _register_temp(
            _os.path.join(tempfile.gettempdir(), f"{_TEMP_PREFIX}{_tag}.html")
        )
        png_path = _register_temp(
            _os.path.join(tempfile.gettempdir(), f"{_TEMP_PREFIX}{_tag}.png")
        )
        from jinja2 import Template

        # Jinja2 渲染是纯 CPU 的同步操作, 长文章模板可到几百毫秒, 放线程池避免卡事件循环
        html = await asyncio.to_thread(Template(template).render, card_data)
        await asyncio.to_thread(_write_text, html_path, html)

        try:
            async with self._get_render_semaphore():
                try:
                    browser = await _get_shared_browser()
                    ctx = await browser.new_context(device_scale_factor=2)
                except Exception as e:
                    logger.error(f"Local card render init failed: {e}")
                    ctx = None

                if ctx is not None:
                    # ctx 必须在 finally 关闭: 原实现只在成功路径 close, 任何异常
                    # (超时/图片卡住/页面崩溃)都会漏掉 context, 长期累积拖垮 Chromium
                    try:
                        page = await ctx.new_page()
                        await page.set_viewport_size({"width": 620, "height": 100})
                        await page.goto(
                            f"file:///{html_path.replace(chr(92), '/')}",
                            wait_until="domcontentloaded",
                            timeout=10000,
                        )
                        await page.wait_for_timeout(300)
                        # 等待所有图片加载完成(成功或失败)再测高/截图,
                        # 瀑布流/网格会随图片加载重排,提前截图会错位或留空白
                        try:
                            await page.wait_for_function(
                                "() => [...document.images].every(img => img.complete)",
                                timeout=8000,
                            )
                        except Exception:
                            pass
                        h = await page.evaluate("document.body.scrollHeight")
                        # 限制截图高度: 超长卡片(长文章/大量图片)会生成巨幅位图,
                        # 单张就能吃掉几百 MB 内存。
                        # 注意: full_page=True 会让 Playwright 重新测量文档真实高度,
                        # 光设 viewport 高度并不能封顶 —— 必须用 clip 明确指定截取区域。
                        max_h = self._as_int(
                            self.config.get("max_card_height", 6000), 6000, 500, 30000
                        )
                        full_h = max(100, int(h or 100))
                        clip_h = min(full_h, max_h)
                        if clip_h < full_h:
                            logger.warning(
                                f"[DenpaPush] Card height {full_h}px exceeds "
                                f"max_card_height={max_h}px, clipping"
                            )
                        await page.set_viewport_size({"width": 620, "height": clip_h})
                        await page.wait_for_timeout(500)
                        await page.screenshot(
                            path=png_path,
                            omit_background=True,
                            clip={"x": 0, "y": 0, "width": 620, "height": clip_h},
                        )
                        if self.config.get("debug_render_dump", False):
                            await self._dump_render_debug(html, card_data, png_path)
                        return png_path
                    except Exception as e:
                        logger.error(f"Local card render failed: {e}")
                    finally:
                        try:
                            await ctx.close()
                        except Exception:
                            pass

            try:
                card_img_url = await self.html_render(
                    template,
                    card_data,
                    options={"type": "png", "full_page": True, "timeout": 15000},
                )
                return card_img_url
            except Exception as e2:
                logger.error(f"Remote card render also failed: {e2}")
                return ""
        finally:
            _remove_temp(html_path)

    async def _process_and_push(self, data: dict, target_sessions: list) -> dict:
        """推送一条推文到各目标会话。

        返回 {session_umo: bool}: 该会话本次是否完整推送成功。
        失败会话由调用方决定基线是否推进(失败不推进, 下轮补推)。
        """
        if not target_sessions:
            return {}

        # 提前下载图片，传给卡片渲染复用
        images, gifs, videos = TwitterClient.extract_tweet_media(data)
        media_url_to_path = {}
        img_files = await asyncio.gather(
            *[
                self._download_file(
                    _twitter_media_url(img.get("media_url", ""), "orig")
                )
                for img in images
                if img.get("media_url", "")
            ]
        )
        for img, path in zip(
            [img for img in images if img.get("media_url", "")], img_files
        ):
            if path:
                media_url_to_path[img["media_url"]] = path

        # 已下载的文件必须先纳入保护: _build_card_data 里任何异常(翻译/取色/渲染)
        # 都会走到下面的 finally, 若此时还没登记这些路径就会永久泄漏在临时目录
        cleanup_paths = [p for p in img_files if p]
        try:
            info = await self._build_card_data(data, media_url_to_path)

            results = {s: False for s in target_sessions}
            # 已发送的本地卡片 PNG 也要回收: 每次渲染都产出新 PNG, 原实现从不删除,
            # 每张几百 KB~数 MB, 长期运行会把磁盘写满
            cleanup_paths.extend(
                p
                for p in info.get("card_img_urls", [])
                if p and not str(p).startswith("http")
            )
            # 发送用图片只压缩一次, 供所有目标会话复用。
            # 原实现在 for session_umo 内压缩, 多会话推送时同一批图会被重复解码
            # 与重编码 N 次(N 倍 CPU + N 倍临时文件), 与"降低 CPU/内存占用"的目标相悖。
            send_images = await self._prepare_send_images(
                [p for p in img_files if p]
            )
            cleanup_paths.extend(send_images)
            # 同一会话的消息串行发出, 避免多条推文并发推送时卡片/图片/视频互相穿插
            async with self._push_lock.get():
                for session_umo in target_sessions:
                    try:
                        # 关键消息(卡片/兜底文本)决定本次是否算推送成功:
                        # context.send_message 在"找不到匹配平台"时返回 False 且不抛异常,
                        # 若忽略返回值仍置成功, 监控循环会推进基线 → 这条推文永久丢失。
                        delivered = True
                        # 主消息：卡片（多张分块）+ 推主注明
                        card_urls = info.get("card_img_urls", [info.get("card_img_url", "")])
                        first_card = True
                        for url in card_urls:
                            if not url:
                                continue
                            card_chain = MessageChain()
                            if url.startswith("http"):
                                card_chain.chain.append(CompImage.fromURL(url))
                            else:
                                card_chain.chain.append(CompImage.fromFileSystem(url))
                            if first_card:
                                card_chain.message(
                                    f"\n📢 @{info['screen_name']}\n{info.get('tweet_url', '')}"
                                )
                                first_card = False
                            logger.info(f"[Push] Card to {session_umo}")
                            if not await self._send(session_umo, card_chain):
                                delivered = False
                                logger.error(
                                    f"[Push] Card not delivered to {session_umo} "
                                    f"(no matching platform?)"
                                )
                            await asyncio.sleep(0.5)
                        if first_card:
                            fallback = MessageChain()
                            if info["translated_text"]:
                                fallback.message(
                                    f"📢 @{info['screen_name']}\n{info.get('tweet_url', '')}\n\n{info['translated_text'][:500]}"
                                )
                            else:
                                fallback.message(
                                    f"📢 @{info['screen_name']} 新推文\n{info.get('tweet_url', '')}"
                                )
                            if not await self._send(session_umo, fallback):
                                delivered = False

                        # 图片合并到一条群合并转发消息。
                        # 复用前面已压缩好的 send_images(循环外只压一次):
                        # 这些图会被 base64 内联进 OneBot 报文, 直接用 orig 原图
                        # 会让单条消息膨胀到几百 MB 打爆内存。
                        from astrbot.api.message_components import Node, Plain

                        img_contents = [CompImage.fromFileSystem(f) for f in send_images]
                        if img_contents:
                            node = Node(
                                uin="0",
                                name=info.get("user_name", info["screen_name"]),
                                content=img_contents,
                            )
                            fwd_chain = MessageChain()
                            fwd_chain.chain.append(node)
                            logger.info(f"[Push] Images forward to {session_umo}")
                            await self._send(session_umo, fwd_chain)
                            await asyncio.sleep(0.5)

                        # GIF/视频直接发送(次要内容: 失败不影响主消息投递判定)
                        for gif in info.get("gifs", []):
                            gurl = gif.get("video_url", gif.get("media_url", ""))
                            if gurl:
                                gif_chain = MessageChain()
                                gif_chain.chain.append(CompVideo.fromURL(gurl))
                                logger.info(f"[Push] GIF to {session_umo}")
                                await self._send(session_umo, gif_chain)
                                await asyncio.sleep(0.5)
                        for vid in info.get("videos", []):
                            vurl = vid.get("video_url", vid.get("media_url", ""))
                            if vurl:
                                vid_chain = MessageChain()
                                vid_chain.chain.append(CompVideo.fromURL(vurl))
                                logger.info(f"[Push] Video to {session_umo}")
                                await self._send(session_umo, vid_chain)
                                await asyncio.sleep(0.5)

                        if delivered:
                            self._total_pushes += 1
                            self._log_push(
                                f"@{info['screen_name']} → {session_umo[:20]}…",
                                "push",
                            )
                            self._record_push_history(info, session_umo, source="auto")
                            results[session_umo] = True
                        else:
                            # 未投递成功 → 不推进基线, 下轮自动补推
                            self._log_push(
                                f"@{info.get('screen_name', '?')} → "
                                f"{session_umo[:20]}… 未找到可用平台，消息未发出",
                                "error",
                            )
                    except Exception as e:
                        logger.error(f"[Push] Failed to push to {session_umo}: {e}")
                        self._log_push(f"推送失败 @{info.get('screen_name', '?')}: {str(e)[:60]}", "error")
        finally:
            # 媒体/卡片临时文件标记为待回收。这里不能立刻删除: 视频等文件是
            # NapCat 后续异步回拉的, 立即删会与之竞态导致发送失败。
            _release_temp(cleanup_paths)
            _release_temp(media_url_to_path.values())
        return results

    async def _push_pending_tweets(
        self, username: str, queues: dict, tweets: list
    ) -> bool:
        """按"推文"维度批量推送, 让同一条推文在所有会话间只建卡一次。

        参数:
          queues: {session_umo: [tweet, ...]} 各会话自己的待推队列, 已按旧→新排序
          tweets: 本轮拉到的全部推文(用于按 id 反查 tweet 对象)

        为什么要聚合: 原实现逐会话调用 _process_and_push(data, [sess_umo]),
        同一推文有几个会话就要完整走几遍"下载媒体 → 翻译 → 取色 → 渲染卡片",
        长文分块时开销再翻 N 倍 —— 这是渲染风暴的主要来源之一。

        顺序保证: 按推文 id 从旧到新推进; 每条推文在每个会话内仍串行发送。
        失败语义不变: 某会话推送失败则其基线不推进, 下轮从该条继续补推。
        """
        if not queues:
            return False

        by_id = {t.id: t for t in tweets}
        # 按推文聚合出 "这条推文要发给哪些会话", 并保持全局旧→新顺序
        session_last_ok = {}  # {sess: 已成功推进到的 tweet_id}
        order = []
        seen = set()
        for sess_umo, seq in queues.items():
            for t in seq:
                if t.id not in seen:
                    seen.add(t.id)
                    order.append(t.id)
        order.sort(key=lambda i: int(i) if str(i).isdigit() else 0)

        pushed_any = False
        for tid in order:
            t = by_id.get(tid)
            if t is None:
                continue
            # 只把"该条仍在各会话待推队列里"的会话作为目标
            targets = [
                sess
                for sess, seq in queues.items()
                if any(x.id == tid for x in seq)
            ]
            if not targets:
                continue

            push_err = None
            results = {}
            try:
                data = TwitterClient.extract_tweet_data(t)
                results = await self._process_and_push(data, targets)
            except Exception as e:
                push_err = e
                logger.error(f"[Monitor] Push failed for {username}: {tid}: {e}")

            for sess_umo in targets:
                sess_users = self.subscriptions.get(sess_umo)
                sess_info = (sess_users or {}).get(username)
                if not sess_info:
                    continue
                if results.get(sess_umo, False):
                    # 推送成功 → 该会话基线推进到本条
                    sess_info["last_tweet_id"] = t.id
                    sess_info["last_checked_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    session_last_ok[sess_umo] = t.id
                    pushed_any = True
                    self._push_fail_counts.pop((sess_umo, username, t.id), None)
                    continue
                # 推送失败 → 基线不推进, 下轮从该条继续补推。
                # 暂时性失败(掉线/网络/超时)不累计轮数; 持久性失败才累计,
                # 连续多轮后强制跳过, 防基线永久卡死。
                fkey = (sess_umo, username, t.id)
                transient = push_err is not None and self._is_transient_failure(push_err)
                if transient:
                    fail_rounds = 0
                    self._log_push(
                        f"@{username} → {sess_umo[:20]}… 推送暂时失败(网络/超时)，下轮自动补推",
                        "error",
                    )
                else:
                    fail_rounds = self._push_fail_counts.get(fkey, 0) + 1
                    self._push_fail_counts[fkey] = fail_rounds
                    self._log_push(
                        f"@{username} → {sess_umo[:20]}… 推送失败，基线未推进，下轮补推",
                        "error",
                    )
                if fail_rounds >= PUSH_MAX_FAIL_ROUNDS:
                    logger.warning(
                        f"[Monitor] {username} → {sess_umo[:20]}…: "
                        f"{str(t.id)[:15]}.. failed {fail_rounds} rounds, skipping"
                    )
                    self._log_push(
                        f"@{username} 推文 {str(t.id)[:12]}… 连续 "
                        f"{fail_rounds} 轮推送失败，已强制跳过",
                        "error",
                    )
                    sess_info["last_tweet_id"] = t.id
                    sess_info["last_checked_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    self._push_fail_counts.pop(fkey, None)
                else:
                    # 该会话卡在这一条: 从它的待推队列里移除本条之后的全部内容,
                    # 保证下轮仍从这一条开始补推(与原有"失败即 break"语义一致)
                    queues[sess_umo] = [
                        x for x in queues[sess_umo] if x.id == t.id
                    ]
            # 每条推文之间稍作停顿, 避免连续轰炸平台
            await asyncio.sleep(2)
        return pushed_any

    async def _get_provider_id(self) -> str:
        pid = self.config.get("text_translate_provider", "")
        if not pid and self.monitored_sessions:
            try:
                pid = await self.context.get_current_chat_provider_id(
                    umo=list(self.monitored_sessions)[0]
                )
            except Exception:
                pass
        return self._normalize_provider_id(pid)

    @staticmethod
    def _normalize_provider_id(pid) -> str:
        """归一化 provider 配置值为 id 字符串。

        配置可能来自 AstrBot select_provider(字符串)、旧版的 dict 结构,
        或用户手填的字符串, 统一取出 id 并去除首尾空白。
        """
        if isinstance(pid, str):
            return pid.strip()
        if isinstance(pid, dict):
            return str(pid.get("id", "") or "").strip()
        return str(pid).strip() if pid else ""

    def _normalize_fallback_list(self, raw) -> list:
        """把回退模型配置归一化为去空、去重、保序的 id 列表。

        兼容三种写法: list(select_providers 多选)、逗号/分号/换行分隔的字符串。
        """
        if isinstance(raw, str):
            items = re.split(r"[,，;；\r\n]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            items = list(raw)
        else:
            items = []

        result = []
        for item in items:
            pid = self._normalize_provider_id(item)
            if pid and pid not in result:
                result.append(pid)
        return result

    def _collect_fallback_providers(self, key: str) -> list:
        """读取回退模型列表配置, 去空去重并保持顺序。"""
        return self._normalize_fallback_list(self.config.get(key, []))

    async def _provider_chain(self, kind: str = "text") -> list:
        """构造 [主模型, *回退模型] 候选链(去重、跳过空值)。

        - text:  主模型 = text_translate_provider(留空时用当前会话默认模型);
                 回退列表 = text_translate_fallback_providers
        - image: 主模型 = image_translate_provider → 文字主模型 → 会话默认模型;
                 回退列表 = image_translate_fallback_providers,
                 留空时沿用文字翻译的回退列表
        """
        if kind == "image":
            primary = self._normalize_provider_id(
                self.config.get("image_translate_provider", "")
            )
            if not primary:
                primary = await self._get_provider_id()
            fallbacks = self._collect_fallback_providers(
                "image_translate_fallback_providers"
            )
            if not fallbacks:
                fallbacks = self._collect_fallback_providers(
                    "text_translate_fallback_providers"
                )
        else:
            primary = await self._get_provider_id()
            fallbacks = self._collect_fallback_providers(
                "text_translate_fallback_providers"
            )

        chain = [primary] if primary else []
        for pid in fallbacks:
            if pid not in chain:
                chain.append(pid)
        return chain

    def _track_token_usage(self, llm_resp):
        """从 LLM 响应对象中提取 token 用量并累计。

        AstrBot 的 LLMResponse.usage 是 TokenUsage dataclass:
          input_other: int   — 非缓存输入 token
          input_cached: int  — 缓存输入 token
          output: int        — 输出 token
          input  (@property) = input_other + input_cached
          total  (@property) = input_other + input_cached + output
        注意 input / total 是 @property, vars() 拿不到, 必须用属性访问。
        """
        if not llm_resp:
            return
        usage = (
            getattr(llm_resp, "usage", None)
            or getattr(llm_resp, "token_usage", None)
            or getattr(llm_resp, "usage_stats", None)
        )
        if not usage:
            return
        try:
            if isinstance(usage, dict):
                # 字典格式 (旧版兼容)
                prompt_t = int(usage.get("prompt_tokens") or usage.get("input_tokens") or usage.get("input") or 0)
                completion_t = int(usage.get("completion_tokens") or usage.get("output_tokens") or usage.get("output") or 0)
                total_t = int(usage.get("total_tokens") or usage.get("total") or (prompt_t + completion_t))
            else:
                # AstrBot TokenUsage 对象: 用属性访问 (input/total 是 @property)
                prompt_t = int(getattr(usage, "input", 0) or getattr(usage, "input_other", 0))
                completion_t = int(getattr(usage, "output", 0))
                total_t = int(getattr(usage, "total", 0) or (prompt_t + completion_t))
            self._token_stats["prompt"] += prompt_t
            self._token_stats["completion"] += completion_t
            self._token_stats["total"] += total_t
            self._token_stats["calls"] += 1
            # 去抖落盘: 高频翻译会把每次调用都写一遍磁盘
            self._schedule_save("token_stats", self._write_token_payload, lambda: dict(self._token_stats), delay=3.0)
        except Exception as e:
            logger.warning(f"[DenpaPush] token usage parse failed: {e}, usage={usage!r}")

    async def _translate_text(self, data: dict) -> str:
        import re as _re

        text = data.get("text", "")
        article = data.get("article")
        if article:
            full = (
                data.get("article_full_text")
                or article.get("full_text")
                or article.get("preview_text", "")
            )
            full = _re.sub(r"<[^>]+>", "", full).strip()
            text = f"{article.get('title', '')}\n\n{full}"

        # 追加引用推文
        quoted = data.get("quoted_tweet", {})
        quoted_text = quoted.get("text", "")
        quoted_user = quoted.get("user", {})
        quoted_article = quoted.get("article", {})
        if quoted_article:
            qa_full = quoted_article.get("full_text") or quoted_article.get(
                "preview_text", ""
            )
            qa_full = _re.sub(r"<[^>]+>", "", qa_full).strip()
            if qa_full:
                quoted_text = f"{quoted_article.get('title', '')}\n\n{qa_full}"
        if quoted_text:
            q_name = quoted_user.get("name", "")
            text = f"{text}\n\n[引用 @{quoted_user.get('screen_name', '')} ({q_name})]:\n{quoted_text}"

        if not text or not text.strip():
            return "(无文字内容)"

        # 过滤链接
        text = _re.sub(r"https?://\S+", "", text).strip()
        if not text:
            return "(无文字内容)"

        target_lang = self.config.get("translation_language", "中文")
        provider_chain = await self._provider_chain("text")
        if not provider_chain:
            return text

        MAX_CHUNK = 10000
        translated_parts = []

        paras = text.split("\n\n")
        chunks, cur, cl = [], [], 0
        for p in paras:
            plen = len(p)
            if cl + plen > MAX_CHUNK and cur:
                chunks.append("\n\n".join(cur))
                cur, cl = [], 0
            cur.append(p)
            cl += plen + 2
        if cur:
            chunks.append("\n\n".join(cur))

        async def _do_chunk(i, chunk):
            prefix = f"(第{i + 1}/{len(chunks)}部分)\n" if len(chunks) > 1 else ""
            default_prompt = f"请将以下内容翻译成{{lang}}，只返回翻译结果:\n\n{{prefix}}{{text}}"
            prompt_tpl = self.config.get("text_translate_prompt", "") or default_prompt
            prompt = prompt_tpl.replace("{lang}", target_lang).replace("{prefix}", prefix).replace("{text}", chunk)
            try:
                llm_resp = await self._llm_generate_with_fallback(
                    provider_ids=provider_chain,
                    prompt=prompt,
                    timeout=self._llm_timeout(),
                )
                if llm_resp and llm_resp.completion_text:
                    return llm_resp.completion_text.strip()
                else:
                    return chunk
            except Exception as e:
                logger.warning(f"LLM translate chunk {i} failed: {e}")
                return chunk

        translated_parts = list(
            await asyncio.gather(
                *[_do_chunk(i, chunk) for i, chunk in enumerate(chunks)]
            )
        )

        return "\n\n".join(translated_parts) if translated_parts else text

    async def _translate_images(self, images: list) -> str:
        if not images:
            return ""

        mode = self.config.get("image_translate_mode", "multimodal")
        target_lang = self.config.get("translation_language", "中文")
        provider_chain = await self._provider_chain("image")

        if not provider_chain:
            return "(未配置翻译提供商)"

        img_urls = [
            img.get("media_url", "") for img in images[:4] if img.get("media_url", "")
        ]
        if not img_urls:
            return ""

        if mode == "multimodal":
            default_img_prompt = (
                f"理解图片内容并翻译成{{lang}}，"
                f"自行组织格式使用户能简单直接理解。"
                f"尽量简短，不要使文本量过大影响阅读。"
                f"如果图片中没有文字输出'(无文字)'。"
            )
            img_prompt_tpl = self.config.get("image_translate_prompt", "") or default_img_prompt

            async def _translate_one(url):
                try:
                    prompt = img_prompt_tpl.replace("{lang}", target_lang)
                    resp = await self._llm_generate_with_fallback(
                        provider_ids=provider_chain,
                        prompt=prompt,
                        image_urls=[url],
                        timeout=60,
                    )
                    return (resp.completion_text or "") if resp else ""
                except Exception as e:
                    logger.warning(
                        f"Image LLM timeout/fail: {url[:50]} - {type(e).__name__}"
                    )
                    return ""

            results = await asyncio.gather(
                *[_translate_one(u) for u in img_urls], return_exceptions=True
            )
            parts = [
                r for r in results if isinstance(r, str) and r and "(无文字)" not in r
            ]
            return " | ".join(parts)
        elif mode == "text_extraction":
            translations = []
            for img_url in img_urls:
                text_in_image = await self._ocr_image(img_url)
                if text_in_image:
                    llm_resp = await self._llm_generate_with_fallback(
                        provider_ids=provider_chain,
                        prompt=f"将以下内容翻译成{target_lang}:\n\n{text_in_image}",
                        timeout=self._llm_timeout(),
                    )
                    result = (llm_resp.completion_text or "") if llm_resp else ""
                    if result:
                        translations.append(result)
            if translations:
                return " | ".join(translations)
        return ""

    def _get_ocr_reader(self):
        """惰性创建并复用 easyocr Reader 单例。

        原先每次 OCR 都 new 一个 Reader(["ch_sim","en"]) —— 会重复加载数百 MB
        模型, 且加载与推理都是同步 CPU 操作, 直接跑在事件循环上会把 AstrBot
        整条链路卡住(同步调用也无法被 wait_for 取消)。
        """
        reader = getattr(self, "_ocr_reader", None)
        if reader is None:
            from easyocr import Reader

            reader = Reader(["ch_sim", "en"], gpu=False)
            self._ocr_reader = reader
        return reader

    async def _ocr_image(self, img_url: str) -> str:
        """下载图片并做 OCR。下载走共享连接池与大小上限, 识别在线程池执行。"""
        tmp_path = None
        try:
            path = await self._download_file(img_url, suffix=".jpg")
            if not path:
                return ""
            tmp_path = path
            # 注意: reader 本身也必须在线程池里构造 —— self._get_ocr_reader() 作为
            # 实参会在调用 to_thread 之前、于事件循环线程上求值, 那样首次 OCR 仍会
            # 同步加载数百 MB 模型把事件循环卡住。这里分两步, 构造与推理都在 worker。
            reader = await asyncio.to_thread(self._get_ocr_reader)
            results = await asyncio.to_thread(reader.readtext, tmp_path)
            return " ".join(txt for _, txt, _ in results)
        except ImportError:
            return ""
        except Exception as e:
            logger.warning(f"OCR failed: {e}")
            return ""
        finally:
            _remove_temp(tmp_path)
