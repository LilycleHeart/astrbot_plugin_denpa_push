"""翻译回归测试。运行: python tests/test_translation_behavior.py"""

import ast
import asyncio
from collections import deque
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

def load_translation_methods():
    """加载真实翻译方法，避免依赖 AstrBot、Twitter 客户端和本地测试桩。"""
    path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    plugin = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DenpaPushPlugin"
    )
    methods = {
        "_normalize_provider_id", "_normalize_fallback_list",
        "_collect_fallback_providers", "_get_provider_id", "_provider_chain",
        "_translate_text", "_translate_images", "_llm_timeout",
        "_translate_text_with_x",
        "_llm_generate_with_fallback", "_api_dashboard_config",
        "_build_card_data", "_record_push_history", "_as_int",
    }
    plugin.bases = []
    plugin.keywords = []
    plugin.decorator_list = []
    plugin.body = [node for node in plugin.body if getattr(node, "name", None) in methods]
    namespace = {
        "asyncio": asyncio, "re": re, "logger": logging.getLogger(__name__),
        "__file__": str(path), "datetime": datetime, "timezone": timezone,
        "TwitterClient": SimpleNamespace(extract_tweet_media=lambda data: ([], [], [])),
    }
    exec(compile(ast.Module(body=[plugin], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["DenpaPushPlugin"]


TranslationPlugin = load_translation_methods()


class TranslationBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, config):
        plugin = TranslationPlugin()
        plugin.config = config
        plugin.monitored_sessions = {"test-session"}
        plugin.context = SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="session-model")
        )
        plugin._llm_generate_with_fallback = AsyncMock(
            return_value=SimpleNamespace(completion_text="译文")
        )
        plugin._ocr_image = AsyncMock(return_value="image text")
        plugin.twitter = SimpleNamespace(translate_tweet=AsyncMock(return_value="X 译文"))
        return plugin

    async def test_text_only_never_translates_images_or_runs_ocr(self):
        for mode in ("multimodal", "text_extraction"):
            for empty_provider in (None, "", "   ", {}, {"id": " "}):
                with self.subTest(mode=mode, provider=empty_provider):
                    plugin = self.make_plugin({
                        "text_translate_provider": "text-model",
                        "text_translate_fallback_providers": ["text-backup"],
                        "image_translate_provider": empty_provider,
                        "image_translate_mode": mode,
                    })
                    result = await plugin._translate_images([
                        {"media_url": "https://example.com/image.jpg"}
                    ])
                    self.assertEqual(result, "")
                    plugin._llm_generate_with_fallback.assert_not_awaited()
                    plugin._ocr_image.assert_not_awaited()
                    plugin.context.get_current_chat_provider_id.assert_not_awaited()

    async def test_session_model_does_not_enable_image_translation(self):
        plugin = self.make_plugin({})
        self.assertEqual(await plugin._provider_chain("image"), [])
        plugin.context.get_current_chat_provider_id.assert_not_awaited()

    async def test_empty_image_fallback_entries_do_not_enable_translation(self):
        for fallbacks in (["", " "], " , ;\n", [{"id": ""}], None):
            with self.subTest(fallbacks=fallbacks):
                plugin = self.make_plugin({
                    "image_translate_fallback_providers": fallbacks,
                    "text_translate_fallback_providers": ["text-backup"],
                })
                self.assertEqual(await plugin._provider_chain("image"), [])

    async def test_explicit_image_chain_preserves_order_and_deduplicates(self):
        plugin = self.make_plugin({
            "image_translate_provider": {"id": " image-model "},
            "image_translate_fallback_providers": ["image-model", "image-backup"],
            "text_translate_provider": "text-model",
            "text_translate_fallback_providers": ["text-backup"],
        })
        self.assertEqual(await plugin._provider_chain("image"), [
            "image-model", "image-backup"
        ])

    async def test_image_fallback_only_uses_explicit_image_models(self):
        plugin = self.make_plugin({
            "image_translate_fallback_providers": "image-backup;image-backup-2",
            "text_translate_provider": "text-model",
        })
        self.assertEqual(await plugin._provider_chain("image"), [
            "image-backup", "image-backup-2"
        ])

    async def test_enabled_images_can_inherit_text_fallbacks(self):
        plugin = self.make_plugin({
            "image_translate_provider": "image-model",
            "text_translate_fallback_providers": ["text-backup"],
        })
        self.assertEqual(await plugin._provider_chain("image"), [
            "image-model", "text-backup"
        ])

    async def test_explicit_images_translate_in_both_modes(self):
        for mode in ("multimodal", "text_extraction"):
            with self.subTest(mode=mode):
                plugin = self.make_plugin({
                    "image_translate_provider": "image-model",
                    "image_translate_mode": mode,
                })
                url = "https://example.com/image.jpg"
                self.assertEqual(await plugin._translate_images([
                    {"media_url": url}
                ]), "译文")
                plugin._llm_generate_with_fallback.assert_awaited_once()
                call = plugin._llm_generate_with_fallback.await_args.kwargs
                self.assertEqual(call["provider_ids"], ["image-model"])
                if mode == "multimodal":
                    self.assertEqual(call["image_urls"], [url])
                    plugin._ocr_image.assert_not_awaited()
                else:
                    self.assertNotIn("image_urls", call)
                    plugin._ocr_image.assert_awaited_once_with(url)

    async def test_text_translation_remains_enabled(self):
        plugin = self.make_plugin({"text_translate_provider": "text-model"})
        self.assertEqual(await plugin._translate_text({"text": "Hello"}), "译文")
        call = plugin._llm_generate_with_fallback.await_args.kwargs
        self.assertEqual(call["provider_ids"], ["text-model"])
        self.assertNotIn("image_urls", call)

    async def test_text_session_default_and_fallbacks_are_unchanged(self):
        plugin = self.make_plugin({
            "text_translate_fallback_providers": ["text-backup"]
        })
        self.assertEqual(await plugin._provider_chain("text"), [
            "session-model", "text-backup"
        ])
        plugin.context.get_current_chat_provider_id.assert_awaited_once()


    async def test_x_success_skips_llm_and_keeps_images_disabled(self):
        plugin = self.make_plugin({"text_translate_source": "x_first"})
        self.assertEqual(await plugin._translate_text({"id": "123", "text": "Hello"}), "X 译文")
        plugin.twitter.translate_tweet.assert_awaited_once_with("123", "中文")
        plugin._llm_generate_with_fallback.assert_not_awaited()
        plugin.context.get_current_chat_provider_id.assert_not_awaited()
        self.assertEqual(await plugin._translate_images([{"media_url": "image"}]), "")
        plugin._ocr_image.assert_not_awaited()

    async def test_llm_mode_does_not_request_x(self):
        for config in ({}, {"text_translate_source": "llm"}):
            with self.subTest(config=config):
                plugin = self.make_plugin(config)
                self.assertEqual(await plugin._translate_text({"id": "123", "text": "Hello"}), "译文")
                plugin.twitter.translate_tweet.assert_not_awaited()

    async def test_x_failures_and_empty_results_fall_back(self):
        for result in ("", "   ", None, RuntimeError("HTTP 403"), asyncio.TimeoutError()):
            with self.subTest(result=result):
                plugin = self.make_plugin({
                    "text_translate_source": "x_first", "text_translate_provider": "text-model",
                    "text_translate_fallback_providers": ["backup"],
                })
                if isinstance(result, Exception):
                    plugin.twitter.translate_tweet.side_effect = result
                else:
                    plugin.twitter.translate_tweet.return_value = result
                self.assertEqual(await plugin._translate_text({"id": "123", "text": "Hello"}), "译文")
                call = plugin._llm_generate_with_fallback.await_args.kwargs
                self.assertEqual(call["provider_ids"], ["text-model", "backup"])

    async def test_x_timeout_cancels_request_before_llm_fallback(self):
        plugin = self.make_plugin({"text_translate_source": "x_first"})
        cancelled = asyncio.Event()

        async def slow_translation(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        plugin.twitter.translate_tweet.side_effect = slow_translation
        real_wait_for = asyncio.wait_for

        async def fast_wait_for(awaitable, timeout):
            self.assertEqual(timeout, 15.0)
            return await real_wait_for(awaitable, timeout=0.01)

        with patch.object(asyncio, "wait_for", fast_wait_for):
            self.assertEqual(await plugin._translate_text({"id": "123", "text": "Hello"}), "译文")
        self.assertTrue(cancelled.is_set())

    async def test_x_translates_quote_separately(self):
        plugin = self.make_plugin({"text_translate_source": "x_first"})
        plugin.twitter.translate_tweet.side_effect = ["正文", "引用译文"]
        result = await plugin._translate_text({
            "id": "123", "text": "Hello", "quoted_tweet": {
                "id": "456", "text": "Quote", "user": {"screen_name": "user", "name": "Name"},
            },
        })
        self.assertEqual(result, "正文\n\n[引用 @user (Name)]:\n引用译文")
        self.assertEqual([c.args[0] for c in plugin.twitter.translate_tweet.await_args_list], ["123", "456"])
        plugin._llm_generate_with_fallback.assert_not_awaited()

    async def test_unavailable_quote_falls_back_without_losing_content(self):
        for quote_id in ("", "456"):
            with self.subTest(quote_id=quote_id):
                plugin = self.make_plugin({"text_translate_source": "x_first"})
                plugin.twitter.translate_tweet.side_effect = ["正文", RuntimeError("unavailable")]
                await plugin._translate_text({
                    "id": "123", "text": "Hello", "quoted_tweet": {"id": quote_id, "text": "Quote"},
                })
                prompt = plugin._llm_generate_with_fallback.await_args.kwargs["prompt"]
                self.assertIn("Hello", prompt)
                self.assertIn("Quote", prompt)

    async def test_articles_and_missing_ids_use_llm(self):
        for data in (
            {"text": "Hello"},
            {"id": "123", "text": "Hello", "article": {"title": "Title", "full_text": "Full article"}},
            {"id": "123", "text": "Hello", "quoted_tweet": {
                "id": "456", "article": {"title": "Title", "full_text": "Full quote"},
            }},
        ):
            with self.subTest(data=data):
                plugin = self.make_plugin({"text_translate_source": "x_first"})
                self.assertEqual(await plugin._translate_text(data), "译文")
                plugin.twitter.translate_tweet.assert_not_awaited()

    async def test_retweet_uses_original_id(self):
        plugin = self.make_plugin({"text_translate_source": "x_first"})
        await plugin._translate_text({
            "id": "123", "translation_tweet_id": "456", "is_retweet": True, "text": "Hello",
        })
        plugin.twitter.translate_tweet.assert_awaited_once_with("456", "中文")

    async def test_no_models_retains_original_text_after_x_failure(self):
        plugin = self.make_plugin({"text_translate_source": "x_first"})
        plugin.twitter.translate_tweet.side_effect = RuntimeError("unavailable")
        plugin.monitored_sessions = set()
        self.assertEqual(await plugin._translate_text({"id": "123", "text": "Hello"}), "Hello")
        plugin._llm_generate_with_fallback.assert_not_awaited()

    async def test_custom_x_fallback_chain_overrides_text_models(self):
        plugin = self.make_plugin({
            "text_translate_source": "x_first", "text_translate_provider": "text-model",
            "x_translate_fallback_providers": ["custom-1", "custom-2", "custom-1"],
        })
        plugin.twitter.translate_tweet.side_effect = RuntimeError("unavailable")
        await plugin._translate_text({"id": "123", "text": "Hello"})
        self.assertEqual(plugin._llm_generate_with_fallback.await_args.kwargs["provider_ids"], ["custom-1", "custom-2"])
        plugin.context.get_current_chat_provider_id.assert_not_awaited()

    async def test_real_fallback_loop_tries_custom_models_in_order(self):
        plugin = self.make_plugin({
            "text_translate_source": "x_first",
            "x_translate_fallback_providers": ["one", "two", "three", "four"],
        })
        plugin.twitter.translate_tweet.side_effect = RuntimeError("unavailable")
        del plugin._llm_generate_with_fallback
        plugin._track_token_usage = Mock()
        plugin._llm_generate = AsyncMock(side_effect=[
            RuntimeError("failed"), None, SimpleNamespace(completion_text=" "),
            SimpleNamespace(completion_text="备用译文"),
        ])
        self.assertEqual(await plugin._translate_text({"id": "123", "text": "Hello"}), "备用译文")
        self.assertEqual([c.kwargs["provider_id"] for c in plugin._llm_generate.await_args_list], [
            "one", "two", "three", "four",
        ])
        self.assertEqual(plugin._track_token_usage.call_count, 2)

    async def test_all_custom_models_fail_preserves_original(self):
        plugin = self.make_plugin({
            "text_translate_source": "x_first", "x_translate_fallback_providers": ["one", "two"],
        })
        plugin.twitter.translate_tweet.side_effect = RuntimeError("unavailable")
        del plugin._llm_generate_with_fallback
        plugin._track_token_usage = Mock()
        plugin._llm_generate = AsyncMock(side_effect=RuntimeError("failed"))
        self.assertEqual(await plugin._translate_text({"id": "123", "text": "Hello"}), "Hello")

    async def test_dashboard_defaults_and_custom_model_list_round_trip(self):
        class Config(dict):
            save_config = Mock()

        plugin = self.make_plugin({})
        plugin.config = Config()
        plugin._apply_twitter_credentials = Mock()
        web = types.ModuleType("astrbot.api.web")
        web.json_response = lambda data: data
        web.error_response = lambda message, status_code: (message, status_code)
        web.request = SimpleNamespace(method="GET")
        with patch.dict(sys.modules, {"astrbot.api.web": web}):
            result = await plugin._api_dashboard_config()
            self.assertEqual(result["text_translate_source"], "llm")
            self.assertEqual(result["x_translate_fallback_providers"], [])
            web.request = SimpleNamespace(method="POST", json=AsyncMock(return_value={
                "text_translate_source": "x_first", "x_translate_fallback_providers": " one ;two;one ",
            }))
            self.assertEqual(await plugin._api_dashboard_config(), {"saved": True})
            plugin.config.save_config.assert_called_once()
            web.request = SimpleNamespace(method="GET")
            result = await plugin._api_dashboard_config()
            self.assertEqual(result["text_translate_source"], "x_first")
            self.assertEqual(result["x_translate_fallback_providers"], ["one", "two"])
            before = dict(plugin.config)
            web.request = SimpleNamespace(method="POST", json=AsyncMock(return_value={
                "text_translate_source": "invalid", "text_translate_provider": "should-not-change",
            }))
            self.assertEqual((await plugin._api_dashboard_config())[1], 400)
            self.assertEqual(dict(plugin.config), before)

    async def test_custom_x_fallbacks_do_not_affect_llm_mode(self):
        plugin = self.make_plugin({
            "text_translate_source": "llm", "text_translate_provider": "text-model",
            "x_translate_fallback_providers": ["custom"],
        })
        await plugin._translate_text({"id": "123", "text": "Hello"})
        self.assertEqual(plugin._llm_generate_with_fallback.await_args.kwargs["provider_ids"], ["text-model"])


    def use_real_model_fallback(self, plugin, generate):
        del plugin._llm_generate_with_fallback
        plugin._llm_generate = generate
        plugin._track_token_usage = Mock()

    async def test_metadata_records_x_success(self):
        plugin = self.make_plugin({"text_translate_source": "x_first"})
        meta = {}
        await plugin._translate_text({"id": "123", "text": "Hello"}, meta)
        self.assertEqual(meta, {"source": "x", "providers": []})

    async def test_metadata_records_only_successful_model(self):
        plugin = self.make_plugin({
            "text_translate_source": "x_first", "x_translate_fallback_providers": ["failed", "success"],
        })
        plugin.twitter.translate_tweet.side_effect = RuntimeError("failed")
        self.use_real_model_fallback(plugin, AsyncMock(side_effect=[
            RuntimeError("failed"), SimpleNamespace(completion_text="译文"),
        ]))
        meta = {}
        await plugin._translate_text({"id": "123", "text": "Hello"}, meta)
        self.assertEqual(meta, {"source": "llm", "providers": ["success"]})

    async def test_metadata_original_and_empty_text_are_not_marked_ai(self):
        plugin = self.make_plugin({"text_translate_provider": "failed"})
        self.use_real_model_fallback(plugin, AsyncMock(side_effect=RuntimeError("failed")))
        meta = {"source": "x", "providers": ["stale"]}
        self.assertEqual(await plugin._translate_text({"text": "Hello"}, meta), "Hello")
        self.assertEqual(meta, {"source": "original", "providers": []})
        await plugin._translate_text({"text": "https://example.com"}, meta)
        self.assertEqual(meta, {"source": "none", "providers": []})

    async def test_metadata_partial_translation_and_multiple_models(self):
        for partial in (True, False):
            with self.subTest(partial=partial):
                plugin = self.make_plugin({
                    "text_translate_provider": "one", "text_translate_fallback_providers": ["two"],
                })

                async def generate(provider_id, prompt, **kwargs):
                    if "AAAA" in prompt:
                        return SimpleNamespace(completion_text="第一段")
                    if partial or provider_id == "one":
                        return None
                    return SimpleNamespace(completion_text="第二段")

                self.use_real_model_fallback(plugin, AsyncMock(side_effect=generate))
                meta = {}
                translated = await plugin._translate_text({"text": "A" * 9999 + "\n\nBBBB"}, meta)
                self.assertEqual(meta["source"], "mixed" if partial else "llm")
                self.assertEqual(meta["providers"], ["one"] if partial else ["one", "two"])
                self.assertIn("BBBB" if partial else "第二段", translated)

    async def test_metadata_is_isolated_between_concurrent_tweets(self):
        plugin = self.make_plugin({"text_translate_source": "x_first", "text_translate_provider": "model"})

        async def native(tweet_id, language):
            await asyncio.sleep(0.01)
            if tweet_id == "123":
                return "X 译文"
            raise RuntimeError("failed")

        plugin.twitter.translate_tweet.side_effect = native
        self.use_real_model_fallback(plugin, AsyncMock(return_value=SimpleNamespace(completion_text="AI 译文")))
        x_meta, ai_meta = {}, {}
        await asyncio.gather(
            plugin._translate_text({"id": "123", "text": "First"}, x_meta),
            plugin._translate_text({"id": "456", "text": "Second"}, ai_meta),
        )
        self.assertEqual(x_meta, {"source": "x", "providers": []})
        self.assertEqual(ai_meta, {"source": "llm", "providers": ["model"]})

    async def test_source_reaches_history_but_not_sent_card_render_data(self):
        plugin = self.make_plugin({"text_translate_source": "x_first"})
        plugin.twitter.get_full_article_text = AsyncMock(return_value="")
        plugin._extract_seed_color = AsyncMock(return_value=None)
        plugin._build_palette_async = AsyncMock(return_value=({}, False))
        plugin._render_card = AsyncMock(return_value="card.png")
        info = await plugin._build_card_data({
            "id": "123", "text": "Hello", "user": {
                "id": "1", "name": "Name", "screen_name": "user", "avatar_url": "",
            },
        })
        self.assertEqual(info["text_translation"], {"source": "x", "providers": []})
        rendered = plugin._render_card.await_args.args[1]
        self.assertNotIn("text_translation", rendered)
        self.assertEqual(rendered["translated_text"], "X 译文")
        plugin._push_history = deque()
        plugin._schedule_save = Mock()
        plugin._write_history_payload = Mock()
        plugin._history_payload = Mock()
        plugin._prune_push_history = Mock()
        plugin._record_push_history(info, "session")
        self.assertEqual(plugin._push_history[0]["text_translation"], {"source": "x", "providers": []})
        info["text_translation"]["source"] = "changed"
        self.assertEqual(plugin._push_history[0]["text_translation"]["source"], "x")

    async def test_full_build_keeps_regular_tweet_on_x_translation_path(self):
        from test_x_translation_client import NativeClient

        plugin = self.make_plugin({"text_translate_source": "x_first"})
        native = NativeClient()
        native._fetch_tweet_result = AsyncMock(return_value={"legacy": {"full_text": "Hello"}})
        plugin.twitter.get_full_article_text = native.get_full_article_text
        plugin._extract_seed_color = AsyncMock(return_value=None)
        plugin._build_palette_async = AsyncMock(return_value=({}, False))
        plugin._render_card = AsyncMock(return_value="card.png")
        data = {
            "id": "123", "text": "Hello", "user": {
                "id": "1", "name": "Name", "screen_name": "user", "avatar_url": "",
            },
        }
        info = await plugin._build_card_data(data)
        plugin.twitter.translate_tweet.assert_awaited_once_with("123", "中文")
        plugin._llm_generate_with_fallback.assert_not_awaited()
        self.assertFalse(data.get("article"))
        self.assertEqual(info["text_translation"]["source"], "x")


if __name__ == "__main__":
    unittest.main()
