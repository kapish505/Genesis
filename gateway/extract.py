"""Content extraction: PDF, HTML, JSON, and plain text.

Extraction runs only in the gateway. Generated agents never parse documents, so
the hosted allowlist stays tiny and a parser bug cannot run inside a generated
agent.
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 200_000


class ExtractionError(ValueError):
    pass


def _normalise_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _relevant(text: str, query: str, window: int = 600) -> str:
    """Keep passages around query terms; fall back to the head of the document."""
    terms = [term.casefold() for term in re.findall(r"[A-Za-z0-9ऀ-ॿ]{3,}", query)]
    if not terms:
        return text[:MAX_TEXT_CHARS]
    lowered = text.casefold()
    spans: list[tuple[int, int]] = []
    for term in dict.fromkeys(terms):
        start = 0
        while True:
            index = lowered.find(term, start)
            if index < 0 or len(spans) >= 12:
                break
            spans.append((max(0, index - window // 3), min(len(text), index + window)))
            start = index + len(term)
    if not spans:
        return text[:MAX_TEXT_CHARS]
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return "\n\n...\n\n".join(text[start:end] for start, end in merged)[:MAX_TEXT_CHARS]


def extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is installed in prod
        raise ExtractionError("PDF extraction is unavailable on this gateway") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
        text = "\n\n".join(pages)
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types
        raise ExtractionError(f"could not read the PDF: {exc}") from exc
    text = _normalise_whitespace(text)
    if not text:
        raise ExtractionError("the PDF contains no extractable text (it may be scanned)")
    return text


def extract_html(data: bytes, query: str = "") -> str:
    raw = data.decode("utf-8", errors="replace")
    text = ""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            raw,
            include_comments=False,
            include_tables=True,
            favor_precision=bool(query),
            no_fallback=False,
        )
        text = extracted or ""
    except Exception as exc:  # noqa: BLE001 - trafilatura is best-effort
        logger.warning("trafilatura failed, falling back to text stripping: %s", exc)
    if not text:
        text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = _normalise_whitespace(text)
    if not text:
        raise ExtractionError("the page had no readable text")
    return _relevant(text, query) if query else text[:MAX_TEXT_CHARS]


def extract_json(data: bytes) -> str:
    try:
        value = json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"invalid JSON: {exc.msg}") from exc
    return json.dumps(value, indent=2, ensure_ascii=False)[:MAX_TEXT_CHARS]


def detect_kind(content_type: str | None, data: bytes, hint: str | None = None) -> str:
    if hint in {"html", "pdf", "text", "json"}:
        return hint
    base = (content_type or "").split(";")[0].strip().casefold()
    if base == "application/pdf" or data[:5] == b"%PDF-":
        return "pdf"
    if base in {"application/json", "text/json"}:
        return "json"
    if base in {"text/html", "application/xhtml+xml", "application/xml", "text/xml"}:
        return "html"
    return "text"


def extract(
    data: bytes, content_type: str | None, query: str = "", hint: str | None = None
) -> dict[str, Any]:
    kind = detect_kind(content_type, data, hint)
    if kind == "pdf":
        text = extract_pdf(data)
    elif kind == "json":
        text = extract_json(data)
    elif kind == "html":
        text = extract_html(data, query)
    else:
        text = _normalise_whitespace(data.decode("utf-8", errors="replace"))
        text = _relevant(text, query) if query else text[:MAX_TEXT_CHARS]
    if not text.strip():
        raise ExtractionError("no text could be extracted")
    return {"kind": kind, "text": text[:MAX_TEXT_CHARS], "chars": min(len(text), MAX_TEXT_CHARS)}
