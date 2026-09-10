"""Signed release manifest + pinned update trust root."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deepcatalog.release_trust import (
    DEFAULT_UPDATE_REPO,
    RELEASE_VERIFY_KEY_HEX,
    SIGSTORE_OIDC_ISSUER,
    SIGSTORE_WORKFLOW_IDENTITY,
    artifact_sha256,
    build_manifest,
    canonical_manifest_bytes,
    encode_signature,
    parse_manifest,
    sign_manifest,
    update_repo,
    verify_manifest_signature,
)

_REPO = Path(__file__).resolve().parent.parent
_COMMIT = "a" * 40


def _ephemeral_pair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    return private.private_bytes_raw().hex(), private.public_key().public_bytes_raw().hex()


def test_update_repo_ignores_environment_override(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_UPDATE_REPO", "evil/other-repo")
    assert update_repo() == DEFAULT_UPDATE_REPO
    assert update_repo() == "dpastoetter/DeepCatalog"


def test_embedded_verify_key_is_raw_ed25519():
    assert len(bytes.fromhex(RELEASE_VERIFY_KEY_HEX)) == 32


def test_sign_and_verify_roundtrip(monkeypatch):
    priv, pub = _ephemeral_pair()
    monkeypatch.setattr("deepcatalog.release_trust.RELEASE_VERIFY_KEY_HEX", pub)
    manifest = build_manifest(
        repo=DEFAULT_UPDATE_REPO,
        tag="v9.9.9",
        commit=_COMMIT,
        artifacts=[{"name": "deepcatalog-9.9.9.tar.gz", "sha256": "b" * 64}],
    )
    signature = encode_signature(sign_manifest(manifest, priv))
    verified = verify_manifest_signature(canonical_manifest_bytes(manifest), signature)
    assert verified["tag"] == "v9.9.9"
    assert verified["commit"] == _COMMIT
    assert artifact_sha256(verified, "deepcatalog-9.9.9.tar.gz") == "b" * 64


def test_verify_rejects_wrong_key(monkeypatch):
    priv, _pub = _ephemeral_pair()
    _other_priv, other_pub = _ephemeral_pair()
    monkeypatch.setattr("deepcatalog.release_trust.RELEASE_VERIFY_KEY_HEX", other_pub)
    manifest = build_manifest(
        repo=DEFAULT_UPDATE_REPO,
        tag="v9.9.9",
        commit=_COMMIT,
        artifacts=[{"name": "deepcatalog-9.9.9.tar.gz", "sha256": "b" * 64}],
    )
    signature = encode_signature(sign_manifest(manifest, priv))
    with pytest.raises(ValueError, match="invalid"):
        verify_manifest_signature(canonical_manifest_bytes(manifest), signature)


def test_parse_manifest_rejects_unknown_media_type():
    with pytest.raises(ValueError, match="mediaType"):
        parse_manifest(
            b'{"mediaType":"nope","repo":"a/b","tag":"v1.0.0","commit":"'
            + _COMMIT.encode()
            + b'","artifacts":[]}'
        )


def test_build_manifest_requires_commit_and_safe_names():
    with pytest.raises(ValueError, match="commit"):
        build_manifest(
            repo=DEFAULT_UPDATE_REPO,
            tag="v1.0.0",
            commit="abc",
            artifacts=[{"name": "a.tar.gz", "sha256": "b" * 64}],
        )
    with pytest.raises(ValueError, match="artifact name"):
        build_manifest(
            repo=DEFAULT_UPDATE_REPO,
            tag="v1.0.0",
            commit=_COMMIT,
            artifacts=[{"name": "../evil.tar.gz", "sha256": "b" * 64}],
        )


def test_sign_release_manifest_script(tmp_path, monkeypatch):
    priv, pub = _ephemeral_pair()
    monkeypatch.setattr("deepcatalog.release_trust.RELEASE_VERIFY_KEY_HEX", pub)
    dist = tmp_path / "dist"
    dist.mkdir()
    archive = "deepcatalog-9.9.9.tar.gz"
    (dist / "SHA256SUMS").write_text(f"{'c' * 64}  {archive}\n", encoding="utf-8")
    key_file = tmp_path / "key"
    key_file.write_text(priv, encoding="ascii")

    spec = importlib.util.spec_from_file_location(
        "sign_release_manifest",
        _REPO / "scripts" / "sign-release-manifest.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main(
        [
            str(dist),
            "--tag",
            "v9.9.9",
            "--commit",
            _COMMIT,
            "--key-file",
            str(key_file),
        ]
    )
    assert rc == 0
    manifest_bytes = (dist / "release-manifest.json").read_bytes()
    sig = (dist / "release-manifest.json.sig").read_text(encoding="ascii")
    verified = verify_manifest_signature(manifest_bytes, sig, public_key_hex=pub)
    assert verified["commit"] == _COMMIT
    assert artifact_sha256(verified, archive) == "c" * 64


def test_release_workflow_signs_and_attests():
    text = (_REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "DEEPCATALOG_RELEASE_SIGNING_KEY" in text
    assert "scripts/sign-release-manifest.py" in text
    assert "actions/attest-build-provenance@96278af6caaf10aea03fd8d33a09a777ca52d62f" in text
    assert "id-token: write" in text
    assert "attestations: write" in text
    assert SIGSTORE_WORKFLOW_IDENTITY in text
    assert SIGSTORE_OIDC_ISSUER in text
    assert "release-manifest.json.sig" in text
    assert "dist/install.sh" in text
    assert "dist/install.ps1" in text
    assert "dist/SHA256SUMS" in text
    assert "official-release" in text
    assert "gh release delete-asset" in text
    assert "GITHUB_REF" in text
    assert "already has an AppImage" not in text
    assert "PACK=false" not in text
    assert "outputs.pack" not in text
    assert "needs.context.outputs.pack" not in text


def test_appimage_workflow_does_not_publish_github_release():
    text = (_REPO / ".github" / "workflows" / "appimage.yml").read_text(encoding="utf-8")
    assert "contents: write" not in text
    assert "gh release" not in text
    assert "permissions:\n  contents: read" in text
