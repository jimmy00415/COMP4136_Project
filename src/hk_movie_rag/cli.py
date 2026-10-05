"""Command-line entry point for the Hong Kong Movie RAG data foundation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

from .cleanup import (
    CleanupError,
    build_cleanup_plan,
    execute_cleanup,
    load_cleanup_plan,
    plan_to_json,
    write_cleanup_plan,
)
from .cleanup import (
    report_to_json as cleanup_report_to_json,
)
from .config import ConfigError, Settings
from .gcp_preflight import (
    GcloudJsonRunner,
    report_to_json,
    run_preflight,
    write_preflight_report,
)
from .gcs_archive import (
    ArchiveError,
    assert_archive_authority_current,
    assert_authority_state_current,
    create_storage_client,
    establish_archive_authority,
    upload_archive,
    verify_archive,
    write_verification_report,
)
from .gcs_archive import (
    report_to_json as archive_report_to_json,
)
from .inventory import build_inventory, write_inventory
from .poster_authority import load_poster_serving_authority
from .poster_fleet import (
    POSTER_FLEET_RELEASE_ID,
    GcsPosterFleetStorage,
    PosterFleetFailure,
    PosterFleetReport,
    verify_poster_fleet,
)
from .posters import build_posters, load_release_ids
from .rag_bundle import (
    RagBundleError,
    build_rag_bundle,
    prepare_pdf_binding,
    verify_rag_bundle,
)
from .rag_db import DatabaseSettings, RagDatabaseError, RagRepository, connect_db
from .rag_ingest import IngestionError, ingest_bundle, verified_bundle_source
from .release_data import build_release_data
from .release_lock import ReleaseLockError
from .release_manifest import ManifestError, build_release_manifest, verify_release_manifest
from .staging import StagingError, validate_analysis_submission, validate_poster_submission
from .vertex_clients import VertexEmbeddingClient, VertexEmbeddingError


class _RedactedArgumentParser(argparse.ArgumentParser):
    """Keep poster-verifier command errors to one controlled JSON object."""

    def error(self, message: str) -> NoReturn:
        if len(sys.argv) > 1 and sys.argv[1] == "verify-poster-fleet":
            del message
            _emit_poster_fleet_cli_failure("cli_arguments_invalid")
            raise SystemExit(2)
        super().error(message)


def _emit_poster_fleet_cli_failure(reason: str) -> None:
    report = PosterFleetReport(
        POSTER_FLEET_RELEASE_ID,
        0,
        0,
        0,
        0,
        (PosterFleetFailure("[fleet]", reason),),
        False,
    )
    print(json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")))


def main() -> None:
    parser = _RedactedArgumentParser(prog="hk-movie-rag")
    subparsers = parser.add_subparsers(dest="command")
    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--output", required=True, type=Path)
    subparsers.add_parser("build-release-data")
    subparsers.add_parser("build-posters")
    subparsers.add_parser("build-manifest")
    verify_parser = subparsers.add_parser("verify-release")
    verify_parser.add_argument("path", type=Path)
    rag_build_parser = subparsers.add_parser("build-rag-bundle")
    rag_build_parser.add_argument("--source-root", required=True, type=Path)
    rag_build_parser.add_argument("--document-source-root", type=Path)
    rag_build_parser.add_argument("--config", required=True, type=Path)
    rag_build_parser.add_argument("--output", required=True, type=Path)
    pdf_preflight_parser = subparsers.add_parser("preflight-pdf-binding")
    pdf_preflight_parser.add_argument("--source-root", required=True, type=Path)
    pdf_preflight_parser.add_argument(
        "--document-source-root", required=True, type=Path
    )
    pdf_preflight_parser.add_argument("--config", required=True, type=Path)
    pdf_preflight_parser.add_argument("--movie-id", required=True)
    pdf_preflight_parser.add_argument("--document-id", required=True)
    pdf_preflight_parser.add_argument("--source-filename", required=True)
    pdf_preflight_parser.add_argument(
        "--rights-status", choices=("restricted",), required=True
    )
    pdf_preflight_parser.add_argument(
        "--quality-status", choices=("manual_approved",), required=True
    )
    rag_verify_parser = subparsers.add_parser("verify-rag-bundle")
    rag_verify_parser.add_argument("path", type=Path)
    rag_ingest_parser = subparsers.add_parser("ingest-rag-bundle")
    rag_ingest_parser.add_argument("manifest")
    rag_ingest_parser.add_argument("--require-existing-active", action="store_true")
    rag_ingest_parser.add_argument("--require-embedded", type=int)
    rag_ingest_parser.add_argument("--require-skipped", type=int)
    rag_ingest_parser.add_argument("--reuse-from-release-id")
    rag_ingest_parser.add_argument("--require-source-eligible-total", type=int)
    rag_ingest_parser.add_argument("--require-non-reusable-embedded-total", type=int)
    rag_stats_parser = subparsers.add_parser("rag-db-stats")
    rag_stats_parser.add_argument("--release-id", default="v1.2-demo")
    poster_fleet_parser = subparsers.add_parser("verify-poster-fleet")
    poster_fleet_parser.add_argument("--manifest", required=True)
    poster_fleet_parser.add_argument("--bucket", required=True)
    poster_fleet_parser.add_argument("--workers", type=int, default=16)
    staging_parser = subparsers.add_parser("validate-staging")
    staging_parser.add_argument("--kind", choices=("poster", "analysis"), required=True)
    staging_parser.add_argument("path", type=Path)
    preflight_parser = subparsers.add_parser("gcp-preflight")
    preflight_parser.add_argument("--output", required=True, type=Path)
    archive_upload_parser = subparsers.add_parser("archive-upload")
    archive_upload_parser.add_argument("--inventory", required=True, type=Path)
    archive_upload_parser.add_argument("--bucket", required=True)
    archive_verify_parser = subparsers.add_parser("archive-verify")
    archive_verify_parser.add_argument("--inventory", required=True, type=Path)
    archive_verify_parser.add_argument("--bucket", required=True)
    archive_verify_parser.add_argument("--output", required=True, type=Path)
    cleanup_plan_parser = subparsers.add_parser("cleanup-plan")
    cleanup_plan_parser.add_argument("--inventory", required=True, type=Path)
    cleanup_plan_parser.add_argument("--archive-verification", required=True, type=Path)
    cleanup_plan_parser.add_argument("--quarantine-root", required=True, type=Path)
    cleanup_plan_parser.add_argument("--output", required=True, type=Path)
    cleanup_execute_parser = subparsers.add_parser("cleanup-execute")
    cleanup_execute_parser.add_argument("--plan", required=True, type=Path)
    cleanup_execute_parser.add_argument("--confirm-release", required=True)
    cleanup_execute_parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    if args.command == "verify-release":
        try:
            result = verify_release_manifest(args.path)
        except (ConfigError, ManifestError, ReleaseLockError) as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "artifact_count": result.artifact_count,
                    "movie_count": result.movie_count,
                    "release_version": result.release_version,
                    "valid": result.valid,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    if args.command == "preflight-pdf-binding":
        try:
            source_root = args.source_root.resolve()
            document_source_root = args.document_source_root.resolve()
            if not source_root.is_dir():
                raise RagBundleError("source root must resolve to a directory")
            if not document_source_root.is_dir():
                raise RagBundleError(
                    "document source root must resolve to a directory"
                )
            prepared = prepare_pdf_binding(
                source_root,
                args.config,
                document_source_root,
                movie_id=args.movie_id,
                document_id=args.document_id,
                source_filename=args.source_filename,
                rights_status=args.rights_status,
                quality_status=args.quality_status,
            )
        except (OSError, RagBundleError) as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                prepared.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    if args.command == "build-rag-bundle":
        try:
            document_source_root = (
                args.source_root
                if args.document_source_root is None
                else args.document_source_root
            ).resolve()
            if not document_source_root.is_dir():
                raise RagBundleError("document source root must resolve to a directory")
            rag_build_result = build_rag_bundle(
                args.source_root,
                args.config,
                args.output,
                document_source_root=document_source_root,
            )
        except (OSError, RagBundleError) as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "bundle_sha256": rag_build_result.bundle_sha256,
                    "document_count": rag_build_result.document_count,
                    "facet_count": rag_build_result.facet_count,
                    "manifest": str(rag_build_result.manifest_path),
                    "movie_count": rag_build_result.movie_count,
                    "pilot_count": rag_build_result.pilot_count,
                    "pdf_passage_count": rag_build_result.pdf_passage_count,
                    "poster_row_count": rag_build_result.poster_row_count,
                    "tier_a_count": rag_build_result.tier_a_count,
                    "tier_b_count": rag_build_result.tier_b_count,
                    "tier_s_count": rag_build_result.tier_s_count,
                    "primary_poster_row_count": rag_build_result.primary_poster_row_count,
                    "approved_poster_object_count": (
                        rag_build_result.approved_poster_object_count
                    ),
                    "unavailable_poster_row_count": (
                        rag_build_result.unavailable_poster_row_count
                    ),
                    "derived_poster_bytes": rag_build_result.derived_poster_bytes,
                    "derived_inventory_sha256": (
                        rag_build_result.derived_inventory_sha256
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    if args.command == "verify-rag-bundle":
        try:
            rag_verify_result = verify_rag_bundle(args.path)
            manifest_bytes = args.path.read_bytes()
            if (
                hashlib.sha256(manifest_bytes).hexdigest()
                != rag_verify_result.contract.manifest_sha256
            ):
                raise RagBundleError("RAG manifest changed after verification")
            manifest = json.loads(manifest_bytes)
            if not isinstance(manifest, dict) or not isinstance(
                manifest.get("documents"), list
            ):
                raise RagBundleError("verified RAG manifest has no exact document array")
            contract = rag_verify_result.contract
            release_id = contract.rag_release_id
            if (
                re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?", release_id)
                is None
                or ".." in release_id
            ):
                raise RagBundleError("RAG release ID is unsafe for immutable object names")
        except (OSError, RagBundleError) as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "access_mode": contract.access_mode,
                    "bundle_sha256": contract.bundle_sha256,
                    "document_count": rag_verify_result.document_count,
                    "document_embedding_profile": contract.document_embedding_profile,
                    "documents": manifest["documents"],
                    "embedding_dimension": rag_verify_result.embedding_dimension,
                    "embedding_model": rag_verify_result.embedding_model,
                    "expected_embedding_count": contract.expected_embeddings,
                    "facet_count": rag_verify_result.facet_count,
                    "gcs_prefix": f"rag/{release_id}/",
                    "generation_model": contract.generation_model,
                    "manifest_sha256": contract.manifest_sha256,
                    "metadata_passage_count": contract.counts.metadata_passages,
                    "movie_count": rag_verify_result.movie_count,
                    "parent_release_manifest_sha256": (
                        contract.parent_release_manifest_sha256
                    ),
                    "pilot_count": rag_verify_result.pilot_count,
                    "pdf_passage_count": rag_verify_result.pdf_passage_count,
                    "poster_authority_sha256": contract.poster_authority_sha256,
                    "poster_row_count": rag_verify_result.poster_row_count,
                    "rag_release_id": release_id,
                    "rag_schema_version": contract.schema_version,
                    "relevance_policy_sha256": contract.relevance_policy_sha256,
                    "tier_a_count": rag_verify_result.tier_a_count,
                    "tier_b_count": rag_verify_result.tier_b_count,
                    "tier_s_count": rag_verify_result.tier_s_count,
                    "primary_poster_row_count": rag_verify_result.primary_poster_row_count,
                    "approved_poster_object_count": (
                        rag_verify_result.approved_poster_object_count
                    ),
                    "unavailable_poster_row_count": (
                        rag_verify_result.unavailable_poster_row_count
                    ),
                    "derived_poster_bytes": rag_verify_result.derived_poster_bytes,
                    "derived_inventory_sha256": (
                        rag_verify_result.derived_inventory_sha256
                    ),
                    "schema_version": "rag-bundle-verification/v2",
                    "text_extraction_profile": contract.text_extraction_profile,
                    "valid": True,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    if args.command == "ingest-rag-bundle":
        required = (args.require_embedded, args.require_skipped)
        if (required[0] is None) != (required[1] is None):
            parser.error("ingestion acceptance counts must be provided together")
        if any(value is not None and value < 0 for value in required):
            parser.error("ingestion acceptance counts must be non-negative")
        reuse_acceptance = (
            args.require_source_eligible_total,
            args.require_non_reusable_embedded_total,
        )
        if args.reuse_from_release_id is not None and not args.reuse_from_release_id.strip():
            parser.error("reuse source release ID must be non-empty")
        if args.reuse_from_release_id is not None and any(
            value is None for value in reuse_acceptance
        ):
            parser.error("reuse acceptance counts must be provided together")
        if args.reuse_from_release_id is None and reuse_acceptance != (None, None):
            parser.error("reuse acceptance counts require a source release")
        if any(value is not None and value < 0 for value in reuse_acceptance):
            parser.error("reuse acceptance counts must be non-negative")
        project_id = os.environ.get("RAG_GCP_PROJECT_ID", "motionexpaiweb")
        location = os.environ.get("RAG_VERTEX_LOCATION", "global")
        try:
            with verified_bundle_source(args.manifest, project_id=project_id) as bundle:
                if args.reuse_from_release_id == bundle.rag_release_id:
                    parser.error("reuse source and target releases must be distinct")
                embedding_client = VertexEmbeddingClient(
                    project_id,
                    location,
                    bundle.embedding_model,
                    bundle.embedding_dimension,
                )
                with _repository_from_env() as repository:
                    ingestion = ingest_bundle(
                        bundle,
                        repository,
                        embedding_client,
                        require_existing_active=args.require_existing_active,
                        reuse_from_release_id=args.reuse_from_release_id,
                        require_source_eligible_total=args.require_source_eligible_total,
                        require_non_reusable_embedded_total=(
                            args.require_non_reusable_embedded_total
                        ),
                    )
        except (
            IngestionError,
            OSError,
            RagBundleError,
            RagDatabaseError,
            ValueError,
            VertexEmbeddingError,
        ) as exc:
            parser.error(str(exc))
        if required[0] is not None and (
            ingestion.embedded != required[0] or ingestion.skipped != required[1]
        ):
            parser.error("ingestion acceptance counts do not match")
        print(
            json.dumps(
                {
                    "active": ingestion.active,
                    "copied_this_run": ingestion.copied_this_run,
                    "embedded_this_run": ingestion.embedded_this_run,
                    "non_reusable_embedded_total": (
                        ingestion.non_reusable_embedded_total
                    ),
                    "preexisting": ingestion.preexisting,
                    "release_id": ingestion.release_id,
                    "run_id": ingestion.run_id,
                    "skipped_this_run": ingestion.skipped_this_run,
                    "source_eligible_total": ingestion.source_eligible_total,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    if args.command == "verify-poster-fleet":
        release_id = POSTER_FLEET_RELEASE_ID
        try:
            project_id = os.environ.get("RAG_GCP_PROJECT_ID", "motionexpaiweb")
            with verified_bundle_source(args.manifest, project_id=project_id) as bundle:
                release_id = bundle.rag_release_id
                poster_authority = load_poster_serving_authority(
                    bundle.rag_release_id,
                    bundle.contract.poster_authority_sha256,
                    contract=bundle.contract,
                )
                with _repository_from_env() as repository:
                    fleet = verify_poster_fleet(
                        repository,
                        GcsPosterFleetStorage(),
                        bundle,
                        args.bucket,
                        workers=args.workers,
                        authority=poster_authority,
                    )
        except Exception:  # noqa: BLE001 - the CLI must redact every provider/DB failure
            fleet = PosterFleetReport(
                release_id,
                0,
                0,
                0,
                0,
                (PosterFleetFailure("[fleet]", "verification_unavailable"),),
                False,
            )
        print(json.dumps(fleet.to_dict(), sort_keys=True, separators=(",", ":")))
        if not fleet.passed:
            raise SystemExit(1)
        return
    if args.command == "rag-db-stats":
        try:
            with _repository_from_env() as repository:
                stats = repository.release_stats(args.release_id)
                state = repository.release_state(args.release_id)
        except (OSError, RagDatabaseError, ValueError) as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "assets": stats.assets,
                    "documents": stats.documents,
                    "embedding_dimension": state.embedding_dimension,
                    "embedding_model": state.embedding_model,
                    "embeddings": stats.embeddings,
                    "metadata_passages": stats.metadata_passages,
                    "movies": stats.movies,
                    "pdf_passages": stats.pdf_passages,
                    "release_id": args.release_id,
                    "status": state.status,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    try:
        settings = Settings.load(Path.cwd())
    except ConfigError as exc:
        if args.command in {
            "archive-upload",
            "archive-verify",
            "cleanup-plan",
            "cleanup-execute",
        }:
            if args.command.startswith("cleanup-"):
                _cleanup_error("cleanup configuration is invalid")
            _archive_error("archive configuration is invalid")
        parser.error(str(exc))
    if args.command == "inventory":
        output = _resolve_output(settings.repo_root, args.output, settings.inventory_roots)
        write_inventory(build_inventory(settings), output)
    elif args.command == "gcp-preflight":
        try:
            output = _resolve_preflight_output(
                settings.repo_root, args.output, settings.inventory_roots
            )
            preflight_report = run_preflight(settings, GcloudJsonRunner())
            write_preflight_report(
                preflight_report,
                output,
                repo_root=settings.repo_root,
                protected_roots=settings.inventory_roots,
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(report_to_json(preflight_report))
        if not preflight_report.ready_for_archive:
            raise SystemExit(1)
    elif args.command == "archive-upload":
        try:
            inventory = _resolve_inventory_path(settings.repo_root, args.inventory)
            archive_runner = GcloudJsonRunner()
            authority = establish_archive_authority(
                settings,
                args.bucket,
                preflight_runner=archive_runner,
                token_runner=archive_runner,
                client_factory=lambda token: create_storage_client(
                    token,
                    settings.gcp.project_id,
                    token_refresher=lambda: archive_runner.access_token(
                        settings.gcp.required_account
                    ),
                ),
            )
            assert_archive_authority_current(authority)
            upload_report = upload_archive(
                authority.bucket,
                repo_root=settings.repo_root,
                inventory_path=inventory,
                release_version=settings.release_version,
                prefix=settings.gcp.archive_prefix,
                source_roots=settings.inventory_roots,
                authority_guard=lambda: assert_authority_state_current(authority),
            )
            assert_archive_authority_current(authority)
        except (ArchiveError, OSError, ValueError) as exc:
            _archive_error(str(exc))
        print(archive_report_to_json(upload_report), end="")
    elif args.command == "archive-verify":
        try:
            inventory = _resolve_inventory_path(settings.repo_root, args.inventory)
            archive_runner = GcloudJsonRunner()
            authority = establish_archive_authority(
                settings,
                args.bucket,
                preflight_runner=archive_runner,
                token_runner=archive_runner,
                client_factory=lambda token: create_storage_client(
                    token,
                    settings.gcp.project_id,
                    token_refresher=lambda: archive_runner.access_token(
                        settings.gcp.required_account
                    ),
                ),
            )
            assert_archive_authority_current(authority)
            output = _resolve_preflight_output(
                settings.repo_root, args.output, settings.inventory_roots
            )
            verification_report = verify_archive(
                authority.bucket,
                repo_root=settings.repo_root,
                inventory_path=inventory,
                release_version=settings.release_version,
                prefix=settings.gcp.archive_prefix,
                source_roots=settings.inventory_roots,
                authority_guard=lambda: assert_authority_state_current(authority),
            )
            assert_archive_authority_current(authority)
            write_verification_report(
                verification_report,
                output.relative_to(settings.repo_root),
                repo_root=settings.repo_root,
                protected_roots=settings.inventory_roots,
            )
        except (ArchiveError, OSError, ValueError) as exc:
            _archive_error(str(exc))
        print(archive_report_to_json(verification_report), end="")
    elif args.command == "cleanup-plan":
        try:
            inventory = args.inventory
            duplicate_proof = inventory.parent / "duplicate_proof.json"
            cleanup_plan = build_cleanup_plan(
                workspace_root=settings.repo_root,
                quarantine_root=args.quarantine_root,
                release_version=settings.release_version,
                source_roots=settings.inventory_roots,
                inventory_path=inventory,
                duplicate_proof_path=duplicate_proof,
                archive_verification_path=args.archive_verification,
            )
            write_cleanup_plan(
                cleanup_plan,
                args.output,
                repo_root=settings.repo_root,
                protected_roots=settings.inventory_roots,
            )
        except (CleanupError, OSError, ValueError) as exc:
            _cleanup_error(str(exc))
        print(plan_to_json(cleanup_plan), end="")
    elif args.command == "cleanup-execute":
        try:
            cleanup_plan = load_cleanup_plan(
                args.plan,
                repo_root=settings.repo_root,
                protected_roots=settings.inventory_roots,
            )
            cleanup_report = execute_cleanup(
                cleanup_plan,
                args.confirm_release,
                repo_root=settings.repo_root,
                report_output=args.output,
                protected_roots=settings.inventory_roots,
            )
        except (CleanupError, OSError, ValueError) as exc:
            _cleanup_error(str(exc))
        print(cleanup_report_to_json(cleanup_report), end="")
        if cleanup_report.status != "detached":
            raise SystemExit(1)
    elif args.command == "build-release-data":
        build_release_data(
            settings,
            settings.repo_root / "data" / "release" / settings.release_version,
        )
    elif args.command == "build-posters":
        release_dir = settings.repo_root / "data" / "release" / settings.release_version
        build_posters(
            settings,
            load_release_ids(release_dir / "movies.parquet"),
            settings.repo_root,
        )
    elif args.command == "build-manifest":
        try:
            manifest = build_release_manifest(settings)
        except (ConfigError, ManifestError, ReleaseLockError) as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "artifact_count": manifest.artifact_count,
                    "manifest": (
                        Path("data")
                        / "release"
                        / manifest.release_version
                        / "release_manifest.json"
                    ).as_posix(),
                    "release_version": manifest.release_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    elif args.command == "validate-staging":
        release_path = (
            settings.repo_root / "data" / "release" / settings.release_version / "movies.parquet"
        )
        try:
            staging_path = _resolve_staging_path(
                settings.repo_root, args.path, settings.inventory_roots
            )
            release_ids = load_release_ids(release_path)
            if args.kind == "poster":
                submission = validate_poster_submission(staging_path, release_ids)
                staging_report = {
                    "content_sha256": submission.content_sha256,
                    "kind": "poster",
                    "movie_id": submission.movie_id,
                    "publishable": submission.publishable,
                    "submission_id": submission.submission_id,
                }
            else:
                document = validate_analysis_submission(staging_path, release_ids)
                staging_report = {
                    "content_sha256": document.content_sha256,
                    "document_id": document.document_id,
                    "identity": document.identity,
                    "kind": "analysis",
                    "movie_id": document.movie_id,
                    "publishable": document.publishable,
                }
        except (StagingError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(staging_report, sort_keys=True, separators=(",", ":")))


def _resolve_output(repo_root: Path, output: Path, inventory_roots: Sequence[Path]) -> Path:
    if output.is_absolute() or ".." in output.parts:
        raise ValueError("output path is outside workspace")
    resolved_repo = repo_root.resolve(strict=True)
    resolved_output = (resolved_repo / output).resolve()
    if not resolved_output.is_relative_to(resolved_repo):
        raise ValueError("output path is outside workspace")
    if any(resolved_output.is_relative_to(root.resolve(strict=True)) for root in inventory_roots):
        raise ValueError("output path is under a protected source root")
    return resolved_output


@contextmanager
def _repository_from_env() -> Iterator[RagRepository]:
    connection = connect_db(DatabaseSettings.from_env())
    try:
        repository = RagRepository(connection)
        repository.ensure_schema()
        yield repository
    finally:
        connection.close()


def _resolve_preflight_output(
    repo_root: Path, output: Path, inventory_roots: Sequence[Path]
) -> Path:
    if output.is_absolute() or ".." in output.parts:
        raise ValueError("output path is outside workspace")
    resolved_repo = repo_root.resolve(strict=True)
    lexical_output = Path(os.path.abspath(resolved_repo / output))
    if not lexical_output.is_relative_to(resolved_repo):
        raise ValueError("output path is outside workspace")
    for root in inventory_roots:
        try:
            protected_root = root.resolve(strict=True)
        except FileNotFoundError:
            protected_root = Path(os.path.abspath(root))
        if not protected_root.is_relative_to(resolved_repo):
            raise ValueError("protected source root is outside workspace")
        if lexical_output.is_relative_to(protected_root):
            raise ValueError("output path is under a protected source root")
    return lexical_output


def _resolve_staging_path(repo_root: Path, path: Path, protected_roots: Sequence[Path]) -> Path:
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("staging path is outside workspace")
    resolved_repo = repo_root.resolve(strict=True)
    unresolved_path = resolved_repo / path
    try:
        resolved_path = unresolved_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("staging path is missing") from exc
    if not resolved_path.is_relative_to(resolved_repo):
        raise ValueError("staging path is outside workspace")
    if any(resolved_path.is_relative_to(root.resolve(strict=True)) for root in protected_roots):
        raise ValueError("staging path is under a protected source root")
    return unresolved_path


def _resolve_inventory_path(repo_root: Path, inventory: Path) -> Path:
    if inventory.is_absolute() or ".." in inventory.parts:
        raise ValueError("inventory path is outside workspace")
    resolved_root = repo_root.resolve(strict=True)
    candidate = resolved_root / inventory
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("inventory path is missing") from exc
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("inventory path is outside workspace")
    return candidate


def _archive_error(reason: str) -> None:
    print(
        json.dumps(
            {"ok": False, "error": "archive_operation_failed", "reason": reason}, sort_keys=True
        )
    )
    raise SystemExit(1)


def _cleanup_error(reason: str) -> None:
    print(
        json.dumps(
            {"ok": False, "error": "cleanup_operation_failed", "reason": reason},
            sort_keys=True,
        )
    )
    raise SystemExit(1)
