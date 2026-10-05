import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_TERRAFORM = ROOT / "infra" / "terraform" / "archive"


def test_archive_bucket_iam_uses_one_authoritative_policy() -> None:
    configuration_paths = sorted(
        [*ARCHIVE_TERRAFORM.glob("*.tf"), *ARCHIVE_TERRAFORM.glob("*.tf.json")]
    )
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in configuration_paths
    )

    assert _resource_count(source, "google_storage_bucket_iam_policy") == 1
    assert _resource_count(source, "google_storage_bucket_iam_member") == 0
    assert _resource_count(source, "google_storage_bucket_iam_binding") == 0


def _resource_count(source: str, resource_type: str) -> int:
    escaped_type = re.escape(resource_type)
    hcl_resources = re.findall(rf'resource\s+"{escaped_type}"\s+"', source)
    json_resources = re.findall(rf'"{escaped_type}"\s*:', source)
    return len(hcl_resources) + len(json_resources)


def test_resource_count_covers_hcl_and_json_terraform_configuration() -> None:
    source = """
resource "google_storage_bucket_iam_policy" "first" {}
resource "google_storage_bucket_iam_policy" "second" {}
{"resource":{"google_storage_bucket_iam_member":{"extra":{}}}}
{"resource":{"google_storage_bucket_iam_binding":{"extra":{}}}}
"""

    assert _resource_count(source, "google_storage_bucket_iam_policy") == 2
    assert _resource_count(source, "google_storage_bucket_iam_member") == 1
    assert _resource_count(source, "google_storage_bucket_iam_binding") == 1
