"""Fail-closed, read-only GCP authentication and project preflight."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .posters import (
    PosterError,
    _atomic_replace_bytes,
    _capture_ancestor_identities,
    _is_reparse,
    _require_ancestor_identities,
    _safe_directory,
    _safe_output_root,
)

_PREFLIGHT_FIXED_BLOCKER_CODES = frozenset(
    {
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
)
_PREFLIGHT_FAILURE_OPERATIONS = frozenset(
    {
        "oauth_probe",
        "account_lookup",
        "project_config_lookup",
        "project_lookup",
        "billing_lookup",
        "services_lookup",
        "services_availability_lookup",
    }
)
_PREFLIGHT_FAILURE_REASONS = frozenset({"timeout", "malformed_json", "gcloud_unavailable"})
PREFLIGHT_BLOCKER_CODES = frozenset(
    _PREFLIGHT_FIXED_BLOCKER_CODES
    | {f"{operation}_failed" for operation in _PREFLIGHT_FAILURE_OPERATIONS}
    | {
        f"{operation}_{reason}"
        for operation in _PREFLIGHT_FAILURE_OPERATIONS
        for reason in _PREFLIGHT_FAILURE_REASONS
    }
)
_PREFLIGHT_CONTRACT_MISMATCH = "preflight_contract_mismatch"
_ARCHIVE_STORAGE_SERVICE = "storage.googleapis.com"
_ARCHIVE_NON_STORAGE_READY_STATES = frozenset({"ENABLED", "ELIGIBLE"})


class GcloudCommandError(RuntimeError):
    """A safe gcloud failure category with all command output discarded."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class CommandRunner(Protocol):
    """Narrow injectable command boundary used by the preflight."""

    def run(self, argv: Sequence[str], *, expect_json: bool = True) -> Any:
        """Run one bounded gcloud argv array and return parsed JSON."""


