"""Parameter and media checks; no paid provider calls."""
import json

import httpx
import pytest

from test_flow import FORM, clean, client, clips, generate_story, project
from app.db import Job, Session
from app.generation import build_prompt
from app.media import MEDIA, compose, probe
from app.provider import Ark


@pytest.fixture(autouse=True)
def isolated_login_limit():
    from app.api import login_attempts
    login_attempts.clear()
    yield
    login_attempts.clear()


def test_preview_matches_wire_and_snapshot(client, monkeypatch):
    pid = project(client)
    settings = {"resolution": "1080p", "generate_audio": False, "watermark": True, "execution_expires_after": 3600}
    assert client.put(f"/api/projects/{pid}", json={
        **FORM, "video_model": "doubao-seedance-2-0-260128", "video_settings": settings,
    }).status_code == 200
    story = generate_story(client, pid)
    sid = story["shots"][0]["id"]
    preview = client.get(f"/api/projects/{pid}/shots/{sid}/generation-preview")
    assert preview.status_code == 200
    assert preview.json()["resolution"] == "1080p" and preview.json()["generate_audio"] is False
    with Session() as db:
        assert not any(j.kind == "video" for j in db.query(Job).all())
    response = client.post(f"/api/projects/{pid}/shots/{sid}/generate", json={})
    with Session() as db:
        snap = db.get(Job, response.json()["id"]).snapshot
    assert snap["prompt"] == preview.json()["prompt"] == build_prompt(snap)
    assert snap["video_settings"] == settings
    captured = []
    monkeypatch.setattr("app.provider.ark_key", lambda: "fixture-secret")
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "fixture-parameter-task"})
    provider = Ark()
    provider.client.close()
    provider.client = httpx.Client(base_url="https://example.test/", transport=httpx.MockTransport(handler))
    try:
        provider.submit(snap)
    finally:
        provider.close()
    assert captured[0]["content"][0]["text"] == preview.json()["prompt"]
    assert all(captured[0][k] == v for k, v in settings.items())
    assert captured[0]["return_last_frame"] is True


@pytest.mark.parametrize("resolution,size", [("480p", (854, 480)), ("1080p", (1920, 1080))])
def test_export_resolution_without_audio(clips, resolution, size):
    output = MEDIA / f"parameter-{resolution}" / "film.mp4"
    compose(list(clips), output, "16:9", resolution=resolution, audio=False)
    _, info = probe(output)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == size
    assert not any(s["codec_type"] == "audio" for s in info["streams"])


def test_unsupported_resolution_rejected_without_job(client):
    pid = project(client)
    response = client.put(f"/api/projects/{pid}", json={
        **FORM, "video_model": "doubao-seedance-2-0-fast-260128",
        "video_settings": {"resolution": "1080p"},
    })
    assert response.status_code == 422
    assert client.get(f"/api/projects/{pid}").json()["video_settings"]["resolution"] == "720p"


def uploaded(client, pid, clips):
    import base64
    from app.media import reference_frame
    target = MEDIA / "uploaded-test" / "image.png"
    reference_frame(clips[0], target, "16:9", 0)
    response = client.post(f"/api/projects/{pid}/reference-images",
                           json={"data": base64.b64encode(target.read_bytes()).decode()})
    assert response.status_code == 200, response.text
    return response.json()["asset_id"]


@pytest.mark.parametrize("mode,roles", [("first_frame", ["first_frame"]),
                                       ("first_last_frame", ["first_frame", "last_frame"]),
                                       ("reference", ["reference_image"])])
def test_uploaded_modes_preview_wire_and_integrity(client, clips, monkeypatch, mode, roles):
    from app.db import Asset
    from app.media import path_for
    from app.provider import ProviderError
    pid = project(client)
    client.put(f"/api/projects/{pid}", json={**FORM, "video_model": "doubao-seedance-2-0-260128"})
    sid = generate_story(client, pid)["shots"][0]["id"]
    aid = uploaded(client, pid, clips)
    config = {"mode": mode}
    if mode == "reference":
        config["reference_image_ids"] = [aid]
    else:
        config["reference_asset_id"] = aid
        if mode == "first_last_frame":
            config["last_frame_asset_id"] = aid
    assert client.put(f"/api/projects/{pid}/shots/{sid}/generation-settings", json=config).status_code == 200
    preview = client.get(f"/api/projects/{pid}/shots/{sid}/generation-preview").json()
    assert [r["role"] for r in preview["references"]] == roles
    response = client.post(f"/api/projects/{pid}/shots/{sid}/generate", json={})
    with Session() as db:
        snap = db.get(Job, response.json()["id"]).snapshot
        path = path_for(db.get(Asset, aid).path)
    captured = []
    monkeypatch.setattr("app.provider.ark_key", lambda: "fixture-secret")
    provider = Ark()
    provider.client.close()
    provider.client = httpx.Client(base_url="https://example.test/", transport=httpx.MockTransport(
        lambda request: (captured.append(json.loads(request.content)) or httpx.Response(200, json={"id": "test"}))))
    try:
        provider.submit(snap)
        assert [r["role"] for r in captured[0]["content"][1:]] == roles
        assert captured[0]["ratio"] == ("16:9" if mode == "reference" else "adaptive")
        path.write_bytes(b"changed")
        with pytest.raises(ProviderError):
            provider.submit(snap)
        assert len(captured) == 1
    finally:
        provider.close()


