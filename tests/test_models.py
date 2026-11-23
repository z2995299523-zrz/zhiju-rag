import asyncio
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


def block(text, source, quote):
    return json.dumps({"text": text, "support": [{"id": source, "quote": quote}]}, ensure_ascii=False)


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
    async def test_rephrased_paragraph_published_before_provider_finishes(self):
        first = block("报销时请提交发票。", "S1", "报销须提供发票")
        second = block("- 采购支出还需附上合同。", "S2", "采购支出需要附合同")
        citations = [DATA[0], {"id": "S2", "kind": "document", "text": "采购支出需要附合同。"}]
        release = asyncio.Event()

        class PausedProvider(httpx.AsyncByteStream):
            async def __aiter__(self):
                for fragment in (first[:10], first[10:]):
                    packet = {"choices": [{"delta": {"content": fragment}}]}
                    yield ("data: " + json.dumps(packet) + "\n\n").encode()
                await release.wait()
                packet = {"choices": [{"delta": {"content": "\n" + second}}]}
                yield ("data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n").encode()

        def respond(req):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=PausedProvider())
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(models.httpx, "AsyncClient", return_value=client):
            stream = models.stream_answer("材料", citations)
            self.assertEqual(await asyncio.wait_for(anext(stream), 1), "报销时请提交发票。 [S1]")
            release.set()
            self.assertEqual([s async for s in stream], ["\n\n- 采购支出还需附上合同。 [S2]"])

    @patch.dict(os.environ, CHAT, clear=True)
    async def test_invalid_citations_or_interrupted_stream_preserve_verified_paragraph_only(self):
        valid = block("申请报销应提供发票。", "S1", "报销须提供发票")
        for tail, done in ((block("还要合同。", "S9", "合同"), True), ('{"text":', True), ("", False)):
            with self.subTest(tail=tail, done=done), patch.object(
                models.httpx, "AsyncClient", return_value=self.provider([valid, tail], done=done)
            ):
                stream = models.stream_answer("材料", [DATA[0]])
                self.assertEqual(await anext(stream), "申请报销应提供发票。 [S1]")
                with self.assertRaises(models.ModelAnswerError):
                    await anext(stream)

    @patch.dict(os.environ, CHAT, clear=True)
    async def test_only_completed_explicit_insufficiency_is_an_abstention(self):
        for body, done, error in [(['{"insufficient":true}'], True, models.InsufficientEvidence),
                                  (['{"insufficient":true}'], False, models.ModelAnswerError),
                                  ([], True, models.ModelAnswerError)]:
            with self.subTest(body=body, done=done), patch.object(
                models.httpx, "AsyncClient", return_value=self.provider(body, done=done)
            ):
                with self.assertRaises(error):
                    await anext(models.stream_answer("材料", [DATA[0]]))


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
    def test_grounded_paragraphs_sync_share_source_and_quantity_checks(self, post):
        def respond(*paragraphs):
            post.return_value = {"choices": [{"message": {"content": "\n".join(paragraphs)}}]}
        api_quote = "年度：2026，执行率：75%"
        valid = block("2026 年的执行率为 **75%**。", "S2", api_quote)
        doc = block("报销时应提交发票。", "S1", "报销须提供发票")
        respond(valid, doc)
        self.assertEqual(models.synthesize("报销及执行率", DATA),
                         "2026 年的执行率为 **75%**。 [S2]\n\n报销时应提交发票。 [S1]")
        for bad in (block("报销时要合同。", "S1", "要合同"), block("执行率为 85%。", "S2", api_quote),
                    block("执行率为 75‰。", "S2", api_quote), block("报销时提交发票。", "S9", "发票"),
                    block("火星上发现恐龙。", "S1", "发票"), block("报销 [S99]", "S1", "发票"),
                    block("报销无需发票。", "S1", "报销须提供发票"), doc):
            with self.subTest(bad=bad):
                respond(doc, bad)
                with self.assertRaises(models.ModelAnswerError):
                    models.synthesize("报销及执行率", DATA)
        respond('{"insufficient":true}')
        with self.assertRaises(models.InsufficientEvidence):
            models.synthesize("材料能否确认三月底的执行率？", DATA)

    @patch.dict(os.environ, CHAT, clear=True)
    @patch.object(models, "_post")
    def test_comparison_labels_years_from_source_titles(self, post):
        evidence = [{"id":"S1", "kind":"document", "title":"2023年公报", "text":"年末城镇人口共93267万人。"},
                    {"id":"S2", "kind":"document", "title":"2024年公报", "text":"年末城镇人口共94350万人。"}]
        p1 = block("- **2023 年**：城镇人口为 93,267 万人。", "S1", "城镇人口共93267万人")
        p2 = block("- **2024 年**：城镇人口为 94350 万人。", "S2", "城镇人口共94350万人")
        def respond(*lines):
            post.return_value = {"choices": [{"message": {"content": "\n".join(lines)}}]}
        respond(p1, p2)
        self.assertEqual(models.synthesize("对比两年城镇人口", evidence),
                         "- **2023 年**：城镇人口为 93,267 万人。 [S1]\n- **2024 年**：城镇人口为 94350 万人。 [S2]")
        for bad in (p1.replace("2023 年", "2024 年"), p2.replace("94350 万人", "94350 人").replace("人口为", "人口共")):
            respond(bad)
            with self.assertRaises(models.ModelAnswerError):
                models.synthesize("对比两年城镇人口", evidence)

    @patch.dict(os.environ, CHAT, clear=True)
    @patch.object(models, "_post")
    def test_calendar_date_must_match_as_a_whole_and_the_inspected_excerpt(self, post):
        evidence = [{"id":"S1", "kind":"document", "title":"日历",
                     "text":"假期起始日期：20260217；更新时间：20240516。隐藏备注：20260322。",
                     "excerpt":"假期起始日期：20260217；更新时间：20240516。"}]
        def respond(text, quote):
            post.return_value = {"choices":[{"message":{"content":block(text, "S1", quote)}}]}
        respond("假期起始日期为 2026年2月17日。", "假期起始日期：20260217")
        self.assertIn("2026年2月17日", models.synthesize("哪天？", evidence))
        respond("假期起始日期为 2026年5月17日。", evidence[0]["excerpt"])
        with self.assertRaises(models.ModelAnswerError):
            models.synthesize("哪天？", evidence)
        respond("假期起始日期为 2026年3月22日。", "隐藏备注：20260322")
        with self.assertRaises(models.ModelAnswerError):
            models.synthesize("哪天？", evidence)

    @patch.dict(os.environ, CHAT, clear=True)
    @patch.object(models, "_post")
    def test_business_date_is_not_an_identifier_or_audit_timestamp(self, post):
        record = {"uid": "20301120@calendar", "dtstamp": "20301215T090000Z", "summary": "开放日"}
        evidence = [{"id": "S1", "kind": "document", "text": json.dumps(record, ensure_ascii=False)}]
        def answer(question, body):
            post.return_value = {"choices": [{"message": {"content": body}}]}
            return models.synthesize(question, evidence)
        with self.assertRaises(models.InsufficientEvidence):
            answer("开放日在哪一天？", '{"insufficient":true}')
        payload = post.call_args.args[2]["messages"][-1]["content"]
        self.assertNotIn("20301120", payload)
        self.assertNotIn("20301215", payload)
        for answer_date, quote in (("2030 年 11 月 20 日", "20301120@calendar"),
                                   ("2030 年 12 月 15 日", "20301215T090000Z")):
            with self.subTest(answer_date=answer_date), self.assertRaises(models.ModelAnswerError):
                answer("开放日在哪一天？", block(f"开放日是 {answer_date}。", "S1", quote))
        record["dtstart"] = "20310216"
        evidence[0]["text"] = json.dumps(record, ensure_ascii=False)
        self.assertIn("2031 年 2 月 16 日", answer("开放日在哪一天？", block(
            "开放日为 2031 年 2 月 16 日。", "S1", '"summary": "开放日", "dtstart": "20310216"')))
        self.assertIn("2030 年 12 月 15 日", answer("记录更新时间？", block(
            "记录更新时间为 2030 年 12 月 15 日。", "S1", '"dtstamp": "20301215T090000Z"')))

    @patch.dict(os.environ, CHAT, clear=True)
    @patch.object(models, "_post")
    def test_missing_requested_date_can_be_named_but_not_used_as_a_fact(self, post):
        quote = "年末客流为 91 万人。"
        sources = [{"id": "S1", "kind": "document", "text": quote}]
        question = "4 月 26 日有多少客流？"
        def answer(text):
            post.return_value = {"choices": [{"message": {"content": block(text, "S1", quote)}}]}
            return models.synthesize(question, sources)
        for text in ("资料未列出4月26日当天的客流。", "4月26日当天的客流无法确定。",
                     "客流 91 万人是年末值，不是 4 月 26 日的数值。"):
            self.assertIn(text, answer(text))
        for text in ("资料未列出4月25日当天的客流。", "4月26日当天客流为 91 万人。",
                     "资料没有列出4月26日的客流，但4月26日客流为 91 万人。"):
            with self.subTest(text=text), self.assertRaises(models.ModelAnswerError):
                answer(text)

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
