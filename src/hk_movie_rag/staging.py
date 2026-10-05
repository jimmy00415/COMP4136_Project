"""Fail-closed validation for untrusted incremental staging submissions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator

_MOVIE_ID_PATTERN = r"^[0-9]{4}_[A-Z0-9]+_[0-9]{3}$"
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_LANGUAGE_PATTERN = r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SAFE_HTTP_URL = re.compile(
    r"^https?://"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*"
    r"(?:/(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})*)*"
    r"(?:\?(?:[A-Za-z0-9._~!$&'()*+,;=:@/?-]|%[0-9A-Fa-f]{2})*)?"
    r"(?:#(?:[A-Za-z0-9._~!$&'()*+,;=:@/?-]|%[0-9A-Fa-f]{2})*)?(?![\s\S])"
)
_PUBLISHABLE_QUALITY = frozenset({"machine_passed", "manual_approved"})
_POSTER_EXTENSIONS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}
_POSTER_REQUIRED = (
    "schema_version",
    "movie_id",
    "submission_id",
    "source_url",
    "retrieved_at",
    "rights_status",
    "expected_sha256",
)
_ANALYSIS_REQUIRED = (
    "schema_version",
    "movie_id",
    "document_id",
    "document_type",
    "language",
    "title",
    "source_urls",
    "rights_status",
    "authorship_method",
    "content_version",
    "content_sha256",
)


class StagingError(ValueError):
    """Raised when an untrusted staging submission violates its contract."""


class _PosterMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"]
    movie_id: StrictStr = Field(pattern=_MOVIE_ID_PATTERN)
    submission_id: StrictStr = Field(pattern=_SAFE_ID_PATTERN)
    source_url: StrictStr
    retrieved_at: StrictStr
    rights_status: Literal["unknown", "cleared", "restricted", "denied"]
    expected_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    expected_mime_type: Literal["image/jpeg", "image/png", "image/webp"] | None = None
    quality_status: Literal[
        "machine_passed",
        "content_conflict",
        "placeholder",
        "missing",
        "manual_approved",
        "rejected",
        "rights_blocked",
    ] = "machine_passed"

    @field_validator("source_url")
    @classmethod
    def _source_url_is_http(cls, value: str) -> str:
        _require_http_url(value)
        return value

    @field_validator("retrieved_at")
    @classmethod
    def _retrieved_at_has_timezone(cls, value: str) -> str:
        _require_timestamp(value)
        return value


class _AnalysisMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"]
    movie_id: StrictStr = Field(pattern=_MOVIE_ID_PATTERN)
    document_id: StrictStr = Field(pattern=_SAFE_ID_PATTERN)
    document_type: Literal["analysis", "review", "research_note", "transcript"]
    language: StrictStr = Field(pattern=_LANGUAGE_PATTERN)
    title: StrictStr = Field(min_length=1, max_length=500)
    source_urls: list[StrictStr] = Field(min_length=1)
    rights_status: Literal["unknown", "cleared", "restricted", "denied"]
    authorship_method: Literal["human", "ai_assisted", "ai_generated"]
    content_version: StrictStr = Field(pattern=_SAFE_ID_PATTERN)
    content_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    quality_status: Literal[
        "machine_passed", "manual_approved", "rejected", "rights_blocked"
    ] = "machine_passed"

    @field_validator("title")
    @classmethod
    def _title_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @field_validator("source_urls")
    @classmethod
    def _source_urls_are_unique_http_urls(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("source_urls must not contain duplicates")
        for value in values:
            _require_http_url(value)
        return values


@dataclass(frozen=True)
class PosterSubmission:
    path: Path
    metadata_path: Path
    poster_path: Path
    schema_version: str
    movie_id: str
    submission_id: str
    source_url: str
    retrieved_at: str
    rights_status: str
    expected_sha256: str
    content_sha256: str
    detected_mime_type: str
    detected_extension: str
    width: int
    height: int
    color_mode: str
    quality_status: str
    publishable: bool


@dataclass(frozen=True)
class MovieDocumentSubmission:
    path: Path
    schema_version: str
    movie_id: str
    document_id: str
    document_type: str
    language: str
    title: str
    source_urls: tuple[str, ...]
    rights_status: str
    authorship_method: str
    content_version: str
    content_sha256: str
    quality_status: str
    identity: str
    publishable: bool


def document_identity(movie_id: str, document_id: str, content: bytes) -> str:
    """Return a stable content-addressed identity for one logical document."""
    digest = hashlib.sha256()
    digest.update(movie_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(document_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(content)
    return digest.hexdigest()


def validate_poster_submission(path: Path, release_ids: set[str]) -> PosterSubmission:
    """Validate one poster directory without copying or mutating any source bytes."""
    submission_path, metadata_path, poster_path = _poster_paths(path)
    metadata_bytes = _read_direct_file(metadata_path, "poster submission entries")
    raw_metadata = _load_unique_json(metadata_bytes)
    metadata = _validate_model(_PosterMetadata, raw_metadata, _POSTER_REQUIRED, "sidecar")
    assert isinstance(metadata, _PosterMetadata)
    _require_poster_path_identity(submission_path, metadata)
    _require_release_movie(metadata.movie_id, release_ids)

    poster_bytes = _read_direct_file(poster_path, "poster submission entries")
    content_sha256 = hashlib.sha256(poster_bytes).hexdigest()
    if content_sha256 != metadata.expected_sha256:
        raise StagingError("poster SHA-256 mismatch")
    detected_format, detected_mime_type, detected_extension, width, height, mode = (
        _inspect_image_bytes(poster_bytes)
    )
    if (
        metadata.expected_mime_type is not None
        and metadata.expected_mime_type != detected_mime_type
    ):
        raise StagingError("poster detected MIME does not match expected_mime_type")
    if poster_path.suffix.lower() != detected_extension:
        raise StagingError(
            f"poster extension does not match detected {detected_format} content"
        )

    return PosterSubmission(
        path=submission_path,
        metadata_path=metadata_path,
        poster_path=poster_path,
        schema_version=metadata.schema_version,
        movie_id=metadata.movie_id,
        submission_id=metadata.submission_id,
        source_url=metadata.source_url,
        retrieved_at=metadata.retrieved_at,
        rights_status=metadata.rights_status,
        expected_sha256=metadata.expected_sha256,
        content_sha256=content_sha256,
        detected_mime_type=detected_mime_type,
        detected_extension=detected_extension,
        width=width,
        height=height,
        color_mode=mode,
        quality_status=metadata.quality_status,
        publishable=_is_publishable(metadata.quality_status, metadata.rights_status),
    )


def validate_analysis_submission(
    path: Path, release_ids: set[str]
) -> MovieDocumentSubmission:
    """Validate Markdown front matter and bind its digest to the exact body bytes."""
    resolved_path = _direct_file(path, "analysis submission")
    payload = _read_direct_file(resolved_path, "analysis submission")
    front_matter, content = _split_front_matter(payload)
    raw_metadata = _load_unique_yaml(front_matter)
    metadata = _validate_model(
        _AnalysisMetadata, raw_metadata, _ANALYSIS_REQUIRED, "front matter"
    )
    assert isinstance(metadata, _AnalysisMetadata)
    _require_analysis_path_identity(resolved_path, metadata)
    _require_release_movie(metadata.movie_id, release_ids)
    if not content:
        raise StagingError("analysis content must not be empty")
    content_sha256 = hashlib.sha256(content).hexdigest()
    if content_sha256 != metadata.content_sha256:
        raise StagingError("content SHA-256 mismatch")

    return MovieDocumentSubmission(
        path=resolved_path,
        schema_version=metadata.schema_version,
        movie_id=metadata.movie_id,
        document_id=metadata.document_id,
        document_type=metadata.document_type,
        language=metadata.language,
        title=metadata.title,
        source_urls=tuple(metadata.source_urls),
        rights_status=metadata.rights_status,
        authorship_method=metadata.authorship_method,
        content_version=metadata.content_version,
        content_sha256=content_sha256,
        quality_status=metadata.quality_status,
        identity=document_identity(metadata.movie_id, metadata.document_id, content),
        publishable=_is_publishable(metadata.quality_status, metadata.rights_status),
    )


def validate_analysis_collection(
    paths: Iterable[Path], release_ids: set[str]
) -> tuple[MovieDocumentSubmission, ...]:
    """Validate and de-duplicate one deterministic future-promotion registry."""
    revisions: dict[tuple[str, str, str], MovieDocumentSubmission] = {}
    for path in sorted(paths, key=lambda item: item.as_posix()):
        submission = validate_analysis_submission(path, release_ids)
        key = (
            submission.movie_id,
            submission.document_id,
            submission.content_version,
        )
        previous = revisions.get(key)
        if previous is None:
            revisions[key] = submission
        elif _analysis_revision_payload(previous) != _analysis_revision_payload(
            submission
        ):
            raise StagingError(
                "analysis revision conflict for "
                f"{submission.movie_id}/{submission.document_id}/{submission.content_version}"
            )
    return tuple(revisions[key] for key in sorted(revisions))


def _poster_paths(path: Path) -> tuple[Path, Path, Path]:
    _require_direct_ancestry(path, "poster submission path")
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise StagingError("poster submission path is unreadable") from exc
    if _is_reparse(path_stat):
        raise StagingError("poster submission entries must be direct regular files")
    if stat.S_ISDIR(path_stat.st_mode):
        submission_path = path.resolve(strict=True)
    elif stat.S_ISREG(path_stat.st_mode):
        submission_path = path.parent.resolve(strict=True)
    else:
        raise StagingError("poster submission path must be a directory or regular file")
    try:
        entries = sorted(submission_path.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise StagingError("poster submission path is unreadable") from exc
    if {entry.name for entry in entries} == {"metadata.json", "poster.jpeg"}:
        raise StagingError("poster filename must use its canonical detected extension")
    metadata_path = submission_path / "metadata.json"
    candidates = [entry for entry in entries if entry.name != "metadata.json"]
    if len(candidates) != 1 or candidates[0].stem != "poster":
        raise StagingError(
            "poster submission must contain exactly metadata.json and one poster file"
        )
    poster_path = candidates[0]
    _direct_file(metadata_path, "poster submission entries")
    _direct_file(poster_path, "poster submission entries")
    return submission_path, metadata_path, poster_path


def _direct_file(path: Path, label: str) -> Path:
    _require_direct_ancestry(path, label)
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise StagingError(f"{label} are unreadable") from exc
    if not stat.S_ISREG(path_stat.st_mode) or _is_reparse(path_stat):
        raise StagingError(f"{label} must be direct regular files")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise StagingError(f"{label} are unreadable") from exc


def _read_direct_file(path: Path, label: str) -> bytes:
    resolved = _direct_file(path, label)
    before = resolved.stat()
    try:
        content = resolved.read_bytes()
    except OSError as exc:
        raise StagingError(f"{label} are unreadable") from exc
    after = resolved.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise StagingError(f"{label} changed during validation")
    return content


def _load_unique_json(payload: bytes) -> dict[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise StagingError(f"duplicate metadata key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StagingError("poster metadata must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise StagingError("poster metadata must be a JSON object")
    return value


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise StagingError("front-matter keys must be scalar") from exc
        if duplicate:
            raise StagingError(f"duplicate front-matter key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _load_unique_yaml(payload: bytes) -> dict[str, object]:
    try:
        value = yaml.load(payload.decode("utf-8"), Loader=_UniqueKeyLoader)
    except StagingError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise StagingError("analysis front matter must be UTF-8 YAML") from exc
    if not isinstance(value, dict):
        raise StagingError("analysis front matter must be a mapping")
    return value


def _split_front_matter(payload: bytes) -> tuple[bytes, bytes]:
    lines = payload.splitlines(keepends=True)
    if not lines or lines[0].rstrip(b"\r\n") != b"---":
        raise StagingError("analysis must begin with YAML front matter")
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip(b"\r\n") == b"---":
            return b"".join(lines[1:index]), b"".join(lines[index + 1 :])
    raise StagingError("analysis front matter is not terminated")


def _validate_model(
    model: type[BaseModel],
    data: dict[str, object],
    required: tuple[str, ...],
    label: str,
) -> BaseModel:
    missing = [field for field in required if field not in data]
    if missing:
        raise StagingError(f"missing required {label} fields: {', '.join(missing)}")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        extras = sorted(
            str(error["loc"][0])
            for error in exc.errors()
            if error["type"] == "extra_forbidden"
        )
        if extras:
            raise StagingError(
                f"additional properties are not allowed: {', '.join(extras)}"
            ) from exc
        error = exc.errors()[0]
        field = ".".join(str(part) for part in error["loc"])
        raise StagingError(f"invalid {label} field {field}: {error['msg']}") from exc


def _require_release_movie(movie_id: str, release_ids: set[str]) -> None:
    if movie_id not in release_ids:
        raise StagingError(f"movie_id {movie_id!r} is not in formal Release")


def _require_poster_path_identity(
    submission_path: Path, metadata: _PosterMetadata
) -> None:
    if submission_path.parent.name != metadata.movie_id:
        raise StagingError("poster path movie_id mismatch")
    if submission_path.name != metadata.submission_id:
        raise StagingError("poster path submission_id mismatch")


def _require_analysis_path_identity(
    path: Path, metadata: _AnalysisMetadata
) -> None:
    if path.parent.name != metadata.movie_id:
        raise StagingError("analysis path movie_id mismatch")
    if path.name != f"{metadata.document_id}.md":
        raise StagingError("analysis path document_id mismatch")


def _analysis_revision_payload(
    submission: MovieDocumentSubmission,
) -> tuple[object, ...]:
    return (
        submission.schema_version,
        submission.movie_id,
        submission.document_id,
        submission.document_type,
        submission.language,
        submission.title,
        submission.source_urls,
        submission.rights_status,
        submission.authorship_method,
        submission.content_version,
        submission.content_sha256,
        submission.quality_status,
        submission.identity,
        submission.publishable,
    )


def _require_direct_ancestry(path: Path, label: str) -> None:
    for candidate in (path, *path.parents):
        try:
            candidate_stat = candidate.lstat()
        except OSError as exc:
            raise StagingError(f"{label} is unreadable") from exc
        if stat.S_ISLNK(candidate_stat.st_mode) or _is_reparse(candidate_stat):
            raise StagingError(f"{label} contains a followed link")


def _require_http_url(value: str) -> None:
    if _SAFE_HTTP_URL.fullmatch(value) is None:
        raise ValueError(
            "must be a safe HTTP(S) URL without credentials, ports, or IPv6 literals"
        )


def _require_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be an ISO 8601 date-time") from exc
    if parsed.tzinfo is None:
        raise ValueError("must include a timezone offset")


def _inspect_image_bytes(content: bytes) -> tuple[str, str, str, int, int, str]:
    from io import BytesIO

    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        with Image.open(BytesIO(content)) as image:
            image.load()
            detected_format = image.format
            width, height = image.size
            mode = image.mode
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise StagingError("poster payload is not a valid supported image") from exc
    if detected_format not in _POSTER_EXTENSIONS:
        raise StagingError(f"unsupported poster format: {detected_format}")
    mime_type, extension = _POSTER_EXTENSIONS[detected_format]
    return detected_format, mime_type, extension, width, height, mode


def _is_publishable(quality_status: str, rights_status: str) -> bool:
    return quality_status in _PUBLISHABLE_QUALITY and rights_status == "cleared"


def _is_reparse(path_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(path_stat.st_file_attributes & reparse_flag) if reparse_flag else False
