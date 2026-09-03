"""The four file tools, plus a listing helper.

These are plain Python functions. Nothing here knows a model exists, which is
the point: if a tool is broken you find out by calling it yourself, not by
guessing at a bad answer.

Every function returns a JSON-serialisable dict, because in Phase 03 the return
value is fed straight back to the model as the result of a tool call.

Rules that keep this safe to expose to a model:
  * filenames are resolved through config.resolve_in_data_dir, so nothing can
    touch a path outside DATA_DIR
  * output is truncated to a character budget, so one large PDF cannot blow
    up the context window
  * failures raise ToolError with a plain message the model can read and act on
"""

from __future__ import annotations

import difflib
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.config import UnsafePathError, ensure_data_dir, resolve_in_data_dir

TOOL_FOR_SUFFIX = {".pdf": "read_pdf", ".xlsx": "read_excel", ".xlsm": "read_excel"}

MAX_TEXT_CHARS = 20_000
MAX_ROWS = 200
MAX_COLS = 40


class ToolError(Exception):
    """Something the model can understand and retry differently."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _table_hint(filename: str) -> str | None:
    """Ask the database module whether this name is one of its tables.

    Imported inside the function on purpose: tools.py must keep working when
    no database is configured, and this is an error path where a failure to
    produce a hint should cost nothing.
    """
    try:
        from app import db
        return db.table_hint(filename)
    except Exception:  # noqa: BLE001
        return None


def _resolve(filename: str, must_exist: bool) -> Path:
    if not filename or not str(filename).strip():
        raise ToolError("No filename given.")
    try:
        path = resolve_in_data_dir(str(filename).strip())
    except UnsafePathError as exc:
        raise ToolError(str(exc)) from exc
    if must_exist and not path.is_file():
        available = [
            p.name for p in sorted(ensure_data_dir().iterdir())
            if p.is_file() and not p.name.startswith(".")
        ]
        # Before anything else: is the user asking for a database table?
        # "Report" is a table in the customer database and not a file, and
        # answering that with eighteen unrelated filenames is what sends the
        # model off hunting through the data folder and never coming back.
        hint = _table_hint(filename)
        if hint:
            raise ToolError(hint)

        if not available:
            raise ToolError(f"No file named {filename!r}. The data folder is empty.")
        message = f"No file named {filename!r} in the data folder."

        # Same name, different extension: the file is there, the tool was wrong.
        wanted_stem = Path(str(filename)).stem.lower()
        twins = [n for n in available if Path(n).stem.lower() == wanted_stem]
        if twins:
            hints = ", ".join(f"{n} (read it with {TOOL_FOR_SUFFIX[Path(n).suffix.lower()]})"
                              for n in twins if Path(n).suffix.lower() in TOOL_FOR_SUFFIX)
            if hints:
                raise ToolError(
                    f"{message} But this does exist: {hints}. "
                    "That is the same file in another format, so use it. Do not ask the user."
                )

        close = difflib.get_close_matches(str(filename), available, n=3, cutoff=0.35)
        if close:
            message += (
                f" The closest names are: {', '.join(close)}."
                " If exactly one of those is clearly the file meant, use it."
                " If more than one could be, ask the user which they want"
                " instead of choosing for them."
            )
        if len(available) <= 12:
            message += f" All files: {', '.join(available)}"
        else:
            message += (
                f" There are {len(available)} files in the folder; call "
                "list_files to see them all."
            )
        raise ToolError(message)
    return path


def _truncate(text: str, budget: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    if len(text) <= budget:
        return text, False
    return text[:budget], True


def _cell(value: Any) -> Any:
    """Make an openpyxl cell value safe to put in JSON."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def _parse_pages(pages: str | None, page_count: int) -> list[int]:
    """Turn '1-3,7' into zero-based page indexes. None means every page."""
    if pages is None or str(pages).strip() in {"", "all"}:
        return list(range(page_count))
    wanted: list[int] = []
    for part in str(pages).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start_s, end_s = part.split("-", 1)
                start, end = int(start_s), int(end_s)
            else:
                start = end = int(part)
        except ValueError as exc:
            raise ToolError(
                f"Could not read page range {part!r}. Use forms like '3' or '1-5' or '1-3,7'."
            ) from exc
        if start < 1 or end < start:
            raise ToolError(f"Page range {part!r} is not valid. Pages start at 1.")
        wanted.extend(range(start - 1, min(end, page_count)))
    return sorted(set(wanted))


# --------------------------------------------------------------------------
# tool 0: list_files
# --------------------------------------------------------------------------

def list_files() -> dict:
    """List the files the assistant can read or write.

    Not one of the original four, but without it the model has to be told a
    filename before it can do anything. Six lines, and it makes the rest usable.
    """
    folder = ensure_data_dir()
    files = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and not path.name.startswith("."):
            stat = path.stat()
            files.append(
                {
                    "name": path.name,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(
                        sep=" ", timespec="minutes"
                    ),
                    # Saying this here costs nothing and saves a wasted call:
                    # otherwise the model guesses the tool from the file name.
                    "read_with": TOOL_FOR_SUFFIX.get(path.suffix.lower(), "not readable"),
                }
            )
    return {"folder": str(folder), "count": len(files), "files": files}


