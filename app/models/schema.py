import warnings
from enum import Enum
from typing import Any, List, Optional, Union

import pydantic
from pydantic import BaseModel, Field

from app.config import config

# 忽略 Pydantic 的特定警告
warnings.filterwarnings(
    "ignore",
    category=UserWarning,
    message="Field name.*shadows an attribute in parent.*",
)


class VideoConcatMode(str, Enum):
    random = "random"
    sequential = "sequential"


class VideoTransitionMode(str, Enum):
    none = None
    shuffle = "Shuffle"
    fade_in = "FadeIn"
    fade_out = "FadeOut"
    slide_in = "SlideIn"
    slide_out = "SlideOut"
    # "快速紧张"风格:全程硬切，只在 tense_transition_count 个随机位置插入
    # quick zoom / whip pan / white flash 之一，时长 0.15~0.25s，不是常规 crossfade。
    tense = "Tense"


class VideoAspect(str, Enum):
    landscape = "16:9"
    portrait = "9:16"
    square = "1:1"

    def to_resolution(self):
        if self == VideoAspect.landscape:
            return 1920, 1080
        elif self == VideoAspect.portrait:
            return 1080, 1920
        elif self == VideoAspect.square:
            return 1080, 1080
        raise ValueError(f"unsupported video aspect: {self}")


class _Config:
    arbitrary_types_allowed = True


@pydantic.dataclasses.dataclass(config=_Config)
class MaterialInfo:
    provider: str = "pexels"
    url: str = ""
    duration: int = 0
    thumbnail: str = ""   # 搜索接口返回的预览缩略图;用于"先看缩略图判相关性,跑题的不下载整段"


