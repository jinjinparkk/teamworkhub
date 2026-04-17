"""TeamWorkHub QA Agent
═══════════════════════
Standalone quality-assurance runner.  Tests all endpoints and verifies
detail page generation works correctly **with and without** Gemini.

Usage
─────
    py -3 scripts/qa_agent.py              # standalone
    pytest scripts/qa_agent.py -v          # via pytest
"""
from __future__ import annotations

import os
import sys
import tempfile
import textwrap
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Ensure project root is importable ─────────────────────────────── #
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from src.app import app
from src.drive_client import DriveFile
from src.gmail_client import Attachment, ParsedMessage
from src.md_writer import compose, filename_for_subject
from src.summarizer import AnalysisResult, analyze_email, _fallback_summary


# ══════════════════════════════════════════════════════════════════════ #
#  Test data builders
# ══════════════════════════════════════════════════════════════════════ #

def _msg(
    message_id: str = "msg_qa_001",
    subject: str = "CM360 캠페인 성과 리포트 확인 요청",
    sender: str = "이해랑 <hrlee@artience.com>",
    to: str = "ejpark@artience.com",
    cc: str = "cwsong@artience.com",
    body_text: str = (
        "박은진대리님,\n\n"
        "금주 CM360 캠페인 성과 리포트를 첨부드립니다.\n"
        "SEGR 지역 CPM이 전주 대비 12% 상승했으며,\n"
        "TRUEVIEW 캠페인은 VTR 목표를 초과 달성했습니다.\n\n"
        "금요일까지 리뷰 부탁드립니다.\n\n"
        "감사합니다.\n"
        "이해랑 팀장"
    ),
) -> ParsedMessage:
    return ParsedMessage(
        message_id=message_id,
        thread_id=f"thread_{message_id}",
        subject=subject,
        sender=sender,
        to=to,
        cc=cc,
        date_utc="2026-04-16T09:30:00+09:00",
        body_text=body_text,
        attachments=[],
    )


def _drive_file(name: str = "twh_msg_qa_001.md") -> DriveFile:
    return DriveFile(
        file_id="df_qa_001",
        name=name,
        web_view_link="https://drive.google.com/file/d/df_qa_001/view",
        created=True,
    )


_FULL_ENV = {
    "DRIVE_OUTPUT_FOLDER_ID": "qa-folder-id",
    "GOOGLE_OAUTH_CLIENT_ID": "qa-client-id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "qa-secret",
    "GOOGLE_OAUTH_REFRESH_TOKEN": "qa-refresh",
}


def _pipeline_patches(
    list_return=None,
    fetch_return=None,
    find_return=None,
    analyze_return=None,
):
    """Return a dict of all external-I/O patches with sensible defaults."""
    creds = MagicMock()
    return {
        "src.app.build_credentials": MagicMock(return_value=creds),
        "src.app.build_gmail_service": MagicMock(return_value=MagicMock()),
        "src.app.build_drive_service": MagicMock(return_value=MagicMock()),
        "src.app.list_messages": MagicMock(return_value=list_return or []),
        "src.app.fetch_message": MagicMock(return_value=fetch_return or _msg()),
        "src.app.find_file_by_name": MagicMock(return_value=find_return),
        "src.app.download_attachment": MagicMock(return_value=b"bytes"),
        "src.app.upload_attachment": MagicMock(return_value=_drive_file()),
        "src.app.upsert_markdown": MagicMock(return_value=_drive_file()),
        "src.app.analyze_email": MagicMock(
            return_value=analyze_return or AnalysisResult()
        ),
        "src.app.extract_assignees": MagicMock(return_value=["박은진"]),
    }


def _run(client: TestClient, method: str, path: str, patches: dict) -> dict:
    with ExitStack() as stack:
        for target, mock in patches.items():
            stack.enter_context(patch(target, mock))
        if method == "GET":
            return client.get(path).json()
        return client.post(path).json()


# ══════════════════════════════════════════════════════════════════════ #
#  QA checks
# ══════════════════════════════════════════════════════════════════════ #

@dataclass
class QAResult:
    name: str
    passed: bool
    detail: str = ""


def _check(name: str, condition: bool, detail: str = "") -> QAResult:
    return QAResult(name=name, passed=condition, detail=detail)


