"""Behavior tests for deterministic PDF text normalization."""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from hk_movie_rag.pdf_extract import PdfExtractError, extract_pdf_pages, normalize_pdf_text


def test_cjk_layout_profile_collapses_only_cjk_layout_spacing() -> None:
    """Breaks if CJK layout spaces remain or meaningful ASCII spacing is erased."""
    source = (
        "富 貴 ， 逼 人 。\n"
        "It's a mad, mad, mad world\n"
        "https://example.test/a b\n"
        "\n"
        "Scene 01 00:01:23 - 00:02:34"
    )

    assert normalize_pdf_text(source, profile="cjk-layout-v1") == (
        "富貴，逼人。\n"
        "It's a mad, mad, mad world\n"
        "https://example.test/a b\n"
        "\n"
        "Scene 01 00:01:23 - 00:02:34"
    )


def test_historical_profile_preserves_legacy_spacing() -> None:
    """Breaks if schema-1.1 reproduction silently changes its page hashes."""
    assert normalize_pdf_text("富 貴 逼 人", profile="historical-v1") == "富 貴 逼 人"


def test_pdf_normalization_rejects_unknown_profile() -> None:
    """Breaks if an undeclared extraction algorithm can enter release identity."""
    with pytest.raises(PdfExtractError, match="unknown PDF text extraction profile"):
        normalize_pdf_text("text", profile="future-profile")


def test_cjk_layout_extraction_omits_a_trailing_blank_physical_page(
    tmp_path: Path,
) -> None:
    """Breaks if a harmless blank sheet after the logical document is rejected."""
    path = tmp_path / "trailing-blank.pdf"
    _write_pdf_pages(path, ("page one", "page two", None))

    pages = extract_pdf_pages(path, profile="cjk-layout-v1")

    assert [page.page_number for page in pages] == [1, 2]
    assert [page.body for page in pages] == ["page one", "page two"]


def _write_pdf_pages(path: Path, page_texts: tuple[str | None, ...]) -> None:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    for text in page_texts:
        page = writer.add_blank_page(width=100, height=100)
        if text is None:
            continue
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        contents = DecodedStreamObject()
        contents.set_data(f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(contents)
    with path.open("wb") as stream:
        writer.write(stream)
