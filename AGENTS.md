# 目标
输入脚本/主题 → AI理解内容 → 本地素材库优先、Pexels在线补足 → 自动按情绪选BGM → 配音+字幕+合成 → 批量产出。
领域:外汇,素材高度同质(盘面/K线/交易界面/货币/财经场景),作为匹配先验。
本版范围:含本地+在线编排、AI视频打标、情绪BGM、批量;不含视频生成(只预留接口)。

# 工作准则
1. 先通读参考项目再动手,不臆测其实现。
2. 新项目独立在 D:\WIKIFX项目\AI自动剪辑;参考项目全程只读。
3. 增量构建,严格按里程碑,一次一个,每个里程碑结束必须能跑出一条完整视频。
4. 实际运行环境:Python 3.13(系统无 3.11/3.12 可用安装;3.13 上 M0 全部依赖已验证)。
   AGENTS.md 原写 3.11 作为规划基准;以 3.13 为实际执行版本,不再回退除非 M2 环节出现兼容问题。
5. 标「原样复用」的代码可清理,但不要重写它解决的底层问题(MoviePy合成/字幕渲染/音频混合)。
6. 每个模块配一个最小自测(给定输入→产出文件),不要堆完再统一调试。

# M2 架构决策:embedding 统一走 Gemini API
打标(视频理解)和 embedding 都使用 google-genai,不引入 torch / sentence-transformers。
理由:M2 本来就要接 Gemini 做视频打标;embedding 走同一家,打标+向量一家全包;
避免 torch 在 Python 3.13 的兼容风险;数据量小,API embedding 成本与延迟可忽略。

# M2 前置检查(在 M2 正式开始前必须执行,需先拿到 Gemini API key)
只验 google-genai,不再验 torch / sentence-transformers。三项全通过才算过:
  1. pip install google-genai; python -c "import google.genai; print('OK')"
  2. 用 Files API 上传一个本地 mp4,调 Gemini 视频理解,确认能返回结构化结果
     (顺便确认 1 FPS 采样对外汇素材够用)
  3. 调 Gemini embedding API,对一句中文文案拿到向量
注意:
  - Gemini API key 由用户单独提供(不在 config.toml 里,等用户给 key 再开始)
  - 不要使用已下线模型(gemini-2.0-flash / 1.5 系列已于 2026-06-01 停服)
  - 前置检查已确认可用模型(2026-06-16):
      视频理解/打标: gemini-3.5-flash  (优先于 2.5-flash,生命周期更长)
      embedding:     gemini-embedding-001 (dim=3072)
                     task_type=RETRIEVAL_DOCUMENT 存素材, RETRIEVAL_QUERY 查 intent
                     A/B 实验证明此方案 mean_gap=+0.137,优于 embedding-2+prefix 的 +0.093
                     (embedding-2 不支持 task_type;裸 embedding 导致外汇/对照重叠,不可用)
    已写入 config.toml: gemini_model_name / gemini_embedding_model

# preprocess_video 的使用规则(重要)
参考项目 video.py 的 preprocess_video 做了三件事:
  (a) 路径安全检查 — 限制素材必须在 storage/local_videos/
  (b) 低分辨率过滤 — < 480x480 丢弃
  (c) 图片→视频转换 + Ken Burns 缩放动效
新项目 LocalProvider 不调用 preprocess_video,而是:
  - 入库时(library.ingest_file)执行 (b)(c),实现完全相同的质量过滤和图片转换逻辑
  - 不执行 (a):library 允许引用任意绝对路径(路径合法性由入库操作控制,不是 API 边界)
  - orchestrator 返回的路径直接传给 combine_videos,绕过 preprocess_video 的路径锁定
这样保留了画质和图片素材支持,只解除了不必要的目录限制。

# 从参考项目复用(复制,勿重写底层)
- app/services/video.py:切片/拼接/转场/字幕渲染/BGM混音/编码 → 原样复用,仅把BGM随机选曲入口换成情绪选择器。
- app/services/voice.py:TTS配音 → 原样复用。
- app/services/subtitle.py:字幕生成 → 原样复用。
- app/services/material.py 的 search_videos_pexels / search_videos_pixabay / search_videos_coverr / save_video → 复用为在线Provider底层;丢弃 download_videos 里"去重→随机洗牌→下到够时长即止"的选择逻辑(这是要被取代的核心)。
- app/services/llm.py 的 generate_script / generate_terms / _generate_response → 参考复用。
- app/models/schema.py → 参考并扩展(本地优先阈值、最少本地数、情绪偏好等)。
- resource/fonts/(字幕字体)、resource/songs/(BGM)→ 复用;songs需补情绪标注。
- main.py / app/controllers/v1/ → 作为后端骨架参考。

# 新建/重写模块
- orchestrator(核心,取代task.py选择逻辑):脚本分段→每段画面意图→本地优先匹配+在线补足→有序素材序列→交合成层。
- library:SQLite存本地素材元数据+embedding,提供检索;ingest_file 做质量过滤和图片转换。
- providers:统一MaterialProvider接口,实现Local/Pexels,预留Generated桩。
- tagging(M2):Gemini读视频→结构化标签/质量分/情绪,按hash缓存增量。
- bgm(M3):情绪标注+按情绪选曲。
- web(M4):更清晰专业的操作界面。

# 核心编排逻辑
  segments = split_script(script)
  对每个 seg:
    seg.visual_intent = LLM生成该段画面意图(调 generate_terms,per-segment)
    candidates = library.search_by_intent(intent, top_k=5)
    best = 按 质量*相关度 取最高
    若 best.score >= local_threshold: 用本地素材
    否则: 标记为待Pexels补足
  ensure_min_local(shots, n=min_local)  # 硬约束:至少N段用本地;不足则从库里挑质量最高、最贴主题的强制塞入
  对待补足的段: 调 search_videos_pexels + save_video 补素材
  (预留:仍无素材且 allow_generation=True 时 → Generated)
  按段序号排好 → 配音 → 字幕 → 合成
  要点:本地优先靠阈值;至少N个本地是硬约束;按段序对齐配音,解决原项目声画错位。

# 数据模型(SQLite materials 表)
  path, sha256(打标缓存key), duration, width/height, aspect,
  description, tags[](场景/主体), topic_fit[], mood, quality(1-10),
  has_watermark, embedding, created_at/updated_at
  BGM库:path, duration, mood, energy(1-5), tags

# AI打标管线(M2)
- Gemini读本地视频(google-genai Files API上传视频,或抽3-5关键帧省成本)。
- 领域特化prompt:声明这是外汇/财经素材,按K线/盘面/交易界面/货币/办公场景维度打标,情绪给 tense/professional/uplifting/neutral。
- 输出严格JSON写入materials表;以sha256缓存,只增量处理新增/变更素材。
- 打标后对 description+tags 调 Gemini embedding API 生成向量存库;SQLite+暴力cosine检索,不上向量库。
- search_by_intent 改成对 visual_intent 也 embed,用余弦相似度替换 M1 关键词重叠评分。
- local_threshold 重新标定(M1 的 0.2 作废;按真实余弦分布定,预计 0.5~0.7)。

