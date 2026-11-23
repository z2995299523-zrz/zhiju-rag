import unittest

from ragcore.answer_text import data_paragraphs, document_paragraphs


class AnswerTextTests(unittest.TestCase):
    def test_multirow_keeps_entity_metric_and_unit_together(self):
        rows = [{"仓库名称": "一区", "库存（件）": 0, "备注": "待盘点"},
                {"仓库名称": "二区", "库存（件）": 25, "备注": "已盘点"}]
        summary, items = data_paragraphs({"id": "S3", "rows": rows}, "比较各仓库的库存")
        self.assertIn("2 条记录", summary)
        self.assertEqual(items.splitlines(), ["1. 仓库名称为一区，库存为 0 件。 [S3]",
                                              "2. 仓库名称为二区，库存为 25 件。 [S3]"])

    def test_scalar_field_and_nested_output(self):
        row = {"有效": False, "详情": {"记录": [1, 2]}}
        result, = data_paragraphs({"id": "S1", "rows": [row]}, "是否有效？")
        self.assertEqual(result, "有效为否。 [S1]")
        result, = data_paragraphs({"id": "S1", "rows": []}, "有多少条？")
        self.assertIn("0 条记录", result)

    def test_json_identifier_alone_is_not_a_named_business_field(self):
        source = {"id": "S2", "title": "记录.json", "excerpt": '{"id":"20261101-X","数量":12}'}
        self.assertEqual(document_paragraphs("活动几号开始？", [source]), [])
        self.assertEqual(document_paragraphs("数量有多少？", [source]), ["数量为 12。 [S2]"])
