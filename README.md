![:name](https://count.getloli.com/@astrbot_plugin_meme_api_python?name=astrbot_plugin_meme_api_python&theme=green&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto&prefix=0)

# astrbot_plugin_meme_api_python

用于 AstrBot 的 meme-generator 辅助插件，包含两部分能力：

- 从多个表情包仓库拉取/更新数据，并重启 meme-generator 容器。
- 调用 meme-generator API，提供表情列表、详情、随机表情和制作表情能力。

## 前置要求

1. 已部署并能访问 meme-generator API，默认地址为 `http://127.0.0.1:2233`。
2. `更新表情包` 和 `重启memeapi` 依赖 Docker 部署的 meme-generator，插件会在更新后执行 `docker restart <容器名>` 重启容器。
3. 如果使用本地更新模式，AstrBot 运行环境需要能执行 `git` 和 `docker restart <容器名>`。
4. 如果使用远程更新模式，AstrBot 运行环境需要能通过 `ssh` 登录远程服务器。
5. QQ 头像和 QQ 图片下载依赖外网访问，网络不通时会影响自动补头像或引用图片生成。

## 配置说明

主要配置项在 AstrBot 插件配置页面中填写：

| 配置项 | 说明 |
| --- | --- |
| `repo_list` | 表情包仓库列表，每项包含仓库地址和数据目录。 |
| `docker_container` | meme-generator 容器名，默认 `meme-generator`。 |
| `meme_api_base_url` | meme-generator API 地址，默认 `http://127.0.0.1:2233`。 |
| `meme_request_timeout` | meme API 请求超时时间，复杂表情可适当调大。 |
| `meme_max_image_mb` | 外部图片下载大小限制，默认 10MB。 |
| `meme_info_concurrency` | 刷新表情详情时的并发数。 |
| `meme_refresh_verbose_log` | 是否输出每个表情详情的刷新日志。 |
| `meme_disabled_keys` | 屏蔽指定表情，可填写 meme key 或中文关键词。 |
| `meme_search_limit` | `meme搜索` 最多返回的结果数量。 |
| `meme_search_forward_enabled` | `meme搜索` 是否优先使用合并消息发送结果。 |
| `meme_usage_stats_limit` | `表情统计` 图片最多展示的表情数量。 |
| `meme_usage_stats_title` | `表情统计` 图片顶部标题。 |
| `meme_shortcut_enabled` | 是否启用表情快捷指令匹配。 |
| `meme_poke_random_enabled` | 是否启用戳一戳机器人时发送随机表情。 |
| `meme_auto_default_texts` | 未提供文字时是否使用 meme API 返回的默认文字。 |
| `meme_auto_sender_avatar` | 图片数量不足时是否自动补当前发送者头像。 |
| `meme_list_text_template` | 表情列表渲染模板，默认 `{index}. {keywords}`。 |
| `meme_list_sort_by` / `meme_list_sort_reverse` | 表情列表排序方式。 |
| `remote_enabled` | 是否使用远程服务器执行更新和重启。 |
| `remote_*` | 远程 SSH 登录和工作目录配置。 |

## 指令

### 更新和状态

- `更新表情包`：拉取所有配置仓库，更新完成后重启 meme-generator，并刷新表情信息。（管理员指令）
- `重启memeapi`：只重启 meme-generator 容器。（管理员指令）
- `表情包状态`：查看仓库安装状态和 meme API 加载状态。（管理员指令）
- `刷新表情信息`：重新从 meme API 拉取表情元数据。（管理员指令）

### 表情功能

- `表情列表`：生成表情列表图片。
- `表情详情 <表情名/关键词>`：查看某个表情的关键词、参数数量和预览图。
- `meme搜索 <关键词>`：搜索表情，支持按 meme key、关键词、标签和快捷句式匹配；开启合并消息时会以合并消息展示结果。
- `表情统计`：查看当前群组（或私聊全局）的表情调用次数统计图。
- `总表情统计`：查看所有群组汇总的表情调用次数统计图。
- `制作表情 <表情名/关键词> [文字/@自己/@QQ号/图片URL...]`：手动制作表情。
- `随机表情 [文字/@自己/@QQ号/图片URL...]`：随机选择一个符合参数数量的表情。

开启 `meme_poke_random_enabled` 后，用户戳一戳机器人也会发送一张随机表情。

开启快捷指令后，可以直接发送 meme API 返回的关键词或快捷句式，例如：

```text
看看你的
摸 @某人
制作表情 看看你的 @自己
随机表情
```

具体可用关键词以 `表情列表` 和 `表情详情` 输出为准。快捷指令支持常见方向参数，例如 `#左`、`#右`、`#上`、`#下`，也会识别部分表情自己的方向句式；方向是否生效取决于对应 meme 本体是否支持该参数。

## 使用补充

### 搜索表情

发送 `meme搜索 <关键词>` 可以快速查找表情。搜索结果数量由 `meme_search_limit` 控制；开启 `meme_search_forward_enabled` 时会优先使用合并消息发送，外层标题显示“表情搜索结果”，内容第一行显示本次结果数量。

### 屏蔽表情

在 `meme_disabled_keys` 中填写 meme key 或中文关键词，可以让指定表情从列表、详情、随机表情和快捷指令中隐藏。适合屏蔽不想被随机到或不希望用户直接触发的表情。

也可以使用管理员指令动态维护屏蔽列表：

- `屏蔽表情 <表情名/关键词/key>`：屏蔽指定表情，配置中优先保存该表情的文字关键词，便于在配置页查看。
- `取消屏蔽表情 <表情名/关键词/key>`：取消屏蔽指定表情，支持用 key 或任意关键词匹配。
- `屏蔽表情列表`：以图片形式展示当前屏蔽列表，优先显示文字关键词并隐藏 emoji。

### 表情统计

每次成功生成表情后，插件会按 meme key 记录一次调用次数。发送 `表情统计` 可以查看统计图，发送 `总表情统计` 可以查看全局汇总的统计图。统计从功能启用后开始累计，不会回溯历史消息；展示名称会优先使用当前 meme API 返回的第一个关键词。

### 图片来源

制作表情时支持以下图片来源：

- 当前消息中的图片或表情图片。
- 引用消息中的图片或表情图片。
- `@用户`、`@自己` 或手写 `@QQ号` 自动取 QQ 头像。
- 直接填写 `http://` / `https://` 图片地址。

外部图片下载会校验地址并受 `meme_max_image_mb` 限制，超过大小或无法访问时会生成失败。

## 远程更新模式

开启 `remote_enabled` 后，`更新表情包` 和 `重启memeapi` 会通过 SSH 到远程服务器执行。请确认：

- `remote_host`、`remote_user`、`remote_port` 填写正确。
- `remote_workdir` 是远程 memeapi 根目录。
- 远程服务器已安装 `git`、`docker`，并且当前用户有权限执行容器重启。
- 使用密码登录时填写 `remote_password`；使用私钥登录时确保运行 AstrBot 的系统用户能正常 SSH 登录。
- 推荐优先使用私钥登录；密码登录依赖本机 SSH/askpass 能力，且凭据暴露面更大。

## 常见问题

### 表情列表或制作表情失败

先执行 `表情包状态`，确认 meme API 地址可访问且已加载表情。如果 API 未启动，执行 `重启memeapi`。

### 生成复杂表情超时

适当调大 `meme_request_timeout`。

### @ 用户头像没有传入

请使用平台原生 @，不要只手打昵称。手动传 QQ 号时可写 `@123456789`。

### 搜索结果没有用合并消息显示

确认 `meme_search_forward_enabled` 已开启。部分平台或适配器不支持合并消息发送，插件会自动回退为普通文本。

### 引用图片没有识别

确认引用的是包含图片或表情图片的消息。某些平台只给缩略图或临时下载地址，过期后可能下载失败。
