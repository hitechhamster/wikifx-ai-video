"""
M4b: orchestration preview API.

Lets the UI show the plan (segments → visual intent → local match or Pexels)
BEFORE the user commits to generating a video — no Pexels downloads, no
synthesis. Only cost incurred is the per-segment LLM call for visual intent,
which is unavoidable since the preview needs the real intent to be meaningful.
"""
import os

from fastapi import Request

from app.controllers.v1.base import new_router
from app.models.schema import OrchestratorPreviewRequest, OrchestratorPreviewResponse
from app.utils import utils

router = new_router()


@router.post(
    "/orchestrate/preview",
    response_model=OrchestratorPreviewResponse,
    summary="Preview script segmentation + local/Pexels material plan before generating",
)
def preview_orchestration(request: Request, body: OrchestratorPreviewRequest):
    from app.services import library as lib
    from app.services import orchestrator

    shots = orchestrator.preview(body.video_script, body)

    out = []
    local_count = 0
    pexels_count = 0
    for s in shots:
        material_id = None
        material_filename = None
        if s.source == "local" and s.material_path:
            rec = lib.get_by_path(s.material_path)
            material_id = rec.id if rec else None
            material_filename = os.path.basename(s.material_path)
            local_count += 1
        elif s.source == "pexels_preview":
            pexels_count += 1

        out.append({
            "segment_index": s.segment_index,
            "segment_text": s.segment_text,
            "visual_intent": s.visual_intent,
            "source": s.source,
            "material_id": material_id,
            "material_filename": material_filename,
            "score": s.score,
        })

    data = {
        "shots": out,
        "total_segments": len(out),
        "local_count": local_count,
        "pexels_count": pexels_count,
    }
    return utils.get_response(200, data)
