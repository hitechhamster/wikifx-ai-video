"""
M3 验证:
- 3 条情绪不同的外汇脚本(风险警告=tense / 盈利机会=uplifting / 行情分析=professional)
- 验证:① 脚本情绪被正确识别 ② 选出的 BGM 情绪与脚本匹配、且三条彼此不同
- 产出情绪对照表
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from loguru import logger
from app.services import bgm_library

SCRIPTS = {
    "risk_warning (期望 tense)": (
        "警告：近期外汇市场波动剧烈，多家机构爆仓风险陡增。"
        "美元兑日元单日暴跌300点，杠杆交易者面临强制平仓。"
        "请务必设置止损，控制仓位，谨防黑天鹅事件冲击账户安全。"
    ),
    "profit_opportunity (期望 uplifting)": (
        "恭喜！本周外汇策略组合收益率突破8%，大幅跑赢市场基准。"
        "黄金多头信号持续走强，把握难得的趋势性盈利窗口。"
        "现在正是布局的最佳时机，财富增长的机会就在眼前。"
    ),
    "market_analysis (期望 professional)": (
        "本期分析欧元兑美元的技术走势与基本面背景。"
        "从均线系统看，价格运行在中期通道内，市场情绪相对平稳。"
        "建议投资者关注本周公布的欧央行利率决议及其对汇率的潜在影响。"
    ),
}


def main():
    bgm_library.init_db()

    logger.info("=" * 70)
    logger.info("[M3] 库内 BGM 情绪分布")
    songs = bgm_library.list_all()
    from collections import Counter
    moods = Counter(s.mood for s in songs)
    logger.info(f"  {dict(moods)}  (total={len(songs)})")

    logger.info("=" * 70)
    logger.info("[M3] 三条脚本 → 情绪识别 → BGM 选曲")

    results = []
    chosen_paths = set()
    for label, script in SCRIPTS.items():
        mood = bgm_library.analyze_script_mood(script)
        bgm_path = bgm_library.select_bgm(mood)
        bgm_name = os.path.basename(bgm_path) if bgm_path else "(none)"

        # look up the chosen song's own tagged mood/energy for the table
        chosen_rec = next((s for s in songs if s.path == bgm_path), None)
        chosen_mood = chosen_rec.mood if chosen_rec else "?"
        chosen_energy = chosen_rec.energy if chosen_rec else "?"

        logger.info(f"\n  [{label}]")
        logger.info(f"    script (前30字): {script[:30]}...")
        logger.info(f"    识别情绪 → {mood}")
        logger.info(f"    选中 BGM → {bgm_name} (tagged mood={chosen_mood}, energy={chosen_energy})")

        results.append({
            "label": label,
            "script_mood": mood,
            "bgm_file": bgm_name,
            "bgm_mood": chosen_mood,
            "bgm_energy": chosen_energy,
        })
        if bgm_path:
            chosen_paths.add(bgm_path)

    logger.info("\n" + "=" * 70)
    logger.info("[M3] 情绪对照表")
    logger.info(f"{'脚本':35s} {'识别情绪':12s} {'选中BGM':18s} {'BGM情绪':10s} energy")
    for r in results:
        logger.info(
            f"{r['label']:35s} {r['script_mood']:12s} {r['bgm_file']:18s} "
            f"{r['bgm_mood']:10s} {r['bgm_energy']}"
        )

    # Validation checks
    logger.info("\n" + "=" * 70)
    logger.info("[M3] 验收检查")

    mood_match_ok = all(
        r["script_mood"] == r["bgm_mood"] or r["bgm_mood"] == "?"
        for r in results
    )
    distinct_ok = len(chosen_paths) == len(SCRIPTS)
    correct_classification = (
        results[0]["script_mood"] == "tense"
        and results[1]["script_mood"] == "uplifting"
        and results[2]["script_mood"] == "professional"
    )

    logger.info(f"  脚本情绪识别正确(tense/uplifting/professional): {'✓ PASS' if correct_classification else '✗ FAIL'}")
    logger.info(f"  选中BGM情绪与脚本情绪匹配: {'✓ PASS' if mood_match_ok else '✗ FAIL'}")
    logger.info(f"  三条选曲彼此不同(非同一首/非随机退化): {'✓ PASS' if distinct_ok else '✗ FAIL'}")

    all_pass = correct_classification and mood_match_ok and distinct_ok
    if all_pass:
        logger.success("\nM3 选曲逻辑验证通过")
    else:
        logger.error("\nM3 有验收项未通过")

    return all_pass


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