# M2 改进点:_ensure_min_local 语义化(M2 实施)
M1 的 _ensure_min_local 按质量分强制插入本地素材,不保证和该段文案语义相关——
为凑 min_local 可能塞入无关素材,牺牲画面对齐。
M2 有 embedding 后,强制塞入逻辑改为:在本地库里挑和该段 visual_intent 余弦相似度最高的素材
(相关性优先、质量次之),而不是无脑取 top-quality。

# 情绪BGM(M3 ✅ 已完成)
- app/services/bgm_library.py:SQLite songs 表(path/sha256/duration/mood/energy/description)。
- tag_songs():Gemini 音频理解(Files API 上传 mp3 + gemini-3.5-flash)→ mood(tense/professional/uplifting/neutral)+energy(1-5),sha256 缓存增量。
- analyze_script_mood():DeepSeek 对整段脚本分类情绪。
- select_bgm(mood):库内同情绪随机选一首;无匹配则回退到任意已标注曲目。
- task.py 接入:params.use_mood_bgm=True 时,start() 在第 5b 步调用上述链路,写回 params.bgm_file,
  复用 get_bgm_file 现有的路径安全校验,不改动混音参数(音量0.2/循环铺满/结尾淡出3s不变)。
- 已知限制:原始 29 首 BGM 库情绪单一(几乎全是 professional/neutral 钢琴环境乐),
  用 Lyria 生成 3 首补 tense 类别后库内 4 种情绪都有,但 tense=2/uplifting=1 样本仍很薄,
  后续应持续用 Lyria 或其他来源扩充各情绪曲目数量。

# UI(要求高级清晰)(M4)
- 方案A(推荐,长期产品):FastAPI后端 + React/Vue + Tailwind。页面:①素材库管理(预览/标签/质量/重打标)②任务创建(脚本/本地阈值/最少本地数/情绪/画幅)③任务队列与产出预览。
- 方案B(起步快):在Streamlit上加素材库管理tab。
- 建议:MVP用B验证逻辑,M4再升级到A;别在前期把精力耗在UI。

# 视频生成接口(本版桩,务必预留)
  MaterialProvider 协议:fetch(visual_intent, aspect, duration) -> MaterialResult | None
  LocalProvider:查标签库
  PexelsProvider:复用 search_videos_pexels + save_video
  GeneratedProvider.fetch:raise NotImplementedError("video generation not enabled in this version")
  编排器按优先级 Local→Pexels→(未来)Generated 取素材;allow_generation 默认 False,主干不耦合具体来源。

# 里程碑(按序,一次一个,每个都要能跑出一条视频)
- M0 ✅ 骨架+复用层接通:建新项目,复制复用模块,确认能用原逻辑跑出一条视频。
- M1 ✅ 标签库+编排器(MVP核心):建SQLite;先用简单标注(文件名/手动)填库;实现本地优先+Pexels补足+至少N个本地+按段序对齐+去重。四条实测通过(2026-06-16)。
- M4a ✅ 后端API骨架(2026-06-17):新增 app/controllers/v1/library.py(素材库/BGM库列表+打标触发),
  注册进 app/router.py;新增 schema.py 响应模型(MaterialListResponse/SongListResponse/TaggingTriggerResponse)。
  任务创建/查询复用已有 /api/v1/videos、/api/v1/tasks/{id}(VideoParams 已含 M1/M3 字段,无需新模型)。
  打标接口同步执行(库小、低频管理操作,非热路径;若库变大需挪到 task_manager 异步)。
  最简前端 resource/public/index.html(原 MoneyPrinterTurbo 占位页替换)串通素材库/BGM库/建任务/轮询/播放,
  端到端验证:POST /api/v1/videos → 任务完成 → /tasks/{id}/final-1.mp4 可访问(200, 6.7MB),
  orchestrator 真实选中 2 本地+1 Pexels,use_mood_bgm 生效。
  修复一个预先存在的依赖缺口:app/controllers/v1/video.py 无条件 import redis(即使 enable_redis=false
  也会导入 RedisTaskManager 类),requirements.txt 补充 redis、google-genai(此前仅 pip install 未记录)。
- M3 ✅ 情绪BGM(2026-06-17):Gemini 音频理解打标 mood/energy(sha256缓存),DeepSeek 脚本情绪分类,
  按情绪选曲替换 get_bgm_file 随机逻辑。关键发现:原 29 首 BGM 全是平静钢琴/环境音乐,
  实测 tense=0/uplifting=1,无法验证区分度;用 Gemini Lyria(lyria-3-pro-preview,
  通过 generate_content 文本prompt直出 audio/mpeg)生成 3 首 tense 风格曲目补齐
  (其中 2 首被 Gemini 判定为 tense,1 首判为 professional,标注未受人工干预)。
  3 条情绪脚本验证:tense/uplifting/professional 识别全部正确,选曲三条彼此不同且情绪匹配。
  select_bgm() 同情绪内随机选(已验证 8 次调用 6 个不同文件,非固定第一首),支持
  exclude_paths(批量任务防同质化重复)和 target_energy(可选二级筛选)。
  注意:lyria-3-pro-preview 是 preview 模型,曲库扩充备选方案,可用性/接口可能变化,
  生产环境长期依赖前应关注 Google 是否转正式版或更换模型名。
- M2 ★前置检查已通过(2026-06-16)★ Gemini打标:接入视频理解自动产出标签/质量/情绪+sha256缓存增量,替换M1简单标注;Gemini embedding语义检索替换关键词评分;_ensure_min_local 改为相关性优先;重新标定 local_threshold。
- M3 ✅ 情绪BGM:标注+按情绪选曲,替换随机选曲(2026-06-17 完成,见上方详述)。
- M4a ✅ 后端API骨架+最简前端打通(2026-06-17,详见上方)。
- M4b ✅ 三正式页面+依赖核对(2026-06-17):
  · 新增 orchestrator.preview(script, params) — 复用 orchestrate() 的 segment/intent/local-match/
    min_local 步骤(抽成 _plan_local 共享函数),但不触发 Pexels 下载,未命中段标 source="pexels_preview"。
    新路由 POST /api/v1/orchestrate/preview。
  · library.py 新增 get_by_id/get_by_path;新路由 POST /api/v1/materials(上传+ingest_file+即时增量打标)、
    GET /api/v1/materials/{id}/file(预览流)。
  · 修了一个预先存在的 bug:GET /api/v1/tasks(列表)没像单任务接口一样把视频路径转成 /tasks/... URI,
    直接返回服务器本地绝对路径,前端拿到play不了——已修复,复用 _task_file_to_uri。
  · resource/public/index.html 重写为三标签页(素材库管理/任务创建+编排预览/任务队列与产出),
    "高级清晰"但克制 — 卡片+徽标配色,无构建步骤,纯原生 JS。
  · 编排预览实测:3段脚本 → 2段自然本地命中(score 0.59/0.59,未触发min_local)+1段pexels_preview,
    预览阶段零下载零合成。
  · 依赖核对:全新临时 venv(纯ASCII路径,C:\temp)按 requirements.txt 装包 → 19个核心模块全部
    import成功 → 跑通 test_m1.py 端到端产出 8MB mp4,PASS。requirements.txt 此前缺 redis、
    google-genai(M2/M3 期间装了但没记录),已补全。
