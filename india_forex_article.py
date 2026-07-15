# -*- coding: utf-8 -*-
"""
印度外汇热点 → 编辑感文章 + 吸睛标题（本机版）

数据流:
  Apify 抓印度语境推文 → Pass1 AI 识别热点 → 取最热 N 个(默认3)
    → 每个热点写一篇英文编辑感文章 + 1 主标题 + 3 备选吸睛标题
    → 落地 output/articles_<时间戳>/NN_<slug>.md

用法:
  python india_forex_article.py             # 抓热点 + 写文章(默认Top3),落盘
  python india_forex_article.py --topics 5  # 取最热5个
  python india_forex_article.py --no-write  # 只打印不落盘(快速看效果)

复用: 抓取 + 热点分析 + AI 调用 + JSON 解析全部复用 india_forex_video.py
(其 main() 在 __main__ 保护下,import 不会触发抓取)。
"""
import os
import re
import sys
import json
import traceback
from datetime import datetime, timezone

import india_forex_video as ifv

# Windows 控制台 GBK,打印 emoji 会报错 → 强制 UTF-8(保险,ifv 也做过一次)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = ifv.PROJECT_ROOT
RUN_TS = datetime.now().strftime('%Y%m%d_%H%M')


# ══════════════════════════════════════════════════════════════════════════
# 取最热 N 个热点
# ══════════════════════════════════════════════════════════════════════════
def pick_top(analysis_data, n=3):
    topics = analysis_data.get("hot_topics", []) or []

    def _rank(tp):
        try:
            heat = int(tp.get("heat_level", 0) or 0)
        except (TypeError, ValueError):
            heat = 0
        try:
            eng = float(tp.get("total_engagement", 0) or 0)
        except (TypeError, ValueError):
            eng = 0.0
        return (heat, eng)

    return sorted(topics, key=_rank, reverse=True)[:n]


# ══════════════════════════════════════════════════════════════════════════
# 把单个热点写成一篇编辑感文章 + 吸睛标题
# ══════════════════════════════════════════════════════════════════════════
def write_article(topic):
    quotes = "\n".join(
        f"  - {q.get('author','')}: \"{q.get('quote','')}\""
        for q in topic.get("key_quotes", []) if q.get("quote")
    ) or "  (none)"

    prompt = f"""You are a senior editor at an Indian financial news site writing for retail
traders and investors. Turn the hot topic below into ONE publish-ready article.

⚠️ CRITICAL JSON RULE: inside any JSON string value, NEVER use ASCII double quotes " for
quotation or emphasis — use single quotes ' instead. Any unescaped " breaks parsing.

EDITORIAL STYLE (this must read like a real edited news feature, NOT a dry summary or a
bullet list):
- Open with a sharp HOOK lede (1-2 sentences that pull the reader in).
- Follow with a nut graf: one short paragraph on why this matters to Indian traders right now.
- Use 2-3 section subheadings (markdown '##') to structure the body.
- Short, punchy paragraphs with rhythm. Vary sentence length.
- Include exactly ONE pull-quote line (markdown '>' blockquote).
- End with a 'What to watch' / reader-takeaway close from a forex-safety angle, and a soft
  nudge to verify brokers / check regulation on WikiFX (keep it natural, one line, not spammy).

CATCHY HEADLINE (吸睛):
- Give 1 primary headline + 3 alternatives in DIFFERENT styles: curiosity, number-driven,
  and warning/urgency. Attention-grabbing but MUST NOT mislead or invent facts.

FACTUAL DISCIPLINE:
- Use ONLY the data present in the summary/quotes below. Do NOT invent specific prices,
  percentages, index levels, names, or figures that are not given.
- 600-900 words of body.

HOT TOPIC:
  topic_name: {topic.get('topic_name','')}
  category: {topic.get('category','')}
  summary: {topic.get('summary_en','')}
  chatter quotes:
{quotes}

Return ONLY valid JSON:
{{
  "headline": "primary catchy headline",
  "alt_headlines": ["curiosity-style", "number-style", "warning-style"],
  "dek": "one-sentence standfirst/subtitle under the headline",
  "body_markdown": "full article body in markdown with ## subheadings and one > pull-quote",
  "tags": ["#ForexIndia", "..."]
}}"""

    print(f"\n✍️  写文章: {topic.get('topic_name','')[:60]} ...")
    try:
        raw = ifv._call_ai([{"role": "user", "content": prompt}],
                           context=f"Article-{topic.get('id','?')}")
        data = ifv._extract_json(raw, context="Article")
        if data and data.get("body_markdown") and data.get("headline"):
            return data
        # 降级:解析失败但有原始文本 → 整段当 body,标题回退 topic_name
        ifv.bug.warning("Article", "JSON解析失败,降级用原始文本当正文")
        return {
            "headline": topic.get("topic_name", "Untitled"),
            "alt_headlines": [],
            "dek": topic.get("summary_en", "")[:160],
            "body_markdown": (raw or "").strip() or "(empty)",
            "tags": [],
        }
    except Exception as e:
        ifv.bug.error("Article", "写文章失败", traceback.format_exc())
        print(f"   ❌ 失败: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
# 落地 markdown
# ══════════════════════════════════════════════════════════════════════════
def _slug(text, maxlen=50):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:maxlen].strip("-")) or "article"


