"""astrbot_plugin_media_montage_429

AstrBot 4.29+ 专版的"视频抽帧拼图"插件。

一、与通用版的区别
    - **零注入**：不包装、不替换 AstrBot 核心的任何函数，只用官方公开的
      `on_llm_request` 钩子与 `req` 实体，随插件卸载即完全还原。
    - **剔除旧版功能**：通用版里为 4.28.1/4.28.2 补的"动图拼图提示"、
      版本门控探测、消息链兜底解析等基于旧核心的逻辑全部移除——
      4.29+ 的官方修复已接管动图提示，插件不再重复。

二、本版只做一件事：视频抽帧拼图
    消息里的视频在核心里只是一行文本 "[Video Attachment: name X, path Y]"，
    模型看不到画面。本插件解析核心在 on_llm_request 钩子**之前**就已写好的
    官方视频标签（astr_main_agent._append_video_attachment 的产物），直接
    复用核心的解析结果：用 ffmpeg 对视频等间隔抽帧，套用与官方动图完全
    相同的 3x3 拼图逻辑合成一张图片，追加进 req.image_urls，并按官方
    montage notice 的风格附加 <system_notice> 提示（临时内容，不落库）。
    钩子之后核心的 prepare_request_images 会统一完成图片准备与标注。

设计要点
--------
1. 只信官方标签：视频来源唯一途径是 req.extra_user_content_parts 里的
   "[Video Attachment: ...]" / "[Video Attachment in quoted message: ...]"。
   标签不存在就什么都不做（例如引用视频落地失败时核心只保留 ref）。
2. 版本要求：加载时校验 AstrBot >= 4.29.0，不满足则拒绝启用并给出明确日志。
3. 失败绝不打断主流程：抽帧、合成、探测全部包在 try/except 中，
   任何一步失败都只是退化为原有行为（视频对模型不可见）。
4. OneBot11 语音转文字（v1.1.0 自 astrbot_plugin_mimo_media 移植）：
   audio_mode=llonebot_stt 时，把消息里（含直接发送与引用消息）的语音通过
   OneBot11 的 voice_msg_to_text（llonebot 5.1+ 扩展接口）转成文字注入请求，
   同时清空 req.audio_urls 并移除核心生成的 [Audio Attachment] 标签。
   该逻辑仅在 OneBot11（aiocqhttp）适配器事件下触发；其他适配器或
   audio_mode=off 时完全不介入，保持 AstrBot 原有音频行为。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 配置类型（>= 4.x 均有）
    from astrbot.api import AstrBotConfig
except Exception:  # pragma: no cover - 仅作类型注解降级
    AstrBotConfig = dict  # type: ignore[misc,assignment]

try:  # 语音转文字所需的核心结构（4.29+ 均有；缺失时仅停用该功能）
    from astrbot.core.agent.message import TextPart
    from astrbot.core.message.components import (
        Forward,
        Image,
        Node,
        Nodes,
        Record,
        Reply,
        Video,
    )
    from astrbot.core.utils.quoted_message.onebot_client import OneBotClient

    _STT_IMPORT_OK = True
except Exception:  # pragma: no cover - 核心结构变动时不影响视频抽帧拼图
    TextPart = None  # type: ignore[assignment,misc]
    Forward = Image = Node = Nodes = Record = Reply = Video = None  # type: ignore[assignment,misc]
    OneBotClient = None  # type: ignore[assignment,misc]
    _STT_IMPORT_OK = False

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PLUGIN_NAME = "astrbot_plugin_media_montage_429"

MIN_ASTRBOT_VERSION = (4, 29, 0)
"""本专版要求的最低 AstrBot 版本。"""

DEFAULT_VIDEO_NOTICE = (
    "The input includes a video converted into a single image with frames "
    "in reading order. Treat this image as a sequence of video frames; "
    "do not mention the conversion or frame layout."
)

# --- OneBot11 语音转文字（自 astrbot_plugin_mimo_media 移植） -----------------
ONEBOT11_PLATFORM_NAME = "aiocqhttp"
"""AstrBot 中 OneBot11 协议适配器的平台名（见官方开发文档
``event.get_platform_name() == "aiocqhttp"`` 判定）。"""

DEFAULT_AUDIO_MODE = "off"
LLONEBOT_STT_AUDIO_MODE = "llonebot_stt"
MAX_COMPONENT_DEPTH = 8
"""消息链（Reply 链 / 合并转发节点）递归遍历的最大深度，与 mimo_media 一致。"""

# 4.29+ 核心生成的语音附件占位标签前缀
# （astr_main_agent._append_audio_attachment / _append_quoted_audio_attachment）。
# 语音已被转写成文字后需要移除，避免模型继续把语音当作附件。
_AUDIO_PLACEHOLDER_PREFIXES = (
    "[Audio Attachment:",
    "[Audio Attachment in quoted message:",
)

# 4.29+ 核心在 on_llm_request 钩子之前就把视频解析成这种官方标签
# （astr_main_agent._append_video_attachment）。插件直接复用其解析结果。
_OFFICIAL_VIDEO_LABEL_RE = re.compile(
    r"^\[Video Attachment(?: in quoted message)?: "
    r"name (?P<name>.+?), path (?P<path>.+)\]$"
)


# ---------------------------------------------------------------------------
# 版本 & 路径工具
# ---------------------------------------------------------------------------
def _get_astrbot_version() -> str:
    """尽力获取当前 AstrBot 版本号字符串。"""
    try:
        from astrbot.core.config.default import VERSION

        if VERSION:
            return str(VERSION)
    except Exception:
        pass
    try:
        import astrbot

        return str(getattr(astrbot, "__version__", "") or "")
    except Exception:
        pass
    return ""


def _version_tuple(version: str) -> tuple[int, int, int]:
    """把 "4.29.0" / "v4.29.1-beta.1" 这类字符串解析成 (4, 29, 0)。"""
    nums = re.findall(r"\d+", version or "")
    parts = [int(n) for n in nums[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _temp_output_path(suffix: str) -> str:
    """在 AstrBot 临时目录下创建一个空文件并返回其路径。"""
    base: Path
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        base = Path(get_astrbot_temp_path())
    except Exception:
        base = Path(tempfile.gettempdir()) / "astrbot"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(tempfile.gettempdir())
    fd, name = tempfile.mkstemp(prefix="media_montage_", suffix=suffix, dir=str(base))
    os.close(fd)
    return name


def _is_temp_path(path: str) -> bool:
    """判断路径是否位于 AstrBot 临时目录内（与 mimo_media 实现一致）。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        temp_dir = os.path.realpath(get_astrbot_temp_path())
        return os.path.realpath(path).startswith(temp_dir + os.sep)
    except Exception:
        return False


