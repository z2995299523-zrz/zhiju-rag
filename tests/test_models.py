import json
import os
import unittest
from unittest.mock import patch

import httpx

from ragcore import models


DATA = [
    {"id": "S1", "kind": "document", "text": "申请报销须提供发票。", "title": "报销指引"},
    {"id": "S2", "kind": "api", "rows": [{"年度": 2026, "执行率": "75%"}]},
]
CHAT = {"RAG_LLM_BASE_URL": "http://127.0.0.1:8922/v1", "RAG_LLM_MODEL": "test"}
EMBED = {"RAG_EMBED_BASE_URL": "http://127.0.0.1:8922/v1", "RAG_EMBED_MODEL": "test"}


class StreamingModelTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, fragments, *, done=True):
        def respond(req):
            self.assertIs(json.loads(req.content)["stream"], True)
            packets = [json.dumps({"choices": [{"delta": {"content": fragment}}]}, ensure_ascii=False)
                       for fragment in fragments]
            if done:
                packets.append("[DONE]")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="".join(f"data: {packet}\r\n\r\n" for packet in packets).encode())
        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    @patch.dict(os.environ, CHAT, clear=True)
    async def test_verbatim_model_deltas_keep_provider_boundaries(self):
        citations = [DATA[0], {"id": "S2", "kind": "document", "text": "并附合同。"}]
        fragments = ["申请报销", "须提供发票。 [S", "1]\n\n并附", "合同。 [S2]"]
        with patch.object(models.httpx, "AsyncClient", return_value=self.provider(fragments)):
            self.assertEqual([s async for s in models.stream_answer("材料", citations)], fragments)

    @patch.dict(os.environ, CHAT, clear=True)
    async def test_invalid_continuation_and_truncated_stream_keep_only_grounded_draft(self):
        for rest, done in [("虚构事实", True), ("须提供", False), ("须提供", True)]:
            with self.subTest(rest=rest, done=done), patch.object(
                models.httpx, "AsyncClient", return_value=self.provider(["申请报销", rest], done=done)
            ):
                stream = models.stream_answer("材料", [DATA[0]])
                self.assertEqual(await anext(stream), "申请报销")
                with self.assertRaises(ValueError):
                    async for piece in stream:
                        self.assertEqual(piece, "须提供")


def reply(selection):
    return {"choices": [{"message": {"content": json.dumps(selection)}}]}


class ModelTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    @patch.object(models, "_post")
    def test_offline_mode_never_calls_network(self, post):
        self.assertIsNone(models.synthesize("问题", DATA))
        self.assertIsNone(models.semantic_rank("问题", [{"text": "原文"}]))
        self.assertEqual(models.refine_route("问题", [], {"intent": "documents"}), {"intent": "documents"})
        post.assert_not_called()

    @patch.dict(os.environ, CHAT, clear=True)
    @patch.object(models, "_post")
    def test_model_plan_is_allowlisted_and_span_checked(self, post):
        initial = {"intent": "documents", "connector_id": None, "params": {}, "missing": []}
        configs = [{"id": 2, "name": "库存", "url": "https://internal-only.test", "token_env": "SECRET",
                    "parameters": [{"name": "warehouse", "label": "仓库", "required": True},
                                   {"name": "max_items", "label": "上限", "type": "integer"}]}]
        question = "想看看东区现在能发出去的头 15 种材料"
        result = {"intent": "data", "connector_id": 2, "params": {"warehouse": "东区", "max_items": "15"}}
        post.return_value = reply(result)
        verified = models.refine_route(question, configs, initial)
        self.assertEqual(verified["params"], {"warehouse": "东区", "max_items": 15})
        self.assertEqual(verified["connector_id"], 2)
        self.assertEqual(verified["missing"], [])
        self.assertEqual(post.call_args.args[1], "/chat/completions")
        payload = json.dumps(post.call_args.args[2], ensure_ascii=False)
        self.assertNotIn("internal-only", payload)
        self.assertNotIn("SECRET", payload)
        for bad in (
            {**result, "connector_id": 99},
            {**result, "params": {"warehouse": "西区"}},
            {**result, "params": {"max_items": "5"}},
            {**result, "params": {"url": "异域地址"}},
            {**result, "url": "异域地址"},
        ):
            with self.subTest(bad=bad):
                post.return_value = reply(bad)
                self.assertEqual(models.refine_route(question, configs, initial), initial)

    @patch.dict(os.environ, CHAT, clear=True)
    @patch.object(models, "_post")
    def test_refine_respects_manual_modes_and_required_fields(self, post):
        config = {"id": 2, "name": "库存", "parameters": [{"name": "warehouse", "label": "仓库", "required": True}]}
        local = {"intent": "documents"}
        self.assertIs(models.refine_route("查询", [config], local, mode="documents"), local)
        post.assert_not_called()
        post.return_value = reply({"intent": "data", "connector_id": 2, "params": {}})
        self.assertEqual(models.refine_route("看看有些什么可以发货", [config], local)["missing"], ["仓库"])

    @patch.dict(os.environ, CHAT, clear=True)
    @patch.object(models, "_post")
    def test_render_only_provided_evidence(self, post):
        post.return_value = reply({"keys": ["q2", "q1"]})
        self.assertEqual(models.synthesize("报销及执行率", DATA),
                         "年度：2026，执行率：75% [S2]\n\n申请报销须提供发票。 [S1]")
        for invalid in ({"keys": ["q9"]}, {"keys": ["q1"]},
                        {"keys": ["q1"], "text": "编造答案"}, {"keys": {"q1": True}}):
            with self.subTest(invalid=invalid):
                post.return_value = reply(invalid)
                self.assertIsNone(models.synthesize("报销及执行率", DATA))

    @patch.dict(os.environ, EMBED, clear=True)
    @patch.object(models, "_post")
    def test_embedding_cache_ranking_and_threshold(self, post):
        models._VECTORS.clear()
        post.return_value = {"data": [
            {"index": 2, "embedding": [0, 1]},
            {"index": 0, "embedding": [1, 0]},
            {"index": 1, "embedding": [2, 0]},
        ]}
        chunks = [{"id": 1, "text": "出差"}, {"id": 2, "text": "数据治理"}]
        for _ in range(2):
            self.assertEqual([c["id"] for c in models.semantic_rank("差旅费", chunks)], [1])
        post.assert_called_once()

    @patch.dict(os.environ, EMBED, clear=True)
    @patch.object(models, "_post")
    def test_invalid_vector_response_falls_back(self, post):
        models._VECTORS.clear()
        post.return_value = {"data": [
            {"index": 0, "embedding": [1]}, {"index": 0, "embedding": [1]},
        ]}
        self.assertIsNone(models.semantic_rank("报销", [{"text": "发票"}]))

    @patch.dict(os.environ, CHAT, clear=True)
    def test_rejects_remote_plain_http_and_url_credentials(self):
        for base in ("http://192.0.2.1/v1", "https://token@example.com/v1", "file:///tmp/data"):
            with self.subTest(base=base), patch.dict(os.environ, {"RAG_LLM_BASE_URL": base}):
                self.assertFalse(models.model_enabled())
