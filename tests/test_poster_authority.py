"""Immutable restricted-demo poster-serving authority tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from dataclasses import asdict, replace
from importlib.resources import files
from pathlib import Path

import pytest

from hk_movie_rag import poster_authority
from hk_movie_rag.poster_authority import (
    POSTER_SERVING_AUTHORITY_SHA256,
    POSTER_SERVING_AUTHORITY_V2_SHA256,
    POSTER_SERVING_AUTHORITY_V3_SHA256,
    PosterAuthorityError,
    load_poster_serving_authority,
)
from hk_movie_rag.rag_bundle import RagReleaseContract, RagReleaseCounts

EXPECTED_AUTHORITY = {
    "access_mode": "restricted_demo",
    "approved_poster_objects": 4545,
    "decision": "authorized",
    "delivery_scope": "authenticated_same_origin_private_proxy",
    "derived_inventory_sha256": (
        "b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154"
    ),
    "operator_permission_confirmed": True,
    "parent_release_manifest_sha256": (
        "cbd529e126e8b72dccac302d9746fcfa63318caecbcda24548c05e282e13f974"
    ),
    "primary_poster_rows": 4658,
    "rag_release_id": "v1.2-demo",
    "schema_version": "poster-serving-authority/v1",
    "source_rights_mutation": "forbidden",
    "source_rights_status": "unknown",
    "unavailable_poster_rows": 113,
}
EXPECTED_CANONICAL = (
    b'{"access_mode":"restricted_demo","approved_poster_objects":4545,'
    b'"decision":"authorized","delivery_scope":"authenticated_same_origin_private_proxy",'
    b'"derived_inventory_sha256":"b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154",'
    b'"operator_permission_confirmed":true,'
    b'"parent_release_manifest_sha256":"cbd529e126e8b72dccac302d9746fcfa63318caecbcda24548c05e282e13f974",'
    b'"primary_poster_rows":4658,"rag_release_id":"v1.2-demo",'
    b'"schema_version":"poster-serving-authority/v1",'
    b'"source_rights_mutation":"forbidden","source_rights_status":"unknown",'
    b'"unavailable_poster_rows":113}'
)
R2_RELEASE_ID = "v1.2-demo-r2"
EXPECTED_V2_AUTHORITY = {
    "access_mode": "restricted_demo",
    "approved_poster_objects": 4545,
    "dataset_id": "v1.2",
    "decision": "authorized",
    "delivery_scope": "authenticated_same_origin_private_proxy",
    "derived_inventory_sha256": (
        "b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154"
    ),
    "operator_permission_confirmed": True,
    "parent_release_manifest_sha256": (
        "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
    ),
    "primary_poster_rows": 4658,
    "schema_version": "poster-serving-authority/v2",
    "source_rights_mutation": "forbidden",
    "source_rights_status": "unknown",
    "unavailable_poster_rows": 113,
}
R3_RELEASE_ID = "v1.2-demo-r3"
EXPECTED_V3_AUTHORITY = {
    "access_mode": "restricted_demo",
    "approved_poster_objects": 4546,
    "dataset_id": "v1.2+rag-r3-supplemental-1",
    "decision": "authorized",
    "delivery_scope": "authenticated_same_origin_private_proxy",
    "derived_inventory_sha256": (
        "3e3cedee507c3017821de1081bc70bbb01d0c73f8683ab484a464d1fc1f7c1db"
    ),
    "operator_permission_confirmed": True,
    "parent_release_manifest_sha256": (
        "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
    ),
    "primary_poster_rows": 4659,
    "rag_release_id": R3_RELEASE_ID,
    "schema_version": "poster-serving-authority/v3",
    "source_rights_mutation": "forbidden",
    "source_rights_status": "unknown_or_restricted",
    "unavailable_poster_rows": 113,
}


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _install_bytes(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    monkeypatch.setattr(poster_authority, "_read_authority_bytes", lambda: raw)


def test_packaged_authority_has_exact_payload_canonical_bytes_and_fixed_digest() -> None:
    """Breaks if the wheel authority drifts from the approved operator decision."""
    resource = files("hk_movie_rag.authorities").joinpath("poster_serving_authority_v1.json")
    raw = resource.read_bytes()

    assert raw == EXPECTED_CANONICAL + b"\n"
    assert POSTER_SERVING_AUTHORITY_SHA256 == (
        "ca99a5c04d8e61029f58e90cb3017730dc2b3511361834d7a0cf4860b54005e4"
    )
    authority = load_poster_serving_authority()
    assert asdict(authority) == {
        **EXPECTED_AUTHORITY,
        "dataset_id": None,
        "artifact_sha256": POSTER_SERVING_AUTHORITY_SHA256,
    }


def _r2_contract() -> RagReleaseContract:
    return RagReleaseContract(
        schema_version="1.2",
        rag_release_id=R2_RELEASE_ID,
        parent_release_manifest_sha256=EXPECTED_V2_AUTHORITY[
            "parent_release_manifest_sha256"
        ],
        manifest_sha256="a" * 64,
        bundle_sha256="b" * 64,
        derived_inventory_sha256=EXPECTED_V2_AUTHORITY["derived_inventory_sha256"],
        counts=RagReleaseCounts(
            movies=4658,
            facet_count=4658,
            tier_s_count=50,
            tier_a_count=313,
            tier_b_count=4295,
            pilot_count=24,
            poster_rows=4658,
            primary_poster_rows=4658,
            approved_poster_objects=4545,
            unavailable_poster_rows=113,
            derived_poster_bytes=1,
            metadata_passages=4658,
            documents=3,
            pdf_passages=12,
        ),
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
        generation_model="gemini-3.5-flash-lite",
        text_extraction_profile="cjk-layout-v1",
        document_embedding_profile="vertex-title-text-v1",
        relevance_policy_sha256="c" * 64,
        poster_authority_sha256=POSTER_SERVING_AUTHORITY_V2_SHA256,
        access_mode="restricted_demo",
    )


def test_v2_authority_is_dataset_scoped_and_accepts_only_the_exact_child_contract() -> None:
    """Breaks if poster authority becomes child-document scoped or skips dataset identity."""
    resource = files("hk_movie_rag.authorities").joinpath(
        "poster_serving_authority_v2.json"
    )
    raw = resource.read_bytes()
    parsed = json.loads(raw)

    assert POSTER_SERVING_AUTHORITY_V2_SHA256 == (
        "9e56adda2a1fdfa648869c53d7e60a085db5d1c6d11db90dbec13bfe12f0eeed"
    )
    assert hashlib.sha256(raw).hexdigest() == POSTER_SERVING_AUTHORITY_V2_SHA256

    authority = load_poster_serving_authority(
        R2_RELEASE_ID,
        POSTER_SERVING_AUTHORITY_V2_SHA256,
        contract=_r2_contract(),
    )

    assert raw == _canonical_bytes(EXPECTED_V2_AUTHORITY) + b"\n"
    assert parsed == EXPECTED_V2_AUTHORITY
    assert "rag_release_id" not in parsed
    assert not {
        "documents",
        "pdf_passages",
        "embeddings",
        "rag_manifest_sha256",
    }.intersection(parsed)
    assert authority.dataset_id == "v1.2"
    assert authority.rag_release_id is None
    assert authority.artifact_sha256 == POSTER_SERVING_AUTHORITY_V2_SHA256


def test_v1_keeps_historical_canonical_digest_while_v2_binds_raw_artifact_bytes() -> None:
    v1 = files("hk_movie_rag.authorities").joinpath(
        "poster_serving_authority_v1.json"
    ).read_bytes()
    v2 = files("hk_movie_rag.authorities").joinpath(
        "poster_serving_authority_v2.json"
    ).read_bytes()

    assert hashlib.sha256(v1.removesuffix(b"\n")).hexdigest() == (
        POSTER_SERVING_AUTHORITY_SHA256
    )
    assert hashlib.sha256(v1).hexdigest() != POSTER_SERVING_AUTHORITY_SHA256
    assert hashlib.sha256(v2).hexdigest() == POSTER_SERVING_AUTHORITY_V2_SHA256


def test_v2_authority_selection_has_no_release_digest_or_contract_fallback() -> None:
    """Breaks if a valid authority can authorize a different child or mismatched dataset."""
    contract = _r2_contract()
    mismatches = (
        replace(contract, parent_release_manifest_sha256="0" * 64),
        replace(contract, derived_inventory_sha256="0" * 64),
        replace(contract, access_mode="public"),
        replace(contract, counts=replace(contract.counts, primary_poster_rows=4657)),
        replace(contract, counts=replace(contract.counts, approved_poster_objects=4544)),
        replace(contract, counts=replace(contract.counts, unavailable_poster_rows=114)),
        replace(contract, poster_authority_sha256="0" * 64),
    )
    for mismatched in mismatches:
        with pytest.raises(PosterAuthorityError):
            load_poster_serving_authority(
                R2_RELEASE_ID,
                POSTER_SERVING_AUTHORITY_V2_SHA256,
                contract=mismatched,
            )
    with pytest.raises(PosterAuthorityError):
        load_poster_serving_authority(R2_RELEASE_ID, POSTER_SERVING_AUTHORITY_V2_SHA256)
    with pytest.raises(PosterAuthorityError):
        load_poster_serving_authority(R2_RELEASE_ID, "0" * 64, contract=contract)
    with pytest.raises(PosterAuthorityError):
        load_poster_serving_authority(
            "v1.2-demo",
            POSTER_SERVING_AUTHORITY_V2_SHA256,
            contract=contract,
        )


def test_v3_authority_accepts_only_the_4659_movie_supplemental_contract() -> None:
    """Breaks if R3 can serve a missing poster or borrow the R2 dataset authority."""
    resource = files("hk_movie_rag.authorities").joinpath(
        "poster_serving_authority_v3.json"
    )
    raw = resource.read_bytes()
    contract = replace(
        _r2_contract(),
        schema_version="1.3",
        rag_release_id=R3_RELEASE_ID,
        derived_inventory_sha256=EXPECTED_V3_AUTHORITY["derived_inventory_sha256"],
        counts=replace(
            _r2_contract().counts,
            movies=4659,
            facet_count=4659,
            tier_s_count=51,
            poster_rows=4659,
            primary_poster_rows=4659,
            approved_poster_objects=4546,
            metadata_passages=4659,
            documents=5,
            pdf_passages=21,
        ),
        poster_authority_sha256=POSTER_SERVING_AUTHORITY_V3_SHA256,
    )

    authority = load_poster_serving_authority(
        R3_RELEASE_ID,
        POSTER_SERVING_AUTHORITY_V3_SHA256,
        contract=contract,
    )

    assert hashlib.sha256(raw).hexdigest() == POSTER_SERVING_AUTHORITY_V3_SHA256
    assert raw == _canonical_bytes(EXPECTED_V3_AUTHORITY) + b"\n"
    assert asdict(authority) == {
        **EXPECTED_V3_AUTHORITY,
        "artifact_sha256": POSTER_SERVING_AUTHORITY_V3_SHA256,
    }
    with pytest.raises(PosterAuthorityError):
        load_poster_serving_authority(
            R3_RELEASE_ID,
            POSTER_SERVING_AUTHORITY_V2_SHA256,
            contract=contract,
        )


@pytest.mark.parametrize(
    ("field_name", "tampered"),
    [
        ("access_mode", "public"),
        ("approved_poster_objects", 4544),
        ("decision", "pending"),
        ("delivery_scope", "public_gcs"),
        ("derived_inventory_sha256", "0" * 64),
        ("operator_permission_confirmed", False),
        (
            "parent_release_manifest_sha256",
            "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c",
        ),
        ("primary_poster_rows", 4657),
        ("rag_release_id", "v1.3-demo"),
        ("schema_version", "poster-serving-authority/v2"),
        ("source_rights_mutation", "allowed"),
        ("source_rights_status", "cleared"),
        ("unavailable_poster_rows", 114),
    ],
)
def test_loader_rejects_every_single_field_tamper_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    tampered: object,
) -> None:
    """Breaks if artifact and runtime scope can drift together and self-approve."""
    payload = {**EXPECTED_AUTHORITY, field_name: tampered}
    _install_bytes(monkeypatch, _canonical_bytes(payload) + b"\n")

    with pytest.raises(PosterAuthorityError) as raised:
        load_poster_serving_authority()

    assert str(raised.value) == "poster serving authority is invalid"
    assert str(tampered) not in str(raised.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json\n",
        b'{"access_mode":"restricted_demo","access_mode":"public"}\n',
        _canonical_bytes({**EXPECTED_AUTHORITY, "extra": "forbidden"}) + b"\n",
        _canonical_bytes({**EXPECTED_AUTHORITY, "primary_poster_rows": True}) + b"\n",
        EXPECTED_CANONICAL,
        EXPECTED_CANONICAL + b"\r\n",
        b" " + EXPECTED_CANONICAL + b"\n",
    ],
)
def test_loader_rejects_malformed_duplicate_unknown_boolean_and_noncanonical_bytes(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
) -> None:
    """Breaks if alternate JSON encodings can impersonate the reviewed artifact."""
    _install_bytes(monkeypatch, raw)

    with pytest.raises(PosterAuthorityError, match="^poster serving authority is invalid$"):
        load_poster_serving_authority()


def test_fresh_wheel_contains_the_exact_authority_resource(tmp_path: Path) -> None:
    """Breaks if source checkout passes while the installed wheel omits or changes authority."""
    root = Path(__file__).resolve().parents[1]
    repository_resource = (
        root
        / "src"
        / "hk_movie_rag"
        / "authorities"
        / "poster_serving_authority_v1.json"
    ).read_bytes()
    repository_v2_resource = (
        root
        / "src"
        / "hk_movie_rag"
        / "authorities"
        / "poster_serving_authority_v2.json"
    ).read_bytes()
    repository_v3_resource = (
        root
        / "src"
        / "hk_movie_rag"
        / "authorities"
        / "poster_serving_authority_v3.json"
    ).read_bytes()
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    install_root = tmp_path / "installed"
    with zipfile.ZipFile(wheels[0]) as archive:
        archive.extractall(install_root)
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import hashlib,sys;"
                "sys.path.insert(0,sys.argv[1]);"
                "from importlib.resources import files;"
                "raw=files('hk_movie_rag.authorities').joinpath("
                "'poster_serving_authority_v1.json').read_bytes();"
                "print(raw.hex());"
                "print(hashlib.sha256(raw[:-1]).hexdigest());"
                "raw_v2=files('hk_movie_rag.authorities').joinpath("
                "'poster_serving_authority_v2.json').read_bytes();"
                "print(raw_v2.hex());"
                "print(hashlib.sha256(raw_v2).hexdigest());"
                "raw_v3=files('hk_movie_rag.authorities').joinpath("
                "'poster_serving_authority_v3.json').read_bytes();"
                "print(raw_v3.hex());"
                "print(hashlib.sha256(raw_v3).hexdigest())"
            ),
            str(install_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    (
        installed_hex,
        installed_digest,
        installed_v2_hex,
        installed_v2_digest,
        installed_v3_hex,
        installed_v3_digest,
    ) = probe.stdout.splitlines()
    assert bytes.fromhex(installed_hex) == repository_resource == EXPECTED_CANONICAL + b"\n"
    assert installed_digest == POSTER_SERVING_AUTHORITY_SHA256
    assert (
        bytes.fromhex(installed_v2_hex)
        == repository_v2_resource
        == _canonical_bytes(EXPECTED_V2_AUTHORITY) + b"\n"
    )
    assert installed_v2_digest == POSTER_SERVING_AUTHORITY_V2_SHA256
    assert (
        bytes.fromhex(installed_v3_hex)
        == repository_v3_resource
        == _canonical_bytes(EXPECTED_V3_AUTHORITY) + b"\n"
    )
    assert installed_v3_digest == POSTER_SERVING_AUTHORITY_V3_SHA256
