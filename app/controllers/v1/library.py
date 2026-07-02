"""
M4: material library + BGM library API.

Exposes app/services/library.py, tagging.py, bgm_library.py as HTTP endpoints
for the new UI — list materials/songs with their Gemini annotations, and
trigger (re)tagging.

Tagging runs synchronously in-request: it's an occasional admin action
(library is small), not part of the per-video hot path. If the library grows
large enough for this to matter, move it onto task_manager like video tasks.
"""
import os

from fastapi import Request, UploadFile
from fastapi.params import File
from fastapi.responses import FileResponse
from loguru import logger

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.schema import (
    MaterialListResponse,
    SongListResponse,
    TaggingTriggerResponse,
)
from app.utils import utils

router = new_router()

_ALLOWED_MATERIAL_SUFFIXES = ("mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png")


def _sanitize_upload_filename(filename: str, request_id: str) -> str:
    normalized_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not normalized_name or normalized_name in {".", ".."}:
        raise HttpException(
            task_id=request_id, status_code=400, message=f"{request_id}: invalid filename"
        )
    return normalized_name


def _material_to_dict(r) -> dict:
    return {
        "id": r.id,
        "path": r.path,
        "duration": r.duration,
        "width": r.width,
        "height": r.height,
        "aspect": r.aspect,
        "description": r.description,
        "tags": r.tags,
        "topic_fit": r.topic_fit,
        "mood": r.mood,
        "quality": r.quality,
        "has_watermark": r.has_watermark,
        "has_embedding": bool(r.embedding),
    }


def _song_to_dict(s) -> dict:
    return {
        "id": s.id,
        "path": s.path,
        "duration": s.duration,
        "mood": s.mood,
        "energy": s.energy,
        "description": s.description,
    }


@router.get(
    "/materials", response_model=MaterialListResponse, summary="List local material library"
)
def get_materials(request: Request):
    from app.services import library as lib

    records = lib.list_all()
    data = {
        "materials": [_material_to_dict(r) for r in records],
        "total": len(records),
    }
    return utils.get_response(200, data)


@router.post(
    "/materials/tagging",
    response_model=TaggingTriggerResponse,
    summary="Trigger Gemini video tagging (incremental, sha256-cached)",
)
def trigger_materials_tagging(request: Request):
    from app.services import tagging

    stats = tagging.run_tagging()
    return utils.get_response(200, stats)


@router.post(
    "/materials",
    response_model=MaterialListResponse,
    summary="Upload a new local material and ingest it into the library",
)
def upload_material(request: Request, file: UploadFile = File(...)):
    """
    Save the upload into storage/local_videos/, run library.ingest_file()
    (quality filter + image→video conversion, same checks as M1), then
    immediately run incremental tagging so it shows up annotated right away.
    """
    from app.services import library as lib
    from app.services import tagging

    request_id = base.get_task_id(request)
    safe_filename = _sanitize_upload_filename(file.filename, request_id)
    normalized = safe_filename.lower()

    if not normalized.endswith(_ALLOWED_MATERIAL_SUFFIXES):
        raise HttpException(
            request_id,
            status_code=400,
            message=f"{request_id}: only {', '.join(_ALLOWED_MATERIAL_SUFFIXES)} files are allowed",
        )

    local_videos_dir = utils.storage_dir("local_videos", create=True)
    save_path = os.path.join(local_videos_dir, safe_filename)
    with open(save_path, "wb+") as buffer:
        file.file.seek(0)
        buffer.write(file.file.read())

    record = lib.ingest_file(
        path=save_path,
        tags=[],
        description="[pending tagging] manually uploaded",
        topic_fit=[],
        mood="neutral",
        quality=5.0,
    )
    if record is None:
        raise HttpException(
            request_id,
            status_code=400,
            message=f"{request_id}: file rejected (resolution too low or unreadable)",
        )

    # Tag just-in-time so the new material shows up annotated immediately.
    try:
        tagging.run_tagging()
    except Exception as e:
        logger.warning(f"upload_material: tagging after ingest failed: {e}")

    fresh = lib.get_by_id(record.id)
    return utils.get_response(200, {"materials": [_material_to_dict(fresh)], "total": 1})


@router.get(
    "/materials/{material_id}/file",
    summary="Stream a material's raw video file (for library preview)",
)
def get_material_file(request: Request, material_id: int):
    from app.services import library as lib

    request_id = base.get_task_id(request)
    record = lib.get_by_id(material_id)
    if record is None or not os.path.isfile(record.path):
        raise HttpException(
            request_id, status_code=404, message=f"{request_id}: material not found"
        )
    return FileResponse(path=record.path, media_type="video/mp4")


@router.get(
    "/songs", response_model=SongListResponse, summary="List BGM library with mood/energy tags"
)
def get_songs(request: Request):
    from app.services import bgm_library

    songs = bgm_library.list_all()
    data = {
        "songs": [_song_to_dict(s) for s in songs],
        "total": len(songs),
    }
    return utils.get_response(200, data)


@router.post(
    "/songs/tagging",
    response_model=TaggingTriggerResponse,
    summary="Trigger Gemini BGM mood/energy tagging (incremental, sha256-cached)",
)
def trigger_songs_tagging(request: Request):
    from app.services import bgm_library

    stats = bgm_library.tag_songs()
    return utils.get_response(200, stats)


@router.get(
    "/songs/{song_id}/file",
    summary="Stream a BGM file (for library preview/audition)",
)
def get_song_file(request: Request, song_id: int):
    from app.services import bgm_library

    request_id = base.get_task_id(request)
    record = bgm_library.get_by_id(song_id)
    if record is None or not os.path.isfile(record.path):
        raise HttpException(
            request_id, status_code=404, message=f"{request_id}: song not found"
        )
    return FileResponse(path=record.path, media_type="audio/mpeg")
