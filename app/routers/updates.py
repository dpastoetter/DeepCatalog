"""In-app software update routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from deepcatalog.inbox_worker import is_processing
from deepcatalog.release_trust import update_repo
from deepcatalog.updater import (
    apply_update,
    check_for_update,
    schedule_restart,
)
from deepcatalog.version import get_current_version

router = APIRouter(tags=["updates"])


@router.get("/api/update/status")
def api_update_status(check: bool = Query(default=False)) -> dict[str, Any]:
    """Installed version; with check=true also queries GitHub for the latest release."""
    if not check:
        return {
            "status": "success",
            "repo": update_repo(),
            "current_version": get_current_version(),
        }
    return check_for_update()


@router.post("/api/update/apply")
def api_update_apply() -> dict[str, Any]:
    """Download the latest GitHub release and install it over this instance."""
    result = apply_update()
    if result.get("status") != "success":
        raw_error = result.get("error")
        error = raw_error if isinstance(raw_error, str) else ""
        lowered = error.lower()
        if "appimage" in lowered:
            detail = (
                "This AppImage cannot be updated in place. "
                "Download the latest DeepCatalog-*-x86_64.AppImage from GitHub "
                "Releases and replace this file."
            )
        elif "up to date" in lowered:
            detail = "Already up to date."
        elif (
            "unsigned" in lowered
            or "checksums are not authentic" in lowered
            or "unverified" in lowered
            or "missing a signed" in lowered
        ):
            detail = "Refusing to install an unsigned release (checksums are not authentic)."
        elif "download failed" in lowered:
            detail = "Download failed. Check your network and try again."
        elif "verification failed" in lowered or "sha-256 mismatch" in lowered:
            detail = "Release verification failed (SHA-256 mismatch)"
        elif "release-commit" in lowered:
            detail = "Archive does not match the signed manifest commit."
        else:
            detail = "update failed"
        raise HTTPException(status_code=409, detail=detail)
    return result


@router.post("/api/update/restart")
def api_update_restart() -> dict[str, Any]:
    """Restart the server process (used after installing an update)."""
    if is_processing():
        raise HTTPException(
            status_code=409,
            detail="documents are being processed — try again in a moment",
        )
    return schedule_restart()
