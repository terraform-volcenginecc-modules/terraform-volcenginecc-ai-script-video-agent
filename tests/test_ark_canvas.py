"""Canvas text-to-video contracts using local HTTP, SQLite, real media and a fake Ark."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from datetime import timezone
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient
from sqlalchemy import DateTime, create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import TypeDecorator

from app import api, db, media, provider, storage, studio, worker


class LocalDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


class ArkCanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="ark-canvas-tests-")
        cls.root = Path(cls.temporary.name)
        cls.fixture = cls.root / "fixture.mp4"
        media.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                   "color=c=blue:s=640x360:r=24:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.fixture)])
        for table in db.Base.metadata.tables.values():
            for column in table.columns:
                if isinstance(column.type, DateTime):
                    column.type = LocalDateTime()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=self.root))
        self.engine = create_engine("sqlite:///" + str(self.folder / "test.sqlite"),
                                    connect_args={"check_same_thread": False})
        db.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.env = {}
        for name, value in (("session_key", "local-session"), ("owner_password", "local-password"),
                            ("ark_api_key", "local-ark-secret")):
            path = self.folder / name
            path.write_text(value)
            self.env[name.upper() + "_FILE"] = str(path)
        self.apply(patch.dict(os.environ, self.env))
        for module in (api, studio, worker, storage):
            self.apply(patch.object(module, "Session", self.sessions))
        self.apply(patch.object(media, "MEDIA", self.folder / "media"))
        self.apply(patch.object(storage, "settings", return_value=None))
        self.apply(patch.object(worker, "POLL_SECONDS", 0))
        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)
        login = self.client.post("/api/login", json={"password": "local-password"})
        self.assertEqual(login.status_code, 200)
        self.client.headers["x-csrf-token"] = login.json()["csrf"]
        self.body = {"prompt": "一只纸船在水面漂动。", "model": studio.ARK_VIDEO_MODELS[0],
                     "duration": 4, "ratio": "16:9", "resolution": "720p", "generation_id": "1"}

    def apply(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def create(self, **changes):
        response = self.client.post("/api/ark/videos", json={**self.body, **changes})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def complete(self, job_id):
        calls = []
        outer = self

        class FakeArk:
            def submit(self, snapshot):
                calls.append(snapshot)
                return "ark-task-original"

            def poll(self, external_id):
                outer.assertEqual(external_id, "ark-task-original")
                return "fixture"

            def download(self, url, target):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(outer.fixture, target)

            def close(self):
                pass

        for _ in range(2):
            job = worker.claim()
            self.assertEqual(job.id, job_id)
            worker.execute(job, provider_factory=FakeArk)
        return calls

    def test_parameters_are_checked_before_a_task_is_created(self):
        for changes in ({"model": "unknown"}, {"ratio": "invalid"}, {"duration": 16},
                        {"prompt": "   "}, {"api_key": "must-not-be-a-node-input"},
                        {"model": studio.ARK_VIDEO_MODELS[1], "resolution": "1080p"}):
            response = self.client.post("/api/ark/videos", json={**self.body, **changes})
            self.assertEqual(response.status_code, 422, changes)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(db.Job)), 0)

    def test_repeated_runs_reuse_the_same_job_and_new_batch_creates_one(self):
        first = self.create()
        self.assertEqual(self.create()["id"], first["id"])
        self.assertNotEqual(self.create(generation_id="2")["id"], first["id"])
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(db.Job)), 2)

    def test_direct_node_always_uses_ark_and_keeps_exact_prompt(self):
        with patch("app.config.video_provider", return_value="comfyui"):
            task = self.create()
        with self.sessions() as session:
            snapshot = session.get(db.Job, task["id"]).snapshot
        self.assertEqual(snapshot["video_provider"], "ark")
        self.assertEqual(snapshot["prompt"], self.body["prompt"])
        self.assertNotIn("local-ark-secret", json.dumps(snapshot))
        with patch.object(provider.Ark, "request", return_value={"id": "ark-task"}) as send:
            ark = provider.Ark()
            try:
                self.assertEqual(ark.client.headers["Authorization"], "Bearer local-ark-secret")
                self.assertEqual(ark.submit(snapshot), "ark-task")
            finally:
                ark.close()
        payload = send.call_args.kwargs["json"]
        self.assertEqual(payload["content"], [{"type": "text", "text": self.body["prompt"]}])
        self.assertNotIn("local-ark-secret", json.dumps(payload))

    def test_uncertain_submission_is_preserved_and_repeated_run_does_not_resubmit(self):
        task = self.create()
        fake = unittest.mock.Mock()
        fake.submit.side_effect = provider.ProviderError("submission uncertain", uncertain=True)
        worker.execute(worker.claim(), provider_factory=lambda: fake)
        self.assertEqual(self.create()["status"], "uncertain")
        self.assertIsNone(worker.claim())
        self.assertEqual(fake.submit.call_count, 1)
        self.assertEqual(self.client.post(f"/api/jobs/{task['id']}/recover", json={}).status_code, 409)
        recovered = self.client.post(f"/api/jobs/{task['id']}/recover", json={"external_id": "existing-ark-task"})
        self.assertEqual(recovered.status_code, 200)
        fake.poll.return_value = None
        worker.execute(worker.claim(), provider_factory=lambda: fake)
        fake.poll.assert_called_once_with("existing-ark-task")
        self.assertEqual(fake.submit.call_count, 1)

    def test_completed_video_is_readable_after_legacy_storyboard_changes(self):
        task = self.create()
        calls = self.complete(task["id"])
        self.assertEqual(len(calls), 1)
        state = self.client.get(f"/api/ark/videos/{task['id']}").json()
        self.assertEqual(state["status"], "succeeded")
        with self.sessions.begin() as session:
            session.get(db.Job, task["id"]).shot_id = None
        content = self.client.get(f"/api/ark/videos/{task['id']}/content")
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.content, self.fixture.read_bytes())
        self.assertEqual(content.headers["x-content-sha256"], media.digest(self.fixture))
        self.assertEqual(self.create()["id"], task["id"])
        self.assertIsNone(worker.claim())

    def test_tos_failure_exposes_only_upload_retry_and_keeps_task_id(self):
        task = self.create()
        self.complete(task["id"])
        with self.sessions.begin() as session:
            job = session.get(db.Job, task["id"])
            asset_id = job.snapshot["output_asset_id"]
            session.add(db.AssetBackup(asset_id=asset_id, bucket="local", object_key="video.mp4",
                                       status="failed", error="upload failed"))
        with patch.object(storage, "settings", return_value={"bucket": "local"}):
            state = self.create()
            self.assertEqual(state["status"], "upload_failed")
            self.assertEqual(state["external_id"], "ark-task-original")
            self.assertEqual(self.client.get(f"/api/ark/videos/{task['id']}/content").status_code, 409)
            with patch.object(api, "storage_settings", return_value={"bucket": "local"}):
                retry = self.client.post(f"/api/assets/{asset_id}/backup/retry", json={})
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(self.create()["status"], "uploading")
            with self.sessions.begin() as session:
                session.get(db.AssetBackup, asset_id).status = "succeeded"
            self.assertEqual(self.create()["status"], "succeeded")
        self.assertIsNone(worker.claim())

    def test_authentication_and_csrf_are_required(self):
        anonymous = TestClient(api.app)
        try:
            self.assertEqual(anonymous.get("/api/ark/video-models").status_code, 401)
            self.assertEqual(anonymous.post("/api/ark/videos", json=self.body).status_code, 401)
        finally:
            anonymous.close()
        del self.client.headers["x-csrf-token"]
        self.assertEqual(self.client.post("/api/ark/videos", json=self.body).status_code, 403)

    def test_workflow_contains_standard_video_and_no_credentials(self):
        root = Path(__file__).resolve().parents[1]
        workflow = json.loads((root / "workflows/ark-text-to-video.json").read_text())
        self.assertEqual([node["type"] for node in workflow["nodes"]], ["VolcArkTextToVideo", "SaveVideo"])
        self.assertEqual(workflow["links"][0][-1], "VIDEO")
        text = json.dumps(workflow).lower()
        self.assertNotIn("api_key", text)
        self.assertNotIn("secret", text)
        auth = (root / "frontend/comfyui-extension/auth.js").read_text()
        self.assertNotIn("registerSidebarTab", auth)

    def test_video_node_rechecks_persistent_state_on_every_run(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("ark_node_test", root / "backend/comfyui_extension.py")
        extension = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(extension)
        node = extension.NODE_CLASS_MAPPINGS["VolcArkTextToVideo"]
        # A shared NaN object can compare equal by identity inside ComfyUI cache keys.
        self.assertIsNot(node.IS_CHANGED(), node.IS_CHANGED())


if __name__ == "__main__":
    unittest.main(verbosity=2)
