"""Extract searchable text with locators in the original, uploaded file."""

import codecs
import csv
import io
import json
import posixpath
import re
import zipfile
from datetime import date, time
from html.parser import HTMLParser
from pathlib import PurePath
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree as safe_xml
from defusedxml.common import DefusedXmlException

from . import ocr

SUPPORTED_EXTENSIONS = frozenset({
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".ndjson",
    ".html", ".htm", ".pdf", ".docx", ".xlsx", ".pptx",
}) | ocr.IMAGE_EXTENSIONS
MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_EXPANDED_BYTES = 64 * 1024 * 1024
_MAX_MEMBER_BYTES = 16 * 1024 * 1024
_MAX_SEGMENTS = 20_000
_MAX_TEXT_CHARS = 4_000_000
_CHUNK_SIZE = 1000
_MAX_COLUMNS = 512
_MAX_ROWS = 50_000

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


class _Evidence:
    def __init__(self):
        self.rows = []
        self.chars = 0

    def add(self, locator, value):
        value = value.strip()
        if not value:
            return
        if len(locator) > 4096:
            raise ValueError("来源位置标识超过 4096 个字符")
        try:
            locator.encode("utf-8")
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("文本含无效 Unicode 字符") from exc
        self.chars += len(value) + len(locator)
        if self.chars > _MAX_TEXT_CHARS:
            raise ValueError("文档可索引文本超过 400 万字符，请拆分文件")
        start = 0
        while start < len(value):
            end = min(start + _CHUNK_SIZE, len(value))
            if end < len(value):
                cut = max(value.rfind(s, start + _CHUNK_SIZE // 2, end)
                          for s in ("\n", "。", "！", "？", "；", " ", "，"))
                if cut >= 0:
                    end = cut + 1
            excerpt = value[start:end].strip()
            if excerpt:
                position = locator
                if len(value) > _CHUNK_SIZE:
                    position += f" · 文本字符 {start + 1}–{end}"
                if len(self.rows) >= _MAX_SEGMENTS:
                    raise ValueError("文档超过 20000 个证据片段，请拆分文件")
                self.rows.append({"locator": position, "text": excerpt})
            start = end


def _decode(data):
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encodings = ("utf-32",)
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings = ("utf-16",)
    else:
        encodings = ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            value = data.decode(encoding)
        except UnicodeError:
            continue
        if "\x00" in value or re.search(r"[\x01-\x08\x0e-\x1f]", value):
            raise ValueError("文件含二进制控制字符，请提供文本文件或正确的文件扩展名")
        return value
    raise ValueError("无法读取文本编码，请使用 UTF-8、GB18030 或带 BOM 的 UTF-16")


def _line_range(start, end):
    return f"第 {start} 行" if start == end else f"第 {start}–{end} 行"


def _plain(evidence, value, markdown=False):
    pending = []
    length = 0
    start = 0
    for number, line in enumerate(io.StringIO(value, newline=None), 1):
        line = line.rstrip("\n")
        heading = markdown and re.match(r"^#{1,6}\s", line)
        if pending and (not line.strip() or heading or length + len(line) > _CHUNK_SIZE):
            evidence.add(_line_range(start, number - 1), "\n".join(pending))
            pending, length = [], 0
        if line.strip():
            if not pending:
                start = number
            pending.append(line)
            length += len(line) + 1
    if pending:
        evidence.add(_line_range(start, start + len(pending) - 1), "\n".join(pending))


def _display(value):
    if value is None:
        return ""
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value).strip()


def _headers(values):
    labels = [_display(value) for value in values]
    seen = set()
    result = []
    for idx, label in enumerate(labels, 1):
        if not label:
            label = f"第 {idx} 列"
        if label in seen:
            label += f"（第 {idx} 列）"
        seen.add(label)
        result.append(label)
    return result


def _fields(labels, values):
    return " ｜ ".join(
        f"{labels[i] if i < len(labels) else f'第 {i + 1} 列'}：{value}"
        for i, raw in enumerate(values) if (value := _display(raw))
    )


def _delimited(evidence, value, delimiter):
    reader = csv.reader(io.StringIO(value, newline=""), delimiter=delimiter, strict=True)
    labels = None
    try:
        for record in range(1, _MAX_ROWS + 2):
            start = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                return
            if record > _MAX_ROWS or len(row) > _MAX_COLUMNS:
                raise ValueError("表格超过 50000 条记录或 512 列，请拆分文件")
            if not any(cell.strip() for cell in row):
                continue
            locator = _line_range(start, reader.line_num)
            if labels is None:
                labels = _headers(row)
                evidence.add(locator + "（表头）", " ｜ ".join(labels))
            else:
                evidence.add(locator, _fields(labels, row))
    except csv.Error as exc:
        raise ValueError(f"第 {reader.line_num} 行附近的表格字段过长或引号格式错误") from exc