- M4b ✅ 三正式页面+依赖核对(2026-06-17,详见上方)。引擎成型,全部里程碑完成。
- (未来,本版不做)接入视频生成Provider。

# "快速紧张"剪辑风格(2026-06-18,非里程碑)

## 五项改动
1. **快切**:VideoParams.video_clip_duration 默认 5s → 2.2s(可配,2~2.5s区间)。
   video.py 的 combine_videos() 本来就按 max_clip_duration 把每个源视频切成连续窗口
   (0~2.2s, 2.2~4.4s...),缩短这个值不需要改逻辑,直接产生更多更短的切片、切换更快。
2. **素材加速**:新增 VideoParams.clip_speed_factor(默认1.3,建议1.2~1.5区间)。
   combine_videos() 里对每个切片调用 clip.with_speed_scaled(factor)。安全性:
   _open_video_clip_quietly() 打开素材时 audio=False(项目素材阶段本来就不保留素材原声,
   配音/BGM 在 generate_video() 阶段才统一挂载),所以加速只影响画面,不会牵动音频。
3. **转场克制**:新增 VideoTransitionMode.tense("Tense")。选这个模式时:
   - combine_videos() 只在 tense_transition_count(默认2)个随机片段边界调用快速转场,
     其余全部硬切(不调用任何 transition 函数,这是和原有"整段统一应用一种转场"逻辑的
     本质区别——原来 video_transition_mode 是对全片生效的单一开关,tense 模式是"按位置选择性应用")。
   - 新增三个转场函数(app/services/utils/video_effects.py):
     quick_zoom_transition(快速 punch-in 缩放,0.2s)、whip_pan_transition(复用 slidein
     位移逻辑但缩到 0.2s,从"1s+ 慢滑入"变成"快速甩入")、white_flash_transition(片段前插入
     极短纯白帧模拟闪白卡点,没用 alpha 渐隐——这版 MoviePy 的 with_opacity 不支持随时间变化
     的回调,强行做容易在合成阶段出问题,改用最朴素的"插入白帧再拼接"反而最稳)。
   - "Glitch" 风格转场暂未实现(故意诚实说明,不是漏做):RGB 通道抖动/像素位移类效果在
     MoviePy 这个版本里没有现成稳定的实现路径,需要更多调研才能保证不在合成阶段崩,
     这次先用 quick_zoom/whip_pan/white_flash 三种覆盖"快速紧张"的转场需求,后续可以再加。
4. **强制紧张 BGM**:新增 VideoParams.force_tense_bgm(默认False)。为True时,task.py 的
   BGM 选曲钩子跳过 analyze_script_mood(),直接 mood="tense" + target_energy=tense池最高能量。
   曲库扩充:Lyria 生成6首新 tense 曲目(不同乐器/节奏描述,确保实际听感有区分度,不是同一首的
   变体),tense池从2首扩到8首,缓解批量产出BGM同质化。
5. **素材复用策略调整**(写入下方独立小节)。

## 判定缓存版本化 + 横向素材去黑边(2026-06-23 第十一轮)

**第一幕又是动画的真因 = 缓存吃旧账**:用户发现第一幕是 $100 动效图(平涂绿底+卡通
手)。排查:拿当前判定逻辑重新问它→正确 REJECT,说明判定本身没坏。真因是 classify
缓存键只有 sha+topic,这个动画文件在更早一轮(判定还弱时)被缓存成"通过",主题没变
就复用了旧结论。**修复:缓存键加判定逻辑版本号 _CLASSIFY_LOGIC_VERSION(当前 v2),
改判定 prompt 就 +1,旧结论全部自动失效重判**;并清空了现有陈旧缓存。重跑确认第一幕
变成真实拍摄的 $100 钞票特写。教训:任何"判定结果缓存"都要带逻辑版本,否则升级判定
标准后历史误判会一直赖着。

**横向素材去黑边(cover 而非 contain)**:伊朗美国样片第一幕是一张横向中东地图,被
combine_videos 的 resize 用 contain 逻辑(缩到刚好放进竖屏)上下留大块黑边。竖屏新闻
里很难看。改成 **cover**:取较大缩放系数让画面铺满整个 9:16,多出部分居中裁掉
(clip.cropped)。竖屏短视频标准做法。竖向素材本来就铺满、不受影响,只有横向源以前会
留黑边。验证:1920x1080→缩放 3414x1920→裁 1080x1920,无黑边。

**两条真实新闻样片验证(第十轮对齐逻辑)**:
- 诈骗故事(406a8389,11句):每句搜索词精准映射剧情(看手机消息→person reading phone;
  网站完美→sleek website+5星好评;提现被拦→withdrawal denied;网站关停→empty office
  frozen login;结尾→wikiFX app)。
- 伊朗美国(f521e523,10句):中东地图→US Capitol(签约)→油桶油价→霍尔木兹油轮→美元→
  欧元澳元→油罐央行→分析师→加元→签字世界地图。按字幕时间戳截帧:第一幕真实地图、
  签约句真实美国国会大厦、霍尔木兹句真实油轮过海峡,全部真实+贴题+一句一镜对齐。

## 片头片尾 + 模板化批量出片(2026-06-25 第十二轮)

用户最终诉求是批量生产,浏览器界面只适合单条试做。

**片头片尾(make_intro_outro.py)**:独立 CLI,给任意成片加"酷炫快速片头"(WIKIFX 品牌
标题卡,whoosh 滑入→定格→滑出 ~1s + 合成 whoosh 音效)并拼接固定片尾视频。
- 片头用 MoviePy:深底 + WIKIFX 白色粗体 + 品牌黄下划线 + 红色警示文字(FRAUD ALERT
  等)。坑:TextClip 用 label 模式会把字形上下裁切,必须用 method="caption"+给足高度
  的 size 框。底部文字按长度自适应字号(长文案如 FOREX SCAM ALERT 缩小避免换行)。
- whoosh 音效:ffmpeg anoisesrc 带通 + 快进慢出包络合成。
- 片尾:用户放在 resource/FX-EN-.mp4(1080x1920/1.54s/带音频,WikiFX 官方片尾卡)。
- 拼接:ffmpeg concat 滤镜,三段统一到 1080x1920/30fps/aac 44100 stereo 再拼,避免
  fps/分辨率/音频参数不一致导致错位。
- 用法:python make_intro_outro.py <正片> "WIKIFX" "FRAUD ALERT" resource/FX-EN-.mp4 <输出>

**模板化批量(batch.py + templates/)**:一个模板文件夹 = 一种节目形态。
- 结构:templates/<名>/config.toml(所有参数) + bgm.mp3 + scripts.xlsx(一行一条,
  列 script必填 / title输出名 / subject/badge/language 选填逐行覆盖)。
- config.toml(flat toml,tomllib 读):映射到 VideoParams,外加 intro_*/outro_video
  片头片尾键(构造 VideoParams 前剔除)。bgm_file 相对模板、outro_video 相对项目根,
  加载时解析成绝对路径。
- **进程内同步调用**:batch.py 不走 HTTP 服务/不轮询,直接 sm.state.update_task +
  tm.start(task_id, params, stop_at="video") 顺序一条接一条跑完,再调 make_intro_outro
  加头尾。某条失败不影响其它条。输出到 output/<模板名>_<时间戳>/[title].mp4 +
  results.csv(成功/失败/路径/耗时汇总)。
