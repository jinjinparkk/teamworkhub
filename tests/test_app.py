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
    assert data["status"] == "ok"
    assert data["email_count"] == 0


# ── /monthly ───────────────────────────────────────────────────────── #

def test_monthly_returns_200(client):
    resp = client.post("/monthly")
    assert resp.status_code == 200


def test_monthly_skipped_when_no_env_vars(client, monkeypatch):
    for var in _EMPTY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    data = client.post("/monthly").json()
    assert data["status"] == "skipped"


def test_monthly_response_has_required_fields(monkeypatch):
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)
    patches = _mock_pipeline()
    with patches[0], patches[1], patches[2], patches[3]:
        with TestClient(app, raise_server_exceptions=True) as c:
            data = c.post("/monthly").json()
    assert {"status", "run_id", "month", "email_count"} <= data.keys()


def test_monthly_ok_with_mocked_pipeline(monkeypatch):
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)
    patches = _mock_pipeline()
    with patches[0], patches[1], patches[2], patches[3]:
        with TestClient(app, raise_server_exceptions=True) as c:
            data = c.post("/monthly").json()
    assert data["status"] == "ok"
    assert data["email_count"] == 0


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
