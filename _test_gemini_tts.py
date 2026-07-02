import os, sys, toml
sys.path.insert(0, os.path.dirname(__file__))
import google.genai as genai
from google.genai import types

cfg = toml.load("config.toml")["app"]
client = genai.Client(api_key=cfg["gemini_api_key"])

text = "Gold prices surged today as investors sought safe-haven assets amid market volatility."

for model in ["gemini-2.5-flash-preview-tts", "gemini-3.1-flash-tts-preview"]:
    print(f"\n=== {model} ===")
    try:
        response = client.models.generate_content(
            model=model,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
                    )
                ),
            ),
        )
        part = response.candidates[0].content.parts[0]
        data = part.inline_data.data
        mime = part.inline_data.mime_type
        print(f"OK: mime_type={mime}, bytes={len(data)}")
        with open(f"_tts_test_{model.replace('.', '_').replace('-', '_')}.raw", "wb") as f:
            f.write(data)
    except Exception as e:
        print(f"ERROR: {repr(e)[:400]}")
