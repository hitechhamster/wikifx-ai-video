"""
Generate 3 'tense' mood BGM tracks via Gemini Lyria → resource/songs/.
Fills the gap found in M3 validation: existing 29 songs had 0 tense-tagged tracks.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import toml
from loguru import logger
import google.genai as genai

cfg = toml.load("config.toml")["app"]
client = genai.Client(api_key=cfg["gemini_api_key"])

PROMPTS = [
    "Tense, urgent instrumental background music for a financial risk warning video. "
    "Driving low strings, dissonant piano stabs, suspenseful rising percussion, no vocals, "
    "instrumental only, loopable, moderate tempo.",

    "Dramatic, high-stakes instrumental track for a forex market volatility alert. "
    "Pulsing bass, sharp staccato strings, ticking clock-like percussion, building tension, "
    "no vocals, instrumental only, loopable.",

    "Urgent, anxious instrumental cue for a stock market crash warning segment. "
    "Dark synth pads, irregular percussive hits, tremolo strings, no vocals, "
    "instrumental only, loopable, fast tempo.",
]

SONG_DIR = os.path.join("resource", "songs")


def main():
    os.makedirs(SONG_DIR, exist_ok=True)
    existing = [f for f in os.listdir(SONG_DIR) if f.startswith("output") and f.endswith(".mp3")]
    next_idx = len(existing)  # continue numbering after existing files (gap at 026 ignored, just append)

    # find max numeric suffix to avoid collision
    max_n = -1
    for f in existing:
        try:
            n = int(f.replace("output", "").replace(".mp3", ""))
            max_n = max(max_n, n)
        except ValueError:
            pass

    generated = []
    for i, prompt in enumerate(PROMPTS):
        idx = max_n + 1 + i
        fname = f"output{idx:03d}.mp3"
        dest = os.path.join(SONG_DIR, fname)

        logger.info(f"generating tense track {i+1}/{len(PROMPTS)} → {fname}")
        try:
            resp = client.models.generate_content(
                model="lyria-3-pro-preview",
                contents=prompt,
            )
            audio_part = None
            for part in resp.candidates[0].content.parts:
                if part.inline_data is not None:
                    audio_part = part.inline_data
                    break
            if audio_part is None:
                logger.error(f"  no audio data in response for {fname}")
                continue

            with open(dest, "wb") as f:
                f.write(audio_part.data)
            size = os.path.getsize(dest)
            logger.success(f"  saved {fname} ({size:,} bytes)")
            generated.append(dest)
        except Exception as e:
            logger.error(f"  failed: {e}")

    logger.info(f"\nGenerated {len(generated)} tense tracks: {generated}")
    logger.info("Run bgm_library.tag_songs() next to tag them (and verify Gemini calls them 'tense').")


if __name__ == "__main__":
    main()
