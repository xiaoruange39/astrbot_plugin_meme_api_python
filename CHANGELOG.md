# 更新日志


## 0.2.8

- Added an opt-in autonomous LLM meme workflow with a plugin configuration switch.
- Candidate requests sample a configurable number of visible, non-blocked templates.
- The AI receives template keywords, tags, image/text constraints, and default text before rendering.
- Candidate data is returned only to the Agent instead of being sent to chat.
- The AI can decide whether to reply with text, generate a meme, do both, or do nothing when no meme fits.
- Generated memes can be sent as QQ small-image stickers with configurable external summary text.
- Current-message and replied-message images are resolved from raw AstrBot/OneBot events, avoiding unusable model-side temporary media URLs when possible.
- A short-lived per-message lock prevents repeated LLM meme generation in recursive tool loops.
- AI generation trims excess images, ignores unavailable explicit image inputs in the LLM path, and keeps validation errors internal.

## 0.2.7

- 新增 `meme_list_render_timeout` 配置项：表情列表图渲染单独使用更长超时（默认 60 秒，且不会小于 `meme_request_timeout`），缓解表情库较大时列表渲染超时。
- 修复用引号包裹的多词文字参数被拆开的问题：`制作表情`、快捷指令现在能正确把 `"hello world"` 当作一段文字，不再误报“文字数量不符”。
- `meme搜索`、`表情统计`、`总表情统计` 现在同样受群白名单约束，与 `表情列表` 等指令保持一致。
- 修复合并转发发送失败时的降级逻辑：`call_action` 调用失败后会继续尝试直接调用适配器方法，不再被提前跳过。
- 表情调用统计改为原子写入（先写临时文件再替换），避免写入中途崩溃/断电导致统计数据损坏或丢失。
- 临时图片清理改为常驻后台定时任务，长时间无人生成表情时旧文件也能按 TTL 回收，不再依赖下一次请求触发。
- 复用网络连接：meme API 请求与外部图片下载分别共享长生命周期会话，减少重复建连开销，并在插件卸载时统一关闭。
- 优化统计图/屏蔽列表图的阴影渲染（合成一次而非逐卡合成）；系统缺少中文字体时输出告警提示。

## 0.2.6

- 修复框架重启后第一条消息会阻塞所有消息收发的问题：表情信息改为插件加载时后台预热，未就绪时快捷指令监听直接放行，不再同步等待初始化、也不再拦截 `help` 等其它消息。
- 修复偶发拿不到他人头像的问题：下载校验拿不到实际连接 IP 时降级信任 DNS 校验结果并继续，仅在能拿到 IP 时做内网/一致性强校验。

## 0.2.5

- `制作表情` 命令对齐快捷指令的参数解析，现在同样支持 `#width=100`、`右/左/上/下` 等通用选项。
- `转` 表情方向随机，移除其无效的方向参数，避免误传给 API 报错。
- 快捷指令某个候选渲染失败时继续尝试后续候选，不再因首个候选异常而中断整条匹配链。
- 修复重启后刷新表情信息的重试文案数字不一致的问题。

## 0.2.4

- 修复屏蔽表情列表通过指令或 Plugin Page 修改后未持久化的问题，重启后不再丢失。
- 优化屏蔽/取消屏蔽/屏蔽列表指令，优先复用已加载的 meme 信息，避免每次重新拉取全部表情详情。
- 新增 `全局屏蔽表情列表` 指令，群聊中也可直接查看全局屏蔽列表。

## 0.2.3

- Plugin Page 入口新增“屏蔽列表”视图，可查看全局屏蔽和各群分群屏蔽。
- 新增 `全局屏蔽表情` 和 `取消全局屏蔽表情` 指令，群聊和私聊都可直接维护全局屏蔽。
- 修复全局屏蔽在群聊中不生效的问题，现在群聊会同时过滤全局屏蔽和当前群屏蔽。

## 0.2.2

- 新增 `全局屏蔽表情` 和 `取消全局屏蔽表情` 指令，群聊和私聊都可直接维护全局屏蔽。
- 全局屏蔽现在会和分群屏蔽一起生效：群聊会同时过滤全局屏蔽和当前群屏蔽。
- `屏蔽表情`、`取消屏蔽表情` 支持可选群号参数；群聊不带群号操作当前群，私聊不带群号操作全局屏蔽。
- `屏蔽表情列表` 群聊中展示当前群列表并在标题显示群名和群号，私聊中展示全局屏蔽列表；也支持传入群号查看指定群。

## 0.2.1

- 新增屏蔽表情管理指令：`屏蔽表情`、`取消屏蔽表情`、`屏蔽表情列表`。
- `屏蔽表情列表` 改为图片展示，样式与表情统计图保持一致。
- 屏蔽表情优先保存和展示文字关键词，而不是内部 meme key。
- 支持使用 key 或任意关键词取消屏蔽同一个表情。
- 支持使用 emoji 关键词触发屏蔽/取消屏蔽，列表图片中会优先展示文字关键词并隐藏 emoji。
- 修复已屏蔽表情再次执行屏蔽时提示“未找到”的问题，现在会正确提示已在屏蔽列表中。
- 为管理类指令增加管理员权限限制：`更新表情包`、`重启memeapi`、`表情包状态`、`刷新表情信息`、`屏蔽表情`、`取消屏蔽表情`、`屏蔽表情列表`。
- 拆分图片渲染逻辑到 `image_renderer.py`，降低 `main.py` 复杂度。
- 拆分屏蔽表情业务逻辑到 `disabled_memes.py`，便于后续维护。
