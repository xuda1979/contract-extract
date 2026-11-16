"""Text extraction from PDF and DOCX.  PDF is parsed locally with pypdf and
pdfplumber (fallback on legacy/complex files); DOCX with python-docx.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from contract_extract.errors import UnsupportedFormatError


@dataclass
class Page:
    """One page of extracted text and its file page number."""

    text: str
    page_index: int  # 0-based
    page_number: int  # 1-based

    def __post_init__(self) -> None:
        # Normalise whitespace per page once.
        self.text = re.sub(r"[ \t]+", " ", self.text).strip()


@dataclass
class Document:
    """Extracted text of a document, page-aware."""

    source: str
    format: str  # 'pdf' | 'docx'
    pages: list[Page]
    nonempty_pages: int = 0
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.nonempty_pages = sum(1 for p in self.pages if p.text.strip())

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    def page_number_of(self, text: str) -> int | None:
        """Return the 1-based page number of the first page containing `text`."""
        for page in self.pages:
            if text in page.text:
                return page.page_number
        return None

    def page_of(self, text: str) -> Page | None:
        for page in self.pages:
            if text in page.text:
                return page
        return None


# ---------------------------------------------------------------------------
# Word fragment pipelines
# ---------------------------------------------------------------------------


def _restore_hyphenation(text: str) -> str:
    """Join 'con-\nstraint' style hyphen breaks minus the '-'.

    Also joins plain continuing lines when a line ends with a hyphen and the
    word continues on the next line.
    """
    text = re.sub(r"(?<=\w)-\n(?=\w)", "", text)
    return text


def _join_paragraph_breaks(text: str) -> str:
    """Re-join lines that were broken mid-paragraph in the source.

    We must not join across a real blank line: hard line breaks (blank line)
    are trusted as paragraph boundaries in the fixture corpus.  Soft breaks
    (paragraph continues on the very next line) are joined so multi-word
    quoted snippets in DOCX data pieces survive intact.
    """
    lines = text.splitlines()
    joined: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:  # blank line -> paragraph boundary
            joined.append("")
            continue
        if len(joined) > 0 and joined[-1] and not _ends_sentenceish(joined[-1]):
            joined[-1] += " " + s
        else:
            joined.append(s)
    return "\n".join(joined)


_SENTENCE_END = re.compile(r"[.!?:。！？；;」』）)]\s*$")


def _ends_sentenceish(s: str) -> bool:
    return bool(_SENTENCE_END.search(s))


def _normalise_text(doc_text: str) -> str:
    """Stage 1: DOCX/PdfPlumber text clean-up.

    - hyphenation across line breaks,
    - collapse multiple spaces,
    - re-join soft line breaks,
    - normalise CRLF.
    """
    text = doc_text.replace("\r\n", "\n").replace("\r", "\n")
    text = _restore_hyphenation(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = _join_paragraph_breaks(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def extract_pdf(path: str | Path, progress: callable | None = None) -> Document:
    """Locally extract text from a PDF with pypdf + pdfplumber.

    pdfplumber is primary (best layout).  Fall back to pypdf when pdfplumber
    cannot coalesce the layout (e.g. some scanned or table-heavy files).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file():
        raise IsADirectoryError(path)

    warnings: list[str] = []
    pages: list[Page] = []

    # --- pypdf pass -----------------------------------------------------
    try:
        import pypdf

        reader = pypdf.PdfReader(str(path))
        py_texts = [(p.extract_text() or "") for p in reader.pages]
    except FileNotFoundError:
        raise
    except Exception as exc:  # malformed PDF
        raise UnsupportedFormatError(f"pypdf could not read {path}: {exc!r}") from exc

    # --- pdfplumber pass -----------------------------------------------
    plumb_texts: list[str] | None = None
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            _buf: list[str] = []
            for i, pg in enumerate(pdf.pages):
                _buf.append(pg.extract_text() or "")
            plumb_texts = _buf
    except Exception as exc:  # not a valid PDF
        if plumb_texts is None:
            plumb_texts = py_texts
        warnings.append(f"pdfplumber pass failed ({exc!r}); used pypdf text.")

    # --- pick per page ---------------------------------------------------
    n = max(len(py_texts), len(plumb_texts or []))
    for i in range(n):
        plumb = plumb_texts[i] if plumb_texts and i < len(plumb_texts) else ""
        pyp = py_texts[i] if i < len(py_texts) else ""
        text = plumb if len(plumb) >= len(pyp) else pyp
        pages.append(Page(_normalise_text(text), i, i + 1))

    return Document(str(path), "pdf", pages, warnings=warnings)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def extract_docx(path: str | Path) -> Document:
    """Extract text and per-paragraph page heuristics from a DOCX file.

    DOCX has no native page numbers.  We guess the page of every paragraph
    from the line height of the document and report a conservative estimate.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file():
        raise IsADirectoryError(path)

    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("python-docx is required for DOCX input.") from exc

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise UnsupportedFormatError(f"Not a valid DOCX file {path}: {exc!r}") from exc

    paras: list[str] = [p.text for p in document.paragraphs]
    # Non-empty paragraphs only.
    paras = [p for p in paras if p.strip()]

    # Estimate pages: assume A4, 11pt / 1.15, ~44 lines per page.
    lines_per_page = 44
    pages: list[Page] = []
    current_text: list[str] = []
    page_number = 1

    def _flush() -> None:
        nonlocal current_text, page_number
        if current_text:
            pages.append(Page(_normalise_text("\n".join(current_text)), page_number - 1, page_number))
        current_text = []

    line_count = 0
    for para in paras:
        n_lines = max(1, len(para) // 80 + 1)
        if line_count + n_lines > lines_per_page:
            _flush()
            page_number += 1
            line_count = 0
        current_text.append(para)
        line_count += n_lines
    _flush()

    warnings: list[str] = []
    try:
        with zipfile.ZipFile(str(path)) as zf:
            if "docProps/core.xml" in zf.namelist():
                core = zf.read("docProps/core.xml").decode("utf-8", "replace")
                # No title handling here (kept simple); warnings stay empty.
    except Exception:
        pass  # core properties absent: not a problem.

    return Document(str(path), "docx", pages, warnings=warnings)


# ---------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------


def load_document(path: str | Path, progress: callable | None = None) -> Document:
    """Load any supported document (pdf/docx)."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract_pdf(path, progress=progress)
    if ext in (".docx", ".doc"):
        return extract_docx(path)
    raise UnsupportedFormatError(f"Unsupported extension {path.suffix!r} (expect .pdf/.docx)")
