"""Script timeout, cancellation and stale-output regression checks; no Ark calls."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest

from test_flow import FORM, SCRIPT, FixtureProvider, clean, client, generate_story, project
from app import worker
from app.db import Job, Project, Session, now
from app.job_policy import SCRIPT_MAX_RETRIES, SCRIPT_TIMEOUT_SECONDS
from app.provider import Ark, ProviderError, SCRIPT_READ_TIMEOUT_SECONDS


@pytest.fixture(autouse=True)
def reset_login_limit():
    from app.api import login_attempts
    login_attempts.clear()


@pytest.mark.parametrize("status", ["queued", "waiting", "running"])
def test_cancel_unlocks_project_and_cannot_be_recovered(client, status):
    pid = project(client)
    job_id = client.post(f"/api/projects/{pid}/script", json={}).json()["id"]
    with Session.begin() as db:
        job = db.get(Job, job_id)
        job.status = status
        job.owner = "old-owner"
        job.lease_until = now() + timedelta(seconds=60)
    response = client.post(f"/api/jobs/{job_id}/cancel", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{job_id}/cancel", json={}).status_code == 200
    assert client.post(f"/api/jobs/{job_id}/recover", json={}).status_code == 409
    assert client.get("/api/projects").json()[0]["active_jobs"] == 0
    assert client.put(f"/api/projects/{pid}", json={**FORM, "name": "可再次编辑"}).status_code == 200
    assert client.post(f"/api/projects/{pid}/script", json={}).status_code == 200
    with Session() as db:
        old = db.get(Job, job_id)
        assert old.owner is None and old.lease_until is None


def test_cancel_rejects_anonymous_missing_finished_and_video_jobs(client):
    from fastapi.testclient import TestClient
    from app.api import app
    with TestClient(app) as anonymous:
        assert anonymous.post("/api/jobs/unknown/cancel", json={}).status_code == 401
    assert client.post("/api/jobs/unknown/cancel", json={}).status_code == 404
    pid = project(client)
    story = generate_story(client, pid)
    finished = story["jobs"][0]["id"]
    assert client.post(f"/api/jobs/{finished}/cancel", json={}).status_code == 409
    video = client.post(f"/api/projects/{pid}/shots/{story['shots'][0]['id']}/generate", json={}).json()
    assert client.post(f"/api/jobs/{video['id']}/cancel", json={}).status_code == 409
    token = client.headers.pop("X-CSRF-Token")
    assert client.post(f"/api/jobs/{video['id']}/cancel", json={}).status_code == 403
    client.headers["X-CSRF-Token"] = token


def test_cancelled_inflight_result_cannot_overwrite_new_story(client):
    pid = project(client)
    job_id = client.post(f"/api/projects/{pid}/script", json={}).json()["id"]
    started, release = threading.Event(), threading.Event()

    class SlowProvider(FixtureProvider):
        def script(self, snapshot):
            started.set()
            assert release.wait(10)
            return {**SCRIPT, "title": "过期结果"}

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.execute, worker.claim(), SlowProvider)
        try:
            assert started.wait(5)
            assert client.post(f"/api/jobs/{job_id}/cancel", json={}).status_code == 200
            assert client.post(f"/api/projects/{pid}/script", json={}).status_code == 200
            worker.execute(worker.claim(), FixtureProvider)
        finally:
            release.set()
        future.result(timeout=10)
    with Session() as db:
        assert db.get(Job, job_id).status == "cancelled"
        assert db.get(Project, pid).script["title"] == SCRIPT["title"]


def test_script_retries_stop_and_recovery_gets_fresh_budget(client):
    class FailingProvider(FixtureProvider):
        def script(self, snapshot):
            raise ProviderError("模型服务读取响应超时", retryable=True)

    pid = project(client)
    job_id = client.post(f"/api/projects/{pid}/script", json={}).json()["id"]
    for failure in range(1, SCRIPT_MAX_RETRIES + 2):
        worker.execute(worker.claim(), FailingProvider)
        with Session.begin() as db:
            job = db.get(Job, job_id)
            assert job.attempts == failure
            assert job.status == ("waiting" if failure <= SCRIPT_MAX_RETRIES else "failed")
            job.next_run = now()
    assert worker.claim() is None
    with Session.begin() as db:
        db.get(Job, job_id).created_at = now() - timedelta(days=1)
    response = client.post(f"/api/jobs/{job_id}/recover", json={})
    assert response.status_code == 200 and response.json()["attempts"] == 0
    worker.execute(worker.claim(), FixtureProvider)
    assert client.get(f"/api/projects/{pid}").json()["jobs"][0]["status"] == "succeeded"


@pytest.mark.parametrize("status", ["queued", "waiting", "running"])
def test_old_script_jobs_expire_across_restarts(client, status):
    pid = project(client)
    job_id = client.post(f"/api/projects/{pid}/script", json={}).json()["id"]
    with Session.begin() as db:
        job = db.get(Job, job_id)
        job.status = status
        job.created_at = now() - timedelta(seconds=SCRIPT_TIMEOUT_SECONDS + 1)
        job.next_run = now() + timedelta(hours=1)
        job.owner = "old-owner"
        job.lease_until = now() + timedelta(hours=1)
    assert worker.claim() is None
    result = client.get(f"/api/projects/{pid}").json()["jobs"][0]
    assert result["status"] == "failed" and result["phase"] == "timeout"
    assert client.get("/api/projects").json()[0]["active_jobs"] == 0


def test_expired_inflight_result_does_not_replace_story(client):
    pid = project(client)
    job_id = client.post(f"/api/projects/{pid}/script", json={}).json()["id"]
    job = worker.claim()
    with Session.begin() as db:
        db.get(Job, job_id).created_at = now() - timedelta(seconds=SCRIPT_TIMEOUT_SECONDS + 1)
    worker.execute(job, FixtureProvider)
    result = client.get(f"/api/projects/{pid}").json()
    assert not result["shots"] and result["jobs"][0]["phase"] == "timeout"


def test_cancel_rechecks_completion_after_waiting_for_project_lock(client, monkeypatch):
    from app import api
    pid = project(client)
    job_id = client.post(f"/api/projects/{pid}/script", json={}).json()["id"]
    lock_project = api.project_locked

    def finish_before_lock(db, project_id, **kwargs):
        # Simulates completion between the cancellation's initial read and lock.
        with Session.begin() as other:
            job = other.get(Job, job_id)
            job.status = "succeeded"
            job.phase = "done"
        return lock_project(db, project_id, **kwargs)

    monkeypatch.setattr(api, "project_locked", finish_before_lock)
    assert client.post(f"/api/jobs/{job_id}/cancel", json={}).status_code == 409
    with Session() as db:
        assert db.get(Job, job_id).status == "succeeded"


@pytest.mark.parametrize("exception,label", [(httpx.ReadTimeout, "读取响应超时"),
                                           (httpx.ConnectTimeout, "连接超时"),
                                           (httpx.ConnectError, "连接失败")])
def test_provider_errors_log_reason_without_secrets(monkeypatch, caplog, exception, label):
    monkeypatch.setattr("app.provider.ark_key", lambda: "fixture-private-key")
    provider = Ark()
    provider.client.close()
    def handler(request):
        raise exception("fixture-private-key must not be logged", request=request)
    provider.client = httpx.Client(base_url="https://example.test/", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as caught:
            provider.request("POST", "chat/completions", json={})
        assert label in str(caught.value) and caught.value.retryable
        assert exception.__name__ in caplog.text and "elapsed=" in caplog.text
        assert "fixture-private-key" not in caplog.text + str(caught.value)
        with pytest.raises(ProviderError) as submit:
            provider.request("POST", "contents/generations/tasks", json={})
        assert submit.value.uncertain and not submit.value.retryable
    finally:
        provider.close()


def test_script_request_has_longer_bounded_read_timeout(monkeypatch):
    monkeypatch.setattr("app.provider.ark_key", lambda: "fixture-private-key")
    captured = []
    def handler(request):
        captured.append(request.extensions["timeout"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(SCRIPT)}}]})
    provider = Ark()
    provider.client.close()
    provider.client = httpx.Client(base_url="https://example.test/", transport=httpx.MockTransport(handler))
    snapshot = {"script_model": "fixture-script", "project": {"duration": 15}}
    try:
        provider.script(snapshot)
        assert captured[-1]["read"] == SCRIPT_READ_TIMEOUT_SECONDS
        provider.script({**snapshot, "_script_deadline": (now() + timedelta(seconds=20)).isoformat()})
        assert 0 < captured[-1]["read"] <= 20
        with pytest.raises(ProviderError):
            provider.script({**snapshot, "_script_deadline": (now() - timedelta(seconds=1)).isoformat()})
        assert len(captured) == 2
    finally:
        provider.close()
