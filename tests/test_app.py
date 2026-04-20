"""Unit tests for /health and /sync endpoints.

No real Gmail/Drive API calls are made here — auth + API layers are mocked
wherever the full pipeline would attempt network I/O.
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.app import app

_FULL_ENV = {
    "DRIVE_OUTPUT_FOLDER_ID": "fake-folder-id",
    "GOOGLE_OAUTH_CLIENT_ID": "fake-client-id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "fake-secret",
    "GOOGLE_OAUTH_REFRESH_TOKEN": "fake-refresh-token",
}

_EMPTY_ENV_VARS = (
    "DRIVE_OUTPUT_FOLDER_ID",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REFRESH_TOKEN",
)


def _mock_pipeline():
    """Patch build_credentials + list_messages so /sync runs without network."""
    creds = MagicMock()
    return (
        patch("src.app.build_credentials", return_value=creds),
        patch("src.app.build_gmail_service", return_value=MagicMock()),
        patch("src.app.build_drive_service", return_value=MagicMock()),
        patch("src.app.list_messages", return_value=[]),
    )


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── /health ────────────────────────────────────────────────────────── #

def test_health_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_shape(client):
    data = client.get("/health").json()
    assert data["status"] == "ok"
    assert "service" in data


# ── /sync — response contract ──────────────────────────────────────── #

REQUIRED_FIELDS = {"status", "run_id", "processed", "skipped", "errors"}


def test_sync_returns_200(client):
    resp = client.post("/sync")
    assert resp.status_code == 200


def test_sync_response_has_required_fields(client):
    data = client.post("/sync").json()
    missing = REQUIRED_FIELDS - data.keys()
    assert not missing, f"Response missing fields: {missing}"


def test_sync_counts_are_integers(client):
    data = client.post("/sync").json()
    assert isinstance(data["processed"], int)
    assert isinstance(data["skipped"], int)
    assert isinstance(data["errors"], int)


def test_sync_run_id_is_nonempty_string(client):
    data = client.post("/sync").json()
    assert isinstance(data["run_id"], str)
    assert len(data["run_id"]) > 0


def test_sync_status_is_valid_value(client):
    data = client.post("/sync").json()
    assert data["status"] in ("ok", "skipped", "error", "partial")


# ── /sync — skipped when env vars missing ─────────────────────────── #

def test_sync_skipped_when_no_env_vars(client, monkeypatch):
    """With no OAuth/Drive env vars, sync must return status=skipped, not crash."""
    for var in _EMPTY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    data = client.post("/sync").json()
    assert data["status"] == "skipped"
    assert "note" in data


def test_sync_ok_when_all_vars_set(monkeypatch):
    """When all required env vars are present and API is mocked, status=ok, processed=0."""
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)

    patches = _mock_pipeline()
    with patches[0], patches[1], patches[2], patches[3]:
        with TestClient(app, raise_server_exceptions=True) as c:
            data = c.post("/sync").json()

    assert data["status"] == "ok"
    assert data["processed"] == 0   # no messages returned by mocked list_messages


# ── each /sync call gets a unique run_id ──────────────────────────── #

def test_sync_run_ids_are_unique(monkeypatch):
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)

    patches = _mock_pipeline()
    with patches[0], patches[1], patches[2], patches[3]:
        with TestClient(app, raise_server_exceptions=True) as c:
            ids = {c.post("/sync").json()["run_id"] for _ in range(5)}

    assert len(ids) == 5, "run_ids should be unique across calls"


# ── /daily ─────────────────────────────────────────────────────────── #

def test_daily_returns_200(client):
    resp = client.post("/daily")
    assert resp.status_code == 200


def test_daily_skipped_when_no_env_vars(client, monkeypatch):
    for var in _EMPTY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    data = client.post("/daily").json()
    assert data["status"] == "skipped"
    assert "note" in data


def test_daily_response_has_required_fields(monkeypatch):
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)
    patches = _mock_pipeline()
    with patches[0], patches[1], patches[2], patches[3]:
        with patch("src.app.fetch_message"), patch("src.app.analyze_email"):
            with TestClient(app, raise_server_exceptions=True) as c:
                data = c.post("/daily").json()
    assert {"status", "run_id", "date", "email_count"} <= data.keys()


def test_daily_ok_with_mocked_pipeline(monkeypatch):
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)
    patches = _mock_pipeline()
    with patches[0], patches[1], patches[2], patches[3]:
        with TestClient(app, raise_server_exceptions=True) as c:
            data = c.post("/daily").json()
    assert data["status"] == "ok"
    assert data["email_count"] == 0


def test_daily_monday_uses_friday_period_start(monkeypatch):
    """On Monday the period_start must be Friday 18:00 (3 days back), not Sunday 18:00."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import src.app as app_mod

    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)

    # 2025-04-07 is a Monday
    monday = datetime(2025, 4, 7, 9, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    captured: list[str] = []

    original_compose = app_mod.compose_daily

    def _spy_compose(messages, date_str, period_start, period_end, *args, **kwargs):
        captured.append(period_start)
        return original_compose(messages, date_str, period_start, period_end, *args, **kwargs)

    patches = _mock_pipeline()
    with patches[0], patches[1], patches[2], patches[3]:
        with patch("src.app.compose_daily", side_effect=_spy_compose):
            with patch("src.app.datetime") as mock_dt:
                mock_dt.now.return_value = monday
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                with TestClient(app, raise_server_exceptions=True) as c:
                    c.post("/daily")

    assert len(captured) == 1
    # period_start should be "2025-04-04 00:00" (Friday), not "2025-04-06 00:00" (Sunday)
    assert captured[0].startswith("2025-04-04"), f"Expected Friday start, got: {captured[0]}"


# ── /weekly ────────────────────────────────────────────────────────── #

def test_weekly_returns_200(client):
    resp = client.post("/weekly")
    assert resp.status_code == 200


def test_weekly_skipped_when_no_env_vars(client, monkeypatch):
    for var in _EMPTY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    data = client.post("/weekly").json()
    assert data["status"] == "skipped"


def test_weekly_response_has_required_fields(monkeypatch):
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)
    patches = _mock_pipeline()
    with patches[0], patches[1], patches[2], patches[3]:
        with TestClient(app, raise_server_exceptions=True) as c:
            data = c.post("/weekly").json()
    assert {"status", "run_id", "week", "email_count"} <= data.keys()