def save_article(art, topic, out_dir, idx):
    slug = _slug(art.get("headline", ""))
    fname = f"{idx:02d}_{slug}.md"
    path = os.path.join(out_dir, fname)

    alts = art.get("alt_headlines", []) or []
    alt_block = ""
    if alts:
        alt_block = "> **备选标题：**\n" + "".join(
            f"> {i}. {a}\n" for i, a in enumerate(alts, 1)
        ) + "\n"

    tags = art.get("tags", []) or []
    heat = int(topic.get("heat_level", 0) or 0)
    md = (
        f"# {art.get('headline','')}\n\n"
        f"*{art.get('dek','')}*\n\n"
        f"{alt_block}"
        f"{art.get('body_markdown','').strip()}\n\n"
        f"---\n"
        f"Tags: {' '.join(tags)} · 热度 {'⭐'*min(heat,5)} · "
        f"互动 {topic.get('total_engagement','?')} · 分类 {topic.get('category','')}\n"
        f"来源热点: {topic.get('topic_name','')}\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path


# ══════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════
def main(n_topics=3, write=True):
    print(f"\n{'='*60}")
    print(f"🇮🇳 印度外汇热点 → 编辑感文章 + 吸睛标题")
    print(f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"🤖 模型: {ifv.AI_MODEL} | Top {n_topics} | 落盘: {'是' if write else '否(--no-write)'}")
    print(f"{'='*60}")

    # Step 1: 抓推文
    tweets = ifv.fetch_tweets_by_keywords(ifv.KEYWORDS_CONFIG)
    if not tweets:
        print("\n❌ 未搜到推文(Apify Token/锚点词过滤/时间范围,详见上方日志)")
        raise SystemExit("无推文数据")

    # Step 2: 热点分析 → 取 Top N
    analysis = ifv.analyze_hot_topics(tweets)
    tops = pick_top(analysis, n_topics)
    if not tops:
        raise SystemExit("Pass1 未产出任何热点")

    print(f"\n🔥 选中最热 {len(tops)} 个热点:")
    for i, tp in enumerate(tops, 1):
        heat = "⭐" * min(int(tp.get("heat_level", 1) or 1), 5)
        print(f"  [{i}] {heat} [{tp.get('category')}] {tp.get('topic_name')}")

    out_dir = ""
    if write:
        out_dir = os.path.join(PROJECT_ROOT, "output", f"articles_{RUN_TS}")
        os.makedirs(out_dir, exist_ok=True)

    # Step 3: 逐个写文章
    saved = []
    for i, tp in enumerate(tops, 1):
        art = write_article(tp)
        if not art:
            continue
        print(f"\n{'─'*60}")
        print(f"📰 [{i}] {art.get('headline','')}")
        for j, a in enumerate(art.get("alt_headlines", []) or [], 1):
            print(f"     备选{j}: {a}")
        if art.get("dek"):
            print(f"     导语: {art['dek']}")
        preview = (art.get("body_markdown", "") or "").strip().replace("\n", " ")
        print(f"     正文预览: {preview[:220]}...")
        if write:
            path = save_article(art, tp, out_dir, i)
            saved.append((art.get("headline", ""), path))
            print(f"     💾 {path}")

    # 汇总
    print(f"\n{'='*60}")
    if write and saved:
        print(f"✅ 已写 {len(saved)} 篇 → {out_dir}")
        for title, path in saved:
            print(f"   · {title}  ({os.path.basename(path)})")
    elif not write:
        print(f"⏸  --no-write: 未落盘")
    else:
        print("⚠️ 没有成功产出的文章")
    print(f"{'='*60}")


if __name__ == "__main__":
    args = sys.argv[1:]
    no_write = "--no-write" in args
    n = 3
    if "--topics" in args:
        try:
            n = int(args[args.index("--topics") + 1])
        except (IndexError, ValueError):
            print("⚠️ --topics 需要一个数字,回退默认 3")
    try:
        main(n_topics=n, write=not no_write)
    except SystemExit as e:
        if e.code and e.code != 0:
            print(f"\n🛑 {e}")
