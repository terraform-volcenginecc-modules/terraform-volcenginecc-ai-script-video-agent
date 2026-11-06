"""Project isolation, model provenance and ratio checks with no paid calls."""
import pytest

from test_flow import FORM, SPEC, clean, client, clips, generate_story, project, process_all
from app.db import Asset, Job, Project, Session, Shot, migrate
from app.media import MEDIA, compose, path_for, probe, reference_frame
from app.schemas import ProjectInput
from sqlalchemy import select, text


@pytest.fixture(autouse=True)
def login_limit():
    from app.api import login_attempts
    login_attempts.clear()
    yield
    login_attempts.clear()


def test_projects_have_independent_tasks_and_archiving(client):
    first = project(client)
    second = client.post("/api/projects", json={**FORM, "name": "第二部", "ratio": "9:16"}).json()["id"]
    s1 = generate_story(client, first)["shots"][0]["id"]
    s2 = generate_story(client, second)["shots"][0]["id"]
    job = client.post(f"/api/projects/{first}/shots/{s1}/generate", json={})
    assert job.status_code == 200
    assert client.put(f"/api/projects/{second}/shots/{s2}", json={**SPEC, "action": "第二项目独立动作"}).status_code == 200
    assert client.put(f"/api/projects/{second}/shots/{s1}", json=SPEC).status_code == 404
    assert client.put(f"/api/projects/{first}/archive", json={"archived": True}).status_code == 409
    assert client.put(f"/api/projects/{second}/archive", json={"archived": True}).status_code == 200
    assert [p["id"] for p in client.get("/api/projects").json()] == [first]
    listed = client.get("/api/projects?include_archived=true").json()
    assert len(listed) == 2
    assert next(p for p in listed if p["id"] == first)["active_jobs"] == 1
    archived = client.get(f"/api/projects/{second}").json()
    assert archived["archived"] and len(archived["shots"]) == 2
    assert client.post(f"/api/projects/{second}/script", json={}).status_code == 409
    assert client.put(f"/api/projects/{second}/archive", json={"archived": False}).status_code == 200
    assert client.get(f"/api/projects/{second}").json()["shots"][0]["spec"]["action"] == "第二项目独立动作"
    assert all(j["project_id"] == first for j in client.get(f"/api/projects/{first}").json()["jobs"])
    assert all(j["project_id"] == second for j in client.get(f"/api/projects/{second}").json()["jobs"])


def test_cross_project_image_binding_rejected(client, clips):
    first = project(client)
    second = client.post("/api/projects", json={**FORM, "video_model": "doubao-seedance-2-0-260128"}).json()["id"]
    sid = generate_story(client, second)["shots"][0]["id"]
    target = path_for(f"{first}/cross-reference/image.png")
    metadata, _ = reference_frame(clips[0], target, "16:9")
    with Session.begin() as db:
        asset = Asset(project_id=first, kind="image", path=str(target.relative_to(MEDIA)),
                      metadata_json={**metadata, "origin": "uploaded"})
        db.add(asset)
        db.flush()
        aid = asset.id
    assert client.put(f"/api/projects/{second}/shots/{sid}/generation-settings",
                      json={"mode": "first_frame", "reference_asset_id": aid}).status_code == 422


def test_video_model_and_ratio_changes_preserve_assets_but_require_generation(client, clips):
    pid = project(client)
    generate_story(client, pid)
    client.post(f"/api/projects/{pid}/videos", json={})
    process_all(clips[0])
    client.post(f"/api/projects/{pid}/compose", json={})
    process_all(clips[0])
    before = client.get(f"/api/projects/{pid}").json()
    assert client.put(f"/api/projects/{pid}", json={**FORM, "script_model": "other-script"}).status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["export_asset"] == before["export_asset"]
    assert client.put(f"/api/projects/{pid}", json={**FORM, "video_model": "doubao-seedance-2-0-260128", "ratio": "3:4"}).status_code == 200
    changed = client.get(f"/api/projects/{pid}").json()
    assert changed["export_asset"] is None
    assert [s["asset_id"] for s in changed["shots"]] == [s["asset_id"] for s in before["shots"]]
    assert all(s["needs_regeneration"] for s in changed["shots"])
    assert client.get(f"/api/assets/{before['export_asset']}").status_code == 200
    assert client.post(f"/api/projects/{pid}/compose", json={}).status_code == 409
    preview = client.get(f"/api/projects/{pid}/shots/{changed['shots'][0]['id']}/generation-preview").json()
    assert preview["model"] == "doubao-seedance-2-0-260128" and preview["ratio"] == "3:4"
    assert "画面比例（以此为准）：3:4" in preview["prompt"]
    client.post(f"/api/projects/{pid}/videos", json={})
    process_all(clips[0])
    assert all(not s["needs_regeneration"] for s in client.get(f"/api/projects/{pid}").json()["shots"])
    with Session() as db:
        for s in db.scalars(select(Shot).where(Shot.project_id == pid)):
            metadata = db.get(Asset, s.asset_id).metadata_json
            assert metadata["video_model"] == "doubao-seedance-2-0-260128" and metadata["ratio"] == "3:4"