# --------------------------------------------------------------------------
# tool 1: read_pdf
# --------------------------------------------------------------------------

def read_pdf(filename: str, pages: str | None = None) -> dict:
    """Extract text from a PDF.

    filename: name of a PDF in the data folder, e.g. "invoice.pdf"
    pages:    optional page selection like "1-3" or "2,5". Omit for all pages.
    """
    import pymupdf

    path = _resolve(filename, must_exist=True)
    if path.suffix.lower() != ".pdf":
        raise ToolError(f"{path.name} is not a PDF. Use read_excel for spreadsheets.")

    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 - surface any parse failure to the model
        raise ToolError(f"Could not open {path.name} as a PDF: {exc}") from exc

    with doc:
        page_count = doc.page_count
        wanted = _parse_pages(pages, page_count)
        if not wanted:
            raise ToolError(f"{path.name} has {page_count} pages; that selection matched none.")
        chunks = []
        for index in wanted:
            text = doc.load_page(index).get_text("text").strip()
            chunks.append(f"--- page {index + 1} ---\n{text}" if text else f"--- page {index + 1} ---\n(no extractable text)")

    body = "\n\n".join(chunks)
    body, truncated = _truncate(body)
    empty = all("(no extractable text)" in c for c in chunks)

    result = {
        "file": path.name,
        "page_count": page_count,
        "pages_read": [i + 1 for i in wanted],
        "characters": len(body),
        "truncated": truncated,
        "text": body,
    }
    if empty:
        result["note"] = (
            "No text layer found. This is probably a scanned PDF, which needs OCR "
            "rather than text extraction."
        )
    return result


# --------------------------------------------------------------------------
# tool 2: read_excel
# --------------------------------------------------------------------------

def read_excel(filename: str, sheet: str | None = None, max_rows: int = MAX_ROWS) -> dict:
    """Read the cells of an Excel worksheet.

    filename: name of an .xlsx in the data folder
    sheet:    optional sheet name. Omit for the first sheet.
    max_rows: how many data rows to return at most.
    """
    from openpyxl import load_workbook

    path = _resolve(filename, must_exist=True)
    suffix = path.suffix.lower()
    if suffix not in {".xlsx", ".xlsm"}:
        right_tool = TOOL_FOR_SUFFIX.get(suffix)
        if right_tool:
            raise ToolError(
                f"{path.name} is not a spreadsheet, it is a {suffix} file. "
                f"Read it with {right_tool} instead."
            )
        raise ToolError(
            f"{path.name} is not a spreadsheet. Only .xlsx and .xlsm can be read; "
            "the old .xls format cannot."
        )

    try:
        book = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Could not open {path.name} as a workbook: {exc}") from exc

    try:
        names = book.sheetnames
        note = None
        if sheet is None or sheet in names:
            ws = book[sheet] if sheet else book[names[0]]
        elif len(names) == 1:
            # Only one sheet exists, so the requested name can only be a guess.
            ws = book[names[0]]
            note = (
                f"There is no sheet named {sheet!r}; this file has only one sheet, "
                f"{names[0]!r}, so that is what was read."
            )
        else:
            raise ToolError(
                f"No sheet named {sheet!r}. Sheets in this file: {', '.join(names)}. "
                "Call read_excel again with one of those, or omit sheet for the first."
            )

        rows: list[list[Any]] = []
        total = 0
        for row in ws.iter_rows(values_only=True):
            total += 1
            if len(rows) < max(1, int(max_rows)):
                rows.append([_cell(v) for v in row[:MAX_COLS]])

        # trim fully empty trailing rows, which openpyxl often reports
        while rows and all(v is None for v in rows[-1]):
            rows.pop()
        sheet_title = ws.title
    finally:
        book.close()

    header = rows[0] if rows else []
    result = {
        "file": path.name,
        "sheet": sheet_title,
        "sheets_available": names,
        "total_rows": total,
        "rows_returned": len(rows),
        "truncated": total > len(rows),
        "likely_header": header,
        "rows": rows,
    }
    if note:
        result["note"] = note
    return result


# --------------------------------------------------------------------------
# tool 3: write_excel
# --------------------------------------------------------------------------

