import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import create_app
from ragcore.business_answers import matched_metrics, operation_answer
from ragcore.connectors import ConnectorError, execute
from ragcore.evaluate import evidence_errors
from ragcore.models import _parameter_literal
from ragcore.retrieval import route_question


class BusinessQueryTests(unittest.TestCase):
    def test_filters_preserve_scope_and_exact_decimals(self):
        config = {"id":2, "name":"库存", "parameters":[
            {"name":"department", "label":"部门", "type":"string", "choices":["研发一部","研发二部"]},
            {"name":"amount","label":"金额","type":"number"}]}
        self.assertEqual(route_question("研发一部的库存是多少？", [config], "data")["params"], {"department":"研发一部"})
        for text in ["某部门的库存是多少？", "采购处的库存是多少？", "研发一部和研发二部的库存是多少？"]:
            self.assertIn("department", route_question(text, [config], "data")["ambiguous_params"])
        raw = "123456789012345678.010"
        question = "库存 金额=" + raw
        self.assertEqual(route_question(question,[config],"data")["params"]["amount"], raw)
        self.assertEqual(_parameter_literal(question,raw,"number"), raw)
        with self.assertRaises(ValueError):
            _parameter_literal("金额为1e9","1","number")

    def test_operations_require_complete_rows_and_declared_metric(self):
        config = {"metrics":[{"field":"amount","label":"金额","unit":"元","additive":True},
                             {"field":"rate","label":"利润率","unit":"%","additive":False}]}
        rows = [{"项目":"甲","amount":"0.10","rate":"1%"},{"项目":"乙","amount":"0.20","rate":"2%"}]
        source = {"rows":rows,"total_count":2,"complete":True}
        def ask(q,s=source):
            return operation_answer(s,q,matched_metrics(config,q))
        self.assertIn("0.30元", ask("金额合计")[0])
        self.assertTrue(ask("金额合计")[1])
        self.assertFalse(ask("利润率合计")[1])
        self.assertIn("乙", ask("利润率最高")[0])
        self.assertLess(ask("金额排名")[0].index("乙"),ask("金额排名")[0].index("甲"))
        self.assertFalse(ask("金额合计",{**source,"complete":False,"total_count":5})[1])

    def test_confirmed_failure_zero_and_page_totals(self):
        contract = {"success_path":"status.ok","success_value":True,"has_more_path":"page.more","total_path":"page.total"}
        config = {"name":"仓储接口","url":"https://stock.example.test/query","records_path":"items",
                  "response_contract":contract}
        def call(ok,total,more,rows):
            body = json.dumps({"status":{"ok":ok},"page":{"total":total,"more":more},"items":rows}).encode()
            with patch.dict(os.environ,{"RAG_ALLOWED_HOSTS":"stock.example.test"}), patch("ragcore.connectors._get_body",return_value=body):
                return execute(config,{})
        self.assertTrue(call(True,0,False,[])["complete"])
        page = call(True,75,True,[{"库存":1}])
        self.assertEqual((page["returned_count"],page["total_count"],page["complete"]), (1,75,False))
        for ok,total,more in ((False,0,False),(1,0,False),(True,0,True)):
            with self.assertRaises(ConnectorError):
                call(ok,total,more,[])

    def test_http_followup_and_archived_envelope(self):
        env={"RAG_ADMIN_PASSWORD":"fixture","RAG_COOKIE_SECURE":"0","RAG_LLM_BASE_URL":"",
             "RAG_EMBED_BASE_URL":"","RAG_ALLOWED_HOSTS":"stock.example.test"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,env):
            app=create_app(tmp,seed=True)
            with TestClient(app, base_url="http://127.0.0.1") as client:
                client.post("/api/login",json={"password":"fixture"}).raise_for_status()
                def stream(q,**params):
                    response=client.post("/api/query/stream",json={"question":q,**params})
                    response.raise_for_status()
                    done, = [frame for frame in response.text.split("\n\n") if frame.startswith("event: done\n")]
                    return json.loads(done.split("data: ",1)[1])["result"]
                parent=stream("材料研发平台2026年的预算执行率是多少？",mode="data")
                for mode in ("auto","data"):
                    follow=stream("那2025年呢？",parent_id=parent["id"], mode=mode)
                    self.assertEqual(follow["params"],{"project":"材料研发平台","year":2025})
                    self.assertIn("68%",follow["answer"])
                all_projects=stream("那所有项目的预算金额合计呢？",parent_id=parent["id"],mode="auto")
                self.assertEqual(all_projects["params"],{"year":2026})
                self.assertIn("2000万元",all_projects["answer"])
                config=client.post("/api/connectors",json={"name":"库存","url":"https://stock.example.test/inventory",
                       "records_path":"data.rows","response_contract":{"total_path":"data.total"}}).json()
                body=b'{"data":{"rows":[{"amount":0.10}],"total":50}}'
                with patch("ragcore.connectors._get_body",return_value=body):
                    result=stream("库存共有多少条记录？",mode="data",connector_id=config["id"])
                self.assertIn("50 条",result["answer"])
                self.assertEqual(evidence_errors(app.state.store,result),[])
