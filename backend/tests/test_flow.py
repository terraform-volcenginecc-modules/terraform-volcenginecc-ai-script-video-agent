"""Offline integration verification against an isolated real PostgreSQL and FFmpeg.

Provider fixtures are injected only here. There is no mock mode in the app.
"""
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

assert os.environ["DATABASE_URL"].rsplit("/", 1)[-1] == "animation_test", "必须使用隔离验证数据库"

from app import worker
from app.api import app
from app.config import MEDIA, secret
from app.db import Asset, Job, Project, Session, Shot, migrate, now, uid
from app.media import allowed_url, compose, path_for, probe, run
from app.provider import Ark, ProviderError

SPEC = {"duration": 4, "scene": "离线测试场景", "characters": ["测试角色"], "action": "走过画面",
        "camera": "固定中景", "dialogue": "", "sound": "环境音"}
SCRIPT = {"title": "离线验证 · 非 AI 生成", "synopsis": "仅用于验证业务状态和媒体流程",
          "style_bible": "离线测试素材", "characters": [{"name": "测试角色", "description": "测试人物"}],
          "shots": [SPEC.copy(), SPEC.copy()]}
FORM = {"name": "离线闭环验证", "brief": "验证生成剧本到合成视频的完整状态流程", "duration": 15,
        "ratio": "16:9", "style": "测试", "characters": "", "script_model": "fixture-script",
        "video_model": "fixture-video"}


class FixtureProvider:
    submits = 0
    def close(self): pass
    def script(self, snapshot): return SCRIPT
    def submit(self, snapshot):
        FixtureProvider.submits += 1
        return "fixture-" + uid()
    def poll(self, external_id): return "fixture://video"


@pytest.fixture(autouse=True)
def clean():
    migrate()
    with Session.begin() as db:
        db.execute(text("TRUNCATE jobs, shots, assets, projects CASCADE"))
    FixtureProvider.submits = 0
    yield


@pytest.fixture
def client():
    # Each test has an independent login budget; production rate limits stay intact.
    from app.api import login_attempts
    login_attempts.clear()
    with TestClient(app) as client:
        response = client.post("/api/login", json={"password": secret("owner_password")})
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf"]
        yield client


@pytest.fixture(scope="session")
def clips():
    MEDIA.mkdir(parents=True, exist_ok=True)
    first, second = MEDIA / "fixture-silent.mp4", MEDIA / "fixture-audio.mp4"
    run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
         "testsrc2=size=320x240:rate=15", "-t", "1", "-c:v", "libx264", "-threads", "2", str(first)])
    run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
         "color=c=green:size=480x270:rate=30", "-f", "lavfi", "-i",
         "sine=frequency=440:sample_rate=44100", "-t", "1.2", "-c:v", "libx264",
         "-threads", "2", "-c:a", "aac", str(second)])
    return first, second


def fixture_downloader(clip):
    def copy(url, target):
        assert url == "fixture://video"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(clip, target)
    return copy


def project(client):
    response = client.post("/api/projects", json=FORM)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def generate_story(client, pid):
    response = client.post(f"/api/projects/{pid}/script", json={})
    assert response.status_code == 200
    worker.execute(worker.claim(), FixtureProvider)
    result = client.get(f"/api/projects/{pid}").json()
    assert len(result["shots"]) == 2
    return result


def process_all(clip):
    for _ in range(8):
        with Session.begin() as db:
            db.execute(text("UPDATE jobs SET next_run = now() WHERE status = 'waiting'"))
        job = worker.claim()
        if not job:
            return
        worker.execute(job, FixtureProvider, fixture_downloader(clip))
    raise AssertionError("任务未收敛")


