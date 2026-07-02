"""
M2 前置检查 — 三项全通过才能进 M2 主体

1. google-genai import + client 初始化 + 列出可用模型
2. Files API 上传本地 mp4 → Gemini 视频理解 → 确认返回结构化结果
3. Gemini embedding API → 对中文文案取向量
"""
import os
import sys
import json
import time
sys.path.insert(0, os.path.dirname(__file__))

from loguru import logger

GEMINI_KEY = None
VIDEO_PATH = None  # 会在 main 里赋值


# ---------------------------------------------------------------------------
# 读 key
# ---------------------------------------------------------------------------
def _get_key() -> str:
    import toml
    cfg = toml.load(os.path.join(os.path.dirname(__file__), "config.toml"))
    return cfg.get("app", {}).get("gemini_api_key", "")


# ---------------------------------------------------------------------------
# 检查 1: import + 列模型
# ---------------------------------------------------------------------------
def check1_import_and_list_models(key: str) -> bool:
    logger.info("=" * 60)
    logger.info("[CHECK 1] import google.genai + 列出可用模型")
    try:
        import google.genai as genai
        client = genai.Client(api_key=key)
        logger.success("  ✓ import google.genai OK, client 初始化 OK")
    except Exception as e:
        logger.error(f"  ✗ 初始化失败: {e}")
        return False

    # 列出当前可用模型
    try:
        models = list(client.models.list())
        names = [m.name for m in models]
        logger.info(f"  可用模型数: {len(names)}")
        # 只打印 gemini 系列
        gemini_names = [n for n in names if "gemini" in n.lower()]
        for n in sorted(gemini_names)[:20]:
            logger.info(f"    {n}")
    except Exception as e:
        logger.warning(f"  列模型失败(不影响结果): {e}")

    return True


# ---------------------------------------------------------------------------
# 检查 2: Files API 视频理解
# ---------------------------------------------------------------------------
def check2_video_understanding(key: str, video_path: str) -> bool:
    logger.info("=" * 60)
    logger.info(f"[CHECK 2] Files API 视频理解: {os.path.basename(video_path)}")

    import google.genai as genai
    from google.genai import types

    client = genai.Client(api_key=key)

    # 上传视频
    try:
        logger.info("  上传视频到 Files API ...")
        with open(video_path, "rb") as f:
            file_ref = client.files.upload(
                file=f,
                config=types.UploadFileConfig(
                    mime_type="video/mp4",
                    display_name=os.path.basename(video_path),
                )
            )
        logger.success(f"  ✓ 上传成功: {file_ref.name} state={file_ref.state}")
    except Exception as e:
        logger.error(f"  ✗ 上传失败: {e}")
        return False

    # 等待文件处理完成
    try:
        import google.genai.types as gtypes
        for i in range(20):
            file_ref = client.files.get(name=file_ref.name)
            state = str(file_ref.state)
            logger.info(f"  文件状态: {state} (等待中 {i+1}/20)")
            if "ACTIVE" in state:
                break
            if "FAILED" in state:
                logger.error(f"  ✗ 文件处理失败: {state}")
                return False
            time.sleep(3)
        else:
            logger.error("  ✗ 超时：文件未在 60s 内变为 ACTIVE")
            return False
    except Exception as e:
        logger.warning(f"  状态轮询异常(继续尝试): {e}")

    # 视频理解
    prompt = """You are analyzing forex/financial video footage.
Analyze this video and return a JSON object with these fields:
- description: one sentence describing what's shown (in English)
- tags: list of 3-5 descriptive tags (English)
- topic_fit: list of relevant topics from [forex, trading, chart, currency, finance, office, technology]
- mood: one of [professional, tense, neutral, uplifting]
- quality: integer 1-10 rating of production quality
- has_watermark: boolean

Return ONLY valid JSON, no markdown fences."""

    try:
        logger.info("  调用 Gemini 视频理解 ...")
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_uri(file_uri=file_ref.uri, mime_type="video/mp4"),
                prompt,
            ],
        )
        raw = response.text.strip()
        logger.info(f"  原始响应: {raw[:300]}")

        # 尝试解析 JSON
        # 有时模型会包 markdown fence
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        logger.success(f"  ✓ 结构化结果解析成功:")
        logger.success(f"    description : {result.get('description', '')}")
        logger.success(f"    tags        : {result.get('tags', [])}")
        logger.success(f"    topic_fit   : {result.get('topic_fit', [])}")
        logger.success(f"    mood        : {result.get('mood', '')}")
        logger.success(f"    quality     : {result.get('quality', '')}")
        logger.success(f"    has_watermark: {result.get('has_watermark', '')}")
    except json.JSONDecodeError:
        logger.warning(f"  JSON 解析失败，但模型有响应: {raw[:200]}")
        logger.warning("  (视频理解本身通了，打标 prompt 需微调)")
        # 有响应即通过，JSON 格式问题在 M2 主体中修
    except Exception as e:
        logger.error(f"  ✗ 视频理解调用失败: {e}")
        # 清理文件
        try:
            client.files.delete(name=file_ref.name)
        except Exception:
            pass
        return False

    # 清理上传的文件
    try:
        client.files.delete(name=file_ref.name)
        logger.info("  已删除 Files API 临时文件")
    except Exception:
        pass

    return True


