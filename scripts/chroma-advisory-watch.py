"""Unsuppressed Chroma/OSV watch: fail when an upgrade or new advisory is available.

CI's dependency-audit.sh ignores Chroma HTTP-server RCEs (CVE-2026-45829 /
CVE-2026-45833) because DeepCatalog only uses embedded PersistentClient.
This script runs pip-audit *without* those ignores and checks PyPI so a
patched chromadb release cannot sit unnoticed.

Exit 0: only known chromadb server advisories remain, and no newer PyPI version.
Exit 1: upgrade chromadb, or a non-Chroma advisory appeared.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parent.parent
CONSTRAINTS_PATH = ROOT / "constraints.txt"
AUDIT_SCRIPT_PATH = ROOT / "scripts" / "dependency-audit.sh"
CHROMA_PACKAGES = frozenset({"chromadb", "chroma-hnswlib"})
PYPI_CHROMADB = "https://pypi.org/pypi/chromadb/json"
_PIN_RE = re.compile(r"^chromadb==([0-9][^\s#]+)", re.MULTILINE)
_CHROMA_IGNORE_FLAGS = ("PYSEC-2026-311", "GHSA-f4j7-r4q5-qw2c")


def pinned_chromadb_version(text: str) -> str:
    match = _PIN_RE.search(text)
    if not match:
        raise SystemExit(f"could not find chromadb== pin in {CONSTRAINTS_PATH}")
    return match.group(1).strip()


def pypi_latest_chromadb(url: str = PYPI_CHROMADB, *, timeout: float = 30.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — fixed PyPI URL
        payload = json.loads(resp.read().decode("utf-8"))
    version = str(payload.get("info", {}).get("version") or "").strip()
    if not version:
        raise SystemExit("PyPI chromadb response did not include info.version")
    return version


def iter_audit_findings(report: dict[str, Any]) -> list[dict[str, str]]:
    """Flatten pip-audit JSON into {package, version, vuln_id} rows."""
    rows: list[dict[str, str]] = []
    deps = report.get("dependencies")
    if not isinstance(deps, list):
        return rows
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name") or "").strip().lower()
        version = str(dep.get("version") or "").strip()
        vulns = dep.get("vulns") or dep.get("vulnerabilities") or []
        if not isinstance(vulns, list):
            continue
        for vuln in vulns:
            if not isinstance(vuln, dict):
                continue
            aliases = vuln.get("aliases") or []
            alias_list = [str(a) for a in aliases] if isinstance(aliases, list) else []
            vuln_id = str(vuln.get("id") or "").strip()
            ids = [vuln_id, *alias_list]
            for item in ids:
                if item:
                    rows.append({"package": name, "version": version, "vuln_id": item})
    return rows


def audit_script_ignores_chroma(text: str) -> bool:
    return any(f"--ignore-vuln {vuln_id}" in text for vuln_id in _CHROMA_IGNORE_FLAGS)


def classify_findings(
    findings: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    chroma: list[dict[str, str]] = []
    other: list[dict[str, str]] = []
    for row in findings:
        if row["package"] in CHROMA_PACKAGES:
            chroma.append(row)
        else:
            other.append(row)
    return chroma, other


def evaluate(
    *,
    pinned: str,
    latest: str,
    findings: list[dict[str, str]],
    ignores_chroma: bool,
) -> list[str]:
    """Return human-readable failure reasons (empty means pass)."""
    reasons: list[str] = []
    chroma, other = classify_findings(findings)
    if other:
        ids = ", ".join(sorted({row["vuln_id"] for row in other}))
        reasons.append(f"unsuppressed non-Chroma advisories: {ids}")
    try:
        if Version(latest) > Version(pinned):
            reasons.append(
                f"chromadb {latest} is on PyPI (constraints pin {pinned}). "
                "Upgrade, drop scripts/dependency-audit.sh ignore-vuln flags if the "
                "HTTP-server RCEs are fixed, and re-run tests."
            )
    except InvalidVersion:
        reasons.append(f"uncomparable chromadb versions pinned={pinned!r} latest={latest!r}")
    if not chroma and ignores_chroma:
        reasons.append(
            "pip-audit no longer reports chromadb advisories on the pinned version; "
            "remove --ignore-vuln PYSEC-2026-311 / GHSA-f4j7-r4q5-qw2c from "
            "scripts/dependency-audit.sh."
        )
    return reasons


def run_pip_audit() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "pip_audit", "--progress-spinner", "off", "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    raw = proc.stdout.strip() or proc.stderr.strip()
    try:
        report = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"pip-audit did not return JSON (exit {proc.returncode}): {raw[:500]}"
        ) from exc
    if not isinstance(report, dict):
        raise SystemExit("pip-audit JSON was not an object")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit-json",
        type=Path,
        default=None,
        help="Use a saved pip-audit JSON file instead of invoking pip-audit",
    )
    parser.add_argument(
        "--latest",
        default=None,
        help="Override PyPI latest chromadb version (tests)",
    )
    args = parser.parse_args(argv)

    pinned = pinned_chromadb_version(CONSTRAINTS_PATH.read_text(encoding="utf-8"))
    if args.audit_json is not None:
        report = json.loads(args.audit_json.read_text(encoding="utf-8"))
    else:
        report = run_pip_audit()
    findings = iter_audit_findings(report)
    latest = args.latest if args.latest is not None else pypi_latest_chromadb()
    ignores_chroma = audit_script_ignores_chroma(AUDIT_SCRIPT_PATH.read_text(encoding="utf-8"))
    reasons = evaluate(
        pinned=pinned,
        latest=latest,
        findings=findings,
        ignores_chroma=ignores_chroma,
    )
    chroma, _other = classify_findings(findings)
    print(f"pinned chromadb={pinned} pypi_latest={latest}")
    print(f"chromadb advisory ids: {sorted({row['vuln_id'] for row in chroma}) or '(none)'}")
    if reasons:
        print("chroma advisory watch FAILED:", file=sys.stderr)
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    print(
        "No newer chromadb on PyPI; remaining advisories are Chroma HTTP-server "
        "RCEs (CVE-2026-45829 / CVE-2026-45833) deferred because DeepCatalog uses "
        "embedded PersistentClient only."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
