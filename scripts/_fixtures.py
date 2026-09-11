"""Build a small PDF and a small .xlsx using nothing but the standard library.

This exists so scripts/check_endtoend.py can be copied to any machine with
Python on it and still run. The end-to-end test is the one that most needs to
run from somewhere else, so it must not depend on the project being installed
there.
"""

from __future__ import annotations

import zipfile
from io import BytesIO


def build_pdf(title: str, lines: list[str]) -> bytes:
    """A single-page PDF with Helvetica text. Enough for PyMuPDF to read back."""
    text_lines = [title, ""] + list(lines)
    content = ["BT", "/F1 14 Tf", "72 780 Td", "18 TL"]
    for line in text_lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content.append(f"({escaped}) Tj T*")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


_OOXML = "http://schemas.openxmlformats.org/"


def build_pptx(lines: list[str]) -> bytes:
    """A one-slide deck with a text box per line.

    Here for Step 4.2's formats gate. `.pptx` is a row in app/parse.py's
    backend table, and a row nothing ever reads is a claim rather than a fact
    -- which is exactly how that row shipped broken. Written against the OOXML
    parts docling's backend actually walks, with the standard library only, for
    the same reason as everything else in this file.
    """
    shapes = []
    for n, line in enumerate(lines, start=1):
        text = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        shapes.append(
            f'<p:sp><p:nvSpPr><p:cNvPr id="{n + 1}" name="TextBox {n}"/>'
            '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="838200" y="{900000 * n}"/>'
            '<a:ext cx="7772400" cy="800000"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
            '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US"/>'
            f"<a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>"
        )
    slide = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sld xmlns:a="{_OOXML}drawingml/2006/main" '
        f'xmlns:r="{_OOXML}officeDocument/2006/relationships" '
        f'xmlns:p="{_OOXML}presentationml/2006/main">'
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>'
        "<p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>"
        + "".join(shapes)
        + "</p:spTree></p:cSld></p:sld>"
    )
    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{_OOXML}package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
            '<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
            "</Types>",
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{_OOXML}package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{_OOXML}officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
            "</Relationships>",
        "ppt/presentation.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:presentation xmlns:a="{_OOXML}drawingml/2006/main" '
            f'xmlns:r="{_OOXML}officeDocument/2006/relationships" '
            f'xmlns:p="{_OOXML}presentationml/2006/main">'
            '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
            '<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
            "</p:presentation>",
        "ppt/_rels/presentation.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{_OOXML}package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{_OOXML}officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>'
            "</Relationships>",
        "ppt/slides/slide1.xml": slide,
        "ppt/slides/_rels/slide1.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{_OOXML}package/2006/relationships"/>',
    }
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def _cell(column: int, row: int, value) -> str:
    letter = ""
    n = column
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letter = chr(65 + remainder) + letter
    reference = f"{letter}{row}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{reference}"><v>{value}</v></c>'
    text = (
        str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return f'<c r="{reference}" t="inlineStr"><is><t>{text}</t></is></c>'


def build_xlsx(rows: list[list], sheet_name: str = "Sheet1") -> bytes:
    """A one-sheet workbook using inline strings, which openpyxl reads fine."""
    body = []
    for index, row in enumerate(rows, start=1):
        cells = "".join(_cell(column, index, value) for column, value in enumerate(row, start=1))
        body.append(f'<row r="{index}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(body)}</sheetData></worksheet>'
    )

    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        "xl/worksheets/sheet1.xml": sheet,
    }

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def xlsx_contains(data: bytes, needle: str) -> bool:
    """Look for a value in a workbook without needing openpyxl to be installed."""
    with zipfile.ZipFile(BytesIO(data)) as archive:
        for name in archive.namelist():
            if name.startswith("xl/") and name.endswith(".xml"):
                if needle in archive.read(name).decode("utf-8", errors="replace"):
                    return True
    return False
