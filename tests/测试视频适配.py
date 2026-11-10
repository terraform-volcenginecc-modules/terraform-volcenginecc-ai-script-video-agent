"""本地 HTTP 假服务集成验证；禁止连接云端或真实模型，临时数据全部放在工程外。"""
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import httpx
from sqlalchemy import create_engine, DateTime
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import TypeDecorator

from app import api, config, db, media, provider, storage, worker
from app.comfyui import ComfyUI
from app.comfyui_config import validate_config, validate_request
from app.provider import ProviderError
from 适配测试数据 import 服务配置, 视频快照


class 时间类型(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


class 假服务:
    def __init__(self, video):
        self.video = video
        self.posts = []
        self.gets = []
        self.status = "queued"
        self.reply_status = 201
        self.reply_body = None
        self.raw_reply = None
        self.poll_body = None
        self.download_status = 200
        self.redirect = None
        self.auth = "Bearer fake-comfy-key"
        outer = self

        class 处理器(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                raw = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                from urllib.parse import unquote
                self.path = unquote(self.path)
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                outer.posts.append((self.path, dict(self.headers), raw))
                if self.headers.get("Authorization") != outer.auth:
                    self.reply(401, {})
                elif self.path.endswith("/assets"):
                    self.reply(201, {"id": "假素材编号"})
                elif outer.raw_reply is not None:
                    self.send_response(outer.reply_status)
                    self.send_header("Content-Length", str(len(outer.raw_reply)))
                    self.end_headers()
                    self.wfile.write(outer.raw_reply)
                else:
                    self.reply(outer.reply_status, outer.reply_body if outer.reply_body is not None else {
                        "id": "fake-job", "status": "queued", "urls": {"self": "/挂载/api/v2/jobs/fake-job"},
                    })

            def do_GET(self):
                from urllib.parse import unquote
                self.path = unquote(self.path)
                outer.gets.append((self.path, dict(self.headers)))
                if self.headers.get("Authorization") != outer.auth:
                    self.reply(401, {})
                elif self.path.endswith("/content"):
                    if outer.download_status != 200:
                        self.reply(outer.download_status, {})
                        return
                    if outer.redirect:
                        from urllib.parse import quote
                        self.send_response(302)
                        self.send_header("Location", quote(outer.redirect, safe=":/?=&%"))
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(outer.video)))
                    self.end_headers()
                    self.wfile.write(outer.video)
                else:
                    self.reply(200, outer.poll_body if outer.poll_body is not None else {
                        "id": "fake-job", "status": outer.status,
                        "error": {"code": "node_execution_error", "message": "不得进入日志的密钥"},
                        "outputs": [{"node_id": "无关输出", "type": "image", "url": "不得选择"},
                                    {"node_id": "假输出", "type": "video", "content_type": "video/mp4",
                                     "url": "/挂载/api/v2/assets/fake-asset/content"}],
                    })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), 处理器)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/挂载"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class 假TOS:
    def __init__(self):
        self.objects = {}
        self.fail = False
        self.puts = 0

    def head_object(self, bucket, key):
        from types import SimpleNamespace
        if key not in self.objects:
            error = RuntimeError("假对象不存在")
            error.status_code = 404
            raise error
        data, metadata = self.objects[key]
        return SimpleNamespace(meta=metadata, content_length=len(data), etag="假对象校验")

    def put_object_from_file(self, bucket, key, path, **kwargs):
        from tos.enum import ACLType
        assert kwargs["acl"] == ACLType.ACL_Private
        self.puts += 1
        if self.fail:
            raise RuntimeError("假上传失败")
        self.objects[key] = (Path(path).read_bytes(), kwargs["meta"])

    def close(self):
        pass


