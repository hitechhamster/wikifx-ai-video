# -*- coding: utf-8 -*-
"""
批量出片:一个模板文件夹 = 一种节目形态(外汇新闻/诈骗预警/…)。

模板文件夹结构:
  templates/外汇新闻/
    config.toml      所有参数(语速/角标/BGM/语言/片头片尾…)
    bgm.mp3          这个模板用的配乐(config 里 bgm_file 相对引用)
    scripts.xlsx     一行一条视频; 列: script(必), title/subject/badge/language(选)

用法:
  python batch.py templates/外汇新闻
输出:
  output/外汇新闻_2026-06-24_153000/[title].mp4  +  results.csv

设计:在进程内顺序调用生成流水线(不开服务/不走浏览器/不轮询),一条接一条,
跑完自动加好片头片尾。某条失败不影响其它条,结果汇总进 results.csv。
"""
import csv
import os
import shutil
import sys
import time
import tomllib
import traceback
from datetime import datetime

# 统一以项目根为工作目录,保证 storage/ resource/ 等相对路径与 app 导入都正确。
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

# 不是 VideoParams 的字段,构造 params 前要剔除(片头片尾 / BGM 处理相关)。
_BUMPER_KEYS = {
    "intro_enabled", "intro_top", "intro_bottom", "outro_video",
    "intro_image", "intro_text", "outro_seconds", "bgm_tense_window",
    "intro_clip_video", "intro_clip_start", "intro_clip_end",
    "outro_clip_video", "outro_clip_start", "outro_clip_end", "outro_freeze_tail",
}
# Excel 里允许的逐行覆盖列(不填就用模板默认)。
_ROW_OVERRIDES = {"subject", "badge", "language", "title"}


def load_config(template_dir: str) -> dict:
    cfg_path = os.path.join(template_dir, "config.toml")
    if not os.path.isfile(cfg_path):
        raise FileNotFoundError(f"模板缺少 config.toml: {cfg_path}")
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)

    # bgm_file 相对模板文件夹 → 绝对路径
    if cfg.get("bgm_file"):
        bgm = cfg["bgm_file"]
        if not os.path.isabs(bgm):
            bgm = os.path.abspath(os.path.join(template_dir, bgm))
        cfg["bgm_file"] = bgm
    # outro_video / intro_image / 片头片尾品牌视频 相对项目根 → 绝对路径
    for key in ("outro_video", "intro_image", "intro_clip_video", "outro_clip_video"):
        if cfg.get(key) and not os.path.isabs(cfg[key]):
            cfg[key] = os.path.abspath(os.path.join(PROJECT_ROOT, cfg[key]))
    return cfg


def read_scripts(template_dir: str) -> list[dict]:
    """读 scripts.xlsx(优先)或 scripts.csv。表头第一行,必须含 script 列。"""
    import glob
    xlsx = os.path.join(template_dir, "scripts.xlsx")
    csv_path = os.path.join(template_dir, "scripts.csv")
    rows = []
    if os.path.isfile(xlsx):
        import openpyxl
        wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
        ws = wb.active
        headers = None
        for r in ws.iter_rows(values_only=True):
            if headers is None:
                headers = [str(c).strip().lower() if c is not None else "" for c in r]
                continue
            if not any(r):
                continue
            rows.append({headers[i]: (r[i] if i < len(r) else None) for i in range(len(headers))})
        wb.close()
    elif os.path.isfile(csv_path):
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            for d in csv.DictReader(f):
                rows.append({(k or "").strip().lower(): v for k, v in d.items()})
    else:
        raise FileNotFoundError(f"模板缺少 scripts.xlsx 或 scripts.csv: {template_dir}")

    # 保留有内容的行(新闻模式看 script 列,混剪模式看 hooks 列)
    out = []
    for r in rows:
        if (str(r.get("script") or "").strip()) or (str(r.get("hooks") or "").strip()):
            out.append(r)
    return out


def build_params(cfg: dict, row: dict):
    from app.models.schema import VideoParams

    params_dict = {k: v for k, v in cfg.items() if k not in _BUMPER_KEYS}
    params_dict["video_script"] = (row.get("script") or "").strip()

    # 逐行覆盖
    if row.get("subject"):
        params_dict["video_subject"] = str(row["subject"]).strip()
    if row.get("language"):
        params_dict["video_language"] = str(row["language"]).strip()
    if row.get("badge"):
        params_dict["news_badge_text"] = str(row["badge"]).strip()

    params_dict.setdefault("video_subject", "Forex News")
    return VideoParams(**params_dict)


