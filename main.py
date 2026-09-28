"""astrbot_plugin_media_montage

把"动图抽帧拼图"这件事做完整：既是补丁，也是增强。

一、动图拼图提示补丁（仅 AstrBot 4.28.1 / 4.28.2 生效）
    AstrBot 从 4.28.1 起，内置 Agent 会把消息里的动图（GIF / APNG / 动态 WebP）
    转成一张 3x3 的"抽帧拼图"再交给模型。但这两个版本忘了告诉模型"这是一张动图"，
    于是模型会把拼图当成一张静态图（或几张并排的图）来理解，经常看不懂。
    官方后续补丁的做法是：往本轮请求里追加一条临时的 <system_notice> 提示。
    本插件用插件的方式把这条提示补回来，行为与官方补丁保持一致
    （同样标记为临时内容，不写入会话历史）。

二、视频抽帧拼图（所有 AstrBot 版本生效）
    消息里的视频目前只会以一行文本 "[Video Attachment: ...]" 的形式进入请求，
    模型看不到画面。本插件用 ffmpeg 对视频等间隔抽帧，然后套用与动图**完全相同**
    的 3x3 拼图逻辑合成一张图片，追加到 req.image_urls 里，并附带同样的提示。

设计要点
--------
1. 精确探测"这一轮到底有没有生成拼图"：
   核心在 `image_input.prepare_request_images()` 里调用
   `media_utils.prepare_model_image()`，只有帧数 > 1 时才走拼图分支。
   插件在加载时包装这个函数（media_utils 与 image_input 两处绑定都要换），
   把"产出路径 -> 是否为拼图"记下来；钩子里只要 req.image_urls 命中记录，
   就说明本轮确实有拼图，与官方补丁的 has_montage 语义完全一致。
2. 视频走同一套拼图逻辑：3x3 网格、白色背景、帧按阅读顺序排布、
   最长边不超过 montage_max_size，和 `_extract_animation_montage_sync` 一致。
3. 版本门控：动图提示只对 4.28.x 的这两个版本生效（更早的版本没有拼图机制，
   提示会误导模型；更晚的版本应当由官方修复接管）。视频抽帧不设版本限制。
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile
from collections import OrderedDict
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 配置类型（>= 4.x 均有）
    from astrbot.api import AstrBotConfig
except Exception:  # pragma: no cover - 仅作类型注解降级
    AstrBotConfig = dict  # type: ignore[misc,assignment]

# ---------------------------------------------------------------------------
# 消息组件类型：优先从官方 api 导入，失败则退回 core
# ---------------------------------------------------------------------------
try:
    from astrbot.api.message_components import Reply, Video
except Exception:  # pragma: no cover
    try:
        from astrbot.core.message.components import Reply, Video
    except Exception:  # pragma: no cover
        Reply = None  # type: ignore[assignment]
        Video = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PLUGIN_NAME = "astrbot_plugin_media_montage"

GIF_FIX_VERSIONS: frozenset[tuple[int, int, int]] = frozenset(
    {(4, 28, 1), (4, 28, 2)}
)
"""动图拼图提示补丁只在这些 AstrBot 版本上生效。"""

DEFAULT_GIF_NOTICE = (
    "The input includes a GIF converted into a single image with frames "
    "in reading order. Treat it as an animation; "
    "do not mention the conversion or frame layout."
)
DEFAULT_VIDEO_NOTICE = (
    "The input includes a video converted into a single image with frames "
    "in reading order. Treat this image as a sequence of video frames; "
    "do not mention the conversion or frame layout."
)

_WRAP_MARK = "__media_montage_wrapped__"
_PATCH_MODULE_ATTR = "_media_montage_original"


class _MontagePathRegistry(OrderedDict):
    """有界的 "产出路径 -> True" 记录表，避免长时间运行后无限增长。"""

    def __init__(self, maxlen: int = 2048) -> None:
        super().__init__()
        self._maxlen = maxlen

    def mark(self, path: str) -> None:
        self[path] = True
        self.move_to_end(path)
        while len(self) > self._maxlen:
            self.popitem(last=False)

    def contains(self, path: object) -> bool:
        return isinstance(path, str) and path in self


_MONTAGE_PATHS = _MontagePathRegistry()
"""prepare_model_image 产出路径中，哪些是动图拼图。"""


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
    """把 "4.28.2" / "v4.28.2-beta.1" 这类字符串解析成 (4, 28, 2)。"""
    nums = re.findall(r"\d+", version or "")
    parts = [int(n) for n in nums[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _core_already_reports_montage() -> bool:
    """检测核心是否已经自带"拼图提示"（官方修复已合入）。

    官方修复的特征是 `prepare_request_images` 里出现 ``has_montage`` 与
    ``<system_notice>``。若已合入，插件让位，避免给模型重复提示。
    """
    try:
        import inspect

        from astrbot.core.pipeline.process_stage.method.agent_sub_stages import (
            image_input,
        )

        source = inspect.getsource(image_input.prepare_request_images)
        return "has_montage" in source or "<system_notice>" in source
    except Exception:
        return False


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


def _ref_to_local_path(ref: object) -> str | None:
    """把本地路径 / file:// 引用转成真实文件路径；网络与 base64 引用返回 None。"""
    if not isinstance(ref, str) or not ref:
        return None
    if ref.startswith("file://"):
        try:
            from astrbot.core.utils.media_utils import file_uri_to_path

            path = file_uri_to_path(ref)
        except Exception:
            from urllib.parse import unquote, urlparse

            path = unquote(urlparse(ref).path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
                path = path[1:]
        try:
            return path if os.path.isfile(path) else None
        except OSError:
            return None
    if ref.startswith(("http://", "https://", "base64://", "data:")):
        return None
    try:
        return ref if os.path.isfile(ref) else None
    except OSError:
        return None


def _count_frames_from_bytes(data: bytes) -> int:
    from PIL import Image as PILImage

    with PILImage.open(io.BytesIO(data)) as image:
        return int(getattr(image, "n_frames", 1) or 1)


def _count_frames_from_path(path: str) -> int:
    from PIL import Image as PILImage

    with PILImage.open(path) as image:
        return int(getattr(image, "n_frames", 1) or 1)


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
    "为 4.28.1/4.28.2 补回动图拼图的模型提示；并让视频也以抽帧拼图的方式被模型理解。",
    "1.0.0",
)
class MediaMontagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

        # --- 配置 ---
        self.enable_gif_notice: bool = _as_bool(
            self.config.get("enable_gif_notice", True)
        )
        self.enable_video_montage: bool = _as_bool(
            self.config.get("enable_video_montage", True)
        )
        self.enable_video_notice: bool = _as_bool(
            self.config.get("enable_video_notice", True)
        )
        self.gif_notice_text: str = str(
            self.config.get("gif_notice_text") or DEFAULT_GIF_NOTICE
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

        # --- 版本门控 ---
        self.version = _get_astrbot_version()
        self.version_tuple = _version_tuple(self.version)
        self.gif_fix_supported = self.version_tuple in GIF_FIX_VERSIONS
        # 官方修复若已合入（例如某些 4.28.1 之后的分支构建），插件让位。
        self.core_fix_present = _core_already_reports_montage()
        self.gif_notice_active = (
            self.gif_fix_supported and self.enable_gif_notice and not self.core_fix_present
        )

        self._patched: list[tuple[object, str, object]] = []
        self._apply_prepare_model_image_patch()

        logger.info(
            "[media_montage] loaded. astrbot=%s, gif_notice=%s "
            "(supported=%s, core_fix=%s, active=%s), "
            "video_montage=%s (grid=%dx%d, frames=%d)",
            self.version or "unknown",
            self.enable_gif_notice,
            self.gif_fix_supported,
            self.core_fix_present,
            self.gif_notice_active,
            self.enable_video_montage,
            self.video_grid,
            self.video_grid,
            self.video_frames,
        )

    async def initialize(self) -> None:
        # initialize() 在实例化之后被调用；这里再确保一次补丁已生效。
        self._apply_prepare_model_image_patch()

    async def terminate(self) -> None:
        self._remove_prepare_model_image_patch()

    # ------------------------------------------------------------------
    # prepare_model_image 包装：精确判断本轮是否生成了动图拼图
    # ------------------------------------------------------------------
    def _apply_prepare_model_image_patch(self) -> None:
        if not self.gif_notice_active:
            return

        target_modules: list[object] = []
        try:
            import astrbot.core.utils.media_utils as media_utils

            target_modules.append(media_utils)
        except Exception as exc:
            logger.warning("[media_montage] 无法导入 media_utils: %s", exc)
            return
        try:
            from astrbot.core.pipeline.process_stage.method.agent_sub_stages import (
                image_input,
            )

            target_modules.append(image_input)
        except Exception:
            pass

        original = getattr(target_modules[0], "prepare_model_image", None)
        if original is None:
            logger.info(
                "[media_montage] 当前版本没有 prepare_model_image，跳过动图提示补丁。"
            )
            return
        if getattr(original, _WRAP_MARK, False):
            return  # 已经打过补丁（例如 initialize 被重复调用）

        plugin = self

        async def wrapped_prepare_model_image(image_ref, *args, **kwargs):
            result = await original(image_ref, *args, **kwargs)
            try:
                if result:
                    produced = result[0] if isinstance(result, (tuple, list)) else result
                    if isinstance(produced, str) and await plugin._ref_is_animated(
                        image_ref
                    ):
                        _MONTAGE_PATHS.mark(produced)
            except Exception as exc:  # 探测失败绝不能影响主流程
                logger.debug("[media_montage] montage probe failed: %s", exc)
            return result

        setattr(wrapped_prepare_model_image, _WRAP_MARK, True)
        setattr(wrapped_prepare_model_image, "__wrapped__", original)

        for module in target_modules:
            if getattr(module, "prepare_model_image", None) is original:
                setattr(module, "prepare_model_image", wrapped_prepare_model_image)
                self._patched.append((module, "prepare_model_image", original))

    def _remove_prepare_model_image_patch(self) -> None:
        for module, attr, original in self._patched:
            current = getattr(module, attr, None)
            if current is not None and getattr(current, _WRAP_MARK, False):
                setattr(module, attr, original)
        self._patched.clear()

    async def _ref_is_animated(self, ref: object) -> bool:
        """判断某个图片引用是否为多帧（动图）。

        与核心 `prepare_model_image` 的判定保持一致：帧数 > 1 即走拼图分支。
        """
        local = _ref_to_local_path(ref)
        if local is not None:
            frames = await asyncio.to_thread(_count_frames_safe, local)
            return frames > 1
        # 非本地引用（少数插件会直接塞 URL）：交给 MediaResolver 落地后再数帧。
        try:
            from astrbot.core.utils.media_utils import MediaResolver

            async with MediaResolver(str(ref), media_type="image").as_path() as source:
                data = await asyncio.to_thread(source.read_bytes)
            frames = await asyncio.to_thread(_count_frames_bytes_safe, data)
            return frames > 1
        except Exception:
            return False

    # ------------------------------------------------------------------
    # LLM 请求钩子
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """在请求装配完成后，补上动图提示，并把视频抽帧拼图塞进请求。"""
        parts: list[str] = []

        # 1) 动图（官方漏掉提示的那部分）：仅 4.28.1 / 4.28.2 且核心尚未自带提示
        if self.gif_notice_active:
            try:
                if self._request_has_montage(req):
                    parts.append(self.gif_notice_text)
            except Exception as exc:
                logger.debug("[media_montage] gif notice check failed: %s", exc)

        # 2) 视频抽帧拼图：所有版本
        if self.enable_video_montage:
            try:
                if await self._attach_video_montages(req, event):
                    if self.enable_video_notice:
                        parts.append(self.video_notice_text)
            except Exception as exc:
                logger.warning(
                    "[media_montage] video montage failed: %s", exc, exc_info=True
                )

        if parts:
            self._append_notice(req, parts)

    def _request_has_montage(self, req) -> bool:
        urls = getattr(req, "image_urls", None) or []
        return any(_MONTAGE_PATHS.contains(url) for url in urls)

    def _append_notice(self, req, bodies: list[str]) -> None:
        """把提示追加到 req.extra_user_content_parts，并标记为临时内容。"""
        try:
            from astrbot.core.agent.message import TextPart
        except Exception:
            try:
                from astrbot.api.message_components import Plain as TextPart  # type: ignore
            except Exception:
                logger.warning("[media_montage] 无法构造 TextPart，跳过提示。")
                return

        text = "<system_notice>\n" + "\n".join(bodies) + "\n</system_notice>"
        try:
            part = TextPart(text=text)
        except Exception:
            logger.warning("[media_montage] 构造提示失败。")
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
    async def _attach_video_montages(self, req, event) -> bool:
        videos = _collect_video_components(event)
        if not videos:
            return False

        urls = getattr(req, "image_urls", None)
        if not isinstance(urls, list):
            return False

        added = False
        existing = set(urls)
        cache: dict[str, str | None] = {}
        for comp in videos:
            video_path: str | None = None
            try:
                video_path = await comp.convert_to_file_path()
            except Exception as exc:
                logger.warning("[media_montage] 视频落地失败，跳过: %s", exc)
                continue
            if not video_path:
                continue

            if video_path not in cache:
                cache[video_path] = await self._build_video_montage(video_path)
            montage_path = cache[video_path]
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
                added = True
                logger.info("[media_montage] 视频已转拼图: %s", montage_path)

        return added

    async def _build_video_montage(self, video_path: str) -> str | None:
        frames = await self._extract_video_frames(video_path)
        if not frames:
            logger.warning("[media_montage] 未能从视频抽到帧: %s", video_path)
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
            logger.warning("[media_montage] 视频拼图合成失败: %s", exc)
            return None
        if not data:
            return None
        path = _temp_output_path(".jpg")
        try:
            await asyncio.to_thread(Path(path).write_bytes, data)
        except OSError as exc:
            logger.warning("[media_montage] 拼图写入失败: %s", exc)
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
                    "[media_montage] 未找到 ffmpeg，无法对视频抽帧。"
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
                    "[media_montage] 视频抽帧超时（%ss）: %s",
                    self.video_timeout,
                    video_path,
                )
                return []

            if process.returncode != 0:
                message = (stderr or b"").decode(errors="ignore").strip()
                logger.warning("[media_montage] ffmpeg 抽帧失败: %s", message)
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
            f"插件: {PLUGIN_NAME}",
            f"AstrBot 版本: {self.version or '未知'} -> {'.'.join(map(str, self.version_tuple))}",
            f"动图提示补丁: {'启用' if self.gif_notice_active else '未生效'}"
            f"（版本支持: {'是' if self.gif_fix_supported else '否'}, "
            f"官方已修复: {'是' if self.core_fix_present else '否'}, "
            f"开关: {'开' if self.enable_gif_notice else '关'}）",
            f"已挂钩模块: {', '.join(getattr(m, '__name__', str(m)) for m, _, _ in self._patched) or '无'}",
            f"视频抽帧拼图: {'启用' if self.enable_video_montage else '关闭'}"
            f"（{self.video_grid}x{self.video_grid} / {self.video_frames} 帧 / 最长边 {self.video_max_size}px）",
            f"已记录拼图数: {len(_MONTAGE_PATHS)}",
        ]
        yield event.plain_result("\n".join(lines))


# ---------------------------------------------------------------------------
# 模块级工具
# ---------------------------------------------------------------------------
def _count_frames_safe(path: str) -> int:
    try:
        return _count_frames_from_path(path)
    except Exception:
        return 1


def _count_frames_bytes_safe(data: bytes) -> int:
    try:
        return _count_frames_from_bytes(data)
    except Exception:
        return 1


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


def _collect_video_components(event) -> list:
    """收集消息本体与引用消息里的视频组件。"""
    if Video is None:
        return []
    message = getattr(getattr(event, "message_obj", None), "message", None) or []
    found: list = []
    for comp in message:
        if isinstance(comp, Video):
            found.append(comp)
        elif Reply is not None and isinstance(comp, Reply):
            for sub in getattr(comp, "chain", None) or []:
                if isinstance(sub, Video):
                    found.append(sub)
    return found


def _remove_tree(path: str) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
