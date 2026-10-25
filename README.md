# astrbot_plugin_media_montage_429

AstrBot **4.29+ 专版**的媒体理解插件：**视频**以「抽帧拼图」的方式被模型看懂，
**OneBot11 语音**可直接转写成文字交给模型。零注入、零核心修改，只用官方钩子。

## 功能一：视频抽帧拼图

AstrBot 4.29 里，消息中的视频对模型只是一行文本
`[Video Attachment: name xxx.mp4, path /tmp/xxx.mp4]`，模型看不到任何画面。

核心在 `on_llm_request` 钩子**之前**就把视频落地为本地文件并写入这个官方标签。
本插件解析这些标签，用 ffmpeg 对视频等间隔抽帧，套用与官方动图完全相同的
**3×3 拼图逻辑**合成一张图，追加进 `req.image_urls`；钩子之后核心的
`prepare_request_images` 统一完成图片准备与标注。插件再按官方 montage notice
的风格附加一条 `<system_notice>`（临时内容，不写入会话历史），并附上被转换的
视频名，让模型能把拼图和 `[Video Attachment]` 标签对应起来。

## 功能二：OneBot11 语音转文字

`audio_mode=llonebot_stt` 时，对 OneBot11 消息里的语音调用协议端的
`voice_msg_to_text` 扩展接口（llonebot 5.1+，入参 `message_id`，返回
`data.text`），把转写文字注入本轮 LLM 请求，同时清空 `req.audio_urls` 并移除
核心生成的 `[Audio Attachment]` 标签——模型收到的是文字，而不是它多半无法
处理的音频文件。

- 直接发送的语音 → 转写文字追加为普通文本块；
- 引用消息里的语音 → 转写文字替换引用占位文本里的 `[Empty Text]`；被引用
  消息只有 reply id 时，会先经 `get_msg` 拉取原文再转写；
- 合并转发内部的语音不解析；转写失败时附加 `[语音转文字失败，已跳过]` 提示，
  不打断主流程；
- 通过 `event.get_platform_name() == "aiocqhttp"` 严格限定在 OneBot11 适配器，
  其他适配器（telegram/discord/webchat 等）完全保持原有音频行为。

## 安装

1. 把本插件目录放进 `AstrBot/data/plugins/`。
2. 在 WebUI「插件」页面点击插件卡片的刷新（重载）图标。
3. 依赖：`Pillow`（AstrBot 自带）。视频抽帧需要系统安装 **ffmpeg** 并在 PATH 中
   —— AstrBot 自身的视频格式转换本来也依赖它。
4. 语音转文字需要协议端支持 llonebot 5.1+ 的 `voice_msg_to_text` 扩展接口。
5. 版本不满足（< 4.29.0）时插件会自动停用并在日志里给出明确提示。

## 配置

WebUI 插件配置页可调，详见 `_conf_schema.json`。常用项：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enable_video_montage` | `true` | 视频抽帧拼图开关 |
| `enable_video_notice` | `true` | 视频拼图是否附加提示 |
| `video_notice_text` | 官方风格文案 | 提示正文，会包在 `<system_notice>` 里 |
| `video_grid` | `3` | 拼图网格边长，默认与动图一致的 3×3 |
| `video_frames` | `0` | 0 = 自动（网格边长²），默认 9 帧 |
| `video_max_size` | `1280` | 拼图最长边，只缩不放 |
| `video_quality` | `95` | JPEG 质量 |
| `video_extract_timeout` | `30` | 单次抽帧超时（秒） |
| `audio_mode` | `off` | 语音处理模式：`off` 保持 AstrBot 原有行为；`llonebot_stt` 仅在 **OneBot11（aiocqhttp）** 适配器下把语音经 `voice_msg_to_text` 转成文字交给模型（不发送音频文件），其他适配器不触发 |

## 诊断

管理员发送 `/montage_status`，会输出当前 AstrBot 版本是否满足要求、
视频拼图参数与解析来源、语音转文字的启停状态。

## 实现要点

1. **零注入**：不 import、不包装、不替换核心的任何函数；唯一的介入点是
   官方 `on_llm_request` 钩子，随插件卸载即完全还原。
2. **只信官方标签**：视频来源唯一途径是核心写好的
   `[Video Attachment: name X, path Y]` 标签；引用视频无法落地时核心只保留
   `ref`（无 `path`），该标签不会被匹配，插件跳过——属于预期行为。
3. **按时间点 seek 抽帧**：时长已知时，把采样点均匀分布在整段时长上（取中点
   位置），逐个用 `ffmpeg -ss <t>` 输入 seek 抽取，每次只解码目标时间点附近的
   一个 GOP——长视频（数分钟、上百 MB）也能秒级完成；时长读不出时退回
   `fps` 滤镜全解码（短视频场景开销可忽略），seek 全部失败时也会退回全解码
   重试。
4. **拼图与官方动图同构**：3×3 网格、白底、帧按阅读顺序、逐格 `LANCZOS`
   缩放、整体最长边不超过 `max_size`。
5. **提示的生命周期与官方一致**：`TextPart(...).mark_as_temp()` 标记为
   本轮临时内容，不写入会话历史。
6. **语音转文字严格平台门控**：仅 `aiocqhttp` 适配器事件进入该分支，且默认
   `audio_mode=off`；不满足时对请求不做任何改动，保持 AstrBot 原有音频行为。
7. **失败绝不打断主流程**：抽帧、合成、探测、转写全部包在 try/except 中，
   任何一步失败都只是退化为原有行为。

## 许可

AGPL-3.0-or-later（与 AstrBot 本体一致）。
