"""Tests for the GitHub self-update mechanism (no network)."""

from __future__ import annotations

import hashlib
import io
import tarfile

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import deepcatalog
from app.main import app
from deepcatalog.release_trust import (
    MANIFEST_NAME,
    MANIFEST_SIG_NAME,
    build_manifest,
    canonical_manifest_bytes,
    encode_signature,
    sign_manifest,
)
from deepcatalog.updater import (
    _github_get,
    _pick_appimage_asset,
    apply_tarball,
    apply_update,
    check_for_update,
    is_newer,
    parse_sha256sums,
    parse_version,
    sha256_hex,
    verify_sha256,
)
from deepcatalog.version import clear_version_cache
from deepcatalog.version import get_current_version as read_version


def test_parse_and_compare_versions():
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("0.1.0") == (0, 1, 0)
    assert parse_version("garbage") == (0,)

    assert is_newer("v0.2.0", "0.1.0")
    assert is_newer("1.0.0", "0.9.9")
    assert not is_newer("0.1.0", "0.1.0")
    assert not is_newer("v0.0.9", "0.1.0")


def test_get_current_version_reads_pyproject():
    clear_version_cache()
    version = read_version()
    assert parse_version(version) > (0,) or version == "0.1.0"
    # FastAPI OpenAPI metadata must track the same version resolution path.
    assert app.version == version
    assert deepcatalog.__version__ == version


def test_parse_sha256sums_indexes_basename():
    text = (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  "
        "dist/deepcatalog-1.0.0.tar.gz\n"
        "# comment\n"
    )
    mapping = parse_sha256sums(text)
    assert (
        mapping["deepcatalog-1.0.0.tar.gz"]
        == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )


def test_verify_sha256_accepts_match_and_rejects_mismatch():
    payload = b"hello-update"
    digest = sha256_hex(payload)
    verify_sha256(payload, digest)
    with pytest.raises(ValueError, match="mismatch"):
        verify_sha256(payload, "0" * 64)


