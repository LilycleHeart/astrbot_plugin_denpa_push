import asyncio
import json
from typing import Optional

import httpx
from twikit import Client
from astrbot.api import logger

# TweetResultByRestId 的公开 GraphQL query id / 固定 Bearer(与 twikit 一致的 Web 端常量)
_ARTICLE_QUERY_URL = (
    "https://x.com/i/api/graphql/Xl5pC_lBk_gcO2ItU39DQw/TweetResultByRestId"
)
_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# 单次 Twitter API 调用的默认超时(秒)。twikit 内部部分流式请求传的是
# timeout=None, 网络抖动时会永久挂起, 必须由调用侧兜住。
DEFAULT_API_TIMEOUT = 30.0


class TwitterClient:
    """twikit 的薄封装: 统一超时、并发闸门与共享连接池。

    并发问题背景(高并发下 AstrBot/NapCat 卡死):
      - 原实现每个实例一个 twikit Client, 且 AsyncClient 未设连接池上限,
        批量检查订阅时会同时开出大量连接;
      - 文章抓取每次调用都新建 httpx.AsyncClient, 不复用连接也不受上限约束,
        积压时形成连接风暴(且完全忽略 proxy 配置);
      - 所有 twikit 调用都没有超时, 一次挂起就把监控循环永久卡死。
    """

    def __init__(self):
        # 初始构造就带上超时与连接池上限: twikit 默认不设任何超时, 且内部存在
        # timeout=None 的流式请求, 一次网络挂起就会永久卡死监控循环。
        self.client = self._build_client(None)
        self._initialized = False
        self._auth_token = ""
        self._ct0 = ""
        self._proxy = None
        self._api_semaphore = None
        self._api_concurrency = 4
        self._lock = None
        self._lock_loop = None
        self._http = None
        self._http_loop = None
        # 被替换掉的连接池: 热改 proxy/凭证/loop 时丢弃会泄漏 FD, 统一在这里
        # 记着并由 close() 关闭
        self._stale_http = []

    @staticmethod
    def _build_client(proxy):
        return Client(
            "en-US",
            proxy=proxy,
            timeout=httpx.Timeout(DEFAULT_API_TIMEOUT, connect=10.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            # trust_env=False: 与媒体下载侧保持一致 —— "proxy 留空" 就是直连,
            # 不再被 HTTP_PROXY/HTTPS_PROXY 等环境变量悄悄接管。否则会出现
            # "Twitter 走环境代理、图片下载走直连" 这类难以排查的不一致行为。
            trust_env=False,
        )

    def _get_lock(self) -> asyncio.Lock:
        """跨事件循环安全的惰性锁(插件热重载会换 loop)。"""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _get_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._api_semaphore is None or getattr(
            self, "_sem_loop", None
        ) is not loop:
            self._api_semaphore = asyncio.Semaphore(max(1, self._api_concurrency))
            self._sem_loop = loop
        return self._api_semaphore

    def set_concurrency(self, limit: int):
        """设置 Twitter API 并发上限(配置热更新时调用)。"""
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 4
        if limit != self._api_concurrency:
            self._api_concurrency = limit
            self._api_semaphore = None  # 下次取用时按新值重建

    def set_proxy(self, proxy: Optional[str]):
        """设置/清除代理。变更后需要重新创建底层客户端。"""
        proxy = (proxy or "").strip() or None
        if proxy != self._proxy:
            self._proxy = proxy
            self._initialized = False
            self._discard_http()
            old_client = self.client
            try:
                self.client = self._build_client(proxy)
                # 旧 twikit client 的连接池不能直接丢弃, 否则每次热改代理
                # 都泄漏一个连接池(FD/socket)。记下来由 close() 统一关闭。
                self._retire_client(old_client)
            except Exception as e:
                logger.warning(f"[Twitter] Failed to rebuild client with proxy: {e}")

    def _discard_http(self) -> None:
        """丢弃共享 GraphQL client, 但先记下来以便真正关闭其连接池。"""
        old, self._http = self._http, None
        self._http_loop = None
        self._retire(old)

    def _retire(self, http) -> None:
        """把被替换掉的连接池排入待关闭队列(避免热改配置泄漏 FD/socket)。"""
        if http is None:
            return
        try:
            if not http.is_closed:
                self._stale_http.append(http)
        except Exception:
            pass

    def _retire_client(self, old) -> None:
        """把被替换掉的 twikit client 的连接池排入待关闭队列。"""
        try:
            self._retire(getattr(old, "http", None))
        except Exception:
            pass

    def set_credentials(self, auth_token: str, ct0: str = ""):
        changed = (auth_token, ct0) != (self._auth_token, self._ct0)
        self._auth_token = auth_token
        self._ct0 = ct0
        self._initialized = False
        if changed:
            # 共享的 GraphQL client 把 auth_token/ct0 固化在 cookie 与 header 里,
            # 凭证轮换后必须重建, 否则文章全文/引用抓取会持续 401/403
            self._discard_http()

    async def ensure_ready(self):
        if self._initialized:
            return
        if not self._auth_token:
            raise ValueError("Twitter auth_token not configured")
        async with self._get_lock():
            if self._initialized:
                return
            cookies = {"auth_token": self._auth_token}
            if self._ct0:
                cookies["ct0"] = self._ct0
            self.client.set_cookies(cookies)
            self._initialized = True
            logger.info("Twitter client initialized successfully")

    async def _call(self, coro, timeout: float = DEFAULT_API_TIMEOUT):
        """经并发闸门 + 超时保护地执行一次 twikit 调用。

        超时后 wait_for 会取消底层请求, 避免单次挂起拖死整条监控循环;
        并发闸门则防止批量账号检查同时打出过多请求触发限流/连接风暴。
        """
        async with self._get_semaphore():
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[Twitter] API call timed out after {timeout}s"
                )
                raise

    async def get_user_by_screen_name(self, screen_name: str):
        await self.ensure_ready()
        return await self._call(self.client.get_user_by_screen_name(screen_name))

    async def get_user_tweets(self, user_id: str, count: int = 20):
        await self.ensure_ready()
        return await self._call(
            self.client.get_user_tweets(user_id, "Tweets", count=count)
        )

    async def get_tweet_by_id(self, tweet_id: str):
        await self.ensure_ready()
        return await self._call(self.client.get_tweet_by_id(tweet_id))

    def _get_http(self) -> httpx.AsyncClient:
        """共享的 GraphQL 客户端(带代理、连接池上限与超时), 跨调用复用连接。"""
        loop = asyncio.get_running_loop()
        if self._http_loop is not None and self._http_loop is not loop:
            # 事件循环已更换(插件热重载): 旧 client 绑在已关闭的 loop 上,
            # 顺手排入待关闭队列, 由 close() 尽力回收
            self._discard_http()
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                proxy=self._proxy,
                timeout=httpx.Timeout(DEFAULT_API_TIMEOUT, connect=10.0),
                limits=httpx.Limits(
                    max_connections=8, max_keepalive_connections=4
                ),
                trust_env=False,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36",
                    "Authorization": f"Bearer {_BEARER}",
                    "X-Csrf-Token": self._ct0,
                    "Content-Type": "application/json",
                },
                cookies={"auth_token": self._auth_token, "ct0": self._ct0},
            )
            self._http_loop = loop
        return self._http

    @staticmethod
    def _tweet_result_params(tweet_id: str) -> dict:
        variables = json.dumps(
            {
                "tweetId": tweet_id,
                "withCommunity": False,
                "includePromotedContent": False,
                "withVoice": False,
            }
        )
        features = json.dumps(
            {
                "creator_subscriptions_tweet_preview_api_enabled": True,
                "communities_web_enable_tweet_community_results_fetch": True,
                "c9s_tweet_anatomy_moderator_badge_enabled": True,
                "articles_preview_enabled": True,
                "responsive_web_twitter_article_tweet_consumption_enabled": True,
                "longform_notetweets_consumption_enabled": True,
                "longform_notetweets_rich_text_read_enabled": True,
                "longform_notetweets_inline_media_enabled": True,
                "responsive_web_graphql_exclude_directive_enabled": True,
                "verified_phone_label_enabled": False,
            }
        )
        field_toggles = json.dumps(
            {
                "withArticleRichContentState": True,
                "withArticlePlainText": True,
                "withGrokAnalyze": False,
            }
        )
        return {
            "variables": variables,
            "features": features,
            "fieldToggles": field_toggles,
        }

    async def _fetch_tweet_result(self, tweet_id: str) -> dict:
        """调用 TweetResultByRestId 拿原始 result(文章全文与引用推文共用)。"""
        await self.ensure_ready()
        if not self._ct0:
            # 缺少 ct0 时该接口必然 403, 直接跳过省掉一次无谓请求
            return {}
        client = self._get_http()
        async with self._get_semaphore():
            r = await client.get(
                _ARTICLE_QUERY_URL,
                params=self._tweet_result_params(tweet_id),
            )
        if r.status_code != 200:
            logger.warning(f"TweetResultByRestId failed: {r.status_code}")
            return {}
        try:
            return (
                r.json()
                .get("data", {})
                .get("tweetResult", {})
                .get("result", {})
            ) or {}
        except Exception as e:
            logger.warning(f"TweetResultByRestId parse failed: {e}")
            return {}

    async def get_full_article_text(self, tweet_id: str) -> str:
        """Fetch full article text body via TweetResultByRestId withArticlePlainText=True."""
        result = await self._fetch_tweet_result(tweet_id)
        if not result:
            return ""

        # Full article text from content_state blocks -> HTML
        art_result = (
            result.get("article", {}).get("article_results", {}).get("result", {})
        )
        content_state = art_result.get("content_state", {})
        blocks = content_state.get("blocks", [])
        if blocks:
            entity_map = content_state.get("entityMap", [])
            entities = {}
            for entry in entity_map:
                if isinstance(entry, dict) and "key" in entry:
                    entities[str(entry["key"])] = entry["value"]

            def apply_entity_ranges(text, ranges):
                if not ranges:
                    return text
                chars = list(text)
                for er in sorted(
                    ranges, key=lambda x: x.get("offset", 0), reverse=True
                ):
                    key = str(er.get("key", ""))
                    ent = entities.get(key, {})
                    etype = ent.get("type", "")
                    offset = er.get("offset", 0)
                    length = er.get("length", 0)
                    if etype == "TWEMOJI":
                        url = ent.get("data", {}).get("url", "")
                        img = f'<img src="{url}" style="width:1.2em;height:1.2em;vertical-align:middle" alt="emoji"/>'
                        for i in range(offset, min(offset + length, len(chars))):
                            chars[i] = ""
                        chars[offset] = img
                return "".join(chars)

            html_parts = []
            in_list = False
            for b in blocks:
                btype = b.get("type", "unstyled")
                text = b.get("text", "")
                if not text:
                    continue
                text = apply_entity_ranges(text, b.get("entityRanges", []))
                if btype == "unstyled":
                    if in_list:
                        html_parts.append("</ul>")
                        in_list = False
                    html_parts.append(f"<p>{text}</p>")
                elif btype in ("unordered-list-item", "ordered-list-item"):
                    if not in_list:
                        html_parts.append("<ul>")
                        in_list = True
                    html_parts.append(f"<li>{text}</li>")
                elif btype.startswith("header-"):
                    if in_list:
                        html_parts.append("</ul>")
                        in_list = False
                    level = btype.replace("header-", "")
                    html_parts.append(f"<h{level}>{text}</h{level}>")
                elif btype == "blockquote":
                    if in_list:
                        html_parts.append("</ul>")
                        in_list = False
                    html_parts.append(f"<blockquote>{text}</blockquote>")
                else:
                    if in_list:
                        html_parts.append("</ul>")
                        in_list = False
                    html_parts.append(f"<p>{text}</p>")
            if in_list:
                html_parts.append("</ul>")

            if html_parts:
                return "\n\n".join(html_parts)

        note = (
            result.get("note_tweet", {})
            .get("note_tweet_results", {})
            .get("result", {})
        )
        if note:
            text = note.get("text", "")
            if text:
                return text
        legacy = result.get("legacy", {})
        if legacy.get("full_text"):
            return legacy["full_text"]
        return legacy.get("text", legacy.get("full_text", ""))

    async def close(self):
        """关闭共享 GraphQL 连接池与所有已替换掉的旧连接池。

        热改 proxy/凭证/事件循环会重建 client, 那些被替换掉的连接池也必须关闭,
        否则每次改配置都会泄漏 FD/socket(审查实测确认过)。
        """
        http, self._http = self._http, None
        self._http_loop = None
        targets = [http] if http is not None else []
        targets.extend(self._stale_http)
        targets.append(getattr(self.client, "http", None))
        self._stale_http = []
        # 去重并跳过已关闭的
        seen = set()
        for c in targets:
            if c is None or id(c) in seen:
                continue
            seen.add(id(c))
            try:
                if not c.is_closed:
                    await c.aclose()
            except Exception:
                pass

    @staticmethod
    def extract_tweet_data(tweet) -> dict:
        try:
            dt = tweet.created_at_datetime
        except Exception:
            dt = None
        data = {
            "id": tweet.id,
            "text": tweet.text or "",
            "full_text": getattr(tweet, "full_text", tweet.text or ""),
            "created_at": tweet.created_at,
            "created_at_datetime": dt,
            "user": {
                "id": tweet.user.id,
                "name": tweet.user.name,
                "screen_name": tweet.user.screen_name,
                "avatar_url": tweet.user.profile_image_url,
            },
            "in_reply_to": tweet.in_reply_to,
            "is_quote": tweet.is_quote_status,
            "retweet_count": tweet.retweet_count,
            "favorite_count": tweet.favorite_count,
            "reply_count": tweet.reply_count,
            "view_count": getattr(tweet, "view_count", 0),
            "has_card": getattr(tweet, "has_card", False),
            "lang": getattr(tweet, "lang", ""),
            "media": [],
            "article": None,
            "urls": [],
            "is_retweet": False,
            "retweeted_user": None,
        }

        # Resolve retweet/repost content: use the original tweet's text and media
        retweeted = getattr(tweet, "retweeted_tweet", None)
        if retweeted is not None:
            rt_text = getattr(retweeted, "full_text", retweeted.text or "")
            if rt_text and len(rt_text) > len(data["text"]):
                data["text"] = rt_text
                data["full_text"] = rt_text
            data["is_retweet"] = True
            data["retweeted_user"] = {
                "name": retweeted.user.name if retweeted.user else "",
                "screen_name": retweeted.user.screen_name if retweeted.user else "",
            }
            # Use the original tweet's media
            data["media"] = []
            if hasattr(retweeted, "media") and retweeted.media:
                for m in retweeted.media:
                    if isinstance(m, dict):
                        mtype = m.get("type", "unknown")
                        poster = m.get("media_url_https", "")
                        item = {
                            "type": mtype,
                            "media_url": poster,
                            "url": m.get("url", ""),
                            "expanded_url": m.get("expanded_url", ""),
                        }
                        if mtype in ("video", "animated_gif"):
                            vi = m.get("video_info", {})
                            variants = vi.get("variants", [])
                            best_url, best_bitrate = "", -1
                            for v in variants:
                                if v.get("content_type") == "video/mp4":
                                    br = v.get("bitrate", 0)
                                    if br > best_bitrate:
                                        best_bitrate = br
                                        best_url = v.get("url", "")
                            if best_url:
                                item["video_url"] = best_url
                        data["media"].append(item)
            # Also pull note_tweet/article from retweeted tweet's raw data
            rt_raw = getattr(retweeted, "_data", {})
            rt_note = (
                rt_raw.get("note_tweet", {})
                .get("note_tweet_results", {})
                .get("result", {})
            )
            rt_note_text = rt_note.get("text", "")
            if rt_note_text and len(rt_note_text) > len(data["text"]):
                data["text"] = rt_note_text
                data["full_text"] = rt_note_text
            rt_article = rt_raw.get("article", {})
            if rt_article:
                data["article"] = rt_article
        if not retweeted and hasattr(tweet, "media") and tweet.media:
            for m in tweet.media:
                if isinstance(m, dict):
                    mtype = m.get("type", "unknown")
                    poster = m.get("media_url_https", "")
                    item = {
                        "type": mtype,
                        "media_url": poster,
                        "url": m.get("url", ""),
                        "expanded_url": m.get("expanded_url", ""),
                    }
                    # Extract actual video URL for videos/GIFs
                    if mtype in ("video", "animated_gif"):
                        vi = m.get("video_info", {})
                        variants = vi.get("variants", [])
                        best_url = ""
                        best_bitrate = -1
                        for v in variants:
                            if v.get("content_type") == "video/mp4":
                                br = v.get("bitrate", 0)
                                if br > best_bitrate:
                                    best_bitrate = br
                                    best_url = v.get("url", "")
                        if best_url:
                            item["video_url"] = best_url
                    data["media"].append(item)

        raw = getattr(tweet, "_data", {})
        # 从原始数据获取 urls，避免 property 内部 KeyError
        raw_legacy = raw.get("legacy", {})
        raw_entities = raw_legacy.get("entities", {})
        data["urls"] = raw_entities.get("urls", [])
        # 用 raw_legacy.full_text 覆盖可能截断的 tweet.text
        raw_full = raw_legacy.get("full_text", "")
        if raw_full and len(raw_full) > len(data.get("text", "")):
            data["text"] = raw_full
            data["full_text"] = raw_full
        # NoteTweet 的全文在 note_tweet 字段，legacy.full_text 只有预览
        note_tweet = (
            raw.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
        )
        note_text = note_tweet.get("text", "")
        if note_text and len(note_text) > len(data["text"]):
            data["text"] = note_text
            data["full_text"] = note_text
        # 旧版 extended_tweet 回退
        ext_text = raw_legacy.get("extended_tweet", {}).get("full_text", "")
        if ext_text and len(ext_text) > len(data["text"]):
            data["text"] = ext_text
            data["full_text"] = ext_text
        article = raw.get("article", {})
        art_result = (
            article.get("article_results", {}).get("result", {}) if article else {}
        )
        if art_result:
            cover_media = art_result.get("cover_media", {}).get("media_info", {})
            data["article"] = {
                "title": art_result.get("title", ""),
                "preview_text": art_result.get("preview_text", ""),
                "cover_url": cover_media.get("original_img_url", ""),
                "rest_id": art_result.get("rest_id", ""),
            }

        # Extract quoted tweet if present in the raw data (available from TweetResultByRestId but not from twikit's get_tweet_by_id)
        quoted_raw = raw.get("quoted_status_result", {}).get("result", {})
        if quoted_raw:
            q_legacy = quoted_raw.get("legacy", {})
            q_core = (
                quoted_raw.get("core", {})
                .get("user_results", {})
                .get("result", {})
                .get("legacy", {})
            )
            q_media = []
            for m in q_legacy.get("extended_entities", {}).get("media", []):
                mtype = m.get("type", "unknown")
                poster = m.get("media_url_https", "")
                item = {
                    "type": mtype,
                    "media_url": poster,
                    "url": m.get("url", ""),
                    "expanded_url": m.get("expanded_url", ""),
                }
                if mtype in ("video", "animated_gif"):
                    vi = m.get("video_info", {})
                    variants = vi.get("variants", [])
                    best_url, best_bitrate = "", -1
                    for v in variants:
                        if v.get("content_type") == "video/mp4":
                            br = v.get("bitrate", 0)
                            if br > best_bitrate:
                                best_bitrate = br
                                best_url = v.get("url", "")
                    if best_url:
                        item["video_url"] = best_url
                q_media.append(item)
            data["quoted_tweet"] = {
                "id": quoted_raw.get("rest_id", ""),
                "text": q_legacy.get("full_text", ""),
                "user": {
                    "name": q_core.get("name", ""),
                    "screen_name": q_core.get("screen_name", ""),
                    "avatar_url": q_core.get("profile_image_url_https", ""),
                },
                "media": q_media,
            }

        if (
            not data.get("quoted_tweet")
            and tweet.is_quote_status
            and hasattr(tweet, "retweeted_tweet")
            and tweet.retweeted_tweet
        ):
            data["quoted_tweet"] = TwitterClient.extract_tweet_data(
                tweet.retweeted_tweet
            )

        return data

    @staticmethod
    def extract_tweet_media(tweet_data: dict):
        media = tweet_data.get("media", [])
        images = [m for m in media if m.get("type") == "photo"]
        gifs = [m for m in media if m.get("type") == "animated_gif"]
        videos = [m for m in media if m.get("type") == "video"]
        return images, gifs, videos

    async def fetch_quoted_tweet_data(self, tweet_id: str) -> dict:
        """Fetch quoted tweet data via TweetResultByRestId which includes quoted_status_result."""
        result = await self._fetch_tweet_result(tweet_id)
        if not result:
            return {}
        return TwitterClient._parse_quoted_from_raw_result(result)

    @staticmethod
    def _parse_quoted_from_raw_result(result: dict) -> dict:
        """Extract quoted_tweet dict from a raw TweetResultByRestId result."""
        quoted_raw = result.get("quoted_status_result", {}).get("result", {})
        if not quoted_raw:
            return {}
        q_legacy = quoted_raw.get("legacy", {})
        q_core = (
            quoted_raw.get("core", {})
            .get("user_results", {})
            .get("result", {})
            .get("legacy", {})
        )
        q_media = []
        for m in q_legacy.get("extended_entities", {}).get("media", []):
            mtype = m.get("type", "unknown")
            poster = m.get("media_url_https", "")
            item = {
                "type": mtype,
                "media_url": poster,
                "url": m.get("url", ""),
                "expanded_url": m.get("expanded_url", ""),
            }
            if mtype in ("video", "animated_gif"):
                vi = m.get("video_info", {})
                variants = vi.get("variants", [])
                best_url, best_bitrate = "", -1
                for v in variants:
                    if v.get("content_type") == "video/mp4":
                        br = v.get("bitrate", 0)
                        if br > best_bitrate:
                            best_bitrate = br
                            best_url = v.get("url", "")
                if best_url:
                    item["video_url"] = best_url
            q_media.append(item)
        quoted = {
            "id": quoted_raw.get("rest_id", ""),
            "text": q_legacy.get("full_text", ""),
            "user": {
                "name": q_core.get("name", ""),
                "screen_name": q_core.get("screen_name", ""),
                "avatar_url": q_core.get("profile_image_url_https", ""),
            },
            "media": q_media,
        }
        # Extract article metadata from quoted tweet
        q_art = quoted_raw.get("article", {})
        q_art_result = (
            q_art.get("article_results", {}).get("result", {}) if q_art else {}
        )
        if q_art_result:
            q_cover = q_art_result.get("cover_media", {}).get("media_info", {})
            quoted["article"] = {
                "title": q_art_result.get("title", ""),
                "preview_text": q_art_result.get("preview_text", ""),
                "cover_url": q_cover.get("original_img_url", ""),
                "rest_id": q_art_result.get("rest_id", ""),
            }
        return quoted

    async def search_user(self, query: str):
        await self.ensure_ready()
        return await self._call(self.client.search_user(query, count=10))
