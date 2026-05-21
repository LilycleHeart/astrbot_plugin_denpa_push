# Changelog

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
