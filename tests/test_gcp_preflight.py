from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from hk_movie_rag import cli, gcp_preflight, posters
from hk_movie_rag.config import Settings
from hk_movie_rag.gcp_preflight import (
    GcloudCommandError,
    GcloudJsonRunner,
    report_to_json,
    run_preflight,
    write_preflight_report,
)


class FakeRunner:
    def __init__(self, settings: Settings) -> None:
        services = [
            {"config": {"name": name}, "state": "ENABLED"}
            for name in settings.gcp.required_services
        ]
        self.responses: dict[str, Any] = {
            "auth print-access-token": None,
            "auth list": [
                {
                    "account": settings.gcp.required_account,
                    "status": "ACTIVE",
                    "credential_file_override": r"C:\credentials\company.json",
                }
            ],
            "config get project": settings.gcp.project_id,
            "projects describe": {
                "projectId": settings.gcp.project_id,
                "projectNumber": "123456789",
                "lifecycleState": "ACTIVE",
            },
            "billing projects describe": {"billingEnabled": True},
            "projects get-iam-policy": {
                "bindings": [
                    {
                        "members": [f"user:{settings.gcp.required_account}"],
                        "role": "roles/viewer",
                    }
                ],
                "access_token": "raw-policy-secret-token",
            },
            "services list": services,
            "services list available": services,
            "storage buckets list": [
                {"name": "z-archive", "credential_path": r"C:\credentials\company.json"},
                {"name": "a-release"},
            ],
            "sql instances list": [
                {"name": "sql-z", "settings": {"password": "do-not-record"}},
                {"name": "sql-a"},
            ],
        }
        self.errors: dict[str, str] = {}
        self.calls: list[tuple[tuple[str, ...], bool]] = []

    @property
    def project(self) -> str:
        return str(self.responses["config get project"])

    @project.setter
    def project(self, value: str) -> None:
        self.responses["config get project"] = value

    def fail(self, command: str, *, stderr: str = "command failed") -> None:
        reason = "oauth_refresh_failed" if "invalid_grant" in stderr else "command_failed"
        self.errors[command] = reason

    def fail_with(self, command: str, reason: str) -> None:
        self.errors[command] = reason

    def run(self, argv: Sequence[str], *, expect_json: bool = True) -> Any:
        command = _operation(argv)
        self.calls.append((tuple(argv), expect_json))
        if command in self.errors:
            raise GcloudCommandError(self.errors[command])
        return self.responses[command]


@pytest.fixture
def settings(repo_root: Path) -> Settings:
    return Settings.load(repo_root)


@pytest.fixture
def fake_gcloud(settings: Settings) -> FakeRunner:
    return FakeRunner(settings)


def test_preflight_fails_closed_on_expired_oauth(
    settings: Settings, fake_gcloud: FakeRunner
) -> None:
    fake_gcloud.fail("auth print-access-token", stderr="invalid_grant")

    report = run_preflight(settings, fake_gcloud)

    assert report.ready_for_archive is False
    assert report.blockers == ["oauth_refresh_failed"]
    assert [call[0][1:3] for call in fake_gcloud.calls] == [("auth", "print-access-token")]


def test_preflight_rejects_wrong_active_project(
    settings: Settings, fake_gcloud: FakeRunner
) -> None:
    fake_gcloud.project = "another-project"

    report = run_preflight(settings, fake_gcloud)

    assert report.ready_for_archive is False
    assert "active_project_mismatch" in report.blockers


