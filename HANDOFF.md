# 交接文档 — 外汇 AI 批量出片引擎

面向:接手同事。目标是让你能**装好环境 → 跑出一条视频 → 会改/新建模板**。
架构与历史决策看 `CLAUDE.md`(很长,是研发日志);本文件是「上手手册」。

---

## 1. 这个项目是干什么的

输入一段文案(脚本)→ 自动:AI 理解分句 → 每句配一段贴题的**真实**视频素材(Pexels/Pixabay
在线库,AI 判定挡掉动画/跑题/加密货币)→ Gemini 配音 + 字幕 → 情绪 BGM → 新闻角标/滚动条 →
拼上片头片尾 → 输出竖屏 9:16 成片。**一个「模板文件夹」= 一种节目形态**(外汇新闻/诈骗预警/
黄金促销/炫富混剪…),批量出片只需换 Excel 里的脚本。

领域是外汇/财经,竖屏短视频(TikTok/Reels/Shorts)。

---

## 2. 环境搭建(全新机器)

### 2.1 必需
- **Python 3.13**(3.13.5 实测可用;不要用 3.11/3.12 以外乱切)
- **ffmpeg**(建议 WinGet 装 Gyan 的 full build,支持 NVENC 硬件编码)
- 装依赖:`pip install -r requirements.txt`

### 2.2 config.toml —— 换机器/换人**必须改**的东西
配置在 `config.toml`(项目根)。以下几项是**机器相关或密钥相关,必改**:

| 键 | 说明 |
|---|---|
| `ffmpeg_path` | **绝对路径**,当前指向我这台机器的 WinGet ffmpeg。同事必须改成自己机器的 ffmpeg.exe 路径,否则 NVENC 不生效/报错。 |
| `video_codec` | `h264_nvenc`(NVIDIA 硬件编码,需 **驱动 570+**)。**没 N 卡或驱动旧就改成 `libx264`**(纯 CPU,慢但通用)。 |
| `gemini_api_key` | Gemini,用于 **配音(TTS)+ 素材真实/相关性判定 + embedding**。核心,必填。 |
| `deepseek_api_key` | DeepSeek,用于 **生成每句的画面搜索词 + 脚本情绪分类**。核心,必填。 |
| `pexels_api_keys` | Pexels 视频库(数组,可多 key 轮询)。素材主来源,必填。 |
| `pixabay_api_keys` | Pixabay 视频+图片库(扩池、图片兜底)。强烈建议填。 |
| `openai_api_key` | 可选,仅当想用 OpenAI TTS(voice_name 前缀 `openai:`)。不填则用 Gemini TTS。 |

> ⚠️ **密钥安全**:config.toml 里现在有真实密钥。交接时要么让同事换成自己的 key,要么确认这份
> key 可共用。别把 config.toml 提交到公开仓库。

### 2.3 冒烟测试(确认环境通)
```bash
python -c "import moviepy, google.genai, openpyxl, numpy; print('deps OK')"
# 跑一条最短的验证(炫富模板 7 秒,不依赖 news 管线):
python batch.py templates/炫富生活
```
输出在 `output/炫富生活_<时间戳>/`。能出一个 mp4 就说明全链路通了。

---

## 3. 日常出片:批量流程(主用法)

**一条命令跑一个模板:**
```bash
python batch.py templates/诈骗预警
```
它会读 `templates/诈骗预警/scripts.xlsx` 里的每一行,逐条生成,输出到
`output/诈骗预警_<时间戳>/<title>.mp4`,并写 `results.csv`(成功/失败/耗时汇总)。

- **一行 = 一条视频**。想批量就在 Excel 里多写几行。
- 某条失败不影响其它条。
- 单条耗时约 **10~20 分钟**(取决于句数、素材好找程度、是否并行)。慢是正常的,大头在
  AI 素材判定(每个候选一次 Gemini 调用)+ MoviePy 多层叠加逐帧渲染。

### 并行跑多个模板
可以同时开多个 `python batch.py templates/X`(不同模板)。已做并行隔离(每个模板的暂存 BGM
按模板名区分,互不误删)。但 2~3 个并行会抢 CPU/GPU,单条更慢。

---

## 4. Excel(scripts.xlsx)列说明

