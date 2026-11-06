"""Failure capture and persistence checks, with no external model requests."""
import json

import httpx
import pytest
from sqlalchemy import text

from test_flow import FixtureProvider, clean, client, generate_story, project
from app import worker
from app.api import login_attempts
from app.db import Job, Session, migrate, now
from app.diagnostics import provider_details
from app.provider import Ark, ProviderError


@pytest.fixture(autouse=True)
def reset_login_limit():
    login_attempts.clear()


@pytest.mark.parametrize("http_status,code,phrase,uncertain,retryable", [
    (400, "InputImageSensitiveContentDetected.PrivacyInformation", "真人", False, False),
    (429, "RateLimitExceeded", "限流", False, False),
    (503, "ServiceUnavailable", "暂时异常", True, False),
])
def test_submission_extracts_allowlisted_fields(monkeypatch, http_status, code, phrase, uncertain, retryable):
    monkeypatch.setattr("app.provider.ark_key", lambda: "fixture-secret")
    monkeypatch.setattr("app.diagnostics.ark_key", lambda: "fixture-secret")
    def handler(request):
        return httpx.Response(http_status, headers={"x-request-id": "request-123"}, json={
            "error": {"code": code, "message":
                      "input rejected fixture-secret Bearer private-value https://a.test/?token=secret"},
            "content": "private input must never be stored", "api_key": "another-secret",
        })
    provider = Ark()
    provider.client.close()
    provider.client = httpx.Client(base_url="https://example.test/", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as caught:
            provider.request("POST", "contents/generations/tasks", json={})
        error = caught.value
        assert error.uncertain is uncertain and error.retryable is retryable
        assert phrase in str(error)
        assert error.details["code"] == code
        assert error.details["request_id"] == "request-123"
        assert error.details["http_status"] == http_status
        saved = json.dumps(error.details)
        for forbidden in ("fixture-secret", "private-value", "https://a.test", "another-secret", "private input"):
            assert forbidden not in saved
    finally:
        provider.close()


@pytest.mark.parametrize("status", ["failed", "cancelled", "expired"])
def test_terminal_poll_keeps_error_and_embedded_request_id(monkeypatch, status):
    monkeypatch.setattr("app.provider.ark_key", lambda: "fixture-secret")
    provider = Ark()
    provider.client.close()
    provider.client = httpx.Client(base_url="https://example.test/", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            "status": status, "error": {"code": "OutputVideoSensitiveContentDetected.PolicyViolation",
            "message": "The output video may be related to copyright restrictions. Request id: request-456"},
        })))
    try:
        with pytest.raises(ProviderError) as caught:
            provider.poll("existing-task")
        assert caught.value.terminal
        assert caught.value.details["request_id"] == "request-456"
        assert caught.value.details["provider_status"] == status
        assert "版权" in caught.value.details["summary"]
    finally:
        provider.close()


def test_malformed_response_and_credentials_are_bounded(monkeypatch):
    monkeypatch.setattr("app.diagnostics.ark_key", lambda: "known-key")
    details = provider_details({"error": {"code": {"bad": "shape"}, "message":
                               "known-key api_key=abc password=pwd data:image/png;base64,abc " + "x" * 5000}})
    assert details["code"] is None and len(details["message"]) <= 1600
    assert all(value not in details["message"] for value in ("known-key", "abc", "pwd"))
    assert provider_details(["invalid"])["message"] is None
    monkeypatch.setattr("app.provider.ark_key", lambda: "fixture-secret")
    provider = Ark()
    provider.client.close()
    provider.client = httpx.Client(base_url="https://example.test/", transport=httpx.MockTransport(
        lambda request: httpx.Response(502, text="<html>private response</html>",
                                      headers={"x-tt-logid": "log-789"})))
    try:
        with pytest.raises(ProviderError) as caught:
            provider.request("GET", "contents/generations/tasks/existing")
        assert caught.value.retryable
        assert caught.value.details["request_id"] == "log-789"
        assert "private response" not in json.dumps(caught.value.details)
    finally:
        provider.close()


def test_worker_persists_exposes_and_clears_diagnostics(client, caplog):
    details = provider_details({"error": {"code": "OutputVideoSensitiveContentDetected.PolicyViolation",
                               "message": "copyright restrictions", "request_id": "req-persist"}}, status="failed")
    class Rejected(FixtureProvider):
        def poll(self, external_id):
            raise ProviderError(details["summary"], terminal=True, details=details)
    pid = project(client)
    story = generate_story(client, pid)
    job_id = client.post(f"/api/projects/{pid}/shots/{story['shots'][0]['id']}/generate", json={}).json()["id"]
    worker.execute(worker.claim(), FixtureProvider)
    with Session.begin() as db:
        db.get(Job, job_id).next_run = now()
    worker.execute(worker.claim(), Rejected)
    job = next(j for j in client.get(f"/api/projects/{pid}").json()["jobs"] if j["id"] == job_id)
    assert job["error_details"] == details
    assert job["status"] == "failed" and job["phase"] == "rejected"
    assert "req-persist" in caplog.text
    assert client.post(f"/api/jobs/{job_id}/recover", json={}).status_code == 409
    recovered = client.post(f"/api/jobs/{job_id}/recover", json={"acknowledge_duplicate_cost": True}).json()
    assert recovered["error"] is None and recovered["error_details"] is None


def test_success_and_cancellation_clear_previous_diagnostics(client):
    class Retry(FixtureProvider):
        def script(self, snapshot):
            raise ProviderError("限流", retryable=True, details=provider_details(
                {"error": {"code": "RateLimitExceeded", "message": "too many requests"}}, http_status=429))
    pid = project(client)
    job_id = client.post(f"/api/projects/{pid}/script", json={}).json()["id"]
    worker.execute(worker.claim(), Retry)
    with Session.begin() as db:
        db.get(Job, job_id).next_run = now()
    worker.execute(worker.claim(), FixtureProvider)
    with Session() as db:
        row = db.get(Job, job_id)
        assert row.status == "succeeded" and row.error is None and row.error_details is None
    job_id = client.post(f"/api/projects/{pid}/script", json={}).json()["id"]
    worker.execute(worker.claim(), Retry)
    cancelled = client.post(f"/api/jobs/{job_id}/cancel", json={}).json()
    assert cancelled["error"] is None and cancelled["error_details"] is None


def test_version_four_upgrade_preserves_legacy_error(client):
    pid = project(client)
    job_id = client.post(f"/api/projects/{pid}/script", json={}).json()["id"]
    with Session.begin() as db:
        job = db.get(Job, job_id)
        job.status = "failed"
        job.error = "模型服务返回 HTTP 400"
    with Session.begin() as db:
        db.execute(text("DROP TABLE asset_backups"))
        db.execute(text("ALTER TABLE jobs DROP COLUMN error_details"))
        db.execute(text("DELETE FROM schema_version WHERE version>=5"))
    try:
        migrate()
        migrate()  # Repeated API/Worker startup is safe.
        with Session() as db:
            row = db.get(Job, job_id)
            assert row.error == "模型服务返回 HTTP 400" and row.error_details is None
            assert db.execute(text("SELECT max(version) FROM schema_version")).scalar() == 6
    finally:
        migrate()
