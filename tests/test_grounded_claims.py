import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from ragcore import models
from ragcore.model_config import Settings
from ragcore.answer_blocks import AnswerBlocks, ModelAnswerError, evidence_sources
from ragcore.answer_text import document_paragraphs
from ragcore.documents import parse_document_report
from ragcore.retrieval import grounded_excerpts, rank_chunks, rank_document_chunks


def feed(question, source_text, body, quote=None):
    sources = [{"id": "S1", "kind": "document", "title": "手册", "excerpt": source_text, "highlights": []}]
    parser = AnswerBlocks(evidence_sources(sources, question), question, sources)
    encoded = json.dumps({"text": body, "support": [{"id": "S1", "quote": quote or source_text}]}, ensure_ascii=False)
    list(parser.feed(encoded))
    parser.finish()
    return sources[0]["highlights"]


class GroundedClaimTests(unittest.TestCase):
    def test_translated_policies_keep_permission_and_support_polarity(self):
        policies = [
            ("Returns are not allowed.", "本店允许退货。", "本店不允许退货。"),
            ("此处不得拍照。", "Photography is permitted here.", "Photography is prohibited here."),
            ("The lab permits photography.", "实验室禁止拍照。", "实验室允许拍照。"),
            ("本系统支持 PDF。", "PDF is not supported.", "PDF is supported."),
            ("We do not support USB.", "系统支持 USB。", "系统不支持 USB。"),
            ("禁止拖行托盘。", "Dragging pallets is allowed.", "Dragging pallets is forbidden."),
            ("Exchanges are permitted and returns are not allowed.", "允许退货。", "允许换货。"),
            ("本店支持换货，但不支持退货。", "Returns are supported.", "Exchanges are supported."),
            ("Returns are not allowed. The support desk handles exchanges.", "本店允许退货。", "服务台负责处理换货。"),
        ]
        for original, bad, good in policies:
            with self.subTest(original=original):
                with self.assertRaises(ModelAnswerError):
                    feed("政策是什么？",original,bad)
                self.assertTrue(feed("政策是什么？",original,good))
        # One clause's prohibition cannot negate a different, supported action.
        text = "Smoking is prohibited\nPhotography is allowed."
        self.assertTrue(feed("哪些行为允许？",text,"这里允许拍照，禁止吸烟。"))
        with self.assertRaises(ModelAnswerError):
            feed("哪些行为允许？",text,"这里允许吸烟，禁止拍照。")
        self.assertTrue(feed("时间？","The support team is on duty on Monday.","客服团队周一值班。"))
        self.assertTrue(feed("手续？","A parking permit is required.","需要办理停车许可证。"))

    def test_swapped_quantity_and_polarity_and_background_attribution(self):
        pairs = [
            ("出生人口954万人，死亡人口1093万人。", "出生人口1093万人，死亡人口954万人。", "出生人口为954万人，死亡人口为1093万人。"),
            ("库存12件，已售30件。", "库存为30件，已售12件。", "库存为12件。"),
            ("本店不支持退货，但支持换货。", "本店支持退货。", "本店支持换货。"),
            ("Applications do not require receipts.", "Applications require receipts.", "Receipts are not required."),
            ("公报数据为初步汇总数据。部分数据因四舍五入，存在总计与分项合计不等的情况。",
             "主要原因是数据为初步汇总数据，且部分数据因四舍五入，导致总计与分项合计不完全相等。",
             "分项合计不等的原因是四舍五入。"),
        ]
        for source, bad, good in pairs:
            with self.subTest(bad=bad):
                with self.assertRaises(ModelAnswerError):
                    feed("请说明", source, bad)
                self.assertTrue(feed("请说明", source, good))

    def test_one_source_cited_twice_keeps_exact_codepoint_offsets(self):
        text = "🔎 第一章。发票  与\n 合同均须提交。交给财务部。"
        source = {"id":"S1", "kind":"document", "title":"材料清单", "excerpt":text, "highlights":[]}
        blocks = AnswerBlocks(evidence_sources([source], "如何提交？"), "如何提交？", [source])
        for body, quote in [("须交发票与合同。", "发票 与 合同均须提交"), ("交给财务部。", "交给财务部")]:
            list(blocks.feed(json.dumps({"text":body, "support":[{"id":"S1", "quote":quote}]}, ensure_ascii=False)))
        blocks.finish()
        original = []
        for group in source["highlights"]:
            span, = group["ranges"]
            original.append(text[span["start"]:span["end"]])
        self.assertEqual(original, ["发票  与\n 合同均须提交", "交给财务部"])

    def test_local_answer_requires_the_requested_field(self):
        base = {"id":"S1","title":"试用期","excerpt":"试用期转正审批适用于全体员工。"}
        question = "试用期转正审批的负责人是谁？"
        self.assertEqual(document_paragraphs(question, [base]), [])
        self.assertIn("人事主管", document_paragraphs(question,[{**base,"excerpt":"转正审批由人事主管负责。"}])[0])

    def test_english_table_cells_prove_their_date_population_unit_and_period(self):
        row = ("Rank：1 ｜ Geographic Area：New York city, New York ｜ Population Estimate (as of July 1) / 2022：8,335,798"
               " ｜ Population Estimate (as of July 1) / 2023：8,258,035")
        feed("两年各有多少人？", row, "2022 年和 2023 年 7 月 1 日的估计人口分别为 8,335,798 人和 8,258,035 人。")
        feed("两年各有多少人？", row, "2022 年 7 月 1 日的常住人口为 8,335,798 人。", "8,335,798")
        split = feed("两年各有多少人？", row, "2022 年 7 月 1 日有 8,335,798 人。",
                     "New York city, New York ... Population Estimate (as of July 1) / 2022：8,335,798")
        self.assertEqual(len(split[0]["ranges"]), 2)
        with self.assertRaises(ModelAnswerError):
            feed("两年各有多少人？", row, "常住人口为 8,335,798 人。", "Chicago city, Illinois ... 8,335,798")
        for wrong in ("2023 年 7 月 1 日为 8,335,798 人。", "2022 年 5 月 1 日为 8,335,798 人。",
                      "2022 年 7 月 1 日为 8,335,798 万人。", "排名为 1 人。"):
            with self.subTest(wrong=wrong), self.assertRaises(ModelAnswerError):
                feed("两年各有多少人？", row, wrong)

    def test_written_quantities_and_nonquantitative_words(self):
        cases = [("工厂年产量为30吨。", "工厂年产量为一百吨。", "工厂年产量为三十吨。"),
                 ("资金总额为12.5万元。","资金总额为十二点六万元。","资金总额为十二点五万元。"),
                 ("通过率为百分之二十。","通过率为百分之三十。","通过率为20%。")]
        for original, bad, good in cases:
            with self.subTest(original=original):
                with self.assertRaises(ModelAnswerError):
                    feed("统计结果",original,bad)
                self.assertTrue(feed("统计结果",original,good))
        self.assertTrue(feed("如何登记","项目登记处理流程见手册。","同一项目一般按此流程登记。"))
        with self.assertRaises(ModelAnswerError):
            feed("年产量","工厂年产量为30吨。","工厂年产量为一百。")

    def test_duplicate_numeric_quotes_require_the_actual_label(self):
        text = "设备甲：120 ｜ 设备乙：120"
        with self.assertRaises(ModelAnswerError):
            feed("设备乙",text,"设备乙为120。","120")
        with self.assertRaises(ModelAnswerError):
            feed("设备甲","设备甲：1200","设备甲为120。","120")
        span, = feed("设备乙",text,"设备乙为120。","设备乙：120")[0]["ranges"]
        self.assertEqual(text[span["start"]:span["end"]], "设备乙：120")

    def test_requested_entity_keeps_other_column_groups_of_its_physical_record(self):
        header = ["厂站", *(f"辅助字段{i}" for i in range(25)), "应急容量"]
        source = ",".join(header) + "\n" + ",".join(["甲山站", *(["用途说明"*14]*25), "321"])
        chunks = [{**chunk,"id":i,"filename":"记录.csv","document_id":1}
                  for i, chunk in enumerate(parse_document_report("记录.csv",source.encode())["chunks"],1)]
        store = Mock()
        store.candidates.return_value = (chunks, "all")
        store.document_terms.return_value = {}
        q = "甲山站应急容量是多少？"
        with patch.object(models,"embedding_enabled",return_value=False):
            ranking = rank_document_chunks(store,q)
        self.assertIn("应急容量：321",ranking.ranked[0]["text"])
        records = [{**s,"id":f"S{i}","title":"记录.csv","excerpt":s["text"]}
                   for i,s in enumerate(ranking.ranked,1)]
        answer, = document_paragraphs(q,records)
        self.assertIn("321",answer)

    def test_nested_json_metrics_stay_with_the_requested_asset_code(self):
        payload = [{"asset_code":"BX-17","metrics":{"冷却压力":78}},{"asset_code":"BX-27","metrics":{"冷却压力":42}}]
        chunks = [{**item,"id":i,"filename":"传感器.json","document_id":1} for i, item in enumerate(
            parse_document_report("传感器.json",json.dumps(payload,ensure_ascii=False).encode())["chunks"],1)]
        store = Mock()
        store.candidates.return_value = (chunks,"all")
        store.document_terms.return_value = {}
        q = "资产码 BX-27 的冷却压力是多少？"
        with patch.object(models,"embedding_enabled",return_value=False):
            results = rank_document_chunks(store,q)
        records = [{**s,"id":f"S{i}","title":"传感器.json","excerpt":s["text"]}
                   for i,s in enumerate(results.ranked,1)]
        answer, = document_paragraphs(q,records)
        self.assertIn("42",answer)
        self.assertNotIn("78",answer)
        self.assertTrue(all(r["metadata"]["record_pointer"] == "/1" for r in records))

    def test_distinct_reports_and_exact_foreign_labels_remain_available(self):
        docs = [{"id":year, "document_id":year, "filename":f"{year}年公报", "locator":"第1页",
                 "text":"常住人口100万人。"} for year in (2023, 2024)]
        excerpts = grounded_excerpts("比较两年相同的指标", docs)
        self.assertEqual([x["id"] for x in excerpts],[2023, 2024])
        sections = [{"id":1, "filename":"Training", "text":"Training Manual\nIntroduction"},
                    {"id":2, "filename":"Training", "text":"Station 2: Each participant must bring three samples."}]
        self.assertTrue(any(c["id"] == 2 for c in rank_chunks("在 Training Manual 中 Station 2 要带几个样品？", sections)))

    def test_same_filename_in_two_named_sources_respects_requested_department(self):
        docs = [{"id":i, "document_id":i, "filename":"值班制度.md", "source_key":dept,
                 "locator":"第一节", "text":f"值班记录须提交给{recipient}。"}
                for i, dept, recipient in [(1,"业务一部","一部经理"),(2,"业务二部","二部主任")]]
        store = Mock()
        store.document_terms.return_value = {}
        store.candidates.return_value = (docs, "all")
        question = "业务二部的值班记录须提交给谁？"
        with patch.object(models,"embedding_enabled",return_value=False):
            result = rank_document_chunks(store, question)
        source, = result.ranked
        self.assertEqual(source["source_key"],"业务二部")
        citation = {**source,"kind":"document","id":"S1","excerpt":source["text"],"title":source["filename"]}
        self.assertIn("业务二部", document_paragraphs(question, [citation])[0])
        self.assertEqual(evidence_sources([citation], question)["S1"]["source"], "业务二部")

    def test_values_cannot_move_between_distinct_report_years(self):
        sources = [{"id":f"S{i}", "kind":"document", "title":f"{year}年报告", "text":f"库存{value}件"}
                   for i, (year,value) in enumerate([(2023,87),(2024,55)],1)]
        support = [{"id":s["id"],"quote":s["text"]} for s in sources]
        blocks = AnswerBlocks(evidence_sources(sources,"历年库存"),"历年库存")
        wrong = {"text":"2023年库存55件，2024年库存87件。","support":support}
        with self.assertRaises(ModelAnswerError):
            list(blocks.feed(json.dumps(wrong,ensure_ascii=False)))

    def test_typographic_quotes_and_standard_codes_remain_exactly_locatable(self):
        quote = "Earth's systems driven by energy from the sun and the force of gravity."
        text = "S-ESS2-4: Earth’s systems driven by energy from the sun and the force of gravity."
        highlights = feed("What does S-ESS2-4 say?", text,
                          "In S-ESS2-4, the cycle results from energy from the sun and the force of gravity.", quote)
        span, = highlights[0]["ranges"]
        self.assertEqual(text[span["start"]:span["end"]],quote.replace("'", "’"))

    def test_missing_year_uses_only_a_uniquely_dated_report_and_its_quoted_day(self):
        source = {"id":"S1", "kind":"document", "title":"2020年气候公报", "excerpt":"汛期从9月1日至10月12日，平均降水量85毫米。"}
        def check(body):
            parser = AnswerBlocks(evidence_sources([source], "汛期"), "汛期")
            return list(parser.feed(json.dumps({"text":body,"support":[{"id":"S1","quote":source["excerpt"]}]})))
        self.assertTrue(check("2020年9月1日至10月12日平均降水量为85毫米。"))
        for wrong in ("2021年9月1日平均降水量为85毫米。", "2020年9月2日平均降水量为85毫米。"):
            with self.assertRaises(ModelAnswerError):
                check(wrong)

    def test_embedding_provider_with_smaller_batches_can_rank_and_reuse_cache(self):
        settings = Settings("http://127.0.0.1:8000", "fixture", "")
        sent = []
        def post(settings, path, payload, **kwargs):
            inputs = payload["input"]
            sent.append(len(inputs))
            if len(inputs) > 2:
                raise HTTPError(settings.base,413,"fixture size limit",None,None)
            return {"data":[{"index":n,"embedding":[1, 2]} for n in range(len(inputs))]}
        models._VECTORS.clear()
        with patch.object(models,"_post",side_effect=post):
            inputs = ["q","a","b","c","d"]
            self.assertEqual(len(models._embed(settings,inputs)), 5)
            calls = len(sent)
            self.assertEqual(len(models._embed(settings,inputs)), 5)
            self.assertEqual(len(sent),calls)
        self.assertEqual(sent,[5,2,2,1])