def test_end_to_end_and_regenerate(client, clips):
    pid = project(client)
    story = generate_story(client, pid)
    assert client.post(f"/api/projects/{pid}/videos", json={}).status_code == 200
    with Session() as db:
        assert len(list(db.scalars(select(Job).where(Job.kind == "video")))) == 2
    # POST returns and persists external ID; a fresh claim continues GET rather than POST.
    worker.execute(worker.claim(), FixtureProvider)
    with Session() as db:
        submitted = db.scalar(select(Job).where(Job.status == "waiting"))
        assert submitted.external_id.startswith("fixture-") and submitted.phase == "polling"
    process_all(clips[0])
    assert FixtureProvider.submits == 2
    data = client.get(f"/api/projects/{pid}").json()
    assert all(s["status"] == "succeeded" for s in data["shots"])
    before = [s["asset_id"] for s in data["shots"]]
    assert client.post(f"/api/projects/{pid}/compose", json={}).status_code == 200
    process_all(clips[0])
    result = client.get(f"/api/projects/{pid}").json()
    assert result["export_asset"]
    asset_response = client.get("/api/assets/" + result["export_asset"])
    assert asset_response.status_code == 200 and asset_response.content[4:8] == b"ftyp"
    first = story["shots"][0]["id"]
    assert client.post(f"/api/projects/{pid}/shots/{first}/generate", json={}).status_code == 200
    process_all(clips[1])
    after = client.get(f"/api/projects/{pid}").json()
    assert after["shots"][0]["asset_id"] != before[0]
    assert after["shots"][1]["asset_id"] == before[1]
    assert after["export_asset"] is None
    assert FixtureProvider.submits == 3
    assert client.post(f"/api/projects/{pid}/compose", json={}).status_code == 200
    process_all(clips[0])
    with Session() as db:
        export = db.get(Asset, db.get(Project, pid).export_asset)
        duration, info = probe(path_for(export.path))
    assert 2.1 <= duration <= 2.5
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"], video["r_frame_rate"]) == (1280, 720, "24/1")
    assert any(s["codec_type"] == "audio" and s["sample_rate"] == "48000" for s in info["streams"])


def test_auth_csrf_validation_and_project_lock(client):
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/projects").status_code == 401
    pid = project(client)
    token = client.headers.pop("X-CSRF-Token")
    assert client.post(f"/api/projects/{pid}/script", json={}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert client.post("/api/projects", json=FORM).status_code == 200
    story = generate_story(client, pid)
    shot = story["shots"][0]
    invalid = {**SPEC, "characters": ["不存在的角色"]}
    assert client.put(f"/api/projects/{pid}/shots/{shot['id']}", json=invalid).status_code == 422
    assert client.put(f"/api/projects/{pid}/shots/{shot['id']}", json={**SPEC, "duration":16}).status_code == 422
    assert client.post(f"/api/projects/{pid}/videos", json={}).status_code == 200
    assert client.put(f"/api/projects/{pid}", json=FORM).status_code == 409
    assert client.post(f"/api/projects/{pid}/compose", json={}).status_code == 409
    assert client.post("/api/login", json={"password": "wrong"}, headers={"Origin":"https://foreign.example"}).status_code == 403


def test_expired_submission_is_uncertain_and_manual_id_resumes(client, clips):
    pid = project(client)
    story = generate_story(client, pid)
    shot_id = story["shots"][0]["id"]
    response = client.post(f"/api/projects/{pid}/shots/{shot_id}/generate", json={})
    job_id = response.json()["id"]
    claimed = worker.claim()
    with Session.begin() as db:
        row = db.get(Job, claimed.id)
        row.phase = "submitting"
        row.lease_until = now() - timedelta(seconds=1)
    assert worker.claim() is None
    with Session() as db:
        assert db.get(Job, job_id).status == "uncertain"
    assert client.post(f"/api/jobs/{job_id}/recover", json={}).status_code == 409
    assert client.post(f"/api/jobs/{job_id}/recover", json={"external_id":"verified-external-id"}).status_code == 200
    process_all(clips[0])
    assert FixtureProvider.submits == 0
    assert client.get(f"/api/projects/{pid}").json()["shots"][0]["status"] == "succeeded"


def test_network_timeout_never_automatically_resubmits(client):
    class Timeout(FixtureProvider):
        def submit(self, snapshot):
            FixtureProvider.submits += 1
            raise ProviderError("测试提交超时", uncertain=True)
    pid = project(client)
    story = generate_story(client, pid)
    response = client.post(f"/api/projects/{pid}/shots/{story['shots'][0]['id']}/generate", json={})
    worker.execute(worker.claim(), Timeout)
    assert worker.claim() is None
    assert FixtureProvider.submits == 1
    assert client.post(f"/api/jobs/{response.json()['id']}/recover", json={"acknowledge_duplicate_cost":True}).status_code == 200


def test_concurrent_claims_and_stale_result_fencing(client):
    pid = project(client)
    generate_story(client, pid)
    client.post(f"/api/projects/{pid}/videos", json={})
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _:worker.claim(), range(2)))
    assert all(jobs) and jobs[0].id != jobs[1].id
    old = jobs[0]
    with Session.begin() as db:
        db.get(Job, old.id).lease_until = now() - timedelta(seconds=1)
    replacement = worker.claim()
    assert replacement.id == old.id and replacement.owner != old.owner
    with pytest.raises(worker.LostLease):
        worker.save(old, lambda db,row:worker.finish(row))
    with Session() as db:
        assert db.get(Job, old.id).owner == replacement.owner


