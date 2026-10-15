"""X 翻译 HTTP 回归测试（MockTransport，无需登录或访问网络）。"""

import ast
import asyncio
import html
import json
import logging
from pathlib import Path
import re
import unittest
from urllib.parse import unquote

import httpx


def load_client_methods():
    path = Path(__file__).resolve().parents[1] / "twitter_client.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    client = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TwitterClient")
    methods = {"_translation_language", "_translation_response_text", "translate_tweet", "_call", "_get_semaphore"}
    client.body = [n for n in client.body if getattr(n, "name", None) in methods]
    namespace = {
        "asyncio": asyncio, "html": html, "json": json, "re": re,
        "unquote": unquote, "logger": logging.getLogger(__name__), "DEFAULT_API_TIMEOUT": 30.0,
    }
    bearer = next(n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "_BEARER" for t in n.targets
    ))
    namespace["_BEARER"] = ast.literal_eval(bearer.value)
    exec(compile(ast.Module(body=[client], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["TwitterClient"]


NativeClient = load_client_methods()


class XTranslationClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, handler):
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"X-Csrf-Token": "test-csrf"},
            cookies={"auth_token": "test-auth", "ct0": "test-csrf"},
        )
        self.addAsyncCleanup(http.aclose)
        client = NativeClient()
        client._auth_token = "test-auth"
        client._ct0 = "test-csrf"
        client._get_http = lambda: http
        client._api_concurrency = 2
        client._api_semaphore = None
        return client

    async def test_request_body_headers_and_chinese_script(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"result": {"content_type": "POST", "text": "译文 &amp; 内容"}})

        client = self.make_client(handle)
        for language, header in (("中文", "zh-cn"), ("繁體中文", "zh-tw"), ("English", "en")):
            self.assertEqual(await client.translate_tweet("123", language), "译文 & 内容")
            request = requests[-1]
            self.assertEqual(str(request.url), "https://api.x.com/2/grok/translation.json")
            self.assertEqual(request.method, "POST")
            self.assertEqual(json.loads(request.content), {
                "content_type": "POST", "id": "123", "dst_lang": "zh" if header.startswith("zh") else "en",
            })
            self.assertEqual(request.headers["x-twitter-client-language"], header)
            self.assertEqual(request.headers["x-twitter-auth-type"], "OAuth2Session")
            self.assertIn("auth_token=test-auth", request.headers["cookie"])
            self.assertEqual(request.headers["x-csrf-token"], "test-csrf")
            self.assertTrue(request.headers["authorization"].startswith("Bearer "))
            self.assertNotIn("%3D", request.headers["authorization"])
            self.assertEqual(request.extensions["timeout"]["read"], 15.0)

    async def test_json_and_newline_json(self):
        for raw, expected in (
            ('{"result":{"text":"one"}}', "one"),
            ('{"translated_text":"one"}', "one"),
            ('{"result":{"text":"one"}}\n{"result":{"text":" two"}}\n', "one two"),
        ):
            with self.subTest(raw=raw):
                client = self.make_client(lambda request: httpx.Response(200, text=raw))
                self.assertEqual(await client.translate_tweet("123", "en"), expected)

    async def test_bad_responses_raise_instead_of_becoming_translations(self):
        for status, raw in (
            (403, '{"error":"Forbidden"}'), (429, '{"error":"Rate limit"}'),
            (200, '{}'), (200, '{"result":{"text":" "}}'),
            (200, '{"errors":[{"message":"failed"}],"text":"error text"}'),
            (200, '{"result":{"text":"partial"}}\n{"error":"failed"}'),
            (200, '{"result":{"text":"partial"}}\nnot-json'),
            (200, '<html>login required</html>'),
        ):
            with self.subTest(status=status, raw=raw):
                client = self.make_client(lambda request: httpx.Response(status, text=raw))
                with self.assertRaises((ValueError, httpx.HTTPStatusError)):
                    await client.translate_tweet("123", "中文")

    async def test_invalid_input_and_missing_cookies_make_no_request(self):
        def forbidden(request):
            self.fail("Unexpected network request")

        for tweet_id, lang, auth, csrf in (
            ("bad-id", "中文", "auth", "csrf"), ("123", "unknown language", "auth", "csrf"),
            ("123", "中文", "", "csrf"), ("123", "中文", "auth", ""),
        ):
            with self.subTest(tweet_id=tweet_id, lang=lang, auth=auth, csrf=csrf):
                client = self.make_client(forbidden)
                client._auth_token, client._ct0 = auth, csrf
                with self.assertRaises(ValueError):
                    await client.translate_tweet(tweet_id, lang)

    async def test_translation_uses_shared_twitter_concurrency_limit(self):
        active = peak = 0

        async def handle(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, json={"result": {"text": "译文"}})

        client = self.make_client(handle)
        self.assertEqual(await asyncio.gather(*[
            client.translate_tweet(str(i), "中文") for i in range(6)
        ]), ["译文"] * 6)
        self.assertEqual(peak, 2)


if __name__ == "__main__":
    unittest.main()
