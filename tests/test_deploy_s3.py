"""Deploy S3 settings, grant-injected AWS credentials, and ``deploy check-bucket``."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from culture_library.cli import main
from culture_library.cli._commands import deploy
from culture_library.explain.catalog import ENTRIES

ROOT = Path(__file__).resolve().parent.parent
TARGETS = '[{"label":"x3","type":"reader","enabled":true}]'


class _Err(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, pab=None, enc="ok", ver="Enabled"):
        self.pab = pab if pab is not None else dict.fromkeys(deploy.PAB_FLAGS, True)
        self.enc, self.ver = enc, ver

    def get_public_access_block(self, Bucket):
        if isinstance(self.pab, Exception):
            raise self.pab
        return {"PublicAccessBlockConfiguration": self.pab}

    def get_bucket_encryption(self, Bucket):
        if isinstance(self.enc, Exception):
            raise self.enc
        rules = [] if self.enc == "empty" else [{"x": 1}]
        return {"ServerSideEncryptionConfiguration": {"Rules": rules}}

    def get_bucket_versioning(self, Bucket):
        return {"Status": self.ver} if self.ver else {}


class FakeSts:
    def __init__(self, arn="arn:aws:iam::123:user/lib"):
        self.arn = arn

    def get_caller_identity(self):
        return {"Arn": self.arn}


class FakeIam:
    def __init__(self, decision="implicitDeny"):
        self.decision, self.args = decision, None

    def simulate_principal_policy(self, **kw):
        self.args = kw
        if isinstance(self.decision, Exception):
            raise self.decision
        return {"EvaluationResults": [{"EvalDecision": self.decision}]}


@pytest.fixture
def clients(monkeypatch):
    box = {"s3": FakeS3(), "sts": FakeSts(), "iam": FakeIam()}
    monkeypatch.setattr(deploy, "_aws_client", lambda svc, region: box[svc])
    return box


def _check(*extra):
    return main(["deploy", "check-bucket", "--bucket", "b1", *extra, "--json"])


def _report(capsys):
    return json.loads(capsys.readouterr().out)


def test_healthy_bucket(clients, capsys):
    assert _check() == 0
    rep = _report(capsys)
    assert rep["healthy"] is True
    assert {c["id"] for c in rep["checks"]} == {
        "public_access_block",
        "default_encryption",
        "versioning",
        "delete_object_denied",
    }
    assert all(
        set(c) >= {"id", "passed", "severity", "message", "remediation"} for c in rep["checks"]
    )
    assert clients["iam"].args["ActionNames"] == ["s3:DeleteObject"]
    assert clients["iam"].args["ResourceArns"] == ["arn:aws:s3:::b1/*"]


def _failed(capsys):
    return {c["id"] for c in _report(capsys)["checks"] if not c["passed"]}


def test_public_access_block_partial_fails(clients, capsys):
    pab = dict.fromkeys(deploy.PAB_FLAGS, True)
    pab["RestrictPublicBuckets"] = False
    clients["s3"] = FakeS3(pab=pab)
    assert _check() == 1
    assert _failed(capsys) == {"public_access_block"}


def test_public_access_block_absent_fails(clients, capsys):
    clients["s3"] = FakeS3(pab=_Err("NoSuchPublicAccessBlockConfiguration"))
    assert _check() == 1
    assert _failed(capsys) == {"public_access_block"}


def test_encryption_missing_fails(clients, capsys):
    clients["s3"] = FakeS3(enc=_Err("ServerSideEncryptionConfigurationNotFoundError"))
    assert _check() == 1
    assert _failed(capsys) == {"default_encryption"}


def test_encryption_empty_rules_fails(clients, capsys):
    clients["s3"] = FakeS3(enc="empty")
    assert _check() == 1
    assert _failed(capsys) == {"default_encryption"}


@pytest.mark.parametrize("status", ["Suspended", None])
def test_versioning_off_fails(clients, capsys, status):
    clients["s3"] = FakeS3(ver=status)
    assert _check() == 1
    assert _failed(capsys) == {"versioning"}


def test_delete_allowed_fails(clients, capsys):
    clients["iam"] = FakeIam("allowed")
    assert _check() == 1
    assert _failed(capsys) == {"delete_object_denied"}


def test_explicit_deny_passes(clients, capsys):
    clients["iam"] = FakeIam("explicitDeny")
    assert _check() == 0


def test_simulation_not_permitted_is_unknown_not_pass(clients, capsys):
    clients["iam"] = FakeIam(_Err("AccessDenied"))
    assert _check() == 1
    rep = _report(capsys)
    chk = next(c for c in rep["checks"] if c["id"] == "delete_object_denied")
    assert chk["passed"] is False
    assert chk["status"] == "unknown"
    assert "iam:SimulatePrincipalPolicy" in chk["remediation"]
    assert rep["healthy"] is False


def test_assumed_role_arn_is_mapped_to_the_role(clients, capsys):
    clients["sts"] = FakeSts("arn:aws:sts::123:assumed-role/libr/sess")
    assert _check() == 0
    assert clients["iam"].args["PolicySourceArn"] == "arn:aws:iam::123:role/libr"


def test_bucket_from_env_and_missing_bucket(clients, monkeypatch, capsys):
    monkeypatch.delenv("CULTURE_LIBRARY_S3_BUCKET", raising=False)
    assert main(["deploy", "check-bucket", "--json"]) == 1
    assert "bucket" in json.loads(capsys.readouterr().err)["message"]
    monkeypatch.setenv("CULTURE_LIBRARY_S3_BUCKET", "envb")
    assert main(["deploy", "check-bucket", "--json"]) == 0


def test_missing_boto3_is_an_env_error(monkeypatch, capsys):
    monkeypatch.setitem(__import__("sys").modules, "boto3", None)
    assert _check() == 2
    assert "culture-library[s3]" in json.loads(capsys.readouterr().err)["remediation"]


def test_explain_and_learn_mention_check_bucket():
    assert ("deploy", "check-bucket") in ENTRIES
    assert "check-bucket" in ENTRIES[("deploy", "check-bucket")]


# --- restart ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _grant(monkeypatch):
    monkeypatch.setattr(deploy, "_grant_secret_exists", lambda name, env: True)


def _env(tmp_path, **over):
    values = {
        "COMPOSE_PROJECT_NAME": "culture-library",
        "CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.com",
        "CULTURE_LIBRARY_ACCESS_AUD": "aud123",
        "CULTURE_LIBRARY_SEND_TARGETS": f"'{TARGETS}'",
    }
    values.update(over)
    path = tmp_path / "live.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def _dry(tmp_path, capsys, *extra, **over):
    comp = tmp_path / "c.yaml"
    comp.write_text("services: {}\n")
    env = _env(tmp_path, **over)
    rc = main(
        [
            "deploy",
            "restart",
            "--env-file",
            str(env),
            "--compose",
            str(comp),
            "--dry-run",
            *extra,
            "--json",
        ]
    )
    return rc, capsys.readouterr()


def test_no_bucket_no_aws_injection(tmp_path, capsys):
    rc, out = _dry(tmp_path, capsys)
    assert rc == 0
    assert "AWS_ACCESS_KEY_ID" not in out.out


def test_bucket_injects_aws_credentials_from_grant(tmp_path, capsys):
    rc, out = _dry(tmp_path, capsys, CULTURE_LIBRARY_S3_BUCKET="my-bucket")
    assert rc == 0
    step = json.loads(out.out)["steps"][-1]
    assert f"AWS_ACCESS_KEY_ID={deploy.DEFAULT_AWS_KEY_ID_SECRET}" in step
    assert f"AWS_SECRET_ACCESS_KEY={deploy.DEFAULT_AWS_SECRET_KEY_SECRET}" in step


def test_aws_secret_names_can_change_and_skip(tmp_path, capsys):
    rc, out = _dry(
        tmp_path,
        capsys,
        "--aws-key-id-secret",
        "K1",
        "--aws-secret-key-secret",
        "K2",
        CULTURE_LIBRARY_S3_BUCKET="b",
    )
    step = json.loads(out.out)["steps"][-1]
    assert "AWS_ACCESS_KEY_ID=K1" in step and "AWS_SECRET_ACCESS_KEY=K2" in step
    rc, out = _dry(tmp_path, capsys, "--no-aws-credentials", CULTURE_LIBRARY_S3_BUCKET="b")
    assert "AWS_ACCESS_KEY_ID" not in out.out


@pytest.mark.parametrize("key", ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"])
def test_aws_credentials_in_env_file_are_refused(tmp_path, capsys, key):
    rc, out = _dry(tmp_path, capsys, CULTURE_LIBRARY_S3_BUCKET="b", **{key: "AKIAxxxx"})
    assert rc == 1
    assert key in json.loads(out.err)["message"]


@pytest.mark.parametrize(
    "key,val",
    [
        ("CULTURE_LIBRARY_S3_BUCKET", "<bucket>"),
        ("CULTURE_LIBRARY_S3_BUCKET", "..."),
        ("CULTURE_LIBRARY_S3_REGION", "<region>"),
        ("CULTURE_LIBRARY_S3_PREFIX", "<prefix>"),
    ],
)
def test_placeholder_s3_settings_are_refused(tmp_path, capsys, key, val):
    over = {"CULTURE_LIBRARY_S3_BUCKET": "b", key: val}
    rc, out = _dry(tmp_path, capsys, **over)
    assert rc == 1
    assert key in json.loads(out.err)["message"]


def test_missing_aws_grant_secret_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "_grant_secret_exists", lambda n, e: n != "AWS_LIB_ID")
    monkeypatch.setattr(deploy, "_which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(
        deploy, "_run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")
    )
    comp = tmp_path / "c.yaml"
    comp.write_text("services: {}\n")
    env = _env(tmp_path, CULTURE_LIBRARY_S3_BUCKET="b")
    rc = main(
        [
            "deploy",
            "restart",
            "--env-file",
            str(env),
            "--compose",
            str(comp),
            "--aws-key-id-secret",
            "AWS_LIB_ID",
            "--json",
        ]
    )
    assert rc == 2
    assert "AWS_LIB_ID" in json.loads(capsys.readouterr().err)["message"]


def test_compose_passes_s3_and_aws_env_through():
    comp = yaml.safe_load((ROOT / "deploy" / "compose.yaml").read_text())
    env = comp["services"]["library"]["environment"]
    for key in (
        "CULTURE_LIBRARY_S3_BUCKET",
        "CULTURE_LIBRARY_S3_REGION",
        "CULTURE_LIBRARY_S3_PREFIX",
        "CULTURE_LIBRARY_MIRROR_INTERVAL_S",
        "CULTURE_LIBRARY_REPODOC_MAX_BYTES",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        assert key in env, key
    assert "${AWS_ACCESS_KEY_ID:-}" in env["AWS_ACCESS_KEY_ID"]


def test_doc_has_s3_section_and_key_table():
    doc = (ROOT / "docs" / "operations" / "deploy.md").read_text()
    for needle in (
        "## S3 mirror",
        "s3:PutObject",
        "s3:GetObject",
        "s3:ListBucket",
        "deploy check-bucket",
        "mirror status",
        "mirror reconcile",
        "## Machine client keys",
        "Gwen",
        "xteink",
        "spec, plan, devague-record, design",
        "'design' kind",
        deploy.DEFAULT_AWS_KEY_ID_SECRET,
    ):
        assert needle in doc, needle
    policy = doc.split('"Action"', 1)[1].split("]", 1)[0]
    assert "Delete" not in policy