class GcloudJsonRunner:
    """Run bounded gcloud commands without a shell or retained diagnostics."""

    def __init__(self, *, executable: str | None = None, timeout_seconds: int = 120) -> None:
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("gcloud timeout must be between 1 and 120 seconds")
        self._executable = _trusted_gcloud_executable(executable)
        self._timeout_seconds = timeout_seconds

    def run(self, argv: Sequence[str], *, expect_json: bool = True) -> Any:
        command = list(argv)
        if (
            not command
            or command[0] != "gcloud"
            or len(command) > 64
            or any(not isinstance(part, str) or not part or "\0" in part for part in command)
        ):
            raise ValueError("invalid bounded gcloud argv")
        if not _is_allowed_read_only_command(command, expect_json=expect_json):
            raise ValueError("gcloud command is outside the read-only allowlist")
        command[0] = self._executable
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=self._timeout_seconds,
                env=_sanitized_gcloud_environment(self._executable),
            )
        except subprocess.TimeoutExpired:
            raise GcloudCommandError("timeout") from None
        except OSError:
            raise GcloudCommandError("gcloud_unavailable") from None
        if completed.returncode != 0:
            diagnostic = f"{completed.stdout}\n{completed.stderr}".casefold()
            if any(
                marker in diagnostic
                for marker in (
                    "invalid_grant",
                    "refresh token",
                    "reauthentication",
                    "credentials have been revoked",
                )
            ):
                raise GcloudCommandError("oauth_refresh_failed")
            raise GcloudCommandError("command_failed")
        if not expect_json:
            return None
        try:
            return json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            raise GcloudCommandError("malformed_json") from None

    def access_token(self, account: str) -> str:
        """Return a short-lived token for one exact account without logging or persisting it."""
        if not _safe_account(account):
            raise GcloudCommandError("invalid_account")
        self.validate_identity()
        command = [
            self._executable,
            "auth",
            "print-access-token",
            f"--account={account}",
            "--format=value(token)",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=self._timeout_seconds,
                env=_sanitized_gcloud_environment(self._executable),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise GcloudCommandError("token_unavailable") from None
        if completed.returncode != 0:
            raise GcloudCommandError("token_unavailable")
        token = completed.stdout.strip()
        if not token or any(character.isspace() for character in token):
            raise GcloudCommandError("token_invalid")
        return token

    def validate_identity(self) -> None:
        """Reject Cloud SDK auth override properties before any preflight/token call."""
        for property_name in (
            "auth/access_token_file",
            "auth/credential_file_override",
            "auth/impersonate_service_account",
        ):
            try:
                completed = subprocess.run(
                    [self._executable, "config", "get-value", property_name],
                    capture_output=True,
                    check=False,
                    shell=False,
                    text=True,
                    timeout=self._timeout_seconds,
                    env=_sanitized_gcloud_environment(self._executable),
                )
            except (OSError, subprocess.TimeoutExpired):
                raise GcloudCommandError("identity_config_unavailable") from None
            if completed.returncode != 0:
                raise GcloudCommandError("identity_config_unavailable")
            if not isinstance(completed.stdout, str) or completed.stdout.strip():
                raise GcloudCommandError("identity_override_configured")


@dataclass(frozen=True)
class GcpPreflightReport:
    active_account: str | None
    active_project: str | None
    project_number: str | None
    project_lifecycle: str | None
    billing_enabled: bool | None
    caller_iam_visible: bool
    required_services: dict[str, str]
    existing_buckets: list[str]
    cloud_sql_instances: list[str]
    ready_for_archive: bool
    blockers: list[str]


def run_preflight(settings: Settings, runner: CommandRunner) -> GcpPreflightReport:
    """Collect normalized read-only GCP state and fail closed on every missing gate."""

    active_account: str | None = None
    active_project: str | None = None
    project_number: str | None = None
    project_lifecycle: str | None = None
    billing_enabled: bool | None = None
    caller_iam_visible = False
    required_services = {name: "UNKNOWN" for name in settings.gcp.required_services}
    existing_buckets: list[str] = []
    cloud_sql_instances: list[str] = []
    blockers: list[str] = []

    try:
        runner.run(
            ["gcloud", "auth", "print-access-token", "--format=none"],
            expect_json=False,
        )
    except GcloudCommandError as exc:
        if exc.reason == "oauth_refresh_failed":
            blockers.append("oauth_refresh_failed")
        else:
            blockers.append(_failure_blocker("oauth_probe", exc.reason))
        return _report(
            active_account,
            active_project,
            project_number,
            project_lifecycle,
            billing_enabled,
            caller_iam_visible,
            required_services,
            existing_buckets,
            cloud_sql_instances,
            blockers,
        )

    try:
        accounts = runner.run(
            [
                "gcloud",
                "auth",
                "list",
                "--filter=status:ACTIVE",
                "--format=json",
            ]
        )
        active_account = _active_account(accounts)
        if active_account is None:
            blockers.append("active_account_missing")
        elif active_account != settings.gcp.required_account:
            blockers.append("active_account_mismatch")
    except GcloudCommandError as exc:
        blockers.append(_failure_blocker("account_lookup", exc.reason))
    except TypeError:
        blockers.append("account_lookup_invalid")

    try:
        configured_project = runner.run(
            ["gcloud", "config", "get", "project", "--format=json"]
        )
        active_project = _scalar_string(configured_project)
        if active_project is None:
            blockers.append("active_project_missing")
        elif active_project != settings.gcp.project_id:
            blockers.append("active_project_mismatch")
    except GcloudCommandError as exc:
        blockers.append(_failure_blocker("project_config_lookup", exc.reason))

    try:
        project = runner.run(
            [
                "gcloud",
                "projects",
                "describe",
                settings.gcp.project_id,
                "--format=json",
            ]
        )
        if not isinstance(project, dict):
            raise TypeError
        described_project = _optional_string(project.get("projectId"))
        project_number = _project_number(project.get("projectNumber"))
        project_lifecycle = _optional_string(project.get("lifecycleState"))
        if described_project != settings.gcp.project_id:
            blockers.append("project_id_mismatch")
        if project_lifecycle != "ACTIVE":
            blockers.append("project_not_active")
        if project_number is None:
            blockers.append("project_number_missing")
    except GcloudCommandError as exc:
        blockers.append(_failure_blocker("project_lookup", exc.reason))
    except TypeError:
        blockers.append("project_lookup_invalid")

    try:
        billing = runner.run(
            [
                "gcloud",
                "billing",
                "projects",
                "describe",
                settings.gcp.project_id,
                "--format=json",
            ]
        )
        if not isinstance(billing, dict) or not isinstance(billing.get("billingEnabled"), bool):
            raise TypeError
        billing_enabled = billing["billingEnabled"]
        if not billing_enabled:
            blockers.append("billing_disabled")
    except GcloudCommandError as exc:
        blockers.append(_failure_blocker("billing_lookup", exc.reason))
    except TypeError:
        blockers.append("billing_lookup_invalid")

    try:
        policy = runner.run(
            [
                "gcloud",
                "projects",
                "get-iam-policy",
                settings.gcp.project_id,
                f"--project={settings.gcp.project_id}",
                "--format=json",
            ]
        )
        if not isinstance(policy, dict) or not isinstance(policy.get("bindings"), list):
            raise TypeError
        caller_iam_visible = True
    except GcloudCommandError:
        blockers.append("iam_visibility_failed")
    except TypeError:
        blockers.append("iam_visibility_invalid")

    enabled_services: set[str] | None = None
    available_services: set[str] | None = None
    try:
        services = runner.run(
            [
                "gcloud",
                "services",
                "list",
                "--enabled",
                f"--project={settings.gcp.project_id}",
                "--format=json",
            ]
        )
        enabled_services = _service_names(services, enabled_only=True)
    except GcloudCommandError as exc:
        blockers.append(_failure_blocker("services_lookup", exc.reason))
    except TypeError:
        blockers.append("services_lookup_invalid")

    try:
        services = runner.run(
            [
                "gcloud",
                "services",
                "list",
                "--available",
                f"--project={settings.gcp.project_id}",
                "--format=json",
            ]
        )
        available_services = _service_names(services, enabled_only=False)
    except GcloudCommandError as exc:
        blockers.append(_failure_blocker("services_availability_lookup", exc.reason))
    except TypeError:
        blockers.append("services_availability_lookup_invalid")

    if enabled_services is not None:
        required_services = {
            name: (
                "ENABLED"
                if name in enabled_services
                else (
                    "ELIGIBLE"
                    if available_services is not None and name in available_services
                    else "UNAVAILABLE" if available_services is not None else "UNKNOWN"
                )
            )
            for name in settings.gcp.required_services
        }
        if not archive_required_services_ready(
            required_services, settings.gcp.required_services
        ):
            blockers.append("required_services_unavailable")

    try:
        buckets = runner.run(
            [
                "gcloud",
                "storage",
                "buckets",
                "list",
                f"--project={settings.gcp.project_id}",
                "--format=json",
            ]
        )
        existing_buckets = _inventory_names(buckets)
    except (GcloudCommandError, TypeError):
        blockers.append("bucket_inventory_unavailable")

    try:
        instances = runner.run(
            [
                "gcloud",
                "sql",
                "instances",
                "list",
                f"--project={settings.gcp.project_id}",
                "--format=json",
            ]
        )
        cloud_sql_instances = _inventory_names(instances)
    except (GcloudCommandError, TypeError):
        blockers.append("sql_inventory_unavailable")

    return _report(
        active_account,
        active_project,
        project_number,
        project_lifecycle,
        billing_enabled,
        caller_iam_visible,
        required_services,
        existing_buckets,
        cloud_sql_instances,
        blockers,
    )


def report_to_json(report: GcpPreflightReport) -> str:
    """Serialize only the normalized report fields in deterministic form."""

    return json.dumps(asdict(report), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def write_preflight_report(
    report: GcpPreflightReport,
    output: Path,
    *,
    repo_root: Path,
    protected_roots: Sequence[Path] = (),
) -> None:
    """Publish below a lexical no-follow path using the hardened atomic installer."""

    try:
        root = _safe_output_root(repo_root)
    except PosterError:
        raise ValueError("preflight report root is unsafe") from None
    candidate = output if output.is_absolute() else root / output
    lexical = Path(os.path.abspath(candidate))
    if lexical == root or not lexical.is_relative_to(root):
        raise ValueError("output path is outside workspace")
    protected = tuple(_protected_root(root, path) for path in protected_roots)
    _validate_lexical_separation(lexical, protected)
    try:
        parent = _safe_directory(root, lexical.parent.relative_to(root))
        expected_ancestors = _capture_ancestor_identities(root, parent)
    except PosterError:
        raise ValueError("preflight report output is unsafe") from None
    destination = parent / lexical.name
    content = (report_to_json(report) + "\n").encode("utf-8")

    def validate_destination() -> None:
        _require_ancestor_identities(root, parent, expected_ancestors)
        _validate_lexical_separation(destination, protected)
        try:
            destination_stat = destination.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise ValueError("preflight report output is unsafe") from None
        if (
            not destination.is_file()
            or _is_reparse(destination_stat)
            or destination_stat.st_nlink != 1
        ):
            raise ValueError("preflight report output is unsafe")

    try:
        _atomic_replace_bytes(
            root,
            destination,
            content,
            pre_install=validate_destination,
        )
    except PosterError:
        raise OSError("preflight report could not be published") from None
    except ValueError:
        raise


def _report(
    active_account: str | None,
    active_project: str | None,
    project_number: str | None,
    project_lifecycle: str | None,
    billing_enabled: bool | None,
    caller_iam_visible: bool,
    required_services: dict[str, str],
    existing_buckets: list[str],
    cloud_sql_instances: list[str],
    blockers: list[str],
) -> GcpPreflightReport:
    normalized_blockers = normalize_preflight_blockers(blockers)
    return GcpPreflightReport(
        active_account=active_account,
        active_project=active_project,
        project_number=project_number,
        project_lifecycle=project_lifecycle,
        billing_enabled=billing_enabled,
        caller_iam_visible=caller_iam_visible,
        required_services=dict(sorted(required_services.items())),
        existing_buckets=sorted(existing_buckets),
        cloud_sql_instances=sorted(cloud_sql_instances),
        ready_for_archive=not normalized_blockers,
        blockers=list(normalized_blockers),
    )


def _active_account(value: Any) -> str | None:
    if not isinstance(value, list):
        raise TypeError
    accounts: list[str] = []
    for entry in value:
        if not isinstance(entry, dict) or str(entry.get("status", "")).casefold() != "active":
            continue
        account = entry.get("account")
        if isinstance(account, str):
            accounts.append(account)
    return min(accounts) if accounts else None


def _scalar_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("project", "core.project"):
            candidate = _optional_string(value.get(key))
            if candidate is not None:
                return candidate
    return None


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _service_names(value: Any, *, enabled_only: bool) -> set[str]:
    if not isinstance(value, list):
        raise TypeError
    result: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise TypeError
        config = entry.get("config")
        name = config.get("name") if isinstance(config, dict) else entry.get("name")
        state = entry.get("state", "ENABLED")
        if not isinstance(name, str) or not isinstance(state, str):
            raise TypeError
        if enabled_only and state.upper() != "ENABLED":
            raise TypeError
        result.add(name)
    return result


def _project_number(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return value
    return None


def _is_allowed_read_only_command(command: list[str], *, expect_json: bool) -> bool:
    parts = tuple(command)
    if parts == ("gcloud", "auth", "print-access-token", "--format=none"):
        return expect_json is False
    if not expect_json:
        return False
    if parts == (
        "gcloud",
        "auth",
        "list",
        "--filter=status:ACTIVE",
        "--format=json",
    ) or parts == ("gcloud", "config", "get", "project", "--format=json"):
        return True
    if len(parts) == 5 and parts[1:3] == ("projects", "describe"):
        return _safe_project(parts[3]) and parts[4] == "--format=json"
    if len(parts) == 6 and parts[1:4] == ("billing", "projects", "describe"):
        return _safe_project(parts[4]) and parts[5] == "--format=json"
    if len(parts) == 6 and parts[1:3] == ("projects", "get-iam-policy"):
        project = parts[3]
        return (
            _safe_project(project)
            and parts[4] == f"--project={project}"
            and parts[5] == "--format=json"
        )
    if len(parts) == 6 and parts[1:3] == ("services", "list"):
        project_flag = parts[4]
        return (
            parts[3] in {"--enabled", "--available"}
            and project_flag.startswith("--project=")
            and _safe_project(project_flag.removeprefix("--project="))
            and parts[5] == "--format=json"
        )
    if len(parts) == 6 and parts[1:4] in {
        ("storage", "buckets", "list"),
        ("sql", "instances", "list"),
    }:
        project_flag = parts[4]
        return (
            project_flag.startswith("--project=")
            and _safe_project(project_flag.removeprefix("--project="))
            and parts[5] == "--format=json"
        )
    return False


def _safe_account(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 320
        and "@" in value
        and not any(character.isspace() or character == "\0" for character in value)
    )


def _trusted_gcloud_executable(executable: str | None) -> str:
    if executable is not None:
        return executable
    path = Path(r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd")
    try:
        lexical = Path(os.path.abspath(path))
        if not lexical.is_absolute() or not lexical.is_file():
            raise OSError
        for ancestor in (lexical, *lexical.parents):
            path_stat = ancestor.lstat()
            attributes = getattr(path_stat, "st_file_attributes", 0)
            if os.path.islink(ancestor) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise OSError
    except OSError:
        raise GcloudCommandError("gcloud_unavailable") from None
    return str(lexical)


def _sanitized_gcloud_environment(executable: str) -> dict[str, str]:
    """Pass only OS/config discovery essentials; never inherited credential overrides or secrets."""
    allowed = ("SYSTEMROOT", "WINDIR", "COMSPEC", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["PATH"] = str(Path(executable).parent) if Path(executable).is_absolute() else ""
    return environment


def _safe_project(project: str) -> bool:
    return bool(project) and not project.startswith("-") and all(
        character.islower() or character.isdigit() or character in {"-", ":", "."}
        for character in project
    )


def _inventory_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise TypeError
    names: list[str] = []
    for entry in value:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise TypeError
        names.append(entry["name"])
    return sorted(set(names))


def _failure_blocker(operation: str, reason: str) -> str:
    candidate = (
        f"{operation}_{reason}"
        if reason in _PREFLIGHT_FAILURE_REASONS
        else f"{operation}_failed"
    )
    return candidate if candidate in PREFLIGHT_BLOCKER_CODES else _PREFLIGHT_CONTRACT_MISMATCH


def normalize_preflight_blockers(blockers: object) -> tuple[str, ...]:
    """Return only registered public blockers, replacing unknown values fail-closed."""
    if not isinstance(blockers, list):
        return (_PREFLIGHT_CONTRACT_MISMATCH,)
    normalized = {
        blocker
        for blocker in blockers
        if isinstance(blocker, str) and blocker in PREFLIGHT_BLOCKER_CODES
    }
    if any(not isinstance(blocker, str) or blocker not in PREFLIGHT_BLOCKER_CODES for blocker in blockers):
        normalized.add(_PREFLIGHT_CONTRACT_MISMATCH)
    return tuple(sorted(normalized))


def archive_required_services_ready(
    required_services: object, required_service_names: object
) -> bool:
    """Apply the archive access service contract to a normalized preflight report."""
    if not isinstance(required_services, dict):
        return False
    if not isinstance(required_service_names, Sequence) or isinstance(
        required_service_names, (str, bytes)
    ):
        return False
    if not all(isinstance(name, str) for name in required_service_names):
        return False
    expected_services = set(required_service_names)
    expected_services.add(_ARCHIVE_STORAGE_SERVICE)
    if not expected_services.issubset(required_services):
        return False
    for name, state in required_services.items():
        if not isinstance(name, str) or not isinstance(state, str):
            return False
        if name == _ARCHIVE_STORAGE_SERVICE:
            if state != "ENABLED":
                return False
        elif state not in _ARCHIVE_NON_STORAGE_READY_STATES:
            return False
    return True


def _protected_root(root: Path, protected: Path) -> Path:
    lexical = Path(os.path.abspath(protected))
    if not lexical.is_relative_to(root):
        raise ValueError("protected source root is outside workspace")
    return lexical


def _validate_lexical_separation(path: Path, protected_roots: Sequence[Path]) -> None:
    if any(path.is_relative_to(protected) for protected in protected_roots):
        raise ValueError("output path is under a protected source root")
