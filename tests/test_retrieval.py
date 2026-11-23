import unittest

from ragcore.retrieval import document_question, filter_evidence, grounded_excerpts, index_terms, rank_chunks, route_question


CONNECTOR = {
    "id": 7, "name": "项目预算执行查询", "url": "demo://budget",
    "keywords": ["经费", "预算", "执行率", "余额", "支出"],
    "description": "按项目与年度查询预算、支出和执行率",
    "parameters": [
        {"name": "year", "label": "年度", "type": "integer", "required": True},
        {"name": "project", "label": "项目名称", "type": "string"},
    ],
}
CHUNKS = [
    {"id": 1, "document_id": 1, "filename": "制度.md", "locator": "第 1 段",
     "text": "差旅报销须提交发票、合同、验收单与支付凭证。"},
    {"id": 2, "document_id": 2, "filename": "财务口径.md", "locator": "第 1 段",
     "text": "预算执行率计算公式为累计支出金额除以年度预算。"},
    {"id": 3, "document_id": 3, "filename": "Travel.txt", "locator": "line 3",
     "text": "Attach the signed invoice when submitting a travel reimbursement."},
]


class RetrievalTests(unittest.TestCase):
    def test_search(self):
        cases = (("报账需要哪些材料？", 1), ("预算执行率如何计算", 2),
                 ("How do I reimburse travel expenses?", 3))
        for question, expected in cases:
            with self.subTest(question=question):
                matches = rank_chunks(question, CHUNKS)
                self.assertEqual(matches[0]["id"], expected)
                excerpt = grounded_excerpts(question, matches)[0]
                self.assertIn(excerpt["excerpt"], excerpt["text"])
                self.assertNotIn("score", CHUNKS[expected - 1])
        self.assertEqual(rank_chunks("蝴蝶为何迁徙", CHUNKS), [])
        self.assertEqual(rank_chunks("how can I", CHUNKS), [])
        self.assertEqual(index_terms("报账，报账").count("报销"), 2)
        self.assertEqual(index_terms("报账，报账").count("报账"), 2)

    def test_route_and_parameters(self):
        plan = route_question("查一下2026年材料研发平台的预算执行率", [CONNECTOR])
        self.assertEqual(plan["intent"], "data")
        self.assertEqual(plan["connector_id"], 7)
        self.assertEqual(plan["params"], {"year": 2026, "project": "材料研发平台"})
        self.assertEqual(route_question("项目预算余额是多少", [CONNECTOR])["missing"], ["年度"])
        self.assertEqual(route_question("预算执行率的口径是什么", [CONNECTOR])["intent"], "documents")
        query = "说明执行率的计算公式，并查询2026年智能制造项目实际执行率"
        self.assertEqual(route_question(query, [CONNECTOR])["intent"], "mixed")
        self.assertEqual(route_question("如何养花", [CONNECTOR])["intent"], "documents")
        plan = route_question("2026年深海项目预算余额是多少", [CONNECTOR])
        self.assertEqual(plan["params"]["project"], "深海项目")
        for project in ("中和科技项目", "甲项目"):
            plan = route_question(f"2026年{project}的预算余额是多少", [CONNECTOR])
            self.assertEqual(plan["params"]["project"], project)

    def test_connectors_and_modes(self):
        self.assertIsNone(route_question("预算余额", [{**CONNECTOR, "enabled": False}], "data")["connector_id"])
        self.assertIsNone(route_question("预算余额", [CONNECTOR, {**CONNECTOR, "id": 8}])["connector_id"])
        self.assertEqual(route_question("unused", [CONNECTOR], "data")["missing"], ["年度"])
        config = {"id": 9, "name": "sales", "keywords": ["sales"], "parameters": [
            {"name": "office", "label": "office", "type": "string", "aliases": ["branch"]},
            {"name": "date", "label": "date", "type": "date", "required": True}]}
        plan = route_question('sales total branch="New York" date=2026-11-03', [config])
        self.assertEqual(plan["params"], {"office": "New York", "date": "2026-11-03"})
        self.assertEqual(route_question("sales total date=2026-02-30", [config])["missing"], ["date"])

    def test_multi_value_scalar_queries_request_clarification(self):
        config = {**CONNECTOR, "parameters": [*CONNECTOR["parameters"][:1],
                  {**CONNECTOR["parameters"][1], "aliases": ["项目"]}]}
        for question, key, keep in (
            ("材料研发平台 2025 年和 2026 年预算执行率对比", "year", {"project": "材料研发平台"}),
            ("对比材料研发平台和智能制造项目 2026 年的执行率", "project", {"year": 2026}),
            ("对比深海项目与远洋平台2026年预算执行率", "project", {"year": 2026}),
        ):
            with self.subTest(question=question):
                plan = route_question(question, [config])
                self.assertEqual(plan["ambiguous_params"], [key])
                self.assertTrue(plan["clarification"])
                self.assertEqual(plan["params"], keep)

    def test_seed_titles_and_mixed_followup(self):
        section = {**CHUNKS[0], "filename": "科研项目经费报销指引.md"}
        title = {**section, "id": 101, "text": "# 科研项目经费报销指引（演示制度）"}
        irrelevant = {**section, "id": 102, "text": "项目负责人由部门指定。"}
        guide = {**section, "id": 103, "text": "## 报销材料\n科研项目经费报销需提交报销申请单、合法有效的发票及支付凭证。"}
        hits = rank_chunks("科研项目经费报销需要哪些材料？", [title, irrelevant, guide])
        self.assertEqual([row["id"] for row in hits], [103])

        question = "材料研发平台 2026 年预算执行率是多少，按什么口径计算？"
        qdoc = document_question(question)
        self.assertEqual(route_question(question, [CONNECTOR])["intent"], "mixed")
        self.assertIn("预算执行率", qdoc)
        self.assertIn("口径", qdoc)
        self.assertNotIn("2026", qdoc)
        self.assertEqual(rank_chunks(qdoc, CHUNKS)[0]["id"], 2)

    def test_unknown_facts_and_positive_numeric_evidence(self):
        unrelated = {**CHUNKS[0], "text": "报销申请须在2026年12月1日提交发票和支付凭证。"}
        maximum = {**CHUNKS[0], "text": "住宿报销标准为每人每天不超过480元。"}
        q = "科研项目经费报销每人每天上限是多少？"
        self.assertEqual(filter_evidence(q, [unrelated, maximum]), [maximum])
        q = "2026年材料研发平台的预算执行率是多少"
        actual = {**CHUNKS[1], "filename": "2026年度预算数据.csv", "text": "材料研发平台的预算执行率为72%。"}
        self.assertEqual(filter_evidence(q, [CHUNKS[1], actual]), [actual])
        self.assertEqual(filter_evidence("预算执行率是多少？", [
            {**CHUNKS[1], "text": "预算执行率＝已支出÷预算×100%。"}]), [])
        phone = {**CHUNKS[0], "text": "采购部门热线电话：0512-12345678。"}
        self.assertEqual(filter_evidence("采购的联系电话是多少？", [unrelated, phone]), [phone])


if __name__ == "__main__":
    unittest.main()