- 依赖:新增 openpyxl(读 xlsx);也支持 scripts.csv 零依赖回退。
- 新模板 = 复制文件夹改 config + 换 Excel,不碰代码。
- 验证:plumbing(配置/Excel/VideoParams/逐行覆盖)全部测通;2 行模板真跑确认进程内
  tm.start 与服务器路径效果一致。

**待办(用户已认可,下一步)**:缩略图预筛提速——选材时先用搜索接口返回的缩略图判
相关性,跑题的不下载整段视频,把每条材料阶段从 ~12min 砍到 ~4min。批量场景收益最大。
(本轮先搭批量:批量是只调流水线的外挂代码、风险低;提速要改核心选材逻辑、风险高,
单独做以免连累刚调好的对齐/相关性。)

## 按句子对齐(一句一镜)+ 允许标志性地标(2026-06-22 第十轮)

用户问"当前匹配逻辑是什么",并指出:最后一句"Washington"应该是白宫/华盛顿航拍、
"分析师"那句应该是交易员而不是空麦克。排查发现根因比"搜索词不准"更深:

**关键发现:之前片段根本不和句子对齐**。_prioritize_unique_source_clips 会
random.shuffle 打乱所有片段,combine_videos 再连续填满音频——所以即使搜到白宫,也会
被打乱到别处;补抓的 b-roll 更不挂靠任何句子。加上第八轮"禁止点名地标"规则把
"Washington"压成了"government building columns",以及 prompt 里"press podium
microphones"被 LLM 滥用(空麦克)。

用户拍板:**按句子对齐(一句一镜)+ 允许 stock 能搜到的标志性地标**。实现:
- **schema** 加 align_clips_to_script(默认 True)。
- **combine_videos** 加 clip_durations 参数 + aligned_mode:给定时,video_paths 是
  "每句一个素材按文案顺序",每段画面对齐到那句话的时间窗、按顺序播放(不打乱、不
  按 max 截断)。SubClippedVideoClip 加 target_display 字段,_process_subclip 按它
  单独算每段速度(源够长≈clip_speed_factor;源偏短<1轻微慢放铺满),不再统一加速。
  消耗循环在 aligned 模式处理全部片段不提前 break。
- **orchestrator** aligned 模式跳过 extra-fetch(一句一镜不需要填充,重复问题自然
  消失),并按"句子字符数占比 × 音频总时长"算每段 clip_durations 返回。
- **数据流**:orchestrate 返回 (paths, shots, clip_durations) → get_video_materials
  → start → generate_final_videos → combine_videos(改了 4 处签名)。
- **llm prompt 放开**:第一个搜索词必须是这句最贴切的画面(Washington→white house
  aerial/us capitol;analysts→trader at trading desk;euro→euro banknotes);**允许
  stock 真有的标志性地标**(白宫/国会山/华尔街/NYSE/时代广场/各城市天际线),只禁
  搜不到的(央行内部、在世官员脸);压制"press podium"滥用(只在官方announcement用、
  最多一次)。
- **顺带修分句 bug**:split_script 之前把 "1.15" 的小数点当句号,把 "below 1.15"
  切成 "below 1." 一个 1 秒截断碎片。改成英文 .!? 只在后面跟空白时才断,中文。！？
  照旧。

**验证(ef52e95d)**:aligned durations=[5.4,1.3,6.0,...]。按字幕时间戳精确截帧:
"Washington"那句→国会山穹顶航拍✓;"ECB"那句→法兰克福金融塔✓;"Analysts"那句→货币
画面、时间对齐、不再空麦克✓。一句一镜、无重复。**遗留**:货币精度——"euro"那句搜
"euro banknotes"实际返回了阿根廷比索(相关性只验"是否财经",没验"是否欧元")。要更
精确需把相关性判定改成"是否匹配这一句"(更严、拒绝率更高),作为后续可选项。

## 片段≥2秒(按播出时长) + 补抓独立素材降重复(2026-06-22 第九轮)

用户反馈:商务人士等同一画面反复出现太多次(且不同视频之间也明显雷同),以及有的
片段一闪而过不到 1 秒。

**片段不到1秒 → 改按"播出时长"控制**:根因是片段会被 clip_speed_factor(1.3x)加速,
1.2s 源窗口播出来只剩 0.92s;加上素材尾段切出的碎片。combine_videos 改为:
min_clip_duration/video_clip_duration 一律按"屏幕上看到的秒数"解释,源窗口 = 播出
时长 * 加速倍数;并加"尾段吸收"——剩余不足一个最短窗口就并入相邻段,不再切碎片。
默认播出区间 [2.0, 3.0]s(schema: min_clip_duration 1.2→2.0, video_clip_duration
2.2→3.0)。模拟+实测确认每段播出都 ≥2.0s。

**同一素材反复出现 → 编排器补抓独立素材**:根因是少量源(9个)填长音频(37s),
combine_videos 把每个源切成多窗口反复取用;长源片尤其容易在不同段落重复出现。
orchestrator 新增 5a 步:按 ceil(音频时长/单片播出时长) 估算需要多少独立片段,
不够就用各段搜索词多抓(走相关性判定+硬去重+跨视频冷却),上限 _MAX_EXTRA_BROLL_CLIPS
(10)。补抓的 URL 也记入冷却历史 → 跨视频也更不一样。实测 9段+6补抓=15 独立源,
正好覆盖 ~15 个窗口,基本一段一镜。

**验证(00a9e7e4)**:日志确认 on-screen [2.00,3.00]s + extra b-roll +6(target 15)。
抽帧 4 帧全部不同画面、全部切题、全部 held 镜头(无<1秒闪切):商务主播/讲台发言人/
穹顶政府建筑(替"Bank of Japan")/看手机K线。相关性判定这次拦了 32 个跑题候选才凑够
15 个切题素材(成本提醒:多样化搜索词会捞回大量跑题素材,过滤调用量不低,但用 lite
模型可控)。

## 相关性判定 + 便宜模型 + 可变片段长度(2026-06-22 第八轮)

用户反馈三点:画面有跑题素材反复出现(非洲乡村妇女)、gemini-3.5-flash 太贵、
美联储这类素材搜不到;另外指出每段素材长度一样、要有长有短。

**可变片段长度**:combine_videos 之前每段都切成 max_clip_duration(2.2s),节奏匀速
单调。加 random_clip_duration(默认 True)+ min_clip_duration(默认 1.2),每个窗口在
[min,max] 内随机取长度。紧张转场的"实际片段数"估算改用区间均值。schema+前端+
task.py 都接好。

**换便宜模型**:gemini_model_name 3.5-flash → gemini-3.1-flash-lite。坑:
gemini-3.1-flash(非 lite)不存在,只有 lite。lite 做图像识别没问题(准确认出 ECB 总部)。

