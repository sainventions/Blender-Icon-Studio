"""Server-only API shapes mirrored from web/src/types.ts (SystemStatus, SampleIcon, ProjectSummary, WsEvent)
and request bodies of the REST table in PLAN §8 that are not part of the shared bis.models contract."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from .models import AppearanceId, CameraSpec, Quality, SplitStrategy

WorkerState = Literal["stopped", "starting", "ready", "busy", "error"]


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------------------------- system
class BlenderInfo(_M):
    found: bool
    path: Optional[str] = None
    version: Optional[str] = None


class WorkerStatus(_M):
    state: WorkerState = "stopped"
    pid: Optional[int] = None
    message: Optional[str] = None


class GpuStatus(_M):
    name: Optional[str] = None
    device: Optional[str] = None
    memoryUsedMB: Optional[float] = None
    memoryTotalMB: Optional[float] = None
    utilization: Optional[float] = None
    temperature: Optional[float] = None


class QueueStatus(_M):
    queued: int = 0
    running: int = 0


class SystemStatus(_M):
    blender: BlenderInfo
    worker: WorkerStatus
    gpu: GpuStatus
    queue: QueueStatus


# ---------------------------------------------------------------------------------------------- library
class SampleIcon(_M):
    name: str
    file: str
    url: str
    # additive extras (not in types.ts yet; harmless for the UI)
    thumbnail: Optional[str] = None
    collection: Optional[str] = None


class ProjectSummary(_M):
    id: str
    name: str
    updatedAt: str
    thumbnail: Optional[str] = None
    layerCount: int = 0


# ---------------------------------------------------------------------------------------------- bodies
class CreateFromSample(_M):
    sample: str
    strategy: SplitStrategy = "smart"
    name: Optional[str] = None


class SplitBody(_M):
    strategy: SplitStrategy = "smart"


class MergeBody(_M):
    layerIds: list[str]


class SplitLayerBody(_M):
    mode: Literal["elements", "islands"] = "elements"


class MoveElementsBody(_M):
    elementIds: list[str]
    toLayerId: Optional[str] = None


class RenditionsBody(_M):
    quality: Quality = "draft"
    size: Optional[int] = None            # default: 256 px thumbnails for the appearance strip
    appearances: Optional[list[AppearanceId]] = None
    camera: Optional[CameraSpec] = None


class BlendBody(_M):
    open: bool = False
    appearance: Optional[AppearanceId] = None


class SwatchesBody(_M):
    size: int = 192
    quality: Quality = "preview"
