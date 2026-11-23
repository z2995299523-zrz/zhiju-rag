"""Extract searchable text with locators in the original, uploaded file."""

import codecs
import csv
import io
import json
import posixpath
import re
import zipfile
from bisect import bisect_left, bisect_right
from datetime import date, time
from html.parser import HTMLParser
from pathlib import PurePath
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree as safe_xml
from defusedxml.common import DefusedXmlException

from . import ocr
from .import_schema import ImportOptions
from .table_import import CHUNK_SIZE as _CHUNK_SIZE, TableRow, emit_table

SUPPORTED_EXTENSIONS = frozenset({
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".ndjson",
    ".html", ".htm", ".pdf", ".docx", ".xlsx", ".pptx",
}) | ocr.IMAGE_EXTENSIONS
MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_EXPANDED_BYTES = 64 * 1024 * 1024
_MAX_MEMBER_BYTES = 16 * 1024 * 1024
_MAX_SEGMENTS = 20_000
_MAX_TEXT_CHARS = 4_000_000
_MAX_COLUMNS = 512
_MAX_ROWS = 50_000

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


class _Evidence:
    def __init__(self, options):
        self.rows = []
        self.chars = 0
        self.options = options
        self.warnings = {}
        self.coverage = {}

    def warn(self, code, message, locator=""):
        entry = self.warnings.setdefault(code, {"code": code, "message": message, "count": 0, "locations": []})
        entry["count"] += 1
        if locator and len(entry["locations"]) < 30:
            entry["locations"].append(locator)

    def add(self, locator, value, *, context="", metadata=None):
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
                row = {"locator": position, "text": excerpt}
                if context:
                    row["context"] = context[:1400]
                if metadata:
                    row["metadata"] = metadata
                self.rows.append(row)
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
    sections = []
    in_fence = None
    table_rows = []
    for number, line in enumerate(io.StringIO(value, newline=None), 1):
        line = line.rstrip("\n")
        if markdown and not in_fence:
            cells = _markdown_cells(line)
            if table_rows:
                if cells:
                    table_rows.append(TableRow(number, _line_range(number, number), cells))
                    continue
                emit_table(evidence, table_rows, context=_section_text(sections))
                table_rows = []
            if cells and all(re.fullmatch(r":?-{3,}:?", c) for c in cells) and pending:
                names = _markdown_cells(pending[-1])
                if len(names) == len(cells):
                    pending.pop()
                    if pending:
                        evidence.add(_line_range(start, number-2), "\n".join(pending), context=_section_text(sections))
                    table_rows.append(TableRow(number-1, _line_range(number-1,number-1), names, declared_header=True))
                    pending, length = [], 0
                    continue
            underline = re.fullmatch(r" {0,3}(=+|-+)\s*", line)
            if (underline and pending and not re.match(r"^(?: {4}|\t|\s{0,3}(?:#|>|[-*+]\s|\d+[.)]\s))", pending[-1])):
                title = pending.pop().strip()
                if pending:
                    evidence.add(_line_range(start, number-2), "\n".join(pending), context=_section_text(sections))
                _update_section(sections, 1 if underline[1][0] == "=" else 2, title)
                evidence.add(_line_range(number-1, number), title+"\n"+line, context=_section_text(sections))
                pending, length = [], 0
                continue
        fence = markdown and re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence:
            marker = fence[1]
            if in_fence is None:
                in_fence = marker
            elif marker[0] == in_fence[0] and len(marker) >= len(in_fence):
                in_fence = None
        heading = markdown and not in_fence and re.match(r"^(#{1,6})\s+(.+)", line)
        if pending and (not line.strip() or heading or length + len(line) > _CHUNK_SIZE):
            evidence.add(_line_range(start, number - 1), "\n".join(pending), context=_section_text(sections))
            pending, length = [], 0
        if heading:
            _update_section(sections, len(heading[1]), heading[2])
        if line.strip():
            if not pending:
                start = number
            pending.append(line)
            length += len(line) + 1
    if pending:
        evidence.add(_line_range(start, start + len(pending) - 1), "\n".join(pending), context=_section_text(sections))
    if table_rows:
        emit_table(evidence, table_rows, context=_section_text(sections))