**真实性判定升级为"真实+相关"判定**(核心):tagging.classify_real_footage →
classify_footage(video_path, topic),一次 Gemini 调用同时判"是否真实拍摄"+"是否跟
财经相关",挡掉跑题素材(街头小贩/野生动物/乡村等)。topic 从 orchestrator 一路传到
PexelsProvider/ImageProvider。缓存键改 sha+topic。踩了两个坑:
  1. **lite 模型对"分类表式"长 prompt 判得太松**(街头卖玩具小贩都放行)。实测换成
     "反问句 + 否决项 inline 写进问句 + 二元 GOOD/REJECT"的尖锐问法,同一 lite 模型
     就判对了。所以不用为相关性回退到贵模型。
  2. **真 bug:对静态图 ffmpeg 抽帧失败**(`-ss` 在单帧图上返回 0 字节)→ 之前
     ImageProvider 的图片永远抽帧失败→默认放行,相关性判定对图片形同虚设。修复:
     classify_footage 检测输入若本身是图片(.jpg/.png/...)就直接读,不再抽帧;只有
     视频才抽帧。finally 只删抽出来的临时帧、不删原图。
  实测:街头小贩 REJECT、ECB/写字楼/商务雕像 PASS。真实视频流程里也拦了 4 个跑题素材。

**搜索词避开找不到的地标**:_generate_diversified_broll_terms 加规则——stock 库没有
"Federal Reserve building"/"ECB headquarters"这类具体机构实拍,禁止点名地标,改用真能
搜到的替身(央行→government building columns/press podium;华尔街→wall street sign/
NYSE facade;美元→dollar bills closeup;经济→cargo port/financial skyline)。

**验证(d9da413e)**:相关性判定拦了 4 个跑题素材,成片逐帧全部切题(讲台发布会/
交易屏/平板标注 K 线),"Federal Reserve"那句配 K 线标注而非莫斯科大楼。便宜 lite 模型
判定够用。

**附带修复**:_ensure_min_local 调 get_top_quality 时 min_duration 从默认 3.0 放宽到
1.0(否则将来重新导入短品牌片会被静默筛掉,min_local 失效)。注:本地种子素材已被
用户手动删除(用户对其不满意),真实品牌库尚未导入,故近期 local=0 全用在线素材属正常。

## 静态图插入(Ken Burns)+ Pixabay 接通(2026-06-22 第七轮)

接上第六轮:用户提供 Pixabay key 填进 config.toml(pixabay_api_keys),实测同一搜索词
候选从 20(仅Pexels)→70(pexels20+pixabay50),池子×3.5,B 多源真正发挥作用。

新增"偶尔插一段静态图+Ken Burns 运镜"丰富节奏(用户拍板做这个,生成式短动画明确
不做 —— 理由:AI 生成"逼真外汇画面"最易露假,和我们 classify_real_footage 强制
真实素材的新闻调性打架,且成本/耗时不划算;要生成式丰富度更该投"金融动态图"而非
假真人视频):
- material.search_images_pixabay():Pixabay 图片库(和视频同一个key),image_type=
  photo 只取真实照片(天然排除插画/矢量,不需要再过 classify_real_footage)。图片库
  体量比视频大一个数量级,既丰富节奏又进一步扩池。
- material.render_ken_burns_clip():把静态图用 ffmpeg zoompan 渲成带缓慢运镜
  (随机 缓推/缓拉 + 随机平移方向,系列化产出多张图不雷同)的小 mp4,输出是普通片段,
  下游 combine_videos 完全无感。zoompan 防抖关键:先 scale 放大到目标2倍+crop 填满
  竖屏(无黑边、源够大避免整数步进抖动),再 zoompan 输出 1080x1920。实测首尾帧确认
  运镜平滑无锯齿、满屏。
- providers.ImageProvider:搜图→去重(同样吃 exclude_urls 硬约束+cooldown_urls 软
  约束两段式回退)→下载→渲 Ken Burns→返回 MaterialResult(source=pixabay_image)。
  没配 pixabay key 时直接返回 None。
- orchestrator:按段号每 image_insert_every 段(默认5)安排一个图片插槽,
  (segment_index+1)%N==0 且该段是 pending(本会走在线视频)时才换成图,落在本地素材
  段则跳过(保留本地);图失败(没key/搜不到/渲染失败)静默回退在线视频,不让镜头落空。
- schema 加 image_insert_every(默认5,0=关闭),前端建任务页加频率控件。

**端到端验证**:v4 样片(9段)插入1张图(seg[4],橙色写字楼,Ken Burns 缓拉),成片
11.4MB 可播放,新闻叠加层(角标/字幕/黄条/ticker)全在,图片片段和视频 b-roll 衔接
自然。注:图片在时间轴位置不按字幕段对齐(combine_videos 连续填充音频时长),逐帧找
要按实际 kb-*.mp4 源确认。9段视频每5段→1张(10段会2张),要更密把 image_insert_every
调小。

## 系列化产出去重 + NVENC 硬件编码(2026-06-22 第六轮)

