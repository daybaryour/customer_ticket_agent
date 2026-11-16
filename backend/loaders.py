"""Text extraction for knowledge-base uploads.

``extract_text`` turns raw file bytes into a list of ``(text, metadata)`` items
that :func:`rag.add_document` can index. PDFs are extracted page by page, CSV
question/answer rows are flagged with ``csv_qa`` so each row stays a single
chunk, and DOCX paragraphs and tables are flattened into one text block.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from docx import Document as DocxDocument
from pypdf import PdfReader

MAX_FILE_BYTES = 10 * 1024 * 1024

# Extension -> source_type stored in the kb_documents table.
SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".pdf": "pdf",
    ".csv": "csv",
    ".docx": "docx",
    ".txt": "txt",
    ".md": "md",
}

QUESTION_COLUMNS = ("question", "q")
ANSWER_COLUMNS = ("answer", "a", "response")


def source_type_for(filename: str) -> str:
    """Return the kb_documents source_type for ``filename``.

    Raises:
        ValueError: If the file extension is not one of the supported types.
    """
    suffix = Path(filename or "").suffix.lower()
    source_type = SUPPORTED_EXTENSIONS.get(suffix)
    if source_type is None:
        allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        shown = suffix or (filename or "file")
        raise ValueError(
            f"Unsupported file type '{shown}'. Supported types: {allowed}."
        )
    return source_type


def extract_text(
    file_bytes: bytes, filename: str
) -> list[tuple[str, dict[str, Any]]]:
    """Extract indexable text items from an uploaded file.

    Args:
        file_bytes: Raw bytes of the uploaded file.
        filename: Original filename, used to pick the parser and validate the
            extension.

    Returns:
        A list of ``(text, metadata)`` tuples. Metadata may contain ``page``
        for PDF pages, ``row`` for CSV rows, and ``csv_qa`` for CSV
        question/answer rows that must not be split.

    Raises:
        ValueError: If the file is empty, larger than 10 MB, has an
            unsupported extension, cannot be decoded/parsed, or contains no
            extractable text. Scanned PDFs raise a dedicated OCR error.
    """
    if not file_bytes:
        raise ValueError("The uploaded file is empty.")

    if len(file_bytes) > MAX_FILE_BYTES:
        raise ValueError("File is too large; the maximum size is 10 MB.")

    source_type = source_type_for(filename)

    if source_type == "pdf":
        return _extract_pdf(file_bytes)
    if source_type == "docx":
        return _extract_docx(file_bytes)
    if source_type == "csv":
        return _extract_csv(file_bytes)
    return _extract_plain_text(file_bytes)


def _extract_pdf(file_bytes: bytes) -> list[tuple[str, dict[str, Any]]]:
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
    except Exception as exc:  # pypdf raises several parser-specific errors
        raise ValueError(f"Could not read the PDF file: {exc}") from exc

    items: list[tuple[str, dict[str, Any]]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # A broken page should not abort the whole upload
            text = ""
        if text.strip():
            items.append((text, {"page": page_number}))

    if not items:
        raise ValueError(
            "This PDF has no extractable text; it looks like a scanned "
            "document. OCR is not supported."
        )
    return items


def _extract_docx(file_bytes: bytes) -> list[tuple[str, dict[str, Any]]]:
    try:
        document = DocxDocument(io.BytesIO(file_bytes))
    except Exception as exc:
        raise ValueError(f"Could not read the DOCX file: {exc}") from exc

    blocks: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            blocks.append(text)

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                blocks.append(" | ".join(cells))

    text = "\n\n".join(blocks).strip()
    if not text:
        raise ValueError("No text was found in this DOCX file.")
    return [(text, {})]


def _extract_csv(file_bytes: bytes) -> list[tuple[str, dict[str, Any]]]:
    text = _decode(file_bytes)
    rows = [
        row
        for row in csv.reader(io.StringIO(text))
        if any(cell.strip() for cell in row)
    ]
    if not rows:
        raise ValueError("The CSV file is empty.")

    header = [cell.strip() for cell in rows[0]]
    normalized = [cell.lower() for cell in header]

    question_index = next(
        (index for index, name in enumerate(normalized) if name in QUESTION_COLUMNS),
        None,
    )
    answer_index = next(
        (index for index, name in enumerate(normalized) if name in ANSWER_COLUMNS),
        None,
    )

    items: list[tuple[str, dict[str, Any]]] = []

    if (
        question_index is not None
        and answer_index is not None
        and question_index != answer_index
    ):
        for row_number, row in enumerate(rows[1:], start=2):
            question = row[question_index].strip() if question_index < len(row) else ""
            answer = row[answer_index].strip() if answer_index < len(row) else ""
            if not question and not answer:
                continue
            items.append(
                (
                    f"Q: {question}\nA: {answer}",
                    {"row": row_number, "csv_qa": True},
                )
            )

        if not items:
            raise ValueError("No question/answer rows were found in this CSV file.")
        return items

    for row_number, row in enumerate(rows[1:], start=2):
        lines: list[str] = []
        for name, value in zip(header, row):
            value = value.strip()
            if name and value:
                lines.append(f"{name}: {value}")

        # Rows with more cells than the header still keep their extra values.
        if len(row) > len(header):
            lines.extend(cell.strip() for cell in row[len(header):] if cell.strip())

        if lines:
            items.append(("\n".join(lines), {"row": row_number}))

    if not items:
        raise ValueError("No data rows were found in this CSV file.")
    return items


def _extract_plain_text(file_bytes: bytes) -> list[tuple[str, dict[str, Any]]]:
    text = _decode(file_bytes)
    if not text.strip():
        raise ValueError("The file contains no text.")
    return [(text, {})]


def _decode(file_bytes: bytes) -> str:
    try:
        return file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("File must be UTF-8 encoded text.") from exc

