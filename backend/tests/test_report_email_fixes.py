"""Reports and email: no clean zero where something actually went wrong.

WHAT THESE GUARD

  1. A REPORT FOR A CLIENT THAT DOES NOT EXIST IS A 404. `build_client_report`
     used to swallow the not-found, so `GET /reports/client/<typo>` answered
     200 with an all-zeros report and `/send` mailed it. A deleted client
     that still has profiles keeps its report.

  2. THE COMBINED DIGEST NAMES A CLIENT IT COULD NOT READ. It used to log a
     warning and leave the client out, so the totals quietly shrank.

  3. A PARTIALLY REFUSED SEND IS NOT "SENT SUCCESSFULLY". `sendmail`
     returns the recipients it refused; that return was thrown away.

  4. THE LIVENESS PROBE DOES NOT CLAIM MONGO IS UP. It never checks.

  5. REPORTS GO TO `report_emails`, falling back to `alert_emails` when it
     is empty. The setting used to be stored and never read.

No Mongo and no network: repositories and the SMTP connection are faked.
"""

from __future__ import annotations

import pytest

from backend.services import email_service, report_service
from backend.shared.errors import NotFoundError


# ------------------------------------------------------------ 1. unknown id

def _fake_profiles(monkeypatch, total: int):
    async def find(client_id, **kw):
        return [], total, None
    monkeypatch.setattr(report_service.profiles_db, "find", find)


def _no_client(monkeypatch):
    async def get(client_id):
        raise NotFoundError(f"client {client_id!r} not found")
    monkeypatch.setattr(report_service.clients_db, "get", get)


@pytest.mark.asyncio
class TestUnknownClient:
    async def test_an_id_with_no_client_and_no_profiles_is_not_found(self, monkeypatch):
        _no_client(monkeypatch)
        _fake_profiles(monkeypatch, total=0)
        with pytest.raises(NotFoundError):
            await report_service.build_client_report("typo")
        assert NotFoundError.status_code == 404

    async def test_sending_it_is_refused_too_never_mailed(self, monkeypatch):
        _no_client(monkeypatch)
        _fake_profiles(monkeypatch, total=0)
        sent = []

        async def send_email(*a, **kw):
            sent.append(a)
            return True, "sent"
        monkeypatch.setattr(report_service.email_service, "send_email", send_email)
        with pytest.raises(NotFoundError):
            await report_service.send_client_report("typo")
        assert sent == []

    async def test_a_deleted_client_with_profiles_still_reports(self, monkeypatch):
        _no_client(monkeypatch)
        _fake_profiles(monkeypatch, total=5)
        rep = await report_service.build_client_report("gone-but-has-data")
        assert rep["name"] == "gone-but-has-data"
        assert rep["pending"]["total"] == 5


# ------------------------------------------------------- 2. combined digest

def _client_rep(cid, new=1):
    return {"client_id": cid, "name": cid.upper(), "generated_at": "x",
            "validated": {"new": new, "delta": 2, "total": new + 2},
            "pending": {"new": 0, "total": 4}, "logo_matches": 1, "samples": []}


@pytest.fixture
def digest_with_one_failure(monkeypatch):
    async def list_all():
        return [{"client_id": "ok", "name": "OK"}, {"client_id": "bad", "name": "Bad Co"}]

    async def build(cid):
        if cid == "bad":
            raise RuntimeError("aggregation timed out")
        return _client_rep(cid)

    monkeypatch.setattr(report_service.clients_db, "list_all", list_all)
    monkeypatch.setattr(report_service, "build_client_report", build)


@pytest.mark.asyncio
class TestCombinedDigestNamesFailures:
    async def test_the_failed_client_is_listed_and_counted(self, digest_with_one_failure):
        rep = await report_service.build_combined_report()
        assert [c["client_id"] for c in rep["clients"]] == ["ok"]
        assert [f["client_id"] for f in rep["failed"]] == ["bad"]
        assert "aggregation timed out" in rep["failed"][0]["error"]
        assert rep["totals"]["failed"] == 1

    async def test_the_html_and_the_email_subject_say_so(self, digest_with_one_failure, monkeypatch):
        rep = await report_service.build_combined_report()
        html = report_service.render_combined_html(rep)
        assert "Bad Co" in html
        assert "1 client could not be read" in html

        sent = {}

        async def send_email(subject, html, text=None, to=None, **kw):
            sent.update(subject=subject, text=text, kw=kw)
            return True, "sent"
        monkeypatch.setattr(report_service.email_service, "send_email", send_email)
        await report_service.send_combined_report()
        assert "1 client could not be read" in sent["subject"]
        assert "Bad Co" in sent["text"]

    async def test_a_complete_digest_carries_no_warning(self, monkeypatch):
        async def list_all():
            return [{"client_id": "ok"}]

        async def build(cid):
            return _client_rep(cid)
        monkeypatch.setattr(report_service.clients_db, "list_all", list_all)
        monkeypatch.setattr(report_service, "build_client_report", build)
        rep = await report_service.build_combined_report()
        assert rep["failed"] == [] and rep["totals"]["failed"] == 0
        assert "could not be read" not in report_service.render_combined_html(rep)


