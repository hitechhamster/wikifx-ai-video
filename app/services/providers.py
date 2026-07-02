"""
MaterialProvider abstraction.

Priority chain: LocalProvider → PexelsProvider → GeneratedProvider (stub).
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from app.config import config
from app.models.schema import VideoAspect


# 每段每遍最多评估(下载/判定)多少个候选;超过就放弃这遍。防止小池子里
# 一段疯狂 churn 几十个候选 × 每个一次 Gemini 判定 → 拖成几十分钟。
_MAX_SCREEN_PER_PASS = 10


@dataclass
class MaterialResult:
    path: str
    score: float       # 0.0–1.0; 0.0 = online provider (score not meaningful)
    source: str        # "local" | "local_forced" | "pexels" | "generated"
    metadata: dict = field(default_factory=dict)


class MaterialProvider(ABC):
    @abstractmethod
    def fetch(
        self,
        visual_intent: str,
        aspect: VideoAspect,
        min_duration: float,
    ) -> Optional[MaterialResult]:
        ...


class LocalProvider(MaterialProvider):
    def __init__(self, threshold: float = 0.3):
        self.threshold = threshold

    def fetch(
        self,
        visual_intent: str,
        aspect: VideoAspect,
        min_duration: float,
        exclude_paths: set = None,
    ) -> Optional[MaterialResult]:
        from app.services import library as lib

        results = lib.search_by_intent(
            visual_intent,
            top_k=5,
            min_duration=min_duration,
            exclude_paths=exclude_paths,
        )
        if not results:
            logger.debug("local: library is empty or no file found on disk")
            return None

        best_record, best_score = results[0]
        if best_score < self.threshold:
            logger.debug(
                f"local: best score {best_score:.3f} < threshold {self.threshold}"
            )
            return None

        logger.info(
            f"local match: score={best_score:.3f} "
            f"'{best_record.description[:40]}' → {best_record.path}"
        )
        return MaterialResult(
            path=best_record.path,
            score=best_score,
            source="local",
            metadata={"record_id": best_record.id},
        )


class PexelsProvider(MaterialProvider):
    """
    在线图库兜底 provider。名字保留为 PexelsProvider(向后兼容),但实际会聚合所有
    已配置 key 的免费图库源(Pexels/Pixabay/Coverr,见 material.search_videos_multi)。
    """

    def __init__(self, require_real_footage: bool = True):
        # require_real_footage:截一帧调 Gemini 判定是否为真实拍摄，拒绝动画/插画/
        # CG/motion-graphic 素材(详见 tagging.classify_real_footage 文档字符串)。
        # 每个候选多一次 Gemini 调用，会增加延迟和 API 成本，默认开启因为新闻播报
        # 场景必须保证画面严肃可信；不需要这层判定的调用方可以传 False 关掉。
        self.require_real_footage = require_real_footage

    def fetch(
        self,
        visual_intent: str,
        aspect: VideoAspect,
        min_duration: float,
        exclude_urls: set = None,
        cooldown_urls: set = None,
        topic: str = "",
    ) -> Optional[MaterialResult]:
        """
        exclude_urls: 本条视频内已用过的 URL —— 硬约束,绝不复用(相近 intent 跨段
            会抓到同一个 top 结果,所以逐个跳过已用项,而不是永远取 items[0])。
        cooldown_urls: 最近 N 条视频用过的 URL —— 软约束(跨视频冷却)。优先选不在
            冷却期的素材;只有当非冷却候选全部不可用时,才回退到冷却期素材,避免
            小池子把视频拖短/拖失败。
        topic: 视频主题,传给 classify_footage 做"真实 且 相关"判定,挡掉跟财经
            无关的素材(野生动物/乡村/体育等)。空则只判真假。

        全部候选都不可用时返回 None(调用方须视为"无法提供唯一素材",不可静默复用)。
        """
        from app.services.material import search_videos_multi, save_video, save_image
        from app.services.tagging import classify_footage

        items = search_videos_multi(
            search_term=visual_intent,
            minimum_duration=max(1, int(min_duration)),
            video_aspect=aspect,
        )
        if not items:
            logger.warning(f"stock: no results for '{visual_intent}'")
            return None

        exclude_urls = exclude_urls or set()
        cooldown_urls = cooldown_urls or set()

        def screen(item):
            """相关性/真假判定。能拿到缩略图就先判缩略图(跑题的不下载整段视频),
            通过了再下整段并直接采纳缩略图的结论;没有缩略图才回退到下整段再判。
            返回(通过?, 视频路径或None)。"""
            if not self.require_real_footage:
                p = save_video(item.url)
                return (bool(p), p)
            thumb = getattr(item, "thumbnail", "") or ""
            if thumb:
                tp = ""
                try:
                    tp = save_image(thumb)
                except Exception:
                    tp = ""
                if tp and not classify_footage(tp, topic=topic):
                    return (False, None)        # 缩略图就跑题/动画 → 不下整段
                p = save_video(item.url)         # 缩略图过了才下整段
                return (bool(p), p)
            # 无缩略图:只能下整段再判
            p = save_video(item.url)
            if not p:
                return (False, None)
            if not classify_footage(p, topic=topic):
                try: os.remove(p)
                except OSError: pass
                return (False, None)
            return (True, p)

        # 第一遍:跳过 硬排除 ∪ 冷却期。第二遍(回退):只跳过硬排除,允许动用冷却期素材。
        # 每遍最多评估 _MAX_SCREEN_PER_PASS 个候选,避免小池子里一段疯狂 churn 几十个
        # 候选(每个都要一次 Gemini 判定 → 拖成几十分钟)。
        for soft_skip in (cooldown_urls, set()):
            skip = exclude_urls | soft_skip
            screened = 0
            for item in items:
                if item.url in skip:
                    continue
                if screened >= _MAX_SCREEN_PER_PASS:
                    break
                screened += 1
                ok, path = screen(item)
                if not ok:
                    continue
                provider = getattr(item, "provider", "stock") or "stock"
                logger.info(f"{provider}: downloaded {path}")
                return MaterialResult(
                    path=path, score=0.0, source=provider, metadata={"url": item.url}
                )
            if soft_skip:
                logger.info(
                    f"stock: no fresh (non-cooldown) candidate for '{visual_intent}', "
                    f"relaxing cooldown and retrying"
                )

        logger.warning(
            f"stock: all candidates for '{visual_intent}' exhausted "
            f"(already used, or rejected by relevance/real-footage screen)"
        )
        return None


class ImageProvider(MaterialProvider):
    """
    Pixabay 静态图 → Ken Burns 运镜小片段 provider。

    用途:在以视频为主的时间线上,偶尔(orchestrator 控制频率)插一段由静态图渲成的
    缓慢运镜片段,丰富节奏、并借 Pixabay 庞大的图片库进一步扩大素材池、缓解重复。

    图片走 image_type=photo 只取真实照片,真假判定可省;但相关性判定仍需要(搜"person
    checking phone"也可能捞到跟财经无关的人像),所以渲染前先对图片做一次相关性筛查。
    输出是普通 mp4,下游 combine_videos 当常规片段处理。URL 同样参与跨视频冷却。
    """

    def fetch(
        self,
        visual_intent: str,
        aspect: VideoAspect,
        min_duration: float,
        exclude_urls: set = None,
        cooldown_urls: set = None,
        topic: str = "",
        strict: bool = None,
    ) -> Optional[MaterialResult]:
        from app.services.material import (
            search_images_pixabay,
            save_image,
            render_ken_burns_clip,
        )

        if not config.app.get("pixabay_api_keys"):
            return None  # 没配 Pixabay key,无图可用

        # visual_intent 是逗号分隔的多个画面词(如 "indian rupee notes, gold bullion bars,
        # phone with gold price")。逐个词搜、合并候选(去重保序),这样即使第一个词不是
        # 主题本体(rupee/phone),靠后的 "gold bullion" 词也能把黄金图捞进来——配合严格
        # 相关性筛掉非黄金、留下黄金图,保证每段都有黄金镜头。
        terms = [t.strip() for t in (visual_intent or "").split(",") if t.strip()] or [visual_intent]
        items = []
        seen = set()
        # 每个词最多取 _PER_TERM_CAP 个候选:否则第一个词(可能不是黄金,如 rupee)会
        # 返回几十张图全排在前面,严格筛逐张调 Gemini 拒掉、迟迟轮不到后面的黄金词,
        # 既慢又可能把黄金词挤出评估范围。限量后能跨词均匀取到黄金候选。
        _PER_TERM_CAP = 8
        for term in terms:
            taken = 0
            for it in search_images_pixabay(term, aspect):
                if it.url in seen:
                    continue
                seen.add(it.url)
                items.append(it)
                taken += 1
                if taken >= _PER_TERM_CAP:
                    break
        if not items:
            logger.warning(f"image: no results for '{visual_intent}'")
            return None

        exclude_urls = exclude_urls or set()
        cooldown_urls = cooldown_urls or set()
        clip_duration = max(3.0, float(min_duration) + 1.0)

        # 与视频源一致:第一遍跳过 硬排除∪冷却,第二遍只跳硬排除(放宽冷却兜底)
        for soft_skip in (cooldown_urls, set()):
            skip = exclude_urls | soft_skip
            for item in items:
                if item.url in skip:
                    continue
                img_path = save_image(item.url)
                if not img_path:
                    continue
                # 相关性筛查放在渲染前,跑题图直接换下一张,不白渲一遍 Ken Burns。
                # strict 可由调用方覆盖:强制插的"黄金图"必须严格判定(挡掉书架/城市
                # 这类被宽松判定放行的非黄金图);否则跟全局开关。
                if topic:
                    from app.services.tagging import classify_footage
                    if not classify_footage(img_path, topic=topic, strict=strict):
                        continue
                clip = render_ken_burns_clip(img_path, clip_duration, aspect)
                if not clip:
                    continue
                logger.info(f"pixabay_image: ken-burns clip from {item.url[:60]}")
                return MaterialResult(
                    path=clip, score=0.0, source="pixabay_image",
                    metadata={"url": item.url},
                )
            if soft_skip:
                logger.info(
                    f"image: no fresh (non-cooldown) photo for '{visual_intent}', "
                    f"relaxing cooldown"
                )
        return None


class GeneratedProvider(MaterialProvider):
    def fetch(
        self,
        visual_intent: str,
        aspect: VideoAspect,
        min_duration: float,
    ) -> Optional[MaterialResult]:
        raise NotImplementedError("video generation not enabled in this version")