class _JsonNumber(str):
    """The numeric token as written, including trailing zeros and exponent."""


def _read_json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("JSON 含重复字段，无法唯一定位原始值")
            result[key] = item
        return result

    def invalid_constant(_):
        raise ValueError("JSON 含非标准数值")

    return json.loads(value, object_pairs_hook=unique, parse_constant=invalid_constant,
                      parse_int=_JsonNumber, parse_float=_JsonNumber)


def _json_scalar(value):
    if isinstance(value, _JsonNumber):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _json_nodes(evidence, node, prefix, pointer="", depth=0):
    if depth > 64:
        raise ValueError("JSON 嵌套超过 64 层")
    if len(pointer) > 4000:
        raise ValueError("JSON 字段路径过长")
    locator = f"{prefix} · JSON Pointer {pointer}" if pointer else f"{prefix} · 根节点"
    if isinstance(node, list) and node:
        for i, item in enumerate(node):
            _json_nodes(evidence, item, prefix, f"{pointer}/{i}", depth + 1)
    elif isinstance(node, dict) and node:
        scalar = {}
        for key, item in node.items():
            if isinstance(item, (list, dict)) and item:
                escaped = key.replace("~", "~0").replace("/", "~1")
                _json_nodes(evidence, item, prefix, f"{pointer}/{escaped}", depth + 1)
            else:
                scalar[key] = item
        if scalar:
            pairs = (f"{json.dumps(key, ensure_ascii=False)}: {_json_scalar(item)}"
                     for key, item in scalar.items())
            evidence.add(locator, "{" + ", ".join(pairs) + "}")
    else:
        evidence.add(locator, _json_scalar(node))


class _HtmlText(HTMLParser):
    blocks = set("address article aside blockquote br dd div dl dt figcaption footer form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tr ul".split())
    hidden = {"head", "script", "style", "template", "noscript", "svg"}

    def __init__(self, evidence):
        super().__init__(convert_charrefs=True)
        self.evidence = evidence
        self.skip = []
        self.pending = []
        self.start = self.end = self.count = 0

    def flush(self):
        value = re.sub(r"\s+", " ", "".join(self.pending)).strip()
        if value:
            self.count += 1
            self.evidence.add(f"文本块 {self.count} · {_line_range(self.start, self.end)}", value)
        self.pending.clear()

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden:
            self.skip.append(tag)
        if not self.skip:
            if tag in self.blocks:
                self.flush()
            elif tag in {"td", "th"} and self.pending:
                self.pending.append(" ｜ ")

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
        elif tag in self.blocks:
            self.flush()

    def handle_data(self, data):
        if not self.skip:
            if not self.pending:
                self.start = self.getpos()[0]
            self.end = self.getpos()[0] + data.count("\n")
            self.pending.append(data)


def _checked_archive(data):
    archive = zipfile.ZipFile(io.BytesIO(data))
    try:
        entries = archive.infolist()
        if len(entries) > 4096 or sum(i.file_size for i in entries) > _MAX_EXPANDED_BYTES:
            raise ValueError("Office 文件解包后超过 64 MiB 或包含过多部件")
        names = set()
        for item in entries:
            if item.file_size > _MAX_MEMBER_BYTES:
                raise ValueError("Office 文件单个部件超过 16 MiB，请拆分文件")
            if item.flag_bits & 1:
                raise ValueError("请先解除 Office 文件加密")
            if item.filename in names:
                raise ValueError("Office 文件含重名部件")
            names.add(item.filename)
        return archive
    except Exception:
        archive.close()
        raise


def _xml(archive, name):
    return safe_xml.fromstring(archive.read(name), forbid_dtd=True)


def _relationships(archive, owner):
    folder, name = posixpath.split(owner)
    path = posixpath.join(folder, "_rels", name + ".rels")
    result = {}
    for link in _xml(archive, path):
        if link.get("TargetMode") == "External":
            continue
        target = link.get("Target", "")
        resolved = posixpath.normpath(posixpath.join(folder, target)).lstrip("/")
        if resolved.startswith("../") or not target:
            raise ValueError("Office 部件路径无效")
        result[link.attrib["Id"]] = (link.get("Type", "").split("/")[-1], resolved)
    return result


def _main_part(archive):
    for kind, part in _relationships(archive, "").values():
        if kind == "officeDocument":
            return part
    raise ValueError("Office 文件缺少主文档")