def test_weekly_ok_with_mocked_pipeline(monkeypatch):
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)
    patches = _mock_pipeline()
    with patches[0], patches[1], patches[2], patches[3]:
        with TestClient(app, raise_server_exceptions=True) as c:
            data = c.post("/weekly").json()
    # weekly endpoint is currently disabled (deprecated=True)
    assert data["status"] == "skipped"


# ── /dashboard ─────────────────────────────────────────────────────── #

def test_dashboard_returns_200(client):
    resp = client.post("/dashboard")
    assert resp.status_code == 200


def test_dashboard_skipped_when_no_local_dir(client, monkeypatch):
    monkeypatch.delenv("LOCAL_DASHBOARD_DIR", raising=False)
    data = client.post("/dashboard").json()
    assert data["status"] == "skipped"
    assert "note" in data


def test_dashboard_ok_when_dir_set(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_DASHBOARD_DIR", str(tmp_path))
    with TestClient(app, raise_server_exceptions=True) as c:
        data = c.post("/dashboard").json()
    assert data["status"] == "ok"
    assert (tmp_path / "Dashboard.md").exists()


def test_dashboard_response_has_run_id(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_DASHBOARD_DIR", str(tmp_path))
    with TestClient(app, raise_server_exceptions=True) as c:
        data = c.post("/dashboard").json()
    assert "run_id" in data
    assert len(data["run_id"]) > 0


def test_dashboard_scans_daily_notes_for_assignees(monkeypatch, tmp_path):
    """Assignee pages must be created for all unique assignees in past Daily Notes."""
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    dash_dir = tmp_path / "dashboard"

    # Write two fake daily notes with different assignees
    (daily_dir / "2026-04-01.md").write_text(
        "---\nassignees: ['박은진', '해랑']\n---\n", encoding="utf-8"
    )
    (daily_dir / "2026-04-02.md").write_text(
        "---\nassignees: ['박은진']\n---\n", encoding="utf-8"
    )

    monkeypatch.setenv("LOCAL_DASHBOARD_DIR", str(dash_dir))
    monkeypatch.setenv("LOCAL_DAILY_OUTPUT_DIR", str(daily_dir))

    with TestClient(app, raise_server_exceptions=True) as c:
        data = c.post("/dashboard").json()

    assert data["status"] == "ok"
    assert data["assignee_pages"] == 2
    assert (dash_dir / "박은진.md").exists()
    assert (dash_dir / "해랑.md").exists()


# ── /scan-archive ─────────────────────────────────────────────────── #

def test_scan_archive_returns_200(client):
    resp = client.post("/scan-archive")
    assert resp.status_code == 200


def test_scan_archive_skipped_when_no_env_vars(client, monkeypatch):
    monkeypatch.delenv("DRIVE_EMAIL_ARCHIVE_FOLDER_ID", raising=False)
    for var in _EMPTY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    data = client.post("/scan-archive").json()
    assert data["status"] == "skipped"
    assert "note" in data


def test_scan_archive_response_has_required_fields(monkeypatch):
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DRIVE_EMAIL_ARCHIVE_FOLDER_ID", "fake-archive-id")

    patches = _mock_pipeline()
    with patches[0], patches[1], patches[2], patches[3]:
        with patch("src.app.scan_archive_folders") as mock_scan:
            from src.archive_scanner import ScanResult
            mock_scan.return_value = ScanResult(processed=0, skipped=0, errors=0)
            with TestClient(app, raise_server_exceptions=True) as c:
                data = c.post("/scan-archive").json()

    assert {"status", "run_id", "processed", "skipped", "errors"} <= data.keys()


def test_scan_archive_ok_with_mocked_scan(monkeypatch):
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DRIVE_EMAIL_ARCHIVE_FOLDER_ID", "fake-archive-id")

    patches = _mock_pipeline()
    with patches[0], patches[1], patches[2], patches[3]:
        with patch("src.app.scan_archive_folders") as mock_scan:
            from src.archive_scanner import ScanResult
            mock_scan.return_value = ScanResult(processed=2, skipped=1, errors=0)
            with TestClient(app, raise_server_exceptions=True) as c:
                data = c.post("/scan-archive").json()

    assert data["status"] == "ok"
    assert data["processed"] == 2
    assert data["skipped"] == 1
