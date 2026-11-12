from typing import Literal

from pydantic import BaseModel, Field, model_validator


class VideoSettings(BaseModel):
    resolution: Literal["480p", "720p", "1080p"] = "720p"
    generate_audio: bool = True
    watermark: bool = False
    execution_expires_after: int = Field(default=172800, ge=3600, le=259200)


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    brief: str = Field(min_length=5, max_length=6000)
    style: str = Field(default="", max_length=2000)
    characters: str = Field(default="", max_length=3000)
    duration: int = Field(default=30, ge=4, le=180)
    ratio: Literal["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"] = "16:9"
    script_model: str = Field(min_length=1, max_length=150)
    video_model: str = Field(min_length=1, max_length=150)
    video_settings: VideoSettings = Field(default_factory=VideoSettings)


class Character(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1500)


class ShotSpec(BaseModel):
    duration: int = Field(ge=4, le=15)
    scene: str = Field(min_length=1, max_length=1500)
    characters: list[str] = Field(default_factory=list, max_length=10)
    action: str = Field(min_length=1, max_length=2000)
    camera: str = Field(min_length=1, max_length=1000)
    dialogue: str = Field(default="", max_length=1500)
    sound: str = Field(default="", max_length=1000)
    start_state: str = Field(default="", max_length=1500)
    end_state: str = Field(default="", max_length=1500)
    continuity_notes: str = Field(default="", max_length=1500)


class ReferenceFrameInput(BaseModel):
    source_shot_id: str = Field(min_length=1, max_length=36)
    source_asset_id: str = Field(min_length=1, max_length=36)
    frame_index: int | None = Field(default=None, ge=0, le=21600)


class ImageUpload(BaseModel):
    data: str = Field(min_length=1, max_length=40 * 1024 * 1024)


class RemoteReference(BaseModel):
    kind: Literal["video", "audio"]
    url: str = Field(min_length=1, max_length=2048)
    duration: float = Field(ge=2, le=15, allow_inf_nan=False)


class GenerationInput(BaseModel):
    mode: Literal["text", "first_frame", "first_last_frame", "reference"] = "text"
    reference_asset_id: str | None = Field(default=None, max_length=36)
    last_frame_asset_id: str | None = Field(default=None, max_length=36)
    reference_image_ids: list[str] = Field(default_factory=list, max_length=9)
    remote_references: list[RemoteReference] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def valid_mode(self):
        if (self.mode in ("first_frame", "first_last_frame")) != bool(self.reference_asset_id):
            raise ValueError("首帧模式须选择首帧，其他模式不能附带首帧")
        if (self.mode == "first_last_frame") != bool(self.last_frame_asset_id):
            raise ValueError("首尾帧模式须同时选择首帧和尾帧")
        if self.mode != "reference" and (self.reference_image_ids or self.remote_references):
            raise ValueError("多模态参考不能与首帧或首尾帧混用")
        if self.mode == "reference" and not (self.reference_image_ids or self.remote_references):
            raise ValueError("请至少添加一份参考素材")
        if len(set(self.reference_image_ids)) != len(self.reference_image_ids):
            raise ValueError("参考图片不能重复")
        videos = [r for r in self.remote_references if r.kind == "video"]
        audios = [r for r in self.remote_references if r.kind == "audio"]
        if len(videos) > 3 or len(audios) > 3 or sum(r.duration for r in videos) > 15 or sum(r.duration for r in audios) > 15:
            raise ValueError("视频、音频各最多 3 份，各类总时长不超过 15 秒")
        if len(self.reference_image_ids) + len(self.remote_references) > 12:
            raise ValueError("多模态参考合计最多 12 份")
        if audios and not (videos or self.reference_image_ids):
            raise ValueError("音频参考须搭配图片或视频")
        return self


class Script(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    synopsis: str = Field(min_length=1, max_length=3000)
    style_bible: str = Field(min_length=1, max_length=3000)
    characters: list[Character] = Field(max_length=10)
    shots: list[ShotSpec] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def valid_refs(self):
        names = {c.name for c in self.characters}
        if len(names) != len(self.characters):
            raise ValueError("角色名称不能重复")
        if any(set(s.characters) - names for s in self.shots):
            raise ValueError("分镜引用了未定义角色")
        if sum(s.duration for s in self.shots) > 180:
            raise ValueError("总时长不得超过 180 秒")
        return self


class RecoveryInput(BaseModel):
    external_id: str | None = Field(default=None, max_length=150, pattern=r"^[A-Za-z0-9_-]+$")
    acknowledge_duplicate_cost: bool = False
