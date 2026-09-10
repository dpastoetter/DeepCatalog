"""Release provenance: signed manifest + pinned GitHub repo trust root.

SHA-256 checksums on a GitHub Release are not authentic: whoever can upload
assets can upload matching sums. The in-app updater therefore requires an
Ed25519 signature over a canonical manifest (tag, commit, artifact hashes)
using a key that is *not* GITHUB_TOKEN. SLSA / Sigstore attestations are
produced in CI for independent ``gh attestation verify`` checks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Trust root for in-app updates. Forks must change this constant (and the
# verify key). DEEPCATALOG_UPDATE_REPO is ignored so a local env change cannot
# redirect production builds.
DEFAULT_UPDATE_REPO = "dpastoetter/DeepCatalog"

MANIFEST_MEDIA_TYPE = "application/vnd.deepcatalog.release.manifest.v1+json"
MANIFEST_NAME = "release-manifest.json"
MANIFEST_SIG_NAME = "release-manifest.json.sig"

# Raw 32-byte Ed25519 public key (hex). Private key is GitHub Actions secret
# DEEPCATALOG_RELEASE_SIGNING_KEY — never GITHUB_TOKEN.
RELEASE_VERIFY_KEY_HEX = "5334cfc14c39e6e44f658109ba6bfa42c00ad6a340a8cf796337a6649c5c8b60"

# Sigstore / GitHub artifact attestations (CI). Used by gh attestation verify.
SIGSTORE_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SIGSTORE_WORKFLOW_IDENTITY = (
    "https://github.com/dpastoetter/DeepCatalog/.github/workflows/release.yml"
)

_COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TAG_RE = re.compile(r"^v[0-9]")


def update_repo() -> str:
    """Repo the updater will query. Production builds cannot override this."""
    return DEFAULT_UPDATE_REPO


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Deterministic JSON used as the Ed25519 message."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def build_manifest(
    *,
    repo: str,
    tag: str,
    commit: str,
    artifacts: list[dict[str, str]],
) -> dict[str, Any]:
    """Assemble a v1 manifest. Artifact list is sorted by name."""
    cleaned: list[dict[str, str]] = []
    for item in artifacts:
        name = str(item.get("name") or "").strip()
        digest = str(item.get("sha256") or "").strip().lower()
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError(f"invalid manifest artifact name: {name!r}")
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError(f"invalid manifest artifact digest: {name!r}")
        cleaned.append({"name": name, "sha256": digest})
    cleaned.sort(key=lambda row: row["name"])
    commit_l = commit.strip().lower()
    if not _COMMIT_RE.fullmatch(commit_l):
        raise ValueError("manifest commit must be a 40-character lowercase SHA")
    repo_s = repo.strip()
    if not _REPO_RE.fullmatch(repo_s):
        raise ValueError("invalid manifest repo")
    tag_s = tag.strip()
    if not _TAG_RE.match(tag_s):
        raise ValueError("manifest tag must look like v0.1.0")
    return {
        "mediaType": MANIFEST_MEDIA_TYPE,
        "repo": repo_s,
        "tag": tag_s,
        "commit": commit_l,
        "artifacts": cleaned,
    }


def parse_manifest(data: bytes) -> dict[str, Any]:
    """Parse and validate a v1 manifest (unknown keys dropped)."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("release manifest is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("release manifest must be a JSON object")
    if raw.get("mediaType") != MANIFEST_MEDIA_TYPE:
        raise ValueError("unsupported release manifest mediaType")
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("release manifest artifacts must be a list")
    normalized: list[dict[str, str]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError("release manifest artifact must be an object")
        normalized.append(
            {
                "name": str(item.get("name") or ""),
                "sha256": str(item.get("sha256") or ""),
            }
        )
    return build_manifest(
        repo=str(raw.get("repo") or ""),
        tag=str(raw.get("tag") or ""),
        commit=str(raw.get("commit") or ""),
        artifacts=normalized,
    )


def load_verify_key(*, public_key_hex: str | None = None) -> Ed25519PublicKey:
    raw = (public_key_hex or RELEASE_VERIFY_KEY_HEX).strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", raw):
        raise ValueError("invalid Ed25519 public key")
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw))


def load_signing_key(private_key_hex: str) -> Ed25519PrivateKey:
    raw = (private_key_hex or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", raw):
        raise ValueError("invalid Ed25519 private key")
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw))


def sign_manifest(manifest: dict[str, Any], private_key_hex: str) -> bytes:
    """Return the raw 64-byte Ed25519 signature over the canonical manifest."""
    key = load_signing_key(private_key_hex)
    return key.sign(canonical_manifest_bytes(manifest))


def encode_signature(signature: bytes) -> str:
    if len(signature) != 64:
        raise ValueError("Ed25519 signature must be 64 bytes")
    return signature.hex()


def decode_signature(text: str) -> bytes:
    raw = (text or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{128}", raw):
        raise ValueError("release manifest signature is not a 64-byte hex digest")
    return bytes.fromhex(raw)


def verify_manifest_signature(
    manifest_bytes: bytes,
    signature_text: str,
    *,
    public_key_hex: str | None = None,
) -> dict[str, Any]:
    """Validate signature then schema. Raises ValueError on failure."""
    manifest = parse_manifest(manifest_bytes)
    message = canonical_manifest_bytes(manifest)
    signature = decode_signature(signature_text)
    key = load_verify_key(public_key_hex=public_key_hex)
    try:
        key.verify(signature, message)
    except InvalidSignature as exc:
        raise ValueError("release manifest signature is invalid") from exc
    return manifest


def artifact_sha256(manifest: dict[str, Any], filename: str) -> str | None:
    want = Path(filename).name
    for item in manifest.get("artifacts") or []:
        name = Path(str(item.get("name") or "")).name
        if name != want:
            continue
        digest = str(item.get("sha256") or "")
        return digest if _SHA256_RE.fullmatch(digest) else None
    return None
