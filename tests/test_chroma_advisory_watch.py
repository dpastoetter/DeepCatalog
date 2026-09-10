"""Scheduled unsuppressed Chroma advisory watch (does not ignore CVE-2026-45829)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _watch():
    spec = importlib.util.spec_from_file_location(
        "chroma_advisory_watch",
        _REPO / "scripts" / "chroma-advisory-watch.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_watch_script_has_no_ignore_vuln_flags():
    text = (_REPO / "scripts" / "chroma-advisory-watch.py").read_text(encoding="utf-8")
    assert '"--ignore-vuln"' not in text
    assert "'--ignore-vuln'" not in text
    workflow = (_REPO / ".github" / "workflows" / "advisory-watch.yml").read_text(encoding="utf-8")
    assert "--ignore-vuln" not in workflow
    assert "pull_request" not in workflow


def test_evaluate_defers_only_chromadb_while_pin_is_latest():
    watch = _watch()
    findings = [
        {"package": "chromadb", "version": "1.5.9", "vuln_id": "PYSEC-2026-311"},
        {"package": "chromadb", "version": "1.5.9", "vuln_id": "CVE-2026-45829"},
    ]
    assert (
        watch.evaluate(
            pinned="1.5.9",
            latest="1.5.9",
            findings=findings,
            ignores_chroma=True,
        )
        == []
    )


def test_evaluate_fails_when_newer_chromadb_is_on_pypi():
    watch = _watch()
    findings = [{"package": "chromadb", "version": "1.5.9", "vuln_id": "PYSEC-2026-311"}]
    reasons = watch.evaluate(
        pinned="1.5.9",
        latest="1.5.10",
        findings=findings,
        ignores_chroma=True,
    )
    assert any("1.5.10" in reason for reason in reasons)


def test_evaluate_fails_on_non_chroma_advisory():
    watch = _watch()
    findings = [
        {"package": "chromadb", "version": "1.5.9", "vuln_id": "PYSEC-2026-311"},
        {"package": "requests", "version": "2.0.0", "vuln_id": "CVE-2099-1"},
    ]
    reasons = watch.evaluate(
        pinned="1.5.9",
        latest="1.5.9",
        findings=findings,
        ignores_chroma=True,
    )
    assert any("CVE-2099-1" in reason for reason in reasons)


def test_evaluate_fails_when_chroma_advisories_are_gone_but_ignores_remain():
    watch = _watch()
    reasons = watch.evaluate(
        pinned="1.5.9",
        latest="1.5.9",
        findings=[],
        ignores_chroma=True,
    )
    assert any("ignore-vuln" in reason for reason in reasons)
    assert (
        watch.evaluate(
            pinned="1.5.10",
            latest="1.5.10",
            findings=[],
            ignores_chroma=False,
        )
        == []
    )


def test_main_with_fixture_audit_json(tmp_path):
    watch = _watch()
    report = {
        "dependencies": [
            {
                "name": "chromadb",
                "version": "1.5.9",
                "vulns": [
                    {
                        "id": "PYSEC-2026-311",
                        "aliases": ["CVE-2026-45829", "GHSA-f4j7-r4q5-qw2c"],
                    }
                ],
            }
        ]
    }
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    rc = watch.main(["--audit-json", str(path), "--latest", "1.5.9"])
    assert rc == 0
    assert watch.main(["--audit-json", str(path), "--latest", "1.5.10"]) == 1
