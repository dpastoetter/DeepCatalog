"""API-level tests for the GitHub self-update endpoints (no network)."""

from __future__ import annotations

import httpx

from deepcatalog.updater import get_current_version


def test_update_status_local_only(client):
    """Without check=true the endpoint must not hit the network."""
    resp = client.get("/api/update/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["current_version"] == get_current_version()
    assert "/" in body["repo"]
    assert "update_available" not in body  # no GitHub check performed


def test_update_status_check_reports_newer(client, monkeypatch):
    monkeypatch.setattr(
        "deepcatalog.updater._fetch_latest_release",
        lambda: {
            "tag": "v99.0.0",
            "name": "v99.0.0",
            "notes": "big release",
            "published_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/x/y/releases/tag/v99.0.0",
            "tarball_url": "https://api.github.com/repos/x/y/tarball/v99.0.0",
            "verifiable": True,
            "signed": True,
            "manifest_commit": "a" * 40,
            "verification_error": None,
            "artifact": {
                "filename": "deepcatalog-99.0.0.tar.gz",
                "download_url": "https://example.invalid/deepcatalog-99.0.0.tar.gz",
                "expected_sha256": "a" * 64,
            },
        },
    )
    body = client.get("/api/update/status?check=true").json()
    assert body["update_available"] is True
    assert body["latest_version"] == "99.0.0"
    assert body["notes"] == "big release"
    assert body["verifiable"] is True
    assert body["signed"] is True
    assert body["expected_sha256"] == "a" * 64
    assert body["installable"] is True
    assert body["appimage"] is False


def test_update_status_check_handles_no_releases(client, monkeypatch):
    monkeypatch.setattr("deepcatalog.updater._fetch_latest_release", lambda: None)
    body = client.get("/api/update/status?check=true").json()
    assert body["status"] == "success"
    assert body["update_available"] is False
    assert "No releases" in body["message"]


def test_update_apply_conflicts_when_up_to_date(client, monkeypatch):
    monkeypatch.setattr(
        "deepcatalog.updater._fetch_latest_release",
        lambda: {
            "tag": f"v{get_current_version()}",
            "name": "current",
            "notes": "",
            "published_at": None,
            "html_url": "",
            "tarball_url": "https://api.github.com/repos/x/y/tarball/current",
        },
    )
    resp = client.post("/api/update/apply")
    assert resp.status_code == 409
    assert "up to date" in resp.json()["detail"].lower()


def test_update_restart_endpoint_schedules(client, monkeypatch):
    """Restart must not actually exec during tests."""
    calls: list[bool] = []
    monkeypatch.setattr(
        "app.routers.updates.schedule_restart",
        lambda: calls.append(True) or {"status": "success", "message": "Restarting..."},
    )
    resp = client.post("/api/update/restart")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert calls == [True]


def test_update_status_check_appimage_not_installable(client, monkeypatch):
    monkeypatch.setattr("deepcatalog.updater.running_as_appimage", lambda: True)
    monkeypatch.setattr(
        "deepcatalog.updater._fetch_latest_release",
        lambda: {
            "tag": "v99.0.0",
            "name": "v99.0.0",
            "notes": "",
            "published_at": None,
            "html_url": "https://github.com/x/y/releases/tag/v99.0.0",
            "tarball_url": "https://api.github.com/repos/x/y/tarball/v99.0.0",
            "verifiable": True,
            "signed": True,
            "manifest_commit": "a" * 40,
            "verification_error": None,
            "artifact": {
                "filename": "deepcatalog-99.0.0.tar.gz",
                "download_url": "https://example.invalid/deepcatalog-99.0.0.tar.gz",
                "expected_sha256": "a" * 64,
            },
            "assets": [
                {
                    "name": "DeepCatalog-99.0.0-x86_64.AppImage",
                    "browser_download_url": "https://example.invalid/DeepCatalog-99.0.0-x86_64.AppImage",
                }
            ],
        },
    )
    body = client.get("/api/update/status?check=true").json()
    assert body["update_available"] is True
    assert body["installable"] is False
    assert body["appimage"] is True
    assert body["appimage_url"].endswith(".AppImage")


def test_update_apply_conflicts_on_unsigned(client, monkeypatch):
    monkeypatch.setattr(
        "deepcatalog.updater.check_for_update",
        lambda: {
            "status": "success",
            "current_version": "0.1.0",
            "latest_version": "9.9.9",
            "update_available": True,
            "verifiable": False,
            "signed": False,
            "verification_error": "Latest release is missing a signed release-manifest.json",
        },
    )
    resp = client.post("/api/update/apply")
    assert resp.status_code == 409
    assert "unsigned" in resp.json()["detail"].lower()


def test_update_apply_conflicts_on_appimage(client, monkeypatch):
    monkeypatch.setattr("deepcatalog.updater.running_as_appimage", lambda: True)
    resp = client.post("/api/update/apply")
    assert resp.status_code == 409
    assert "AppImage" in resp.json()["detail"]


def test_update_status_check_hides_http_error_details(client, monkeypatch):
    def boom():
        raise httpx.ConnectError("secret-internal-host.example")

    monkeypatch.setattr("deepcatalog.updater._fetch_latest_release", boom)
    body = client.get("/api/update/status?check=true").json()
    assert body["status"] == "error"
    blob = str(body).lower()
    assert "secret-internal-host" not in blob
    assert "could not reach github" in body["error"].lower()
