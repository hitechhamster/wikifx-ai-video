# -*- coding: utf-8 -*-
"""
印度外汇热点 → AI 视频（本机版）

数据流:
  Apify 抓印度语境推文 → Pass1 AI 识别热点 → 取最热 2 个
    → AI 把两个热点拼成一条 ~40s 英文外汇新闻口播脚本
    → 写 templates/印度外汇热点/scripts.csv
    → batch.main() 进程内出片 → output/印度外汇热点_<时间戳>/<title>.mp4

用法:
  python india_forex_video.py            # 全流程,含出片(~10min 渲染)
  python india_forex_video.py --no-video # 只抓热点+写脚本,不出片(省钱省时,先看脚本)

说明:
  · fetch + Pass1 分析逻辑移植自 Colab 脚本 ai社媒发帖机器人(印度).py 的纯 Python 部分,
    去掉了 google.colab / gspread / Sheets 输出(那些只在 Colab 里能跑)。
  · 密钥优先读环境变量 APIFY_API_TOKEN / OPENROUTER_API_KEY,取不到回退到 Colab 里现有值。
"""
import os
import sys
import csv
import re
import json
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

# Windows 控制台默认 GBK,打印 emoji 会 UnicodeEncodeError → 强制 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

TEMPLATE_DIR = os.path.join(PROJECT_ROOT, "templates", "印度外汇热点")

# ── API 密钥 ───────────────────────────────────────────────────────────────
# 优先环境变量,其次本地未入库文件 keys_local.py(见 .gitignore)。
# 绝不硬编码明文进仓库。本地跑:设 APIFY_API_TOKEN / OPENROUTER_API_KEY 环境变量,
# 或建 keys_local.py(内容: APIFY_TOKEN="..." / OPENROUTER_KEY="...")。
try:
    import keys_local as _kl
except ImportError:
    _kl = None
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN") or getattr(_kl, "APIFY_TOKEN", "")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY") or getattr(_kl, "OPENROUTER_KEY", "")

# ── 印度关键词配置(lang:en + 印度锚点词强约束)──────────────────────────
KEYWORDS_CONFIG = [
    {
        "keyword": "forex_IN",
        "description": "印度语境外汇",
        "query": '(forex OR #forex OR #ForexIndia OR "forex trading") '
                 '(RBI OR SEBI OR INR OR Rupee OR India OR Indian OR NSE OR BSE OR LRS) '
                 'lang:en',
    },
    {
        "keyword": "trading_IN",
        "description": "印度语境交易",
        "query": '(trading OR #trading OR "intraday trading" OR "day trading") '
                 '(RBI OR SEBI OR INR OR Rupee OR Nifty OR Sensex OR NSE OR BSE OR India OR Indian) '
                 'lang:en',
    },
]

# ── 系统参数 ──────────────────────────────────────────────────────────────
SEARCH_ACTOR = "apidojo/tweet-scraper"
DAYS_BACK = 1
MAX_PER_KEYWORD = 80
MAX_TO_AI = 300
AI_MODEL = "anthropic/claude-sonnet-4.6"
AI_RETRIES = 2
AI_TIMEOUT = 180
MAX_FOLLOWERS = 50000

RUN_TS = datetime.now().strftime('%Y%m%d_%H%M')


# ══════════════════════════════════════════════════════════════════════════
# 轻量日志(替代 Colab 的 BugLogger,只保留分级打印)
# ══════════════════════════════════════════════════════════════════════════
class _Log:
    _ICONS = {"INFO": "ℹ️ ", "WARNING": "⚠️ ", "ERROR": "❌", "CRITICAL": "🔥"}

    def _log(self, level, module, message, detail=""):
        ts = datetime.now().strftime("%H:%M:%S")
        icon = self._ICONS.get(level, "📝")
        print(f"  {icon} [{ts}][{level}] {module} → {message}")
        if detail:
            print(f"      └─ {str(detail)[:300].replace(chr(10), ' ')}")

    def info(self, m, msg, detail=""):     self._log("INFO", m, msg, detail)
    def warning(self, m, msg, detail=""):  self._log("WARNING", m, msg, detail)
    def error(self, m, msg, detail=""):    self._log("ERROR", m, msg, detail)
    def critical(self, m, msg, detail=""): self._log("CRITICAL", m, msg, detail)