| 列 | 必填 | 说明 |
|---|---|---|
| `title` | 建议 | 输出文件名(不含扩展名) |
| `script` | **是**(新闻类) | 完整口播文案。AI 按句子(。！？.!?)分句,**每句配一个画面**。 |
| `subject` | 否 | 该行的素材搜索主题,覆盖模板默认。不填用模板 config 的 `video_subject`。 |
| `badge` | 否 | 左上角角标 + 片头文字,逐行覆盖(如 `FRAUD ALERT` / `BERITA BROKER` / `ANALISIS FOREX`)。 |
| `language` | 否 | `en` / `id` / `zh` …,覆盖模板默认。影响配音语言。 |
| `hooks` | 混剪模板用 | 炫富混剪没有 script,用 hooks(大字金句,`|` 分隔多条)。 |

写脚本注意:小数(如 `1.15`、`15.31`)后面不要紧跟空格会被误当句号——已修过,`15.31 crore` 没问题;
emoji / 「Scene 1」这种标注**不要写进 script**(会被念出来),只留纯口播文字。

---

## 5. 五个现成模板

| 文件夹 | 形态 | 语言 | 角标 | 特点 |
|---|---|---|---|---|
| `templates/外汇新闻` | 外汇突发新闻 | en | FOREX MARKET NEWS | 快切紧张(1.3x),news 包装,图片片头+片尾 |
| `templates/外汇新闻印尼` | 印尼语新闻/分析 | id | BERITA FOREX(可逐行覆盖) | 不加速,全视频 |
| `templates/诈骗预警` | 诈骗预警/科普 | en | FRAUD ALERT | 快切紧张,全视频 |
| `templates/wikigold` | 黄金促销 | en | (无 news) | 视频头尾从品牌视频截段 + 强制1张黄金图 + BGM音纹截最紧张段 |
| `templates/炫富生活` | 纯音乐炫富混剪 | en | (无) | montage 模式,7秒,大字金句,狠转场,开头画外音 |

**新建模板 = 复制一个现成文件夹 → 改 `config.toml` → 换 `bgm.mp3` → 换 `scripts.xlsx`。不用碰代码。**
config.toml 里每个键都有中文注释,照着改即可(语速、快切、角标、片头片尾、BGM 等)。

---

## 6. 代码结构(要改逻辑时看)

**入口脚本(项目根):**
- `batch.py` — **主入口**。读模板 config+Excel → 逐条调流水线 → 加片头尾 → 汇总。
- `montage.py` — 炫富混剪的实现(纯音乐快切 + 大字金句 + 开头画外音)。
- `make_intro_outro.py` — 片头片尾(生成标题卡 / 图片擦入片头 / 从品牌视频截时间段 / 拼接)。
- `bgm_tense.py` — 音纹分析:在一首 BGM 里找最紧张的一段截出来(wikigold 用)。

**核心引擎(`app/services/`):**
- `orchestrator.py` — **编排核心**:分句 → 每句生成画面搜索词 → 取素材(本地→在线→图片兜底)→
  一句一镜对齐。素材不够/跑题的处理都在这。
- `providers.py` — 素材源:`PexelsProvider`(在线视频,多源聚合)、`ImageProvider`(Pixabay 图片
  +Ken Burns 运镜)、`LocalProvider`(本地库)。
- `tagging.py` — **素材判定**:Gemini 看一帧,判「真实拍摄 + 贴题(+可选严格/加密拒绝)」。
  改判定标准要 **bump `_CLASSIFY_LOGIC_VERSION`**(否则旧缓存结论会赖着)。
- `video.py` — 切片/拼接/转场/字幕/BGM 混音/编码(复用自参考项目,别重写底层)。
- `voice.py` — TTS 配音(Gemini/OpenAI)。 `bgm_library.py` — 情绪 BGM 选曲。
- `material.py` — 图库搜索/下载/Ken Burns 渲染 底层函数。
- `models/schema.py` — `VideoParams` 所有参数字段(config 的键基本对应这里)。

数据流:`batch.build_params()` → `VideoParams` → `tm.start()` → orchestrator 取材 → video 合成 →
`final-1.mp4` → make_intro_outro 加头尾 → `output/`。

---

## 7. 关键素材资产(别删)

- `resource/片头.png` — 外汇新闻图片片头底图
- `resource/片尾.mp4` — WikiFX App 下载片尾(新闻/诈骗模板用)
- `resource/wikigold-EN-投放尾板.mp4` — wikigold 的片头尾来源视频
- `resource/songs/` — BGM 库(49 首,含 `output042.mp3`)。各模板自己的 `bgm.mp3` 在模板文件夹里。
- `resource/fonts/` — 字幕/标题字体(微软雅黑粗体、STHeiti 等)

---

## 8. 常见坑(踩过的,省时间)

