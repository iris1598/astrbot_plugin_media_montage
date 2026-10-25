"""离线自测（4.29+ 专版）：不启动 AstrBot，直接在当前 venv 下验证关键逻辑。

运行：
    cd AstrBot && .venv/Scripts/python.exe ../astrbot_plugin_media_montage_429/tests/test_montage_429.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
PLUGIN_DIR = HERE.parent.parent

_env_dir = None
import os  # noqa: E402

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

import astrbot.core.utils.media_utils as media_utils  # noqa: E402


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_media_montage_429_main", PLUGIN_DIR / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
    def __init__(self, image_urls=(), parts=()):
        self.image_urls = list(image_urls)
        self.extra_user_content_parts = list(parts)


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        raise AssertionError(name)


async def main() -> None:
    mod = _load_plugin_module()

    # --- 1. 版本解析 -------------------------------------------------------
    check("版本解析 4.29.0", mod._version_tuple("4.29.0") == (4, 29, 0))
    check("版本解析 v4.29.1-beta.1", mod._version_tuple("v4.29.1-beta.1") == (4, 29, 1))
    check("版本解析空串", mod._version_tuple("") == (0, 0, 0))
    check("最低版本要求为 4.29.0", mod.MIN_ASTRBOT_VERSION == (4, 29, 0))

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

    # --- 3. 零注入断言 -----------------------------------------------------
    plugin = mod.MediaMontagePlugin(context=None, config={})
    print(
        f"[info] version={plugin.version}, version_ok={plugin.version_ok}, "
        f"video_montage={plugin.enable_video_montage}"
    )
    check(
        "快照版本满足 4.29+ 要求",
        plugin.version_ok is True,
        plugin.version,
    )
    check(
        "没有任何核心函数被包装（零注入）",
        getattr(media_utils.prepare_model_image, "__media_montage_wrapped__", False)
        is False,
    )
    check("terminate 为空操作", await plugin.terminate() is None)
    check("插件没有 _apply_prepare_model_image_patch 等注入方法", not hasattr(plugin, "_apply_prepare_model_image_patch"))
    check("插件没有 _MONTAGE_PATHS 注入产物", not hasattr(mod, "_MONTAGE_PATHS"))
    check("插件没有 GIF_FIX_VERSIONS 旧版门控", not hasattr(mod, "GIF_FIX_VERSIONS"))

    # handler 是否被 AstrBot 正确注册（装饰器解析无误）
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    ours = [
        h
        for h in star_handlers_registry.star_handlers_map.values()
        if "astrbot_plugin_media_montage_429_main" in (h.handler_full_name or "")
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

    # --- 3.5 OneBot11 语音转文字（自 mimo_media 移植） ----------------------
    from astrbot.core.agent.message import TextPart
    from astrbot.core.message.components import Nodes as CoreNodes
    from astrbot.core.message.components import Node as CoreNode
    from astrbot.core.message.components import Record as CoreRecord
    from astrbot.core.message.components import Reply as CoreReply

    class _SttApi:
        """OneBotClient 走的 bot.api.call_action（get_msg 等）。"""

        def __init__(self, payloads):
            self.payloads = payloads
            self.calls = []

        async def call_action(self, action, **params):
            self.calls.append((action, params))
            key = str(params.get("message_id") or params.get("id") or "")
            return self.payloads.get(key)

    class _SttBot:
        """voice_msg_to_text 走的 bot.call_action（与 mimo_media 相同入口）。"""

        def __init__(self, text="", payloads=None):
            self.api = _SttApi(payloads or {})
            self.text = text
            self.stt_calls = []

        async def call_action(self, **kwargs):
            self.stt_calls.append(kwargs)
            return {"status": "ok", "retcode": 0, "data": {"text": self.text}}

    class _SttEvent:
        def __init__(
            self, message=(), platform="aiocqhttp", raw_message=None, bot=None
        ):
            self.message_obj = _MessageObj(message)
            self.message_obj.raw_message = raw_message
            self.message_obj.message_id = None
            self.platform = platform
            self.bot = bot
            self._extras = {}

        def get_platform_name(self):
            return self.platform

        def get_extra(self, key=None, default=None):
            return self._extras.get(key, default)

        def set_extra(self, key, value):
            self._extras[key] = value

    class _SttReq:
        def __init__(self, audio_urls=(), parts=()):
            self.audio_urls = list(audio_urls)
            self.extra_user_content_parts = list(parts)

    stt_plugin = mod.MediaMontagePlugin(
        context=None,
        config={"audio_mode": "llonebot_stt", "enable_video_montage": False},
    )
    check(
        "audio_mode 归一化（默认 off / 合法值 / 非法回退 off）",
        stt_plugin.audio_mode == "llonebot_stt"
        and mod.MediaMontagePlugin(context=None, config={}).audio_mode == "off"
        and mod.MediaMontagePlugin(
            context=None, config={"audio_mode": "bogus"}
        ).audio_mode
        == "off",
    )

    # 直接发送的语音 -> 追加转写文字
    bot = _SttBot("这是直接语音")
    event = _SttEvent(
        [CoreRecord(file="voice.amr")],
        raw_message={"message_id": 123},
        bot=bot,
    )
    req = _SttReq(
        audio_urls=["/tmp/voice.silk"],
        parts=[TextPart(text="[Audio Attachment: path /tmp/voice.silk]")],
    )
    await stt_plugin.on_llm_request(event, req)
    check(
        "直接语音调用 voice_msg_to_text（参数结构与 mimo_media 一致）",
        bot.stt_calls == [{"action": "voice_msg_to_text", "message_id": 123}],
        str(bot.stt_calls),
    )
    check(
        "转写文字追加且音频标签移除、audio_urls 清空",
        [getattr(p, "text", "") for p in req.extra_user_content_parts]
        == ["这是直接语音"]
        and req.audio_urls == [],
    )

    # 引用消息里的语音 -> 替换 [Empty Text]，用被引用消息 id 转写
    bot = _SttBot("这是引用语音转写")
    event = _SttEvent(
        [CoreReply(id=456, chain=[CoreRecord(file="quote.amr")])],
        raw_message={"message_id": 999},
        bot=bot,
    )
    req = _SttReq(
        parts=[
            TextPart(
                text="<Quoted Message>\n(某人): [Empty Text]\n</Quoted Message>"
            ),
            TextPart(
                text="[Audio Attachment in quoted message: path /tmp/quote.silk]"
            ),
        ]
    )
    await stt_plugin.on_llm_request(event, req)
    check(
        "引用语音用被引用消息 id 转写",
        bot.stt_calls == [{"action": "voice_msg_to_text", "message_id": 456}],
        str(bot.stt_calls),
    )
    texts = [getattr(p, "text", "") for p in req.extra_user_content_parts]
    check(
        "引用占位 [Empty Text] 被转写文字替换",
        texts[0] == "<Quoted Message>\n(某人): 这是引用语音转写\n</Quoted Message>",
        texts[0],
    )
    check(
        "引用语音附件标签被移除",
        all("Audio Attachment" not in text for text in texts),
        str(texts),
    )

    # 仅有 reply id（链内无媒体）-> 先 get_msg 拉原文再转写
    bot = _SttBot(
        "远程引用语音",
        payloads={
            "321": {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "message": [{"type": "record", "data": {"file": "reply.amr"}}]
                },
            }
        },
    )
    event = _SttEvent(
        [CoreReply(id=321, chain=[])],
        raw_message={"message_id": 100},
        bot=bot,
    )
    req = _SttReq(
        parts=[
            TextPart(text="<Quoted Message>\n(u): [Empty Text]\n</Quoted Message>")
        ]
    )
    await stt_plugin.on_llm_request(event, req)
    check(
        "reply-id-only 引用语音经 get_msg 解析后转写",
        bot.stt_calls == [{"action": "voice_msg_to_text", "message_id": 321}],
        str(bot.stt_calls),
    )
    check(
        "get_msg 已被调用",
        bool(bot.api.calls) and bot.api.calls[0][0] == "get_msg",
        str(bot.api.calls),
    )
    check(
        "远程转写替换占位",
        "远程引用语音" in getattr(req.extra_user_content_parts[0], "text", ""),
    )

    # 合并转发内部的语音不收集
    node = CoreNode(content=[CoreRecord(file="in-forward.amr")])
    event = _SttEvent(
        [CoreNodes([node])], raw_message={"message_id": 77}, bot=_SttBot("不应转写")
    )
    check(
        "合并转发内部语音不收集",
        stt_plugin._collect_audio_targets(event) == [],
    )

    # 转写失败（空文本）-> 附加失败提示，且不发送音频
    bot = _SttBot("")
    event = _SttEvent(
        [CoreRecord(file="v.amr")], raw_message={"message_id": 9}, bot=bot
    )
    req = _SttReq(
        audio_urls=["/tmp/c.silk"],
        parts=[TextPart(text="[Audio Attachment: path /tmp/c.silk]")],
    )
    await stt_plugin.on_llm_request(event, req)
    texts = [getattr(p, "text", "") for p in req.extra_user_content_parts]
    check(
        "转写失败附加提示且不发送音频",
        texts == ["[语音转文字失败，已跳过]"] and req.audio_urls == [],
        str(texts),
    )

    # 与视频拼图共存：视频官方标签必须保留
    bot = _SttBot("语音内容")
    event = _SttEvent(
        [CoreRecord(file="v.amr")], raw_message={"message_id": 11}, bot=bot
    )
    req = _SttReq(
        parts=[
            TextPart(text="[Video Attachment: name a.mp4, path /tmp/a.mp4]"),
            TextPart(text="[Audio Attachment: path /tmp/v.silk]"),
        ]
    )
    await stt_plugin.on_llm_request(event, req)
    texts = [getattr(p, "text", "") for p in req.extra_user_content_parts]
    check(
        "视频官方标签保留、音频标签移除",
        texts[0].startswith("[Video Attachment:")
        and all("Audio Attachment" not in text for text in texts),
        str(texts),
    )

    # 非 OneBot11 平台：完全不触发，保持原有音频行为
    bot = _SttBot("不应被调用")
    event = _SttEvent(
        [CoreRecord(file="voice.amr")],
        platform="telegram",
        raw_message={"message_id": 1},
        bot=bot,
    )
    req = _SttReq(
        audio_urls=["/tmp/a.silk"],
        parts=[TextPart(text="[Audio Attachment: path /tmp/a.silk]")],
    )
    await stt_plugin.on_llm_request(event, req)
    check(
        "非 onebot11 平台不触发转写",
        bot.stt_calls == [],
        str(bot.stt_calls),
    )
    check(
        "非 onebot11 平台保持原有音频行为",
        req.audio_urls == ["/tmp/a.silk"]
        and [getattr(p, "text", "") for p in req.extra_user_content_parts]
        == ["[Audio Attachment: path /tmp/a.silk]"],
    )
    # 平台名缺失的普通事件对象同样安全（视为非 onebot11）
    await stt_plugin.on_llm_request(_Event(), _SttReq())

    # audio_mode=off：即使在 onebot11 平台也不处理语音
    off_plugin = mod.MediaMontagePlugin(context=None, config={})
    bot = _SttBot("off 模式不应调用")
    event = _SttEvent(
        [CoreRecord(file="v.amr")], raw_message={"message_id": 5}, bot=bot
    )
    req = _SttReq(audio_urls=["/tmp/b.silk"])
    await off_plugin.on_llm_request(event, req)
    check(
        "audio_mode=off 时不处理语音",
        bot.stt_calls == [] and req.audio_urls == ["/tmp/b.silk"],
    )

    # --- 4. 官方标签解析 ---------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        video_path = Path(tmp) / "test.mp4"
        if subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0:
            print("[info] 未找到 ffmpeg，跳过视频端到端检查。")
            print("\nALL CHECKS PASSED")
            return

        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
                "-pix_fmt", "yuv420p", str(video_path),
            ],
            check=True,
        )
        duration = await plugin._probe_duration(str(video_path))
        check(
            "ffprobe 读出时长（秒）",
            bool(duration) and 1.5 < duration < 2.5,
            str(duration),
        )

        montage_path = await plugin._build_video_montage(str(video_path))
        check("视频拼图生成成功", bool(montage_path) and Path(montage_path).is_file())
        if montage_path:
            vm = Image.open(montage_path)
            check("视频拼图为 3x3", vm.size == (960, 720), str(vm.size))

        # 长视频：验证 seek 抽帧（时长已知时走按时间点路径）
        import time as _time

        long_video = Path(tmp) / "long.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i",
                "testsrc=duration=30:size=320x240:rate=5",
                "-pix_fmt", "yuv420p", str(long_video),
            ],
            check=True,
        )
        t0 = _time.time()
        long_frames = await plugin._extract_video_frames(str(long_video))
        elapsed = _time.time() - t0
        check(
            "长视频（30s）按 seek 抽满 9 帧",
            len(long_frames) == plugin.video_frames,
            f"got {len(long_frames)} in {elapsed:.1f}s",
        )
        check(
            "长视频帧取自不同时间点（首尾帧不同）",
            long_frames[0] != long_frames[-1],
        )
        long_montage = await plugin._build_video_montage(str(long_video))
        check(
            "长视频拼图生成成功",
            bool(long_montage) and Path(long_montage).is_file(),
        )

        from astrbot.core.agent.message import TextPart

        check(
            "官方标签正则匹配普通视频标签",
            mod._collect_official_video_refs(
                _Req(parts=[TextPart(
                    text=f"[Video Attachment: name {video_path.name}, path {video_path}]"
                )])
            )
            == [(video_path.name, str(video_path))],
        )
        check(
            "官方标签正则匹配引用消息视频标签",
            mod._collect_official_video_refs(
                _Req(parts=[TextPart(
                    text=(
                        "[Video Attachment in quoted message: "
                        f"name {video_path.name}, path {video_path}]"
                    )
                )])
            )
            == [(video_path.name, str(video_path))],
        )
        check(
            "官方标签正则不匹配 ref-only 标签（引用视频未落地）",
            mod._collect_official_video_refs(
                _Req(parts=[TextPart(
                    text="[Video Attachment in quoted message: name a.mp4, ref http://x/a.mp4]"
                )])
            )
            == [],
        )
        check(
            "官方标签正则不匹配普通文本",
            mod._collect_official_video_refs(
                _Req(parts=[TextPart(text="[File Attachment: name a, path b]")])
            )
            == [],
        )

        # --- 5. 端到端：官方标签 -> 拼图 -> 提示 ----------------------------
        event = _Event()
        req = _Req(parts=[TextPart(
            text=f"[Video Attachment: name {video_path.name}, path {video_path}]"
        )])
        converted = await plugin._attach_video_montages(req, event)
        check("视频名被正确返回", converted == [video_path.name], str(converted))
        check("拼图被加入 image_urls", len(req.image_urls) == 1)
        check("已登记临时文件", len(event.tracked) == 1)

        await plugin.on_llm_request(event, req)
        notices = [
            p
            for p in req.extra_user_content_parts
            if getattr(p, "text", "").startswith("<system_notice>")
        ]
        check("提示已追加", len(notices) == 1)
        if notices:
            text = notices[0].text
            check("提示包含视频名", video_path.name in text)
            check("提示被标记为临时内容（不落库）", notices[0]._no_save is True)
            check(
                "提示首尾正确",
                text.startswith("<system_notice>\n")
                and text.endswith("\n</system_notice>"),
            )

        # 消息链里有视频但无官方标签 -> 专版不做兜底，什么都不做
        from astrbot.core.message.components import Video as CoreVideo

        video_comp = CoreVideo.fromFileSystem(str(video_path))
        event2 = _Event([video_comp])
        req2 = _Req()
        converted2 = await plugin._attach_video_montages(req2, event2)
        check(
            "无官方标签时不做消息链兜底（旧版功能已剔除）",
            converted2 == [] and req2.image_urls == [],
        )

        # 只有文本、无视频 -> 不产生任何东西
        event3 = _Event()
        req3 = _Req(parts=[TextPart(text="hello")])
        await plugin.on_llm_request(event3, req3)
        notices3 = [
            p
            for p in req3.extra_user_content_parts
            if getattr(p, "text", "").startswith("<system_notice>")
        ]
        check(
            "无视频时不产生提示",
            len(req3.extra_user_content_parts) == 1 and not notices3,
        )

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
