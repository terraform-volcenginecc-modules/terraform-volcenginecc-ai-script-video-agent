import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import database_url


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    brief: Mapped[str] = mapped_column(Text)
    style: Mapped[str] = mapped_column(Text, default="")
    characters: Mapped[str] = mapped_column(Text, default="")
    duration: Mapped[int] = mapped_column(Integer, default=30)
    ratio: Mapped[str] = mapped_column(String(10), default="16:9")
    script_model: Mapped[str] = mapped_column(String(150))
    video_model: Mapped[str] = mapped_column(String(150))
    video_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    script: Mapped[dict] = mapped_column(JSON, default=dict)
    export_asset: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Shot(Base):
    __tablename__ = "shots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    generation_config: Mapped[dict] = mapped_column(JSON, default=dict)
    edit_config: Mapped[dict] = mapped_column(JSON, default=dict)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    phase: Mapped[str] = mapped_column(String(30), default="pending")
    snapshot: Mapped[dict] = mapped_column(JSON)
    external_id: Mapped[str | None] = mapped_column(String(150), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_run: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(String(20), default="video")


class AssetBackup(Base):
    __tablename__ = "asset_backups"
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    bucket: Mapped[str] = mapped_column(String(100))
    object_key: Mapped[str] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_run: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(36), nullable=True)


engine = create_engine(database_url(), pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)


def migrate():
    # Serialized startup migration. Subsequent schema changes must add a versioned migration.
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(7342091)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY)"))
        version = conn.execute(text("SELECT max(version) FROM schema_version")).scalar()
        if version is None:
            Base.metadata.create_all(conn)
            conn.execute(text("INSERT INTO schema_version VALUES (6)"))
            return
        elif version == 1:
            conn.execute(text("ALTER TABLE shots ADD COLUMN generation_config JSON NOT NULL DEFAULT '{}'"))
            conn.execute(text("ALTER TABLE shots ADD COLUMN edit_config JSON NOT NULL DEFAULT '{}'"))
            conn.execute(text("ALTER TABLE assets ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'video'"))
            conn.execute(text("ALTER TABLE assets ALTER COLUMN duration DROP NOT NULL"))
            conn.execute(text("INSERT INTO schema_version VALUES (2)"))
            version = 2
        if version in (1, 2):
            conn.execute(text("ALTER TABLE projects ADD COLUMN video_settings JSON NOT NULL DEFAULT '{}'"))
            conn.execute(text("INSERT INTO schema_version VALUES (3)"))
            version = 3
        if version == 3:
            conn.execute(text("ALTER TABLE projects ADD COLUMN archived BOOLEAN NOT NULL DEFAULT false"))
            # Preserve original input model/ratio for currently bound legacy videos.
            conn.execute(text("""
                UPDATE assets a SET metadata_json = (
                    jsonb_build_object('video_model', COALESCE(
                        (SELECT j.snapshot->>'video_model' FROM jobs j
                         WHERE j.shot_id=s.id AND j.kind='video' AND j.status='succeeded'
                         ORDER BY j.created_at DESC LIMIT 1), p.video_model),
                        'ratio', COALESCE(
                        (SELECT j.snapshot->>'ratio' FROM jobs j
                         WHERE j.shot_id=s.id AND j.kind='video' AND j.status='succeeded'
                         ORDER BY j.created_at DESC LIMIT 1), p.ratio))
                    || a.metadata_json::jsonb)::json
                FROM shots s JOIN projects p ON p.id=s.project_id
                WHERE a.id=s.asset_id AND a.kind='video'
            """))
            conn.execute(text("INSERT INTO schema_version VALUES (4)"))
            version = 4
        if version == 4:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN error_details JSON"))
            conn.execute(text("INSERT INTO schema_version VALUES (5)"))
            version = 5
        if version == 5:
            AssetBackup.__table__.create(conn)
            conn.execute(text("INSERT INTO schema_version VALUES (6)"))
        elif version != 6:
            raise RuntimeError("数据库版本不兼容")