class 视频适配验证(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="视频适配验证-")
        cls.root = Path(cls.temporary.name)
        source = cls.root / "假视频.mp4"
        media.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                   "color=c=blue:s=640x360:r=24:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
        cls.video = source.read_bytes()
        for table in db.Base.metadata.tables.values():
            for column in table.columns:
                if isinstance(column.type, DateTime):
                    column.type = 时间类型()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.server = 假服务(self.video)
        self.key_file = self.root / "假服务密钥"
        self.key_file.write_text("fake-comfy-key")
        self.cfg = 服务配置(self.server.url, self.key_file)
        self.snapshot = 视频快照(self.cfg)
        self.runtime = {"video_provider": "comfyui", "comfyui": self.cfg, "storage": {"auto_upload": True}}
        self.patches = [
            patch("app.config.runtime", lambda: deepcopy(self.runtime)),
            patch("app.storage.runtime", lambda: deepcopy(self.runtime)),
            patch("app.media.MEDIA", self.root),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.server.close)
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    def make_provider(self):
        service = ComfyUI(self.cfg)
        self.addCleanup(service.close)
        return service

    def prepare_database(self):
        engine = create_engine("sqlite:///" + str(self.root / "假数据库.sqlite"))
        db.Base.metadata.drop_all(engine)
        db.Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        for module in (db, api, worker, storage):
            item = patch.object(module, "Session", sessions)
            item.start()
            self.addCleanup(item.stop)
        for module in (worker, storage):
            item = patch.object(module, "heartbeat", lambda *_: None)
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(engine.dispose)
        with sessions.begin() as session:
            project = db.Project(id="fake-project", script_model="假剧本模型", video_model="假视频模型",
                                 name="假项目", brief="假服务验证", script=self.snapshot["script"])
            session.add(project)
            session.flush()
            session.add(db.Shot(id="fake-shot", project_id=project.id, position=1, spec=self.snapshot["shot"]))
            session.flush()
            session.add(db.Job(id="fake-local-job", project_id=project.id, shot_id="fake-shot",
                               kind="video", snapshot=self.snapshot))
        return sessions

    def test_默认视频与剧本始终选择方舟(self):
        self.runtime = {}
        self.assertEqual(config.video_provider(), "ark")
        with patch.object(provider, "Ark") as ark:
            provider.video_provider({})
            provider.script_provider()
            self.assertEqual(ark.call_count, 2)
        self.runtime = {"video_provider": "comfyui"}
        with patch.object(provider, "Ark") as ark:
            provider.script_provider()
            provider.video_provider({})
            self.assertEqual(ark.call_count, 2)

    def test_配置缺失与错误映射在提交前失败(self):
        for mutate in (
            lambda c: c.clear(),
            lambda c: c["bindings"]["prompt"].update(node_id="不存在"),
            lambda c: c.update(workflow={"nodes": [], "links": []}),
            lambda c: c.update(output_node_id="不存在"),
            lambda c: c["bindings"]["width"].update(input="不存在"),
            lambda c: c["capabilities"].update(generate_audio=[True, False]) or c["bindings"].pop("generate_audio"),
        ):
            value = deepcopy(self.cfg)
            mutate(value)
            with self.subTest(config=value), self.assertRaises(ValueError):
                validate_config(value)
        self.runtime["comfyui"] = {}
        with self.assertRaises(ValueError):
            config.validate_video_config()
        self.assertEqual(self.server.posts, [])

    def test_认证和TOS归档条件必填(self):
        self.key_file.write_text("")
        with self.assertRaises(ValueError):
            config.validate_video_config()
        self.key_file.write_text("fake-comfy-key")
        self.runtime["storage"]["auto_upload"] = False
        with self.assertRaises(ValueError):
            config.validate_video_config()

    def test_能力校验不依赖Seedance模型前缀(self):
        from types import SimpleNamespace
        project = SimpleNamespace(video_model="真实工作流标识", video_settings=self.snapshot["video_settings"], ratio="16:9")
        api.validate_reference_mode(project, {"mode": "text"}, [])
        for changes in ({"resolution": "1080p"},):
            with self.assertRaises(ValueError):
                validate_request(self.cfg, {**self.snapshot["video_settings"], **changes})
        with self.assertRaises(ValueError):
            validate_request(self.cfg, self.snapshot["video_settings"], duration=3)
        with self.assertRaises(ValueError):
            validate_request(self.cfg, self.snapshot["video_settings"], references=[
                {"type": "video_url", "role": "reference_video"}])

    def test_仅首帧工作流能创建项目但拒绝纯文本分镜(self):
        sessions = self.prepare_database()
        self.runtime["comfyui"]["capabilities"]["modes"] = ["first_frame"]
        result = api.create_project(api.ProjectInput(
            name="仅首帧项目", brief="本地配置验证", script_model="假剧本模型", video_model="工作流模型"))
        with sessions() as session:
            self.assertIsNotNone(session.get(db.Project, result["id"]))
        with self.assertRaises(ValueError):
            validate_request(self.runtime["comfyui"], self.snapshot["video_settings"],
                             generation={"mode": "text"}, references=[])
        self.assertEqual(self.server.posts, [])

    def test_认证地址拒绝跨服务和路径越界(self):
        service = self.make_provider()
        for address in ("https://其他服务.example/api/v2/jobs/id",
                        "/api/v2/jobs/id", "/挂载/api/v2/%2e%2e/admin",
                        "/挂载/api/v2/jobs/id#片段"):
            with self.subTest(address=address), self.assertRaises(ProviderError):
                service.request("GET", address)
        self.assertEqual(self.server.gets, [])

    def test_提交轮询和指定输出节点(self):
        service = self.make_provider()
        original = deepcopy(self.cfg["workflow"])
        self.assertEqual(service.submit(self.snapshot), "fake-job")
        path, headers, raw = self.server.posts[-1]
        self.assertEqual(path, "/挂载/api/v2/jobs")
        self.assertEqual(headers["Idempotency-Key"], self.snapshot["submission_key"])
        workflow = json.loads(raw)["workflow"]
        self.assertEqual(workflow["假输入"]["inputs"]["种子"], 42)
        self.assertEqual(workflow["假输入"]["inputs"]["宽"], 1280)
        self.assertEqual(self.cfg["workflow"], original)
        for state in ("queued", "running", "canceling"):
            self.server.status = state
            self.assertIsNone(service.poll("fake-job"))
        self.server.status = "succeeded"
        self.assertEqual(service.poll("fake-job")["video_url"], "/挂载/api/v2/assets/fake-asset/content")
        for state in ("failed", "canceled", "expired"):
            self.server.status = state
            with self.assertRaises(ProviderError) as failure:
                service.poll("fake-job")
            self.assertTrue(failure.exception.terminal)
            self.assertNotIn("不得进入日志", str(failure.exception))

    def test_参考素材先上传再提交资产引用(self):
        ref = self.root / "假首帧.png"
        media.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                   "color=s=640x360", "-frames:v", "1", str(ref)])
        self.snapshot.update(generation_config={"mode": "first_frame"}, references=[
            {"path": ref.name, "asset_id": "fake-input", "role": "first_frame",
             "type": "image_url", "mime_type": "image/png", "sha256": media.digest(ref)},
        ])
        with patch("app.continuity.validate_reference_file"):
            self.make_provider().submit(self.snapshot)
        self.assertEqual([p[0] for p in self.server.posts], ["/挂载/api/v2/assets", "/挂载/api/v2/jobs"])
        multipart = self.server.posts[0][2]
        self.assertLess(multipart.index(b'name="content_type"'), multipart.index(b'name="file"'))
        inputs = json.loads(self.server.posts[1][2])["workflow"]["假输入"]["inputs"]
        self.assertEqual(inputs["首帧"], {"__type": "core/ASSET", "info": {"id": "假素材编号"}})

    def test_网络异常按受理风险区分(self):
        service = self.make_provider()
        for error, retryable, uncertain in (
            (httpx.ConnectError("假连接失败"), True, False),
            (httpx.ConnectTimeout("假连接超时"), True, False),
            (httpx.ReadTimeout("假读取超时"), False, True),
            (httpx.WriteTimeout("假发送超时"), False, True),
            (httpx.RemoteProtocolError("假协议断开"), False, True),
        ):
            with patch.object(service.client, "request", side_effect=error), self.assertRaises(ProviderError) as failure:
                service.submit(self.snapshot)
            self.assertEqual((failure.exception.retryable, failure.exception.uncertain), (retryable, uncertain))

    def test_服务端错误和重复幂等键不自动重提(self):
        service = self.make_provider()
        for status, body, retryable, uncertain in (
            (500, {}, False, True), (503, {}, False, True),
            (422, {"error": {"code": "idempotency_key_reuse"}}, False, True),
            (429, {"error": {"code": "queue_full"}}, True, False),
            (422, {"error": {"code": "invalid_workflow"}}, False, False),
            (201, {}, False, True),
        ):
            self.server.reply_status, self.server.reply_body = status, body
            with self.assertRaises(ProviderError) as failure:
                service.submit(self.snapshot)
            self.assertEqual((failure.exception.retryable, failure.exception.uncertain), (retryable, uncertain))

    def test_任务链接缺失时仍保存编号并查询原任务(self):
        sessions = self.prepare_database()
        self.server.reply_body = {"id": "fake-job", "urls": {"self": "https://其他服务.example/api/v2/jobs/id"}}
        worker.execute(worker.claim())
        with sessions.begin() as session:
            row = session.get(db.Job, "fake-local-job")
            self.assertEqual(row.external_id, "fake-job")
            self.assertEqual(row.snapshot["comfyui_job_url"], self.server.url + "/api/v2/jobs/fake-job")
            row.next_run = db.now() - timedelta(seconds=1)
        worker.execute(worker.claim())
        self.assertEqual(self.server.gets[-1][0], "/挂载/api/v2/jobs/fake-job")
        self.assertEqual(len(self.server.posts), 1)

    def test_无效JSON提交进入不确定且不能再次领取(self):
        sessions = self.prepare_database()
        self.server.raw_reply = b"<html>invalid-json</html>"
        worker.execute(worker.claim())
        with sessions() as session:
            self.assertEqual(session.get(db.Job, "fake-local-job").status, "uncertain")
        self.assertIsNone(worker.claim())
        self.assertEqual(len(self.server.posts), 1)

    def test_异常输出响应保留原编号且不重新生成(self):
        sessions = self.prepare_database()
        worker.execute(worker.claim())
        self.server.poll_body = {"id": "fake-job", "status": "succeeded",
                                 "outputs": [{"node_id": "假输出", "type": "video", "content_type": None}]}
        with sessions.begin() as session:
            session.get(db.Job, "fake-local-job").next_run = db.now() - timedelta(seconds=1)
        worker.execute(worker.claim())
        with sessions() as session:
            row = session.get(db.Job, "fake-local-job")
            self.assertEqual((row.status, row.external_id), ("failed", "fake-job"))
        self.assertEqual(len(self.server.posts), 1)

    def test_下载失败退避后仅查询和下载原任务(self):
        sessions = self.prepare_database()
        worker.execute(worker.claim())
        self.server.status, self.server.download_status = "succeeded", 503
        with sessions.begin() as session:
            session.get(db.Job, "fake-local-job").next_run = db.now() - timedelta(seconds=1)
        worker.execute(worker.claim())
        with sessions.begin() as session:
            row = session.get(db.Job, "fake-local-job")
            self.assertEqual((row.status, row.phase, row.external_id), ("waiting", "polling", "fake-job"))
            self.assertIsNone(session.get(db.Shot, "fake-shot").asset_id)
            row.next_run = db.now() - timedelta(seconds=1)
        self.server.download_status = 200
        worker.execute(worker.claim())
        with sessions() as session:
            row = session.get(db.Job, "fake-local-job")
            self.assertEqual(row.status, "succeeded")
            self.assertFalse(storage.media_ready(session, session.get(db.Shot, "fake-shot").asset_id))
        self.assertEqual(len(self.server.posts), 1)

    def test_Worker持久化编号并沿原媒体链路归档(self):
        sessions = self.prepare_database()
        worker.execute(worker.claim())
        with sessions() as session:
            job = session.get(db.Job, "fake-local-job")
            self.assertEqual((job.external_id, job.phase, job.status), ("fake-job", "polling", "waiting"))
            self.assertEqual(job.snapshot["comfyui_job_url"], self.server.url + "/api/v2/jobs/fake-job")
        self.server.status = "succeeded"
        with sessions.begin() as session:
            session.get(db.Job, "fake-local-job").next_run = db.now() - timedelta(seconds=1)
        # 当前默认已切回 Ark，历史 ComfyUI 任务仍按快照轮询。
        self.runtime["video_provider"] = "ark"
        worker.execute(worker.claim())
        with sessions() as session:
            job = session.get(db.Job, "fake-local-job")
            self.assertEqual(job.status, "succeeded")
            asset = session.get(db.Asset, session.get(db.Shot, "fake-shot").asset_id)
            self.assertEqual(asset.metadata_json["video_provider"], "comfyui")
            self.assertAlmostEqual(asset.duration, 4)
            self.assertTrue(media.path_for(asset.path).is_file())
            self.assertTrue(asset.metadata_json["last_frame_asset_id"])
        cfg = {"bucket": "假私有桶"}
        self.assertEqual(storage.enqueue_missing(cfg), 2)
        tos = 假TOS()
        tos.fail = True
        backup = storage.claim()
        storage.execute(backup, cfg, client_factory=lambda _: tos)
        with sessions.begin() as session:
            row = session.get(db.AssetBackup, backup.asset_id)
            self.assertEqual(row.status, "failed")
            row.next_run = db.now() - timedelta(seconds=1)
        tos.fail = False
        storage.execute(storage.claim(), cfg, client_factory=lambda _: tos)
        storage.execute(storage.claim(), cfg, client_factory=lambda _: tos)
        with sessions() as session:
            self.assertTrue(storage.media_ready(session, asset.id))
        self.assertEqual(len([p for p in self.server.posts if p[0].endswith("/jobs")]), 1)

    def test_Worker不确定提交与人工恢复(self):
        sessions = self.prepare_database()
        self.server.reply_status = 500
        worker.execute(worker.claim())
        with sessions() as session:
            job = session.get(db.Job, "fake-local-job")
            self.assertEqual((job.status, job.external_id), ("uncertain", None))
        self.assertIsNone(worker.claim())
        with self.assertRaises(api.HTTPException):
            api.recover("fake-local-job", api.RecoveryInput())
        api.recover("fake-local-job", api.RecoveryInput(external_id="fake-job"))
        self.server.status = "running"
        worker.execute(worker.claim())
        self.assertEqual(len(self.server.posts), 1)

    def test_Worker提交中断租约过期不能重提(self):
        sessions = self.prepare_database()
        with sessions.begin() as session:
            job = session.get(db.Job, "fake-local-job")
            job.phase, job.status, job.owner = "submitting", "running", "旧租约"
            job.lease_until = db.now() - timedelta(seconds=1)
        self.assertIsNone(worker.claim())
        with sessions() as session:
            self.assertEqual(session.get(db.Job, "fake-local-job").status, "uncertain")
        self.assertEqual(self.server.posts, [])

    def test_下载跳转不泄露远程服务密钥(self):
        other = 假服务(self.video)
        self.addCleanup(other.close)
        other.auth = None
        self.server.redirect = other.url + "/api/v2/assets/fake-asset/content"
        target = self.root / "跨服务下载.mp4"
        with patch("app.media.allowed_url") as check:
            self.make_provider().download("/挂载/api/v2/assets/fake-asset/content", target)
        self.assertEqual(target.read_bytes(), self.video)
        self.assertNotIn("Authorization", other.gets[0][1])
        check.assert_called_once()

    def test_API创建快照固定工作流与种子(self):
        sessions = self.prepare_database()
        with sessions.begin() as session:
            session.delete(session.get(db.Job, "fake-local-job"))
            session.flush()
            project, shot = session.get(db.Project, "fake-project"), session.get(db.Shot, "fake-shot")
            job = api.video_job(session, project, shot)
            session.flush()
            job_id = job.id
            self.assertEqual(job.snapshot["video_provider"], "comfyui")
            self.assertIsInstance(job.snapshot["seed"], int)
            self.assertNotIn("api_key", job.snapshot["comfyui"])
        self.runtime["comfyui"]["workflow"]["假输入"]["inputs"]["秒数"] = 99
        with sessions() as session:
            self.assertEqual(session.get(db.Job, job_id).snapshot["comfyui"]["workflow"]["假输入"]["inputs"]["秒数"], 4)

    def test_Worker连接前失败退避后仍可提交(self):
        sessions = self.prepare_database()
        service = self.make_provider()
        with patch.object(service.client, "request", side_effect=httpx.ConnectError("假连接异常")):
            worker.execute(worker.claim(), provider_factory=lambda: service)
        with sessions.begin() as session:
            row = session.get(db.Job, "fake-local-job")
            self.assertEqual((row.status, row.phase, row.external_id), ("waiting", "pending", None))
            row.next_run = db.now() - timedelta(seconds=1)
        worker.execute(worker.claim())
        with sessions() as session:
            self.assertEqual(session.get(db.Job, "fake-local-job").external_id, "fake-job")
        self.assertEqual(len(self.server.posts), 1)

    def test_Worker剧本始终使用Ark(self):
        sessions = self.prepare_database()
        with sessions.begin() as session:
            row = session.get(db.Job, "fake-local-job")
            row.kind, row.shot_id = "script", None
        script = {**self.snapshot["script"], "shots": [self.snapshot["shot"]]}
        with patch.object(provider, "Ark") as ark:
            ark.return_value.script.return_value = script
            worker.execute(worker.claim())
            ark.assert_called_once_with()
        with sessions() as session:
            self.assertEqual(session.get(db.Job, "fake-local-job").status, "succeeded")
        self.assertEqual(self.server.posts, [])

    def test_终止任务恢复须确认且使用新的幂等键(self):
        sessions = self.prepare_database()
        worker.execute(worker.claim())
        self.server.status = "failed"
        with sessions.begin() as session:
            session.get(db.Job, "fake-local-job").next_run = db.now() - timedelta(seconds=1)
        worker.execute(worker.claim())
        with self.assertRaises(api.HTTPException):
            api.recover("fake-local-job", api.RecoveryInput())
        api.recover("fake-local-job", api.RecoveryInput(acknowledge_duplicate_cost=True))
        with sessions() as session:
            row = session.get(db.Job, "fake-local-job")
            self.assertEqual((row.external_id, row.phase), (None, "pending"))
            self.assertNotIn("comfyui_job_url", row.snapshot)
        worker.execute(worker.claim())
        keys = [headers["Idempotency-Key"] for path, headers, _ in self.server.posts if path.endswith("/jobs")]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        with sessions() as session:
            row = session.get(db.Job, "fake-local-job")
            self.assertIn("comfyui_submission_nonce", row.snapshot)
            self.assertEqual(row.snapshot["seed"], 42)
            self.assertEqual(row.snapshot["comfyui"]["workflow"], self.snapshot["comfyui"]["workflow"])

    def test_Ark原有提交轮询和剧本契约(self):
        self.runtime = {}
        self.key_file.write_text("fake-ark-key")
        with patch("app.provider.ark_key", return_value="fake-ark-key"):
            ark = provider.Ark()
        self.addCleanup(ark.close)
        script = {**self.snapshot["script"], "shots": [self.snapshot["shot"]]}
        with patch.object(ark, "request", return_value={"choices": [{"message": {"content": json.dumps(script)}}]}):
            self.assertEqual(ark.script(self.snapshot)["title"], "假服务剧本")
        with patch.object(ark, "request", return_value={"id": "fake-ark-job"}) as request:
            self.assertEqual(ark.submit(self.snapshot), "fake-ark-job")
            self.assertEqual(request.call_args.kwargs["json"]["model"], self.snapshot["video_model"])
        with patch.object(ark, "request", return_value={"status": "succeeded", "content": {"video_url": "假视频地址"}}):
            self.assertEqual(ark.poll("fake-ark-job")["video_url"], "假视频地址")

    def test_安装器生成服务保留Ark凭据并按配置增加ComfyUI凭据(self):
        script = Path(__file__).resolve().parents[1] / "scripts/ecs-native.py"
        spec = importlib.util.spec_from_file_location("安装服务验证", script)
        native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native)
        root = self.root / "假安装目录"
        (root / "config").mkdir(parents=True, exist_ok=True)
        runtime = root / "config/runtime.json"
        with patch.object(native, "ROOT", root):
            for cfg in ({}, self.runtime, {**self.runtime, "video_provider": "ark"}):
                runtime.write_text(json.dumps(cfg))
                for kind in ("api", "worker"):
                    unit = native.service(root / "假发布", kind)
                    self.assertIn("LoadCredential=ark_api_key:", unit)
                    self.assertIn("Environment=ARK_API_KEY_FILE=%d/ark_api_key", unit)
                    self.assertEqual("LoadCredential=comfyui_api_key:" in unit, bool(cfg.get("comfyui")))
                    self.assertNotIn("fake-comfy-key", unit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