@pytest.mark.parametrize(
    ("command", "response", "expected_blocker"),
    [
        ("projects describe", {"lifecycleState": "DELETE_REQUESTED"}, "project_not_active"),
        ("billing projects describe", {"billingEnabled": False}, "billing_disabled"),
        ("projects get-iam-policy", GcloudCommandError("command_failed"), "iam_visibility_failed"),
        ("storage buckets list", GcloudCommandError("command_failed"), "bucket_inventory_unavailable"),
        ("sql instances list", GcloudCommandError("command_failed"), "sql_inventory_unavailable"),
    ],
)
def test_preflight_fails_closed_when_a_governance_check_is_not_satisfied(
    settings: Settings,
    fake_gcloud: FakeRunner,
    command: str,
    response: Any,
    expected_blocker: str,
) -> None:
    if isinstance(response, GcloudCommandError):
        fake_gcloud.fail_with(command, response.reason)
    else:
        fake_gcloud.responses[command] = response

    report = run_preflight(settings, fake_gcloud)

    assert report.ready_for_archive is False
    assert expected_blocker in report.blockers


def test_preflight_reports_timeout_and_malformed_json_without_diagnostics(
    settings: Settings, fake_gcloud: FakeRunner
) -> None:
    fake_gcloud.fail_with("projects describe", "timeout")
    timeout_report = run_preflight(settings, fake_gcloud)
    fake_gcloud.errors["projects describe"] = "malformed_json"
    malformed_report = run_preflight(settings, fake_gcloud)

    assert "project_lookup_timeout" in timeout_report.blockers
    assert "project_lookup_malformed_json" in malformed_report.blockers
    assert "stderr" not in report_to_json(timeout_report)
    assert "stderr" not in report_to_json(malformed_report)


def test_preflight_blocker_contract_is_finite_and_covers_every_failure_family() -> None:
    fixed = {
        "oauth_refresh_failed",
        "active_account_missing",
        "active_account_mismatch",
        "account_lookup_invalid",
        "active_project_missing",
        "active_project_mismatch",
        "project_id_mismatch",
        "project_not_active",
        "project_number_missing",
        "project_lookup_invalid",
        "billing_disabled",
        "billing_lookup_invalid",
        "iam_visibility_failed",
        "iam_visibility_invalid",
        "services_lookup_invalid",
        "services_availability_lookup_invalid",
        "required_services_unavailable",
        "bucket_inventory_unavailable",
        "sql_inventory_unavailable",
    }
    operations = {
        "oauth_probe",
        "account_lookup",
        "project_config_lookup",
        "project_lookup",
        "billing_lookup",
        "services_lookup",
        "services_availability_lookup",
    }
    reasons = {"timeout", "malformed_json", "gcloud_unavailable"}
    expected = fixed | {f"{operation}_failed" for operation in operations}
    expected |= {f"{operation}_{reason}" for operation in operations for reason in reasons}

    assert gcp_preflight.PREFLIGHT_BLOCKER_CODES == frozenset(expected)
    for operation in operations:
        assert gcp_preflight._failure_blocker(operation, "command_failed") in expected
        for reason in reasons:
            assert gcp_preflight._failure_blocker(operation, reason) in expected


def test_preflight_blocker_normalizer_deduplicates_known_codes_and_replaces_unknowns() -> None:
    assert gcp_preflight.normalize_preflight_blockers(
        [
            "services_availability_lookup_timeout",
            "services_availability_lookup_timeout",
            "raw_token_secret",
            "admin_motionexp_com",
        ]
    ) == ("preflight_contract_mismatch", "services_availability_lookup_timeout")