bug = _Log()


# ══════════════════════════════════════════════════════════════════════════
# MODULE 1 ── 按关键词搜索推文(移植自 Colab,纯 requests)
# ══════════════════════════════════════════════════════════════════════════

def get_time_coeff(created_at_str, now):
    FORMATS = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%a %b %d %H:%M:%S %z %Y",
    ]
    for fmt in FORMATS:
        try:
            t = datetime.strptime(created_at_str, fmt)
            t = (t.replace(tzinfo=timezone.utc) if t.tzinfo is None
                 else t.astimezone(timezone.utc))
            h = (now - t).total_seconds() / 3600
            if h <= 12: return 4, round(h, 1)
            if h <= 24: return 2, round(h, 1)
            return None, round(h, 1)
        except ValueError:
            continue
    bug.warning("Fetch", "无法解析时间格式(已丢弃)", repr(created_at_str))
    return None, -1


def _detect_has_video(item):
    media_sources = [
        item.get("media", []),
        (item.get("entities", {}) or {}).get("media", []),
        (item.get("extendedEntities", {}) or {}).get("media", []),
    ]
    for media_list in media_sources:
        if not media_list:
            continue
        for m in media_list:
            if isinstance(m, dict):
                media_type = (m.get("type", "") or "").lower()
                if media_type in ("video", "animated_gif"):
                    return True
    if item.get("isVideo", False):
        return True
    return False


# ★ 第二层印度语境过滤:关键词匹配(轻量,省 AI 调用)
INDIAN_ANCHORS = [
    "rbi", "sebi", "bappebti",
    "inr", "rupee", "rupees", "₹",
    "nifty", "sensex", "nse", "bse", "banknifty", "finnifty",
    "lrs", "liberalised remittance",
    "india", "indian", "mumbai", "delhi", "bengaluru", "bangalore",
    "chennai", "kolkata", "hyderabad", "pune",
    "nri", "dalal street",
    "bhai", "yaar", "paisa", "crore", "lakh",
]


def _quick_indian_check(text):
    if not text:
        return False
    lower = text.lower()
    for anchor in INDIAN_ANCHORS:
        if anchor in lower:
            return True
    return False


