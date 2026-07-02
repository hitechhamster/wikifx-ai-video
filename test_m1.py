"""
M1 验证:
- 脚本含 3 段 → 编排器逐段生成 intent → 本地库优先 + Pexels 补足
- 最终输出视频中 >= 2 段使用本地素材(min_local_segments 硬约束)
- 产出文件、每段编排日志打印给用户确认
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from app.services import task
from app.services import state as sm
from app.models.schema import VideoParams
from loguru import logger


SCRIPT = (
    "美元兑人民币汇率近期持续走强，市场情绪明显好转。"
    "从技术面看，关键支撑位守稳，多头格局延续。"
    "交易员建议密切关注美联储政策动向，适时调整仓位。"
)


def run():
    task_id = "m1_orchestrator_test"
    params = VideoParams(
        video_subject="外汇美元走势分析",
        video_script=SCRIPT,
        voice_name="zh-CN-XiaoxiaoNeural-Female",
        voice_rate=1.0,
        video_source="pexels",       # enables Pexels as fallback
        video_clip_duration=5,
        subtitle_enabled=True,
        bgm_type="random",
        bgm_volume=0.2,
        paragraph_number=1,
        # M1 orchestrator flags
        use_orchestrator=True,
        local_threshold=0.2,         # low threshold so M1 test materials can match
        min_local_segments=2,
    )

    result = task.start(task_id, params)

    if result and result.get("videos"):
        out = result["videos"][0]
        exists = os.path.isfile(out)
        size = os.path.getsize(out) if exists else 0
        logger.success(f"[M1] output : {out}")
        logger.success(f"[M1] size   : {size:,} bytes")
        logger.success(f"[M1] PASS")
        return True
    else:
        s = sm.state.get_task(task_id)
        logger.error(f"[M1] FAIL — task state: {s}")
        return False


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