@pytest.mark.parametrize(
    ("required_services", "expected"),
    [
        (
            {
                "storage.googleapis.com": "ENABLED",
                "serviceusage.googleapis.com": "ELIGIBLE",
                "cloudresourcemanager.googleapis.com": "ENABLED",
            },
            True,
        ),
        (
            {
                "storage.googleapis.com": "ENABLED",
                "serviceusage.googleapis.com": "ENABLED",
                "cloudresourcemanager.googleapis.com": "ELIGIBLE",
            },
            True,
        ),
        (
            {
                "storage.googleapis.com": "ELIGIBLE",
                "serviceusage.googleapis.com": "ENABLED",
                "cloudresourcemanager.googleapis.com": "ENABLED",
            },
            False,
        ),
        (
            {
                "storage.googleapis.com": "ENABLED",
                "serviceusage.googleapis.com": "UNKNOWN",
                "cloudresourcemanager.googleapis.com": "ENABLED",
            },
            False,
        ),
        (
            {
                "storage.googleapis.com": "ENABLED",
                "serviceusage.googleapis.com": "UNAVAILABLE",
                "cloudresourcemanager.googleapis.com": "ENABLED",
            },
            False,
        ),
        (
            {"storage.googleapis.com": "ENABLED", "serviceusage.googleapis.com": "ENABLED"},
            False,
        ),
        ({"serviceusage.googleapis.com": "ENABLED"}, False),
    ],
)
def test_archive_required_service_contract_is_storage_strict_and_nonstorage_eligible(
    required_services: dict[str, str], expected: bool
) -> None:
    assert (
        gcp_preflight.archive_required_services_ready(
            required_services,
            (
                "storage.googleapis.com",
                "serviceusage.googleapis.com",
                "cloudresourcemanager.googleapis.com",
            ),
        )
        is expected
    )


def test_preflight_fails_closed_on_malformed_account_payload(
    settings: Settings, fake_gcloud: FakeRunner
) -> None:
    """Breaks if a valid JSON value with the wrong shape escapes as an exception."""
    fake_gcloud.responses["auth list"] = {"unexpected": []}

    report = run_preflight(settings, fake_gcloud)

    assert report.ready_for_archive is False
    assert "account_lookup_invalid" in report.blockers


def test_preflight_normalizes_sorted_inventory_and_redacts_raw_inputs(
    settings: Settings, fake_gcloud: FakeRunner
) -> None:
    report = run_preflight(settings, fake_gcloud)
    serialized = report_to_json(report)

    assert report.ready_for_archive is True
    assert report.active_account == "admin@motionexp.com"
    assert report.active_project == "motionexpaiweb"
    assert report.project_number == "123456789"
    assert report.project_lifecycle == "ACTIVE"
    assert report.billing_enabled is True
    assert report.caller_iam_visible is True
    assert report.required_services == {
        "cloudresourcemanager.googleapis.com": "ENABLED",
        "serviceusage.googleapis.com": "ENABLED",
        "storage.googleapis.com": "ENABLED",
    }
    assert report.existing_buckets == ["a-release", "z-archive"]
    assert report.cloud_sql_instances == ["sql-a", "sql-z"]
    assert report.blockers == []
    for forbidden in (
        "raw-policy-secret-token",
        "company.json",
        "do-not-record",
        "bindings",
        "members",
        "roles/viewer",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("enabled", "available", "expected_state", "ready", "blocker"),
    [
        (True, True, "ENABLED", True, None),
        (False, True, "ELIGIBLE", True, None),
        (False, False, "UNAVAILABLE", False, "required_services_unavailable"),
    ],
)
def test_preflight_distinguishes_enabled_eligible_and_unavailable_services(
    settings: Settings,
    fake_gcloud: FakeRunner,
    enabled: bool,
    available: bool,
    expected_state: str,
    ready: bool,
    blocker: str | None,
) -> None:
    required = settings.gcp.required_services[0]
    fake_gcloud.responses["services list"] = (
        [{"config": {"name": required}, "state": "ENABLED"}] if enabled else []
    )
    fake_gcloud.responses["services list available"] = (
        [{"config": {"name": required}, "state": "DISABLED"}] if available else []
    )
    for service in settings.gcp.required_services[1:]:
        fake_gcloud.responses["services list"].append(
            {"config": {"name": service}, "state": "ENABLED"}
        )
        fake_gcloud.responses["services list available"].append(
            {"config": {"name": service}, "state": "ENABLED"}
        )

    report = run_preflight(settings, fake_gcloud)

    assert report.required_services[required] == expected_state
    assert report.ready_for_archive is ready
    if blocker is None:
        assert report.blockers == []
    else:
        assert blocker in report.blockers


