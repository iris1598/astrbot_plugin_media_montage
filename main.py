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
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 配置类型（>= 4.x 均有）
    from astrbot.api import AstrBotConfig
except Exception:  # pragma: no cover - 仅作类型注解降级
    AstrBotConfig = dict  # type: ignore[misc,assignment]

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
    "AstrBot 4.29+ 专版：让视频以抽帧拼图的方式被模型看懂（零注入，只用官方钩子）。",
    "1.0.0",
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
            "video_montage=%s (grid=%dx%d, frames=%d)",
            self.version or "unknown",
            self.version_ok,
            self.enable_video_montage,
            self.video_grid,
            self.video_grid,
            self.video_frames,
        )

    async def terminate(self) -> None:
        # 零注入：没有任何核心函数包装需要还原。
        return

    # ------------------------------------------------------------------
    # LLM 请求钩子
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """把本轮请求里的视频转成抽帧拼图并塞进请求。"""
        if not self.version_ok or not self.enable_video_montage:
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

    async def _extract_video_frames(self, video_path: str) -> list[bytes]:
        """用 ffmpeg 对视频等间隔抽帧，返回按时间排序的编码帧。

        中间帧写成 PNG（无损，且不像 mjpeg 那样拒收 full-range YUV），
        最终由 `_compose_montage` 统一缩放并压成一张 JPEG。
        """
        count = self.video_frames
        duration = await self._probe_duration(video_path)
        # 等间隔抽帧：把整段时长压缩成 count 个采样点。
        if duration and duration > 0:
            fps = max(count / duration, 1e-6)
            filters = [f"fps={fps:.6f}"]
        else:
            # 读不出时长时退化为每秒 1 帧，取前 count 帧。
            filters = ["fps=1"]
        # 只在源分辨率过大时缩小，避免抽到 4K 帧把临时目录撑爆；不会放大。
        box = max(self.video_max_size, 16) * self.video_grid
        filters.append(
            "scale=w='if(gte(iw,ih),min(iw,{b}),-2)':"
            "h='if(gte(iw,ih),-2,min(ih,{b}))'".format(b=box)
        )
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
                return []

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
                    "[media_montage_429] 视频抽帧超时（%ss）: %s",
                    self.video_timeout,
                    video_path,
                )
                return []

            if process.returncode != 0:
                message = (stderr or b"").decode(errors="ignore").strip()
                logger.warning("[media_montage_429] ffmpeg 抽帧失败: %s", message)
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
        lines = [
            f"插件: {PLUGIN_NAME} v1.0.0",
            f"AstrBot 版本: {self.version or '未知'} -> {'.'.join(map(str, self.version_tuple))}",
            f"版本要求 (>= {MIN_ASTRBOT_VERSION[0]}.{MIN_ASTRBOT_VERSION[1]}.{MIN_ASTRBOT_VERSION[2]}): "
            f"{'满足' if self.version_ok else '不满足（插件停用）'}",
            f"核心函数注入: 无（零注入，只用官方钩子）",
            f"视频抽帧拼图: {'启用' if self.enable_video_montage else '关闭'}"
            f"（{self.video_grid}x{self.video_grid} / {self.video_frames} 帧 / 最长边 {self.video_max_size}px）",
            f"视频解析来源: 官方 [Video Attachment] 标签",
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
