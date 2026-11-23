"""Regression fixtures for the table, tree, document and OCR import contracts."""
import io
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from ragcore.documents import SUPPORTED_EXTENSIONS, parse_document_report
from test_documents import DRAW, OFFICE, PRES, RELS, WORD, office_zip


def workbook_bytes():
    book = Workbook()
    sheet = book.active
    sheet.title = "统计"
    sheet.append(["库存表（件）"])
    sheet.merge_cells("A1:C1")
    sheet.append(["部门", "期末库存"])
    sheet.merge_cells("B2:C2")
    sheet.append([None, 2023, 2024])
    sheet.append(["一部", 12, 23])
    sheet.append([])
    sheet.append(["部门", "编码", "进度"])
    sheet.append(["一部", 12, 0.175])
    sheet["B7"].number_format = "00000"
    sheet["C7"].number_format = "0.0%"
    sheet.append(["隐藏测试", 3, "=C7"])
    sheet.row_dimensions[8].hidden = True
    hidden = book.create_sheet("草稿")
    hidden.append(["未发布的值"])
    hidden.sheet_state = "hidden"
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


class ImportFormatsTests(unittest.TestCase):
    def test_every_supported_extension_has_a_workflow_row_and_rejects_damaged_input(self):
        guide = (Path(__file__).resolve().parents[1] / "docs" / "import-formats.md").read_text(encoding="utf-8")
        documented = re.findall(r"^\| `(\.[a-z0-9]+)` \|", guide, re.M)
        self.assertEqual(sorted(documented), sorted(SUPPORTED_EXTENSIONS))
        for suffix in sorted(SUPPORTED_EXTENSIONS):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                parse_document_report("damaged" + suffix, b"\x00invalid")

    def test_workbook_regions_coordinates_formats_and_visibility(self):
        blob = workbook_bytes()
        report = parse_document_report("统计.xlsx", blob)
        rows = {row["locator"]:row for row in report["chunks"]}
        first = rows["工作表「统计」 · A4:C4"]
        self.assertEqual(first["context"], "库存表（件）")
        self.assertIn("期末库存 / 2024：23", first["text"])
        self.assertEqual(first["metadata"]["cells"][-1]["header_cells"], ["B2", "C3"])
        second = rows["工作表「统计」 · A7:C7"]
        self.assertIn("编码：00012", second["text"])
        self.assertIn("进度：17.5%", second["text"])
        self.assertEqual(second["metadata"]["cells"][-1]["raw"], "0.175")
        self.assertNotIn("隐藏测试", str(rows))
        self.assertNotIn("未发布的值", str(rows))
        included = parse_document_report("统计.xlsx", blob, {"include_hidden":True})
        self.assertIn("隐藏测试", str(included["chunks"]))
        self.assertIn("未发布的值", str(included["chunks"]))
        self.assertIn("formula_uncached", [w["code"] for w in included["warnings"]])

    def test_wide_row_chunks_keep_column_boundaries_and_the_row_identity(self):
        book = Workbook()
        sheet = book.active
        sheet.append(["单号"] + [f"指标{n}" for n in range(90)])
        sheet.append(["ZJ-781"] + [str(n) + "单位说明" * 4 for n in range(90)])
        binary = io.BytesIO()
        book.save(binary)
        book.close()
        report = parse_document_report("月报.xlsx", binary.getvalue(), {"header_rows":[1]})
        rows = [r for r in report["chunks"] if r.get("metadata", {}).get("row") == 2]
        self.assertGreater(len(rows), 1)
        addresses = []
        for chunk in rows:
            self.assertIn("单号：ZJ-781", chunk["context"])
            cells = chunk["metadata"]["cells"]
            self.assertIn(cells[0]["address"] + ":" + cells[-1]["address"], chunk["locator"])
            self.assertTrue(all(cell["label"] + "：" in chunk["text"] for cell in cells))
            addresses.extend(cell["address"] for cell in cells)
        self.assertEqual(len(set(addresses)), 91)

    def test_delimited_input_supports_explicit_and_no_headers(self):
        for suffix, separator in [("csv",","), ("tsv","\t")]:
            with self.subTest(suffix=suffix):
                blob = ("备注\n项目;数量\n一部;0").replace(";", separator).encode()
                report = parse_document_report("表."+suffix, blob, {"header_rows":[2]})
                self.assertIn("数量：0", report["chunks"][-1]["text"])
                report = parse_document_report("表."+suffix, blob, {"headerless":True})
                self.assertIn("第 2 列：0", report["chunks"][-1]["text"])
                with self.assertRaises(ValueError):
                    parse_document_report("表."+suffix, blob, {"header_rows":[2], "headerless":True})

    def test_table_continuations_repeated_headers_and_notes(self):
        blob = "部门,姓名\n一部,甲\n\n二部,乙\n部门,姓名\n三部,丙\n来源：名册".encode()
        chunks = parse_document_report("名单.csv",blob)["chunks"]
        self.assertIn("部门：二部 ｜ 姓名：乙",[r["text"] for r in chunks])
        self.assertFalse(any("部门：部门" in r["text"] for r in chunks))
        self.assertEqual(chunks[-1]["text"], "来源：名册")
        self.assertEqual(chunks[-1]["metadata"]["role"], "note")

    def test_excel_vertical_merge_and_hidden_column(self):
        book = Workbook()
        sheet = book.active
        for values in [["部门","批次","说明","金额"],["甲",1,"未发布内部值",17],[None,2,"#REF!",19]]:
            sheet.append(values)
        sheet.merge_cells("A2:A3")
        sheet.column_dimensions["C"].hidden = True
        data = io.BytesIO()
        book.save(data)
        book.close()
        r = parse_document_report("单据.xlsx",data.getvalue())
        last = r["chunks"][-1]
        self.assertIn("部门：甲 ｜ 批次：2",last["text"])
        self.assertEqual(last["metadata"]["cells"][0]["value_cell"],"A2")
        self.assertNotIn("未发布内部值",str(r))
        exposed = parse_document_report("单据.xlsx",data.getvalue(),{"include_hidden":True})
        self.assertIn("未发布内部值",str(exposed["chunks"]))
        self.assertIn("cell_error",[w["code"] for w in exposed["warnings"]])

    def test_text_markdown_and_html_sections(self):
        plain = parse_document_report("案.txt", "甲乙\r\n\r\n丙丁".encode("gb18030"))
        self.assertEqual(plain["chunks"][-1]["locator"], "第 3 行")
        md = parse_document_report("案.md", "## 甲类\n\n应在 3 日内完成。\n\n## 乙类\n\n应在 8 日内完成。".encode())
        self.assertEqual(md["chunks"][1]["context"], "甲类")
        self.assertEqual(md["chunks"][-1]["context"], "乙类")
        setext = parse_document_report("标题.md","## 甲类\n\n三日内提交。\n\n乙类\n----\n\n五日内提交。".encode())
        self.assertEqual(setext["chunks"][-1]["context"],"乙类")
        md_table = parse_document_report("表.md", "## 指标\n\n|名称|件数|\n|---|---:|\n|甲|2|".encode())
        self.assertEqual((md_table["chunks"][-1]["text"],md_table["chunks"][-1]["context"]),
                         ("名称：甲 ｜ 件数：2", "指标"))
        html = '<h2>年度库存</h2><table><tr><th rowspan="2">项目</th><th colspan="2">年末</th></tr><tr><th>2023</th><th>2024</th></tr><tr><td>甲</td><td>8</td><td>11</td></tr></table>'
        for ext in ("html","htm"):
            data = parse_document_report("表."+ext, html.encode())["chunks"][-1]
            self.assertIn("年末 / 2024：11", data["text"])
            self.assertEqual(data["context"], "年度库存")
        body = "<table><tr><th>门店</th><th>月</th><th>件数</th></tr><tr><td rowspan='2'>甲店</td><td>一月</td><td>3</td></tr><tr><td>二月</td><td>5</td></tr></table>"
        self.assertIn("门店：甲店 ｜ 月：二月 ｜ 件数：5",parse_document_report("月报.html",body.encode())["chunks"][-1]["text"])

    def test_json_identity_never_crosses_array_elements_or_lines(self):
        record = [{"name":"甲", "start":{"date":"20280201"}},{"name":"乙","start":{"date":"20290302"}}]
        for ext in ("json","jsonl","ndjson"):
            content = json.dumps(record,ensure_ascii=False) if ext == "json" else "\n".join(
                json.dumps([r],ensure_ascii=False) for r in record)
            chunks = parse_document_report("日期."+ext, content.encode())["chunks"]
            for row in chunks:
                if '"date":' in row["text"]:
                    self.assertIn("甲" if "2028" in row["text"] else "乙",row["context"])
                    self.assertNotIn("乙" if "2028" in row["text"] else "甲",row["context"])
                    self.assertTrue(row["metadata"]["pointer"].endswith("/start"))
                    self.assertTrue(row["locator"].startswith("JSON" if ext == "json" else "第 "))

    def test_nested_json_codes_identify_only_their_own_record(self):
        assets = [{"asset_code":code, "updated_at":"周五", "valid":"无关", "statusCode":"保留在本节点",
                   "randomNumber":867, "一般属性":"备用", "metrics/pressure":{"冷却压力":value}}
                  for code, value in (("BX-17",78), ("BX-27",42))]
        record = {"projectID":301, "assets":assets}
        for ext in ("json","jsonl","ndjson"):
            chunks = parse_document_report("设备." + ext,json.dumps(record,ensure_ascii=False).encode())["chunks"]
            for index, asset in enumerate(assets):
                path = f"/assets/{index}"
                leaf = next(c for c in chunks if c["metadata"]["pointer"] == path + "/metrics~1pressure")
                self.assertEqual(leaf["metadata"]["record_pointer"], path)
                self.assertEqual(leaf["metadata"]["record_identifiers"], [
                    {"key":"projectID","value":"301","pointer":"/projectID"},
                    {"key":"asset_code","value":asset["asset_code"],"pointer":path + "/asset_code"}])
                self.assertIn("projectID=301",leaf["context"])
                self.assertIn("asset_code=" + asset["asset_code"],leaf["context"])
                self.assertNotIn(assets[1-index]["asset_code"],leaf["context"])
                self.assertNotIn("周五",leaf["context"])
                self.assertNotIn("备用",leaf["context"])
                self.assertEqual(json.loads(leaf["text"]),asset["metrics/pressure"])

    def test_word_headings_and_table_title(self):
        def p(value):
            return f'<w:p><w:r><w:t>{value}</w:t></w:r></w:p>'
        def row(*cells):
            return '<w:tr>'+''.join('<w:tc>'+p(c)+'</w:tc>' for c in cells)+'</w:tr>'
        heads = '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>入库</w:t></w:r></w:p>'
        table = '<w:tbl>'+row("年报（件）")+row("项目","数量")+row("甲","17")+'</w:tbl>'
        data = office_zip("word/document.xml",f'<w:document xmlns:w="{WORD}"><w:body>{heads}{p("核对明细。")}{table}</w:body></w:document>')
        chunks = parse_document_report("说明.docx",data)["chunks"]
        self.assertEqual(chunks[1]["context"], "入库")
        self.assertEqual(chunks[-1]["context"], "入库 / 年报（件）")
        self.assertIn("数量：17", chunks[-1]["text"])

    def test_every_raster_alias_reports_missing_frames(self):
        extensions = {".png",".jpg",".jpeg",".tif",".tiff"}
        self.assertTrue(extensions.issubset(SUPPORTED_EXTENSIONS))
        for ext in extensions:
            with self.subTest(ext=ext), patch("ragcore.ocr.image_pages",return_value=[(1, "测试 12"),(2,"")]):
                report = parse_document_report("资料"+ext,b"controlled-image")
                self.assertEqual(report["coverage"]["ocr_pages"], [1])
                self.assertEqual(report["coverage"]["skipped_pages"], [2])
                self.assertEqual(report["coverage"]["status"], "partial")
                self.assertEqual(report["warnings"][0]["code"], "ocr_no_text")

    def test_reports_separate_indexed_and_reviewable_documents(self):
        self.assertEqual(parse_document_report("正文.txt", "交接后签收。".encode())["coverage"]["status"], "indexed")
        self.assertEqual(parse_document_report("统计.xlsx", workbook_bytes())["coverage"]["status"], "review")

    def test_mixed_pdf_distinguishes_unread_pages_and_supplemental_ocr(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=80, height=80)
        font = DictionaryObject({NameObject("/Type"):NameObject("/Font"), NameObject("/Subtype"):NameObject("/Type1"),
                                 NameObject("/BaseFont"):NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):font})})
        native = DecodedStreamObject()
        native.set_data(b"BT /F1 12 Tf (native) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(native)
        scanned = writer.add_blank_page(width=80, height=80)
        content = DecodedStreamObject()
        content.set_data(b"q 2 2 4 4 re f Q")
        scanned[NameObject("/Contents")] = writer._add_object(content)
        out = io.BytesIO()
        writer.write(out)
        with patch("ragcore.ocr.enabled",return_value=False):
            report = parse_document_report("混合.pdf",out.getvalue())
        self.assertEqual(report["coverage"]["skipped_pages"], [2])
        self.assertEqual(report["coverage"]["text_pages"], [1])
        with patch("ragcore.ocr.enabled",return_value=True), patch("ragcore.ocr.pdf_pages",return_value=[(1,"图中文字")]):
            supplemented = parse_document_report("混合.pdf",out.getvalue(),{"ocr_pages":[1]})
        self.assertEqual(supplemented["coverage"]["ocr_pages"], [1])
        self.assertEqual(supplemented["coverage"]["skipped_pages"], [2])
        self.assertEqual([c["text"] for c in supplemented["chunks"]], ["native","图中文字"])

    def test_slides_keep_table_coordinates_and_skip_hidden_page(self):
        main = f'<p:presentation xmlns:p="{PRES}" xmlns:r="{OFFICE}"><p:sldIdLst><p:sldId r:id="one"/><p:sldId r:id="two"/></p:sldIdLst></p:presentation>'
        links = (f'<Relationships xmlns="{RELS}"><Relationship Id="one" Type="{OFFICE}/slide" Target="slides/one.xml"/>'
                 f'<Relationship Id="two" Type="{OFFICE}/slide" Target="slides/two.xml"/></Relationships>')
        cell = lambda v: '<a:tc><a:txBody><a:p><a:r><a:t>'+v+'</a:t></a:r></a:p></a:txBody></a:tc>'
        table = '<a:tbl><a:tr>'+cell("项目")+cell("件数")+'</a:tr><a:tr>'+cell("甲")+cell("18")+'</a:tr></a:tbl>'
        page = f'<p:sld xmlns:p="{PRES}" xmlns:a="{DRAW}"><p:cSld><p:spTree><p:graphicFrame>{table}</p:graphicFrame></p:spTree></p:cSld></p:sld>'
        data = office_zip("ppt/presentation.xml",main, {"ppt/_rels/presentation.xml.rels":links,
                          "ppt/slides/one.xml":page, "ppt/slides/two.xml":page.replace("<p:sld ","<p:sld show='0' ",1)})
        report = parse_document_report("表.pptx",data)
        self.assertIn("件数：18",report["chunks"][-1]["text"])
        self.assertIn("第 1 张幻灯片",report["chunks"][-1]["locator"])
        self.assertEqual(report["warnings"][0]["code"],"slide_hidden")