def _word_text(paragraph):
    ns = "{" + _NS["w"] + "}"
    parts = []
    for child in paragraph.iter():
        if child.tag == ns + "t":
            parts.append(child.text or "")
        elif child.tag == ns + "tab":
            parts.append("\t")
        elif child.tag in {ns + "br", ns + "cr"}:
            parts.append("\n")
    return "".join(parts).strip()


def _docx(evidence, archive):
    body = _xml(archive, _main_part(archive)).find("w:body", _NS)
    if body is None:
        raise ValueError("Word 文件缺少正文")
    para = table = 0
    ns = "{" + _NS["w"] + "}"

    def walk(block):
        nonlocal para, table
        for elem in block:
            if elem.tag == ns + "p":
                para += 1
                evidence.add(f"正文第 {para} 段", _word_text(elem))
            elif elem.tag == ns + "tbl":
                table += 1
                table_number = table
                labels = None
                for i, row in enumerate(elem.findall("w:tr", _NS), 1):
                    values = ["\n".join(filter(None, (_word_text(p) for p in cell.findall("w:p", _NS))))
                              for cell in row.findall("w:tc", _NS)]
                    if len(values) > _MAX_COLUMNS or i > _MAX_ROWS:
                        raise ValueError("Word 表格超出解析上限")
                    locator = f"表格 {table_number} · 第 {i} 行"
                    if labels is None and any(values):
                        labels = _headers(values)
                        evidence.add(locator, " ｜ ".join(labels))
                    elif labels is not None:
                        evidence.add(locator, _fields(labels, values))
                    for cell in row.findall("w:tc", _NS):
                        for nested in cell.findall("w:tbl", _NS):
                            # Reuse traversal for nested tables without reading cell paragraphs twice.
                            walk([nested])
            elif elem.tag in {ns + "sdt", ns + "sdtContent", ns + "customXml", ns + "ins"}:
                walk(elem)
    walk(body)


def _pptx(evidence, archive):
    main = _main_part(archive)
    slides = _xml(archive, main).findall("p:sldIdLst/p:sldId", _NS)
    links = _relationships(archive, main)
    if len(slides) > 1200:
        raise ValueError("演示文稿超过 1200 张幻灯片")
    for number, slide in enumerate(slides, 1):
        kind, path = links[slide.attrib["{" + _NS["r"] + "}id"]]
        if kind != "slide":
            raise ValueError("幻灯片引用无效")
        tree = _xml(archive, path).find("p:cSld/p:spTree", _NS)
        if tree is None:
            continue
        paragraphs = []
        for p in tree.iterfind(".//a:p", _NS):
            parts = []
            for run in p.iter():
                if run.tag == "{" + _NS["a"] + "}t":
                    parts.append(run.text or "")
                elif run.tag == "{" + _NS["a"] + "}br":
                    parts.append("\n")
            if parts:
                paragraphs.append("".join(parts))
        evidence.add(f"第 {number} 张幻灯片", "\n".join(paragraphs))


def _xlsx(evidence, archive, data):
    from openpyxl import load_workbook
    from openpyxl.utils import coordinate_to_tuple, get_column_letter

    # Reject DTD/entity declarations even when a system's XML backend changes.
    sheet_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    for part in archive.namelist():
        if part.lower().endswith((".xml", ".rels")):
            with archive.open(part) as stream:
                for _, element in safe_xml.iterparse(stream, forbid_dtd=True):
                    if element.tag == sheet_ns + "c" and (ref := element.get("r")):
                        row_num, col_num = coordinate_to_tuple(ref)
                        if not (1 <= row_num <= _MAX_ROWS and 1 <= col_num <= _MAX_COLUMNS):
                            raise ValueError("工作表含超出 50000 行或 512 列的单元格，请拆分文件")
                    elif element.tag == sheet_ns + "row" and (ref := element.get("r")):
                        if not 1 <= int(ref) <= _MAX_ROWS:
                            raise ValueError("工作表超过 50000 行，请拆分文件")
                    element.clear()
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
    try:
        values_book = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        try:
            scanned = 0
            for sheet in book.worksheets:
                cached_sheet = values_book[sheet.title]
                # Real worksheets can report incorrect dimensions; traverse the actual rows.
                sheet.reset_dimensions()
                cached_sheet.reset_dimensions()
                labels = None
                for i, (row, cached) in enumerate(zip(sheet.rows, cached_sheet.values), 1):
                    scanned += max(1, len(row))
                    if i > _MAX_ROWS or len(row) > _MAX_COLUMNS or scanned > 2_000_000:
                        raise ValueError("工作簿超过单表 50000 行、512 列或总计 200 万个单元格")
                    values = []
                    for j, cell in enumerate(row):
                        value = cell.value
                        if cell.data_type == "f":
                            cached_value = cached[j]
                            formula = getattr(value, "text", value)
                            value = (f"{_display(cached_value)}（公式 {formula}）" if cached_value is not None
                                     else f"公式 {formula}（文件未保存计算结果）")
                        values.append(_display(value))
                    if not any(values):
                        continue
                    columns = [j + 1 for j, value in enumerate(values) if value]
                    span = f"{get_column_letter(columns[0])}{i}:{get_column_letter(columns[-1])}{i}"
                    locator = f"工作表「{sheet.title}」 · {span}"
                    if labels is None:
                        labels = _headers(values)
                        evidence.add(locator + "（表头）", " ｜ ".join(labels))
                    else:
                        evidence.add(locator, _fields(labels, values))
        finally:
            values_book.close()
    finally:
        book.close()