@pytest.mark.parametrize(
    ("command", "response", "expected_states", "expected_blocker"),
    [
        (
            "services list",
            GcloudCommandError("command_failed"),
            {"UNKNOWN"},
            "services_lookup_failed",
        ),
        (
            "services list",
            {"not": "a list"},
            {"UNKNOWN"},
            "services_lookup_invalid",
        ),
        (
            "services list available",
            GcloudCommandError("command_failed"),
            {"ENABLED"},
            "services_availability_lookup_failed",
        ),
        (
            "services list available",
            {"not": "a list"},
            {"ENABLED"},
            "services_availability_lookup_invalid",
        ),
    ],
)
def test_preflight_fails_closed_on_service_query_failure_or_malformed_payload(
    settings: Settings,
    fake_gcloud: FakeRunner,
    command: str,
    response: Any,
    expected_states: set[str],
    expected_blocker: str,
) -> None:
    if isinstance(response, GcloudCommandError):
        fake_gcloud.fail_with(command, response.reason)
    else:
        fake_gcloud.responses[command] = response

    report = run_preflight(settings, fake_gcloud)

    assert set(report.required_services.values()) == expected_states
    assert report.ready_for_archive is False
    assert expected_blocker in report.blockers


def test_preflight_uses_argv_json_queries_scoped_to_configured_project(
    settings: Settings, fake_gcloud: FakeRunner
) -> None:
    run_preflight(settings, fake_gcloud)

    calls = [call for call, _expect_json in fake_gcloud.calls]
    assert all(isinstance(call, tuple) for call in calls)
    assert all("--format=json" in call for call in calls[1:])
    assert calls[0][-1] == "--format=none"
    assert [_operation(call) for call in calls] == [
        "auth print-access-token",
        "auth list",
        "config get project",
        "projects describe",
        "billing projects describe",
        "projects get-iam-policy",
        "services list",
        "services list available",
        "storage buckets list",
        "sql instances list",
    ]
    assert any(call[:3] == ("gcloud", "projects", "describe") for call in calls)
    service_calls = [call for call in calls if call[1:3] == ("services", "list")]
    assert service_calls == [
        (
            "gcloud",
            "services",
            "list",
            "--enabled",
            "--project=motionexpaiweb",
            "--format=json",
        ),
        (
            "gcloud",
            "services",
            "list",
            "--available",
            "--project=motionexpaiweb",
            "--format=json",
        ),
    ]
    assert all(
        "--project=motionexpaiweb" in call
        for call in calls
        if call[1:3]
        in {
            ("projects", "get-iam-policy"),
            ("services", "list"),
            ("storage", "buckets"),
            ("sql", "instances"),
        }
    )


def test_gcloud_runner_is_bounded_shell_false_and_parses_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, '{"projectId":"motionexpaiweb"}', "")

    monkeypatch.setattr("hk_movie_rag.gcp_preflight.subprocess.run", fake_run)
    runner = GcloudJsonRunner(executable="gcloud")

    result = runner.run(
        ["gcloud", "projects", "describe", "motionexpaiweb", "--format=json"]
    )

    assert result == {"projectId": "motionexpaiweb"}
    environment = observed.pop("env")
    assert "GOOGLE_OAUTH_ACCESS_TOKEN" not in environment
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in environment
    assert "CLOUDSDK_CONFIG" not in environment
    assert observed == {
        "argv": ["gcloud", "projects", "describe", "motionexpaiweb", "--format=json"],
        "capture_output": True,
        "check": False,
        "shell": False,
        "text": True,
        "timeout": 120,
    }


