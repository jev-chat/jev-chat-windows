import json
import unittest
from unittest.mock import patch

try:
    import httpx2 as httpx
except ImportError:
    import httpx
import openai

from core import llm
from core.jev_client import JevError
from core.providers import DRAFT_PROVIDERS


class AimlapiTests(unittest.TestCase):
    def setUp(self):
        self.provider = DRAFT_PROVIDERS["aimlapi"]
        self.requests = []

    def client(self, handler):
        original = openai.DefaultHttpxClient

        def make(**kwargs):
            return original(transport=httpx.MockTransport(handler), **kwargs)

        return patch.object(openai, "DefaultHttpxClient", side_effect=make)

    def test_preset(self):
        p = self.provider
        self.assertEqual((p.name, p.protocol, p.base, p.default),
                         ("AI/ML API", "openai", "https://api.aimlapi.com/v1", ""))
        self.assertEqual(p.extra(True), {})
        self.assertIsNone(p.headers)

    def test_public_models_filter(self):
        def handle(request):
            self.requests.append(request)
            self.assertEqual(str(request.url),
                             self.provider.base + "/models?include=all")
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, json={"data": [
                {"id": "chat", "type": "openai/chat-completions"},
                {"id": "image", "type": "image"}, {"id": "unknown"}]})

        with self.client(handle):
            self.assertEqual(llm.list_models("openai", self.provider.base, "test-key"),
                             ["chat"])
        self.assertEqual(len(self.requests), 1)

    def test_chat_auth_and_redirect(self):
        for target in (None, "https://example.org/chat",
                       "https://api.aimlapi.com.evil.test/chat"):
            with self.subTest(target=target):
                self.requests.clear()

                def handle(request):
                    self.requests.append(request)
                    self.assertEqual(request.url.host, "api.aimlapi.com")
                    self.assertEqual(request.headers["authorization"], "Bearer test-key")
                    self.assertNotIn("x-aimlapi-source", request.headers)
                    self.assertFalse(json.loads(request.content)["stream"])
                    if target:
                        return httpx.Response(307, headers={"location": target})
                    return httpx.Response(200, json={"choices": [
                        {"message": {"content": "ok"}}]})

                with self.client(handle):
                    if target:
                        with self.assertRaises(JevError):
                            llm.chat("openai", self.provider.base, "test-key", "chat", "", ["hi"])
                    else:
                        self.assertEqual(llm.chat("openai", self.provider.base, "test-key",
                                                  "chat", "", ["hi"]), "ok")
                self.assertEqual(len(self.requests), 1)

    def test_models_redirect(self):
        def handle(request):
            self.requests.append(request)
            self.assertEqual(request.url.host, "api.aimlapi.com")
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(302, headers={"location": "https://example.org/models"})

        with self.client(handle), self.assertRaises(JevError):
            llm.list_models("openai", self.provider.base, "test-key")
        self.assertEqual(len(self.requests), 1)
