"""翻译回归测试。运行: python tests/test_translation_behavior.py"""

import ast
import asyncio
import logging
from pathlib import Path
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    }
    plugin.bases = []
    plugin.keywords = []
    plugin.decorator_list = []
    plugin.body = [node for node in plugin.body if getattr(node, "name", None) in methods]
    namespace = {"asyncio": asyncio, "re": re, "logger": logging.getLogger(__name__)}
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


if __name__ == "__main__":
    unittest.main()