def test_ratio_change_invalidates_first_frame_and_failed_compose_snapshot(client, clips):
    from test_flow import FixtureProvider, fixture_downloader
    from app import worker
    pid = project(client)
    client.put(f"/api/projects/{pid}", json={**FORM, "video_model": "doubao-seedance-2-0-260128"})
    generate_story(client, pid)
    client.post(f"/api/projects/{pid}/videos", json={})
    process_all(clips[0])
    first, second = client.get(f"/api/projects/{pid}").json()["shots"]
    frame = client.post(f"/api/projects/{pid}/shots/{second['id']}/reference-frame", json={
        "source_shot_id": first["id"], "source_asset_id": first["asset_id"],
    }).json()
    assert client.put(f"/api/projects/{pid}/shots/{second['id']}/generation-settings", json={
        "mode": "first_frame", "reference_asset_id": frame["asset_id"],
    }).status_code == 200
    client.post(f"/api/projects/{pid}/shots/{second['id']}/generate", json={})
    process_all(clips[0])
    compose_job = client.post(f"/api/projects/{pid}/compose", json={}).json()["id"]
    with Session.begin() as db:
        job = db.get(Job, compose_job)
        job.status = "failed"
    assert client.put(f"/api/projects/{pid}", json={**FORM, "video_model": "doubao-seedance-2-5-custom"}).status_code == 200
    assert client.post(f"/api/jobs/{compose_job}/recover", json={}).status_code == 409
    assert client.put(f"/api/projects/{pid}", json={**FORM, "video_model": "doubao-seedance-2-0-260128", "ratio": "9:16"}).status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["shots"][1]["reference_stale"] is True
    assert client.post(f"/api/projects/{pid}/shots/{second['id']}/generate", json={}).status_code == 409
    assert client.post(f"/api/jobs/{compose_job}/recover", json={}).status_code == 409


@pytest.mark.parametrize("ratio,size", [("4:3", (640,480)), ("3:4", (480,640)), ("21:9", (1120,480))])
def test_new_ratios_export_and_reference_frame(clips, ratio, size):
    ProjectInput.model_validate({**FORM, "ratio": ratio})
    output = MEDIA / f"ratio-{ratio.replace(':','-')}" / "film.mp4"
    compose(list(clips), output, ratio, resolution="480p", audio=False)
    _, data = probe(output)
    stream = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert (stream["width"], stream["height"]) == size
    metadata, _ = reference_frame(clips[0], output.with_name("ref.png"), ratio)
    assert metadata["width"] / metadata["height"] == size[0] / size[1]


def test_model_catalog_configuration(client, monkeypatch):
    monkeypatch.setattr("app.config.runtime", lambda: {
        "model_catalog": {"video": [{"id": "ep-configured", "name": "已开通接入点"}]},
    })
    data = client.get("/api/config").json()
    assert {o["id"] for o in data["model_catalog"]["video"]} >= {data["video_model"], "ep-configured"}
    assert set(data["ratios"]) == {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}


def test_migration_backfills_legacy_video_provenance(client, clips):
    pid = project(client)
    generate_story(client, pid)
    client.post(f"/api/projects/{pid}/videos", json={})
    process_all(clips[0])
    with Session.begin() as db:
        for s in db.scalars(select(Shot).where(Shot.project_id == pid)):
            asset = db.get(Asset, s.asset_id)
            asset.metadata_json = {k: v for k, v in asset.metadata_json.items() if k not in ("video_model", "ratio")}
        db.execute(text("ALTER TABLE projects DROP COLUMN archived"))
        db.execute(text("DROP TABLE asset_backups"))
        db.execute(text("ALTER TABLE jobs DROP COLUMN error_details"))
        db.execute(text("INSERT INTO schema_version VALUES (3) ON CONFLICT DO NOTHING"))
        db.execute(text("DELETE FROM schema_version WHERE version>=4"))
    try:
        migrate()
        with Session() as db:
            s = db.scalar(select(Shot).where(Shot.project_id == pid))
            metadata = db.get(Asset, s.asset_id).metadata_json
            assert metadata["video_model"] == FORM["video_model"] and metadata["ratio"] == FORM["ratio"]
            assert db.get(Project, pid).archived is False
    finally:
        migrate()