def test_transient_poll_keeps_external_id(client, clips):
    class RateLimited(FixtureProvider):
        def poll(self, external_id): raise ProviderError("测试429", retryable=True)
    pid = project(client)
    story = generate_story(client, pid)
    client.post(f"/api/projects/{pid}/shots/{story['shots'][0]['id']}/generate", json={})
    worker.execute(worker.claim(), FixtureProvider)
    with Session.begin() as db:
        db.execute(text("UPDATE jobs SET next_run=now() WHERE status='waiting'"))
    worker.execute(worker.claim(), RateLimited)
    with Session() as db:
        row = db.scalar(select(Job).where(Job.kind=="video"))
        assert row.status=="waiting" and row.external_id and row.attempts==1
    process_all(clips[0])
    assert FixtureProvider.submits == 1


def test_download_restrictions_and_real_normalization(clips):
    with pytest.raises(ValueError): allowed_url("http://127.0.0.1/private")
    with pytest.raises(ValueError): allowed_url("https://evil.example/video.mp4")
    with pytest.raises(ValueError): path_for("../escape")
    output = MEDIA / "mixed" / "film.mp4"
    compose(list(clips), output, "9:16")
    duration, info = probe(output)
    assert 2.1 <= duration <= 2.5
    video = next(s for s in info["streams"] if s["codec_type"]=="video")
    assert (video["width"], video["height"]) == (720,1280)


def test_ark_wire_protocol_without_network(monkeypatch):
    monkeypatch.setattr("app.provider.ark_key", lambda:"fixture-secret")
    requests = []
    def handler(request):
        requests.append(request)
        if request.method=="POST":
            data = json.loads(request.content)
            assert data["model"]=="fixture-video"
            assert data["duration"]==4 and data["generate_audio"] is True
            return httpx.Response(200,json={"id":"task-wire-fixture"})
        assert request.url.path.endswith("/task-wire-fixture")
        return httpx.Response(200,json={"status":"succeeded","content":{"video_url":"https://example.volces.com/video.mp4"}})
    provider = Ark()
    provider.client.close()
    provider.client = httpx.Client(base_url="https://example.test/api/v3/",transport=httpx.MockTransport(handler))
    snap = {"shot":SPEC,"script":SCRIPT,"video_model":"fixture-video","ratio":"16:9"}
    assert provider.submit(snap)=="task-wire-fixture"
    assert provider.poll("task-wire-fixture")["video_url"].endswith("video.mp4")
    assert len(requests)==2
    provider.close()


