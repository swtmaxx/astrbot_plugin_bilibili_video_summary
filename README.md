# Bilibili 视频总结

这是一个独立的 AstrBot 插件。用户不需要输入任何插件指令，只要在支持 Function Calling 的模型对话中明确要求总结 Bilibili 视频，提供 BV 号、AV 号或视频链接，模型就会自动调用本插件。

示例：

```text
请总结这个视频：https://www.bilibili.com/video/BVxxxx
```

插件会读取视频标题、简介、发布时间、总时长、所有分P信息和所有可用字幕，然后把**全部字幕一次性**交给配置的模型生成总结。插件不做分段总结、不截断字幕、不下载视频，也不执行 ASR。

## 配置

在 WebUI 配置：

- `provider_id`：用于生成总结的已启用模型提供商，模型需要支持 Function Calling。
- `sessdata`：可选的 Bilibili 登录 Cookie。可以填写纯 SESSDATA 值，也可以粘贴完整 Cookie；遇到 `-352`、`-412` 风控或受限字幕时配置有效值。
- `summary_prompt`：完整自定义提示词模板。插件不会额外追加固定 system prompt。
- `request_interval_seconds`：Bilibili 请求间隔。
- `request_timeout_seconds`：单次 Bilibili 请求超时，范围为 1 到 300 秒。
- `max_concurrent_jobs`：后台任务并发数，默认 1。

SESSDATA 属于敏感凭据，请只通过 WebUI 配置，不要写入日志、提示词或聊天消息。

## 提示词占位符

```text
{{video_bvid}}          视频 BV 号
{{video_aid}}           视频 AV 号
{{video_url}}           视频链接
{{video_title}}         视频标题
{{video_description}}   视频简介
{{video_published_at}}  投稿发布时间（UTC+08:00）
{{video_duration}}      视频总时长
{{video_metadata}}      视频元数据组合文本
{{video_parts}}         所有分P名称、CID、时长和字幕状态
{{part_count}}          分P数量
{{subtitle_count}}      字幕轨道数量
{{subtitles}}           所有分P字幕原文
{{user_request}}        用户的额外总结要求
```

未知占位符会保持原样。全部字幕只会放入一次模型请求；视频过长导致模型上下文不足或模型网关超时后，插件会明确提示失败，不会静默截断或改用分段总结。

## 工作流程

1. AstrBot 模型识别用户是否明确提出视频总结需求。
2. 模型调用 `summarize_bilibili_video` 工具并传入视频链接。
3. 插件在后台读取视频详情、分P和字幕，工具本身快速返回，避免长视频占用 AstrBot 工具调用超时。
4. 插件将总结进度发送到当前会话。
5. 插件使用 WebUI 指定模型生成一次总结，并将最终结果发送到当前会话。

只有明确的总结、概括、分析或提炼重点请求才应触发工具。单纯分享链接、询问视频信息或讨论 Bilibili 时不应调用。

## 限制

- 当前一次自然语言请求处理一个视频。
- 视频必须存在至少一个可读取的字幕轨道。
- 没有字幕时不会下载视频或自动语音识别。
- Bilibili 风控需要有效 SESSDATA；模型也必须有足够的上下文窗口容纳全部字幕。
- 插件不注册任何聊天指令。
