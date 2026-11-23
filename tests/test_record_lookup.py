import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from main import create_app
from ragcore import models
from ragcore.engine import document_sources
from ragcore.record_lookup import direct_record_answer
from ragcore.retrieval import grounded_excerpts


class RecordLookupTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"RAG_ADMIN_PASSWORD":"test-admin", "RAG_COOKIE_SECURE":"0",
                                     "RAG_LLM_BASE_URL":"", "RAG_EMBED_BASE_URL":""})
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = create_app(tmp.name,seed=False)
        self.client = TestClient(self.app,base_url="http://127.0.0.1")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.client.post("/api/login",json={"password":"test-admin"}).raise_for_status()
        self.store = self.app.state.store

    def ingest(self, filename, text):
        self.store.ingest(filename,text.encode(),"test")

    def test_duplicate_subject_label_or_unspecified_condition_cannot_fall_back(self):
        q = "甲山站的容量是多少？"
        def lookup():
            return direct_record_answer(self.store,q,document_sources(self.store,q))
        self.ingest("记录.csv","厂站,容量\n甲山站,321\n")
        self.assertIn("321",lookup()[0])
        q = "甲山站高峰期的容量是多少？"
        self.assertIsNone(lookup())
        q = "甲山站的容量是多少？"
        self.ingest("记录.csv","厂站,容量,容量\n甲山站,321,520\n")
        self.assertIsNone(lookup())
        self.ingest("记录.csv","厂站,容量\n甲山站,321\n甲山站,520\n")
        self.assertIsNone(lookup())

    def test_foreign_narrative_sources_do_not_hide_a_unique_row_and_other_tables_still_block_it(self):
        q = "甲山站的容量是多少？"
        self.ingest("设备容量.csv","厂站,容量\n甲山站,321")
        self.ingest("统计公报.html","<p>全区容量为500。</p><p>年末全区容量为630。</p>")
        def candidates():
            rows = [r for r in self.store.candidates(q) if "表头" not in r["locator"]]
            sources = grounded_excerpts(q,rows,limit=len(rows))
            return [{**s,"chunk_id":s["id"],"title":s["filename"]} for s in sources]
        selected = candidates()
        self.assertEqual({s["title"] for s in selected},{"设备容量.csv","统计公报.html"})
        answer, refs = direct_record_answer(self.store,q,selected)
        self.assertIn("321",answer)
        self.assertEqual([s["title"] for s in refs],["设备容量.csv"])
        self.ingest("备用台账.csv","站点,容量\n甲山站,456")
        self.assertIsNone(direct_record_answer(self.store,q,candidates()))
        self.assertIsNone(direct_record_answer(self.store,q,selected))

    def test_exact_calendar_date_or_original_json_field_and_ambiguous_date(self):
        q = "2026年6月1日对应的值班人员是什么？"
        records = [{"日期":"2026-06-01","值班人员":"张梅"},{"日期":"2026-06-02","值班人员":"孙河"}]
        self.ingest("值班.json",json.dumps(records,ensure_ascii=False))
        ans, citations = direct_record_answer(self.store,q,document_sources(self.store,q))
        self.assertIn("张梅",ans)
        self.assertEqual(len(citations),1)
        self.assertEqual(len(citations[0]["highlights"][0]["ranges"]),2)
        records.append({"日期":"2026-06-01","值班人员":"王明"})
        self.ingest("值班.json",json.dumps(records,ensure_ascii=False))
        self.assertIsNone(direct_record_answer(self.store,q,document_sources(self.store,q)))

    def test_validation_only_before_the_first_visible_paragraph(self):
        self.ingest("记录.csv","厂站,容量\n甲山站,321")
        payload = {"question":"甲山站的容量是多少？","mode":"documents"}
        def events():
            response = self.client.post("/api/query/stream",json=payload)
            response.raise_for_status()
            return [(a.removeprefix("event: "),json.loads(b.removeprefix("data: ")))
                    for a,b in (block.splitlines() for block in response.text.strip().split("\n\n"))]
        async def partial(*args):
            yield "已核实的第一段。 [S1]"
            raise models.RejectedAnswer("later validation")
        async def offline(*args):
            raise models.ModelAnswerError("transport")
            yield
        async def rejected(*args):
            raise models.RejectedAnswer("rejected paragraph")
            yield
        with patch.object(models,"model_enabled",return_value=True):
            for provider in (partial,offline):
                with patch.object(models,"stream_answer",provider), patch("ragcore.engine.direct_record_answer") as lookup:
                    stream = events()
                    lookup.assert_not_called()
                    self.assertEqual(stream[-1][0],"error")
                    self.assertNotIn("done",[name for name,_ in stream])
            with patch.object(models,"stream_answer",rejected):
                stream = events()
            result = stream[-1][1]["result"]
            self.assertEqual(result["status"],"answered")
            self.assertIn("321",result["answer"])
            self.assertEqual(result["citations"][0]["locator"],"第 2 行")
            self.assertEqual(result["answer"],"".join(v["text"] for name,v in stream if name=="delta"))