def _pdf(evidence, data):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("请先解除 PDF 密码保护")
    if len(reader.pages) > 1200:
        raise ValueError("PDF 超过 1200 页，请拆分文件")
    pages = {}
    scanned = []
    num_chars = 0
    for i, page in enumerate(reader.pages, 1):
        value = (page.extract_text() or "").strip()
        if value:
            num_chars += len(value)
            if num_chars > _MAX_TEXT_CHARS:
                raise ValueError("文档可检索文本超过 400 万字符，请拆分文件")
            pages[i] = (f"第 {i} 页", value)
        elif ocr.enabled() and page.get("/Contents") is not None:
            scanned.append(i)
    if scanned:
        for i, value in ocr.pdf_pages(data, scanned):
            pages[i] = (f"第 {i} 页 · OCR 识别，请核对原页", value)
    for number in sorted(pages):
        evidence.add(*pages[number])
    if not evidence.rows:
        ocr.require_enabled()
        raise ValueError("PDF 未提取到可辨识的 OCR 文字，请核对原件清晰度")


def parse_document(filename: str, data: bytes) -> list[dict]:
    """Return locally extracted evidence in source order. Raise ValueError on bad input."""
    suffix = PurePath(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支持此文件扩展名：{suffix or '无'}")
    if not data:
        raise ValueError("文件为空")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("单文件上限为 20 MiB")
    evidence = _Evidence()
    try:
        if suffix == ".pdf":
            _pdf(evidence, data)
        elif suffix in ocr.IMAGE_EXTENSIONS:
            for number, value in ocr.image_pages(data, suffix):
                evidence.add(f"第 {number} 页 · OCR 识别，请核对原图", value)
            if not evidence.rows:
                raise ValueError("未识别到清晰文字，请核对图片方向和分辨率")
        elif suffix in {".xlsx", ".docx", ".pptx"}:
            with _checked_archive(data) as archive:
                if suffix == ".xlsx":
                    _xlsx(evidence, archive, data)
                elif suffix == ".docx":
                    _docx(evidence, archive)
                else:
                    _pptx(evidence, archive)
        else:
            value = _decode(data)
            if suffix in {".txt", ".md"}:
                _plain(evidence, value, suffix == ".md")
            elif suffix in {".csv", ".tsv"}:
                _delimited(evidence, value, "," if suffix == ".csv" else "\t")
            elif suffix in {".html", ".htm"}:
                parser = _HtmlText(evidence)
                parser.feed(value)
                parser.close()
                parser.flush()
            elif suffix == ".json":
                _json_nodes(evidence, _read_json(value), "JSON")
            else:
                for i, line in enumerate(io.StringIO(value, newline=None), 1):
                    if line.strip():
                        try:
                            _json_nodes(evidence, _read_json(line), f"第 {i} 行")
                        except ValueError as exc:
                            raise ValueError(f"JSONL 第 {i} 行解析失败：{exc}") from exc
    except (zipfile.BadZipFile, KeyError, ParseError, DefusedXmlException, RecursionError) as exc:
        raise ValueError("文件结构损坏、嵌套过深或包含不支持的 XML 声明") from exc
    except ValueError:
        raise
    except ImportError as exc:
        raise ValueError(f"服务缺少文件解析依赖：{exc.name}") from exc
    except Exception as exc:
        # PDF and workbook parsers raise different third-party exception types.
        raise ValueError(f"无法解析 {suffix} 文件，请核对文件格式或重新导出") from exc
    if not evidence.rows:
        raise ValueError("文件不含可提取的文本")
    return evidence.rows