def test_gcloud_runner_uses_a_bounded_120_second_default_and_allows_short_injection() -> None:
    assert GcloudJsonRunner(executable="gcloud")._timeout_seconds == 120
    assert GcloudJsonRunner(executable="gcloud", timeout_seconds=1)._timeout_seconds == 1
    assert GcloudJsonRunner(executable="gcloud", timeout_seconds=120)._timeout_seconds == 120
    for invalid_timeout in (0, 121):
        with pytest.raises(ValueError, match="between 1 and 120"):
            GcloudJsonRunner(executable="gcloud", timeout_seconds=invalid_timeout)


def test_gcloud_runner_short_injected_timeout_fails_closed_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(*_args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        raise subprocess.TimeoutExpired(["gcloud"], 2)

    monkeypatch.setattr("hk_movie_rag.gcp_preflight.subprocess.run", fake_run)

    with pytest.raises(GcloudCommandError, match="timeout"):
        GcloudJsonRunner(executable="gcloud", timeout_seconds=2).run(
            ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=json"]
        )

    assert observed["timeout"] == 2


def test_identity_validation_uses_scalar_config_values_and_allows_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert "--format=value" not in argv
        assert argv[:3] == ["gcloud", "config", "get-value"]
        return subprocess.CompletedProcess(argv, 0, "\n", "(unset)\ndiagnostic-secret")

    monkeypatch.setattr("hk_movie_rag.gcp_preflight.subprocess.run", fake_run)

    GcloudJsonRunner(executable="gcloud").validate_identity()

    assert calls == [
        ["gcloud", "config", "get-value", "auth/access_token_file"],
        ["gcloud", "config", "get-value", "auth/credential_file_override"],
        ["gcloud", "config", "get-value", "auth/impersonate_service_account"],
    ]


@pytest.mark.parametrize("configured_value", ["None\n", "(unset)\n", "C:/private/credential.json\n"])
def test_identity_validation_rejects_any_configured_override_without_leaking_value(
    monkeypatch: pytest.MonkeyPatch,
    configured_value: str,
) -> None:
    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, configured_value, "diagnostic-secret")

    monkeypatch.setattr("hk_movie_rag.gcp_preflight.subprocess.run", fake_run)

    with pytest.raises(GcloudCommandError) as raised:
        GcloudJsonRunner(executable="gcloud").validate_identity()

    assert raised.value.reason == "identity_override_configured"
    assert configured_value.strip() not in repr(raised.value)
    assert "diagnostic-secret" not in repr(raised.value)