**背景**:系列化产出外汇视频时,不同视频的素材大量重复。根因是两点叠加 ——
(1) 池子小:外汇题材在 Pexels 真实拍摄素材本就少,搜索词又窄(旧 generate_terms
约束 #2 "always add the main subject" 逼着每个词都带 forex/dollar,高度雷同),
真实素材过滤再砍掉一半动画;(2) 跨视频无记忆:orchestrator 的 used_*_urls 只在
单条视频内生效,每条新视频都从同样的搜索词、同样的 items[0] 开始抓,必然撞车。
两点是乘性的:只加去重不扩池,小池子两三条就被掏空 → "素材不足"硬失败。所以
必须同时"扩池 + 记忆",三管齐下:

- **A 搜索词多样化**:llm.generate_terms 加 diversify_broll 模式(走
  _generate_diversified_broll_terms),换一套 prompt —— 不再把主题词硬塞进每个词,
  改产出"具体、可拍摄、彼此不同"的泛财经 b-roll 画面(交易员/交易所/城市金融区/
  银行大楼/点钞/报纸头条/办公室),最多一个词可带字面主题。同源可用池立刻翻几倍。
  orchestrator.generate_visual_intent 默认开启。
- **B 多源聚合**:material.search_videos_multi 把 Pexels/Pixabay/Coverr 三个源
  (搜索函数早就写好了,只差接通)按"已配置 key"聚合合并候选。某源 key 未配置就
  静默跳过(不报错),所以用户没填 Pixabay key 时照常只用 Pexels。providers.
  PexelsProvider 改用 search_videos_multi(类名保留向后兼容,实际已是多源)。
  **要扩池得在 config.toml 填 pixabay_api_keys / coverr_api_keys**(免费注册)。
- **C 跨视频冷却去重**:新模块 usage_history.py(独立 SQLite usage_history.sqlite3),
  把每条视频用到的"在线图库下载 URL"按 video_seq 记账。新视频开始前预加载最近
  N 条(material_cooldown_videos,默认8)用过的 URL 作为**软排除**注入 stock 选材。
  软+两段式回退:provider 先跳过 硬排除(本视频内)∪冷却期;若非冷却候选全不可用
  再放宽冷却 —— 小池子不会被烧干变短/失败,正是"冷却 N 条"而非"永久拉黑"的意义。
  **只作用于在线图库,不碰本地库**(本地 3 条是自有品牌 b-roll,跨视频复用合理,
  对它冷却只会误触发 min_local 不足)。冷却记录只在 orchestrate 的硬不复用检查
  通过后(视频真的会被生成)才落库。

**直接验证(不渲染整片,省时)**:同一外汇主题连跑两次 orchestrate,RUN1 用 2 条
在线素材,RUN2 用完全不同的 2 条,**重叠=NONE**;本地 3 条两次都复用(符合设计)。
搜索词肉眼确认已多样化(city skyline / trader monitors / ECB HQ / DC capitol /
counting bills,而非 forex dollar high×N)。新增 schema 字段 material_cooldown_videos
(默认8) / diversify_broll(默认True),前端建任务页加了对应控件。

**NVENC 硬件编码(同轮)**:之前查到最终合成编码占总耗时 56%(8分51秒)用的是纯 CPU
libx264。用户机器有 RTX 4060 Laptop。坑一:NVIDIA 驱动 566.26 太旧,ffmpeg 这个
build 要求 nvenc API 13.0 / 驱动 570.0+,实测报 "Driver does not support required
nvenc API version"。用户升级到 Studio 驱动 610.62 后实测 NVENC 编码成功。坑二:
只在 config.toml 设 video_codec=h264_nvenc 不够 —— 硬件编码器探测用的是
utils.get_ffmpeg_binary()(能找到支持 NVENC 的 WinGet ffmpeg),但 MoviePy 内部
真正编码走的是它自带的 imageio_ffmpeg 精简版二进制(不含 NVENC,许可证原因),
两边不是同一个 ffmpeg → 先尝试 nvenc 失败再静默回退 libx264,反而更慢(17分40秒)。
**修复:config.toml 显式加 ffmpeg_path 指向 WinGet ffmpeg**,启动时 config.py 会
把它写进 IMAGEIO_FFMPEG_EXE,MoviePy 内部才真正吃到 NVENC。三组对比:纯CPU 15分48秒
→ 配置不全误回退 17分40秒 → 真正NVENC 10分23秒(快34%,最终编码段快40%)。提速没到
"编码归零"是因为这一段还含 MoviePy 逐帧渲染多层滚动叠加(纯 Python 回调,换编码器
帮不上),那块是剩余瓶颈。

## 财经突发新闻包装 news_mode(2026-06-18 第四轮)
新增 VideoParams.news_mode 总开关，融合此前全部设置(快切/加速/紧张BGM/绝不复用/
atempo语速/英文配音字幕)，叠加新闻包装层：
- 角标(左上角，墨蓝底白字)：news_badge_text + 当前日期。
- Lower-third 标题条：复用字幕的句级时间轴(sub.subtitles 本身就是按段对齐的，
  不需要从 orchestrator 另外拉取分镜数据)，深色半透明条 + 左侧墨蓝色 tab。
- 底部 ticker：从字幕文本提取关键词拼接滚动，不编造具体数字(没接实时行情API，
  编造价格类数字属于误导内容，明确不做)。
- 字幕样式本来就是白字配深色半透明条、整句显示(不是逐字卡拉OK)，news_mode 下
  只是把 Y 坐标上移，让位给下方 lower-third+ticker，没有改字幕渲染逻辑本身。
- 配音"主播腔"：gemini_tts() 新增 style_prompt 参数，只拼接进*送给模型*的内容，
  不影响传给字幕时间轴生成的原始 text——实测验证过指令本身不会被读出来(5.5s音频
  对应13词原文，不是38词)。

**用真实素材发现的问题(不是猜测)**:跑通第一条验证片后逐帧截图复查，10个素材里
4个不是真实拍摄(3个是Pexels上的"DOLLAR/FUNDS"动画解说卡片+小猪存钱罐插画系列，
1个是真实照片裁进圆形蒙版的拼贴动效包装)。根因:Pexels对"dollar/funds/profit"
这类抽象金融概念词的搜索结果里，混杂大量"概念解释"类卡通动画素材，这类素材
因为更贴合抽象关键词反而经常排在真实拍摄镜头前面。

**修复:app/services/tagging.classify_real_footage()** —— ffmpeg 截一帧静态图
(不上传整段视频到 Files API，单帧判定够用、快得多也省得多)，调 Gemini vision
判定 REAL/ANIMATED。接入 PexelsProvider(require_real_footage=True 默认开启)：
下载候选后先判定，非真实拍摄就丢弃文件、换下一个候选，候选耗尽则视为"素材不足"
(复用已有的硬性不复用失败路径，不会静默接受动画素材)。
判定服务本身故障时保守放行(返回True)，避免分类器抖动连带导致"素材不足"误报。
**实测验证**:用已知的3个动画+3个真实素材做对照测试，6/6 全部判断正确。
**成本提醒**:每个 Pexels 候选多一次 Gemini vision 调用，会增加延迟(单次约5-10s)
和 API 调用量；批量出片时这个开销会和已经存在的"快切+绝不复用导致 Pexels 调用量
上升"问题叠加，更要关注 API 速率限制和总生成耗时。

**Lower-third 重做为品牌横条(同日第五轮)**:原方案复用字幕原文导致和字幕区完全
重复，改成固定不变、持续滚动的"WIKIFX NEWS ★"品牌横条，颜色用 WikiFX 品牌黄
(`#F2B705`，例外于本项目其余墨蓝/米白配色规则——这是品牌识别色，非"廉价电视台
红黄"，用户已确认)。`_build_news_lower_third_brand_clip()` 整段只生成一次(不再
按字幕分段重建)，位置在 ticker 正上方。

**用真实素材+完整流水线二次验证(同日第五轮)**:用同一段特朗普外汇文案重新跑一条
~46s 的完整验证片(28 个 clip，含 news_mode 全部元素)。
- 日志确认 classify_real_footage 在真实流程里真的拦截了第一轮验证片暴露的同一批
  动画素材(vid-6945f1cc/vid-accb1dfa/vid-22b4b9ad)，换上的全部10个最终素材都是
  真实拍摄镜头，逐帧复查未见动画/插画混入。
- 顺手发现并修了一个效率问题：同一批动画素材在不同近义关键词的搜索里反复出现，
  没有缓存导致重复调 Gemini 判定。新增 `storage/real_footage_cache.json`
  (sha256 → bool)，`classify_real_footage()` 现在先查缓存。
- 截帧逐帧确认四层叠加无重叠:左上角角标(墨蓝)→中下方新闻字幕(黑底白字整句，非
  卡拉OK)→黄色品牌横条→最底部黑底白字 ticker，布局清晰、配色符合预期。

**发现并修复一个严重的 Windows 可靠性 bug**:MoviePy `write_videofile()` 在
Windows 上写完正片*之后*，清理 `xxxTEMP_MPY_wvf_snd.mp4` 临时音频文件这一步偶发
撞上文件还被 ffmpeg 子进程占用，抛出 `PermissionError: [WinError 32]`。这个异常
之前完全没被捕获，会让任务线程(`Thread-3 (run_task)`)直接挂掉且不打印到任务状态
——任务永远卡在"进行中"(progress 卡死不动)，即使 `final-*.mp4` 其实已经写完、完
全可播放(本次验证片就是这样：18:42 报错，但 ffmpeg 仍把 16.4MB 完整视频写到了
18:52，用 `ffmpeg -f null` 校验完整无损)。
修复:`_write_videofile_with_codec_fallback()` 新增专门捕获 `PermissionError`，
只有输出文件确认存在且非空时才把异常降级成警告(忽略)，文件确实没写出时仍然
`raise`，不会误吞真正的失败。这个 bug 在此之前的所有验证轮次里可能都存在，只是
长视频(更多 clip/叠加层、写入耗时更长)更容易撞上这个时序窗口，之前的短验证片
侥幸没触发。

## BGM 试听 + 手动指定(2026-06-18 第三轮)
用户反馈强制随机选的紧张曲太吵，想自己听、自己挑。
- 新增 GET /api/v1/songs/{id}/file 流式接口(对照 materials 已有的同款接口)，
  前端素材库页 BGM 表格每行加 mood 色点 + "▶ 试听"按钮，复用 toggleMaterialPlay 同款交互。
- 新增 VideoParams.bgm_mode("auto"|"manual")。task.py 的 BGM 选曲钩子改成 bgm_mode 优先判断：
  manual 时完全跳过 use_mood_bgm/force_tense_bgm 整个分支，直接用前端已经设好的 params.bgm_file。
  **这是个真实的修复点**:旧逻辑里 use_mood_bgm=True 会无条件覆盖 params.bgm_file，就算用户手动
  指定了曲子也会被自动选曲悄悄顶掉，"手动指定"形同虚设。已用实测验证:手动选一首 energy=1 的
  安静曲(自动逻辑绝对不会选中)，提交任务后日志只有"bgm_mode=manual"一行，后面直接进
  generate_final_videos，中间没有任何 force_tense_bgm/select_tense_or_high_energy_bgm 的调用痕迹。
- 新增 VideoParams.bgm_max_energy(默认5)，bgm_library.select_tense_or_high_energy_bgm() 加
  max_energy 参数，自动选曲池从"tense∪energy>=4"进一步收紧成"tense∪energy>=4 且 energy<=上限"，
  嫌 energy=5 太吵可以把上限调到 3~4。
- bgm_volume(混音音量)本来就是已有字段，这次在任务创建页前端暴露成输入框(之前是硬编码0.2)。
- 任务创建页新增 BGM 模式选择(自动/手动)+手动选曲下拉(带试听)+音量输入+energy上限输入。

## 三项调整(2026-06-18 第二轮)
1. **语速真正生效**:Gemini TTS API 不支持调速参数，voice_rate=1.2 之前完全空转。
   新增 voice.apply_voice_rate_postprocess()：TTS 生成音频后用 ffmpeg atempo 滤镜做
   后处理变速(变速不变调)，与具体 TTS provider 无关，文件生成好就能用。已在 gemini_tts()
   里接入，必须在变速*之后*再读音频时长、填字幕时间轴，否则字幕会按变速前时长错位。
   atempo 单次调用有效范围 0.5~2.0，项目 1.2~1.5 区间在范围内不需要链式叠加。
   实测验证:1.0x 音频 6.74s，1.2x 音频 5.62s，比值 1.199 ≈ 1.2，精确匹配。
2. **BGM 选曲池放宽**:新增 bgm_library.select_tense_or_high_energy_bgm(min_energy=4)，
   选曲池从"mood=tense"放宽成"mood=tense OR energy>=4"——产品要的是"听感快速紧张"，
   不是死磕标签;很多 Lyria 生成曲目会被 Gemini 判成 professional 但 energy 仍然很高，
   高能量的 professional/uplifting 曲目同样有"快"的听感。task.py 的 force_tense_bgm
   分支已切换成调用这个新函数。实测放宽后 tense∪energy>=4 池从 4 首扩到 8 首
   (tense=5, energy>=4=6，去重合并=8)，达到目标区间。
3. **转场视觉确认**:用 ffmpeg 截帧(10fps精细截取转场时间点附近)实际看了两处转场画面，
   确认是 whip_pan_transition(从底部/侧边快速滑入，0.2s内基本完成)，不是慢速 crossfade，
   视觉效果符合"快速紧张"预期。

## 验证中发现并修复的 bug:转场随机位置可能落在用不到的片段上
tense_indices 最初用 `range(1, len(subclipped_items))` 全量候选池(比如41个)做 random.sample，
但下面的消耗循环一旦音频时长被填满就会提前 break，实际只会用到其中一部分(比如14个)。如果转场
随机抽中的位置落在没被实际消耗的尾部，转场就静默失效——不报错，只是最终视频里看不到。
修复:按 `audio_duration / (max_clip_duration / clip_speed_factor)` 估算实际会用到的片段数，
候选范围收紧到这个估计值内。验证方式:观察单个 clip 的渲染耗时——quick_zoom/white_flash 等转场
比硬切多了一层 CompositeVideoClip/concatenate 合成，单 clip 耗时从~3s 涨到~19-20s，据此可以
反推转场是否真的在预期位置触发，不需要肉眼逐帧看视频。

## 素材复用策略(2026-06-18 更新,替代纯硬性"路径级"不复用)
- **段级别**(orchestrator.py):跨脚本段的素材文件路径硬性不重复——这条 M1 起就是硬约束,
  本次不变。
- **同段内部细分窗口**(video.py combine_videos):允许同一个源文件被多次用于填充音频时长,
  但每次必须是不同的时间片段(start_time/end_time 不同),不允许两次取完全相同的时间区间。
  实现方式:把"音频时长不够时 itertools.cycle 复制已渲染片段"的旧逻辑整个去掉,改成优先
  消耗 subclipped_items 池里还没用过的剩余时间窗口(同源不同段,画面不同);只有这些也耗尽时,
  才记录警告日志、让最终视频比配音短,**绝不**循环复制一个已经用过的完全相同片段。
- **Pexels 补足**:跨段维护 used_pexels_urls,同关键词的不同段不会撞到同一个 Pexels 结果
  (PexelsProvider.fetch 遍历搜索结果列表跳过已用 URL)。
- **极端情况**:脚本段数远超素材库覆盖范围、又没有更多 Pexels 候选可补时,不会静默拼出
  画面重复的视频——会在日志里明确提示"素材不足",让运营加素材或换关键词。
- 备注(用户原话保留):若后续要求"连同源不同片段都不许复用",快切节奏会受素材库规模直接
  限制(2.2s 一个切片,音频30s大概要十几个互不相同的片段),届时需要重新评估这条策略。

## 性能/成本提醒:快切 + 绝不复用 = Pexels 下载量显著上升
- 切片越短(2.2s vs 原来5s),同样音频时长需要的"不同素材"数量成倍增加;本地库覆盖不到的
  部分全部走 Pexels 实时搜索+下载,而且段级硬性不复用意味着不能像旧逻辑那样靠重复同一个
  下载结果蒙混过关。
- 批量出片(矩阵号场景)时,Pexels API 调用次数和下载带宽会随之上升,需要关注:
  ① Pexels API 速率限制(免费层有请求频率上限) ② 下载带宽/storage/cache_videos 磁盘占用
  ③ 单条视频生成耗时(更多素材意味着更多下载+更多 ffmpeg 切片调用)。
  建议批量铺量前,先用几条真实脚本实测平均每条视频的 Pexels 调用次数和总生成耗时,
  再估算批量上限,不要直接拿小规模测试的速度线性外推。

# 上线前功能升级(2026-06-17,非里程碑)

## TTS provider 化 + 英文化
- app/services/voice.py 的 tts() 调度本来就是按 voice_name 前缀分发(类似 MaterialProvider 模式),
  只是 Gemini TTS 实现是坏的:用了旧 SDK google.generativeai(本项目没装)+ pydub(Python 3.13
  缺 audioop 模块,import 就炸,M3 bgm_library.py 已经踩过同款坑)。
- 修复:gemini_tts() 改用 google.genai(新 SDK,M2/M3 打标+embedding 已经在用,架构统一)+
  ffmpeg subprocess 直转裸 PCM→mp3(复用 generate_silent_audio() 已验证的模式,不引入 pydub)。
  TTS 模型用 gemini_tts_model="gemini-3.1-flash-tts-preview"(实测可用,比 2.5-flash-preview-tts 新)。
- 新增 OpenAI TTS:is_openai_voice()/get_openai_voices()/openai_tts(),voice_name 前缀 "openai:"。
  OpenAI TTS 原生支持 speed 参数(0.25~4.0),是几个 provider 里唯一能真按 voice_rate 调速的;
  Gemini/Edge 走 legacy 整段时长比例字幕填充(populate_legacy_submaker_with_full_text,沿用项目已有逻辑)。
- **成本提醒**:OpenAI TTS(tts-1)约 $15 / 100万字符,一条 1-2 分钟文案(约1500-3000字符)只要几分钱;
  批量铺量(矩阵号场景)要按视频数 × 平均字符数估算总成本,别假设"单条很便宜=批量也可忽略"。
  ElevenLabs 贵很多(行业报价通常 5-10x),暂不接入。
- 顺手修了一个预先存在的 gap:task.py 的 generate_audio() 调 voice.tts() 时没传 voice_volume
  (虽然函数签名接受这个参数),已补上。
- A/B 测试:test_tts_ab.py,同一段英文外汇文案分别用 Gemini 几个候选 voice(Kore/Puck/Charon/Aoede)
  各出一条样本到 storage/tts_ab_samples/;OpenAI 候选(alloy/onyx/nova)等用户提供 openai_api_key 后补。
  没有 key 时脚本自动只出 Gemini 样本,不报错卡死。
- 语言切英文:不需要改代码——video_language="en" 时 build_script_prompt() 会在 prompt 里加
  "language: en" 提示行;字幕断句 utils.split_string_by_punctuations() 本来就同时处理中英文标点
  (含千分位逗号/小数点的特判),配音 voice_name 换成对应语言的 voice 即可。

## voice_rate 默认值
VideoParams/AudioRequest/SubtitleRequest 的 voice_rate 默认统一改成 1.2(原来 VideoParams 是 1.0,
和另外两个 Request 模型不一致;现在三处一致)。

## BGM 默认偏好:无明确情绪时倾向 tense+高energy
bgm_library.select_bgm():脚本情绪识别仍然生效(tense/uplifting/professional 各走自己的曲库)。
但 analyze_script_mood() 分类落到 "neutral"(没有明确情绪信号)时,产品默认改投 tense 池、
且 target_energy 拉到 tense 池里能量最高的那一档,而不是去挑库里同样标 neutral 的平淡曲子。
这是"默认偏好"不是"写死选择"——tense 池内仍然按 energy 就近 + random.choice 保持多样性。

## 素材硬性禁止复用(M1 软回退 → 硬约束)
- library.search_by_intent() 的 exclude_paths 原来是"prefer 排除,排除完没候选就回退到允许复用"的软逻辑;
  改成硬过滤(valid = [r for r in valid if r.path not in exclude_paths]),排除完没候选就是没候选,
  不会再悄悄把用过的素材塞回来。
- providers.py PexelsProvider.fetch() 新增 exclude_urls 参数:相近 intent 的不同段可能搜出同一个
  Pexels 热门结果,这里改成遍历结果列表跳过已用 URL,而不是永远只取 items[0]。
- orchestrator.orchestrate() 维护 used_pexels_urls 贯穿整条视频的 Pexels 填补循环;最后加了一道
  硬校验(5b 步):assemble 完的 shot 列表如果还有路径重复,直接 raise RuntimeError,不静默接受。
- task.py 的 get_video_materials() 捕获这个 RuntimeError → 任务标记 FAILED + 记录原因,不会降级
  回旧的随机下载逻辑(那条路径本身就是被取代的对象,会重新引入撞车问题)。
- 极端情况(脚本段数远超素材库覆盖范围 + Pexels 搜索词太窄)就是要报错,提醒运营加素材或换词,
  而不是产出一条素材重复、矩阵号特征明显的视频。

# 运营准备阶段(2026-06-17,非里程碑,上线前工具)
- 清理 M0 凑数素材:库里 vid-*(猫咪等,topic_fit=[])4 条记录+文件已删除,只保留 forex-* 真实外汇素材。
  判定规则:真实打标过的外汇素材 topic_fit 必然非空,M0 凑数素材从未与外汇相关,topic_fit=[] 即可识别。
- batch_ingest_library.py:批量导入真实素材库的工具。
  流程:sha256去重(按内容,不按文件名)→ library.ingest_file()质量过滤+复制进local_videos → 
  tagging.run_tagging()打标+embedding(失败自动重试,默认2次,5s/10s退避)。
  已用三条路径验证:① 内容重复(改名后)正确跳过 ② 低分辨率(100x100)正确拒绝且清理临时复制 
  ③ 全新素材正确入库+打标+embedding(实测：Gemini 把一段动画素材识别为 uplifting/finance,
  没有被"必须是真实拍摄画面"这类假设卡住,标注质量可信)。
  用法: python batch_ingest_library.py <真实素材目录>
- tagging.run_tagging() 新增 max_retries 参数(默认2),失败重试带退避,不再因单次API抖动判定为打标失败。

# M2 threshold 认知:7条小样本下的脆弱边界(M3后扩库时重新标定)
local_threshold=0.55 基于 7 条素材(3外汇+4对照)校准,safety_margin 仅 +0.0047
(forex_min=0.5438, other_max=0.5391)。这是小样本固有现象,边界很窄。
真实素材库扩到几十/上百条后,分布会变,0.55 不能假设长期成立——
扩库后必须用真实分布重新跑一遍 A/B 式的 min/max 校验,重新定 threshold。

# M1 匹配机制认知(不要在M2前改代码)
当前 search_by_intent 使用关键词重叠评分(matching_terms / total_terms × quality/10 + 外汇bonus)。
M1 实测分值区间 0.28~0.35,阈值 0.2 — 只要库里有素材就几乎必然命中,阈值形同虚设。
这是简单手工标注的预期局限:M1 本质是「库里有就匹配」,不是真语义相似度。
M2 接入 embedding 后使用真实余弦相似度;届时阈值需重新标定(预计 0.5~0.7 才有区分力)。
在此之前不要基于阈值做任何召回率假设。

# 贯穿原则
画面跟着文案且逐段对齐(改掉原项目随机拼接)/ 本地优先 / 能缓存的都缓存(打标按hash增量)/ Provider可插拔。
