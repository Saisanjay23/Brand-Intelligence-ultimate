"""Failure reports: what broke, which platform, which file and line, and how
to fix it -- for discovery jobs, analysis jobs and Scheduler runs.

Pure where it can be: the findings are built from a job's own record, so
they are tested against hand-built jobs. Sending is tested against the
suite-wide SMTP recorder in conftest.py, never a real server.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from backend.services import email_service, failure_alerts as FA
from backend.shared import diagnostics, resilience

# What the fixture below records instead of sending.
SENT_EMAILS: list[dict] = []


def _sweep(platform, keyword, tab="people", stopped="exhausted", outcome=resilience.SATISFIED,
           error="", where="", source="", hits=0):
    return types.SimpleNamespace(
        platform=platform, keyword=keyword, tab=tab, stopped=stopped, outcome=outcome,
        error=error, where=where, source=source, hits_found=hits)


def _job(history=(), platforms=None, keywords=("acme",)):
    return types.SimpleNamespace(
        id="job1", group_id="", status="done", message="3 found", found=3, new=1,
        history=list(history),
        keyword_plan=[types.SimpleNamespace(search=k) for k in keywords],
        platforms=platforms or {})


def _body(msg) -> str:
    """Both parts of a sent email, decoded (they travel base64-encoded)."""
    return chr(10).join(p.get_payload(decode=True).decode("utf-8")
                        for p in msg.walk() if not p.is_multipart())


def _prog(status, note=""):
    return types.SimpleNamespace(status=status, note=note)


@pytest.fixture
def settings_on(monkeypatch):
    state = {"alert_emails": ["ops@example.com"], "smtp_host": "smtp.example.com",
             "smtp_port": 587}

    async def _get():
        return dict(state)

    monkeypatch.setattr(FA.alert_settings_db, "get_settings", _get)
    monkeypatch.setattr(email_service.alert_settings_db, "get_settings", _get)
    SENT_EMAILS.clear()

    def _record(host, port, user, password, sender, recipients, msg, use_ssl=False):
        SENT_EMAILS.append({"to": list(recipients), "subject": msg["Subject"], "msg": msg})
        return True, "recorded"

    monkeypatch.setattr(email_service, "_send_smtp_sync", _record)
    return state


# ------------------------------------------------------------ discovery

class TestDiscoveryFindings:
    def test_broken_sweeps_are_grouped_with_location_cause_and_fix(self):
        job = _job([
            _sweep("twitter", "acme", stopped="stalled", outcome=resilience.BROKEN),
            _sweep("twitter", "acme corp", stopped="stalled", outcome=resilience.BROKEN),
            _sweep("twitter", "fine", hits=4),
        ])
        [f] = FA.discovery_findings(job)
        assert f["severity"] == FA.CRITICAL
        assert f["platform"] == "twitter"
        assert f["count"] == 2 and "acme [people]" in f["subjects"]
        # the line in the X engine that decides "stalled"
        assert f["where"].startswith("backend/platforms/twitter/discovery_engine.py:")
        assert f["cause"] and f["fix"]

    def test_a_raised_error_points_at_its_own_traceback_line(self):
        job = _job([_sweep("facebook", "acme", stopped="error", outcome=resilience.BROKEN,
                           error="KeyError: 'edges'",
                           where="backend/platforms/facebook/discovery_engine.py:400 in iter_results()")])
        [f] = FA.discovery_findings(job)
        assert f["where"].endswith("in iter_results()")
        assert f["error"] == "KeyError: 'edges'"

    def test_a_skipped_platform_lists_every_keyword_as_not_searched(self):
        job = _job(platforms={"instagram": _prog("skipped", "session expired")},
                   keywords=("acme", "acme corp"))
        [f] = FA.discovery_findings(job)
        assert f["title"] == "Platform was not searched at all"
        assert f["subjects"] == ["acme", "acme corp"]
        assert f["error"] == "session expired"

    def test_cancels_and_clean_sweeps_are_not_failures(self):
        job = _job([
            _sweep("twitter", "a", stopped="cancelled", outcome=resilience.TRUNCATED),
            _sweep("twitter", "b", stopped="cap:results"),
            _sweep("twitter", "c", stopped="no-results"),
        ])
        assert FA.discovery_findings(job) == []

    def test_a_time_budget_is_a_warning_not_a_failure(self):
        job = _job([_sweep("facebook", "a", stopped="cap:seconds",
                           outcome=resilience.TRUNCATED)])
        [f] = FA.discovery_findings(job)
        assert f["severity"] == FA.WARNING


class TestDiscoveryEmail:
    def test_a_broken_sweep_sends_one_email_naming_platform_file_and_fix(self, settings_on):
        job = _job([_sweep("twitter", "acme", stopped="login", outcome=resilience.BROKEN,
                           error="x search page shows a login wall")])
        assert asyncio.run(FA.notify_discovery(job)) is True
        [mail] = SENT_EMAILS
        assert "Discovery failures" in mail["subject"] and "Twitter" in mail["subject"]
        body = _body(mail["msg"])
        assert "twitter/discovery_engine.py" in body
        assert "Admin -> Sessions" in body          # the fix

    def test_warnings_alone_send_nothing(self, settings_on):
        job = _job([_sweep("facebook", "a", stopped="cap:seconds",
                           outcome=resilience.TRUNCATED)])
        assert asyncio.run(FA.notify_discovery(job)) is False
        assert SENT_EMAILS == []

    def test_the_toggle_turns_it_off(self, settings_on):
        settings_on["alert_on_discovery_failure"] = False
        job = _job([_sweep("twitter", "a", stopped="stalled", outcome=resilience.BROKEN)])
        assert asyncio.run(FA.notify_discovery(job)) is False
        assert SENT_EMAILS == []


# ------------------------------------------------------------- analysis

def _item(url, platform="twitter", status="done", row_status="OK", missed=(),
          error="", where=""):
    return types.SimpleNamespace(url=url, platform=platform, status=status,
                                 row_status=row_status, missed=list(missed),
                                 error=error, comments=error, where=where)


class TestAnalysisFindings:
    def test_missing_fields_and_failures_are_both_reported(self):
        job = types.SimpleNamespace(items=[
            _item("https://x.com/a", missed=["last post date"]),
            _item("https://x.com/b", missed=["last post date", "followers"]),
            _item("https://x.com/c", status="error", row_status="CHECKPOINT",
                  error="CHECKPOINT"),
            _item("https://x.com/d"),                           # clean
        ])
        found = {f["title"]: f for f in FA.analysis_findings(job)}
        last = found["Last post date not read"]
        assert last["count"] == 2
        assert last["where"].startswith("backend/platforms/twitter/analysis_engine.py:")
        assert "Account hit a checkpoint or rate limit while reading profiles" in found

    def test_a_gone_profile_is_a_finding_not_a_failure(self):
        job = types.SimpleNamespace(items=[
            _item("https://x.com/gone", status="error", row_status="GONE", error="GONE")])
        assert FA.analysis_findings(job) == []

    def test_an_analysis_email_is_sent_for_missing_data(self, settings_on):
        job = types.SimpleNamespace(
            id="a1", org_id="", status="done", message="1/1 scraped", total=1,
            items=[_item("https://x.com/a", missed=["followers"])])
        assert asyncio.run(FA.notify_analysis(job)) is True
        [mail] = SENT_EMAILS
        assert "Analysis issues" in mail["subject"]
        assert "https://x.com/a" in _body(mail["msg"])


# ------------------------------------------------------------ scheduler

class TestSchedulerEmail:
    def test_a_clean_run_sends_nothing(self, settings_on):
        entries = [{"client_id": "c1", "name": "C1", "status": "done", "owed": 0}]
        sent = asyncio.run(FA.notify_scheduler_run(
            run_id="r", trigger="scheduled", status="done", message="1 swept",
            entries=entries, findings_by_client={}))
        assert sent is False and SENT_EMAILS == []

    def test_owed_searches_send_one_email_for_the_run(self, settings_on):
        entries = [{"client_id": "c1", "name": "Acme", "status": "done", "owed": 3,
                    "found": 10, "new_profiles": 2, "message": "swept"},
                   {"client_id": "c2", "name": "Beta", "status": "skipped", "owed": 0,
                    "message": "nothing swept -- facebook: session expired"}]
        findings = {"c1": FA.discovery_findings(_job([
            _sweep("twitter", "acme", stopped="stalled", outcome=resilience.BROKEN)]))}
        assert asyncio.run(FA.notify_scheduler_run(
            run_id="r", trigger="scheduled", status="done", message="1 swept, 1 skipped",
            entries=entries, findings_by_client=findings)) is True
        [mail] = SENT_EMAILS
        body = _body(mail["msg"])
        assert "Acme" in body and "Beta" in body and "session expired" in body

    def test_a_missed_run_is_emailed(self, settings_on):
        assert asyncio.run(FA.notify_scheduler_missed(
            due_at=None, reason="the backend was not running")) is True
        assert "missed" in SENT_EMAILS[0]["subject"]


# -------------------------------------------------------------- rendering

class TestRendering:
    def test_everything_from_outside_is_escaped(self):
        html = email_service.render_failure_report(
            title="t", badge_text="b", headline="h", intro="i", summary=[("k", "<v>")],
            findings=[{"severity": "critical", "platform_name": "X", "title": "<b>t</b>",
                       "subjects": ["<script>"], "count": 1, "error": "a & b",
                       "where": "f.py:1", "cause": "c", "fix": "f"}])
        assert "<script>" not in html and "&lt;script&gt;" in html
        assert "a &amp; b" in html

    def test_long_lists_are_capped_but_counted(self):
        f = {"severity": "critical", "platform_name": "X", "title": "t",
             "subjects": [f"kw{i}" for i in range(40)], "count": 40}
        text = email_service.render_failure_report_text(
            headline="h", intro="i", summary=[], findings=[f], subject_cap=15)
        assert "(40)" in text and "and 25 more" in text


class TestSmtpRetry:
    def test_a_momentary_disconnect_is_retried_once(self, settings_on, monkeypatch):
        calls = []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                return False, "SMTPServerDisconnected: Connection unexpectedly closed"
            return True, "ok"

        monkeypatch.setattr(email_service, "_send_smtp_sync", flaky)
        monkeypatch.setattr(email_service, "_SMTP_RETRY_DELAY_S", 0.0)
        ok, _ = asyncio.run(email_service.send_email("s", "<p>h</p>"))
        assert ok and len(calls) == 2

    def test_a_bad_password_is_not_retried(self, settings_on, monkeypatch):
        calls = []

        def denied(*a, **kw):
            calls.append(1)
            return False, "SMTPAuthenticationError: (535, 'bad credentials')"

        monkeypatch.setattr(email_service, "_send_smtp_sync", denied)
        ok, _ = asyncio.run(email_service.send_email("s", "<p>h</p>"))
        assert not ok and len(calls) == 1


# ------------------------------------------------------------ diagnostics

class TestDiagnostics:
    def test_where_names_our_own_line(self):
        from backend.shared import resilience as R
        try:
            R.sweep_outcome(None, complete=object())  # runs fine; force a real raise below
            {}["missing"]
        except KeyError as e:
            loc = diagnostics.where(e)
        assert loc.startswith("backend/tests/test_failure_alerts.py:")

    def test_stop_sites_are_real_assignments_not_comments(self):
        for platform, code in (("youtube", "quota"), ("telegram", "flood-wait"),
                               ("tiktok", "geoblocked"), ("twitter", "stalled")):
            where = diagnostics.stop_site(platform, code)
            path, line = where.rsplit(":", 1)
            src = open(path, encoding="utf-8").read().splitlines()[int(line) - 1]
            assert f'"{code}"' in src and "stopped" in src, (platform, code, src)


# ------------------------------------------------------- the runner hooks

class TestTheJobsSendTheirOwnReports:
    @pytest.mark.asyncio
    async def test_a_job_where_every_platform_was_skipped_still_alerts(self, monkeypatch):
        from backend.discovery import runner as R
        from backend.tests.test_keyword_coverage import FakeLedger

        ledger = FakeLedger()
        for name in ("plan", "record", "miss", "owed"):
            monkeypatch.setattr(R.coverage_db, name, getattr(ledger, name))
        sent = []

        async def _notify(job):
            sent.append(job)

        monkeypatch.setattr(FA, "notify_discovery", _notify)
        runner = R.DiscoveryRunner()

        async def _no_client(group_id):
            return None

        async def _readiness(only=None):
            return [], {"facebook": "no usable session"}

        monkeypatch.setattr(runner, "_client", _no_client)
        monkeypatch.setattr(runner, "platform_readiness", _readiness)
        job, _ = await runner.start(group_id="c1", individual_keywords=["kw"],
                                    domain_keywords=[])
        await asyncio.sleep(0)
        assert sent == [job]
        [f] = FA.discovery_findings(job)
        assert f["title"] == "Platform was not searched at all"

    @pytest.mark.asyncio
    async def test_a_finished_job_alerts_unless_the_scheduler_owns_it(self, monkeypatch):
        from backend.discovery import runner as R
        from backend.tests.test_keyword_coverage import FakeLedger

        ledger = FakeLedger()
        for name in ("plan", "record", "miss", "owed"):
            monkeypatch.setattr(R.coverage_db, name, getattr(ledger, name))

        async def _noop(*a, **kw):
            return None

        monkeypatch.setattr(R.telemetry_db, "record_sweeps", _noop)
        sent = []

        async def _notify(job):
            sent.append(job)

        monkeypatch.setattr(FA, "notify_discovery", _notify)

        async def _no_client(group_id):
            return None

        async def _readiness(only=None):
            return ["twitter"], {}

        async def _sweep_platform(job, pid, *a, **kw):
            job.history.append(R.CompletedSweep(
                platform=pid, display_name="X", keyword="kw", tab="people",
                duration_seconds=1.0, hits_found=0, hits_new=0, timestamp="00:00:00",
                complete=False, stopped="stalled", outcome=resilience.BROKEN))
            job.platforms[pid].status = "partial"

        for notify in (True, False):
            sent.clear()
            runner = R.DiscoveryRunner()
            monkeypatch.setattr(runner, "_client", _no_client)
            monkeypatch.setattr(runner, "platform_readiness", _readiness)
            monkeypatch.setattr(runner, "_sweep_platform", _sweep_platform)
            monkeypatch.setattr(runner, "_maybe_report", _noop)
            job, _ = await runner.start(group_id="c1", individual_keywords=["kw"],
                                        domain_keywords=[], notify_failures=notify)
            await job.task
            await asyncio.sleep(0)
            assert sent == ([job] if notify else [])
        assert FA.discovery_findings(job)[0]["title"].startswith("Search stopped")


class TestAnalysisRecordsWhatItMissed:
    @pytest.mark.asyncio
    async def test_blank_fields_after_a_good_visit_are_recorded(self):
        from backend.analysis import runner as AR
        from backend.shared.models.row import Row

        it = AR.AnalysisItem(id="i", raw_url="https://x.com/a", url="https://x.com/a",
                             platform="twitter", entity_id="a")
        job = AR.AnalysisJob(id="j", items=[it], total=1)
        row = Row(url=it.url, target="", status="OK", profile_name="Someone")
        await AR.AnalysisRunner()._populate(job, it, row, None)
        assert set(it.missed) == {"followers", "last post date", "screenshot"}

    @pytest.mark.asyncio
    async def test_a_field_discovery_already_had_is_not_missing(self):
        from backend.analysis import runner as AR
        from backend.shared.models.row import Row

        it = AR.AnalysisItem(id="i", raw_url="https://x.com/a", url="https://x.com/a",
                             platform="twitter", entity_id="a")
        job = AR.AnalysisJob(id="j", items=[it], total=1)
        row = Row(url=it.url, target="", status="OK", profile_name="Someone",
                  last_post_iso="2026-09-01", screenshot_bytes=b"png")
        await AR.AnalysisRunner()._populate(job, it, row, {"followers": 12})
        assert it.missed == []
