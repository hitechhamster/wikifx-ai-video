"""
TTS A/B 样本生成:同一段英文外汇文案,Gemini TTS 多个候选 voice 各出一条样本。
OpenAI TTS 暂无 key,先跳过(用户提供 key 后补)。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from loguru import logger
from app.services import voice

SCRIPT = (
    "Gold prices surged today as investors sought safe-haven assets amid escalating "
    "market volatility. The Federal Reserve's upcoming policy decision has traders "
    "on edge, with many repositioning ahead of tomorrow's announcement."
)

GEMINI_CANDIDATES = ["Kore", "Puck", "Charon", "Aoede"]

OUT_DIR = "storage/tts_ab_samples"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    logger.info("=" * 60)
    logger.info("Gemini TTS samples")
    for v in GEMINI_CANDIDATES:
        out_path = os.path.join(OUT_DIR, f"gemini_{v}.mp3")
        logger.info(f"  generating gemini:{v} ...")
        result = voice.gemini_tts(SCRIPT, v, voice_rate=1.2, voice_file=out_path)
        if result:
            size = os.path.getsize(out_path)
            logger.success(f"    OK → {out_path} ({size:,} bytes)")
        else:
            logger.error(f"    FAILED for voice {v}")

    openai_key = ""
    try:
        import toml
        openai_key = toml.load("config.toml")["app"].get("openai_api_key", "")
    except Exception:
        pass

    if openai_key:
        logger.info("=" * 60)
        logger.info("OpenAI TTS samples")
        for v in ["alloy", "onyx", "nova"]:
            out_path = os.path.join(OUT_DIR, f"openai_{v}.mp3")
            logger.info(f"  generating openai:{v} ...")
            result = voice.openai_tts(SCRIPT, v, voice_rate=1.2, voice_file=out_path)
            if result:
                size = os.path.getsize(out_path)
                logger.success(f"    OK → {out_path} ({size:,} bytes)")
            else:
                logger.error(f"    FAILED for voice {v}")
    else:
        logger.warning("OpenAI API key not set in config.toml — skipping OpenAI samples for now")

    logger.info("=" * 60)
    logger.info(f"All samples saved under: {os.path.abspath(OUT_DIR)}")


if __name__ == "__main__":
    main()
