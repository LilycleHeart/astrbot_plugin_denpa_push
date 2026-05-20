import json
from typing import Optional
from twikit import Client
from astrbot.api import logger


class TwitterClient:
    def __init__(self):
        self.client = Client("en-US")
        self._initialized = False
        self._auth_token = ""
        self._ct0 = ""

    def set_credentials(self, auth_token: str, ct0: str = ""):
        self._auth_token = auth_token
        self._ct0 = ct0
        self._initialized = False

    async def ensure_ready(self):
        if self._initialized:
            return
        if not self._auth_token:
            raise ValueError("Twitter auth_token not configured")
        cookies = {"auth_token": self._auth_token}
        if self._ct0:
            cookies["ct0"] = self._ct0
        self.client.set_cookies(cookies)
        self._initialized = True
        logger.info("Twitter client initialized successfully")

    async def get_user_by_screen_name(self, screen_name: str):
        await self.ensure_ready()
        return await self.client.get_user_by_screen_name(screen_name)

    async def get_user_tweets(self, user_id: str, count: int = 20):
        await self.ensure_ready()
        return await self.client.get_user_tweets(user_id, "Tweets", count=count)

    async def get_tweet_by_id(self, tweet_id: str):
        await self.ensure_ready()
        return await self.client.get_tweet_by_id(tweet_id)

    async def get_full_article_text(self, tweet_id: str) -> str:
        """Fetch full article text body via TweetResultByRestId with withArticlePlainText=True."""
        await self.ensure_ready()
        import httpx, json as _json

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
            "X-Csrf-Token": self._ct0,
            "Content-Type": "application/json",
        }
        cookies = {"auth_token": self._auth_token, "ct0": self._ct0}
        variables = _json.dumps(
            {
                "tweetId": tweet_id,
                "withCommunity": False,
                "includePromotedContent": False,
                "withVoice": False,
            }
        )
        features = _json.dumps(
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
        field_toggles = _json.dumps(
            {
                "withArticleRichContentState": True,
                "withArticlePlainText": True,
                "withGrokAnalyze": False,
            }
        )
        url = "https://x.com/i/api/graphql/Xl5pC_lBk_gcO2ItU39DQw/TweetResultByRestId"
        params = {
            "variables": variables,
            "features": features,
            "fieldToggles": field_toggles,
        }
        async with httpx.AsyncClient(cookies=cookies, headers=headers, timeout=30) as c:
            r = await c.get(url, params=params)
            if r.status_code != 200:
                logger.warning(f"Article fetch failed: {r.status_code}")
                return ""
            data = r.json()
            result = data.get("data", {}).get("tweetResult", {}).get("result", {})

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

            legacy = result.get("legacy", {})
            if legacy.get("full_text"):
                return legacy["full_text"]
            note = (
                result.get("note_tweet", {})
                .get("note_tweet_results", {})
                .get("result", {})
            )
            if note:
                text = note.get("text", "")
                if text:
                    return text
            return legacy.get("text", legacy.get("full_text", ""))
        return ""

    @staticmethod
    def extract_tweet_data(tweet) -> dict:
        data = {
            "id": tweet.id,
            "text": tweet.text or "",
            "full_text": getattr(tweet, "full_text", tweet.text or ""),
            "created_at": tweet.created_at,
            "created_at_datetime": tweet.created_at_datetime,
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
        }

        if hasattr(tweet, "media") and tweet.media:
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
        logger.info(f"extract: tweet.text len={len(data.get('text',''))}, raw_full len={len(raw_full)}")
        if raw_full and len(raw_full) > len(data.get("text", "")):
            data["text"] = raw_full
            data["full_text"] = raw_full
            logger.info(f"extract: overrode text with raw_full (len={len(raw_full)})")
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
        await self.ensure_ready()
        import httpx, json as _json

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
            "X-Csrf-Token": self._ct0,
            "Content-Type": "application/json",
        }
        cookies = {"auth_token": self._auth_token, "ct0": self._ct0}
        variables = _json.dumps(
            {
                "tweetId": tweet_id,
                "withCommunity": False,
                "includePromotedContent": False,
                "withVoice": False,
            }
        )
        features = _json.dumps(
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
        field_toggles = _json.dumps(
            {
                "withArticleRichContentState": True,
                "withArticlePlainText": True,
                "withGrokAnalyze": False,
            }
        )
        url = "https://x.com/i/api/graphql/Xl5pC_lBk_gcO2ItU39DQw/TweetResultByRestId"
        params = {
            "variables": variables,
            "features": features,
            "fieldToggles": field_toggles,
        }
        async with httpx.AsyncClient(cookies=cookies, headers=headers, timeout=30) as c:
            r = await c.get(url, params=params)
            if r.status_code != 200:
                return {}
            result = r.json().get("data", {}).get("tweetResult", {}).get("result", {})
            if not result:
                return {}
            # Parse quoted tweet directly from the raw result
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
        result = {
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
            result["article"] = {
                "title": q_art_result.get("title", ""),
                "preview_text": q_art_result.get("preview_text", ""),
                "cover_url": q_cover.get("original_img_url", ""),
                "rest_id": q_art_result.get("rest_id", ""),
            }
        return result

    async def search_user(self, query: str):
        await self.ensure_ready()
        return await self.client.search_user(query, count=10)