def test_github_get_retries_remote_protocol_error(monkeypatch):
    calls = {"n": 0}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    def fake_get(_self, _url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return FakeResponse()

    monkeypatch.setattr("time.sleep", lambda _s: None)
    with httpx.Client() as client:
        monkeypatch.setattr(client, "get", fake_get.__get__(client, httpx.Client))
        resp = _github_get(client, "https://api.github.com/example")
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_github_get_retries_http_503(monkeypatch):
    calls = {"n": 0}

    class FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.headers = {}

        @staticmethod
        def json():
            return {}

    def fake_get(_self, _url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(503)
        return FakeResponse(200)

    monkeypatch.setattr(__import__("time"), "sleep", lambda _s: None)
    with httpx.Client() as client:
        monkeypatch.setattr(client, "get", fake_get.__get__(client, httpx.Client))
        resp = _github_get(client, "https://api.github.com/example")
    assert resp.status_code == 200
    assert calls["n"] == 2


def _make_tarball(files: dict[str, bytes], root: str = "owner-repo-abc123") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=f"{root}/{name}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.fixture()
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setattr("deepcatalog.config.PROJECT_ROOT", tmp_path)
    return tmp_path


def test_apply_tarball_updates_code_but_protects_user_data(isolated_root):
    # Existing local state that must survive an update.
    (isolated_root / "data").mkdir()
    (isolated_root / "data" / "deepcatalog.db").write_bytes(b"precious")
    (isolated_root / ".env").write_text("OPENAI_API_KEY=secret\n")
    (isolated_root / "app").mkdir()
    (isolated_root / "app" / "main.py").write_text("old code\n")

    tarball = _make_tarball(
        {
            "app/main.py": b"new code\n",
            "deepcatalog/new_module.py": b"print('hi')\n",
            "pyproject.toml": b'[project]\nversion = "0.2.0"\n',
            "data/deepcatalog.db": b"attacker data",
            ".env": b"OPENAI_API_KEY=evil",
        }
    )

    result = apply_tarball(tarball)
    assert result["status"] == "success"
    assert result["updated_count"] == 3

    assert (isolated_root / "app" / "main.py").read_text() == "new code\n"
    assert (isolated_root / "deepcatalog" / "new_module.py").exists()
    # Protected paths untouched.
    assert (isolated_root / "data" / "deepcatalog.db").read_bytes() == b"precious"
    assert "secret" in (isolated_root / ".env").read_text()


def test_apply_tarball_prunes_obsolete_release_files(isolated_root):
    (isolated_root / "app").mkdir()
    (isolated_root / "app" / "main.py").write_text("old\n")
    (isolated_root / "legacy.py").write_text("stale\n")
    (isolated_root / ".release-files").write_text("app/main.py\nlegacy.py\n")
    (isolated_root / "data").mkdir()
    (isolated_root / "data" / "keep.db").write_bytes(b"db")

    tarball = _make_tarball(
        {
            "app/main.py": b"new\n",
            ".release-files": b"app/main.py\n",
            ".release-commit": b"tag=v9.9.9\n",
        }
    )
    result = apply_tarball(tarball)
    assert result["status"] == "success"
    assert result["removed_count"] == 1
    assert "legacy.py" in result["removed"]
    assert not (isolated_root / "legacy.py").exists()
    assert (isolated_root / "app" / "main.py").read_text() == "new\n"
    assert (isolated_root / "data" / "keep.db").read_bytes() == b"db"
    assert (isolated_root / ".release-files").read_text() == "app/main.py\n"


def test_apply_tarball_rejects_unexpected_layout(isolated_root):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="loose-file.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"xx"))
    result = apply_tarball(buffer.getvalue())
    assert result["status"] == "error"


def test_apply_tarball_rejects_commit_mismatch(isolated_root):
    tarball = _make_tarball({"pyproject.toml": b"version=1\n"}, root="owner-repo-deadbeef")
    result = apply_tarball(
        tarball,
        commit_sha="c" * 40,
        expect_commit_match=True,
    )
    assert result["status"] == "error"
    assert "does not match release commit" in result["error"]


def test_apply_update_refuses_when_up_to_date(monkeypatch):
    monkeypatch.setattr(
        "deepcatalog.updater.check_for_update",
        lambda: {
            "status": "success",
            "current_version": "0.1.0",
            "latest_version": "0.1.0",
            "download_url": "https://example.invalid/tarball",
            "expected_sha256": "a" * 64,
            "verifiable": True,
            "update_available": False,
        },
    )
    result = apply_update()
    assert result["status"] == "error"
    assert "up to date" in result["error"]


def test_apply_update_refuses_unverified_release(monkeypatch):
    monkeypatch.setattr(
        "deepcatalog.updater.check_for_update",
        lambda: {
            "status": "success",
            "current_version": "0.1.0",
            "latest_version": "9.9.9",
            "update_available": True,
            "verifiable": False,
            "signed": False,
            "verification_error": "missing SHA-256",
        },
    )
    result = apply_update()
    assert result["status"] == "error"
    assert "missing SHA-256" in result["error"]


def test_apply_update_refuses_checksum_mismatch(isolated_root, monkeypatch):
    tarball = _make_tarball({"pyproject.toml": b'[project]\nversion = "9.9.9"\n'})
    monkeypatch.setattr(
        "deepcatalog.updater.check_for_update",
        lambda: {
            "status": "success",
            "current_version": "0.1.0",
            "latest_version": "9.9.9",
            "update_available": True,
            "verifiable": True,
            "signed": True,
            "manifest_commit": "a" * 40,
            "download_url": "https://example.invalid/deepcatalog-9.9.9.tar.gz",
            "expected_sha256": "0" * 64,
            "artifact_name": "deepcatalog-9.9.9.tar.gz",
        },
    )
    monkeypatch.setattr("deepcatalog.updater._download_bytes", lambda _url: tarball)
    result = apply_update()
    assert result["status"] == "error"
    assert "mismatch" in result["error"].lower()
    assert not (isolated_root / "pyproject.toml").exists()


def test_apply_update_refuses_unsigned_even_with_checksum(monkeypatch):
    monkeypatch.setattr(
        "deepcatalog.updater.check_for_update",
        lambda: {
            "status": "success",
            "current_version": "0.1.0",
            "latest_version": "9.9.9",
            "update_available": True,
            "verifiable": True,
            "signed": False,
            "download_url": "https://example.invalid/deepcatalog-9.9.9.tar.gz",
            "expected_sha256": "a" * 64,
            "verification_error": None,
        },
    )
    result = apply_update()
    assert result["status"] == "error"
    assert "unsigned" in result["error"].lower()


def test_apply_update_installs_verified_release(isolated_root, monkeypatch):
    commit = "a" * 40
    tarball = _make_tarball(
        {
            "pyproject.toml": b'[project]\nversion = "9.9.9"\n',
            ".release-commit": f"commit={commit}\n".encode(),
        },
        root="deepcatalog-9.9.9",
    )
    digest = hashlib.sha256(tarball).hexdigest()
    monkeypatch.setattr(
        "deepcatalog.updater.check_for_update",
        lambda: {
            "status": "success",
            "current_version": "0.1.0",
            "latest_version": "9.9.9",
            "update_available": True,
            "verifiable": True,
            "signed": True,
            "manifest_commit": commit,
            "download_url": (
                "https://github.com/dpastoetter/DeepCatalog/releases/download/"
                "v9.9.9/deepcatalog-9.9.9.tar.gz"
            ),
            "expected_sha256": digest,
            "artifact_name": "deepcatalog-9.9.9.tar.gz",
            "commit_sha": commit,
        },
    )
    monkeypatch.setattr("deepcatalog.updater._download_bytes", lambda _url: tarball)
    result = apply_update()
    assert result["status"] == "success"
    assert result["restart_required"] is True
    assert result["installed_version"] == "9.9.9"
    assert result["verified_sha256"] == digest
    assert result["verified_commit"] == commit
    assert (isolated_root / "pyproject.toml").exists()


def test_apply_tarball_rejects_signed_commit_mismatch(isolated_root):
    tarball = _make_tarball(
        {
            "pyproject.toml": b"version=1\n",
            ".release-commit": b"commit=" + (b"a" * 40) + b"\n",
        }
    )
    result = apply_tarball(tarball, expected_release_commit="b" * 40)
    assert result["status"] == "error"
    assert "release-commit" in result["error"]


def test_fetch_unsigned_release_is_not_verifiable(monkeypatch):

    class Fake:
        def __init__(self, status_code, json_data=None, content=b"", text=""):
            self.status_code = status_code
            self._json = json_data
            self.content = content
            self.text = text
            self.is_success = 200 <= status_code < 300
            self.headers = {}

        def json(self):
            return self._json

        def raise_for_status(self):
            if not self.is_success:
                raise httpx.HTTPStatusError(
                    "err",
                    request=httpx.Request("GET", "https://x"),
                    response=httpx.Response(self.status_code),
                )

    def handler(_client, url, *, headers=None, params=None):
        if url.endswith("/releases/latest"):
            return Fake(
                200,
                {
                    "tag_name": "v9.9.9",
                    "name": "v9.9.9",
                    "body": "",
                    "published_at": None,
                    "html_url": "https://github.com/dpastoetter/DeepCatalog/releases/tag/v9.9.9",
                    "tarball_url": "https://api.github.com/repos/dpastoetter/DeepCatalog/tarball/v9.9.9",
                    "assets": [
                        {
                            "name": "deepcatalog-9.9.9.tar.gz",
                            "browser_download_url": "https://example.invalid/deepcatalog-9.9.9.tar.gz",
                        },
                        {
                            "name": "SHA256SUMS",
                            "browser_download_url": "https://example.invalid/SHA256SUMS",
                        },
                    ],
                },
            )
        if "/commits/" in url:
            return Fake(200, {"sha": "a" * 40})
        if url.endswith("/SHA256SUMS"):
            return Fake(200, text=("b" * 64) + "  deepcatalog-9.9.9.tar.gz\n")
        return Fake(404)

    monkeypatch.setattr("deepcatalog.updater._github_get", handler)
    info = check_for_update()
    assert info["status"] == "success"
    assert info["signed"] is False
    assert info["verifiable"] is False
    assert "signed" in (info.get("verification_error") or "").lower()


def test_fetch_signed_release_is_installable(monkeypatch):
    private = Ed25519PrivateKey.generate()
    pub = private.public_key().public_bytes_raw().hex()
    priv = private.private_bytes_raw().hex()
    monkeypatch.setattr("deepcatalog.release_trust.RELEASE_VERIFY_KEY_HEX", pub)
    commit = "a" * 40
    digest = "b" * 64
    manifest = build_manifest(
        repo="dpastoetter/DeepCatalog",
        tag="v9.9.9",
        commit=commit,
        artifacts=[{"name": "deepcatalog-9.9.9.tar.gz", "sha256": digest}],
    )
    manifest_bytes = canonical_manifest_bytes(manifest)
    sig_text = encode_signature(sign_manifest(manifest, priv))

    class Fake:
        def __init__(self, status_code, json_data=None, content=b"", text=""):
            self.status_code = status_code
            self._json = json_data
            self.content = content
            self.text = text
            self.is_success = 200 <= status_code < 300
            self.headers = {}

        def json(self):
            return self._json

        def raise_for_status(self):
            if not self.is_success:
                raise httpx.HTTPStatusError(
                    "err",
                    request=httpx.Request("GET", "https://x"),
                    response=httpx.Response(self.status_code),
                )

    def handler(_client, url, *, headers=None, params=None):
        if url.endswith("/releases/latest"):
            return Fake(
                200,
                {
                    "tag_name": "v9.9.9",
                    "name": "v9.9.9",
                    "body": "notes",
                    "published_at": "2026-01-01T00:00:00Z",
                    "html_url": "https://github.com/dpastoetter/DeepCatalog/releases/tag/v9.9.9",
                    "tarball_url": "https://api.github.com/repos/dpastoetter/DeepCatalog/tarball/v9.9.9",
                    "assets": [
                        {
                            "name": "deepcatalog-9.9.9.tar.gz",
                            "browser_download_url": "https://example.invalid/deepcatalog-9.9.9.tar.gz",
                        },
                        {
                            "name": MANIFEST_NAME,
                            "browser_download_url": "https://example.invalid/release-manifest.json",
                        },
                        {
                            "name": MANIFEST_SIG_NAME,
                            "browser_download_url": "https://example.invalid/release-manifest.json.sig",
                        },
                    ],
                },
            )
        if "/commits/" in url:
            return Fake(200, {"sha": commit})
        if url.endswith("/release-manifest.json"):
            return Fake(200, content=manifest_bytes)
        if url.endswith("/release-manifest.json.sig"):
            return Fake(200, content=sig_text.encode("ascii"))
        return Fake(404)

    monkeypatch.setattr("deepcatalog.updater._github_get", handler)
    monkeypatch.setenv("DEEPCATALOG_UPDATE_REPO", "evil/other")
    info = check_for_update()
    assert info["status"] == "success"
    assert info["repo"] == "dpastoetter/DeepCatalog"
    assert info["signed"] is True
    assert info["verifiable"] is True
    assert info["expected_sha256"] == digest
    assert info["manifest_commit"] == commit
    assert info["artifact_name"] == "deepcatalog-9.9.9.tar.gz"


def test_fetch_rejects_manifest_commit_mismatch(monkeypatch):
    private = Ed25519PrivateKey.generate()
    pub = private.public_key().public_bytes_raw().hex()
    priv = private.private_bytes_raw().hex()
    monkeypatch.setattr("deepcatalog.release_trust.RELEASE_VERIFY_KEY_HEX", pub)
    manifest = build_manifest(
        repo="dpastoetter/DeepCatalog",
        tag="v9.9.9",
        commit="a" * 40,
        artifacts=[{"name": "deepcatalog-9.9.9.tar.gz", "sha256": "b" * 64}],
    )
    manifest_bytes = canonical_manifest_bytes(manifest)
    sig_text = encode_signature(sign_manifest(manifest, priv))

    class Fake:
        def __init__(self, status_code, json_data=None, content=b"", text=""):
            self.status_code = status_code
            self._json = json_data
            self.content = content
            self.text = text
            self.is_success = 200 <= status_code < 300
            self.headers = {}

        def json(self):
            return self._json

        def raise_for_status(self):
            if not self.is_success:
                raise httpx.HTTPStatusError(
                    "err",
                    request=httpx.Request("GET", "https://x"),
                    response=httpx.Response(self.status_code),
                )

    def handler(_client, url, *, headers=None, params=None):
        if url.endswith("/releases/latest"):
            return Fake(
                200,
                {
                    "tag_name": "v9.9.9",
                    "name": "v9.9.9",
                    "body": "",
                    "published_at": None,
                    "html_url": "https://github.com/dpastoetter/DeepCatalog/releases/tag/v9.9.9",
                    "tarball_url": "https://api.github.com/repos/dpastoetter/DeepCatalog/tarball/v9.9.9",
                    "assets": [
                        {
                            "name": "deepcatalog-9.9.9.tar.gz",
                            "browser_download_url": "https://example.invalid/deepcatalog-9.9.9.tar.gz",
                        },
                        {
                            "name": MANIFEST_NAME,
                            "browser_download_url": "https://example.invalid/release-manifest.json",
                        },
                        {
                            "name": MANIFEST_SIG_NAME,
                            "browser_download_url": "https://example.invalid/release-manifest.json.sig",
                        },
                    ],
                },
            )
        if "/commits/" in url:
            return Fake(200, {"sha": "c" * 40})
        if url.endswith("/release-manifest.json"):
            return Fake(200, content=manifest_bytes)
        if url.endswith("/release-manifest.json.sig"):
            return Fake(200, content=sig_text.encode("ascii"))
        return Fake(404)

    monkeypatch.setattr("deepcatalog.updater._github_get", handler)
    info = check_for_update()
    assert info["signed"] is False
    assert info["verifiable"] is False
    assert "commit" in (info.get("verification_error") or "").lower()


def test_pick_appimage_prefers_x86_64():
    chosen = _pick_appimage_asset(
        [
            {"name": "DeepCatalog-1.0.0-aarch64.AppImage"},
            {"name": "DeepCatalog-1.0.0-x86_64.AppImage"},
        ]
    )
    assert chosen is not None
    assert chosen["name"].endswith("x86_64.AppImage")


def test_apply_update_refuses_appimage(monkeypatch):
    monkeypatch.setattr("deepcatalog.updater.running_as_appimage", lambda: True)
    result = apply_update()
    assert result["status"] == "error"
    assert result["installable"] is False
    assert "AppImage" in result["error"]
