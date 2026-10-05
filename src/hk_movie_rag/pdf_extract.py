"""Deterministic page-by-page PDF text extraction for the RAG bundle."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

_HISTORICAL_PROFILE = "historical-v1"
_CJK_LAYOUT_PROFILE = "cjk-layout-v1"
_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_PUNCTUATION = "，。！？；：、（）【】《》〈〉「」『』〔〕…—"


class PdfExtractError(ValueError):
    """Raised when a declared PDF cannot be read deterministically."""


@dataclass(frozen=True)
class PdfPage:
    page_number: int
    body: str
    body_sha256: str


def normalize_pdf_text(text: str, *, profile: str = _HISTORICAL_PROFILE) -> str:
    """Normalize line endings and horizontal whitespace without erasing paragraphs."""
    if profile not in {_HISTORICAL_PROFILE, _CJK_LAYOUT_PROFILE}:
        raise PdfExtractError(f"unknown PDF text extraction profile: {profile}")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[\t\f\v ]+", " ", line).strip() for line in text.split("\n")]
    if profile == _CJK_LAYOUT_PROFILE:
        punctuation = re.escape(_CJK_PUNCTUATION)
        left = f"[{_CJK}{punctuation}]"
        right = f"[{_CJK}{punctuation}]"
        lines = [re.sub(fr"(?<={left}) (?={right})", "", line) for line in lines]
    return "\n".join(lines).strip()


def extract_pdf_pages(
    path: Path, *, profile: str = _HISTORICAL_PROFILE
) -> tuple[PdfPage, ...]:
    """Return only non-empty, page-addressable normalized text from *path*."""
    try:
        reader = PdfReader(path)
        pages = []
        for page_number, page in enumerate(reader.pages, start=1):
            body = normalize_pdf_text(page.extract_text() or "", profile=profile)
            if body:
                pages.append(
                    PdfPage(
                        page_number=page_number,
                        body=body,
                        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    )
                )
        return tuple(pages)
    except PdfExtractError:
        raise
    except Exception as exc:
        raise PdfExtractError(f"PDF extraction failed: {path.name}") from exc
