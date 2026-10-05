"""Opt-in full-population proof for the locally governed Release v1.2 source."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest
from opencc import OpenCC

from hk_movie_rag.rag_bundle import RagBundleError, build_rag_bundle, verify_rag_bundle
from hk_movie_rag.retrieval import (
    explicit_title_intent_position,
    normalize_query_text,
    normalize_title_text,
)

_PARENT_MANIFEST_SHA256 = "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
_PARENT_POSTERS_SHA256 = "db08e8cf15ab97f7ac33134cb6b34af50dc0ff371d001ad5a3edc63bce1d5c78"


def test_real_release_titles_share_query_and_stored_equivalence_keys() -> None:
    """Breaks if full-query OpenCC context drifts outside resolver equality."""
    source_value = os.environ.get("HK_MOVIE_RAG_SOURCE_ROOT")
    if not source_value:
        pytest.skip("set HK_MOVIE_RAG_SOURCE_ROOT for the local full-population proof")
    movies_path = (
        Path(source_value).resolve() / "data" / "release" / "v1.2" / "movies.csv"
    )
    with movies_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    simplify = OpenCC("hk2s")
    mismatches: list[str] = []
    prefixes = (
        "", "看", "看看", "想看", "讲一下", "聊聊", "介绍", "分析", "请问",
        "为什么", "关于", "香港电影", "电影", "这部电影", "推荐", "我想了解",
        "比较", "重看", "细说", "说说", "讨论", "评价", "解析", "喜欢", "不喜欢",
        "记得", "听说", "寻找", "有没有", "经典",
    )
    suffixes = (
        "", "的", "怎么样", "好看吗", "的导演", "的剧情", "的创作", "的美学",
        "的资料", "的结局", "的意义", "里", "中", "上", "下", "前", "后", "版",
        "电影", "是谁演的", "为何经典", "与醉拳比较", "和其他电影", "讲什么",
        "值得看吗", "的时代背景", "的喜剧创作", "的视觉风格", "的动作设计",
        "的故事",
    )
    query_templates = tuple(f"{prefix}{{}}" for prefix in prefixes) + tuple(
        f"{{}}{suffix}" for suffix in suffixes
    )
    for row in rows:
        movie_id = str(row["movie_id"])
        stored_title = str(row["chinese_title"]).strip()
        stored_key = normalize_title_text(stored_title)
        query_titles = (stored_title, simplify.convert(stored_title))
        if any(
            stored_key
            not in normalize_title_text(
                normalize_query_text(template.format(query_title))
            )
            for query_title in query_titles
            for template in query_templates
        ):
            mismatches.append(movie_id)

    assert len(rows) == 4658
    assert mismatches == []


def test_real_release_titles_support_bounded_request_templates() -> None:
    """Breaks if a bounded request form works only for hand-picked titles."""
    source_value = os.environ.get("HK_MOVIE_RAG_SOURCE_ROOT")
    if not source_value:
        pytest.skip("set HK_MOVIE_RAG_SOURCE_ROOT for the local full-population proof")
    movies_path = (
        Path(source_value).resolve() / "data" / "release" / "v1.2" / "movies.csv"
    )
    with movies_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    simplify = OpenCC("hk2s")
    query_templates = (
        "我想问一下{}的导演是谁？",
        "请问一下{}的导演是谁？",
        "关于{}的视觉风格",
        "想知道{}的剧情",
        "能介绍一下{}的动作设计吗？",
        "可不可以聊聊{}的喜剧创作？",
    )
    misses: list[str] = []
    for row in rows:
        movie_id = str(row["movie_id"])
        stored_title = str(row["chinese_title"]).strip()
        query_titles = (stored_title, simplify.convert(stored_title))
        if any(
            explicit_title_intent_position(
                template.format(query_title), stored_title
            )
            < 0
            for query_title in query_titles
            for template in query_templates
        ):
            misses.append(movie_id)

    assert len(rows) == 4658
    assert misses == []


def test_real_release_build_has_4658_primary_rag_rows_and_keeps_parent_immutable(
    tmp_path: Path,
) -> None:
    """Breaks if the full RAG derivation changes parent bytes or loses its poster policy."""
    source_value = os.environ.get("HK_MOVIE_RAG_SOURCE_ROOT")
    if not source_value:
        pytest.skip("set HK_MOVIE_RAG_SOURCE_ROOT for the local full-population proof")
    source_root = Path(source_value).resolve()
    config_path = Path(__file__).resolve().parents[2] / "config" / "rag_demo.yaml"
    parent_manifest = source_root / "data" / "release" / "v1.2" / "release_manifest.json"
    parent_posters = source_root / "data" / "release" / "v1.2" / "poster_manifest.parquet"
    parent_tiers = source_root / "data" / "release" / "v1.2" / "movie_tiers.parquet"
    parent_pilots = source_root / "data" / "release" / "v1.2" / "pilot_movies.parquet"
    before = (
        _sha256(parent_manifest),
        _sha256(parent_posters),
        _sha256(parent_tiers),
        _sha256(parent_pilots),
    )
    assert before == (
        _PARENT_MANIFEST_SHA256,
        _PARENT_POSTERS_SHA256,
        "d9bb0ddd8ab7269bb9b3948be7e34f4a032dc2fe7d59a134df02b5b2ff3c4256",
        "ad2e8cf500a70f2f2d8e063aacb6ea26643d75268046de8ce2076e54fdd07565",
    )

    first = build_rag_bundle(source_root, config_path, tmp_path / "one")
    second = build_rag_bundle(source_root, config_path, tmp_path / "two")
    verified = verify_rag_bundle(first.manifest_path)
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    records = list(verified.iter_records())
    posters = [record for record in records if record.get("record_kind") == "poster"]
    facets = [record for record in records if record.get("record_kind") == "facet"]
    quality = Counter(str(record.get("quality_status")) for record in posters)
    approved = [
        record
        for record in posters
        if record.get("quality_status") in {"machine_passed", "manual_approved"}
        and record.get("derived_object_uri")
    ]

    assert first.bundle_sha256 == second.bundle_sha256
    assert first.manifest_bytes == second.manifest_bytes
    assert verified.movie_count == 4658
    assert manifest["facet_artifacts"] == {
        "movie_tiers": {
            "relative_path": "data/release/v1.2/movie_tiers.parquet",
            "sha256": "d9bb0ddd8ab7269bb9b3948be7e34f4a032dc2fe7d59a134df02b5b2ff3c4256",
            "row_count": 4658,
        },
        "pilot_movies": {
            "relative_path": "data/release/v1.2/pilot_movies.parquet",
            "sha256": "ad2e8cf500a70f2f2d8e063aacb6ea26643d75268046de8ce2076e54fdd07565",
            "row_count": 24,
        },
    }
    assert first.facet_count == 4658
    assert (first.tier_s_count, first.tier_a_count, first.tier_b_count) == (50, 313, 4295)
    assert first.pilot_count == 24
    assert verified.facet_count == 4658
    assert (verified.tier_s_count, verified.tier_a_count, verified.tier_b_count) == (50, 313, 4295)
    assert verified.pilot_count == 24
    assert verified.poster_row_count == 4658
    assert verified.primary_poster_row_count == 4658
    assert verified.approved_poster_object_count == 4545
    assert verified.unavailable_poster_row_count == 113
    assert len(posters) == 4658
    assert len(facets) == 4658
    assert {record["movie_id"] for record in facets} == {
        record["movie_id"] for record in records if record.get("record_kind") == "movie"
    }
    assert all(record["tier"] in {"S", "A", "B"} for record in facets)
    assert all(isinstance(record["pilot_movie"], bool) for record in facets)
    assert all(_valid_facet_content_hash(record) for record in facets)
    assert all(record.get("is_primary") is True for record in posters)
    assert all(record.get("source_is_primary") is False for record in posters)
    assert all(
        record.get("rag_primary_policy") == "one_state_row_per_movie" for record in posters
    )
    assert quality == Counter(
        {"machine_passed": 4545, "content_conflict": 73, "missing": 31, "placeholder": 9}
    )
    assert len(approved) == 4545
    assert len(posters) - len(approved) == 113
    assert all(
        record["derived_object_uri"]
        == f"assets/posters/derived/{record['movie_id']}.webp"
        for record in approved
    )
    for record in approved:
        derived_path = source_root.joinpath(
            *PurePosixPath(str(record["derived_object_uri"])).parts
        )
        assert derived_path.is_file()
        assert record["derived_content_sha256"] == _sha256(derived_path)
        assert record["derived_content_sha256"] != record["content_sha256"]
        assert record["derived_byte_length"] == derived_path.stat().st_size
        assert record["derived_mime_type"] == "image/webp"
        with derived_path.open("rb") as stream:
            header = stream.read(12)
        assert header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    unavailable = [
        record
        for record in posters
        if record.get("quality_status") in {"content_conflict", "missing", "placeholder"}
    ]
    assert all(record.get("derived_object_uri") == "" for record in unavailable)
    assert all("derived_content_sha256" not in record for record in unavailable)
    assert all("derived_byte_length" not in record for record in unavailable)
    assert all("derived_mime_type" not in record for record in unavailable)
    by_id = {str(record["movie_id"]): record for record in posters}
    assert by_id["1978_ZQ_001"]["derived_object_uri"] == (
        "assets/posters/derived/1978_ZQ_001.webp"
    )
    assert by_id["1982_ZJPD_001"]["derived_object_uri"] == (
        "assets/posters/derived/1982_ZJPD_001.webp"
    )
    assert by_id["1978_ZQ_001"]["derived_content_sha256"] == (
        "a968160d2bafc410199e326a6dba6e357606ca0de8c38e6d0d9afd9caf0f8b3d"
    )
    assert by_id["1978_ZQ_001"]["derived_byte_length"] == 38140
    assert by_id["1982_ZJPD_001"]["derived_content_sha256"] == (
        "596ddd1d82855e6b7f3ca4c5e3c58af3655cb78d263cb21679464730dbba5b2b"
    )
    assert by_id["1982_ZJPD_001"]["derived_byte_length"] == 41166
    assert (
        _sha256(parent_manifest),
        _sha256(parent_posters),
        _sha256(parent_tiers),
        _sha256(parent_pilots),
    ) == before

    forged_records = [
        json.loads(line)
        for line in second.bundle_path.read_text(encoding="utf-8").splitlines()
    ]
    forged_poster = next(
        record
        for record in forged_records
        if record.get("record_kind") == "poster"
        and record.get("quality_status") in {"machine_passed", "manual_approved"}
    )
    forged_poster["derived_content_sha256"] = "0" * 64
    forged_bundle = b"".join(_canonical_json(record) + b"\n" for record in forged_records)
    second.bundle_path.write_bytes(forged_bundle)
    forged_manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    forged_manifest["bundle"]["sha256"] = hashlib.sha256(forged_bundle).hexdigest()
    forged_manifest["bundle"]["size_bytes"] = len(forged_bundle)
    forged_manifest["poster_derivation"]["derived_inventory_sha256"] = (
        _derived_inventory_sha256(forged_records)
    )
    second.manifest_path.write_bytes(_canonical_json(forged_manifest) + b"\n")
    with pytest.raises(RagBundleError, match="v1.2 derived poster inventory mismatch"):
        verify_rag_bundle(second.manifest_path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _valid_facet_content_hash(record: dict[str, object]) -> bool:
    body = {
        key: value
        for key, value in record.items()
        if key not in {"record_kind", "content_sha256"}
    }
    return record["content_sha256"] == hashlib.sha256(_canonical_json(body)).hexdigest()


def _derived_inventory_sha256(records: list[dict[str, object]]) -> str:
    fields = (
        "movie_id",
        "derived_object_uri",
        "derived_content_sha256",
        "derived_byte_length",
        "derived_mime_type",
    )
    inventory = [
        {key: record[key] for key in fields}
        for record in records
        if record.get("record_kind") == "poster"
        and record.get("quality_status") in {"machine_passed", "manual_approved"}
    ]
    return hashlib.sha256(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
