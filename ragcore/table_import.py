"""Convert source table regions into labelled rows with traceable header cells."""
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from itertools import groupby
import re

from openpyxl.utils import get_column_letter

CHUNK_SIZE = 1000


def excel_value(value, fmt):
    if value is None:
        return "", {}
    if isinstance(value, (date, time)):
        display = value.isoformat()
        return display, {"raw": display, "format": fmt}
    raw = str(value).strip()
    if isinstance(value, bool) or not isinstance(value, (float, int)) or fmt.lower() in {"general", "", "@"}:
        return raw, {}
    meta = {"raw": raw, "format": fmt}
    pattern = re.fullmatch(r"(#,##)?(0+)(?:\.(0+))?(%)?", fmt)
    if not pattern:
        return raw, {**meta, "unformatted": True}
    group, digits, decimals, percent = pattern.groups()
    number = Decimal(raw) * (100 if percent else 1)
    display = format(number, ("," if group else "") + "." + str(len(decimals or "")) + "f")
    if len(digits) > 1 and not group:
        sign = "-" if display.startswith("-") else ""
        integer, sep, fraction = display.lstrip("-").partition(".")
        display = sign + integer.zfill(len(digits)) + sep + fraction
    if percent:
        display += "%"
    return display, {**meta, "display": display}


@dataclass
class TableRow:
    number: int
    locator: str
    values: list
    # Maps covered columns to the column containing the original merged header.
    horizontal: dict = field(default_factory=dict)
    formats: dict = field(default_factory=dict)
    declared_header: bool = False
    # Vertically merged source cells still apply to later physical rows.
    inherited: dict = field(default_factory=dict)

    @property
    def filled(self):
        return [v for v in self.values if v]


def _numeric(value):
    return bool(re.fullmatch(r"[+−-]?(?:\d[\d,.]*|\.\d+)(?:[%‰])?", value.strip()))


def _years(row):
    return bool(row.filled) and all(re.fullmatch(r"(?:19|20)\d\d(?:年)?", s) for s in row.filled)


def _likely_header(row):
    return len(row.filled) > 1 and all(not _numeric(v) for v in row.filled)


def _header_paths(rows):
    width = max((max(len(r.values), max(r.horizontal, default=-1) + 1) for r in rows), default=0)
    labels, origins = [], []
    for col in range(width):
        names, coords = [], []
        for r in rows:
            source_col = r.horizontal.get(col, col)
            if source_col < len(r.values) and (v := r.values[source_col]):
                if not names or names[-1] != v:
                    names.append(v)
                coords.append(get_column_letter(source_col + 1) + str(r.number))
        labels.append(" / ".join(names) or f"第 {col + 1} 列")
        origins.append(coords)
    seen = set()
    for i, label in enumerate(labels):
        if label in seen:
            labels[i] += f"（第 {i + 1} 列）"
        seen.add(label)
    return labels, origins


def _same_header(first, headers):
    return any(first.values == h.values for h in headers)


def _note(row):
    return len(row.filled) == 1 and bool(re.match(r"^(?:注(?:释)?|说明|数据来源|资料来源|来源|备注|"
                                                  r"sources?|notes?|footnotes?)\s*[：:]", row.filled[0], re.I))


def _leading_rows(rows):
    # A single cell before a wider row is usually the title/reading note. If
    # the following row is itself unlabelled data it will stay searchable.
    start = 0
    while start < min(len(rows) - 1, 12) and len(rows[start].filled) == 1 and not rows[start].declared_header:
        if not any(len(r.filled) > 1 for r in rows[start + 1:start + 5]):
            break
        start += 1
    headers = []
    if rows[start].declared_header or _likely_header(rows[start]) or _years(rows[start]):
        headers.append(rows[start])
    elif len(rows[start].values) == 1 and not _numeric(rows[start].filled[0]):
        headers.append(rows[start])
    # Adjacent declared headers or subordinate year labels form a hierarchy.
    if headers:
        for r in rows[start + 1:start + 12]:
            if r.declared_header or (_years(r) and not _years(headers[-1])):
                headers.append(r)
            elif (not headers[-1].declared_header and headers[-1].horizontal
                  and _likely_header(r) and not _years(headers[-1])):
                headers.append(r)
            else:
                break
    return rows[:start], headers


