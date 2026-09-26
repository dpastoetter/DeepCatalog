#!/usr/bin/env python3
"""Enforce review expiry for Chroma pip-audit suppressions.

Reads scripts/chroma_vuln_suppressions.json (rationale + expires dates) and fails
when any suppression is past its review expiry. Optionally emits pip-audit
``--ignore-vuln`` flags so dependency-audit.sh stays in sync with the JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPPRESSIONS_PATH = ROOT / "scripts" / "chroma_vuln_suppressions.json"
AUDIT_SCRIPT_PATH = ROOT / "scripts" / "dependency-audit.sh"


def load_suppressions(path: Path = SUPPRESSIONS_PATH) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("suppressions")
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"{path} has no suppressions list")
    return [row for row in rows if isinstance(row, dict)]


def parse_iso_date(value: object, *, field: str, vuln_id: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"{vuln_id}: missing {field}")
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"{vuln_id}: invalid {field}={value!r} (want YYYY-MM-DD)") from exc


def check_suppressions(
    rows: list[dict[str, object]],
    *,
    today: date,
) -> list[str]:
    """Return failure reasons (empty means pass)."""
    reasons: list[str] = []
    seen: set[str] = set()
    for row in rows:
        vuln_id = str(row.get("id") or "").strip()
        if not vuln_id:
            reasons.append("suppression entry missing id")
            continue
        if vuln_id in seen:
            reasons.append(f"duplicate suppression id {vuln_id}")
        seen.add(vuln_id)
        rationale = str(row.get("rationale") or "").strip()
        if len(rationale) < 40:
            reasons.append(f"{vuln_id}: rationale too short / missing")
        reviewed = parse_iso_date(row.get("reviewed"), field="reviewed", vuln_id=vuln_id)
        expires = parse_iso_date(row.get("expires"), field="expires", vuln_id=vuln_id)
        if expires < reviewed:
            reasons.append(f"{vuln_id}: expires {expires} is before reviewed {reviewed}")
        if expires < today:
            reasons.append(
                f"{vuln_id}: suppression expired on {expires} (today {today}). "
                "Re-evaluate: upgrade chromadb if fixed, or extend reviewed/expires "
                f"in {SUPPRESSIONS_PATH.name} after confirming embedded-only controls."
            )
    return reasons


def ignore_vuln_ids(rows: list[dict[str, object]]) -> list[str]:
    return [str(row["id"]).strip() for row in rows if str(row.get("id") or "").strip()]


def audit_script_has_ignores(audit_text: str, vuln_ids: list[str]) -> list[str]:
    """
    Return vuln ids missing from the audit script.

    Accepts either inline ``--ignore-vuln ID`` flags or dynamic generation via
    ``check_chroma_suppressions.py --pip-audit-args``.
    """
    if (
        "check_chroma_suppressions.py" in audit_text
        and "--pip-audit-args" in audit_text
        and "CHROMA_IGNORE_ARGS" in audit_text
    ):
        return []
    return [vid for vid in vuln_ids if f"--ignore-vuln {vid}" not in audit_text]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pip-audit-args",
        action="store_true",
        help="Print --ignore-vuln flags for dependency-audit.sh and exit 0",
    )
    parser.add_argument(
        "--today",
        default=None,
        help="Override today's date (YYYY-MM-DD) for expiry checks / tests",
    )
    args = parser.parse_args(argv)

    rows = load_suppressions()
    vuln_ids = ignore_vuln_ids(rows)

    if args.pip_audit_args:
        # One flag token per line so bash mapfile keeps --ignore-vuln and ID separate.
        for vuln_id in vuln_ids:
            print("--ignore-vuln")
            print(vuln_id)
        return 0

    today = (
        datetime.strptime(args.today, "%Y-%m-%d").date()
        if args.today
        else date.today()
    )
    reasons = check_suppressions(rows, today=today)
    audit_text = AUDIT_SCRIPT_PATH.read_text(encoding="utf-8")
    missing = audit_script_has_ignores(audit_text, vuln_ids)
    if missing:
        reasons.append(
            "dependency-audit.sh missing --ignore-vuln for: " + ", ".join(missing)
        )
    # Ensure audit script still documents the compensating control and JSON source.
    if "chroma_vuln_suppressions.json" not in audit_text:
        reasons.append("dependency-audit.sh must reference chroma_vuln_suppressions.json")
    if "chroma_local" not in audit_text and "PersistentClient" not in audit_text:
        reasons.append("dependency-audit.sh must document embedded PersistentClient control")

    if reasons:
        print("chroma vuln suppressions check FAILED:", file=sys.stderr)
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 1

    print(
        f"chroma vuln suppressions OK ({len(vuln_ids)} ids; "
        f"next expiry {min(parse_iso_date(r.get('expires'), field='expires', vuln_id=str(r.get('id'))) for r in rows)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