def fetch_tweets_by_keywords(keywords_config):
    now = datetime.now(timezone.utc)
    today = datetime.now()
    since_str = (today - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    until_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    all_tweets = []
    seen_urls = set()
    skipped_old = skipped_parse = skipped_dup = skipped_bigv = skipped_global = 0

    api_url = (
        f"https://api.apify.com/v2/acts/"
        f"{SEARCH_ACTOR.replace('/', '~')}/run-sync-get-dataset-items"
    )

    print(f"\n{'='*58}")
    print(f"📡 🇮🇳 印度语境关键词搜索(粉丝<{MAX_FOLLOWERS}过滤)")
    print(f"   🔑 关键词: {[kw['keyword'] for kw in keywords_config]}")
    print(f"   📅 范围: {since_str} ~ {until_str}")
    print(f"   🇮🇳 二层过滤: 推文必须含印度锚点词(RBI/INR/Nifty/India等)")
    print(f"{'='*58}")

    for kw_idx, kw_config in enumerate(keywords_config, 1):
        keyword = kw_config["keyword"]
        query = f"{kw_config['query']} since:{since_str} until:{until_str}"

        print(f"\n🔍 [{kw_idx}/{len(keywords_config)}] 搜索: {keyword}")
        print(f"   查询: {query}")

        try:
            resp = requests.post(
                api_url,
                params={"token": APIFY_TOKEN},
                json={
                    "searchTerms": [query],
                    "sort": "Top",
                    "maxItems": MAX_PER_KEYWORD,
                    "includeSearchTerms": True,
                },
                timeout=300,
            )
            resp.raise_for_status()
            items = resp.json()
            if not isinstance(items, list):
                raise ValueError(f"Apify 返回非列表: {str(items)[:200]}")

            kept = kw_old = kw_parse = kw_dup = kw_bigv = kw_global = 0

            for item in items:
                url = item.get("url", "")
                if url in seen_urls:
                    kw_dup += 1
                    continue
                if url:
                    seen_urls.add(url)

                created_at = item.get("createdAt", "")
                coeff, hours_ago = get_time_coeff(created_at, now)
                if coeff is None:
                    if hours_ago == -1: kw_parse += 1
                    else:               kw_old += 1
                    continue

                author = item.get("author", {})
                followers = author.get("followers", 0) or 0
                if followers >= MAX_FOLLOWERS:
                    kw_bigv += 1
                    continue

                tweet_text = item.get("text", "")
                if not _quick_indian_check(tweet_text):
                    kw_global += 1
                    continue

                likes = item.get("likeCount", 0) or 0
                rts = item.get("retweetCount", 0) or 0
                replies = item.get("replyCount", 0) or 0
                raw_sc = likes + rts * 2 + replies * 2
                engage = raw_sc * coeff
                has_video = _detect_has_video(item)

                all_tweets.append({
                    "tweet_id": 0,
                    "keyword": keyword,
                    "username": f"@{author.get('userName', '')}",
                    "display_name": author.get("name", ""),
                    "followers": followers,
                    "text": tweet_text.replace("\n", " "),
                    "created_at": created_at,
                    "hours_ago": hours_ago,
                    "likes": likes,
                    "retweets": rts,
                    "replies": replies,
                    "time_coeff": coeff,
                    "raw_score": raw_sc,
                    "engagement": engage,
                    "url": url,
                    "has_video": has_video,
                })
                kept += 1

            skipped_old += kw_old
            skipped_parse += kw_parse
            skipped_dup += kw_dup
            skipped_bigv += kw_bigv
            skipped_global += kw_global

            print(f"   ✅ 获取 {len(items)} 条 → 保留 {kept} 条")
            print(f"      (重复:{kw_dup} 超24h:{kw_old} 大V:{kw_bigv} "
                  f"非印度:{kw_global} 格式异常:{kw_parse})")

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:300] if e.response is not None else ""
            bug.error("Fetch", f"关键词'{keyword}'HTTP错误 {status}", body)
            print(f"   ❌ 搜索失败 (HTTP {status}): {body}")
        except Exception as e:
            bug.error("Fetch", f"关键词'{keyword}'搜索失败", traceback.format_exc())
            print(f"   ❌ 搜索失败: {e}")

        time.sleep(3)

    all_tweets.sort(key=lambda x: x["engagement"], reverse=True)
    for idx, t in enumerate(all_tweets, 1):
        t["tweet_id"] = idx

    c4 = sum(1 for t in all_tweets if t["time_coeff"] == 4)
    c2 = sum(1 for t in all_tweets if t["time_coeff"] == 2)

    print(f"\n{'='*58}")
    print(f"📊 最终保留: {len(all_tweets)} 条(已按互动量排序)")
    print(f"   ⏱  0~12h(×4): {c4} | 12~24h(×2): {c2}")
    print(f"   🗑  大V:{skipped_bigv} 非印度:{skipped_global} 超24h:{skipped_old} "
          f"重复:{skipped_dup} 格式:{skipped_parse}")
    print(f"{'='*58}")

    if all_tweets:
        print(f"\n🏆 互动量 Top 3:")
        for t in all_tweets[:3]:
            print(f"   #{t['tweet_id']} {t['username']} 🔥{t['engagement']} "
                  f"| {t['text'][:60]}...")

    return all_tweets


# ══════════════════════════════════════════════════════════════════════════
# MODULE 2 ── AI 调用 + JSON 解析(移植自 Colab)
# ══════════════════════════════════════════════════════════════════════════