def test_reference_frame_snapshot_cut_and_upstream_change(client, clips):
    pid = project(client)
    assert client.put(f"/api/projects/{pid}", json={**FORM, "video_model":"doubao-seedance-2-0-260128"}).status_code == 200
    generate_story(client, pid)
    assert client.post(f"/api/projects/{pid}/videos", json={}).status_code == 200
    process_all(clips[0])
    first, second = client.get(f"/api/projects/{pid}").json()["shots"]
    response = client.post(f"/api/projects/{pid}/shots/{second['id']}/reference-frame", json={
        "source_shot_id":first["id"], "source_asset_id":first["asset_id"], "frame_index":8,
    })
    assert response.status_code == 200, response.text
    frame = response.json()
    assert frame["frame_index"] == 8 and frame["out_frame_exclusive"] == 9
    assert frame["frame_count"] == 24 and (frame["width"],frame["height"]) == (1280,720)
    assert client.get(f"/api/assets/{frame['asset_id']}").headers["content-type"] == "image/png"
    assert client.get(f"/api/assets/{frame['asset_id']}/source-preview").status_code == 200
    assert client.put(f"/api/projects/{pid}/shots/{second['id']}/generation-settings", json={
        "mode":"first_frame", "reference_asset_id":frame["asset_id"],
    }).status_code == 200
    state = client.get(f"/api/projects/{pid}").json()
    assert state["shots"][0]["edit_config"]["out_frame_exclusive"] == 9
    assert state["shots"][1]["asset_id"] == second["asset_id"]
    assert state["shots"][1]["needs_regeneration"] is True
    assert client.post(f"/api/projects/{pid}/compose", json={}).status_code == 409
    response = client.post(f"/api/projects/{pid}/shots/{second['id']}/generate", json={})
    assert response.status_code == 200
    with Session() as db:
        snap = db.get(Job, response.json()["id"]).snapshot
        assert snap["reference"]["asset_id"] == frame["asset_id"]
        assert snap["reference"]["source_asset_id"] == first["asset_id"]
        assert snap["reference"]["out_frame_exclusive"] == 9
        assert "base64" not in json.dumps(snap)
    process_all(clips[1])
    assert client.post(f"/api/projects/{pid}/compose", json={}).status_code == 200
    process_all(clips[1])
    with Session() as db:
        film = db.get(Asset, db.get(Project,pid).export_asset)
        length, info = probe(path_for(film.path))
        assert 1.5 <= length <= 1.7
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert video["r_frame_rate"] == "24/1"
    assert client.post(f"/api/projects/{pid}/shots/{first['id']}/generate", json={}).status_code == 200
    process_all(clips[0])
    state = client.get(f"/api/projects/{pid}").json()
    assert state["shots"][1]["reference_stale"] is True
    assert client.post(f"/api/projects/{pid}/shots/{second['id']}/generate", json={}).status_code == 409
    assert client.post(f"/api/projects/{pid}/compose", json={}).status_code == 409
    assert client.get(f"/api/assets/{frame['asset_id']}").status_code == 200


