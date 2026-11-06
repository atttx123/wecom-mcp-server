"""零依赖 XLSX 写出器。

仅实现导出所需的最小 OOXML 子集：内联字符串、数值、粗体表头、冻结首行、
自动筛选、列宽。之所以不依赖 openpyxl，是因为部分环境无法访问 PyPI，
而审批导出是本项目的核心能力，必须在离线环境下可用。
"""

from __future__ import annotations

import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence
from xml.sax.saxutils import escape

# XML 1.0 不允许的控制字符
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_INVALID_SHEET_NAME = re.compile(r"[\[\]:*?/\\]")

_MAX_COL = 16384
_MAX_ROW = 1048576

_CONTENT_TYPES_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
)
_CONTENT_TYPES_TAIL = "</Types>"

_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/>'
    "</Relationships>"
)

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    "</fonts>"
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def col_letter(index: int) -> str:
    """0 基列序号转 Excel 列标 (0 -> A, 26 -> AA)。"""
    if index < 0:
        raise ValueError("列序号不能为负")
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _clean(text: str) -> str:
    return _ILLEGAL.sub("", text)


def _cell_xml(ref: str, value: Any, style: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" s="{style}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    if isinstance(value, (datetime, date)):
        value = value.isoformat(sep=" ")
    text = _clean(str(value))
    if text == "":
        return ""
    return (
        f'<c r="{ref}" s="{style}" t="inlineStr"><is>'
        f'<t xml:space="preserve">{escape(text)}</t></is></c>'
    )


def _sheet_xml(headers: Sequence[str], rows: Sequence[Sequence[Any]], widths: Sequence[int]) -> str:
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>",
        '<sheetFormatPr defaultRowHeight="15"/>',
    ]
    if widths:
        cols = []
        for i, width in enumerate(widths, start=1):
            cols.append(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>')
        parts.append(f"<cols>{''.join(cols)}</cols>")

    parts.append("<sheetData>")
    row_parts: list[str] = []
    if headers:
        cells = []
        for i, head in enumerate(headers):
            cells.append(_cell_xml(f"{col_letter(i)}1", head, 1))
        row_parts.append(f'<row r="1">{"".join(cells)}</row>')
    for r, row in enumerate(rows, start=2):
        if r > _MAX_ROW:
            break
        cells = []
        for c, value in enumerate(row):
            if c >= _MAX_COL:
                break
            cells.append(_cell_xml(f"{col_letter(c)}{r}", value, 0))
        row_parts.append(f'<row r="{r}">{"".join(cells)}</row>')
    parts.append("".join(row_parts))
    parts.append("</sheetData>")

    if headers and rows:
        last_col = col_letter(min(max(len(headers), 1) - 1, _MAX_COL - 1))
        parts.append(f'<autoFilter ref="A1:{last_col}1"/>')
    parts.append("</worksheet>")
    return "".join(parts)


def _safe_sheet_name(name: str, used: set[str]) -> str:
    cleaned = _INVALID_SHEET_NAME.sub("_", str(name or "Sheet")).strip() or "Sheet"
    cleaned = cleaned[:31]
    candidate = cleaned
    index = 1
    while candidate in used:
        suffix = f"_{index}"
        candidate = cleaned[: 31 - len(suffix)] + suffix
        index += 1
    used.add(candidate)
    return candidate


def write_xlsx(
    path: str | Path,
    sheets: Sequence[dict[str, Any]],
) -> Path:
    """写出 xlsx 文件。

    Args:
        path: 输出路径
        sheets: [{name, headers, rows, widths}, ...]
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    used_names: set[str] = set()
    prepared = []
    for index, sheet in enumerate(sheets, start=1):
        name = _safe_sheet_name(sheet.get("name", f"Sheet{index}"), used_names)
        headers = [str(h) for h in (sheet.get("headers") or [])]
        rows = sheet.get("rows") or []
        widths = sheet.get("widths") or []
        prepared.append((name, headers, rows, widths))

    content_types = [_CONTENT_TYPES_HEAD]
    for index in range(1, len(prepared) + 1):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    content_types.append(_CONTENT_TYPES_TAIL)

    workbook_sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, (name, *_rest) in enumerate(prepared, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{workbook_sheets}</sheets></workbook>"
    )

    rels = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    for index in range(1, len(prepared) + 1):
        rels.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
    rels.append(
        f'<Relationship Id="rId{len(prepared) + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    rels.append("</Relationships>")

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "".join(content_types))
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
        zf.writestr("xl/styles.xml", _STYLES)
        for index, (_name, headers, rows, widths) in enumerate(prepared, start=1):
            zf.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(headers, rows, widths))

    return target