@pytest.mark.parametrize(
    "effect",
    [
        subprocess.CompletedProcess(["gcloud"], 1, "value-secret", "diagnostic-secret"),
        subprocess.TimeoutExpired(["gcloud"], 60, output="value-secret", stderr="diagnostic-secret"),
    ],
)
def test_identity_validation_fails_closed_without_leaking_provider_output(
    monkeypatch: pytest.MonkeyPatch,
    effect: subprocess.CompletedProcess[str] | subprocess.TimeoutExpired,
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(effect, subprocess.TimeoutExpired):
            raise effect
        return effect

    monkeypatch.setattr("hk_movie_rag.gcp_preflight.subprocess.run", fake_run)

    with pytest.raises(GcloudCommandError) as raised:
        GcloudJsonRunner(executable="gcloud").validate_identity()

    assert raised.value.reason == "identity_config_unavailable"
    assert "value-secret" not in repr(raised.value)
    assert "diagnostic-secret" not in repr(raised.value)


@pytest.mark.parametrize(
    ("effect", "reason"),
    [
        (subprocess.TimeoutExpired(["gcloud"], 60), "timeout"),
        (subprocess.CompletedProcess(["gcloud"], 0, "not-json", ""), "malformed_json"),
        (
            subprocess.CompletedProcess(
                ["gcloud"],
                1,
                "",
                "invalid_grant token=secret credential=C:/secret/company.json",
            ),
            "oauth_refresh_failed",
        ),
    ],
)
def test_gcloud_runner_returns_only_safe_error_categories(
    monkeypatch: pytest.MonkeyPatch,
    effect: BaseException | subprocess.CompletedProcess[str],
    reason: str,
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(effect, BaseException):
            raise effect
        return effect

    monkeypatch.setattr("hk_movie_rag.gcp_preflight.subprocess.run", fake_run)
    runner = GcloudJsonRunner(executable="gcloud")

    with pytest.raises(GcloudCommandError) as raised:
        runner.run(
            [
                "gcloud",
                "auth",
                "list",
                "--filter=status:ACTIVE",
                "--format=json",
            ]
        )

    assert raised.value.reason == reason
    assert str(raised.value) == reason
    assert "secret" not in repr(raised.value)
    assert "company.json" not in repr(raised.value)


def test_gcloud_runner_rejects_commands_outside_read_only_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(["gcloud"], 0, "{}", "")

    monkeypatch.setattr("hk_movie_rag.gcp_preflight.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="read-only allowlist"):
        GcloudJsonRunner(executable="gcloud").run(
            ["gcloud", "projects", "delete", "motionexpaiweb", "--format=json"]
        )

    assert called is False


def test_preflight_normalizes_numeric_project_number_and_checks_project_id(
    settings: Settings, fake_gcloud: FakeRunner
) -> None:
    fake_gcloud.responses["projects describe"] = {
        "projectId": "another-project",
        "projectNumber": 123456789,
        "lifecycleState": "ACTIVE",
    }

    report = run_preflight(settings, fake_gcloud)

    assert report.project_number == "123456789"
    assert "project_id_mismatch" in report.blockers


def test_report_write_is_deterministic_and_atomic(
    tmp_path: Path, settings: Settings, fake_gcloud: FakeRunner
) -> None:
    report = run_preflight(settings, fake_gcloud)
    output = tmp_path / "artifacts" / "gcp" / "preflight.json"

    write_preflight_report(report, output, repo_root=tmp_path)
    write_preflight_report(report, output, repo_root=tmp_path)

    assert output.read_bytes() == (report_to_json(report) + "\n").encode("utf-8")
    assert not list(output.parent.glob("*.tmp"))


def test_report_write_preserves_previous_file_if_atomic_replace_fails(
    tmp_path: Path,
    settings: Settings,
    fake_gcloud: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = run_preflight(settings, fake_gcloud)
    output = tmp_path / "preflight.json"
    output.write_text("previous\n", encoding="utf-8")

    def deny_replace(*_args: object, **_kwargs: object) -> None:
        raise posters.PosterError("cannot write poster artifact")

    monkeypatch.setattr("hk_movie_rag.gcp_preflight._atomic_replace_bytes", deny_replace)

    with pytest.raises(OSError, match="preflight report could not be published"):
        write_preflight_report(report, output, repo_root=tmp_path)

    assert output.read_text(encoding="utf-8") == "previous\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_report_write_rejects_path_outside_repo(
    tmp_path: Path, settings: Settings, fake_gcloud: FakeRunner
) -> None:
    report = run_preflight(settings, fake_gcloud)

    with pytest.raises(ValueError, match="outside workspace"):
        write_preflight_report(report, tmp_path.parent / "escape.json", repo_root=tmp_path)


def test_cli_output_resolution_preserves_leaf_reparse_for_writer_rejection(
    tmp_path: Path, settings: Settings, fake_gcloud: FakeRunner
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "sentinel.bin"
    sentinel.write_bytes(b"protected-target")
    alias = tmp_path / "preflight.json"
    _make_directory_alias(alias, target)
    report = run_preflight(settings, fake_gcloud)

    output = cli._resolve_preflight_output(tmp_path, Path("preflight.json"), ())

    assert output == alias
    with pytest.raises(ValueError, match="unsafe"):
        write_preflight_report(report, output, repo_root=tmp_path)
    assert sentinel.read_bytes() == b"protected-target"


def test_cli_output_resolution_preserves_parent_alias_for_writer_rejection(
    tmp_path: Path, settings: Settings, fake_gcloud: FakeRunner
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "artifacts"
    _make_directory_alias(alias, target)
    report = run_preflight(settings, fake_gcloud)

    output = cli._resolve_preflight_output(tmp_path, Path("artifacts/preflight.json"), ())

    assert output == alias / "preflight.json"
    with pytest.raises(ValueError, match="unsafe|followed link"):
        write_preflight_report(report, output, repo_root=tmp_path)
    assert not (target / "preflight.json").exists()


def test_cli_output_resolution_accepts_a_detached_protected_root(
    tmp_path: Path,
) -> None:
    """Breaks if post-cleanup report output requires an archived source root."""
    detached_root = tmp_path / "detached-posters"

    output = cli._resolve_preflight_output(
        tmp_path,
        Path("artifacts/post-cleanup-verification.json"),
        (detached_root,),
    )

    assert output == tmp_path / "artifacts" / "post-cleanup-verification.json"


def test_report_write_rejects_hardlink_leaf_without_changing_alias(
    tmp_path: Path, settings: Settings, fake_gcloud: FakeRunner
) -> None:
    protected = tmp_path / "protected.json"
    protected.write_text("protected-hardlink-bytes\n", encoding="utf-8")
    output = tmp_path / "preflight.json"
    os.link(protected, output)
    report = run_preflight(settings, fake_gcloud)

    with pytest.raises(ValueError, match="unsafe"):
        write_preflight_report(report, output, repo_root=tmp_path)

    assert protected.read_text(encoding="utf-8") == "protected-hardlink-bytes\n"
    assert output.read_text(encoding="utf-8") == "protected-hardlink-bytes\n"
    assert not list(tmp_path.glob(".*.tmp"))


def test_report_write_enforces_protected_root_without_following_aliases(
    tmp_path: Path, settings: Settings, fake_gcloud: FakeRunner
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    report = run_preflight(settings, fake_gcloud)

    with pytest.raises(ValueError, match="protected source root"):
        write_preflight_report(
            report,
            protected / "preflight.json",
            repo_root=tmp_path,
            protected_roots=(protected,),
        )

    assert not (protected / "preflight.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows parent-swap race regression")
def test_report_writer_locks_parent_against_deterministic_swap_race(
    tmp_path: Path,
    settings: Settings,
    fake_gcloud: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "artifacts"
    parent.mkdir()
    moved = tmp_path / "moved-artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside-protected")
    report = run_preflight(settings, fake_gcloud)
    real_create_temporary = posters._create_windows_temporary_file
    attack: dict[str, object] = {"attempted": False, "moved": False, "error": None}

    def attacking_create_temporary(path: Path) -> tuple[object, Path]:
        attack["attempted"] = True
        try:
            parent.rename(moved)
            attack["moved"] = True
        except OSError as exc:
            attack["error"] = exc.winerror
        return real_create_temporary(path)

    monkeypatch.setattr(posters, "_create_windows_temporary_file", attacking_create_temporary)

    write_preflight_report(
        report,
        parent / "preflight.json",
        repo_root=tmp_path,
    )

    assert attack["attempted"] is True
    assert attack["moved"] is False
    assert attack["error"] in {5, 32}
    assert sentinel.read_bytes() == b"outside-protected"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel.bin"]
    assert not list(tmp_path.rglob(".*.tmp"))


def test_report_writer_rejects_parent_swap_before_atomic_guard_capture(
    tmp_path: Path,
    settings: Settings,
    fake_gcloud: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "artifacts"
    parent.mkdir()
    original_parent = tmp_path / "original-artifacts"
    report = run_preflight(settings, fake_gcloud)
    real_atomic_replace = gcp_preflight._atomic_replace_bytes

    def swap_before_guard(
        root: Path, path: Path, content: bytes, **kwargs: object
    ) -> None:
        parent.rename(original_parent)
        parent.mkdir()
        real_atomic_replace(root, path, content, **kwargs)

    monkeypatch.setattr(gcp_preflight, "_atomic_replace_bytes", swap_before_guard)

    with pytest.raises(OSError, match="could not be published"):
        write_preflight_report(report, parent / "preflight.json", repo_root=tmp_path)

    assert not (parent / "preflight.json").exists()
    assert not (original_parent / "preflight.json").exists()
    assert not list(tmp_path.rglob(".*.tmp"))


def test_cli_writes_report_and_prints_same_deterministic_json(
    tmp_path: Path,
    settings: Settings,
    fake_gcloud: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_settings = dataclasses.replace(
        settings,
        repo_root=tmp_path,
        inventory_roots=(),
        canonical_poster_roots=(),
    )
    report = run_preflight(settings, fake_gcloud)
    monkeypatch.setattr(cli.Settings, "load", lambda _root: cli_settings)
    monkeypatch.setattr(cli, "GcloudJsonRunner", lambda: fake_gcloud)
    monkeypatch.setattr(cli, "run_preflight", lambda _settings, _runner: report)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hk-movie-rag", "gcp-preflight", "--output", "artifacts/gcp/preflight.json"],
    )

    cli.main()

    assert capsys.readouterr().out == report_to_json(report) + "\n"
    assert (tmp_path / "artifacts" / "gcp" / "preflight.json").read_text(
        encoding="utf-8"
    ) == report_to_json(report) + "\n"


def test_cli_exits_nonzero_after_writing_blocked_report(
    tmp_path: Path,
    settings: Settings,
    fake_gcloud: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_settings = dataclasses.replace(settings, repo_root=tmp_path, inventory_roots=())
    fake_gcloud.fail("auth print-access-token", stderr="invalid_grant")
    report = run_preflight(settings, fake_gcloud)
    monkeypatch.setattr(cli.Settings, "load", lambda _root: cli_settings)
    monkeypatch.setattr(cli, "GcloudJsonRunner", lambda: fake_gcloud)
    monkeypatch.setattr(cli, "run_preflight", lambda _settings, _runner: report)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hk-movie-rag", "gcp-preflight", "--output", "preflight.json"],
    )

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 1
    assert json.loads(capsys.readouterr().out)["blockers"] == ["oauth_refresh_failed"]
    assert json.loads((tmp_path / "preflight.json").read_text(encoding="utf-8"))[
        "ready_for_archive"
    ] is False


def test_cli_reports_containment_error_without_running_gcloud(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_settings = dataclasses.replace(settings, repo_root=tmp_path, inventory_roots=())
    monkeypatch.setattr(cli.Settings, "load", lambda _root: cli_settings)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hk-movie-rag", "gcp-preflight", "--output", "../outside.json"],
    )

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 2
    assert "output path is outside workspace" in capsys.readouterr().err


def _operation(argv: Sequence[str]) -> str:
    parts = tuple(argv)
    if parts[1:3] == ("auth", "print-access-token"):
        return "auth print-access-token"
    if parts[1:3] == ("auth", "list"):
        return "auth list"
    if parts[1:4] == ("config", "get", "project"):
        return "config get project"
    if parts[1:3] == ("projects", "describe"):
        return "projects describe"
    if parts[1:4] == ("billing", "projects", "describe"):
        return "billing projects describe"
    if parts[1:3] == ("projects", "get-iam-policy"):
        return "projects get-iam-policy"
    if parts[1:3] == ("services", "list"):
        return "services list available" if "--available" in parts else "services list"
    if parts[1:4] == ("storage", "buckets", "list"):
        return "storage buckets list"
    if parts[1:4] == ("sql", "instances", "list"):
        return "sql instances list"
    raise AssertionError(f"unexpected gcloud argv shape: {parts!r}")


def _make_directory_alias(alias: Path, target: Path) -> None:
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(target)],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
        )
        if completed.returncode != 0:
            pytest.skip(f"cannot create directory alias fixture: {completed.stderr}")
