import asyncio
import json
import os
import re
from datetime import datetime, timezone

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image as CompImage, Video as CompVideo, Plain
from astrbot.api.star import Context, Star, register

from .twitter_client import TwitterClient

DATA_DIR = "data/config"
DATA_FILE = "astrbot_plugin_twitter_monitor_data.json"


@register(
    "astrbot_plugin_twitter_monitor",
    "astrbot_user",
    "Twitter/X 推文监控、翻译与推送插件",
    "1.0.0",
)
class TwitterMonitorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.twitter = TwitterClient()
        self.subscriptions = {}  # {session_umo: {username: {info}}}
        self.monitored_sessions = set()
        self.monitor_task = None
        self._running = False
        self._data_path = self._get_data_path()

    def _get_data_path(self):
        root = getattr(self.context, "astrbot_root", os.getcwd())
        return os.path.join(root, DATA_DIR, DATA_FILE)

    async def initialize(self):
        try:
            import twikit

        except ImportError:
            logger.error("twikit 未安装，请确保 requirements.txt 中的依赖已被安装")
        self._apply_twitter_credentials()
        self._load_data()
        auto_monitor = True
        if self.subscriptions and auto_monitor:
            self._start_monitor()
        logger.info("Twitter Monitor plugin initialized")

    def _apply_twitter_credentials(self):
        auth_token = self.config.get("twitter_auth_token", "")
        ct0 = self.config.get("twitter_ct0", "")
        if auth_token:
            self.twitter.set_credentials(auth_token, ct0)

    async def terminate(self):
        self._running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = None
        self._save_data()

    def _load_data(self):
        try:
            if os.path.exists(self._data_path):
                with open(self._data_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
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
                logger.info(f"Loaded {len(self.subscriptions)} sessions with {total} tracked users")
        except Exception as e:
            logger.warning(f"Failed to load data: {e}")
            self.subscriptions = {}
            self.monitored_sessions = set()

    def _save_data(self):
        try:
            os.makedirs(os.path.dirname(self._data_path), exist_ok=True)
            data = {
                "subscriptions": self.subscriptions,
                "monitored_sessions": list(self.monitored_sessions),
            }
            with open(self._data_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save data: {e}")

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
            yield event.plain_result(
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
            yield event.plain_result("请先在插件配置中设置 twitter_auth_token 和 twitter_ct0")
            return
        self.twitter.set_credentials(auth_token, self.config.get("twitter_ct0", ""))

        if sub == "add" and len(parts) >= 3:
            yield await self._cmd_add(event, parts[2])
        elif sub == "remove" and len(parts) >= 3:
            yield await self._cmd_remove(event, parts[2])
        elif sub == "list":
            yield await self._cmd_list(event)
        elif sub == "push" and len(parts) >= 3:
            async for result in self._cmd_push(event, parts[2]):
                yield result
        elif sub == "monitor":
            yield await self._cmd_monitor(event)
        else:
            yield event.plain_result(f"未知子指令: {sub}")

    @filter.llm_tool(name="twitter_add")
    async def twitter_add(self, event: AstrMessageEvent, usernames: list):
        '''当用户说「关注」「订阅」「跟踪」某个推特账号时使用此工具。会开始监控该用户的推文并自动推送新内容。

        Args:
            usernames(array[string]): 要关注的用户名，如 ["ApexLiveComms"]
        '''
        if isinstance(usernames, str):
            usernames = [usernames]
        for name in usernames:
            result = await self._cmd_add(event, name)
            yield result

    @filter.llm_tool(name="twitter_remove")
    async def twitter_remove(self, event: AstrMessageEvent, usernames: list):
        '''当用户说「取消关注」「取关」「删除订阅」某个推特账号时使用此工具。支持模糊匹配。
        Args:
            usernames(array[string]): 要取消关注的用户名，如 ["apexlive"]
        '''
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
        '''当用户发来一个推特链接并要求「推送」「翻译」「看看」「读取」「解析」时使用此工具。会获取推文内容、翻译、生成卡片并发送图片/视频。不要自己解释推文内容，交给此工具处理。

        Args:
            url(string): 推特链接，如 https://x.com/username/status/123456
        '''
        async for r in self._cmd_push(event, url):
            yield r

    @filter.llm_tool(name="twitter_list")
    async def twitter_list(self, event: AstrMessageEvent):
        '''列出当前群聊已关注的 Twitter 用户。'''
        umo = event.unified_msg_origin
        session_users = self.subscriptions.get(umo, {})
        lines = ["已关注用户:"]
        for name in session_users:
            lines.append(f"  @{name}")
        yield event.plain_result("\n".join(lines) if len(lines) > 1 else "暂无关注用户")

    @filter.llm_tool(name="twitter_monitor")
    async def twitter_monitor(self, event: AstrMessageEvent):
        '''开启或关闭当前会话的自动推送。'''
        umo = event.unified_msg_origin
        if umo in self.monitored_sessions:
            self.monitored_sessions.discard(umo)
            self._save_data()
            yield event.plain_result("已关闭本会话的自动推送")
        else:
            self.monitored_sessions.add(umo)
            self._save_data()
            if any(self.subscriptions.get(s) for s in self.monitored_sessions):
                self._start_monitor()
            yield event.plain_result("已开启本会话的自动推送")

    async def _cmd_add(self, event: AstrMessageEvent, username: str):
        username = username.lstrip("@")
        umo = event.unified_msg_origin
        session_users = self.subscriptions.setdefault(umo, {})
        if username in session_users:
            return event.plain_result(f"本群已关注 @{username}")
        try:
            user = await self.twitter.get_user_by_screen_name(username)
            tweets = await self.twitter.get_user_tweets(user.id, count=1)
            last_id = tweets[0].id if tweets else "0"
            session_users[username] = {
                "user_id": user.id,
                "last_tweet_id": last_id,
                "last_checked_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_data()
            self._start_monitor()
            return event.plain_result(f"本群已关注 @{username}（{user.name}），开始跟踪")
        except Exception as e:
            logger.error(f"Failed to add user {username}: {e}")
            return event.plain_result(f"添加失败: {str(e)[:100]}")

    async def _cmd_remove(self, event: AstrMessageEvent, username: str):
        username = username.lstrip("@")
        umo = event.unified_msg_origin
        session_users = self.subscriptions.get(umo, {})
        if username not in session_users:
            return event.plain_result(f"本群未关注 @{username}")
        del session_users[username]
        if not session_users:
            del self.subscriptions[umo]
        self._save_data()
        if not self.subscriptions:
            self._stop_monitor()
        return event.plain_result(f"本群已取消关注 @{username}")

    async def _cmd_list(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        session_users = self.subscriptions.get(umo, {})
        if not session_users:
            return event.plain_result("本群暂无关注用户")
        lines = ["本群关注用户:"]
        for name, info in session_users.items():
            lines.append(f"  @{name}  (最后ID: {info.get('last_tweet_id', 'N/A')[:12]}...)")
        return event.plain_result("\n".join(lines))

    async def _cmd_push(self, event: AstrMessageEvent, url: str):
        m = re.search(r"(?:twitter\.com|x\.com)/(\w+)/status/(\d+)", url)
        if not m:
            yield event.plain_result("无效的推文链接，格式: https://x.com/username/status/123456")
            return
        username, tweet_id = m.group(1), m.group(2)
        try:
            yield event.plain_result("正在获取推文...")
            tweet = await self.twitter.get_tweet_by_id(tweet_id)
            data = TwitterClient.extract_tweet_data(tweet)
            info = await self._build_card_data(data)

            # 1. 卡片 PNG 直接发送
            if info["card_img_url"]:
                yield event.image_result(info["card_img_url"])
            yield event.plain_result(f"📢 @{info['screen_name']}\n{info.get('tweet_url', '')}")

            # 2. 图片合并到一条群合并转发消息
            from astrbot.api.message_components import Node, Plain
            uname = info.get("user_name", info["screen_name"])
            img_contents = [Plain(f"📸 @{info['screen_name']} 的图片")]
            for img in info.get("images", []):
                iurl = img.get("media_url", "")
                if iurl:
                    img_contents.append(CompImage.fromURL(iurl))
            if len(img_contents) > 1:
                node = Node(uin="0", name=uname, content=img_contents)
                yield event.chain_result([node])
            for gif in info.get("gifs", []):
                vurl = gif.get("video_url", gif.get("media_url", ""))
                if vurl:
                    yield event.chain_result([CompVideo.fromURL(vurl)])
            for vid in info.get("videos", []):
                vurl = vid.get("video_url", vid.get("media_url", ""))
                if vurl:
                    yield event.chain_result([CompVideo.fromURL(vurl)])
        except Exception as e:
            logger.error(f"Failed to push tweet {tweet_id}: {e}")
            yield event.plain_result(f"推送失败: {str(e)[:100]}")

    async def _cmd_monitor(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        if umo in self.monitored_sessions:
            self.monitored_sessions.discard(umo)
            self._save_data()
            return event.plain_result("已关闭本会话的自动推送")
        else:
            self.monitored_sessions.add(umo)
            self._save_data()
            if any(self.subscriptions.get(s) for s in self.monitored_sessions):
                self._start_monitor()
            return event.plain_result("已开启本会话的自动推送")

    async def _monitor_loop(self):
        interval = max(1, int(self.config.get("poll_interval", 5))) * 60
        total_subs = sum(len(users) for users in self.subscriptions.values()) if self.subscriptions else 0
        logger.info(f"[Monitor] Loop started, interval={interval}s, subscriptions={len(self.subscriptions)}, tracked_users={total_subs}, monitored_sessions={len(self.monitored_sessions)}")
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

                    # Highest last_tweet_id across sessions tracking this user
                    last_id = "0"
                    for sess_umo in user_sessions.get(username, []):
                        sess_users = self.subscriptions.get(sess_umo, {})
                        uid = sess_users.get(username, {}).get("last_tweet_id", "0")
                        if uid > last_id:
                            last_id = uid

                    new_tweets = [t for t in tweets if t.id > last_id]

                    if new_tweets:
                        target_sessions = [s for s in user_sessions.get(username, []) if s in self.monitored_sessions]
                        logger.info(
                            f"[Monitor] {username}: {len(new_tweets)} new tweets "
                            f"(last={last_id[:15]}.., targets={len(target_sessions)})"
                        )

                        for t in reversed(new_tweets):
                            data = TwitterClient.extract_tweet_data(t)
                            await self._process_and_push(data, target_sessions)
                            await asyncio.sleep(2)

                        max_id = new_tweets[0].id
                        for sess_umo in user_sessions.get(username, []):
                            sess_users = self.subscriptions.get(sess_umo)
                            if sess_users and username in sess_users:
                                sess_users[username]["last_tweet_id"] = max_id
                                sess_users[username]["last_checked_at"] = (
                                    datetime.now(timezone.utc).isoformat()
                                )
                        self._save_data()
                        logger.info(f"[Monitor] {username}: last_id updated to {max_id[:15]}..")
                    else:
                        logger.debug(f"[Monitor] {username}: no new tweets")
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.error(f"[Monitor] Error for {username}: {e}")

            await asyncio.sleep(interval)

    async def _extract_seed_color(self, avatar_url: str):
        try:
            import httpx
            from PIL import Image
            import io
            proxy = self.config.get("proxy", None)
            async with httpx.AsyncClient(proxy=proxy if proxy else None, timeout=10) as _c:
                _r = await _c.get(avatar_url)
                _r.raise_for_status()
                _img = Image.open(io.BytesIO(_r.content)).convert("RGBA")
                _img = _img.resize((1, 1), resample=Image.Resampling.LANCZOS)
                _pr, _pg, _pb, _pa = _img.getpixel((0, 0))
                _seed = (255 << 24) | (_pr << 16) | (_pg << 8) | _pb
                logger.debug(f"Seed color extracted: ARGB={_seed} RGB=({_pr},{_pg},{_pb})")
                return _seed
        except Exception as e:
            logger.warning(f"Seed color extraction failed: {e}")
            return "#6750a4"

    def _generate_md3_palette(self, seed) -> dict:
        # Try PyMCUlib (pure Python official MCU implementation)
        try:
            from PyMCUlib.hct import Hct
            from PyMCUlib.scheme.scheme_vibrant import SchemeVibrant
            from PyMCUlib.dynamiccolor.material_dynamic_colors import MaterialDynamicColors
            from PyMCUlib.utils.string_utils import hex_from_argb
            
            if isinstance(seed, str):
                seed_int = int(seed.lstrip("#"), 16) | (0xFF << 24)
            else:
                seed_int = int(seed) if seed > 0xFFFFFF else int(seed) | (0xFF << 24)
            
            scheme = SchemeVibrant(
                Hct.from_int(seed_int),
                False,  # is_dark=False for light theme
                0.25    # contrast_level (default in official MCU)
            )
            
            mdc = MaterialDynamicColors()
            
            def _get_hex(dynamic_color_func, scheme):
                argb = dynamic_color_func().get_argb(scheme)
                return hex_from_argb(argb)
            
            palette = {
                "surface": _get_hex(mdc.surface, scheme),
                "surface_variant": _get_hex(mdc.surface_variant, scheme),
                "primary": _get_hex(mdc.primary, scheme),
                "on_primary": _get_hex(mdc.on_primary, scheme),
                "primary_container": _get_hex(mdc.primary_container, scheme),
                "on_primary_container": _get_hex(mdc.on_primary_container, scheme),
                "secondary": _get_hex(mdc.secondary, scheme),
                "on_surface": _get_hex(mdc.on_surface, scheme),
                "on_surface_variant": _get_hex(mdc.on_surface_variant, scheme),
                "outline": _get_hex(mdc.outline, scheme),
                "outline_variant": _get_hex(mdc.outline_variant, scheme),
                "footer": _get_hex(mdc.outline_variant, scheme),
                "quote_bg": _get_hex(mdc.surface_variant, scheme),
            }
            logger.debug(f"Generated MD3 palette (PyMCUlib): {json.dumps(palette)}")
            return palette
        except Exception as e:
            logger.debug(f"PyMCUlib failed: {e}, trying pure Python fallback")

        # Pure Python MD3-like palette generator (fallback)
        try:
            if isinstance(seed, str):
                seed_int = int(seed.lstrip("#"), 16)
            else:
                seed_int = int(seed)
            sr = (seed_int >> 16) & 0xFF
            sg = (seed_int >> 8) & 0xFF
            sb = seed_int & 0xFF
            
            def _rgb_to_hsl(r, g, b):
                r, g, b = r / 255.0, g / 255.0, b / 255.0
                mx, mn = max(r, g, b), min(r, g, b)
                l = (mx + mn) / 2.0
                if mx == mn:
                    h = s = 0.0
                else:
                    d = mx - mn
                    s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
                    if mx == r: h = (g - b) / d + (6.0 if g < b else 0.0)
                    elif mx == g: h = (b - r) / d + 2.0
                    else: h = (r - g) / d + 4.0
                    h /= 6.0
                return h * 360.0, s, l
            
            def _hsl_to_rgb(h, s, l):
                h = h / 360.0
                if s == 0:
                    r = g = b = l
                else:
                    def hue2rgb(p, q, t):
                        if t < 0: t += 1
                        if t > 1: t -= 1
                        if t < 1/6: return p + (q - p) * 6 * t
                        if t < 1/2: return q
                        if t < 2/3: return p + (q - p) * (2/3 - t) * 6
                        return p
                    q = l * (1 + s) if l < 0.5 else l + s - l * s
                    p = 2 * l - q
                    r = hue2rgb(p, q, h + 1/3)
                    g = hue2rgb(p, q, h)
                    b = hue2rgb(p, q, h - 1/3)
                return int(r * 255), int(g * 255), int(b * 255)
            
            h, s, l = _rgb_to_hsl(sr, sg, sb)
            
            def _role(hue, sat, tone):
                r, g, b = _hsl_to_rgb(hue, sat, tone)
                return f"#{r:02x}{g:02x}{b:02x}"
            
            palette = {
                "surface": _role(h, s * 0.1, 0.95),
                "surface_variant": _role(h, s * 0.2, 0.85),
                "primary": _role(h, min(s * 1.2, 1.0), 0.5),
                "on_primary": _role(h, s * 0.1, 0.1 if l > 0.5 else 0.95),
                "primary_container": _role(h, min(s * 1.2, 1.0), 0.8),
                "on_primary_container": _role(h, min(s * 1.2, 1.0), 0.1),
                "secondary": _role((h + 15) % 360, s * 0.6, 0.5),
                "on_surface": _role(h, s * 0.1, 0.1),
                "on_surface_variant": _role(h, s * 0.2, 0.3),
                "outline": _role(h, s * 0.2, 0.4),
                "outline_variant": _role(h, s * 0.2, 0.6),
                "footer": _role(h, s * 0.2, 0.6),
                "quote_bg": _role(h, s * 0.2, 0.85),
            }
            logger.debug(f"Generated MD3 palette (pure Python): {json.dumps(palette)}")
            return palette
        except Exception as e:
            logger.warning(f"MD3 palette pure Python fallback failed: {e}")

        # Final hardcoded fallback
        return {
            "surface": "#fdf7ff",
            "surface_variant": "#e7dff2",
            "primary": "#5700d2",
            "on_primary": "#ffffff",
            "primary_container": "#9f7aff",
            "on_primary_container": "#1c004f",
            "secondary": "#554262",
            "on_surface": "#1d1a24",
            "on_surface_variant": "#494453",
            "outline": "#686272",
            "outline_variant": "#958fa0",
            "footer": "#958fa0",
            "quote_bg": "#e7dff2",
        }

    async def _build_card_data(self, data: dict) -> dict:
        article = data.get("article")
        if article and article.get("rest_id"):
            try:
                full_text = await self.twitter.get_full_article_text(data["id"])
                if full_text:
                    article["full_text"] = full_text
                    data["article_full_text"] = full_text
            except Exception as e:
                logger.warning(f"Full article fetch failed: {e}")

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
            translated_text = await asyncio.wait_for(
                self._translate_text(data), timeout=120
            )
        except asyncio.TimeoutError:
            logger.warning("Text translation timed out, using original text")
            translated_text = data.get("text", "(翻译超时)")
        except Exception as e:
            logger.warning(f"Translation failed: {e}")
            translated_text = data.get("text", "(翻译失败)")

        # 提取引用推文数据用于卡片
        quoted = data.get("quoted_tweet", {})
        quoted_user = quoted.get("user", {}) if quoted else {}
        quoted_media = quoted.get("media", []) if quoted else []
        quoted_thumbnails = [m.get("media_url", "") for m in quoted_media[:2] if m.get("media_url", "")]
        q_user_name = quoted_user.get("name", "")
        q_screen_name = quoted_user.get("screen_name", "")
        q_avatar = quoted_user.get("avatar_url", "")

        # 引用推文的文章数据
        q_article_data = quoted.get("article", {}) if quoted else {}
        q_article_title = q_article_data.get("title", "") if q_article_data else ""
        q_article_text = q_article_data.get("full_text", "") if q_article_data else ""
        q_article_preview = q_article_data.get("preview_text", "") if q_article_data else ""

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
            text_no_urls = text_no_urls.replace(u.get("url", ""), "").replace(u.get("expanded_url", ""), "").strip()
        is_link_only = len(text_no_urls.strip()) < 5
        has_article = bool(data.get("article"))
        if is_link_only or has_article:
            images_for_translate = []
        else:
            images_for_translate = images
        if images_for_translate:
            try:
                image_translations = await asyncio.wait_for(
                    self._translate_images(data), timeout=120
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
                thumbnail_urls.append(poster)

        import os as _os
        tmpl_path = _os.path.join(_os.path.dirname(__file__), "templates", "tweet_card.html")
        with open(tmpl_path, "r", encoding="utf-8") as f:
            template = f.read()

        # 图片译文合并到文字译文末尾
        if image_translations:
            translated_text = f"{translated_text}\n\n{image_translations}"

        seed_color = await self._extract_seed_color(data["user"]["avatar_url"])
        palette = self._generate_md3_palette(seed_color)
        card_data = {
            "user_name": data["user"]["name"],
            "screen_name": data["user"]["screen_name"],
            "user_id": data["user"]["id"],
            "avatar_url": data["user"]["avatar_url"],
            "created_at_str": data["created_at_datetime"].strftime("%b %d, %Y · %H:%M UTC")
            if hasattr(data["created_at_datetime"], "strftime")
            else str(data["created_at"]),
            "article_title": article.get("title", "") if article else "",
            "article_cover_url": article.get("cover_url", "") if article else "",
            "article_text": data.get("article_full_text") or (article.get("full_text", "") if article else ""),
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
        }

        card_img_url = await self._render_card(template, card_data)

        return {
            "card_img_url": card_img_url,
            "translated_text": translated_text,
            "images": images,
            "gifs": gifs,
            "videos": videos,
            "screen_name": data["user"]["screen_name"],
            "user_name": data["user"]["name"],
            "user_id": data["user"]["id"],
            "tweet_url": f"https://x.com/{data['user']['screen_name']}/status/{data['id']}",
        }

    async def _dump_render_debug(self, html: str, card_data: dict, png_path: str):
        debug_dir = os.path.join(
            getattr(self.context, "astrbot_root", os.getcwd()),
            "data", "config", "debug_render"
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
        """本地 Playwright 渲染 HTML → PNG，返回文件路径。"""
        import tempfile, os as _os
        from playwright.async_api import async_playwright

        html_path = _os.path.join(tempfile.gettempdir(), f"astrbot_twitter_{id(self)}.html")
        png_path = _os.path.join(tempfile.gettempdir(), f"astrbot_twitter_{id(self)}.png")
        try:
            from jinja2 import Template
            html = Template(template).render(card_data)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                ctx = await browser.new_context(device_scale_factor=2)
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 620, "height": 100})
                await page.goto(f"file:///{html_path.replace(chr(92), '/')}", wait_until="networkidle")
                await page.wait_for_timeout(2000)
                h = await page.evaluate("document.body.scrollHeight")
                await page.set_viewport_size({"width": 620, "height": h})
                await page.wait_for_timeout(500)
                await page.screenshot(path=png_path, full_page=True)
                await browser.close()
            await self._dump_render_debug(html, card_data, png_path)
            return png_path
        except Exception as e:
            logger.error(f"Local card render failed: {e}")
            try:
                card_img_url = await self.html_render(template, card_data,
                    options={"type": "png", "full_page": True, "timeout": 15000})
                return card_img_url
            except Exception as e2:
                logger.error(f"Remote card render also failed: {e2}")
                return ""
        finally:
            try:
                if _os.path.exists(html_path):
                    _os.remove(html_path)
            except Exception:
                pass

    async def _process_and_push(self, data: dict, target_sessions: list):
        if not target_sessions:
            return

        info = await self._build_card_data(data)

        for session_umo in target_sessions:
            try:
                # 主消息：卡片 + 推主注明
                chain = MessageChain()
                if info["card_img_url"]:
                    url = info["card_img_url"]
                    if url.startswith("http"):
                        chain.chain.append(CompImage.fromURL(url))
                    else:
                        chain.chain.append(CompImage.fromFileSystem(url))
                    chain.message(f"\n📢 @{info['screen_name']}\n{info.get('tweet_url', '')}")
                elif info["translated_text"]:
                    chain.message(f"📢 @{info['screen_name']}\n{info.get('tweet_url', '')}\n\n{info['translated_text'][:500]}")
                else:
                    chain.message(f"📢 @{info['screen_name']} 新推文\n{info.get('tweet_url', '')}")

                if chain.chain:
                    logger.info(f"[Push] Card to {session_umo} ({len(chain.chain)} components)")
                    await self.context.send_message(session_umo, chain)

                # 图片合并到一条群合并转发消息
                from astrbot.api.message_components import Node, Plain
                img_contents = []
                for img in info.get("images", []):
                    iurl = img.get("media_url", "")
                    if iurl:
                        img_contents.append(CompImage.fromURL(iurl))
                if img_contents:
                    node = Node(
                        uin="0",
                        name=info.get("user_name", info["screen_name"]),
                        content=img_contents,
                    )
                    fwd_chain = MessageChain()
                    fwd_chain.chain.append(node)
                    logger.info(f"[Push] Images forward to {session_umo}")
                    await self.context.send_message(session_umo, fwd_chain)
                    await asyncio.sleep(0.5)

                # GIF/视频直接发送
                for gif in info.get("gifs", []):
                    gurl = gif.get("video_url", gif.get("media_url", ""))
                    if gurl:
                        gif_chain = MessageChain()
                        gif_chain.chain.append(CompVideo.fromURL(gurl))
                        logger.info(f"[Push] GIF to {session_umo}")
                        await self.context.send_message(session_umo, gif_chain)
                        await asyncio.sleep(0.5)
                for vid in info.get("videos", []):
                    vurl = vid.get("video_url", vid.get("media_url", ""))
                    if vurl:
                        vid_chain = MessageChain()
                        vid_chain.chain.append(CompVideo.fromURL(vurl))
                        logger.info(f"[Push] Video to {session_umo}")
                        await self.context.send_message(session_umo, vid_chain)
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"[Push] Failed to push to {session_umo}: {e}")

    async def _get_provider_id(self) -> str:
        pid = self.config.get("text_translate_provider", "")
        if not pid and self.monitored_sessions:
            try:
                pid = await self.context.get_current_chat_provider_id(
                    umo=list(self.monitored_sessions)[0]
                )
            except Exception:
                pass
        if isinstance(pid, str):
            return pid
        if isinstance(pid, dict):
            return pid.get("id", "")
        return str(pid) if pid else ""

    async def _translate_text(self, data: dict) -> str:
        text = data.get("text", "")
        article = data.get("article")
        if article:
            full = data.get("article_full_text") or article.get("full_text") or article.get("preview_text", "")
            text = f"{article.get('title', '')}\n\n{full}"

        # 追加引用推文
        quoted = data.get("quoted_tweet", {})
        quoted_text = quoted.get("text", "")
        quoted_user = quoted.get("user", {})
        if quoted_text:
            q_name = quoted_user.get("name", "")
            text = f"{text}\n\n[引用 @{quoted_user.get('screen_name', '')} ({q_name})]:\n{quoted_text}"

        if not text or not text.strip():
            return "(无文字内容)"

        # 翻译前过滤掉链接，防止非多模态模型误以为要它看图
        import re as _re
        clean_text = _re.sub(r'https?://\S+', '', text).strip()
        if not clean_text:
            clean_text = text

        target_lang = self.config.get("translation_language", "中文")
        provider_id = await self._get_provider_id()
        if not provider_id:
            provider_id = self.config.get("text_translate_provider", "")

        if provider_id:
            try:
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=f"请将以下内容翻译成{target_lang}，只返回翻译结果:\n\n{clean_text}",
                )
                if llm_resp and llm_resp.completion_text:
                    return llm_resp.completion_text
            except Exception as e:
                logger.warning(f"LLM translate failed: {e}")
        return text

    async def _translate_images(self, data: dict) -> str:
        images, _, _ = TwitterClient.extract_tweet_media(data)
        if not images:
            return ""

        mode = self.config.get("image_translate_mode", "multimodal")
        provider_id = self.config.get("image_translate_provider", "")
        target_lang = self.config.get("translation_language", "中文")
        if not provider_id:
            provider_id = self.config.get("text_translate_provider", "")

        if not provider_id:
            return "(未配置翻译提供商)"

        translations = []
        for img in images[:3]:
            img_url = img.get("media_url", "")
            if not img_url:
                continue

            if mode == "multimodal":
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=(
                        f"理解图片内容并翻译成{target_lang}，自行组织格式使"
                        f"用户能简单直接理解。尽量简短，不要使文本量过大影响阅读。"
                        f"如果图片中没有文字输出'(无文字)'。"
                    ),
                    image_urls=[img_url],
                )
                result = llm_resp.completion_text or ""
                if result and "(无文字)" not in result:
                    translations.append(result)
            elif mode == "text_extraction":
                text_in_image = await self._ocr_image(img_url)
                if text_in_image:
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=f"将以下内容翻译成{target_lang}:\n\n{text_in_image}",
                    )
                    result = llm_resp.completion_text or ""
                    if result:
                        translations.append(result)

        if translations:
            return " | ".join(translations)
        return ""

    async def _ocr_image(self, img_url: str) -> str:
        try:
            from easyocr import Reader
            import httpx

            reader = Reader(["ch_sim", "en"], gpu=False)
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(img_url)
                if r.status_code == 200:
                    import tempfile

                    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                    tmp.write(r.content)
                    tmp.close()
                    results = reader.readtext(tmp.name)
                    os.unlink(tmp.name)
                    return " ".join(txt for _, txt, _ in results)
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"OCR failed: {e}")
        return ""