def _call_ai(messages, context="", expect_json=True):
    total_chars = sum(len(m.get("content", "")) for m in messages)
    bug.info("AI", f"开始调用 [{context}]", f"请求字符数:{total_chars}")

    for attempt in range(AI_RETRIES + 1):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://wikifx.local",
                    "X-Title": "IndiaForexVideo",
                },
                json={
                    "model": AI_MODEL,
                    "messages": messages,
                    "temperature": 0.65,
                },
                timeout=AI_TIMEOUT,
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                bug.info("AI", f"调用成功 [{context}]", f"回复长度:{len(content)}字符")
                return content
            else:
                err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                bug.error("AI", f"HTTP错误 [{context}] 第{attempt+1}次", err)
                if attempt < AI_RETRIES:
                    wait = 10 * (attempt + 1)
                    print(f"   ⏳ {wait}s 后重试...")
                    time.sleep(wait)
                else:
                    raise Exception(f"OpenRouter {resp.status_code}: {resp.text[:400]}")
        except requests.Timeout:
            bug.error("AI", f"超时 [{context}] 第{attempt+1}次", f"超过{AI_TIMEOUT}s")
            if attempt < AI_RETRIES:
                time.sleep(15)
            else:
                raise
        except Exception as e:
            if "OpenRouter" in str(e):
                raise
            bug.error("AI", f"未知错误 [{context}] 第{attempt+1}次", traceback.format_exc())
            if attempt < AI_RETRIES:
                time.sleep(10)
            else:
                raise


def _repair_json(text):
    result = []
    in_string = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_string and c == '\\' and i + 1 < n:
            result.append(c)
            result.append(text[i + 1])
            i += 2
            continue
        if c == '"':
            if not in_string:
                in_string = True
                result.append(c)
            else:
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                next_char = text[j] if j < n else ''
                if next_char in (':', ',', '}', ']', ''):
                    in_string = False
                    result.append(c)
                else:
                    result.append('\\"')
            i += 1
            continue
        result.append(c)
        i += 1
    return ''.join(result)


def _extract_json(text, context=""):
    if not text:
        bug.error("ParseJSON", "输入为空", context)
        return None

    cleaned = text.strip().lstrip('﻿')
    cleaned = re.sub(r'[​‌‍⁠﻿]', '', cleaned)
    cleaned = cleaned.replace('｀', '`')

    candidates = []
    for m in re.finditer(r'```json\s*([\s\S]*?)\s*```', cleaned):
        candidates.append(("方式1", m.group(1).strip()))
    for m in re.finditer(r'```\s*([\s\S]*?)\s*```', cleaned):
        candidates.append(("方式2", m.group(1).strip()))
    m = re.search(r'\{[\s\S]*\}', cleaned)
    if m:
        candidates.append(("方式3", m.group(0).strip()))
    candidates.append(("方式4", cleaned))

    for method, candidate in candidates:
        if not candidate:
            continue
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
        try:
            result = json.loads(_repair_json(candidate))
            if isinstance(result, dict):
                bug.warning("ParseJSON", f"修复后解析成功 [{method}]")
                return result
        except json.JSONDecodeError:
            continue

    bug.critical("ParseJSON", f"所有方式均失败 [{context}]", repr(cleaned[:300]))
    return None


# ══════════════════════════════════════════════════════════════════════════
# Pass 1: 印度热点分析(移植自 Colab,精简 prompt 保留印度语境知识)
# ══════════════════════════════════════════════════════════════════════════

