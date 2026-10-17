import io
import re

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:  # pragma: no cover
    import fitz
from docx import Document


_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad]")


def extract_text(uploaded_file) -> str:
    """Extract plain text from an uploaded PDF, DOCX or TXT file."""
    return _INVISIBLE.sub("", _extract_raw(uploaded_file))


def _extract_raw(uploaded_file) -> str:
    name = uploaded_file.name.lower()
    data = uploaded_file.getvalue()

    if name.endswith(".pdf"):
        return _extract_pdf(data)
    if name.endswith(".docx"):
        return _extract_docx(data)
    if name.endswith(".txt"):
        return data.decode("utf-8", errors="ignore")
    raise ValueError(f"Unsupported file type: {name}")


def _extract_pdf(data: bytes) -> str:
    parts = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            # sort=True keeps two-column resumes in natural reading order
            parts.append(page.get_text("text", sort=True))
    return "\n".join(parts)


def _extract_docx(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    parts = []
    # Many resume templates put the candidate's name/contact in the page header.
    for section in doc.sections:
        for p in section.header.paragraphs:
            if p.text.strip():
                parts.append(p.text)
        for table in section.header.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
    parts += [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = list(dict.fromkeys(c.text.strip() for c in row.cells if c.text.strip()))
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)
