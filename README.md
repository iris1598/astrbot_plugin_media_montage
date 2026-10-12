# astrbot_plugin_media_montage_429

AstrBot **4.29+ 专版**的「视频抽帧拼图」插件。零注入、零核心修改，只用官方钩子。

## 与通用版（astrbot_plugin_media_montage）的区别

| | 通用版 | 本专版 |
| --- | --- | --- |
| 适配版本 | 4.28.x ~ 4.29+ | **仅 4.29+**（加载时校验） |
| 动图拼图提示补丁 | 有（仅 4.28.1/4.28.2） | **无**（官方修复已接管） |
| 核心函数包装（注入） | 有（仅在 4.28.x 生效） | **无，任何情况下都不修改核心** |
| 视频解析 | 官方标签优先 + 消息链兜底 | **只解析官方标签** |
| 配置项 | 10 项 | 8 项（剔除动图相关） |

二选一安装即可；两者同时安装也不冲突（插件名不同），但功能会重复。

## 它解决什么问题

AstrBot 4.29 里，消息中的视频对模型只是一行文本
`[Video Attachment: name xxx.mp4, path /tmp/xxx.mp4]`，模型看不到任何画面。

核心在 `on_llm_request` 钩子**之前**就把视频落地为本地文件并写入这个官方标签。
本插件解析这些标签，用 ffmpeg 对视频等间隔抽帧，套用与官方动图完全相同的
**3×3 拼图逻辑**合成一张图，追加进 `req.image_urls`；钩子之后核心的
`prepare_request_images` 统一完成图片准备与标注。插件再按官方 montage notice
的风格附加一条 `<system_notice>`（临时内容，不写入会话历史），并附上被转换的
视频名，让模型能把拼图和 `[Video Attachment]` 标签对应起来。

## 安装

1. 把整个 `astrbot_plugin_media_montage_429` 目录放进 `AstrBot/data/plugins/`。
2. 在 WebUI「插件」页面点击插件卡片的刷新（重载）图标。
3. 依赖：`Pillow`（AstrBot 自带）。视频抽帧需要系统安装 **ffmpeg** 并在 PATH 中
   —— AstrBot 自身的视频格式转换本来也依赖它。
4. 版本不满足（< 4.29.0）时插件会自动停用并在日志里给出明确提示。

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

## 诊断

管理员发送 `/montage_status`，会输出当前 AstrBot 版本是否满足要求、
视频拼图参数与解析来源。

## 实现要点

1. **零注入**：不 import、不包装、不替换核心的任何函数；唯一的介入点是
   官方 `on_llm_request` 钩子，随插件卸载即完全还原。
2. **只信官方标签**：视频来源唯一途径是核心写好的
   `[Video Attachment: name X, path Y]` 标签；引用视频无法落地时核心只保留
   `ref`（无 `path`），该标签不会被匹配，插件跳过——属于预期行为。
3. **拼图与官方动图同构**：3×3 网格、白底、帧按阅读顺序、逐格 `LANCZOS`
   缩放、整体最长边不超过 `max_size`。
4. **提示的生命周期与官方一致**：`TextPart(...).mark_as_temp()` 标记为
   本轮临时内容，不写入会话历史。
5. **失败绝不打断主流程**：抽帧、合成、探测全部包在 try/except 中，
   任何一步失败都只是退化为原有行为（视频对模型不可见）。

## 许可

AGPL-3.0-or-later（与 AstrBot 本体一致）。