class VideoParams(BaseModel):
    """
    {
      "video_subject": "",
      "video_aspect": "横屏 16:9（西瓜视频）",
      "voice_name": "女生-晓晓",
      "bgm_name": "random",
      "font_name": "STHeitiMedium 黑体-中",
      "text_color": "#FFFFFF",
      "font_size": 60,
      "stroke_color": "#000000",
      "stroke_width": 1.5
    }
    """

    video_subject: str
    video_script: str = ""  # Script used to generate the video
    video_terms: Optional[str | list] = None  # Keywords used to generate the video
    video_aspect: Optional[VideoAspect] = VideoAspect.portrait.value
    video_concat_mode: Optional[VideoConcatMode] = VideoConcatMode.random.value
    video_transition_mode: Optional[VideoTransitionMode] = None
    video_clip_duration: Optional[float] = 3.0  # 片段播出时长上限(秒)；下限见 min_clip_duration，区间内随机
    match_materials_to_script: bool = False
    video_count: Optional[int] = 1

    video_source: Optional[str] = "pexels"
    video_materials: Optional[List[MaterialInfo]] = (
        None  # Materials used to generate the video
    )
    
    custom_audio_file: Optional[str] = None  # Custom audio file path, will ignore video_script and disable subtitle
    video_language: Optional[str] = ""  # auto detect

    voice_name: Optional[str] = ""
    voice_volume: Optional[float] = 1.0
    voice_rate: Optional[float] = 1.2
    bgm_type: Optional[str] = "random"
    bgm_file: Optional[str] = ""
    bgm_volume: Optional[float] = 0.2

    subtitle_enabled: Optional[bool] = True
    subtitle_position: Optional[str] = config.ui.get("subtitle_position", "bottom")  # top, bottom, center, custom
    custom_position: float = config.ui.get("custom_position", 70.0)
    font_name: Optional[str] = "STHeitiMedium.ttc"
    text_fore_color: Optional[str] = "#FFFFFF"
    text_background_color: Union[bool, str] = True
    rounded_subtitle_background: bool = False

    font_size: int = 60
    stroke_color: Optional[str] = "#000000"
    stroke_width: float = 1.5
    n_threads: Optional[int] = 2
    paragraph_number: int = Field(default=1, ge=1, le=10)
    video_script_prompt: str = Field(default="", max_length=2000)
    custom_system_prompt: str = Field(default="", max_length=8000)

    # Orchestrator fields (M1+)
    use_orchestrator: bool = False
    local_threshold: float = 0.3    # min local-match score to prefer local over Pexels
    min_local_segments: int = 2     # hard minimum number of local shots per video

    # Mood-driven BGM selection (M3)
    use_mood_bgm: bool = False      # if True, pick bgm_file by script mood instead of random

    # "快速紧张" 剪辑风格(2026-06-18)
    clip_speed_factor: float = 1.3       # 1.2~1.5x，单个素材片段加速倍数(只影响画面，B-roll本身无声)
    tense_transition_count: int = 2      # 整段视频里允许出现"快速紧张型"转场的次数(其余全部硬切)
    force_tense_bgm: bool = False        # True 时跳过脚本情绪识别，一律从 tense+高energy 池选曲

    # 片段时长随机化(2026-06-22 第八轮)：避免每段都一样长、节奏单调
    random_clip_duration: bool = True    # True 时每段在 [min_clip_duration, video_clip_duration] 播出时长内随机取
    min_clip_duration: float = 2.0       # 片段播出时长下限(秒)，已含加速因素，保证每段至少看到这么久

    # BGM 试听 + 手动指定(2026-06-18 第三轮)
    bgm_mode: str = "auto"               # "auto"(自动选曲) | "manual"(用户在前端直接选曲，覆盖自动逻辑)
    bgm_max_energy: int = 5              # 自动选曲池的 energy 上限(1-5)，覆盖最吵的那批时调成 3~4

    # 财经突发新闻包装(news_mode，2026-06-18 第四轮)
    news_mode: bool = False              # 总开关:lower-third标题条+底部ticker+角标+新闻字幕样式+主播腔
    news_badge_text: str = "FOREX MARKET NEWS"  # 角标文字

    # 系列化产出去重(2026-06-22 第六轮)：缓解不同视频反复撞同一批在线图库素材
    material_cooldown_videos: int = 8    # 跨视频冷却窗口:在线图库素材在最近 N 条视频内不重复(0=关闭)；池子小可调小，要更"永不重复"可调大
    diversify_broll: bool = True         # 搜索词多样化:产出泛财经b-roll画面词而非把主题词硬塞进每个词，扩大同源可用池
    image_insert_every: int = 5          # 每 N 个在线镜头插一段"静态图+Ken Burns运镜"片段丰富节奏(0=关闭)；只替换在线视频镜头，不动本地素材

    # 按句子对齐(2026-06-22 第十轮)：每句配一个对应画面、对齐到那句话的时间窗、按顺序播放
    align_clips_to_script: bool = True   # True=一句一镜语义对齐(华盛顿那句出现华盛顿)；False=回到快切混填(打乱+多抓素材填充)
    strict_topic_relevance: bool = False  # True=相关性判定收紧:必须画面里清楚出现主题本体(如黄金题材要真出现金条/金币/金饰),否则拒;泛财经(酒店/他国钞票)一律拒。视觉很具体的题材(黄金)用,常规外汇新闻保持 False
    image_query: str = ""                 # 非空时:静态图插槽(image_insert_every)改用这个固定搜索词(逗号分隔多词),与逐段视频搜索词解耦。用于"视频走通用市场/城市素材、但强制插1张特定题材图(如黄金)"的场景
    video_query_pool: str = ""            # 非空时:所有视频段从这个通用词库(逗号分隔)循环取搜索词,不跟逐段句子走。用于"句子题材在免费库没视频(如黄金),但通用市场/城市素材随便填"——视频走市场/城市,题材镜头另由 image_query 插图