def run_all_checks() -> list[QAResult]:
    results: list[QAResult] = []
    tmp = tempfile.mkdtemp(prefix="twh_qa_")

    # ── 1. Health endpoint ────────────────────────────────────────── #
    with TestClient(app) as c:
        data = c.get("/health").json()
    results.append(_check(
        "health-200", data.get("status") == "ok",
        f"status={data.get('status')}",
    ))

    # ── 2. Fallback summary (no Gemini) ───────────────────────────── #
    body = "첫째 줄입니다.\n둘째 줄입니다.\n셋째 줄입니다.\n넷째 줄."
    fb = _fallback_summary(body)
    results.append(_check(
        "fallback-summary-not-empty",
        bool(fb),
        f"fallback='{fb[:60]}...'",
    ))
    results.append(_check(
        "fallback-summary-3-lines",
        fb.count("\n") == 2,
        f"lines={fb.count(chr(10)) + 1}",
    ))

    # ── 3. analyze_email without API key → fallback summary ──────── #
    ar = analyze_email("테스트 제목", "sender@test.com", body, "")
    results.append(_check(
        "analyze-no-key-has-summary",
        bool(ar.summary),
        f"summary='{ar.summary[:40]}...'",
    ))
    results.append(_check(
        "analyze-no-key-source-fallback",
        ar.source == "fallback",
        f"source={ar.source}",
    ))

    # ── 4. compose() with fallback summary → no '요약 없음' ──────── #
    msg = _msg()
    md = compose(msg, [], "2026-04-16T10:00:00+00:00", ar.summary, "", ar)
    results.append(_check(
        "compose-no-gemini-has-summary",
        "_(요약 없음)_" not in md,
        "detail page summary should NOT be empty when body exists",
    ))
    results.append(_check(
        "compose-body-included",
        "CM360 캠페인 성과 리포트" in md,
        "full body text must appear in detail page",
    ))
    results.append(_check(
        "compose-frontmatter-valid",
        md.startswith("---\n") and md.count("---") >= 2,
        "YAML frontmatter delimiters",
    ))
    results.append(_check(
        "compose-sections-present",
        "### 요약" in md and "### 본문" in md and "### 첨부파일 링크" in md,
        "all 3 sections must exist",
    ))

    # ── 5. compose() with Gemini summary ──────────────────────────── #
    ar_gemini = AnalysisResult(
        summary="- CM360 CPM 12% 상승\n- TRUEVIEW VTR 초과 달성",
        assignees=["박은진", "송찬우"],
        priority="보통",
        category="보고",
        source="gemini",
    )
    md_gemini = compose(msg, [], "2026-04-16T10:00:00+00:00", ar_gemini.summary, "", ar_gemini)
    results.append(_check(
        "compose-gemini-summary",
        "CM360 CPM 12% 상승" in md_gemini,
        "Gemini summary must appear in detail page",
    ))
    results.append(_check(
        "compose-gemini-tags",
        "#박은진" in md_gemini and "#보고" in md_gemini,
        "assignee and category tags in frontmatter",
    ))

    # ── 6. Media/Subsidiary auto-tagging ──────────────────────────── #
    results.append(_check(
        "compose-media-tags",
        "TRUEVIEW" in md or "CM360" in md,
        "media keywords auto-tagged",
    ))

    # ── 7. /sync endpoint — full pipeline (no Gemini) ─────────────── #
    os_env = {**_FULL_ENV, "LOCAL_OUTPUT_DIR": tmp}
    patches = _pipeline_patches(
        list_return=[{"id": "msg_qa_001"}],
        find_return=None,  # not yet in Drive
    )
    with TestClient(app) as c:
        for k, v in os_env.items():
            os.environ[k] = v
        try:
            data = _run(c, "POST", "/sync", patches)
        finally:
            for k in os_env:
                os.environ.pop(k, None)
    results.append(_check(
        "sync-status-ok",
        data.get("status") == "ok",
        f"status={data.get('status')}",
    ))
    results.append(_check(
        "sync-processed-1",
        data.get("processed") == 1,
        f"processed={data.get('processed')}",
    ))

    # ── 8. /sync — idempotency (already synced) ──────────────────── #
    patches_skip = _pipeline_patches(
        list_return=[{"id": "msg_qa_001"}],
        find_return=_drive_file(),  # already in Drive
    )
    with TestClient(app) as c:
        for k, v in _FULL_ENV.items():
            os.environ[k] = v
        try:
            data = _run(c, "POST", "/sync", patches_skip)
        finally:
            for k in _FULL_ENV:
                os.environ.pop(k, None)
    results.append(_check(
        "sync-idempotent-skip",
        data.get("skipped") == 1 and data.get("processed") == 0,
        f"skipped={data.get('skipped')}, processed={data.get('processed')}",
    ))

    # ── 9. /daily endpoint ────────────────────────────────────────── #
    patches_daily = _pipeline_patches()
    with TestClient(app) as c:
        for k, v in _FULL_ENV.items():
            os.environ[k] = v
        try:
            data = _run(c, "POST", "/daily", patches_daily)
        finally:
            for k in _FULL_ENV:
                os.environ.pop(k, None)
    results.append(_check(
        "daily-status-ok",
        data.get("status") == "ok",
        f"status={data.get('status')}",
    ))
    results.append(_check(
        "daily-has-date",
        bool(data.get("date")),
        f"date={data.get('date')}",
    ))

    # ── 10. /daily with local output — detail pages created ───────── #
    daily_tmp = tempfile.mkdtemp(prefix="twh_qa_daily_")
    note_tmp = tempfile.mkdtemp(prefix="twh_qa_notes_")
    daily_env = {
        **_FULL_ENV,
        "LOCAL_OUTPUT_DIR": note_tmp,
        "LOCAL_DAILY_OUTPUT_DIR": daily_tmp,
    }
    ar_fallback = AnalysisResult(
        summary=_fallback_summary(_msg().body_text),
        assignees=["박은진"],
    )
    patches_daily_local = _pipeline_patches(
        list_return=[{"id": "msg_qa_daily"}],
        fetch_return=_msg(message_id="msg_qa_daily"),
        analyze_return=ar_fallback,
    )
    with TestClient(app) as c:
        for k, v in daily_env.items():
            os.environ[k] = v
        try:
            data = _run(c, "POST", "/daily", patches_daily_local)
        finally:
            for k in daily_env:
                os.environ.pop(k, None)
    results.append(_check(
        "daily-local-status-ok",
        data.get("status") == "ok",
        f"status={data.get('status')}, count={data.get('email_count')}",
    ))

    # Check that daily note was written
    daily_files = list(Path(daily_tmp).glob("*.md"))
    results.append(_check(
        "daily-note-created",
        len(daily_files) >= 1,
        f"daily files: {[f.name for f in daily_files]}",
    ))

    # Check that individual detail page was written
    note_files = list(Path(note_tmp).glob("*.md"))
    results.append(_check(
        "detail-page-created",
        len(note_files) >= 1,
        f"note files: {[f.name for f in note_files]}",
    ))

    # Verify detail page content
    if note_files:
        content = note_files[0].read_text(encoding="utf-8")
        results.append(_check(
            "detail-page-has-body",
            "CM360 캠페인 성과 리포트" in content or "박은진대리님" in content,
            "detail page must contain full email body",
        ))
        results.append(_check(
            "detail-page-no-empty-summary",
            "_(요약 없음)_" not in content,
            "fallback summary must replace empty placeholder",
        ))
        results.append(_check(
            "detail-page-has-frontmatter",
            content.startswith("---"),
            "YAML frontmatter must exist",
        ))
    else:
        results.append(_check("detail-page-has-body", False, "no note files found"))
        results.append(_check("detail-page-no-empty-summary", False, "no note files found"))
        results.append(_check("detail-page-has-frontmatter", False, "no note files found"))

    # ── 11. /weekly (disabled) ────────────────────────────────────── #
    with TestClient(app) as c:
        for k, v in _FULL_ENV.items():
            os.environ[k] = v
        try:
            data = _run(c, "POST", "/weekly", _pipeline_patches())
        finally:
            for k in _FULL_ENV:
                os.environ.pop(k, None)
    results.append(_check(
        "weekly-skipped",
        data.get("status") == "skipped",
        f"status={data.get('status')}",
    ))

    # ── 12. /monthly (disabled) ───────────────────────────────────── #
    with TestClient(app) as c:
        for k, v in _FULL_ENV.items():
            os.environ[k] = v
        try:
            data = _run(c, "POST", "/monthly", _pipeline_patches())
        finally:
            for k in _FULL_ENV:
                os.environ.pop(k, None)
    results.append(_check(
        "monthly-skipped",
        data.get("status") == "skipped",
        f"status={data.get('status')}",
    ))

    # ── 13. /dashboard ────────────────────────────────────────────── #
    dash_tmp = tempfile.mkdtemp(prefix="twh_qa_dash_")
    with TestClient(app) as c:
        os.environ["LOCAL_DASHBOARD_DIR"] = dash_tmp
        try:
            data = c.post("/dashboard").json()
        finally:
            os.environ.pop("LOCAL_DASHBOARD_DIR", None)
    results.append(_check(
        "dashboard-ok",
        data.get("status") == "ok",
        f"status={data.get('status')}",
    ))
    results.append(_check(
        "dashboard-file-created",
        (Path(dash_tmp) / "Dashboard.md").exists(),
        "Dashboard.md must be created",
    ))

    # ── 14. /sync — auth failure graceful ─────────────────────────── #
    patches_auth_fail = _pipeline_patches()
    patches_auth_fail["src.app.build_credentials"] = MagicMock(
        side_effect=Exception("OAuth token expired")
    )
    with TestClient(app) as c:
        for k, v in _FULL_ENV.items():
            os.environ[k] = v
        try:
            data = _run(c, "POST", "/sync", patches_auth_fail)
        finally:
            for k in _FULL_ENV:
                os.environ.pop(k, None)
    results.append(_check(
        "sync-auth-fail-graceful",
        data.get("status") == "error" and data.get("errors", 0) >= 1,
        f"status={data.get('status')}, errors={data.get('errors')}",
    ))

    # ── 15. filename_for_subject — wiki-link safe ─────────────────── #
    name = filename_for_subject("Re: Hello/World")
    results.append(_check(
        "filename-safe",
        "/" not in name and ":" not in name and name.endswith(".md"),
        f"filename={name}",
    ))

    # ── 16. AnalysisResult source tracking ────────────────────────── #
    ar_default = AnalysisResult()
    results.append(_check(
        "analysis-default-source",
        ar_default.source == "fallback",
        "default source should be 'fallback'",
    ))
    ar_gem = AnalysisResult(source="gemini")
    results.append(_check(
        "analysis-gemini-source",
        ar_gem.source == "gemini",
        "explicit gemini source",
    ))

    # ── 17. Empty body → empty fallback summary ──────────────────── #
    ar_empty = analyze_email("제목", "sender@test.com", "", "")
    results.append(_check(
        "analyze-empty-body-empty-summary",
        ar_empty.summary == "",
        "empty body should produce empty summary",
    ))

    # ── 18. Whitespace-only body → empty fallback summary ─────────── #
    ar_ws = analyze_email("제목", "sender@test.com", "   \n  \n  ", "")
    results.append(_check(
        "analyze-whitespace-body-empty-summary",
        ar_ws.summary == "",
        "whitespace-only body should produce empty summary",
    ))

    # ── 19. Gemini configured but failing → fallback detail page ──── #
    gemini_fail_env = {
        **_FULL_ENV,
        "LOCAL_OUTPUT_DIR": tempfile.mkdtemp(prefix="twh_qa_gf_notes_"),
        "LOCAL_DAILY_OUTPUT_DIR": tempfile.mkdtemp(prefix="twh_qa_gf_daily_"),
    }
    # analyze_email raises → _collect_messages catches and uses fallback
    patches_gemini_fail = _pipeline_patches(
        list_return=[{"id": "msg_qa_gf"}],
        fetch_return=_msg(message_id="msg_qa_gf"),
    )
    patches_gemini_fail["src.app.analyze_email"] = MagicMock(
        side_effect=Exception("Gemini 500 Internal Server Error")
    )
    with TestClient(app) as c:
        for k, v in gemini_fail_env.items():
            os.environ[k] = v
        try:
            data = _run(c, "POST", "/daily", patches_gemini_fail)
        finally:
            for k in gemini_fail_env:
                os.environ.pop(k, None)
    results.append(_check(
        "gemini-configured-but-failing",
        data.get("status") == "ok",
        f"status={data.get('status')}, daily should succeed even when Gemini fails",
    ))
    gf_note_files = list(Path(gemini_fail_env["LOCAL_OUTPUT_DIR"]).glob("*.md"))
    results.append(_check(
        "daily-gemini-fail-still-creates-notes",
        len(gf_note_files) >= 1,
        f"detail pages: {[f.name for f in gf_note_files]}",
    ))

    # ── 20. Detail page contains full email body ─────────────────── #
    full_body = (
        "안녕하세요.\n\n"
        "이번 분기 실적 보고서를 첨부합니다.\n"
        "매출은 전 분기 대비 15% 증가했습니다.\n"
        "자세한 내용은 첨부 파일을 참조해 주세요.\n\n"
        "감사합니다.\n"
        "김철수 부장"
    )
    msg_full = _msg(message_id="msg_full_body", body_text=full_body,
                    subject="분기 실적 보고서")
    ar_full = AnalysisResult(summary=_fallback_summary(full_body), assignees=["김철수"])
    md_full = compose(msg_full, [], "2026-04-17T10:00:00+00:00", ar_full.summary, "", ar_full)
    results.append(_check(
        "detail-page-full-body",
        "매출은 전 분기 대비 15% 증가" in md_full and "김철수 부장" in md_full,
        "detail page must include the complete email body text",
    ))

    # ── 21. No Gemini → full body + fallback summary in detail ────── #
    ar_no_gem = analyze_email("분기 실적 보고서", "kimcs@test.com", full_body, "")
    md_no_gem = compose(msg_full, [], "2026-04-17T10:00:00+00:00", ar_no_gem.summary, "", ar_no_gem)
    results.append(_check(
        "detail-page-no-gemini-full-body",
        "매출은 전 분기 대비 15% 증가" in md_no_gem
        and "_(요약 없음)_" not in md_no_gem
        and ar_no_gem.summary != "",
        "no-Gemini detail page must have body + fallback summary",
    ))

    # ── 22. Pipeline resilience — one msg fails, rest processed ───── #
    resilience_env = {**_FULL_ENV}
    call_count = {"n": 0}
    original_msg1 = _msg(message_id="msg_ok_1")
    original_msg2 = _msg(message_id="msg_ok_2")
    def _fetch_resilience(svc, msg_id):
        call_count["n"] += 1
        if msg_id == "msg_fail":
            raise Exception("Simulated fetch failure")
        if msg_id == "msg_ok_2":
            return original_msg2
        return original_msg1
    patches_resilience = _pipeline_patches(
        list_return=[{"id": "msg_ok_1"}, {"id": "msg_fail"}, {"id": "msg_ok_2"}],
        find_return=None,
    )
    patches_resilience["src.app.fetch_message"] = MagicMock(side_effect=_fetch_resilience)
    with TestClient(app) as c:
        for k, v in resilience_env.items():
            os.environ[k] = v
        try:
            data = _run(c, "POST", "/sync", patches_resilience)
        finally:
            for k in resilience_env:
                os.environ.pop(k, None)
    results.append(_check(
        "pipeline-resilience",
        data.get("processed", 0) == 2 and data.get("errors", 0) == 1,
        f"processed={data.get('processed')}, errors={data.get('errors')} — should be 2/1",
    ))

    # ── 23. _collect_messages error isolation — analyze raises ─────── #
    isolation_env = {**_FULL_ENV}
    call_idx = {"n": 0}
    def _analyze_sometimes_fail(subject, sender, body, key, to="", cc=""):
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            raise Exception("Gemini transient error")
        return AnalysisResult(
            summary=_fallback_summary(body),
            assignees=["박은진"],
        )
    msg_iso1 = _msg(message_id="msg_iso_1")
    msg_iso2 = _msg(message_id="msg_iso_2")
    def _fetch_iso(svc, msg_id):
        return msg_iso2 if msg_id == "msg_iso_2" else msg_iso1
    patches_isolation = _pipeline_patches(
        list_return=[{"id": "msg_iso_1"}, {"id": "msg_iso_2"}],
        find_return=None,
    )
    patches_isolation["src.app.analyze_email"] = MagicMock(side_effect=_analyze_sometimes_fail)
    patches_isolation["src.app.fetch_message"] = MagicMock(side_effect=_fetch_iso)
    with TestClient(app) as c:
        for k, v in isolation_env.items():
            os.environ[k] = v
        try:
            data = _run(c, "POST", "/sync", patches_isolation)
        finally:
            for k in isolation_env:
                os.environ.pop(k, None)
    results.append(_check(
        "collect-messages-error-isolation",
        data.get("processed", 0) == 2,
        f"processed={data.get('processed')} — both messages should be processed despite analyze failure",
    ))

    # ── 24. compose() with analysis=None still works ──────────────── #
    md_none = compose(_msg(), [], "2026-04-17T10:00:00+00:00", "", "", None)
    results.append(_check(
        "compose-none-analysis",
        md_none.startswith("---") and "### 요약" in md_none and "### 본문" in md_none,
        "compose(analysis=None) must produce valid markdown",
    ))

    # ── 25. /sync — no Gemini, detail page still created locally ──── #
    sync_local_tmp = tempfile.mkdtemp(prefix="twh_qa_sync_local_")
    sync_local_env = {**_FULL_ENV, "LOCAL_OUTPUT_DIR": sync_local_tmp}
    patches_sync_local = _pipeline_patches(
        list_return=[{"id": "msg_qa_sync_local"}],
        fetch_return=_msg(message_id="msg_qa_sync_local"),
        find_return=None,
        analyze_return=AnalysisResult(summary=_fallback_summary(_msg().body_text)),
    )
    with TestClient(app) as c:
        for k, v in sync_local_env.items():
            os.environ[k] = v
        try:
            data = _run(c, "POST", "/sync", patches_sync_local)
        finally:
            for k in sync_local_env:
                os.environ.pop(k, None)
    sync_local_files = list(Path(sync_local_tmp).glob("*.md"))
    results.append(_check(
        "sync-no-gemini-detail-page",
        len(sync_local_files) >= 1 and data.get("status") == "ok",
        f"files={[f.name for f in sync_local_files]}, status={data.get('status')}",
    ))

    # ── 26. analyze_email with None body_text → no crash ─────────── #
    ar_none_body = analyze_email("제목", "sender@test.com", None, "")
    results.append(_check(
        "analyze-none-body-no-crash",
        ar_none_body.source == "fallback",
        "None body_text must not raise AttributeError",
    ))

    return results


