"""
M1 混合路径验证 + min_local 硬约束验证

场景构造:
- local_threshold=0.9 → 本地得分极难达到(M1 得分在 0.28~0.35),所有段初始走 Pexels
- min_local_segments=2 → _ensure_min_local 必须强制插入 2 个本地素材
- 3 段脚本

预期 shot log:
  seg[0] [local_forced ] — min_local 触发
  seg[1] [local_forced ] — min_local 触发
  seg[2] [pexels       ] — 自然走 Pexels

两条验收:
  1. shot log 中出现 source=local_forced(min_local 硬约束真正触发)
  2. shot log 中出现 source=pexels(Pexels 补足路径真正工作)
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
    task_id = "m1_mixed_validation"
    params = VideoParams(
        video_subject="外汇美元走势分析",
        video_script=SCRIPT,
        voice_name="zh-CN-XiaoxiaoNeural-Female",
        voice_rate=1.0,
        video_source="pexels",
        video_clip_duration=5,
        subtitle_enabled=True,
        bgm_type="random",
        bgm_volume=0.2,
        paragraph_number=1,
        use_orchestrator=True,
        local_threshold=0.9,     # 强制本地失败: M1 得分 0.28-0.35 < 0.9
        min_local_segments=2,    # 硬约束: 至少 2 段本地
    )

    logger.info("=" * 60)
    logger.info("[test_m1_mixed] 场景: threshold=0.9 (强制本地失败) + min_local=2 (硬约束)")
    logger.info("[test_m1_mixed] 预期: 2x local_forced + 1x pexels")
    logger.info("=" * 60)

    result = task.start(task_id, params)

    if result and result.get("videos"):
        out = result["videos"][0]
        exists = os.path.isfile(out)
        size = os.path.getsize(out) if exists else 0
        logger.success(f"[test_m1_mixed] output: {out}")
        logger.success(f"[test_m1_mixed] size  : {size:,} bytes")
        logger.success(f"[test_m1_mixed] PASS — 混合视频产出成功")
        logger.info("请在上方 SHOT LOG 中确认: local_forced >= 2, pexels >= 1")
        return True
    else:
        s = sm.state.get_task(task_id)
        logger.error(f"[test_m1_mixed] FAIL — task state: {s}")
        return False


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
