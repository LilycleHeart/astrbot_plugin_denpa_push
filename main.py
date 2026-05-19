import asyncio
import json
import os
import re
from datetime import datetime, timezone

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image as CompImage, Video as CompVideo
from astrbot.api.star import Context, Star, register

from .twitter_client import TwitterClient

DATA_DIR = "data/config"
DATA_FILE = "astrbot_plugin_denpa_push_data.json"


@register(
    "astrbot_plugin_denpa_push",
    "astrbot_user",
    "電波プッシュ · 捕捉 X 推文的電波，自動翻譯並生成卡片推送給你",
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
        self._running = False
        self._data_path = self._get_data_path()

    def _get_data_path(self):
        root = getattr(self.context, "astrbot_root", os.getcwd())
        return os.path.join(root, DATA_DIR, DATA_FILE)

    async def initialize(self):
        import importlib.util

        if importlib.util.find_spec("twikit") is None:
            logger.error("twikit 未安装，请确保 requirements.txt 中的依赖已被安装")
        self._apply_twitter_credentials()
        self._load_data()
        auto_monitor = True
        if self.subscriptions and auto_monitor:
            self._start_monitor()
        logger.info("DenpaPush plugin initialized")

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
                logger.info(
                    f"Loaded {len(self.subscriptions)} sessions with {total} tracked users"
                )
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
            yield event.plain_result(
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
            for result in await self._cmd_push(event, parts[2]):
                yield result
        elif sub == "monitor":
            yield await self._cmd_monitor(event)
        else:
            yield event.plain_result(f"未知子指令: {sub}")

    @filter.llm_tool(name="twitter_add")
    async def twitter_add(self, event: AstrMessageEvent, usernames: list):
        """当用户说「关注」「订阅」「跟踪」某个推特账号时使用此工具。会开始监控该用户的推文并自动推送新内容。

        Args:
            usernames(array[string]): 要关注的用户名，如 ["ApexLiveComms"]
        """
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
        for r in await self._cmd_push(event, url):
            yield r

    @filter.llm_tool(name="twitter_list")
    async def twitter_list(self, event: AstrMessageEvent):
        """列出当前群聊已关注的 Twitter 用户。"""
        umo = event.unified_msg_origin
        session_users = self.subscriptions.get(umo, {})
        lines = ["已关注用户:"]
        for name in session_users:
            lines.append(f"  @{name}")
        yield event.plain_result("\n".join(lines) if len(lines) > 1 else "暂无关注用户")

    @filter.llm_tool(name="denpa_push")
    async def denpa_push(self, event: AstrMessageEvent):
        """开启或关闭当前会话的自动推送。"""
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
            return event.plain_result(
                f"本群已关注 @{username}（{user.name}），开始跟踪"
            )
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
            lines.append(
                f"  @{name}  (最后ID: {info.get('last_tweet_id', 'N/A')[:12]}...)"
            )
        return event.plain_result("\n".join(lines))

    async def _cmd_push(self, event: AstrMessageEvent, url: str):
        results = []
        m = re.search(r"(?:twitter\.com|x\.com)/(\w+)/status/(\d+)", url)
        if not m:
            results.append(
                event.plain_result(
                    "无效的推文链接，格式: https://x.com/username/status/123456"
                )
            )
            return results
        tweet_id = m.group(2)
        try:
            results.append(event.plain_result("正在获取推文..."))
            tweet = await self.twitter.get_tweet_by_id(tweet_id)
            data = TwitterClient.extract_tweet_data(tweet)
            info = await self._build_card_data(data)

            # 1. 卡片 PNG 直接发送
            if info["card_img_url"]:
                results.append(event.image_result(info["card_img_url"]))
            results.append(
                event.plain_result(
                    f"📢 @{info['screen_name']}\n{info.get('tweet_url', '')}"
                )
            )

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
                results.append(event.chain_result([node]))
            for gif in info.get("gifs", []):
                vurl = gif.get("video_url", gif.get("media_url", ""))
                if vurl:
                    results.append(event.chain_result([CompVideo.fromURL(vurl)]))
            for vid in info.get("videos", []):
                vurl = vid.get("video_url", vid.get("media_url", ""))
                if vurl:
                    results.append(event.chain_result([CompVideo.fromURL(vurl)]))
        except Exception as e:
            logger.error(f"Failed to push tweet {tweet_id}: {e}")
            results.append(event.plain_result(f"推送失败: {str(e)[:100]}"))
        return results

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

                    # Highest last_tweet_id across sessions tracking this user
                    last_id = "0"
                    for sess_umo in user_sessions.get(username, []):
                        sess_users = self.subscriptions.get(sess_umo, {})
                        uid = sess_users.get(username, {}).get("last_tweet_id", "0")
                        if uid > last_id:
                            last_id = uid

                    new_tweets = [t for t in tweets if t.id > last_id]

                    if new_tweets:
                        target_sessions = [
                            s
                            for s in user_sessions.get(username, [])
                            if s in self.monitored_sessions
                        ]
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
                                sess_users[username]["last_checked_at"] = datetime.now(
                                    timezone.utc
                                ).isoformat()
                        self._save_data()
                        logger.info(
                            f"[Monitor] {username}: last_id updated to {max_id[:15]}.."
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
                        break
                    logger.error(f"[Monitor] Error for {username}: {e}")

            await asyncio.sleep(interval)

    # NetEase 预设色板（来自 material-you-theme-netease）
    SCHEME_PRESETS = {
        "dark-blue": {
            "primary": (189, 230, 251),
            "bg": (30, 37, 41),
            "bg-darken": (23, 29, 32),
        },
        "dark-gray": {
            "primary": (255, 255, 255),
            "bg": (32, 32, 32),
            "bg-darken": (25, 25, 25),
        },
        "dark-green": {
            "primary": (183, 241, 222),
            "bg": (26, 36, 33),
            "bg-darken": (21, 28, 25),
        },
        "dark-orange": {
            "primary": (255, 200, 182),
            "bg": (39, 30, 27),
            "bg-darken": (30, 23, 21),
        },
        "dark-purple": {
            "primary": (216, 196, 241),
            "bg": (34, 31, 38),
            "bg-darken": (26, 24, 30),
        },
        "dark-red": {
            "primary": (253, 180, 180),
            "bg": (39, 27, 27),
            "bg-darken": (30, 21, 21),
        },
        "dark-pink": {
            "primary": (255, 217, 228),
            "bg": (54, 41, 41),
            "bg-darken": (33, 26, 26),
        },
        "dark-rose-pine": {
            "primary": (235, 188, 186),
            "secondary": (224, 222, 244),
            "bg": (35, 33, 54),
            "bg-darken": (57, 53, 82),
        },
        "tokyo-night": {
            "primary": (181, 185, 214),
            "bg": (36, 38, 56),
            "bg-darken": (28, 29, 43),
        },
        "one-dark-blue": {
            "primary": (113, 189, 242),
            "secondary": (171, 178, 191),
            "bg": (40, 44, 52),
            "bg-darken": (33, 37, 43),
        },
        "one-dark-green": {
            "primary": (167, 203, 139),
            "secondary": (171, 178, 191),
            "bg": (40, 44, 52),
            "bg-darken": (33, 37, 43),
        },
        "one-dark-cyan": {
            "primary": (101, 193, 205),
            "secondary": (171, 178, 191),
            "bg": (40, 44, 52),
            "bg-darken": (33, 37, 43),
        },
        "one-dark-red": {
            "primary": (231, 130, 135),
            "secondary": (171, 178, 191),
            "bg": (40, 44, 52),
            "bg-darken": (33, 37, 43),
        },
        "one-dark-pink": {
            "primary": (255, 121, 198),
            "secondary": (171, 178, 191),
            "bg": (40, 44, 52),
            "bg-darken": (33, 37, 43),
        },
        "one-dark-yellow": {
            "primary": (218, 170, 120),
            "secondary": (171, 178, 191),
            "bg": (40, 44, 52),
            "bg-darken": (33, 37, 43),
        },
        "one-dark-purple": {
            "primary": (209, 144, 227),
            "secondary": (171, 178, 191),
            "bg": (40, 44, 52),
            "bg-darken": (33, 37, 43),
        },
        "osu-pink": {
            "primary": (255, 102, 171),
            "secondary": (240, 219, 228),
            "bg": (42, 34, 38),
            "bg-darken": (28, 23, 25),
        },
        "osu-purple": {
            "primary": (140, 102, 255),
            "secondary": (224, 219, 240),
            "bg": (36, 34, 42),
            "bg-darken": (24, 23, 28),
        },
        "osu-blue": {
            "primary": (102, 204, 255),
            "secondary": (219, 233, 240),
            "bg": (34, 40, 42),
            "bg-darken": (23, 26, 28),
        },
        "osu-green": {
            "primary": (115, 255, 102),
            "secondary": (221, 240, 219),
            "bg": (35, 42, 34),
            "bg-darken": (23, 28, 23),
        },
        "osu-orange": {
            "primary": (255, 153, 102),
            "secondary": (240, 226, 219),
            "bg": (42, 37, 34),
            "bg-darken": (28, 25, 23),
        },
        "osu-yellow": {
            "primary": (255, 217, 102),
            "secondary": (240, 235, 219),
            "bg": (42, 40, 34),
            "bg-darken": (28, 27, 23),
        },
        "cyberpunk": {
            "primary": (252, 236, 12),
            "bg": (19, 99, 119),
            "bg-darken": (8, 74, 90),
        },
        "matrix": {"primary": (0, 255, 65), "bg": (6, 2, 8), "bg-darken": (0, 22, 0)},
        "dracula-mint": {
            "primary": (47, 222, 182),
            "secondary": (226, 226, 228),
            "bg": (41, 45, 62),
            "bg-darken": (33, 36, 50),
        },
        "discord": {
            "primary": (88, 101, 242),
            "secondary": (255, 255, 255),
            "bg": (54, 57, 63),
            "bg-darken": (47, 49, 54),
        },
        "pure-black": {
            "primary": (240, 240, 240),
            "bg": (0, 0, 0),
            "bg-darken": (20, 20, 20),
        },
        "light-blue": {
            "primary": (34, 197, 253),
            "secondary": (18, 51, 84),
            "bg": (245, 247, 250),
            "bg-darken": (255, 255, 255),
            "light": True,
        },
        "light-gray": {
            "primary": (97, 113, 124),
            "secondary": (41, 41, 42),
            "bg": (247, 247, 247),
            "bg-darken": (255, 255, 255),
            "light": True,
        },
        "light-green": {
            "primary": (42, 225, 142),
            "secondary": (25, 72, 62),
            "bg": (246, 249, 249),
            "bg-darken": (229, 236, 235),
            "light": True,
        },
        "light-orange": {
            "primary": (255, 130, 101),
            "secondary": (86, 59, 37),
            "bg": (250, 248, 247),
            "bg-darken": (255, 255, 255),
            "light": True,
        },
        "light-purple": {
            "primary": (159, 116, 231),
            "secondary": (64, 43, 77),
            "bg": (249, 247, 249),
            "bg-darken": (255, 255, 255),
            "light": True,
        },
        "light-red": {
            "primary": (255, 89, 102),
            "secondary": (87, 41, 32),
            "bg": (250, 247, 246),
            "bg-darken": (255, 255, 255),
            "light": True,
        },
        "light-pink": {
            "primary": (255, 130, 171),
            "secondary": (99, 10, 39),
            "bg": (250, 247, 246),
            "bg-darken": (255, 255, 255),
            "light": True,
        },
        "light-rose-pine": {
            "primary": (215, 130, 126),
            "secondary": (87, 82, 121),
            "bg": (242, 233, 225),
            "bg-darken": (250, 244, 237),
            "light": True,
        },
        "cerulean": {
            "primary": (66, 141, 185),
            "secondary": (33, 33, 33),
            "bg": (243, 248, 251),
            "bg-darken": (223, 238, 243),
            "light": True,
        },
        "wechat": {
            "primary": (7, 193, 96),
            "secondary": (34, 34, 34),
            "bg": (245, 245, 245),
            "bg-darken": (218, 218, 218),
            "light": True,
        },
        "tim": {
            "primary": (29, 110, 255),
            "secondary": (34, 34, 34),
            "bg": (244, 246, 248),
            "bg-darken": (255, 255, 255),
            "light": True,
        },
        "Cloud & Moon": {
            "primary": (93, 131, 194),
            "secondary": (87, 111, 147),
            "bg": (237, 241, 248),
            "bg-darken": (247, 250, 245),
            "light": True,
        },
    }

    @staticmethod
    def _rgb_to_lab(rgb):
        r, g, b = [x / 255.0 for x in rgb]
        r = r / 12.92 if r <= 0.03928 else ((r + 0.055) / 1.055) ** 2.4
        g = g / 12.92 if g <= 0.03928 else ((g + 0.055) / 1.055) ** 2.4
        b = b / 12.92 if b <= 0.03928 else ((b + 0.055) / 1.055) ** 2.4
        x = (r * 0.4124 + g * 0.3576 + b * 0.1805) * 100
        y = (r * 0.2126 + g * 0.7152 + b * 0.0722) * 100
        z = (r * 0.0193 + g * 0.1192 + b * 0.9505) * 100

        def f(t):
            return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

        return (
            116 * f(y / 100) - 16,
            500 * (f(x / 95.047) - f(y / 100)),
            200 * (f(y / 100) - f(z / 108.883)),
        )

    @staticmethod
    def _lab_distance(lab1, lab2):
        return (
            (lab1[0] - lab2[0]) ** 2
            + (lab1[1] - lab2[1]) ** 2
            + (lab1[2] - lab2[2]) ** 2
        ) ** 0.5

    @staticmethod
    def _derive_preset_palette(primary, secondary, bg, bg_darken, is_dark):
        if not secondary:

            def lum(rgb):
                r, g, b = [x / 255.0 for x in rgb]
                return 0.2126 * r + 0.7152 * g + 0.0722 * b

            pl = lum(primary)
            on_p = (255, 255, 255) if pl < 0.55 else (30, 30, 30)
            secondary = (
                on_p
                if is_dark
                else tuple(int(on_p[i] * 0.6 + bg[i] * 0.4) for i in range(3))
            )

        def hx(rgb):
            return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

        return {
            "primary": hx(primary),
            "secondary": hx(secondary),
            "bg": hx(bg),
            "bg-darken": hx(bg_darken),
            "primary_rgb": f"{primary[0]},{primary[1]},{primary[2]}",
            "secondary_rgb": f"{secondary[0]},{secondary[1]},{secondary[2]}",
            "bg_rgb": f"{bg[0]},{bg[1]},{bg[2]}",
            "bg_darken_rgb": f"{bg_darken[0]},{bg_darken[1]},{bg_darken[2]}",
        }

    async def _extract_seed_color(self, avatar_url: str):
        try:
            import httpx
            from PIL import Image
            import io

            proxy = self.config.get("proxy", None)
            async with httpx.AsyncClient(
                proxy=proxy if proxy else None, timeout=10
            ) as _c:
                _r = await _c.get(avatar_url)
                _r.raise_for_status()
                _img = Image.open(io.BytesIO(_r.content)).convert("RGBA")
                _img = _img.resize((1, 1), resample=Image.Resampling.LANCZOS)
                _pr, _pg, _pb, _pa = _img.getpixel((0, 0))
                logger.debug(f"Seed color extracted: RGB=({_pr},{_pg},{_pb})")
                return (_pr, _pg, _pb)
        except Exception as e:
            logger.warning(f"Seed color extraction failed: {e}")
            return (103, 80, 164)

    def _generate_palette(self, seed_rgb):
        h = int(__import__("datetime").datetime.now(
            __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))
        ).strftime("%H"))
        is_dark = h >= 18 or h < 6

        try:
            seed_lab = self._rgb_to_lab(seed_rgb)
            best_name, best_dist = None, float("inf")
            presets = self.SCHEME_PRESETS

            for name, p in presets.items():
                is_light = p.get("light", False)
                if is_dark and is_light:
                    continue
                if not is_dark and not is_light:
                    continue
                p_lab = self._rgb_to_lab(p["primary"])
                dist = self._lab_distance(seed_lab, p_lab)
                if dist < best_dist:
                    best_dist = dist
                    best_name = name

            if not best_name:
                best_name = "dark-blue" if is_dark else "light-blue"

            preset = presets[best_name]
            primary = preset["primary"]
            secondary = preset.get("secondary", None)
            bg = preset["bg"]
            bg_darken = preset["bg-darken"]

            palette = self._derive_preset_palette(
                primary, secondary, bg, bg_darken, is_dark
            )
            logger.debug(
                f"Matched preset '{best_name}' (dist={best_dist:.1f}, dark={is_dark}): {json.dumps(palette)}"
            )
            return palette, is_dark
        except Exception as e:
            logger.warning(f"Preset palette failed: {e}")

        # Final hardcoded fallback
        if is_dark:
            return {
                "primary": "#d0bcff",
                "primary_rgb": "208,188,255",
                "secondary": "#cac4d0",
                "secondary_rgb": "202,196,208",
                "bg": "#1c1b1f",
                "bg_rgb": "28,27,31",
                "bg-darken": "#141318",
                "bg_darken_rgb": "20,19,24",
            }, is_dark
        return {
            "primary": "#5700d2",
            "primary_rgb": "87,0,210",
            "secondary": "#554262",
            "secondary_rgb": "85,66,98",
            "bg": "#fdf7ff",
            "bg_rgb": "253,247,255",
            "bg-darken": "#efe5ff",
            "bg_darken_rgb": "239,229,255",
        }, is_dark

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
        quoted_thumbnails = [
            m.get("media_url", "") for m in quoted_media[:2] if m.get("media_url", "")
        ]
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

        # 读取卡片 HTML 模板
        tmpl_path = _os.path.join(
            _os.path.dirname(__file__), "templates", "tweet_card.html"
        )
        try:
            with open(tmpl_path, "r", encoding="utf-8") as f:
                template = f.read()
        except FileNotFoundError:
            logger.error(f"Card template not found: {tmpl_path}")
            return {
                "card_img_urls": [],
                "card_img_url": "",
                "translated_text": "",
                "images": [],
                "gifs": [],
                "videos": [],
                "screen_name": "",
                "user_name": "",
                "user_id": "",
                "tweet_url": "",
            }

        # 图片译文合并到文字译文末尾
        if image_translations:
            translated_text = f"{translated_text}\n\n{image_translations}"

        # 从头像取色 → 匹配 NetEase 预设色板
        seed_rgb = await self._extract_seed_color(data["user"]["avatar_url"])
        palette, is_dark = self._generate_palette(seed_rgb)
        card_data = {
            "user_name": data["user"]["name"],
            "screen_name": data["user"]["screen_name"],
            "user_id": data["user"]["id"],
            "avatar_url": data["user"]["avatar_url"],
            "created_at_str": data["created_at_datetime"].strftime(
                "%b %d, %Y · %H:%M UTC"
            )
            if hasattr(data["created_at_datetime"], "strftime")
            else str(data["created_at"]),
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
        }

        # 长文章分块渲染
        article_text = data.get("article_full_text") or (
            article.get("full_text", "") if article else ""
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
            chunks = split_into_chunks(article_text)
            t_chunks = split_into_chunks(translated_text)
            min_len = min(len(chunks), len(t_chunks))
            chunks = chunks[:min_len]
            t_chunks = t_chunks[:min_len]
            card_img_urls = []
            for i, (chunk, t_chunk) in enumerate(zip(chunks, t_chunks)):
                sub = dict(card_data)
                sub["article_title"] = (
                    card_data["article_title"]
                    if i == 0
                    else f"(续 {i + 1}/{len(chunks)})"
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
            "tweet_url": f"https://x.com/{data['user']['screen_name']}/status/{data['id']}",
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
        """本地 Playwright 渲染 HTML → PNG，返回文件路径。"""
        import tempfile
        import os as _os
        from playwright.async_api import async_playwright

        html_path = _os.path.join(
            tempfile.gettempdir(), f"astrbot_twitter_{id(self)}.html"
        )
        png_path = _os.path.join(
            tempfile.gettempdir(), f"astrbot_twitter_{id(self)}.png"
        )
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
                await page.goto(
                    f"file:///{html_path.replace(chr(92), '/')}",
                    wait_until="networkidle",
                )
                await page.wait_for_timeout(2000)
                h = await page.evaluate("document.body.scrollHeight")
                await page.set_viewport_size({"width": 620, "height": h})
                await page.wait_for_timeout(500)
                await page.screenshot(
                    path=png_path, full_page=True, omit_background=True
                )
                await browser.close()
            await self._dump_render_debug(html, card_data, png_path)
            return png_path
        except Exception as e:
            logger.error(f"Local card render failed: {e}")
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
            try:
                if _os.path.exists(html_path):
                    _os.remove(html_path)
            except Exception:
                pass

    async def _process_and_push(self, data: dict, target_sessions: list):
        """构建卡片并推送到所有目标会话。每会话独立 try 防止一个失败影响其他。"""
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
                    chain.message(
                        f"\n📢 @{info['screen_name']}\n{info.get('tweet_url', '')}"
                    )
                elif info["translated_text"]:
                    chain.message(
                        f"📢 @{info['screen_name']}\n{info.get('tweet_url', '')}\n\n{info['translated_text'][:500]}"
                    )
                else:
                    chain.message(
                        f"📢 @{info['screen_name']} 新推文\n{info.get('tweet_url', '')}"
                    )

                if chain.chain:
                    logger.info(
                        f"[Push] Card to {session_umo} ({len(chain.chain)} components)"
                    )
                    await self.context.send_message(session_umo, chain)

                # 图片合并到一条群合并转发消息
                from astrbot.api.message_components import Node

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
        """翻译推文正文（含文章标题、引用推文），过滤链接后调用 LLM 提供商。"""
        text = data.get("text", "")
        article = data.get("article")
        if article:
            full = (
                data.get("article_full_text")
                or article.get("full_text")
                or article.get("preview_text", "")
            )
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

        clean_text = _re.sub(r"https?://\S+", "", text).strip()
        if not clean_text:
            clean_text = text

        # 截断过长的文章正文，防止 LLM 调用超时
        if len(clean_text) > 4000:
            clean_text = clean_text[:4000] + "\n\n...(截断)"

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
        """翻译推文图片中的文字。支持 multimodal（多模态）和 text_extraction（OCR+LLM）两种模式。"""
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

            try:
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
                    result = llm_resp.completion_text if llm_resp else ""
                    if result and "(无文字)" not in result:
                        translations.append(result)
                elif mode == "text_extraction":
                    text_in_image = await self._ocr_image(img_url)
                    if text_in_image:
                        llm_resp = await self.context.llm_generate(
                            chat_provider_id=provider_id,
                            prompt=f"将以下内容翻译成{target_lang}:\n\n{text_in_image}",
                        )
                        result = llm_resp.completion_text if llm_resp else ""
                        if result:
                            translations.append(result)
            except Exception as e:
                logger.warning(f"Image translation failed for {img_url[:40]}: {e}")

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
