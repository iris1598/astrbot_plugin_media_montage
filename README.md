# astrbot_plugin_media_montage

把「动图抽帧拼图」这件事做完整：既补回官方漏掉的提示，也让视频走同一条链路。

## 它解决什么问题

AstrBot 从 **4.28.1** 起，内置 Agent 会把消息里的动图（GIF / APNG / 动态 WebP）
转成一张 **3×3 的抽帧拼图** 再交给模型 —— 因为绝大多数模型接口不接受动图。

问题在于：**拼图是一张静态图，模型并不知道它是动图。** 于是模型会把九宫格
当成「一张奇怪的图」或「九张并排的图」，经常答非所问。

官方修复（就是那个 `prepare_model_image` 返回 `(path, is_montage)` 的补丁）
会额外追加一条临时提示告诉模型：

```
<system_notice>
The input includes a GIF converted into a single image with frames in reading order.
Treat it as an animation; do not mention the conversion or frame layout.
</system_notice>
```

**但这个修复没有进入 4.28.1 和 4.28.2 的发行版**（`4.28.2` 的更新日志只有
SQLite 时间字段相关修复）。本插件用插件的形式把它补回来。

顺带解决第二件事：**视频目前对模型完全不可见**。核心只会往请求里塞一行
`[Video Attachment: name xxx.mp4, path /tmp/xxx.mp4]`，模型看不到任何画面。
本插件用 ffmpeg 对视频等间隔抽帧，套用**与动图完全相同的 3×3 拼图逻辑**
合成一张图，追加进 `req.image_urls`，并附上同样的提示。

## 生效范围

| 能力 | 生效版本 |
| --- | --- |
| 动图拼图提示补丁 | **仅** AstrBot `4.28.1` / `4.28.2` |
| 视频抽帧拼图 | **所有** AstrBot 版本 |

- 为什么限制动图提示的版本？< 4.28.1 根本没有拼图机制，加提示会误导模型；
  > 4.28.2 应当由官方修复接管，插件主动让位，避免重复提示。
- **额外兜底：核心已修复时自动让位。** 插件加载时会检查
  `image_input.prepare_request_images` 的源码里是否已经出现 `has_montage` /
  `<system_notice>`。若已出现（例如某些基于 4.28.1 的、已经带上官方修复的分支构建，
  或者将来官方把修复合回了 4.28.x），插件会跳过动图补丁，避免同一条提示被发两遍。
  这个状态可以用 `/montage_status` 查看（`官方已修复: 是/否`）。
- 视频抽帧不依赖核心的拼图机制（插件自己抽帧、自己合成），因此不设版本限制，
  也**不受上面这两个开关影响**。

## 安装

1. 把整个 `astrbot_plugin_media_montage` 目录放进 `AstrBot/data/plugins/`。
2. 在 WebUI「插件」页面点击插件卡片的刷新（重载）图标。
3. 依赖：`Pillow`（AstrBot 自带）。视频抽帧需要系统安装 **ffmpeg** 并在 PATH 中
   —— AstrBot 自身的视频格式转换本来也依赖它。
4. 动图提示补丁是进程级的函数包装，**改动代码后必须重载插件**才能生效/撤销。

## 配置

WebUI 插件配置页可调，详见 `_conf_schema.json`。常用项：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enable_gif_notice` | `true` | 动图提示补丁开关（仅 4.28.1/4.28.2 有效） |
| `gif_notice_text` | 官方文案 | 提示正文，会包在 `<system_notice>` 里 |
| `enable_video_montage` | `true` | 视频抽帧拼图开关 |
| `enable_video_notice` | `true` | 视频拼图是否附加提示 |
| `video_grid` | `3` | 拼图网格边长，默认与动图一致的 3×3 |
| `video_frames` | `0` | 0 = 自动（网格边长²），默认 9 帧 |
| `video_max_size` | `1280` | 拼图最长边，只缩不放 |
| `video_quality` | `95` | JPEG 质量 |
| `video_extract_timeout` | `30` | 单次抽帧超时（秒） |

## 诊断

管理员发送 `/montage_status`，会输出当前 AstrBot 版本、动图补丁是否生效、
已挂钩的模块、视频拼图参数，以及已记录的拼图数量。用来快速确认版本门控是否符合预期。

## 实现要点

1. **精确判断「本轮到底有没有生成拼图」**：核心在
   `image_input.prepare_request_images()` 里调用 `media_utils.prepare_model_image()`，
   只有帧数 > 1 才走拼图分支。插件在加载时包装该函数
   （`media_utils` 和 `image_input` 两处绑定都要替换），记录
   `产出路径 → 是否为拼图`；钩子里只需检查 `req.image_urls` 是否命中记录，
   与官方补丁的 `has_montage` 语义完全一致 —— 包括缓存命中的拼图。
2. **视频复用同一套拼图参数**：3×3 网格、白底、帧按阅读顺序、逐格 `LANCZOS`
   缩放、整体最长边不超过 `max_size`，与核心 `_extract_animation_montage_sync` 同构。
3. **提示的生命周期与官方一致**：通过 `TextPart(...).mark_as_temp()` 标记为
   本轮临时内容，不写入会话历史（旧版本无此 API 时自动降级）。
4. **插件在钩子里追加的图片会被核心二次处理**：核心在 `on_llm_request` 之后
   还会再跑一次图片准备，所以插件只需把拼图路径塞进 `req.image_urls`，
   缩放/编码/上传策略仍由核心统一负责。
5. **失败绝不打断主流程**：抽帧、合成、探测全部包在 try/except 中，
   任何一步失败都只是退化为原有行为（视频对模型不可见）。

## 许可

AGPL-3.0-or-later（与 AstrBot 本体一致）。
提示文案 `gif_notice_text` 的默认值取自 AstrBot 官方修复的实现。