def generate_one(params) -> str:
    """进程内同步跑完整流水线,返回 final-1.mp4 绝对路径(失败返回空串)。"""
    from app.services import state as sm
    from app.services import task as tm
    from app.services import tagging
    from app.utils import utils

    # 严格相关性是全局开关,按本条视频的设置 set,跑完 reset,避免污染同进程后续任务。
    tagging.set_strict_relevance(bool(getattr(params, "strict_topic_relevance", False)))
    task_id = utils.get_uuid()
    sm.state.update_task(task_id)
    try:
        tm.start(task_id=task_id, params=params, stop_at="video")
    finally:
        tagging.set_strict_relevance(False)
    final = os.path.join(utils.task_dir(task_id), "final-1.mp4")
    return final if (os.path.isfile(final) and os.path.getsize(final) > 0) else ""


def main(template_dir: str):
    template_dir = os.path.abspath(template_dir)
    template_name = os.path.basename(template_dir.rstrip("\\/"))
    cfg = load_config(template_dir)
    rows = read_scripts(template_dir)
    if not rows:
        print("没有可生成的脚本行。")
        return

    # 新闻管线的 get_bgm_file 有路径安全限制(只允许 resource/songs,防 API 用户传
    # 任意路径)。模板里的 bgm 在模板文件夹、会被误拦,所以这里把它暂存进 resource/
    # songs(通过安全检查),跑完清理。montage 模式不走 get_bgm_file,不受影响。
    staged_bgm = ""
    mode = str(cfg.get("mode", "news")).strip().lower()
    if mode != "montage" and cfg.get("bgm_file"):
        from app.utils import utils as _u
        import glob as _glob
        song_dir = _u.song_dir()
        # 先删掉本模板上一批遗留的暂存 BGM(Windows 上结束时偶发文件锁没删成,进程退出
        # 后锁已释放,这里补删)。只删自己模板的那一个文件名,不要 glob 全部 _tpl_*.mp3
        # ——否则两个批量并行时会误删对方正在用的暂存 BGM。
        _self_stale = os.path.join(song_dir, f"_tpl_{template_name}.mp3")
        if os.path.isfile(_self_stale):
            try: os.remove(_self_stale)
            except OSError: pass
        bgm_abs = os.path.abspath(cfg["bgm_file"])
        if os.path.isfile(bgm_abs):
            staged_bgm = os.path.join(song_dir, f"_tpl_{template_name}.mp3")
            try:
                if cfg.get("bgm_tense_window"):
                    # 音纹分析:截 BGM 最紧张的一段当配乐(混音时循环铺满全片)。
                    # 产物直接写进 songs 目录,天然过 get_bgm_file 安全检查。
                    import bgm_tense
                    st = bgm_tense.most_tense_segment(
                        bgm_abs, staged_bgm, window=float(cfg["bgm_tense_window"]))
                    print(f"BGM 音纹分析:取最紧张 {cfg['bgm_tense_window']}s(start={st:.1f}s)")
                    cfg["bgm_file"] = staged_bgm
                elif os.path.dirname(bgm_abs) != os.path.abspath(song_dir):
                    shutil.copy2(bgm_abs, staged_bgm)
                    cfg["bgm_file"] = staged_bgm
                else:
                    staged_bgm = ""   # 已在 songs 目录内,无需暂存
            except Exception as e:
                print(f"BGM 处理失败,本批将无 BGM: {e}")
                staged_bgm = ""

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = os.path.join(PROJECT_ROOT, "output", f"{template_name}_{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    print(f"模板: {template_name} | {len(rows)} 条 | 输出: {out_dir}\n")

    results = []
    for i, row in enumerate(rows, 1):
        title = (str(row.get("title")).strip() if row.get("title") else "") or f"row{i:02d}"
        safe_title = "".join(c for c in title if c not in '\\/:*?"<>|').strip() or f"row{i:02d}"
        out_path = os.path.join(out_dir, f"{safe_title}.mp4")
        t0 = time.time()
        print(f"[{i}/{len(rows)}] {title} … 生成中")
        status, err = "ok", ""
        mode = str(cfg.get("mode", "news")).strip().lower()
        try:
            # —— 产出"正片"(content video):新闻模式走脚本管线,混剪模式走 montage ——
            if mode == "montage":
                import montage as mtg
                hooks = [h.strip() for h in str(row.get("hooks") or "").replace("\n", "|").split("|") if h.strip()]
                kw = None
                if row.get("keywords"):
                    kw = [k.strip() for k in str(row["keywords"]).replace("\n", "|").replace(",", "|").split("|") if k.strip()]
                main_video = os.path.join(out_dir, f"_main_{i:02d}.mp4")
                vo = (str(row["voiceover"]).strip() if row.get("voiceover")
                      else cfg.get("voiceover", ""))
                mtg.generate_montage(
                    main_video, cfg["bgm_file"], hooks, keywords=kw or cfg.get("keywords"),
                    aspect=cfg.get("video_aspect", "9:16"),
                    clip_min=cfg.get("clip_min", 0.7), clip_max=cfg.get("clip_max", 1.4),
                    speed=cfg.get("clip_speed_factor", 1.0), n_sources=cfg.get("n_sources", 12),
                    target_seconds=cfg.get("target_seconds", 25.0),
                    voiceover=vo, voice_name=cfg.get("voice_name", "gemini:Puck-Male"),
                    voice_rate=cfg.get("voice_rate", 1.0),
                )
                if not os.path.isfile(main_video):
                    main_video, status, err = "", "failed", "montage 未产出"
            else:
                params = build_params(cfg, row)
                main_video = generate_one(params)
                if not main_video:
                    status, err = "failed", "生成未产出 final-1.mp4"

            # —— 片头片尾(两种模式共用)——
            if main_video:
                if cfg.get("intro_clip_video") or cfg.get("outro_clip_video"):
                    # 片头/片尾从同一条品牌视频里按时间段截取(wikigold 投放尾板:
                    # 0-1s 当片头,7-9s 当下载 CTA 片尾)。
                    import make_intro_outro as mio
                    intro = os.path.join(out_dir, "_intro.mp4")
                    outro = os.path.join(out_dir, "_outro.mp4")
                    iv = cfg.get("intro_clip_video"); ov = cfg.get("outro_clip_video")
                    mio.extract_clip(iv, cfg.get("intro_clip_start", 0.0),
                                     cfg.get("intro_clip_end", 1.0), intro)
                    mio.extract_clip(ov, cfg.get("outro_clip_start", 0.0),
                                     cfg.get("outro_clip_end", 2.0), outro,
                                     freeze_tail=cfg.get("outro_freeze_tail", 0.0))
                    mio.concat(intro, main_video, outro, out_path, outro_seconds=None)
                elif cfg.get("intro_enabled") and cfg.get("outro_video") and os.path.isfile(cfg["outro_video"]):
                    import make_intro_outro as mio
                    whoosh = os.path.join(out_dir, "_whoosh.wav")
                    intro = os.path.join(out_dir, "_intro.mp4")
                    mio.make_whoosh(whoosh)
                    intro_text = (str(row["badge"]).strip() if row.get("badge")
                                  else cfg.get("intro_text") or cfg.get("news_badge_text") or "WIKIFX")
                    if cfg.get("intro_image") and os.path.isfile(cfg["intro_image"]):
                        # 图片片头 + 从左到右擦入文字动效
                        mio.make_image_intro(cfg["intro_image"], intro_text, whoosh, intro)
                    else:
                        # 旧的生成式标题卡
                        mio.make_intro(cfg.get("intro_top", "WIKIFX"), intro_text, whoosh, intro)
                    mio.concat(intro, main_video, cfg["outro_video"], out_path,
                               outro_seconds=cfg.get("outro_seconds"))
                else:
                    shutil.copy2(main_video, out_path)
                # 清理混剪临时正片
                if mode == "montage" and os.path.isfile(main_video):
                    try: os.remove(main_video)
                    except OSError: pass
        except Exception as e:
            status, err = "failed", f"{type(e).__name__}: {e}"
            traceback.print_exc()
        dur = round(time.time() - t0, 1)
        results.append({"row": i, "title": title, "status": status,
                        "output": out_path if status == "ok" else "",
                        "seconds": dur, "error": err})
        print(f"    -> {status} ({dur}s)" + (f"  {err}" if err else ""))

    # 清理临时 + 写 results.csv
    for tmp in ("_whoosh.wav", "_intro.mp4", "_outro.mp4"):
        p = os.path.join(out_dir, tmp)
        if os.path.isfile(p):
            try: os.remove(p)
            except OSError: pass
    # 清理暂存进 resource/songs 的模板 BGM
    if staged_bgm and os.path.isfile(staged_bgm):
        try: os.remove(staged_bgm)
        except OSError: pass
    res_csv = os.path.join(out_dir, "results.csv")
    with open(res_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["row", "title", "status", "output", "seconds", "error"])
        w.writeheader()
        w.writerows(results)

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\n完成: {ok}/{len(results)} 成功 | 汇总: {res_csv}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python batch.py <模板文件夹>  例: python batch.py templates\\外汇新闻")
        sys.exit(1)
    main(sys.argv[1])