1. **NVENC 不生效**:光设 `video_codec=h264_nvenc` 不够,必须同时设 `ffmpeg_path` 指向支持 NVENC
   的 ffmpeg;且 NVIDIA 驱动要 570+。没 N 卡就用 `libx264`。
2. **黄金/某些具体题材视频稀缺**:免费图库真实「黄金视频」几乎没有。策略是「视频走通用市场/城市
   词库 + 强制插 1 张题材图」(见 wikigold 的 `video_query_pool` / `image_query`)。
3. **静态图卡顿 / 结尾空档**:长句配静态图会被拉长慢放显得卡;句数多时最后一句可能没素材。
   已修:新闻模板 `image_insert_every=0`(全视频)+ orchestrator「视频补不上自动图片兜底」。
4. **判定缓存**:`storage/real_footage_cache.json` 按 `逻辑版本|松严|sha|主题` 缓存。改了判定 prompt
   一定要 bump 版本号,否则旧误判会复用。
5. **Windows 编码**:偶发 `UnicodeDecodeError(gbk)` 出现在**视频产出之后**的收尾打印,不影响成片。
6. **montage 时长**:`target_seconds` 控制;已修过「无声片比 BGM 略长导致越界崩」的 bug。
7. **成本**:每个素材候选一次 Gemini vision 判定 + 每条配音 TTS。批量铺量前先按「视频数 × 句数」
   估算 API 调用量和费用,别拿单条速度线性外推。

---

## 9. 已知待办 / 可优化(非阻塞)

- 缩略图预筛已做(选材先判缩略图再下整段),但素材判定仍是主要耗时,可继续提速。
- 相关性判定目前只验「是否贴题」,不验「币种精确」(欧元那句可能配到别国钞票)。要更精确需把判定
  收严(拒绝率会上升)。
- 收尾的 `UnicodeDecodeError(gbk)` 打印可清理(不影响成片)。
- 还有个 FastAPI 后端 + 网页(`main.py` / `app/controllers/` / `resource/public/index.html`),
  是早期单条试做用的;**批量生产用 batch.py,不需要起服务**。

---

## 10. 一句话上手

装好依赖 + 改 `config.toml`(ffmpeg_path / 各 API key / 没N卡改 libx264)→
`python batch.py templates/诈骗预警` → 去 `output/` 拿片。改模板只动那个文件夹里的
`config.toml` + `scripts.xlsx` + `bgm.mp3`。

---

## 11. 推到 GitHub + 同事(用 Codex)拉取

### 11.1 ⚠️ 铁律:真实密钥绝不进仓库
`config.toml` 里有真实 API key。已用 `.gitignore` 忽略它,仓库里只放 `config.example.toml`(占位符)。
**推之前务必确认 `git status` 里看不到 `config.toml`。**
> 如果这个项目**以前**曾经把带密钥的 config.toml 提交过(哪怕后来删了,历史里还在),
> **一定要去各家控制台把 Gemini / DeepSeek / Pexels / Pixabay 的 key 全部重置(rotate)一次**,
> 否则等于已泄露。没提交过就不用。

### 11.2 你(项目所有者)首次推送
```bash
cd D:/WIKIFX项目/AI自动剪辑
git init
git add .
git status                      # 再三确认没有 config.toml、没有 storage/ output/
git commit -m "forex ai video engine"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

### 11.3 同事(Codex)那边:克隆 → 配置 → 跑
1. `git clone https://github.com/<你的用户名>/<仓库名>.git`
2. `pip install -r requirements.txt`(Python 3.13)
3. 装 ffmpeg;没 NVIDIA 显卡就用 config 里默认的 `libx264`
4. **复制配置并填自己的 key**:
   ```bash
   cp config.example.toml config.toml        # Windows: copy config.example.toml config.toml
   ```
   打开 `config.toml`,填上他自己的 `gemini_api_key` / `deepseek_api_key` / `pexels_api_keys`
   (Pixabay 可选);有 N 卡再把 `video_codec` 改 `h264_nvenc` + 设 `ffmpeg_path`。
5. 跑:`python batch.py templates/诈骗预警` → 成片在 `output/`。

> 同事需要的账号:**Gemini(Google AI Studio)+ DeepSeek + Pexels**(都能免费注册拿 key)。
> Pixabay 可选(扩素材池)。**不需要 OpenRouter**(本项目配音用 Gemini TTS,OpenRouter 没有 TTS)。

### 11.4 建议(可选)
- 想更省心可以建 **private 仓库**,把同事加成 collaborator——一样能 clone,更安全。
- 仓库里 `templates/*/scripts.xlsx` 现在是最近跑过的脚本,当示例留着即可,同事换成自己的脚本。
