import os
from datetime import date, datetime
from pathlib import Path

import pytest

from hk_movie_rag import release_data
from hk_movie_rag.release_data import (
    MOVIE_COLUMNS,
    ReleaseDataError,
    assert_disjoint,
    join_tiers,
    validate_movie_rows,
)


def test_release_rows_reject_duplicate_movie_id() -> None:
    """Breaks if duplicate release identifiers are allowed into canonical outputs."""
    rows = [_movie_row("1970_GSQ_001"), _movie_row("1970_GSQ_001")]

    with pytest.raises(ReleaseDataError, match="duplicate movie id"):
        validate_movie_rows(rows)


def test_release_rows_reject_source_column_reordering() -> None:
    """Breaks if a reordered source schema can be silently mapped to the wrong fields."""
    reordered = list(MOVIE_COLUMNS)
    reordered[0], reordered[1] = reordered[1], reordered[0]

    with pytest.raises(ReleaseDataError, match="column order"):
        validate_movie_rows([_movie_row("1970_GSQ_001")], columns=reordered)


def test_quarantine_ids_cannot_enter_release() -> None:
    """Breaks if a quarantined identifier can leak into the release population."""
    with pytest.raises(ReleaseDataError, match="quarantine overlap"):
        assert_disjoint({"1970_GSQ_001"}, {"1970_GSQ_001"})


def test_tier_join_is_exact_id_only() -> None:
    """Breaks if tier assignment joins on title, row position, or another fuzzy key."""
    result = join_tiers(
        [_movie_row("1970_GSQ_001"), _movie_row("1970_YCCS_001")],
        [_tier_row("1970_YCCS_001", "A"), _tier_row("1970_GSQ_001", "B")],
    )

    assert result[0].movie_id == "1970_GSQ_001"
    assert result[0].tier == "B"
    assert result[1].movie_id == "1970_YCCS_001"
    assert result[1].tier == "A"


@pytest.mark.parametrize(
    ("source_value", "expected"),
    [
        (
            "2026-07-17 02:10:25.955000",
            "2026-07-17T02:10:25.955000",
        ),
        (
            datetime(2026, 7, 17, 2, 10, 25, 955000),  # noqa: DTZ001
            "2026-07-17T02:10:25.955000",
        ),
        (date(2026, 7, 17), "2026-07-17T00:00:00.000000"),
        (46220.10457175926, "2026-07-17T02:30:35.000000"),
    ],
    ids=["text", "datetime", "date", "excel-serial"],
)
def test_retrieved_at_uses_one_fixed_naive_iso_format(
    source_value: object, expected: str
) -> None:
    """Breaks if any workbook cell representation leaks into provenance storage."""
    assert release_data._retrieved_at_text(source_value) == expected


def test_artifact_write_replaces_hardlink_without_mutating_target(tmp_path: Path) -> None:
    """Breaks if an existing output link is followed instead of atomically replaced."""
    protected_target = tmp_path / "protected-source.csv"
    protected_target.write_bytes(b"protected-source-bytes")
    output_dir = tmp_path / "release"
    output_dir.mkdir()
    output_path = output_dir / "movies.csv"
    os.link(protected_target, output_path)
    assert os.path.samefile(output_path, protected_target)

    release_data._write_outputs(
        output_dir,
        movies=[{column: "" for column in release_data.MOVIE_STORAGE_COLUMNS}],
        tiers=[],
        pilots=[],
        provenance=[],
    )

    assert protected_target.read_bytes() == b"protected-source-bytes"
    assert not os.path.samefile(output_path, protected_target)
    assert output_path.read_bytes().startswith(b"movie_id,chinese_title,english_title,")
    assert not list(output_dir.glob(".*.tmp"))


def _movie_row(movie_id: str) -> dict[str, str]:
    return {
        "影片唯一ID": movie_id,
        "中文片名": "高山青",
        "英文片名": "The evergreen mountains",
        "上映日期": "1970-01-01",
        "出品地區": "香港",
        "導演": "李嘉",
        "編劇": "李至善",
        "主演": "葛香亭、甄珍、武家麒",
        "影片類型": "",
        "片長": "",
        "出品公司": "香港 :萬聲電影有限公司",
        "數據來源": "香港電影資料館",
    }


def _tier_row(movie_id: str, tier: str) -> dict[str, str | bool]:
    return {
        "影片唯一ID": movie_id,
        "分級": tier,
        "分級依據": "結構化評分=1",
        "人工覆核": "not_required",
        "試點影片": False,
    }