def test_invalid_upload_and_mode_rejected(client, clips):
    import base64
    pid = project(client)
    client.put(f"/api/projects/{pid}", json={**FORM, "video_model": "doubao-seedance-2-0-260128"})
    sid = generate_story(client, pid)["shots"][0]["id"]
    assert client.post(f"/api/projects/{pid}/reference-images", json={"data": base64.b64encode(b"not image").decode()}).status_code == 422
    aid = uploaded(client, pid, clips)
    for config in [
        {"mode": "first_last_frame", "reference_asset_id": aid},
        {"mode": "first_frame", "reference_asset_id": aid, "reference_image_ids": [aid]},
        {"mode": "reference", "reference_image_ids": [aid, aid]},
        {"mode": "reference", "reference_image_ids": ["missing"]},
    ]:
        assert client.put(f"/api/projects/{pid}/shots/{sid}/generation-settings", json=config).status_code == 422
    assert client.get(f"/api/projects/{pid}").json()["shots"][0]["generation_config"]["mode"] == "text"


def test_multimodal_remote_snapshot_wire_and_limits(client, clips, monkeypatch):
    from app.schemas import GenerationInput
    from pydantic import ValidationError
    monkeypatch.setattr("app.media.socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    pid = project(client)
    client.put(f"/api/projects/{pid}", json={**FORM, "video_model": "doubao-seedance-2-0-260128"})
    sid = generate_story(client, pid)["shots"][0]["id"]
    aid = uploaded(client, pid, clips)
    refs = [{"kind": "video", "url": "https://example.volces.com/reference.mp4", "duration": 4},
            {"kind": "audio", "url": "https://example.volces.com/reference.mp3", "duration": 3}]
    config = {"mode": "reference", "reference_image_ids": [aid], "remote_references": refs}
    assert client.put(f"/api/projects/{pid}/shots/{sid}/generation-settings", json=config).status_code == 200
    response = client.post(f"/api/projects/{pid}/shots/{sid}/generate", json={})
    with Session() as db:
        snap = db.get(Job, response.json()["id"]).snapshot
    monkeypatch.setattr("app.provider.ark_key", lambda: "fixture-secret")
    captured = []
    provider = Ark()
    provider.client.close()
    provider.client = httpx.Client(base_url="https://example.test/", transport=httpx.MockTransport(
        lambda request: (captured.append(json.loads(request.content)) or httpx.Response(200, json={"id": "test"}))))
    try:
        provider.submit(snap)
    finally:
        provider.close()
    assert [r["role"] for r in captured[0]["content"][1:]] == ["reference_image", "reference_video", "reference_audio"]
    assert captured[0]["content"][2]["video_url"]["url"] == refs[0]["url"]
    with pytest.raises(ValidationError):
        GenerationInput.model_validate({**config, "remote_references": [dict(refs[0], duration=10)] * 2})
    with pytest.raises(ValidationError):
        GenerationInput.model_validate({"mode": "reference", "remote_references": [refs[1]]})


def test_remote_signed_url_and_audio_disabled_rejected(client, clips, monkeypatch):
    monkeypatch.setattr("app.media.socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    pid = project(client)
    client.put(f"/api/projects/{pid}", json={**FORM, "video_model": "doubao-seedance-2-0-260128", "video_settings": {"generate_audio": False}})
    sid = generate_story(client, pid)["shots"][0]["id"]
    aid = uploaded(client, pid, clips)
    config = {"mode": "reference", "reference_image_ids": [aid]}
    for kind, url in [("video", "https://example.volces.com/video.mp4?signature=temporary"),
                      ("audio", "https://example.volces.com/audio.mp3"),
                      ("video", "http://127.0.0.1/video.mp4")]:
        assert client.put(f"/api/projects/{pid}/shots/{sid}/generation-settings", json={
            **config, "remote_references": [{"kind": kind, "url": url, "duration": 4}],
        }).status_code == 422


def test_timeout_change_preserves_existing_output(client, clips):
    from test_flow import process_all
    pid = project(client)
    generate_story(client, pid)
    client.post(f"/api/projects/{pid}/videos", json={})
    process_all(clips[0])
    client.post(f"/api/projects/{pid}/compose", json={})
    process_all(clips[0])
    before = client.get(f"/api/projects/{pid}").json()
    assert client.put(f"/api/projects/{pid}", json={**FORM, "video_settings": {"execution_expires_after": 3600}}).status_code == 200
    after = client.get(f"/api/projects/{pid}").json()
    assert after["export_asset"] == before["export_asset"]
    assert not any(s["needs_regeneration"] for s in after["shots"])