def analyze_hot_topics(tweets):
    top = sorted(tweets, key=lambda x: x["engagement"], reverse=True)[:MAX_TO_AI]
    top.sort(key=lambda x: x["tweet_id"])

    total_likes = sum(t["likes"] for t in top)
    total_rts = sum(t["retweets"] for t in top)
    total_replies = sum(t["replies"] for t in top)
    avg_engage = round(sum(t["engagement"] for t in top) / max(len(top), 1), 1)

    tweet_block = "\n".join(
        f"[{t['tweet_id']}] {t['username']} [KW:{t['keyword']}] "
        f"👍{t['likes']} 🔁{t['retweets']} 💬{t['replies']} ⏱{t['hours_ago']}h "
        f"| {t['text'][:220]}"
        for t in top
    )

    today = datetime.now().strftime('%Y-%m-%d')

    prompt = f"""You are a senior financial analyst specializing in the Indian retail forex/trading market. Today is {today}.

⚠️ CRITICAL JSON RULE: Inside any JSON string value, NEVER use ASCII double quotes " for
quotation or emphasis. Use 「」for Chinese quoted terms, or single quotes ' for English.

Below are {len(top)} tweets from Indian Twitter/X (lang:en + Indian context keywords),
past 24 hours, sorted by engagement, from accounts with < {MAX_FOLLOWERS} followers.

🇮🇳 INDIAN MARKET CONTEXT:
- RBI (central bank, controls forex via LRS), SEBI (capital markets), FEMA (forex law).
- Retail forex with foreign MT4/MT5 brokers is ILLEGAL under FEMA; RBI keeps an Alert List.
- Common scams: Telegram signal groups, WhatsApp "uncle" schemes, fake broker sites,
  pig-butchering via dating apps.
- Key markets: USD/INR, Nifty 50, Sensex, BankNifty, retail F&O boom.

⚠️ HEAT RANKING: base heat_level (1-5) primarily on REAL ENGAGEMENT DATA. Provide
"total_engagement" for verification.

Identify 4–6 distinct hot topics in the Indian forex/trading scene. Return ONLY valid JSON:

{{
  "analysis_date": "{today}",
  "hot_topics": [
    {{
      "id": 1,
      "topic_name": "Concise topic title in English",
      "heat_level": 5,
      "total_engagement": 12345,
      "category": "Forex | Options | Stocks | Regulation | Scam | Remittance | Macro | Other",
      "summary_zh": "2-3句中文摘要,引用词用「」而非双引号",
      "summary_en": "2-3 sentence English summary for Indian audience, single quotes for emphasis",
      "supporting_tweet_ids": [1, 5, 12],
      "key_quotes": [
        {{"tweet_id": 1, "author": "@handle", "quote": "exact short quote ≤80 chars"}}
      ]
    }}
  ],
  "market_sentiment": {{"overall": "Bullish|Bearish|Neutral|Mixed", "score": 3, "reasoning_zh": "1-2句中文"}}
}}

TWEETS:
{tweet_block}"""

    print(f"\n🤖 Pass 1 — 印度热点识别(送入 {len(top)} 条推文)...")
    print(f"   📊 互动汇总: 👍{total_likes} 🔁{total_rts} 💬{total_replies} | 平均分:{avg_engage}")

    try:
        raw = _call_ai([{"role": "user", "content": prompt}], context="Pass1-印度热点")
        data = _extract_json(raw, context="Pass1")
        if data and data.get("hot_topics"):
            print(f"   ✅ 识别到 {len(data['hot_topics'])} 个热点")
        else:
            bug.critical("Analysis", "Pass1 JSON解析失败或hot_topics为空")
            data = {"hot_topics": [], "market_sentiment": {}}
    except Exception as e:
        bug.critical("Analysis", "Pass1 API调用失败", traceback.format_exc())
        print(f"   ❌ API调用失败: {e}")
        data = {"hot_topics": [], "market_sentiment": {}}

    return data


# ══════════════════════════════════════════════════════════════════════════
# 桥接 ── 取 Top2 热点 → AI 拼一条英文新闻口播脚本
# ══════════════════════════════════════════════════════════════════════════

def pick_top_two(analysis_data):
    """按 (heat_level, total_engagement) 降序取前 2 个热点。"""
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

    return sorted(topics, key=_rank, reverse=True)[:2]


def write_combined_news_script(top_topics):
    """把 Top 热点拼成一条 ~40s、可口播的英文外汇新闻脚本(纯文本,非 JSON)。"""
    blocks = []
    for i, tp in enumerate(top_topics, 1):
        quotes = "; ".join(q.get("quote", "") for q in tp.get("key_quotes", []) if q.get("quote"))
        blocks.append(
            f"TOPIC {i}: {tp.get('topic_name', '')}\n"
            f"  category: {tp.get('category', '')}\n"
            f"  summary: {tp.get('summary_en', '')}\n"
            f"  chatter: {quotes}"
        )
    topics_text = "\n\n".join(blocks)

    prompt = f"""You are a broadcast news scriptwriter for a short vertical WIKIFX forex-news video
aimed at an INDIAN retail-trading audience. Write ONE spoken voice-over script that covers
the two hot topics below as a single coherent ~40 second news segment.

RULES:
- Indian English, clear broadcast/anchor tone. NOT savage, NOT meme-y.
- 100–130 words total (this is read aloud, sped up slightly).
- Short, punchy sentences (one idea per sentence) — the video shows one clip per sentence.
- Open with a hook, cover topic 1, transition, cover topic 2, close with a WIKIFX-style
  caution line (e.g. verify brokers, check the RBI Alert List).
- Reference real Indian context where natural (RBI, SEBI, LRS, Nifty, Sensex, INR, Dalal Street).
- DO NOT invent specific prices, percentages, or index levels — talk about the discussion/trend,
  not fabricated numbers.
- Output ONLY the narration text. No headings, no bullet points, no stage directions, no quotes.

HOT TOPICS:
{topics_text}"""

    print(f"\n✍️  拼接脚本 — 把 Top{len(top_topics)} 热点写成一条英文新闻口播...")
    raw = _call_ai([{"role": "user", "content": prompt}],
                   context="Script-合并新闻脚本", expect_json=False)
    script = (raw or "").strip()
    # 去掉模型偶尔包的引号/代码块
    script = re.sub(r'^```[a-z]*\s*|\s*```$', '', script).strip().strip('"').strip()
    return script


