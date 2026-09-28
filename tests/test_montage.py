"""离线自测：不启动 AstrBot，直接在当前 venv 下验证插件关键逻辑。

运行：
    cd AstrBot && .venv/Scripts/python.exe ../astrbot_plugin_media_montage/tests/test_montage.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
PLUGIN_DIR = HERE.parent.parent

# 用 MM_ASTROBOT_DIR 指定要测试的 AstrBot 目录（例如 4.28.1 快照）。
_env_dir = os.environ.get("MM_ASTROBOT_DIR")
if _env_dir:
    ASTROBOT_DIR = Path(_env_dir)
elif (PLUGIN_DIR.parent / "AstrBot" / "astrbot").is_dir():
    ASTROBOT_DIR = PLUGIN_DIR.parent / "AstrBot"
else:
    ASTROBOT_DIR = PLUGIN_DIR.parent / "AstrBot"
    for candidate in sorted(Path(PLUGIN_DIR.parent).glob("AstrBot-*")):
        if (candidate / "astrbot").is_dir():
            ASTROBOT_DIR = candidate
            break

print(f"[setup] AstrBot 目录: {ASTROBOT_DIR}")

sys.path.insert(0, str(ASTROBOT_DIR))
sys.path.insert(0, str(ASTROBOT_DIR / "data" / "site-packages"))

from PIL import Image  # noqa: E402

import astrbot.core.pipeline.process_stage.method.agent_sub_stages.image_input as image_input  # noqa: E402
import astrbot.core.utils.media_utils as media_utils  # noqa: E402


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_media_montage_main", PLUGIN_DIR / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _path_of(result):
    """兼容两种核心：返回 str，或返回 (path, is_montage)。"""
    if isinstance(result, (tuple, list)):
        return result[0]
    return result


def _jpeg(color, size=(60, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


class _MessageObj:
    def __init__(self, message=()):
        self.message = list(message)


class _Event:
    def __init__(self, message=()):
        self.message_obj = _MessageObj(message)
        self.tracked = []

    def track_temporary_local_file(self, path):
        self.tracked.append(path)


class _Req:
    def __init__(self, image_urls=()):
        self.image_urls = list(image_urls)
        self.extra_user_content_parts = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        raise AssertionError(name)


async def main() -> None:
    mod = _load_plugin_module()

    # --- 1. 版本解析与门控 -------------------------------------------------
    check("版本解析 4.28.2", mod._version_tuple("4.28.2") == (4, 28, 2))
    check("版本解析 v4.28.1-beta.1", mod._version_tuple("v4.28.1-beta.1") == (4, 28, 1))
    check("版本解析空串", mod._version_tuple("") == (0, 0, 0))
    check(
        "门控集合仅 4.28.1/4.28.2",
        mod.GIF_FIX_VERSIONS == frozenset({(4, 28, 1), (4, 28, 2)}),
    )
    for version, expected in (
        ("4.28.0", False),
        ("4.28.1", True),
        ("4.28.2", True),
        ("4.28.3", False),
        ("4.29.0", False),
    ):
        supported = mod._version_tuple(version) in mod.GIF_FIX_VERSIONS
        check(f"门控 {version} -> {expected}", supported is expected)

    # --- 2. 拼图合成 -------------------------------------------------------
    montage = mod._compose_montage(
        [
            _jpeg((255, 0, 0)),
            _jpeg((0, 255, 0)),
            _jpeg((0, 0, 255)),
        ],
        3,
        1280,
        95,
    )
    image = Image.open(io.BytesIO(montage)).convert("RGB")
    check("拼图尺寸 = 3x3 网格", image.size == (180, 90), str(image.size))
    check("第 1 格为红", image.getpixel((10, 10))[0] > 200)
    check("第 2 格为绿", image.getpixel((70, 10))[1] > 200)
    check("第 3 格为蓝", image.getpixel((130, 10))[2] > 200)
    check("第 4 格（下一行）为空", image.getpixel((10, 40)) == (255, 255, 255))
    check("空格为白", image.getpixel((170, 80)) == (255, 255, 255))

    big = mod._compose_montage([_jpeg("red", (2000, 1000))], 3, 1280, 95)
    big_image = Image.open(io.BytesIO(big))
    check(
        "最长边被限制在 1280 内",
        max(big_image.size) <= 1280,
        str(big_image.size),
    )
    check("小图不放大", mod._compose_montage([_jpeg("red")], 3, 1280, 95) is not None)
    check("空帧返回 None", mod._compose_montage([], 3, 1280, 95) is None)

    # --- 3. 补丁安装 -------------------------------------------------------
    plugin = mod.MediaMontagePlugin(context=None, config={})
    print(
        f"[info] version={plugin.version}, gif_fix_supported={plugin.gif_fix_supported}, "
        f"core_fix_present={plugin.core_fix_present}, "
        f"gif_notice_active={plugin.gif_notice_active}"
    )
    check(
        "插件识别到 4.28.x 版本",
        plugin.version_tuple in {(4, 28, 1), (4, 28, 2)},
        plugin.version,
    )
    check("版本属于补丁目标版本", plugin.gif_fix_supported is True)

    expect_patch = plugin.gif_notice_active
    check(
        "media_utils 包装状态符合预期",
        getattr(media_utils.prepare_model_image, mod._WRAP_MARK, False) is expect_patch,
    )
    check(
        "image_input 包装状态符合预期",
        getattr(image_input.prepare_model_image, mod._WRAP_MARK, False) is expect_patch,
    )
    await plugin.initialize()
    check(
        "initialize 幂等",
        len(plugin._patched) == (2 if expect_patch else 0),
        str(len(plugin._patched)),
    )

    # handler 是否被 AstrBot 正确注册（装饰器解析无误）
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    ours = [
        h
        for h in star_handlers_registry.star_handlers_map.values()
        if "astrbot_plugin_media_montage_main" in (h.handler_full_name or "")
    ]
    names = {h.handler_name for h in ours}
    check("on_llm_request 钩子已注册", "on_llm_request" in names, str(sorted(names)))
    check("montage_status 指令已注册", "montage_status" in names)
    hook = next((h for h in ours if h.handler_name == "on_llm_request"), None)
    check(
        "钩子事件类型为 OnLLMRequestEvent",
        hook is not None and hook.event_type == EventType.OnLLMRequestEvent,
        str(getattr(hook, "event_type", None)),
    )

    # --- 4. 端到端：动图 -> 拼图 -> 提示 ------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        gif = tmp_path / "anim.gif"
        frames = [Image.new("RGB", (60, 30), c) for c in ("red", "green", "blue")]
        frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:])

        still = tmp_path / "still.png"
        Image.new("RGB", (60, 30), "red").save(still)

        out_dir = tmp_path / "out"
        produced_raw = await media_utils.prepare_model_image(
            str(gif), max_size=1280, output_dir=out_dir
        )
        # 已合入官方修复的核心会返回 (path, is_montage)，未合入的返回 path。
        produced = _path_of(produced_raw)
        check(
            "动图准备返回可用路径",
            isinstance(produced, str) and Path(produced).is_file(),
            str(type(produced_raw)),
        )
        if expect_patch:
            check("动图被登记为拼图", mod._MONTAGE_PATHS.contains(produced))

        still_produced = _path_of(
            await media_utils.prepare_model_image(
                str(still), max_size=1280, output_dir=out_dir
            )
        )
        check("静图未被登记为拼图", not mod._MONTAGE_PATHS.contains(still_produced))

        # 缓存命中（第二次调用同一个动图）也应登记
        cached = _path_of(
            await media_utils.prepare_model_image(
                str(gif), max_size=1280, output_dir=out_dir
            )
        )
        if expect_patch:
            check("缓存命中的动图仍被登记", mod._MONTAGE_PATHS.contains(cached))

        if not expect_patch:
            print("[info] 核心已自带拼图提示，跳过插件侧提示断言。")

        # 钩子：命中拼图 -> 应追加 <system_notice>
        req = _Req([produced])
        await plugin.on_llm_request(_Event(), req)
        notices = [
            p
            for p in req.extra_user_content_parts
            if getattr(p, "text", "").startswith("<system_notice>")
        ]
        check(
            "动图提示数量符合预期",
            len(notices) == (1 if expect_patch else 0),
            f"got {len(notices)}, expect_patch={expect_patch}",
        )
        if expect_patch and notices:
            text = notices[0].text
            check("提示被标记为临时内容（不落库）", notices[0]._no_save is True)
            check("提示包含 reading order", "reading order" in text)
            check("提示包含 Treat it as an animation", "Treat it as an animation" in text)
            check(
                "提示首尾正确",
                text.startswith("<system_notice>\n")
                and text.endswith("\n</system_notice>"),
            )

        # 钩子：只有静图 -> 不应追加
        req2 = _Req([still_produced])
        await plugin.on_llm_request(_Event(), req2)
        check("静图不产生提示", len(req2.extra_user_content_parts) == 0)

    # --- 5. 视频抽帧拼图 ---------------------------------------------------
    if subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "test.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
                    "-pix_fmt", "yuv420p", str(video),
                ],
                check=True,
            )
            duration = await plugin._probe_duration(str(video))
            check(
                "ffprobe 读出时长（秒）",
                bool(duration) and 1.5 < duration < 2.5,
                str(duration),
            )

            montage_path = await plugin._build_video_montage(str(video))
            check("视频拼图生成成功", bool(montage_path) and Path(montage_path).is_file())
            if montage_path:
                vm = Image.open(montage_path)
                check("视频拼图为 3x3", vm.size == (960, 720), str(vm.size))

            from astrbot.core.message.components import Video as CoreVideo

            check(
                "插件使用的 Video 与核心同一个类",
                mod.Video is CoreVideo,
                f"{mod.Video} vs {CoreVideo}",
            )
            video_comp = CoreVideo.fromFileSystem(str(video))
            req = _Req()
            event = _Event([video_comp])
            check(
                "能从消息链收集到视频组件",
                len(mod._collect_video_components(event)) == 1,
            )
            added = await plugin._attach_video_montages(req, event)
            check("视频拼图被加入 image_urls", added and len(req.image_urls) == 1)
            check("已登记临时文件", len(event.tracked) == 1)
            await plugin.on_llm_request(event, req)
            video_notices = [
                p
                for p in req.extra_user_content_parts
                if "video" in getattr(p, "text", "")
            ]
            check("视频提示已追加", len(video_notices) == 1, str(len(video_notices)))

    # --- 6. 卸载还原 -------------------------------------------------------
    await plugin.terminate()
    check(
        "media_utils 已还原",
        getattr(media_utils.prepare_model_image, mod._WRAP_MARK, False) is False,
    )
    check(
        "image_input 已还原",
        getattr(image_input.prepare_model_image, mod._WRAP_MARK, False) is False,
    )

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