def write_excel(
    filename: str,
    rows: Sequence[Sequence[Any]],
    sheet: str | None = None,
    mode: str = "overwrite",
    headers: Sequence[Any] | None = None,
) -> dict:
    """Create an Excel file, or append rows to one that exists.

    filename: name of the .xlsx to write in the data folder
    rows:     list of rows, each row a list of cell values
    sheet:    sheet name. Defaults to the first sheet, or "Sheet1" on create.
    mode:     "overwrite" to replace the sheet's contents, "append" to add rows
              to the end of it.
    headers:  optional header row written above the data on overwrite/create.

    A cell value beginning with "=" is written as a live Excel formula, so
    "=SUM(B2:B10)" behaves the way it would if you typed it.
    """
    from openpyxl import Workbook, load_workbook

    mode = str(mode).lower().strip()
    if mode not in {"overwrite", "append"}:
        raise ToolError(f"mode must be 'overwrite' or 'append', got {mode!r}.")

    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes)):
        raise ToolError("rows must be a list of rows, each row a list of cell values.")
    clean: list[list[Any]] = []
    for row in rows:
        if isinstance(row, (str, bytes)) or not isinstance(row, Iterable):
            raise ToolError("Each row must be a list of cell values, not a single value.")
        clean.append(list(row))
    if not clean and mode == "append":
        raise ToolError("No rows given to append.")

    path = _resolve(filename, must_exist=False)
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ToolError("write_excel only writes .xlsx files. Give the filename an .xlsx suffix.")

    existed = path.is_file()
    if existed:
        try:
            book = load_workbook(path)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"Could not open existing {path.name}: {exc}") from exc
        if sheet is None:
            ws = book[book.sheetnames[0]]
        elif sheet in book.sheetnames:
            ws = book[sheet]
        else:
            ws = book.create_sheet(sheet)
    else:
        if mode == "append":
            raise ToolError(
                f"Cannot append: {path.name} does not exist yet. "
                "Use mode='overwrite' to create it."
            )
        book = Workbook()
        ws = book.active
        ws.title = sheet or "Sheet1"

    if mode == "overwrite":
        ws.delete_rows(1, ws.max_row)
        if headers:
            ws.append(list(headers))

    start_row = ws.max_row
    for row in clean:
        ws.append(row)

    try:
        book.save(path)
    except PermissionError as exc:
        raise ToolError(
            f"Could not save {path.name}: the file is open in Excel. Close it and retry."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Could not save {path.name}: {exc}") from exc
    finally:
        book.close()

    return {
        "file": path.name,
        "path": str(path),
        "sheet": ws.title,
        "action": "appended to" if (existed and mode == "append") else ("overwrote" if existed else "created"),
        "rows_written": len(clean),
        "total_rows_now": ws.max_row,
        "started_at_row": start_row + 1 if clean else start_row,
    }


# --------------------------------------------------------------------------
# tool 4: write_pdf
# --------------------------------------------------------------------------

def write_pdf(filename: str, title: str, body: str) -> dict:
    """Create a simple text PDF: a title, then paragraphs of body text.

    filename: name of the .pdf to write in the data folder
    title:    heading printed at the top of the first page
    body:     the text. Blank lines separate paragraphs; a line starting with
              "# " becomes a section heading.
    """
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape

    path = _resolve(filename, must_exist=False)
    if path.suffix.lower() != ".pdf":
        raise ToolError("write_pdf only writes .pdf files. Give the filename a .pdf suffix.")
    if not str(title).strip():
        raise ToolError("A title is required.")
    if not str(body).strip():
        raise ToolError("The body text is empty; there would be nothing on the page.")

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DocTitle", parent=styles["Title"], alignment=TA_LEFT, fontSize=20, spaceAfter=14
    )
    heading_style = ParagraphStyle(
        "DocHeading", parent=styles["Heading2"], fontSize=13, spaceBefore=12, spaceAfter=5
    )
    body_style = ParagraphStyle(
        "DocBody", parent=styles["BodyText"], fontSize=10.5, leading=15, spaceAfter=8
    )

    story = [Paragraph(escape(str(title)), title_style), Spacer(1, 4)]
    paragraphs = 0
    for block in str(body).replace("\r\n", "\n").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("# "):
            story.append(Paragraph(escape(block[2:].strip()), heading_style))
        else:
            story.append(Paragraph(escape(block).replace("\n", "<br/>"), body_style))
        paragraphs += 1

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        title=str(title),
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    try:
        doc.build(story)
    except PermissionError as exc:
        raise ToolError(
            f"Could not save {path.name}: the file is open in a PDF viewer. Close it and retry."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Could not build {path.name}: {exc}") from exc

    # Report what actually went in, not just that something did.
    # "1 page, 2 paragraphs, 2.1 KB" reads like success whether the body was a
    # hundred rows of data or a two-line summary of them. The character count
    # and the opening line are the difference, and they are what lets the model
    # -- and the user reading the transcript -- notice a summary was written
    # where the real content was asked for.
    text = str(body).strip()
    opening = " ".join(text.split())[:160]
    return {
        "file": path.name,
        "path": str(path),
        "pages": doc.page,
        "paragraphs": paragraphs,
        "characters_written": len(text),
        "lines_written": len(text.splitlines()),
        "starts_with": opening + ("..." if len(opening) == 160 else ""),
        "size_kb": round(path.stat().st_size / 1024, 1),
        "note": "characters_written is what you actually put on the page. If "
                "the user asked you to save data you had retrieved and this "
                "number is small, you summarised instead of saving it. Fetch "
                "the data again and write the real rows.",
    }
