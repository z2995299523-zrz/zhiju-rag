"""Read bounded, unformatted excerpts from the stored original for import inspection."""
import io
import re

from .documents import _checked_archive, _decode


TEXT_FORMATS = {".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".htm", ".html"}
LINE = re.compile(r"^(?:文本块 \d+ · )?第 (\d+)(?:[–-](\d+))? 行")


def text_window(data, locator, header_locator=None, plain=False):
    match = LINE.search(locator)
    start = int(match[1]) if match else None
    end = int(match[2] or match[1]) if match else None
    first = max(1, (start or 1) - 3)
    last = min(first + 39, (end + 3) if end else 24)
    wanted = set(range(first, last + 1))
    if header_locator and (header := LINE.search(header_locator)):
        wanted.update(range(int(header[1]), min(int(header[2] or header[1]), int(header[1]) + 3) + 1))
    char_span = re.search(r" · 文本字符 (\d+)–\d+$", locator)
    rows = []
    stop = max(wanted)
    for n, raw in enumerate(io.StringIO(_decode(data), newline=None), 1):
        if n > stop:
            break
        if n not in wanted:
            continue
        line = raw.rstrip("\n")
        # Character offsets apply to extracted text; use them only for an exact single source line.
        pos = 0
        if plain and n == start == end and char_span:
            pos = min(len(line), len(line) - len(line.lstrip()) + int(char_span[1]) - 1)
        left = max(0, pos - 100)
        rows.append({"number": n, "text": line[left:left + 2048], "prefix_clipped": left > 0,
                     "suffix_clipped": len(line) > left + 2048})
    return {"kind": "text", "first": start, "last": end, "rows": rows,
            "range_clipped": bool(end and end > last)}


def sheet_window(data, locator):
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter, range_boundaries

    match = re.match(r"^工作表「(.+)」 · ([A-Z]{1,3}\d+:[A-Z]{1,3}\d+)(?:（表头）| ·|$)", locator, re.S)
    if not match:
        raise ValueError("此分块没有可定位的工作表范围，可下载原件核对")
    sheet_name, span = match.groups()
    col1, row1, col2, row2 = range_boundaries(span)
    if not (1 <= row1 == row2 <= 50000 and 1 <= col1 <= col2 <= 512):
        raise ValueError("工作表位置超出原件预览范围")
    first_col = max(1, col1 - 1)
    last_col = min(512, max(first_col + 3, col2 + 1), first_col + 23)
    rows = []
    found_header = False
    with _checked_archive(data):
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
        try:
            sheet = book[sheet_name]
            sheet.reset_dimensions()
            for n, cells in enumerate(sheet.values, 1):
                if n > row1 + 3:
                    break
                first_nonempty = not found_header and any(v is not None and str(v).strip() for v in cells)
                if first_nonempty:
                    found_header = True
                if not first_nonempty and n < row1 - 3:
                    continue
                values = []
                for col in range(first_col, last_col + 1):
                    value = cells[col - 1] if col <= len(cells) else None
                    value = "" if value is None else str(getattr(value, "text", value))
                    values.append(value if len(value) <= 512 else value[:512] + "…")
                rows.append({"number": n, "cells": values})
        finally:
            book.close()
    return {"kind": "sheet", "sheet": sheet_name, "first": row1, "last": row2,
            "columns": [get_column_letter(i) for i in range(first_col, last_col + 1)],
            "range_clipped": last_col < col2, "rows": rows}