def _emit_record(evidence, row, fields, columns, metadata, context):
    separator = " ｜ "
    text = separator.join(fields)
    if len(text) <= CHUNK_SIZE:
        evidence.add(row.locator, text, context=context, metadata=metadata)
        return

    # Carry values from the same physical record into later column groups. They
    # may be an ID, rank or name, so retain the actual labels and avoid guessing.
    leading = separator.join(field for field in fields[:3] if len(field) <= 120)
    record_context = " / ".join(part for part in (context, leading) if part)
    start = 0
    while start < len(fields):
        end = start + 1
        size = len(fields[start])
        while end < len(fields) and size + len(separator) + len(fields[end]) <= CHUNK_SIZE:
            size += len(separator) + len(fields[end])
            end += 1
        first, last = columns[start], columns[end - 1]
        if "sheet" in metadata:
            cells = metadata["cells"][start:end]
            location = f"工作表「{metadata['sheet']}」 · {cells[0]['address']}:{cells[-1]['address']}"
            chunk_metadata = {**metadata, "cells": cells}
        else:
            location = f"{row.locator} · 第 {first}–{last} 列"
            chunk_metadata = metadata
        group_context = record_context
        if size > CHUNK_SIZE:
            group_context += " / 字段 " + fields[start].split("：", 1)[0]
        evidence.add(location, separator.join(fields[start:end]), context=group_context,
                     metadata=chunk_metadata)
        start = end


def emit_table(evidence, rows, *, sheet="", context="", headerless=False):
    """Blank rows delimit candidate regions. Plain continuations retain their header."""
    last_headers, last_title = [], ""
    options = evidence.options
    selected = options.get("header_rows", [])
    headerless = options.get("headerless") or (headerless and not selected)
    matched = set()
    for populated, region_rows in groupby(rows, key=lambda r: bool(r.filled)):
        if not populated:
            continue
        region = list(region_rows)
        if headerless:
            preface, headers = [], []
        elif selected and any(r.number in selected for r in region):
            headers = [r for r in region if r.number in selected]
            matched.update(r.number for r in headers)
            preface = [r for r in region if r.number < headers[0].number]
        elif selected and region[-1].number < selected[0]:
            preface, headers = region, []
        else:
            preface, headers = _leading_rows(region)
            if (last_headers and not preface and headers and not headers[0].declared_header
                    and not _same_header(headers[0], last_headers)
                    and (len(region) == 1 or all(_likely_header(r) for r in region[:3]))
                    and len(region[0].values) == len(last_headers[-1].values)):
                # Two adjacent text-only regions may be one table separated by a blank line.
                # Keep all values and flag the ambiguous boundary for source review.
                evidence.warn("table_continuation", "空行后的文本区域列数相同，已沿用前表头，请核对是否另起一表。", region[0].locator)
                headers = []
        pos = len(preface) + len(headers)
        if last_headers and not headers and not preface and not headerless:
            headers = last_headers
            title = last_title
        else:
            title = " / ".join(r.filled[0] for r in preface)[-480:]
        for r in preface:
            evidence.add(r.locator, " ｜ ".join(r.filled), context=context)
        for r in region[len(preface):pos]:
            evidence.add(r.locator + "（表头）", " ｜ ".join(r.values), context=context)
        if not headers and pos < len(region) and not headerless:
            evidence.warn("table_header_uncertain", "此表区未识别出表头，已按列号保留数据，可指定表头行重建。",
                          region[pos].locator)
        elif (not selected and headers and headers[0] in region[:pos] and not headers[0].declared_header
              and pos < len(region) and _likely_header(region[pos]) and len(headers) == 1):
            evidence.warn("table_text_header", "此表首行与数据均为文本，已推断表头，请复核列名或改用无表头导入。",
                          headers[0].locator)
        labels, origins = _header_paths(headers)
        hnums = [r.number for r in headers]
        row_context = " / ".join(s for s in (context, title) if s)
        for r in region[pos:]:
            if _same_header(r, headers):
                evidence.add(r.locator + "（表头）", " ｜ ".join(r.values), context=row_context)
                continue
            if _note(r) or (len(labels) >= 2 and len(r.filled) == 1 and len(r.horizontal) >= len(labels) - 1):
                evidence.add(r.locator, r.filled[0], context=row_context, metadata={"row":r.number,"role":"note"})
                continue
            fields, columns, cells = [], [], []
            for col in range(max(len(r.values),max(r.inherited, default=-1)+1)):
                value = r.values[col] if col < len(r.values) else ""
                source_cell = None
                if not value and col in r.inherited:
                    value, source_cell = r.inherited[col]
                if not value:
                    continue
                label = labels[col] if col < len(labels) else f"第 {col + 1} 列"
                fields.append(f"{label}：{value}")
                columns.append(col + 1)
                if sheet:
                    address = get_column_letter(col + 1) + str(r.number)
                    cells.append({"column": col + 1, "address": address, "label": label,
                                  "header_cells": origins[col] if col < len(origins) else [],
                                  **r.formats.get(col, {})})
                    if source_cell:
                        cells[-1]["value_cell"] = source_cell
            meta = {"row": r.number, "header_rows": hnums}
            if sheet:
                meta.update(sheet=sheet, cells=cells)
            _emit_record(evidence, r, fields, columns, meta, row_context)
        if headers:
            last_headers, last_title = headers, title
    if selected and set(selected) - matched:
        evidence.warn("header_rows_absent", "指定的表头行在此表中未找到或为空，请检查导入预览。", sheet)