def _markdown_cells(line):
    body = line.strip().removeprefix("|")
    if body.endswith("|") and not body.endswith(r"\|"):
        body = body[:-1]
    cells = [s.strip().replace(r"\|", "|") for s in re.split(r"(?<!\\)\|", body)]
    return cells if len(cells) > 1 else []


def _update_section(sections, level, title):
    while sections and sections[-1][0] >= level:
        sections.pop()
    if title.strip():
        sections.append((level, title.strip()[:240]))


def _section_text(sections):
    return " / ".join(t for _, t in sections)


def _display(value):
    if value is None:
        return ""
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value).strip()


def _delimited(evidence, value, delimiter):
    reader = csv.reader(io.StringIO(value, newline=""), delimiter=delimiter, strict=True)
    def rows():
        for record in range(1, _MAX_ROWS + 2):
            start = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                return
            if record > _MAX_ROWS or len(row) > _MAX_COLUMNS:
                raise ValueError("表格超过 50000 条记录或 512 列，请拆分文件")
            yield TableRow(start, _line_range(start, reader.line_num), [_display(v) for v in row])
    try:
        emit_table(evidence, rows())
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


def _json_pointer(parent, key):
    return parent + "/" + key.replace("~", "~0").replace("/", "~1")


def _identifier_key(key):
    # Match complete tokens in English so that "valid", "decode" or timestamps do
    # not become entity identifiers. Camel-case and separated names are common.
    words = re.findall(r"[a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower())
    if words and words[-1] in {"id", "uid", "uuid", "identifier", "sku", "ean", "isbn", "vin", "code", "no", "number"}:
        if words[-1] == "number" and not set(words[:-1]).intersection({
            "account", "asset", "batch", "case", "catalog", "claim", "contract", "customer", "device",
            "document", "employee", "invoice", "item", "lot", "model", "order", "part", "project",
            "record", "reference", "registration", "serial", "ticket", "tracking",
        }):
            return False
        return not set(words[:-1]).intersection({"status", "response", "error", "postal", "zip", "phone", "telephone"})
    if re.search(r"(?:状态|响应|错误|邮政)(?:代码|编码)$", key):
        return False
    return bool(re.search(r"(?:编号|编码|代码|标识符|[单货料工学批证]号|序列号|合同号|设备号|档案号)$", key))


def _json_record_ids(evidence, scalar, pointer, inherited, locator):
    identifiers = list(inherited)
    for key, value in scalar.items():
        if not isinstance(value, str) or not value.strip() or not _identifier_key(key):
            continue
        if len(value) > 140 or len(identifiers) >= 16:
            evidence.warn("json_identifier_limit", "记录标识过长或超过 16 项，超出部分保留在原节点。", locator)
            continue
        identifiers.append({"key":key, "value":str(value), "pointer":_json_pointer(pointer, key)})
    return identifiers


def _json_nodes(evidence, node, prefix, pointer="", depth=0, parents=(), record=None, identifiers=()):
    if depth > 64:
        raise ValueError("JSON 嵌套超过 64 层")
    if len(pointer) > 4000:
        raise ValueError("JSON 字段路径过长")
    locator = f"{prefix} · JSON Pointer {pointer}" if pointer else f"{prefix} · 根节点"
    id_labels = (f"{i['key']}={i['value']}" for i in identifiers)
    context = " / ".join((*parents, *id_labels, pointer))[-1400:]
    metadata = {"pointer": pointer, "record_pointer": record if record is not None else pointer}
    if identifiers:
        metadata["record_identifiers"] = identifiers
    if isinstance(node, list) and node:
        for i, item in enumerate(node):
            child = f"{pointer}/{i}"
            _json_nodes(evidence, item, prefix, child, depth + 1, parents, child if isinstance(item, dict) else record,
                        identifiers)
    elif isinstance(node, dict) and node:
        scalar = {k: v for k, v in node.items() if not isinstance(v, (list, dict)) or not v}
        names = [(k, v) for k, v in scalar.items() if isinstance(v, str) and not isinstance(v, _JsonNumber)
                 and re.search(r"(?:name|title|summary|subject|project|label|company|名称|标题|节日|项目|部门|产品|姓名|类别)$", k, re.I)]
        label = "，".join(f"{k}={v[:140]}" for k, v in names[:4])
        child_parents = (*parents, label)[-3:] if label else parents
        child_ids = _json_record_ids(evidence, scalar, pointer, identifiers, locator)
        child_record = pointer if len(child_ids) > len(identifiers) else record
        if child_ids:
            metadata["record_identifiers"] = child_ids
        if child_record is not None:
            metadata["record_pointer"] = child_record
        for key, item in node.items():
            if isinstance(item, (list, dict)) and item:
                _json_nodes(evidence, item, prefix, _json_pointer(pointer, key), depth + 1, child_parents, child_record,
                            child_ids)
        if scalar:
            pairs = (f"{json.dumps(key, ensure_ascii=False)}: {_json_scalar(item)}"
                     for key, item in scalar.items())
            evidence.add(locator, "{" + ", ".join(pairs) + "}", context=context, metadata=metadata)
    else:
        evidence.add(locator, _json_scalar(node), context=context, metadata=metadata)


class _HtmlText(HTMLParser):
    blocks = set("address article aside blockquote br dd div dl dt figcaption footer form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tr ul".split())
    hidden = {"head", "script", "style", "template", "noscript", "svg"}

    def __init__(self, evidence):
        super().__init__(convert_charrefs=True)
        self.evidence = evidence
        self.skip = []
        self.pending = []
        self.start = self.end = self.count = 0
        self.sections = []
        self.heading = None
        self.tables = []

    def flush(self):
        value = re.sub(r"\s+", " ", "".join(self.pending)).strip()
        if value:
            self.count += 1
            if self.heading:
                _update_section(self.sections, self.heading, value)
            self.evidence.add(f"文本块 {self.count} · {_line_range(self.start, self.end)}", value,
                              context=_section_text(self.sections))
        self.pending.clear()

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden:
            self.skip.append(tag)
        if self.skip:
            return
        if tag == "table":
            self.flush()
            self.tables.append({"rows": [], "row": None, "carried": {}, "caption": [], "in_caption": False,
                                "caption_line": self.getpos()[0]})
        elif self.tables:
            t = self.tables[-1]
            if tag == "tr":
                self._finish_row()
                t["row"] = {"line": self.getpos()[0], "values": [], "horizontal": {}, "col": 0,
                            "header": True, "cell": None, "inherited":{c:(v,loc) for c,(_,v,loc) in t["carried"].items()}}
            elif tag in {"td", "th"} and t["row"] is not None:
                self._finish_cell()
                attrs = dict(attrs)
                def span(name):
                    value = attrs.get(name) or "1"
                    return min(int(value), _MAX_COLUMNS if name == "colspan" else _MAX_ROWS) if re.fullmatch(r"[1-9]\d{0,4}", value) else 1
                t["row"]["cell"] = ([], span("colspan"), span("rowspan"))
                t["row"]["header"] &= tag == "th"
            elif tag == "caption":
                t["in_caption"] = True
                t["caption_line"] = self.getpos()[0]
            elif tag in self.blocks and t["row"] and t["row"]["cell"]:
                t["row"]["cell"][0].append(" ")
        elif tag in self.blocks:
            self.flush()
            if re.fullmatch(r"h[1-6]", tag):
                self.heading = int(tag[1])

    def _finish_cell(self):
        t = self.tables[-1]
        r = t["row"]
        if r is None or r["cell"] is None:
            return
        parts, width, height = r["cell"]
        while r["col"] in t["carried"]:
            r["col"] += 1
        start = r["col"]
        if start + width > _MAX_COLUMNS:
            raise ValueError("HTML 表格超过 512 列")
        while len(r["values"]) < start + width:
            r["values"].append("")
        r["values"][start] = re.sub(r"\s+", " ", "".join(parts)).strip()
        for col in range(start, start + width):
            if col > start:
                r["horizontal"][col] = start
            if height > 1:
                t["carried"][col] = (height, r["values"][start] if col == start else "",
                                     f"第 {r['line']} 行，第 {start+1} 列")
        r["col"] = start + width
        r["cell"] = None

    def _finish_row(self):
        t = self.tables[-1]
        r = t["row"]
        if r is None:
            return
        self._finish_cell()
        number = len(t["rows"]) + 1
        if number > _MAX_ROWS:
            raise ValueError("HTML 表格超过 50000 行")
        t["carried"] = {c:(n-1,v,loc) for c,(n,v,loc) in t["carried"].items() if n > 1}
        self.count += 1
        locator = f"文本块 {self.count} · {_line_range(r['line'], self.getpos()[0])}"
        t["rows"].append(TableRow(number, locator, r["values"], r["horizontal"], declared_header=r["header"],
                                  inherited=r["inherited"]))
        t["row"] = None

    def _finish_table(self):
        self._finish_row()
        t = self.tables.pop()
        caption = re.sub(r"\s+", " ", "".join(t["caption"])).strip()
        emit_table(self.evidence, t["rows"], context=" / ".join(filter(None, [_section_text(self.sections), caption])))
        if caption:
            self.evidence.add(f"表格标题 · 第 {t['caption_line']} 行", caption, context=_section_text(self.sections))

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
        elif self.tables:
            if tag == "table":
                self._finish_table()
            elif tag == "tr":
                self._finish_row()
            elif tag in {"td", "th"}:
                self._finish_cell()
            elif tag == "caption":
                self.tables[-1]["in_caption"] = False
        elif tag in self.blocks:
            self.flush()
            if re.fullmatch(r"h[1-6]", tag):
                self.heading = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.tables:
            t = self.tables[-1]
            if t["in_caption"]:
                t["caption"].append(data)
            elif t["row"] and t["row"]["cell"]:
                t["row"]["cell"][0].append(data)
        else:
            if not self.pending:
                self.start = self.getpos()[0]
            self.end = self.getpos()[0] + data.count("\n")
            self.pending.append(data)

    def close(self):
        super().close()
        while self.tables:
            self.evidence.warn("html_table_unclosed", "HTML 表格缺少结束标签，已保留可恢复的行。")
            self._finish_table()


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
    sections = []
    prior = ""

    def walk(block):
        nonlocal para, table, prior
        for elem in block:
            if elem.tag == ns + "p":
                para += 1
                value = _word_text(elem)
                level = elem.find("w:pPr/w:outlineLvl", _NS)
                style = elem.find("w:pPr/w:pStyle", _NS)
                heading = re.search(r"(?:Heading|标题)\s*([1-9])$", style.get(ns+"val", ""), re.I) if style is not None else None
                if level is not None and level.get(ns+"val", "").isdigit() and int(level.get(ns+"val")) < 9:
                    _update_section(sections, int(level.get(ns+"val")) + 1, value)
                elif heading:
                    _update_section(sections, int(heading[1]), value)
                elif re.match(r"^第[一二三四五六七八九十百\d]+[章节篇]\s*.{0,100}$", value):
                    _update_section(sections, 1, value)
                context = _section_text(sections)
                if not context and prior and value:
                    context = f"前一段（正文第 {para-1} 段） {prior}"
                evidence.add(f"正文第 {para} 段", value, context=context)
                prior = value if re.fullmatch(r"[^。！？；，,.!?:：\n]{2,48}",value) else ""
            elif elem.tag == ns + "tbl":
                prior = ""
                table += 1
                table_number = table
                rows, nested_tables = [], []
                vertical = {}
                for i, row in enumerate(elem.findall("w:tr", _NS), 1):
                    values, spans, inherited, following = [], {}, {}, {}
                    for cell in row.findall("w:tc", _NS):
                        span = cell.find("w:tcPr/w:gridSpan", _NS)
                        width = int(span.get(ns+"val", "1")) if span is not None else 1
                        if width < 1 or len(values) + width > _MAX_COLUMNS or i > _MAX_ROWS:
                            raise ValueError("Word 表格超出解析上限")
                        start = len(values)
                        values.append("\n".join(filter(None, (_word_text(p) for p in cell.findall("w:p", _NS)))))
                        merge = cell.find("w:tcPr/w:vMerge", _NS)
                        if merge is not None:
                            if merge.get(ns+"val") == "restart":
                                following[start] = (values[start], f"表格 {table_number} 第 {i} 行第 {start+1} 列")
                            elif start in vertical:
                                inherited[start] = following[start] = vertical[start]
                        values.extend([""] * (width - 1))
                        spans.update((c, start) for c in range(start + 1, start + width))
                        nested_tables.extend(cell.findall("w:tbl", _NS))
                    rows.append(TableRow(i, f"表格 {table_number} · 第 {i} 行", values, spans,
                                         declared_header=row.find("w:trPr/w:tblHeader", _NS) is not None,
                                         inherited=inherited))
                    vertical = following
                emit_table(evidence, rows, context=_section_text(sections))
                walk(nested_tables)
            elif elem.tag in {ns + "sdt", ns + "sdtContent", ns + "customXml", ns + "ins"}:
                walk(elem)
    walk(body)
    if body.find(".//w:drawing", _NS) is not None:
        evidence.warn("word_drawing", "正文含绘图或嵌入图片，图内文字需对照原件。")


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
        slide_root = _xml(archive, path)
        if slide_root.get("show") in ("0", "false") and not evidence.options.get("include_hidden"):
            evidence.warn("slide_hidden", "已按导入设置排除隐藏幻灯片。", f"第 {number} 张幻灯片")
            continue
        tree = slide_root.find("p:cSld/p:spTree", _NS)
        if tree is None:
            continue
        paragraphs = []
        tables = tree.findall(".//a:tbl", _NS)
        table_paragraphs = {p for table in tables for p in table.iterfind(".//a:p", _NS)}
        for p in tree.iterfind(".//a:p", _NS):
            if p in table_paragraphs:
                continue
            parts = []
            for run in p.iter():
                if run.tag == "{" + _NS["a"] + "}t":
                    parts.append(run.text or "")
                elif run.tag == "{" + _NS["a"] + "}br":
                    parts.append("\n")
            if parts:
                paragraphs.append("".join(parts))
        locator = f"第 {number} 张幻灯片"
        title = ""
        for shape in tree.iterfind(".//p:sp", _NS):
            ph = shape.find("p:nvSpPr/p:nvPr/p:ph", _NS)
            if ph is not None and ph.get("type") in {"title", "ctrTitle"}:
                title = " ".join(n.text or "" for n in shape.iterfind(".//a:t", _NS))
                break
        graphics = tree.findall(".//p:graphicFrame", _NS)
        if (not (paragraphs or tables) or tree.find(".//p:pic", _NS) is not None
                or any(frame.find(".//a:tbl", _NS) is None for frame in graphics)):
            evidence.warn("slide_visuals", "此页含非文本对象或没有可提取文字，请对照幻灯片复核。", locator)
        evidence.add(locator, "\n".join(paragraphs), context=title, metadata={"slide": number})
        for index, table in enumerate(tables,1):
            rows = []
            for j, row in enumerate(table.findall("a:tr", _NS),1):
                cells = row.findall("a:tc", _NS)
                if len(cells) > _MAX_COLUMNS or j > _MAX_ROWS:
                    raise ValueError("幻灯片表格超过行列上限")
                values, spans = [], {}
                for col, cell in enumerate(cells):
                    value = "\n".join("".join(t.text or "" for t in p.iterfind(".//a:t", _NS))
                                      for p in cell.iterfind(".//a:p", _NS)).strip()
                    values.append(value)
                    width = int(cell.get("gridSpan", "1"))
                    if width < 1 or col + width > _MAX_COLUMNS:
                        raise ValueError("幻灯片表格合并区域无效")
                    spans.update((k, col) for k in range(col + 1,col + width))
                rows.append(TableRow(j, f"{locator} · 表格 {index} 第 {j} 行", values, spans))
            emit_table(evidence, rows, context=title)


def _xlsx(evidence, archive, data):
    from openpyxl import load_workbook
    from openpyxl.utils import coordinate_to_tuple, get_column_letter, range_boundaries
    from .table_import import excel_value

    sheet_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    layouts = {}
    for part in archive.namelist():
        if part.lower().endswith((".xml", ".rels")):
            hidden_rows, hidden_cols, ranges, vertical, table_ids = set(), set(), {}, {}, []
            with archive.open(part) as stream:
                for _, element in safe_xml.iterparse(stream, forbid_dtd=True):
                    if element.tag == sheet_ns + "c" and (ref := element.get("r")):
                        row_num, col_num = coordinate_to_tuple(ref)
                        if not (1 <= row_num <= _MAX_ROWS and 1 <= col_num <= _MAX_COLUMNS):
                            raise ValueError("工作表含超出 50000 行或 512 列的单元格，请拆分文件")
                    elif element.tag == sheet_ns + "row" and (ref := element.get("r")):
                        if not 1 <= int(ref) <= _MAX_ROWS:
                            raise ValueError("工作表超过 50000 行，请拆分文件")
                        if element.get("hidden") in {"true", "1"}:
                            hidden_rows.add(int(ref))
                    elif element.tag == sheet_ns + "col" and element.get("hidden") in {"true", "1"}:
                        hidden_cols.update(range(max(1,int(element.get("min"))), min(_MAX_COLUMNS,int(element.get("max"))) + 1))
                    elif element.tag == sheet_ns + "mergeCell":
                        a, row, b, bottom = range_boundaries(element.attrib["ref"])
                        if 1 <= a <= b <= _MAX_COLUMNS and b > a:
                            top = ranges.setdefault(row, {})
                            top.update((c-1, a-1) for c in range(a+1, b+1))
                        if 1 <= a <= _MAX_COLUMNS and row < bottom <= _MAX_ROWS:
                            vertical.setdefault(row, []).append((a-1, bottom))
                    elif element.tag == sheet_ns + "tablePart":
                        table_ids.append(element.get("{" + _NS["r"] + "}id"))
                    element.clear()
            if ranges or vertical or hidden_rows or hidden_cols or table_ids:
                layouts[part] = (hidden_rows, hidden_cols, ranges, vertical, table_ids)
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
    try:
        values_book = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        try:
            scanned = 0
            for sheet in book.worksheets:
                if sheet.sheet_state != "visible" and not evidence.options.get("include_hidden"):
                    evidence.warn("sheet_hidden", "已按导入设置排除隐藏工作表。", sheet.title)
                    continue
                cached_sheet = values_book[sheet.title]
                # Real worksheets can report incorrect dimensions; traverse the actual rows.
                sheet.reset_dimensions()
                cached_sheet.reset_dimensions()
                sheet_part = sheet._worksheet_path.lstrip("/")
                hidden_rows, hidden_cols, ranges, vertical, table_ids = layouts.get(sheet_part, (set(),set(),{},{},[]))
                if (hidden_rows or hidden_cols) and not evidence.options.get("include_hidden"):
                    evidence.warn("cells_hidden", "已按导入设置排除隐藏行或隐藏列。", sheet.title)
                rows = []
                active_vertical = {}
                for i, (row, cached) in enumerate(zip(sheet.rows, cached_sheet.values), 1):
                    scanned += max(1, len(row))
                    if i > _MAX_ROWS or len(row) > _MAX_COLUMNS or scanned > 2_000_000:
                        raise ValueError("工作簿超过单表 50000 行、512 列或总计 200 万个单元格")
                    if i in hidden_rows and not evidence.options.get("include_hidden"):
                        continue
                    values, formats = [], {}
                    for j, cell in enumerate(row):
                        if j+1 in hidden_cols and not evidence.options.get("include_hidden"):
                            values.append("")
                            continue
                        value = cell.value
                        cached_value = cached[j] if j < len(cached) else None
                        raw = cached_value if cell.data_type == "f" else value
                        display, meta = excel_value(raw, cell.number_format or "")
                        if cell.data_type == "e" or (cell.data_type == "f" and isinstance(cached_value,str)
                                and cached_value in {"#REF!","#DIV/0!","#VALUE!","#N/A","#NUM!","#NAME?","#NULL!"}):
                            evidence.warn("cell_error", "此单元格保存的是工作表错误值，请核对公式后重新计算。",
                                          f"{sheet.title}!{cell.coordinate}")
                        if cell.data_type == "f":
                            formula = str(getattr(value, "text", value))
                            meta.update(formula=formula, cached=cached_value is not None)
                            display = (f"{display}（公式 {formula}）" if cached_value is not None
                                       else f"公式 {formula}（文件未保存计算结果）")
                            if cached_value is None:
                                evidence.warn("formula_uncached", "公式缺少已保存的计算结果，仅保留公式。", f"{sheet.title}!{cell.coordinate}")
                        if meta:
                            if meta.pop("unformatted", False):
                                evidence.warn("custom_number_format", "此单元格的自定义显示格式需回原件核对，已保留原值与格式。",
                                              f"{sheet.title}!{cell.coordinate}")
                            formats[j] = meta
                        values.append(display)
                    columns = [j + 1 for j, value in enumerate(values) if value]
                    active_vertical = {c:v for c,v in active_vertical.items() if v[0] >= i}
                    inherited = {c:(v,addr) for c,(_,v,addr) in active_vertical.items()}
                    for c, bottom in vertical.get(i, []):
                        if c < len(values) and values[c]:
                            active_vertical[c] = (bottom, values[c], get_column_letter(c+1)+str(i))
                    occupied = columns + [c+1 for c in inherited]
                    start, end = (min(occupied), max(occupied)) if occupied else (1, 1)
                    locator = f"工作表「{sheet.title}」 · {get_column_letter(start)}{i}:{get_column_letter(end)}{i}"
                    rows.append(TableRow(i, locator, values, ranges.get(i, {}), formats, inherited=inherited))
                regions = []
                if table_ids:
                    links = _relationships(archive, sheet_part)
                    for key in table_ids:
                        kind, path = links[key]
                        if kind != "table":
                            raise ValueError("工作表引用的表格区域无效")
                        defined = _xml(archive, path)
                        regions.append((range_boundaries(defined.attrib["ref"]), int(defined.get("headerRowCount", "1"))))
                numbers = [r.number for r in rows]
                for (a, first, b, last), header_count in regions:
                    if not 1 <= a <= b <= _MAX_COLUMNS or not 1 <= first <= last <= _MAX_ROWS:
                        raise ValueError("Excel 表格区域超出行列上限")
                    selected = []
                    for r in rows[bisect_left(numbers,first):bisect_right(numbers,last)]:
                        fields = [""] * (a-1) + r.values[a-1:b]
                        locator = f"工作表「{sheet.title}」 · {get_column_letter(a)}{r.number}:{get_column_letter(b)}{r.number}"
                        selected.append(TableRow(r.number, locator, fields, r.horizontal, r.formats,
                                                 declared_header=r.number < first + header_count,
                                                 inherited={c:val for c,val in r.inherited.items() if a-1 <= c < b}))
                        r.values[a-1:b] = [""] * len(r.values[a-1:b])
                        r.inherited = {c:val for c,val in r.inherited.items() if c < a-1 or c >= b}
                    emit_table(evidence, selected, sheet=sheet.title, headerless=not header_count)
                emit_table(evidence, rows, sheet=sheet.title)
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
    forced = evidence.options.get("ocr_pages", [])
    if forced:
        ocr.require_enabled()
        if max(forced) > len(reader.pages):
            raise ValueError("指定的 OCR 页码超过 PDF 总页数")
    pages, scanned = {}, []
    native, empty, omitted = [], [], []
    num_chars = 0
    for i, page in enumerate(reader.pages, 1):
        value = (page.extract_text() or "").strip()
        if value:
            num_chars += len(value)
            if num_chars > _MAX_TEXT_CHARS:
                raise ValueError("文档可检索文本超过 400 万字符，请拆分文件")
            pages[i] = (f"第 {i} 页", value)
            native.append(i)
            if i in forced:
                scanned.append(i)
            else:
                resources = page.get("/Resources")
                if resources and resources.get_object().get("/XObject"):
                    evidence.warn("pdf_embedded_objects", "本页保留文字层，嵌入图像或图形中的文字可指定页码补识别。",
                                  f"第 {i} 页")
        elif ocr.enabled() and (not forced or i in forced) and page.get("/Contents") is not None:
            scanned.append(i)
        elif page.get("/Contents") is not None:
            omitted.append(i)
            evidence.warn("pdf_no_text", "此页无可提取文字，本次未做 OCR，已保留原页。", f"第 {i} 页")
        else:
            empty.append(i)
    recognized = []
    supplements = {}
    if scanned:
        for i, value in ocr.pdf_pages(data, scanned):
            if value.strip():
                recognized.append(i)
                entry = (f"第 {i} 页 · OCR 识别，请核对原页", value)
                if i in pages:
                    supplements[i] = entry
                else:
                    pages[i] = entry
            else:
                evidence.warn("ocr_no_text", "该页 OCR 未取得清晰文字，请核对原页。", f"第 {i} 页")
                if i not in pages:
                    omitted.append(i)
    for number in sorted(pages):
        evidence.add(*pages[number])
        if number in supplements:
            evidence.add(*supplements[number])
    evidence.coverage.update(total_pages=len(reader.pages), text_pages=native, ocr_pages=recognized,
                             empty_pages=empty, skipped_pages=omitted)
    if not evidence.rows:
        ocr.require_enabled()
        raise ValueError("PDF 未提取到可辨识的 OCR 文字，请核对原件清晰度")


def parse_document_report(filename: str, data: bytes, options=None) -> dict:
    """Extract located evidence and describe import decisions and coverage."""
    suffix = PurePath(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支持此文件扩展名：{suffix or '无'}")
    if not data:
        raise ValueError("文件为空")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("单文件上限为 20 MiB")
    options = ImportOptions.model_validate({} if options is None else options).model_dump(exclude_defaults=True)
    evidence = _Evidence(options)
    try:
        if suffix == ".pdf":
            _pdf(evidence, data)
        elif suffix in ocr.IMAGE_EXTENSIONS:
            recognized, omitted = [], []
            for number, value in ocr.image_pages(data, suffix):
                if not value.strip():
                    omitted.append(number)
                    evidence.warn("ocr_no_text", "该图或帧未识别到清晰文字，请核对原图。", f"第 {number} 页")
                else:
                    recognized.append(number)
                    evidence.add(f"第 {number} 页 · OCR 识别，请核对原图", value)
            evidence.coverage.update(total_pages=len(recognized) + len(omitted), ocr_pages=recognized, skipped_pages=omitted)
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
    if evidence.coverage.get("skipped_pages"):
        status = "partial"
    elif evidence.warnings or evidence.coverage.get("ocr_pages"):
        status = "review"
    else:
        status = "indexed"
    evidence.coverage.update(format=suffix, chunks=len(evidence.rows), status=status)
    return {"chunks": evidence.rows, "warnings": list(evidence.warnings.values()), "coverage": evidence.coverage}


def parse_document(filename: str, data: bytes) -> list[dict]:
    """Compatibility interface for callers that consume only located text."""
    return [{"locator": row["locator"], "text": row["text"]} for row in parse_document_report(filename, data)["chunks"]]