def _cleanup_paths(paths: list[str]) -> None:
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[media_montage_429] 清理临时文件失败 %s: %s", path, exc
            )


# ---------------------------------------------------------------------------
# 拼图合成（与核心 _extract_animation_montage_sync 同构）
# ---------------------------------------------------------------------------
def _compose_montage(
    frame_bytes: list[bytes], grid: int, max_size: int, quality: int
) -> bytes | None:
    """把若干帧按 grid x grid 拼成一张白底静图，帧序即阅读顺序。

    Args:
        frame_bytes: 已编码的帧（按时间顺序）。
        grid: 网格边长（动图逻辑固定为 3）。
        max_size: 拼图最长边上限，仅缩小不放大。
        quality: JPEG 输出质量 1-100。

    Returns:
        JPEG 字节；没有可用帧时返回 None。
    """
    from PIL import Image as PILImage
    from PIL import ImageOps

    if not frame_bytes:
        return None

    grid = max(1, int(grid))
    capacity = grid * grid
    frames = frame_bytes[:capacity]

    decoded: list[PILImage.Image] = []
    try:
        for raw in frames:
            with PILImage.open(io.BytesIO(raw)) as src:
                src.load()
                oriented = ImageOps.exif_transpose(src)
            decoded.append(oriented)
        if not decoded:
            return None

        width, height = decoded[0].size
        # 与核心一致：按"单格最长边 * 网格数"作为整体最长边来算缩放比，
        # 向下取整，保证拼图整体不会超过 max_size。
        longest_edge = max(width, height, 1) * grid
        scale = min(1.0, max(int(max_size), 1) / longest_edge)
        cell = (max(1, int(width * scale)), max(1, int(height * scale)))

        canvas = PILImage.new(
            "RGB", (cell[0] * grid, cell[1] * grid), (255, 255, 255)
        )
        try:
            for index, image in enumerate(decoded):
                frame = image.convert("RGBA")
                try:
                    resized = frame
                    if frame.size != cell:
                        resized = frame.resize(cell, PILImage.Resampling.LANCZOS)
                    try:
                        canvas.paste(
                            resized,
                            ((index % grid) * cell[0], (index // grid) * cell[1]),
                            resized,  # 用自身做蒙版，透明像素透出白底
                        )
                    finally:
                        if resized is not frame:
                            resized.close()
                finally:
                    frame.close()

            buffer = io.BytesIO()
            canvas.save(buffer, "JPEG", quality=int(quality))
            return buffer.getvalue()
        finally:
            canvas.close()
    finally:
        for image in decoded:
            try:
                image.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 插件
# ---------------------------------------------------------------------------
@register(
    PLUGIN_NAME,
    "WorkBuddy",
    "AstrBot 4.29+ 专版：让视频以抽帧拼图的方式被模型看懂；"
    "OneBot11 消息语音可转写成文字（零注入，只用官方钩子）。",
    "1.1.0",
)
class MediaMontagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

        # --- 配置 ---
        self.enable_video_montage: bool = _as_bool(
            self.config.get("enable_video_montage", True)
        )
        self.enable_video_notice: bool = _as_bool(
            self.config.get("enable_video_notice", True)
        )
        self.video_notice_text: str = str(
            self.config.get("video_notice_text") or DEFAULT_VIDEO_NOTICE
        )
        self.video_grid: int = _as_int(self.config.get("video_grid", 3), 3, 1, 8)
        auto_frames = self.video_grid * self.video_grid
        self.video_frames: int = _as_int(
            self.config.get("video_frames", 0), 0, 0, auto_frames
        )
        if self.video_frames <= 0:
            self.video_frames = auto_frames
        self.video_max_size: int = _as_int(
            self.config.get("video_max_size", 1280), 1280, 16, 100000
        )
        self.video_quality: int = _as_int(
            self.config.get("video_quality", 95), 95, 1, 100
        )
        self.video_timeout: int = _as_int(
            self.config.get("video_extract_timeout", 30), 30, 1, 600
        )

        # --- OneBot11 语音转文字（自 astrbot_plugin_mimo_media 移植） ---
        self.stt_ready = _STT_IMPORT_OK
        self.audio_mode: str = self._normalize_audio_mode(
            self.config.get("audio_mode", DEFAULT_AUDIO_MODE)
        )
        if self.audio_mode == LLONEBOT_STT_AUDIO_MODE and not self.stt_ready:
            logger.warning(
                "[media_montage_429] audio_mode=llonebot_stt 但当前核心缺少所需"
                "结构（TextPart/消息组件/OneBotClient），语音转文字已停用。"
            )

        # --- 版本要求 ---
        self.version = _get_astrbot_version()
        self.version_tuple = _version_tuple(self.version)
        self.version_ok = self.version_tuple >= MIN_ASTRBOT_VERSION

        if not self.version_ok:
            logger.warning(
                "[media_montage_429] 需要 AstrBot >= %s，当前为 %s，插件保持停用。",
                ".".join(map(str, MIN_ASTRBOT_VERSION)),
                self.version or "未知",
            )

        logger.info(
            "[media_montage_429] loaded. astrbot=%s, version_ok=%s, "
            "video_montage=%s (grid=%dx%d, frames=%d), audio_mode=%s",
            self.version or "unknown",
            self.version_ok,
            self.enable_video_montage,
            self.video_grid,
            self.video_grid,
            self.video_frames,
            self.audio_mode,
        )

    @staticmethod
    def _normalize_audio_mode(raw: object) -> str:
        """归一化 audio_mode：off / llonebot_stt，非法值回退 off（不动语音）。"""
        mode = str(raw or "").strip().lower()
        if mode not in (DEFAULT_AUDIO_MODE, LLONEBOT_STT_AUDIO_MODE):
            if str(raw or "").strip():
                logger.warning(
                    "[media_montage_429] 无效的 audio_mode=%r，保持不处理语音",
                    raw,
                )
            return DEFAULT_AUDIO_MODE
        return mode

    async def terminate(self) -> None:
        # 零注入：没有任何核心函数包装需要还原。
        return

    # ------------------------------------------------------------------
    # LLM 请求钩子
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """把本轮请求里的视频转成抽帧拼图，并按需执行 OneBot11 语音转文字。"""
        if not self.version_ok:
            return

        # OneBot11 语音转文字：仅 aiocqhttp 平台 + llonebot_stt 模式触发；
        # 其他适配器不进入此分支，保持 AstrBot 原有音频行为。
        if (
            self.audio_mode == LLONEBOT_STT_AUDIO_MODE
            and self.stt_ready
            and self._is_onebot11(event)
        ):
            try:
                await self._handle_llonebot_stt(event, req)
            except Exception as exc:
                logger.warning(
                    "[media_montage_429] llonebot 语音转文字处理失败: %s",
                    exc,
                    exc_info=True,
                )

        if not self.enable_video_montage:
            return
        try:
            converted = await self._attach_video_montages(req, event)
        except Exception as exc:
            logger.warning(
                "[media_montage_429] video montage failed: %s", exc, exc_info=True
            )
            return
        if converted and self.enable_video_notice:
            self._append_notice(req, self._video_notice_body(converted))

    def _video_notice_body(self, converted_names: list[str]) -> str:
        """视频提示正文：固定文案 + 官方标签里的视频名（便于模型对应）。"""
        body = self.video_notice_text
        names = [name for name in converted_names if name]
        if names:
            body += "\nConverted videos: " + ", ".join(names) + "."
        return body

    def _append_notice(self, req, body: str) -> None:
        """把提示追加到 req.extra_user_content_parts，并标记为临时内容。"""
        try:
            from astrbot.core.agent.message import TextPart
        except Exception:
            logger.warning("[media_montage_429] 无法构造 TextPart，跳过提示。")
            return

        text = "<system_notice>\n" + body + "\n</system_notice>"
        try:
            part = TextPart(text=text)
        except Exception:
            logger.warning("[media_montage_429] 构造提示失败。")
            return

        marker = getattr(part, "mark_as_temp", None)
        if callable(marker):
            try:
                part = marker() or part
            except Exception:
                pass

        extra = getattr(req, "extra_user_content_parts", None)
        if not isinstance(extra, list):
            return
        # 核心在钩子之后还会再跑一次图片准备，它会按原顺序保留非图片内容块，
        # 所以这里直接追加即可。
        extra.append(part)

    # ------------------------------------------------------------------
    # OneBot11 语音转文字（自 astrbot_plugin_mimo_media 移植）
    # ------------------------------------------------------------------
    @staticmethod
    def _is_onebot11(event: AstrMessageEvent) -> bool:
        """仅在 OneBot11（aiocqhttp）适配器的事件下返回 True。"""
        try:
            return event.get_platform_name() == ONEBOT11_PLATFORM_NAME
        except Exception:
            return False

    async def _handle_llonebot_stt(self, event: AstrMessageEvent, req) -> None:
        """llonebot_stt 模式：语音经 voice_msg_to_text 转成文字，不再发送音频。

        与 astrbot_plugin_mimo_media 的 llonebot 分支保持同一行为与参数结构：
        清空 req.audio_urls，把转写文字注入 extra_user_content_parts（引用语音
        替换 <Quoted Message> 里的 [Empty Text]），移除核心生成的
        [Audio Attachment] 标签，并清理由核心预先落地的临时音频文件。
        合并转发内部的语音不解析（与 mimo_media 一致）。
        """
        await self._resolve_remote_audio(event)
        audio_targets = self._collect_audio_targets(event)
        request_audio_refs = list(getattr(req, "audio_urls", None) or [])
        notes: list[str] = []
        transcriptions: list[tuple[str, bool]] = []

        req.audio_urls = []
        if audio_targets:
            transcriptions = await self._transcribe_with_llonebot(
                event,
                audio_targets,
            )
            self._inject_llonebot_transcriptions(req, transcriptions)
            if not transcriptions:
                notes.append("[语音转文字失败，已跳过]")
        elif request_audio_refs:
            notes.append("[语音转文字失败：无法获取 OneBot 消息 ID]")

        if not transcriptions and not notes:
            return

        self._drop_audio_placeholders(req)
        for note in notes:
            req.extra_user_content_parts.append(TextPart(text=note))

        # 语音已替换为文字，核心预先落地的音频文件不再被使用，清理临时目录内的残留。
        _cleanup_paths([path for path in request_audio_refs if _is_temp_path(path)])

    @classmethod
    def _walk_components(
        cls,
        components: list[Any] | None,
        *,
        reply_id: str | int | None = None,
        in_forward: bool = False,
        depth: int = 0,
    ) -> Iterator[tuple[Any, str | int | None, bool]]:
        """递归遍历 Reply 链与合并转发节点，产出 (组件, reply_id, in_forward)。"""
        if not isinstance(components, list) or depth > MAX_COMPONENT_DEPTH:
            return
        for component in components:
            yield component, reply_id, in_forward
            if isinstance(component, Reply) and component.chain:
                yield from cls._walk_components(
                    component.chain,
                    reply_id=component.id,
                    in_forward=in_forward,
                    depth=depth + 1,
                )
            elif isinstance(component, Node):
                yield from cls._walk_components(
                    component.content,
                    reply_id=reply_id,
                    in_forward=True,
                    depth=depth + 1,
                )
            elif isinstance(component, Nodes):
                for node in component.nodes:
                    yield node, reply_id, True
                    yield from cls._walk_components(
                        node.content,
                        reply_id=reply_id,
                        in_forward=True,
                        depth=depth + 1,
                    )

    @classmethod
    def _component_entries(
        cls, event: AstrMessageEvent
    ) -> list[tuple[Any, str | int | None, bool]]:
        """本地消息链 + 引用消息远程解析结果（见 _resolve_remote_audio）。"""
        message_obj = getattr(event, "message_obj", None)
        entries = list(cls._walk_components(getattr(message_obj, "message", []) or []))
        entries.extend(event.get_extra("media_montage_stt_remote_entries") or [])
        return entries

    @classmethod
    def _collect_audio_targets(
        cls,
        event: AstrMessageEvent,
    ) -> list[tuple[Any, str | int | None, bool]]:
        """收集语音目标 (Record, message_id, 是否引用消息)，跳过合并转发内部。

        与 mimo_media 保持一致：直接发送的语音用本条消息的 message_id；
        引用消息里的语音用被引用消息的 id（Reply.id）。
        """
        raw_message = getattr(event.message_obj, "raw_message", None)
        direct_message_id = None
        if isinstance(raw_message, dict):
            direct_message_id = raw_message.get("message_id") or raw_message.get("id")
        if direct_message_id is None:
            direct_message_id = getattr(event.message_obj, "message_id", None)

        targets: list[tuple[Any, str | int | None, bool]] = []
        for component, reply_id, in_forward in cls._component_entries(event):
            if not isinstance(component, Record) or in_forward:
                continue
            message_id = reply_id or direct_message_id
            targets.append((component, message_id, reply_id is not None))
        return targets

    async def _resolve_remote_audio(self, event: AstrMessageEvent) -> None:
        """解析 Reply 链里被省略的语音（仅有 reply id 时通过 OneBot get_msg 拉取）。

        mimo_media 同款逻辑：Reply 链里没有任何媒体组件时，用 OneBot11 的
        get_msg 拉取被引用消息原文，从中提取 record 语音段；每个事件只解析
        一次，结果挂在 event extras 上供 _component_entries 合并。
        """
        if event.get_extra("media_montage_stt_remote_resolved"):
            return
        event.set_extra("media_montage_stt_remote_resolved", True)

        local_entries = list(
            self._walk_components(
                getattr(getattr(event, "message_obj", None), "message", []) or []
            )
        )
        unresolved_replies: list[Reply] = []
        for component, _, _ in local_entries:
            if not isinstance(component, Reply) or not str(component.id).strip():
                continue
            embedded_media = any(
                isinstance(nested, (Image, Video, Record, Forward))
                for nested, _, _ in self._walk_components(component.chain or [])
            )
            if not embedded_media:
                unresolved_replies.append(component)

        if not unresolved_replies:
            return

        if OneBotClient is None:
            logger.warning(
                "[media_montage_429] 当前核心没有 OneBotClient，无法解析引用语音"
            )
            return
        client = OneBotClient(event)
        remote_entries: list[tuple[Any, str | int | None, bool]] = []
        for reply in unresolved_replies:
            payload = await client.get_msg(reply.id)
            if not payload:
                continue
            records = self._parse_payload_records(payload)
            remote_entries.extend((record, reply.id, False) for record in records)

        deduplicated_entries = []
        seen_entries: set[tuple[str, str, str, bool]] = set()
        for component, reply_id, in_forward in remote_entries:
            source = str(
                getattr(component, "url", None)
                or getattr(component, "file", None)
                or getattr(component, "path", None)
                or ""
            )
            key = (
                component.__class__.__name__,
                source,
                str(reply_id or ""),
                in_forward,
            )
            if key in seen_entries:
                continue
            seen_entries.add(key)
            deduplicated_entries.append((component, reply_id, in_forward))
        event.set_extra("media_montage_stt_remote_entries", deduplicated_entries)

    @classmethod
    def _parse_payload_records(cls, payload: Any) -> list[Record]:
        """从 OneBot get_msg 返回的消息原文里提取语音段（跳过合并转发内部）。

        与 mimo_media 的 _parse_forward_payload 语音分支一致：识别
        record / audio / voice 段，来源取 url/file/path；位于 node/nodes
        容器内部的语音（合并转发）不提取；按来源去重。
        """
        records: list[Record] = []
        seen_sources: set[tuple[str, str]] = set()

        def visit(value: Any, depth: int = 0, in_forward: bool = False) -> None:
            if depth > MAX_COMPONENT_DEPTH:
                return
            if isinstance(value, list):
                for item in value:
                    visit(item, depth + 1, in_forward)
                return
            if not isinstance(value, dict):
                return

            segment_type = str(value.get("type", "") or "").lower()
            segment_data = value.get("data")
            data = segment_data if isinstance(segment_data, dict) else value
            source = str(
                data.get("url") or data.get("file") or data.get("path") or ""
            ).strip()
            if (
                not in_forward
                and segment_type in ("record", "audio", "voice")
                and source
            ):
                key = ("record", source)
                if key not in seen_sources:
                    seen_sources.add(key)
                    records.append(
                        Record(
                            file=source,
                            url=str(data.get("url") or ""),
                            path=str(data.get("path") or ""),
                        )
                    )
                return

            containers = [value]
            if data is not value:
                containers.append(data)
            nested_in_forward = in_forward or segment_type in ("node", "nodes")
            for container in containers:
                for key in ("messages", "message", "nodes", "content"):
                    nested = container.get(key)
                    if isinstance(nested, (list, dict)):
                        visit(nested, depth + 1, nested_in_forward)
                    elif isinstance(nested, str) and nested.strip().startswith(
                        ("[", "{")
                    ):
                        try:
                            visit(json.loads(nested), depth + 1, nested_in_forward)
                        except json.JSONDecodeError:
                            pass

        visit(payload)
        return records

    async def _transcribe_with_llonebot(
        self,
        event: AstrMessageEvent,
        audio_targets: list[tuple[Any, str | int | None, bool]],
    ) -> list[tuple[str, bool]]:
        """调用 OneBot11 扩展接口 voice_msg_to_text 把语音转成文字。

        接口说明（onebot11 文档 / llonebot 5.1+）：POST /voice_msg_to_text，
        参数 message_id（整数）；返回 {status, retcode, data: {text}, ...}。
        与 mimo_media 保持同一调用参数结构：call_action(
            action="voice_msg_to_text", message_id=<int>)，数字字符串 id
        会先转成 int 再发送。
        """
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        if not callable(call_action):
            logger.warning("[media_montage_429] 当前 OneBot11 事件不支持 call_action")
            return []

        transcriptions: list[tuple[str, bool]] = []
        seen_message_ids: set[str] = set()
        for _, message_id, is_reply in audio_targets:
            if message_id is None or not str(message_id).strip():
                logger.warning(
                    "[media_montage_429] 语音消息缺少 message_id，无法调用 llonebot 转写"
                )
                continue
            message_id_key = str(message_id)
            if message_id_key in seen_message_ids:
                continue
            seen_message_ids.add(message_id_key)

            try:
                api_message_id: str | int = message_id
                if isinstance(message_id, str) and message_id.isdigit():
                    api_message_id = int(message_id)
                result = await call_action(
                    action="voice_msg_to_text",
                    message_id=api_message_id,
                )
                data = result.get("data", result) if isinstance(result, dict) else {}
                text = data.get("text") if isinstance(data, dict) else None
                if isinstance(text, str) and text.strip():
                    transcriptions.append((text.strip(), is_reply))
                else:
                    logger.warning(
                        "[media_montage_429] llonebot 转写返回空文本，message_id=%s",
                        message_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[media_montage_429] llonebot 语音转文字失败，message_id=%s: %s",
                    message_id,
                    exc,
                )
        return transcriptions

    @staticmethod
    def _inject_llonebot_transcriptions(
        req,
        transcriptions: list[tuple[str, bool]],
    ) -> None:
        """把转写文字注入请求：引用语音替换 <Quoted Message> 的 [Empty Text]，
        直接发送的语音追加为普通文本块（与 mimo_media 一致）。"""
        for text, is_reply in transcriptions:
            if is_reply:
                for part in req.extra_user_content_parts:
                    if not isinstance(part, TextPart):
                        continue
                    if "<Quoted Message>" not in part.text:
                        continue
                    if "[Empty Text]" not in part.text:
                        continue
                    part.text = part.text.replace("[Empty Text]", text, 1)
                    break
                else:
                    req.extra_user_content_parts.append(TextPart(text=text))
            else:
                req.extra_user_content_parts.append(TextPart(text=text))

    @staticmethod
    def _drop_audio_placeholders(req) -> None:
        """移除核心生成的 [Audio Attachment ...] 占位标签（语音已替换为文字）。

        注意只删音频标签：[Video Attachment] 标签是本插件视频抽帧拼图的
        唯一数据来源，必须保留。
        """
        kept = []
        for part in getattr(req, "extra_user_content_parts", None) or []:
            text = getattr(part, "text", "")
            if isinstance(text, str) and text.startswith(_AUDIO_PLACEHOLDER_PREFIXES):
                continue
            kept.append(part)
        req.extra_user_content_parts = kept

    # ------------------------------------------------------------------
    # 视频 -> 抽帧 -> 拼图
    # ------------------------------------------------------------------
    async def _attach_video_montages(self, req, event) -> list[str]:
        """把本轮请求里的视频转成抽帧拼图并塞进 req.image_urls。

        Returns:
            被转换的视频名列表（取自官方标签），空列表表示没有转换。

        只信官方标签：4.29+ 的核心在钩子之前已经把每个视频落地为本地文件，
        并以 ``[Video Attachment: name X, path Y]`` 标签写进
        ``req.extra_user_content_parts``。插件解析这些标签拿到路径后抽帧；
        拼图路径追加进 ``req.image_urls`` 后，由钩子之后的
        ``prepare_request_images`` 统一完成准备与标注，与官方图片一致。
        """
        urls = getattr(req, "image_urls", None)
        if not isinstance(urls, list):
            return []

        entries = _collect_official_video_refs(req)
        if not entries:
            return []

        added_names: list[str] = []
        existing = set(urls)
        cache: dict[str, str | None] = {}
        for name, video_path in entries:
            if not video_path or video_path in cache:
                continue
            montage_path = cache[video_path] = await self._build_video_montage(
                video_path
            )
            if not montage_path:
                continue

            try:
                track = getattr(event, "track_temporary_local_file", None)
                if callable(track):
                    track(montage_path)
            except Exception:
                pass

            if montage_path not in existing:
                urls.append(montage_path)
                existing.add(montage_path)
                added_names.append(name or os.path.basename(video_path))
                logger.info(
                    "[media_montage_429] 视频已转拼图: %s <- %s",
                    montage_path,
                    video_path,
                )

        return added_names

    async def _build_video_montage(self, video_path: str) -> str | None:
        frames = await self._extract_video_frames(video_path)
        if not frames:
            logger.warning(
                "[media_montage_429] 未能从视频抽到帧: %s", video_path
            )
            return None
        try:
            data = await asyncio.to_thread(
                _compose_montage,
                frames,
                self.video_grid,
                self.video_max_size,
                self.video_quality,
            )
        except Exception as exc:
            logger.warning("[media_montage_429] 视频拼图合成失败: %s", exc)
            return None
        if not data:
            return None
        path = _temp_output_path(".jpg")
        try:
            await asyncio.to_thread(Path(path).write_bytes, data)
        except OSError as exc:
            logger.warning("[media_montage_429] 拼图写入失败: %s", exc)
            return None
        return path

    def _scale_filter(self) -> str:
        """仅在源分辨率过大时缩小的滤镜；不会放大。"""
        box = max(self.video_max_size, 16) * self.video_grid
        return (
            "scale=w='if(gte(iw,ih),min(iw,{b}),-2)':"
            "h='if(gte(iw,ih),-2,min(ih,{b}))'".format(b=box)
        )

    async def _extract_video_frames(self, video_path: str) -> list[bytes]:
        """抽帧，返回按时间排序的编码帧。

        优先"按时间点 seek 抽帧"：时长已知时，把 count 个采样点均匀分布在
        整段时长上（取中点位置），逐个用 `-ss` 输入 seek 抽取——每次只需
        解码目标时间点附近的一个 GOP，长视频（数分钟、上百 MB）也能秒级
        完成。旧实现用 `fps=N/duration` 滤镜等间隔采样，必须从头解码整段
        视频，长视频很容易超出超时，导致一张帧都抽不到、模型完全看不到
        画面。读不出时长时才退回 fps 滤镜全解码（短视频场景，开销可忽略）。
        """
        count = self.video_frames
        duration = await self._probe_duration(video_path)
        if duration and duration > 0:
            frames = await self._extract_frames_by_seek(video_path, duration, count)
            if frames:
                return frames
            # seek 全部失败（罕见：损坏的索引等）时退回全解码再试一次。
            logger.warning(
                "[media_montage_429] 按时间点抽帧未得到任何帧，退回全解码重试: %s",
                video_path,
            )
        return await self._extract_frames_by_fps(video_path, count)

    async def _run_ffmpeg(self, command: list[str]) -> tuple[int, bytes]:
        """运行一次 ffmpeg，返回 (returncode, stderr)。

        未安装 ffmpeg 时返回 (-1, b"")；超时返回 (-2, b"")。
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error(
                "[media_montage_429] 未找到 ffmpeg，无法对视频抽帧。"
                "请安装 ffmpeg 并确保在 PATH 中。"
            )
            return -1, b""
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.video_timeout
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            logger.warning(
                "[media_montage_429] ffmpeg 运行超时（%ss）: %s",
                self.video_timeout,
                " ".join(command[:8]),
            )
            return -2, b""
        return process.returncode or 0, stderr or b""

    async def _extract_frames_by_seek(
        self, video_path: str, duration: float, count: int
    ) -> list[bytes]:
        """按均匀分布的时间点逐帧 seek 抽取（长视频友好）。"""
        timestamps = [duration * (index + 0.5) / count for index in range(count)]
        temp_dir = tempfile.mkdtemp(prefix="media_montage_frames_")
        frames: list[bytes] = []
        try:
            for index, timestamp in enumerate(timestamps):
                output = os.path.join(temp_dir, f"frame_{index:03d}.png")
                command = [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    video_path,
                    "-frames:v",
                    "1",
                    "-vf",
                    self._scale_filter(),
                    output,
                ]
                returncode, stderr = await self._run_ffmpeg(command)
                if returncode != 0:
                    if returncode > 0:
                        logger.warning(
                            "[media_montage_429] 第 %d 帧抽帧失败: %s",
                            index + 1,
                            (stderr or b"").decode(errors="ignore").strip(),
                        )
                    continue
                try:
                    if os.path.getsize(output) > 0:
                        frames.append(
                            await asyncio.to_thread(Path(output).read_bytes)
                        )
                except OSError:
                    continue
            return frames
        finally:
            _remove_tree(temp_dir)

    async def _extract_frames_by_fps(self, video_path: str, count: int) -> list[bytes]:
        """fps 滤镜等间隔抽帧（全解码，仅作兜底；中间帧写 PNG）。

        最终由 `_compose_montage` 统一缩放并压成一张 JPEG。
        """
        duration = await self._probe_duration(video_path)
        if duration and duration > 0:
            fps = max(count / duration, 1e-6)
            filters = [f"fps={fps:.6f}"]
        else:
            # 读不出时长时退化为每秒 1 帧，取前 count 帧。
            filters = ["fps=1"]
        filters.append(self._scale_filter())
        vf = ",".join(filters)

        temp_dir = tempfile.mkdtemp(prefix="media_montage_frames_")
        try:
            pattern = os.path.join(temp_dir, "frame_%03d.png")
            command = [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                video_path,
                "-vf",
                vf,
                "-frames:v",
                str(count),
                pattern,
            ]
            returncode, stderr = await self._run_ffmpeg(command)
            if returncode != 0:
                if returncode > 0:
                    logger.warning(
                        "[media_montage_429] ffmpeg 抽帧失败: %s",
                        (stderr or b"").decode(errors="ignore").strip(),
                    )
                return []

            files = sorted(Path(temp_dir).glob("frame_*.png"))
            frames: list[bytes] = []
            for file in files[:count]:
                try:
                    frames.append(await asyncio.to_thread(file.read_bytes))
                except OSError:
                    continue
            return frames
        finally:
            _remove_tree(temp_dir)

    async def _probe_duration(self, video_path: str) -> float | None:
        """返回视频时长（秒）。"""

        # 官方 helper 存在时优先使用；注意它返回的是毫秒。
        try:
            from astrbot.core.utils.media_utils import get_media_duration

            milliseconds = await get_media_duration(video_path)
            if milliseconds and milliseconds > 0:
                return float(milliseconds) / 1000.0
        except Exception:
            pass

        # 兜底：自己调 ffprobe（format=duration 的单位是秒）。
        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.video_timeout
            )
            if process.returncode == 0:
                value = float((stdout or b"").decode(errors="ignore").strip())
                return value if value > 0 else None
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # 诊断指令
    # ------------------------------------------------------------------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("montage_status")
    async def montage_status(self, event: AstrMessageEvent):
        """查看媒体拼图插件状态（仅管理员）。"""
        if self.audio_mode == LLONEBOT_STT_AUDIO_MODE:
            audio_status = (
                "llonebot_stt（仅 OneBot11/aiocqhttp 平台生效）"
                if self.stt_ready
                else "llonebot_stt（核心结构缺失，已停用）"
            )
        else:
            audio_status = "关闭（保持 AstrBot 原有音频行为）"
        lines = [
            f"插件: {PLUGIN_NAME} v1.1.0",
            f"AstrBot 版本: {self.version or '未知'} -> {'.'.join(map(str, self.version_tuple))}",
            f"版本要求 (>= {MIN_ASTRBOT_VERSION[0]}.{MIN_ASTRBOT_VERSION[1]}.{MIN_ASTRBOT_VERSION[2]}): "
            f"{'满足' if self.version_ok else '不满足（插件停用）'}",
            f"核心函数注入: 无（零注入，只用官方钩子）",
            f"视频抽帧拼图: {'启用' if self.enable_video_montage else '关闭'}"
            f"（{self.video_grid}x{self.video_grid} / {self.video_frames} 帧 / 最长边 {self.video_max_size}px）",
            f"视频解析来源: 官方 [Video Attachment] 标签",
            f"语音转文字 (llonebot): {audio_status}",
        ]
        yield event.plain_result("\n".join(lines))


# ---------------------------------------------------------------------------
# 模块级工具
# ---------------------------------------------------------------------------
def _collect_official_video_refs(req) -> list[tuple[str, str]]:
    """从核心写好的官方标签里收集 (视频名, 本地路径)。

    4.29+ 的核心在 on_llm_request 钩子之前就完成了视频解析
    （astr_main_agent._append_video_attachment），标签形如::

        [Video Attachment: name xxx.mp4, path /tmp/xxx.mp4]
        [Video Attachment in quoted message: name xxx.mp4, path /tmp/xxx.mp4]

    引用视频无法落地时核心只保留 ``ref`` 而不是 ``path``，这类标签不会被
    匹配（插件无法对未落地视频抽帧），属于预期行为。
    """
    parts = getattr(req, "extra_user_content_parts", None) or []
    found: list[tuple[str, str]] = []
    for part in parts:
        text = getattr(part, "text", None)
        if not isinstance(text, str):
            continue
        match = _OFFICIAL_VIDEO_LABEL_RE.match(text.strip())
        if match:
            found.append((match.group("name").strip(), match.group("path").strip()))
    return found


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _remove_tree(path: str) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
