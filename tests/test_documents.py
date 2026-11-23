import io
import unittest
import zipfile

from ragcore.documents import parse_document


RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DRAW = "http://schemas.openxmlformats.org/drawingml/2006/main"
PRES = "http://schemas.openxmlformats.org/presentationml/2006/main"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def office_zip(main, xml, extra=None):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("_rels/.rels", f'<Relationships xmlns="{RELS}"><Relationship Id="rId1" Type="{OFFICE}/officeDocument" Target="{main}"/></Relationships>')
        archive.writestr(main, xml)
        for path, value in (extra or {}).items():
            archive.writestr(path, value)
    return result.getvalue()


class ExtractionTests(unittest.TestCase):
    def test_chinese_text_locators_and_chunk_boundaries(self):
        result = parse_document("a.MD", "# 支出规则\r\n\r\n## 预算\r\n项目支出应核验合同。\r\n".encode("gb18030"))
        self.assertEqual(result, [
            {"locator": "第 1 行", "text": "# 支出规则"},
            {"locator": "第 3–4 行", "text": "## 预算\n项目支出应核验合同。"},
        ])
        fragments = parse_document("long.txt", ("甲" * 2222).encode())
        self.assertEqual([len(e["text"]) for e in fragments], [1000, 1000, 222])
        self.assertTrue(fragments[-1]["locator"].endswith("2001–2222"))

    def test_delimited_multiline_and_duplicate_headings(self):
        report = '部门,说明,金额,金额\n科研,"一行,\n二行",0,200\n'
        entry = parse_document("a.csv", report.encode())[1]
        self.assertEqual(entry["locator"], "第 2–3 行")
        self.assertIn("一行,\n二行", entry["text"])
        self.assertIn("金额：0", entry["text"])
        self.assertIn("金额（第 4 列）：200", entry["text"])
        self.assertIn("名字：甲", parse_document("a.tsv", "名字\t数量\n甲\t2".encode())[1]["text"])

    def test_json_precise_paths_and_no_silent_overwrites(self):
        result = parse_document("a.json", '{"items/~":[{"金额":20},{"金额":30}]}'.encode())
        self.assertEqual([entry["locator"] for entry in result], [
            "JSON · JSON Pointer /items~1~0/0", "JSON · JSON Pointer /items~1~0/1"])
        self.assertIn("30", result[1]["text"])
        precise = parse_document("money.json", b'{"amount":123456789.9876543210,"rate":1.23e-18}')
        self.assertIn('123456789.9876543210', precise[0]["text"])
        self.assertIn('1.23e-18', precise[0]["text"])
        result = parse_document("logs.ndjson", b'{"count":0}\n\n{"count":1}')
        self.assertTrue(result[1]["locator"].startswith("第 3 行"))
        with self.assertRaisesRegex(ValueError, "重复字段"):
            parse_document("bad.json", b'{"n":1,"n":2}')
        with self.assertRaisesRegex(ValueError, "第 2 行"):
            parse_document("bad.jsonl", b'{"n":1}\n!bad')

    def test_html_excludes_embedded_code(self):
        source = "<head><title>skip</title></head>\n<body><p>预算 <b>100 万</b></p>\n<script>x=1</script><p>经费</p></body>"
        result = parse_document("a.html", source.encode())
        self.assertEqual([row["text"] for row in result], ["预算 100 万", "经费"])
        self.assertIn("第 2 行", result[0]["locator"])

    def test_word_tables_and_body(self):
        def para(text):
            return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"

        def table_row(*values):
            return "<w:tr>" + "".join(f"<w:tc>{para(v)}</w:tc>" for v in values) + "</w:tr>"

        table = "<w:tbl>" + table_row("部门", "额度") + table_row("采购部", "56.5") + "</w:tbl>"
        xml = f'<w:document xmlns:w="{WORD}"><w:body>{para("部门管理办法")}{table}</w:body></w:document>'
        result = parse_document("a.docx", office_zip("word/document.xml", xml))
        self.assertEqual(result[0], {"locator": "正文第 1 段", "text": "部门管理办法"})
        self.assertEqual(result[-1], {"locator": "表格 1 · 第 2 行", "text": "部门：采购部 ｜ 额度：56.5"})

    def test_powerpoint_display_order_comes_from_manifest(self):
        main = f'<p:presentation xmlns:p="{PRES}" xmlns:r="{OFFICE}"><p:sldIdLst><p:sldId r:id="b"/><p:sldId r:id="a"/></p:sldIdLst></p:presentation>'
        links = f'<Relationships xmlns="{RELS}"><Relationship Id="a" Type="{OFFICE}/slide" Target="slides/slide1.xml"/><Relationship Id="b" Type="{OFFICE}/slide" Target="slides/slide2.xml"/></Relationships>'

        def slide(text):
            return f'<p:sld xmlns:p="{PRES}" xmlns:a="{DRAW}"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'

        data = office_zip("ppt/presentation.xml", main, {"ppt/_rels/presentation.xml.rels": links,
            "ppt/slides/slide1.xml": slide("次页"), "ppt/slides/slide2.xml": slide("首页")})
        result = parse_document("a.pptx", data)
        self.assertEqual(result, [{"locator": "第 1 张幻灯片", "text": "首页"},
                                  {"locator": "第 2 张幻灯片", "text": "次页"}])

    def test_excel_references_and_uncached_formula(self):
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.title = "科研预算"
        sheet.append(["项目", "结余", "预算"])
        sheet.append(["星河", 0, "=B2+10"])
        output = io.BytesIO()
        book.save(output)
        result = parse_document("a.xlsx", output.getvalue())
        self.assertEqual(result[1]["locator"], "工作表「科研预算」 · A2:C2")
        self.assertIn("结余：0", result[1]["text"])
        self.assertIn("未保存计算结果", result[1]["text"])
        sheet["XFD2"] = 11
        output = io.BytesIO()
        book.save(output)
        with self.assertRaisesRegex(ValueError, "512 列"):
            parse_document("wide.xlsx", output.getvalue())

    def test_pdf_page_text_and_image_only_detection(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=500, height=500)
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaisesRegex(ValueError, "OCR"):
            parse_document("scan.pdf", output.getvalue())
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 100 Td (Approved budget) Tj ET")
        page[NameObject("/Contents")] = stream
        output = io.BytesIO()
        writer.write(output)
        self.assertEqual(parse_document("budget.pdf", output.getvalue()),
                         [{"locator":"第 1 页", "text":"Approved budget"}])

    def test_invalid_inputs(self):
        for name, content in [("blank.txt", b"  "), ("not-office.docx", b"hello"), ("x.exe", b"x"),
                              ("binary.txt", b"\x00hello"), ("x.json", b'"\\ud800"')]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    parse_document(name, content)
        xml = f'<!DOCTYPE w:document [<!ENTITY obj "unsafe">]><w:document xmlns:w="{WORD}"><w:body/></w:document>'
        with self.assertRaisesRegex(ValueError, "XML"):
            parse_document("a.docx", office_zip("word/document.xml", xml))