def test_first_frame_wire_and_local_archive_recovery(client, clips, monkeypatch):
    from app.media import reference_frame
    pid = project(client)
    story = generate_story(client,pid)
    target = MEDIA / "first-frame-wire" / "frame.png"
    metadata, _ = reference_frame(clips[0],target,"16:9",4)
    reference = {**metadata, "path":str(target.relative_to(MEDIA))}
    monkeypatch.setattr("app.provider.ark_key",lambda:"fixture-secret")
    captured=[]
    def handler(request):
        data=json.loads(request.content)
        captured.append(data)
        assert data["content"][1]["role"] == "first_frame"
        assert data["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert data["ratio"] == "adaptive" and data["return_last_frame"] is True
        return httpx.Response(200,json={"id":"wire-first-frame"})
    provider=Ark()
    provider.client.close()
    provider.client=httpx.Client(base_url="https://example.test/api/v3/",transport=httpx.MockTransport(handler))
    snap={"shot":SPEC,"script":SCRIPT,"video_model":"doubao-seedance-2-0-260128","ratio":"16:9","reference":reference}
    assert provider.submit(snap) == "wire-first-frame"
    target.write_bytes(b"changed-reference")
    with pytest.raises(ProviderError):
        provider.submit(snap)
    assert len(captured)==1
    provider.close()

    class Completed(FixtureProvider):
        def poll(self, external_id):
            return {"video_url":"fixture://video","last_frame_url":"https://example.volces.com/tail.png"}
    response=client.post(f"/api/projects/{pid}/shots/{story['shots'][0]['id']}/generate",json={})
    job_id=response.json()["id"]
    worker.execute(worker.claim(),Completed)
    with Session.begin() as db:
        row=db.get(Job,job_id);external_id=row.external_id
        row.next_run=now()
    monkeypatch.setattr("app.worker.download",lambda *args,**kwargs: (_ for _ in ()).throw(ValueError("图片下载失败")))
    worker.execute(worker.claim(),Completed,fixture_downloader(clips[0]))
    with Session() as db:
        row=db.get(Job,job_id)
        assert row.status=="failed" and row.external_id==external_id and row.phase=="polling"
    assert client.post(f"/api/jobs/{job_id}/recover",json={}).status_code == 200
    def image_copy(url,destination,image=False):
        assert image
        reference_frame(clips[0],destination,"16:9",5)
    monkeypatch.setattr("app.worker.download",image_copy)
    worker.execute(worker.claim(),Completed,fixture_downloader(clips[0]))
    assert FixtureProvider.submits == 1
    with Session() as db:
        row=db.get(Job,job_id)
        assert row.status=="succeeded" and row.external_id==external_id
        video=db.get(Asset,db.get(Shot,story["shots"][0]["id"]).asset_id)
        tail=db.get(Asset,video.metadata_json["last_frame_asset_id"])
        assert tail.kind=="image" and tail.metadata_json["source_asset_id"]==video.id


def test_terminal_video_requires_paid_retry_acknowledgement(client, clips):
    class Terminal(FixtureProvider):
        def poll(self, external_id):
            raise ProviderError("测试模型任务失败", terminal=True)
    pid = project(client)
    story = generate_story(client, pid)
    response = client.post(f"/api/projects/{pid}/shots/{story['shots'][0]['id']}/generate", json={})
    job_id = response.json()["id"]
    worker.execute(worker.claim(), FixtureProvider)
    with Session.begin() as db:
        db.execute(text("UPDATE jobs SET next_run=now() WHERE status='waiting'"))
    worker.execute(worker.claim(), Terminal)
    with Session() as db:
        assert db.get(Job, job_id).phase == "rejected"
    assert client.post(f"/api/jobs/{job_id}/recover", json={}).status_code == 409
    assert client.post(f"/api/jobs/{job_id}/recover", json={"acknowledge_duplicate_cost":True}).status_code == 200
    process_all(clips[0])
    assert FixtureProvider.submits == 2


def test_stale_failed_job_cannot_replace_edited_shot(client):
    class Failure(FixtureProvider):
        def submit(self, snapshot):
            raise ProviderError("测试参数拒绝")
    pid = project(client)
    story = generate_story(client, pid)
    shot_id = story["shots"][0]["id"]
    response = client.post(f"/api/projects/{pid}/shots/{shot_id}/generate", json={})
    worker.execute(worker.claim(), Failure)
    assert client.put(f"/api/projects/{pid}/shots/{shot_id}", json={**SPEC,"action":"修改后的动作"}).status_code == 200
    assert client.post(f"/api/jobs/{response.json()['id']}/recover", json={}).status_code == 409