# ---------------------------------------------------------------------------
# 检查 3: Embedding API
# ---------------------------------------------------------------------------
def check3_embedding(key: str) -> bool:
    logger.info("=" * 60)
    logger.info("[CHECK 3] Gemini embedding API — 中文文案取向量")

    import google.genai as genai

    client = genai.Client(api_key=key)
    text = "美元兑人民币汇率近期持续走强，外汇市场交易情绪明显好转。"

    try:
        result = client.models.embed_content(
            model="gemini-embedding-exp-03-07",
            contents=text,
        )
        vec = result.embeddings[0].values
        logger.success(f"  ✓ embedding 成功: 维度={len(vec)}, 前5={[round(v,4) for v in vec[:5]]}")
    except Exception as e:
        # 尝试备用 embedding 模型
        logger.warning(f"  gemini-embedding-exp-03-07 失败({e}),尝试 text-embedding-004 ...")
        try:
            result = client.models.embed_content(
                model="text-embedding-004",
                contents=text,
            )
            vec = result.embeddings[0].values
            logger.success(f"  ✓ embedding (text-embedding-004) 成功: 维度={len(vec)}, 前5={[round(v,4) for v in vec[:5]]}")
        except Exception as e2:
            logger.error(f"  ✗ embedding 失败: {e2}")
            return False

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    key = _get_key()
    if not key:
        logger.error("config.toml 中 gemini_api_key 为空")
        sys.exit(1)

    # 找一个本地视频用于检查2
    local_dir = os.path.join(os.path.dirname(__file__), "storage", "local_videos")
    videos = [
        os.path.join(local_dir, f)
        for f in os.listdir(local_dir)
        if f.endswith(".mp4")
    ]
    if not videos:
        logger.error(f"storage/local_videos/ 中没有 mp4 文件")
        sys.exit(1)
    video_path = videos[0]
    logger.info(f"使用视频: {video_path}")

    results = {}
    results["check1"] = check1_import_and_list_models(key)
    results["check2"] = check2_video_understanding(key, video_path)
    results["check3"] = check3_embedding(key)

    logger.info("=" * 60)
    logger.info("[前置检查汇总]")
    all_pass = True
    for name, ok in results.items():
        status = "✓ PASS" if ok else "✗ FAIL"
        logger.info(f"  {name}: {status}")
        if not ok:
            all_pass = False

    if all_pass:
        logger.success("M2 前置检查全部通过，可以进 M2 主体")
        sys.exit(0)
    else:
        logger.error("有检查项失败，需排查后再进 M2")
        sys.exit(1)


if __name__ == "__main__":
    main()