# ══════════════════════════════════════════════════════════════════════ #
#  Report
# ══════════════════════════════════════════════════════════════════════ #

_GREEN = "\033[92m"
_RED = "\033[91m"
_RESET = "\033[0m"
_BOLD = "\033[1m"


def print_report(results: list[QAResult]) -> bool:
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    print()
    print(f"{_BOLD}{'=' * 60}{_RESET}")
    print(f"{_BOLD}  TeamWorkHub QA Agent — {total} checks{_RESET}")
    print(f"{'=' * 60}")
    print()

    for r in results:
        icon = f"{_GREEN}PASS{_RESET}" if r.passed else f"{_RED}FAIL{_RESET}"
        print(f"  [{icon}] {r.name}")
        if r.detail:
            print(f"         {r.detail}")

    print()
    print(f"{'─' * 60}")
    if failed == 0:
        print(f"  {_GREEN}{_BOLD}ALL {passed} CHECKS PASSED{_RESET}")
    else:
        print(f"  {_GREEN}{passed} passed{_RESET}, {_RED}{failed} FAILED{_RESET}")
    print(f"{'─' * 60}")
    print()

    return failed == 0


# ══════════════════════════════════════════════════════════════════════ #
#  Pytest interface  (pytest scripts/qa_agent.py -v)
# ══════════════════════════════════════════════════════════════════════ #

_cached_results: list[QAResult] | None = None


def _get_results() -> list[QAResult]:
    global _cached_results
    if _cached_results is None:
        _cached_results = run_all_checks()
    return _cached_results


def _make_test(r: QAResult):
    def test_fn():
        assert r.passed, f"{r.name}: {r.detail}"
    test_fn.__name__ = f"test_{r.name.replace('-', '_')}"
    test_fn.__qualname__ = test_fn.__name__
    return test_fn


# Dynamically generate test functions for pytest discovery
for _r in run_all_checks():
    globals()[f"test_{_r.name.replace('-', '_')}"] = _make_test(_r)


# ══════════════════════════════════════════════════════════════════════ #
#  Standalone entry point
# ══════════════════════════════════════════════════════════════════════ #

if __name__ == "__main__":
    results = run_all_checks()
    ok = print_report(results)
    sys.exit(0 if ok else 1)
