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
| `meme_api_base_url` | meme-generator API 地址。 |
| `meme_request_timeout` | meme API 请求超时时间，复杂表情可适当调大。 |
| `meme_info_concurrency` | 刷新表情详情时的并发数。 |
| `meme_refresh_verbose_log` | 是否输出每个表情详情的刷新日志。 |
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

- `更新表情包`：拉取所有配置仓库，更新完成后重启 meme-generator，并刷新表情信息。
- `重启memeapi`：只重启 meme-generator 容器。
- `表情包状态`：查看仓库安装状态和 meme API 加载状态。
- `刷新表情信息`：重新从 meme API 拉取表情元数据。

### 表情功能

- `表情列表`：生成表情列表图片。
- `表情详情 <表情名/关键词>`：查看某个表情的关键词、参数数量和预览图。
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

具体可用关键词以 `表情列表` 和 `表情详情` 输出为准。

## 远程更新模式

开启 `remote_enabled` 后，`更新表情包` 和 `重启memeapi` 会通过 SSH 到远程服务器执行。请确认：

- `remote_host`、`remote_user`、`remote_port` 填写正确。
- `remote_workdir` 是远程 memeapi 根目录。
- 远程服务器已安装 `git`、`docker`，并且当前用户有权限执行容器重启。
- 使用密码登录时填写 `remote_password`；使用私钥登录时确保运行 AstrBot 的系统用户能正常 SSH 登录。

## 常见问题

### 表情列表或制作表情失败

先执行 `表情包状态`，确认 meme API 地址可访问且已加载表情。如果 API 未启动，执行 `重启memeapi`。

### 生成复杂表情超时

适当调大 `meme_request_timeout`。

### @ 用户头像没有传入

请使用平台原生 @，不要只手打昵称。手动传 QQ 号时可写 `@123456789`。

### 引用图片没有识别

确认引用的是包含图片或表情图片的消息。某些平台只给缩略图或临时下载地址，过期后可能下载失败。