# --------------------------------------------------- 3. partial SMTP refusal

class _FakeSMTP:
    """An SMTP connection that accepts the message for some recipients and
    refuses the rest -- exactly what smtplib's `sendmail` reports by
    RETURNING the refusals rather than raising."""

    refuse: dict = {}
    sends: list = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self, *a, **kw):
        return (250, b"ok")

    def has_extn(self, name):
        return False

    def login(self, *a, **kw):
        return (235, b"ok")

    def sendmail(self, sender, recipients, text):
        _FakeSMTP.sends.append(list(recipients))
        return dict(_FakeSMTP.refuse)


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.refuse = {}
    _FakeSMTP.sends = []
    monkeypatch.setattr(email_service.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(email_service.smtplib, "SMTP_SSL", _FakeSMTP)
    return _FakeSMTP


def _settings(monkeypatch, **over):
    doc = {"alert_emails": ["alert@x.test"], "report_emails": [],
           "smtp_host": "smtp.x.test", "smtp_port": 25}
    doc.update(over)

    async def get_settings():
        return dict(doc)
    monkeypatch.setattr(email_service.alert_settings_db, "get_settings", get_settings)


class TestPartialRefusal:
    def test_a_partial_refusal_is_not_reported_as_sent(self, fake_smtp):
        from email.mime.multipart import MIMEMultipart
        fake_smtp.refuse = {"b@x.test": (550, b"no such user")}
        ok, detail = email_service._send_smtp_sync(
            "smtp.x.test", 25, "", "", "from@x.test", ["a@x.test", "b@x.test"], MIMEMultipart())
        assert ok is False
        assert "b@x.test" in detail and "550 no such user" in detail
        assert "1 of 2" in detail
        assert "a@x.test" not in detail.split("refused:")[1]

    def test_a_clean_send_is_still_a_success(self, fake_smtp):
        from email.mime.multipart import MIMEMultipart
        ok, detail = email_service._send_smtp_sync(
            "smtp.x.test", 25, "", "", "from@x.test", ["a@x.test"], MIMEMultipart())
        assert ok is True

    @pytest.mark.asyncio
    async def test_a_partial_refusal_is_never_retried(self, fake_smtp, monkeypatch):
        """A 4xx refusal reads as transient ("try again"), but the accepted
        recipients already have the mail -- a retry would send it twice."""
        _settings(monkeypatch, alert_emails=["a@x.test", "b@x.test"])
        monkeypatch.setattr(email_service, "_SMTP_RETRY_DELAY_S", 0.0)
        fake_smtp.refuse = {"b@x.test": (451, b"temporarily unavailable, try again")}
        ok, detail = await email_service.send_email("s", "<p>h</p>")
        assert ok is False and "b@x.test" in detail
        assert len(fake_smtp.sends) == 1


# ------------------------------------------------------------ 4. liveness

@pytest.mark.asyncio
async def test_liveness_does_not_claim_mongo_is_up():
    from backend.api import health
    body = await health.live()
    assert body["status"] == "ok"
    assert body["mongo"] is None, "the liveness probe never checks Mongo -- it must not say True"
    # and the response model accepts the null rather than coercing it
    assert health.LiveHealth(**body).mongo is None


# --------------------------------------------------- 5. report recipients

@pytest.mark.asyncio
class TestReportRecipients:
    async def test_reports_go_to_report_emails(self, fake_smtp, monkeypatch):
        _settings(monkeypatch, report_emails=["reports@x.test"])
        ok, _ = await email_service.send_email("s", "<p>h</p>", report=True)
        assert ok and fake_smtp.sends == [["reports@x.test"]]

    async def test_reports_fall_back_to_alert_emails(self, fake_smtp, monkeypatch):
        _settings(monkeypatch, report_emails=[])
        ok, _ = await email_service.send_email("s", "<p>h</p>", report=True)
        assert ok and fake_smtp.sends == [["alert@x.test"]]

    async def test_alerts_still_go_to_alert_emails(self, fake_smtp, monkeypatch):
        _settings(monkeypatch, report_emails=["reports@x.test"])
        ok, _ = await email_service.send_email("s", "<p>h</p>")
        assert ok and fake_smtp.sends == [["alert@x.test"]]

    async def test_the_report_service_sends_as_a_report(self, monkeypatch):
        async def build(cid):
            return _client_rep(cid)
        seen = {}

        async def send_email(*a, **kw):
            seen.update(kw)
            return True, "sent"
        monkeypatch.setattr(report_service, "build_client_report", build)
        monkeypatch.setattr(report_service.email_service, "send_email", send_email)
        await report_service.send_client_report("ok")
        assert seen.get("report") is True
