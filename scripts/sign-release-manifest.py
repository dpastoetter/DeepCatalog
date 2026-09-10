#!/usr/bin/env python3
"""Sign dist/SHA256SUMS into a canonical release-manifest.json + .sig.

Usage (CI):
  DEEPCATALOG_RELEASE_SIGNING_KEY=<64-hex> \\
    python scripts/sign-release-manifest.py dist --tag v0.5.1 --commit <sha>

The private key is a separately protected Actions secret — not GITHUB_TOKEN.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepcatalog.release_trust import (  # noqa: E402
    DEFAULT_UPDATE_REPO,
    MANIFEST_NAME,
    MANIFEST_SIG_NAME,
    artifact_sha256,
    build_manifest,
    canonical_manifest_bytes,
    encode_signature,
    sign_manifest,
)

_SHA256_LINE_RE = re.compile(r"^\s*([A-Fa-f0-9]{64})\s+\*?(.+?)\s*$")


def parse_sha256sums(text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SHA256_LINE_RE.match(line)
        if not match:
            continue
        digest, filename = match.group(1).lower(), match.group(2).strip()
        mapping[filename] = digest
        mapping[Path(filename).name] = digest
    return mapping


def _read_key(path: Path | None) -> str:
    env = os.getenv("DEEPCATALOG_RELEASE_SIGNING_KEY", "").strip()
    if env:
        return env
    if path is not None and path.is_file():
        return path.read_text(encoding="ascii").strip()
    raise SystemExit(
        "Set DEEPCATALOG_RELEASE_SIGNING_KEY (64-char hex) or pass --key-file. "
        "This must not be GITHUB_TOKEN."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path, help="Directory containing SHA256SUMS")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repo", default=DEFAULT_UPDATE_REPO)
    parser.add_argument(
        "--key-file",
        type=Path,
        default=None,
        help="Optional file with 64-char hex private key (gitignored)",
    )
    args = parser.parse_args(argv)

    dist = args.dist.resolve()
    sums_path = dist / "SHA256SUMS"
    if not sums_path.is_file():
        raise SystemExit(f"missing {sums_path}")
    mapping = parse_sha256sums(sums_path.read_text(encoding="utf-8"))
    artifacts: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, digest in mapping.items():
        base = Path(name).name
        if base in seen:
            continue
        seen.add(base)
        artifacts.append({"name": base, "sha256": digest})
    if not artifacts:
        raise SystemExit("SHA256SUMS contained no artifacts")

    manifest = build_manifest(
        repo=args.repo,
        tag=args.tag,
        commit=args.commit,
        artifacts=artifacts,
    )
    key = _read_key(args.key_file)
    signature = sign_manifest(manifest, key)
    (dist / MANIFEST_NAME).write_bytes(canonical_manifest_bytes(manifest) + b"\n")
    (dist / MANIFEST_SIG_NAME).write_text(encode_signature(signature) + "\n", encoding="ascii")

    archive_name = f"deepcatalog-{args.tag.lstrip('v')}.tar.gz"
    if artifact_sha256(manifest, archive_name) is None:
        raise SystemExit(f"manifest is missing required archive {archive_name}")

    print(f"Wrote {dist / MANIFEST_NAME}")
    print(f"Wrote {dist / MANIFEST_SIG_NAME}")
    print(f"artifacts={len(manifest['artifacts'])} commit={manifest['commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