# ══════════════════════════════════════════════════════════════════════════
# 出片 ── 写 scripts.csv → 进程内调用 batch.main()
# ══════════════════════════════════════════════════════════════════════════

def run_video(script_text, title, badge="INDIA FOREX NEWS"):
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    csv_path = os.path.join(TEMPLATE_DIR, "scripts.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["script", "title", "badge"])
        w.writeheader()
        w.writerow({"script": script_text, "title": title, "badge": badge})
    print(f"\n📝 已写入脚本 → {csv_path}")

    # 进程内复用 batch 的完整出片管线(新闻包装 + 片头片尾 + BGM + results.csv)
    import batch
    batch.main(TEMPLATE_DIR)


# ══════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════

def main(make_video=True):
    print(f"\n{'='*60}")
    print(f"🇮🇳 印度外汇热点 → AI 视频")
    print(f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"🤖 模型: {AI_MODEL} | 出片: {'是' if make_video else '否(--no-video)'}")
    print(f"{'='*60}")

    # Step 1: 抓推文
    tweets = fetch_tweets_by_keywords(KEYWORDS_CONFIG)
    if not tweets:
        print("\n❌ 未搜到推文,可能原因:")
        print("   · 印度锚点词过滤过严 / 过去24h无匹配印度推文")
        print("   · Apify Token 失效或余额不足(看上方 HTTP 错误码)")
        print(f"   · 粉丝过滤(≥{MAX_FOLLOWERS})导致全部被排除")
        raise SystemExit("无推文数据")

    # Step 2: AI 分析热点 → 取 Top2
    analysis = analyze_hot_topics(tweets)
    top2 = pick_top_two(analysis)
    if not top2:
        raise SystemExit("Pass1 未产出任何热点,无法拼脚本(见上方日志)")

    print(f"\n{'='*60}")
    print(f"🔥 选中最热 {len(top2)} 个热点:")
    print(f"{'='*60}")
    for i, tp in enumerate(top2, 1):
        heat = "⭐" * min(int(tp.get("heat_level", 1) or 1), 5)
        print(f"\n  [{i}] {heat} [{tp.get('category')}] {tp.get('topic_name')}")
        print(f"      📊 总互动: {tp.get('total_engagement', '?')}")
        print(f"      📌 中文: {tp.get('summary_zh', '')}")
        print(f"      🇮🇳 EN:  {tp.get('summary_en', '')}")

    # Step 3: 拼英文新闻脚本
    script = write_combined_news_script(top2)
    print(f"\n{'='*60}")
    print(f"📰 生成的英文新闻脚本:")
    print(f"{'='*60}")
    print(script)
    print(f"{'='*60}")
    print(f"   (约 {len(script.split())} 词)")

    if not make_video:
        print(f"\n⏸  --no-video: 到此为止,未出片。确认脚本 OK 后去掉该参数即可渲染。")
        return

    # Step 4: 出片
    title = f"India_Forex_{RUN_TS}"
    badge = "INDIA FOREX NEWS"
    run_video(script, title, badge)

    out_root = os.path.join(PROJECT_ROOT, "output")
    print(f"\n✅ 出片完成。产物在 {out_root}\\印度外汇热点_* 下(见上方 batch 汇总)。")


if __name__ == "__main__":
    no_video = "--no-video" in sys.argv[1:]
    try:
        main(make_video=not no_video)
    except SystemExit as e:
        if e.code and e.code != 0:
            print(f"\n🛑 {e}")