class SubtitleRequest(BaseModel):
    video_script: str
    video_language: Optional[str] = ""
    voice_name: Optional[str] = "zh-CN-XiaoxiaoNeural-Female"
    voice_volume: Optional[float] = 1.0
    voice_rate: Optional[float] = 1.2
    bgm_type: Optional[str] = "random"
    bgm_file: Optional[str] = ""
    bgm_volume: Optional[float] = 0.2
    subtitle_position: Optional[str] = config.ui.get("subtitle_position", "bottom")
    font_name: Optional[str] = "STHeitiMedium.ttc"
    text_fore_color: Optional[str] = "#FFFFFF"
    text_background_color: Union[bool, str] = True
    rounded_subtitle_background: bool = False
    font_size: int = 60
    stroke_color: Optional[str] = "#000000"
    stroke_width: float = 1.5
    video_source: Optional[str] = "local"
    subtitle_enabled: Optional[str] = "true"


class AudioRequest(BaseModel):
    video_script: str
    video_language: Optional[str] = ""
    voice_name: Optional[str] = "zh-CN-XiaoxiaoNeural-Female"
    voice_volume: Optional[float] = 1.0
    voice_rate: Optional[float] = 1.2
    bgm_type: Optional[str] = "random"
    bgm_file: Optional[str] = ""
    bgm_volume: Optional[float] = 0.2
    video_source: Optional[str] = "local"


class VideoScriptParams:
    """
    {
      "video_subject": "春天的花海",
      "video_language": "",
      "paragraph_number": 1,
      "video_script_prompt": "",
      "custom_system_prompt": ""
    }
    """

    video_subject: Optional[str] = "春天的花海"
    video_language: Optional[str] = ""
    paragraph_number: int = Field(default=1, ge=1, le=10)
    video_script_prompt: str = Field(default="", max_length=2000)
    custom_system_prompt: str = Field(default="", max_length=8000)


class VideoTermsParams:
    """
    {
      "video_subject": "",
      "video_script": "",
      "amount": 5
    }
    """

    video_subject: Optional[str] = "春天的花海"
    video_script: Optional[str] = (
        "春天的花海，如诗如画般展现在眼前。万物复苏的季节里，大地披上了一袭绚丽多彩的盛装。金黄的迎春、粉嫩的樱花、洁白的梨花、艳丽的郁金香……"
    )
    amount: Optional[int] = 5


class VideoSocialMetadataParams:
    """
    {
      "video_subject": "A day in Shanghai",
      "video_script": "",
      "language": "auto",
      "platform": "tiktok"
    }
    """

    video_subject: Optional[str] = Field(default="A day in Shanghai", max_length=500)
    video_script: Optional[str] = Field(default="", max_length=8000)
    language: Optional[str] = Field(default="auto", max_length=64)
    platform: Optional[str] = Field(default="tiktok", max_length=64)


class BaseResponse(BaseModel):
    status: int = 200
    message: Optional[str] = "success"
    data: Any = None


class TaskVideoRequest(VideoParams, BaseModel):
    pass


class TaskQueryRequest(BaseModel):
    pass


class VideoScriptRequest(VideoScriptParams, BaseModel):
    pass


class VideoTermsRequest(VideoTermsParams, BaseModel):
    pass


class VideoSocialMetadataRequest(VideoSocialMetadataParams, BaseModel):
    pass


######################################################################################################
######################################################################################################
######################################################################################################
######################################################################################################
class TaskResponse(BaseResponse):
    class TaskResponseData(BaseModel):
        task_id: str

    data: TaskResponseData

    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {"task_id": "6c85c8cc-a77a-42b9-bc30-947815aa0558"},
            },
        }


class TaskQueryResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "state": 1,
                    "progress": 100,
                    "videos": [
                        "http://127.0.0.1:8080/tasks/6c85c8cc-a77a-42b9-bc30-947815aa0558/final-1.mp4"
                    ],
                    "combined_videos": [
                        "http://127.0.0.1:8080/tasks/6c85c8cc-a77a-42b9-bc30-947815aa0558/combined-1.mp4"
                    ],
                },
            },
        }


class TaskDeletionResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "state": 1,
                    "progress": 100,
                    "videos": [
                        "http://127.0.0.1:8080/tasks/6c85c8cc-a77a-42b9-bc30-947815aa0558/final-1.mp4"
                    ],
                    "combined_videos": [
                        "http://127.0.0.1:8080/tasks/6c85c8cc-a77a-42b9-bc30-947815aa0558/combined-1.mp4"
                    ],
                },
            },
        }


class VideoScriptResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "video_script": "春天的花海，是大自然的一幅美丽画卷。在这个季节里，大地复苏，万物生长，花朵争相绽放，形成了一片五彩斑斓的花海..."
                },
            },
        }


class VideoTermsResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {"video_terms": ["sky", "tree"]},
            },
        }


class VideoSocialMetadataResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "title": "A Day in Shanghai You Should Not Miss",
                    "caption": "Save this quick Shanghai inspiration and follow for more short travel ideas.",
                    "hashtags": ["#shorts", "#travel", "#shanghai", "#viral", "#fyp"],
                },
            },
        }


class BgmRetrieveResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "files": [
                        {
                            "name": "output013.mp3",
                            "size": 1891269,
                            "file": "/MoneyPrinterTurbo/resource/songs/output013.mp3",
                        }
                    ]
                },
            },
        }


class BgmUploadResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {"file": "/MoneyPrinterTurbo/resource/songs/example.mp3"},
            },
        }

class VideoMaterialRetrieveResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "files": [
                        {
                            "name": "example.mp4",
                            "size": 12345678,
                            "file": "/MoneyPrinterTurbo/resource/videos/example.mp4",
                        }
                    ]
                },
            },
        }

class VideoMaterialUploadResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "file": "/MoneyPrinterTurbo/resource/videos/example.mp4",
                },
            },
        }


# ---------------------------------------------------------------------------
# M4: material library + BGM library API models
# ---------------------------------------------------------------------------

class MaterialListResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "materials": [
                        {
                            "id": 1,
                            "path": "storage/local_videos/forex-1176d919b7d69e05.mp4",
                            "duration": 7.5,
                            "width": 1080,
                            "height": 1920,
                            "aspect": "9:16",
                            "description": "A hand moves a magnifying glass across financial trading charts.",
                            "tags": ["magnifying glass", "financial charts", "trading analysis"],
                            "topic_fit": ["forex", "trading", "chart"],
                            "mood": "professional",
                            "quality": 8.0,
                            "has_watermark": False,
                            "has_embedding": True,
                        }
                    ],
                    "total": 1,
                },
            },
        }


class TaggingTriggerResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {"tagged": 3, "skipped": 4, "failed": 0},
            },
        }


class SongListResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "songs": [
                        {
                            "id": 1,
                            "path": "resource/songs/output000.mp3",
                            "duration": 64.2,
                            "mood": "professional",
                            "energy": 1,
                            "description": "A calm and minimalist piano track with soft, atmospheric synth pads.",
                        }
                    ],
                    "total": 1,
                },
            },
        }


# ---------------------------------------------------------------------------
# M4b: orchestration preview (shows the plan before generation/Pexels calls)
# ---------------------------------------------------------------------------

class OrchestratorPreviewRequest(BaseModel):
    video_subject: str = ""
    video_script: str
    video_aspect: Optional[VideoAspect] = VideoAspect.portrait
    video_clip_duration: Optional[float] = 2.2
    local_threshold: float = 0.3
    min_local_segments: int = 2


class OrchestratorPreviewResponse(BaseResponse):
    class Config:
        json_schema_extra = {
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "shots": [
                        {
                            "segment_index": 0,
                            "segment_text": "美元兑人民币汇率近期持续走强。",
                            "visual_intent": "US dollar forex trend, exchange rate chart",
                            "source": "local",
                            "material_id": 5,
                            "material_filename": "forex-1176d919b7d69e05.mp4",
                            "score": 0.613,
                        },
                        {
                            "segment_index": 1,
                            "segment_text": "交易员建议关注美联储政策动向。",
                            "visual_intent": "Federal Reserve policy, forex trading",
                            "source": "pexels_preview",
                            "material_id": None,
                            "material_filename": None,
                            "score": 0.0,
                        },
                    ],
                    "total_segments": 2,
                    "local_count": 1,
                    "pexels_count": 1,
                },
            },
        }
