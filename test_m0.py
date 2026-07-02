"""
M0 验证脚本：
1. local 模式 - 用参考项目缓存视频跑"配音→字幕→合成"链路
2. pexels 模式 - 验证在线检索+下载
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app.services import task
from app.services import state as sm
from app.models.schema import VideoParams, MaterialInfo
from app.utils import utils
from loguru import logger

REF_CACHE = r"D:\WIKIFX项目\AI自动剪辑\storage\local_videos"


def run_local_test():
    logger.info("=" * 60)
    logger.info("TEST 1: local 模式")
    logger.info("=" * 60)

    mp4_files = [f for f in os.listdir(REF_CACHE) if f.endswith(".mp4")][:4]
    if not mp4_files:
        logger.error("找不到本地 mp4 文件，跳过 local 测试")
        return False

    materials = [
        MaterialInfo(provider="local", url=os.path.join(REF_CACHE, f), duration=10)
        for f in mp4_files
    ]
    logger.info(f"使用 {len(materials)} 个本地素材")

    task_id = "m0_local_test"
    params = VideoParams(
        video_subject="外汇美元走势分析",
        video_script="美元兑人民币汇率近期持续波动，市场情绪趋于谨慎。技术面显示关键支撑位守稳，投资者需密切关注美联储政策动向。",
        voice_name="zh-CN-XiaoxiaoNeural-Female",
        voice_rate=1.0,
        video_source="local",
        video_materials=materials,
        video_clip_duration=5,
        subtitle_enabled=True,
        bgm_type="random",
        bgm_volume=0.2,
        paragraph_number=1,
    )

    result = task.start(task_id, params)
    if result and result.get("videos"):
        out = result["videos"][0]
        exists = os.path.isfile(out)
        size = os.path.getsize(out) if exists else 0
        logger.success(f"[LOCAL] 输出文件: {out}")
        logger.success(f"[LOCAL] 文件存在: {exists}, 大小: {size:,} bytes")
        return exists and size > 0
    else:
        state = sm.state.get_task(task_id)
        logger.error(f"[LOCAL] 任务失败，state: {state}")
        return False


def run_pexels_test():
    logger.info("=" * 60)
    logger.info("TEST 2: pexels 模式")
    logger.info("=" * 60)

    task_id = "m0_pexels_test"
    params = VideoParams(
        video_subject="forex dollar exchange rate",
        video_script="The US dollar has been strengthening against major currencies. Traders are watching key support levels as market volatility increases.",
        voice_name="en-US-JennyNeural-Female",
        voice_rate=1.0,
        video_source="pexels",
        video_clip_duration=5,
        subtitle_enabled=True,
        bgm_type="random",
        bgm_volume=0.2,
        paragraph_number=1,
    )

    result = task.start(task_id, params)
    if result and result.get("videos"):
        out = result["videos"][0]
        exists = os.path.isfile(out)
        size = os.path.getsize(out) if exists else 0
        logger.success(f"[PEXELS] 输出文件: {out}")
        logger.success(f"[PEXELS] 文件存在: {exists}, 大小: {size:,} bytes")
        return exists and size > 0
    else:
        state = sm.state.get_task(task_id)
        logger.error(f"[PEXELS] 任务失败，state: {state}")
        return False


if __name__ == "__main__":
    local_ok = run_local_test()
    pexels_ok = run_pexels_test()

    logger.info("=" * 60)
    logger.info(f"LOCAL  测试: {'PASS' if local_ok else 'FAIL'}")
    logger.info(f"PEXELS 测试: {'PASS' if pexels_ok else 'FAIL'}")
    logger.info(f"M0 总体: {'PASS' if local_ok and pexels_ok else 'FAIL'}")
    logger.info("=" * 60)
