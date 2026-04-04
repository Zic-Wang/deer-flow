# WeChat Channel 实现说明

## 1. 文档目标

本文档面向两类读者：

- 需要在 DeerFlow 中维护/重构 WeChat channel 的开发者
- 需要把同类能力迁移到别的代码库，或让 AI 直接按文档复刻实现的人

目标不是介绍“怎么安装补丁包”，而是完整说明 **WeChat channel 运行时本身** 是如何工作的：

- 在 DeerFlow 架构中的位置
- 依赖的消息抽象和生命周期
- iLink 接口协议和状态机
- 文本、图片、文件的收发链路
- 认证、轮询、上下文、状态持久化
- 与 `ChannelManager`、上传目录、artifact 回传之间的配合
- 如果要从零重写，最少需要实现哪些能力

相关源码入口：

- 运行时实现：`backend/app/channels/wechat.py`
- Channel 抽象：`backend/app/channels/base.py`
- 消息总线：`backend/app/channels/message_bus.py`
- 调度器：`backend/app/channels/manager.py`
- 入站文件桥接：`backend/app/channels/inbound_uploads.py`
- 注册入口：`backend/app/channels/service.py`
- 示例配置：`config.example.yaml`

---

## 2. WeChat channel 在 DeerFlow 中的位置

DeerFlow 的 channel 系统本质上是一个“外部 IM 平台 <-> DeerFlow agent”桥接层。

整体分工如下：

1. `ChannelService` 读取 `config.yaml` 中的 `channels.*` 配置。
2. 它按 `_CHANNEL_REGISTRY` 实例化各个平台 channel。
3. 每个 channel：
   - 从外部平台收消息
   - 转成统一的 `InboundMessage`
   - 发布到 `MessageBus`
4. `ChannelManager` 从 `MessageBus` 取出 `InboundMessage`：
   - 查找或创建 DeerFlow thread
   - 组装 LangGraph 输入
   - 等待 agent 结果
   - 把结果转成 `OutboundMessage`
5. channel 订阅 `MessageBus` 的 outbound 事件，再把回复发回外部平台。

对应当前 WeChat 注册位置：

- `service.py` 中 `_CHANNEL_REGISTRY["wechat"] = "app.channels.wechat:WechatChannel"`
- `manager.py` 中 `CHANNEL_CAPABILITIES["wechat"] = {"supports_streaming": False}`

这说明当前 WeChat channel：

- **已经接入 DeerFlow 统一 channel 框架**
- **不走前端**，而是后端直接长轮询 iLink
- **当前不启用流式增量回复**，只发送最终结果

---

## 3. 必须先理解的公共抽象

### 3.1 `Channel`

`WechatChannel` 继承自 `Channel`，最少需要实现三个核心方法：

- `start()`：开始监听平台消息
- `stop()`：停止监听并清理资源
- `send(msg)`：把 DeerFlow 生成的回复发送回平台

此外还可选实现：

- `send_file(msg, attachment)`：上传单个附件
- `send_typing(chat_id, context_token)`：发送“正在输入”状态

`Channel` 基类已经处理了一个重要约束：

1. 先发送文本
2. 文本成功后，再依次发送附件
3. 如果文本发送失败，则完全跳过附件发送

这样可以避免“用户只收到文件、没收到说明文本”的部分投递问题。

### 3.2 `MessageBus`

`MessageBus` 是异步队列 + 回调分发器：

- 入站：`publish_inbound(InboundMessage)`
- 出站：`publish_outbound(OutboundMessage)`

WeChat channel 不直接调用 agent，而是只负责：

- 发布 inbound
- 监听 outbound

这让 channel 实现保持平台适配层的单一职责。

### 3.3 `InboundMessage` / `OutboundMessage`

WeChat channel 主要依赖这些字段：

#### `InboundMessage`

- `channel_name`：固定为 `wechat`
- `chat_id`：iLink 用户 ID
- `user_id`：当前实现里与 `chat_id` 相同
- `text`：用户文本
- `msg_type`：普通对话或命令
- `thread_ts`：当前平台线程标识，这里复用 `context_token` 或消息 ID
- `files`：入站附件元数据列表
- `metadata`：额外平台信息，如 `context_token`、`ref_msg`、`raw_message`

#### `OutboundMessage`

- `chat_id`：目标用户
- `thread_id`：DeerFlow thread ID
- `text`：要发送的文本
- `attachments`：已经解析到宿主机文件路径的附件
- `thread_ts`：回给平台时可用于恢复上下文
- `metadata`：可选透传上下文

---

## 4. `WechatChannel` 的核心职责

`WechatChannel` 做的事情可以分成 6 块：

1. **认证与状态恢复**
   - 使用现成 `bot_token`
   - 或通过二维码绑定获取 `bot_token`
   - 保存认证状态到磁盘
2. **长轮询收消息**
   - 调 iLink `getupdates`
   - 维护 `get_updates_buf`
3. **解析入站文本/媒体**
   - 文本从 `item_list` 中提取
   - 图片/文件从 CDN 拉取并解密后暂存到本地
4. **把消息发布给 DeerFlow**
   - 使用统一 `InboundMessage`
5. **发送出站文本/附件**
   - 文本：`sendmessage`
   - 图片/文件：`getuploadurl` -> CDN 上传 -> `sendmessage`
6. **维护平台上下文**
   - 记录 `context_token`
   - 发送 typing
   - 处理 token 过期与自动重绑

可以把它理解成一个“带状态的 iLink 协议适配器”。

---

## 5. 协议常量与数据模型

### 5.1 `MessageItemType`

当前实现使用的消息项类型：

- `TEXT = 1`
- `IMAGE = 2`
- `VOICE = 3`
- `FILE = 4`
- `VIDEO = 5`

当前 DeerFlow WeChat channel 实际重点支持的是：

- 文本
- 图片
- 文件

语音/视频常量已保留，但运行时未完整打通。

### 5.2 `UploadMediaType`

上传时使用的媒体类型：

- `IMAGE = 1`
- `VIDEO = 2`
- `FILE = 3`
- `VOICE = 4`

当前发送附件时实际用到：

- 图片 -> `IMAGE`
- 普通文件 -> `FILE`

### 5.3 `TypingStatus`

- `TYPING = 1`
- `CANCEL = 2`

当前实现只显式发送 `TYPING`。

---

## 6. 配置面：`channels.wechat`

当前实现从配置中读取以下字段。

### 6.1 必填/准必填

- `enabled`
- `bot_token`
- `ilink_bot_id`

说明：

- 运行时严格依赖的是 `bot_token`
- 但当 `bot_token` 缺失时，可以打开二维码登录能力
- `ilink_bot_id` 主要是绑定态信息的一部分，建议保留

### 6.2 网络与协议字段

- `base_url`
  - 默认：`https://ilinkai.weixin.qq.com`
- `cdn_base_url`
  - 默认：`https://novac2c.cdn.weixin.qq.com/c2c`
- `channel_version`
  - 默认：`1.0`
- `ilink_app_id`
  - 可选，写入请求头 `iLink-App-Id`
- `route_tag`
  - 可选，写入请求头 `SKRouteTag`

### 6.3 收消息轮询字段

- `polling_timeout`
  - 本地长轮询超时秒数，默认 `35`
- `polling_retry_delay`
  - 轮询失败后的重试间隔，默认 `5`
- `respect_server_longpoll_timeout`
  - 是否采用服务端返回的 `longpolling_timeout_ms`

### 6.4 认证/二维码字段

- `qrcode_login_enabled`
- `auto_rebind_on_expired`
- `qrcode_poll_interval`
- `qrcode_poll_timeout`
- `qrcode_bot_type`

### 6.5 本地状态字段

- `state_dir`
  - 用于保存：
    - `wechat-getupdates.json`
    - `wechat-auth.json`
    - 入站媒体下载目录 `downloads/`

### 6.6 权限与过滤字段

- `allowed_users`
  - 空数组代表放行所有用户
- `allowed_file_extensions`
  - 控制入站/出站普通文件类型

### 6.7 媒体大小限制

- `max_inbound_image_bytes`
- `max_outbound_image_bytes`
- `max_inbound_file_bytes`
- `max_outbound_file_bytes`

### 6.8 回复体验字段

- `chunked_reply_enabled`
- `chunked_reply_trigger_chars`
- `chunked_reply_max_chunk_chars`
- `chunked_reply_min_chunk_chars`
- `chunked_reply_max_chunks`
- `chunked_reply_interval_seconds`
- `typing_enabled`
- `typing_refresh_interval`（由 manager 读取，channel config 中可配）

### 6.9 DeerFlow session 覆盖字段

`channels.wechat.session` 不是 `WechatChannel` 自己消费的，而是 `ChannelManager` 消费的。它用于指定：

- 默认 agent
- 运行参数 `config`
- 运行上下文 `context`
- 针对某个 `user_id` 的精细化覆写

也就是说：

- `wechat.py` 负责“怎么收发微信消息”
- `manager.py` 负责“收到后把消息交给哪个 agent、用什么 session 跑”

---

## 7. 默认限制与内建策略

### 7.1 默认允许的文件扩展名

代码内置了一组较宽的扩展名白名单，覆盖：

- 文本文档：`.txt`、`.md`、`.log`
- 数据文件：`.csv`、`.json`、`.yaml`、`.yml`、`.xml`
- Office 文档：`.doc`、`.docx`、`.xls`、`.xlsx`、`.ppt`、`.pptx`
- 压缩包：`.zip`
- 代码与配置：`.py`、`.js`、`.ts`、`.tsx`、`.jsx`、`.java`、`.go`、`.rs`、`.c`、`.cpp`、`.h`、`.hpp`、`.sql`、`.sh`、`.bat`、`.ps1`、`.toml`、`.ini`、`.conf`

若配置中传入 `allowed_file_extensions`，会用配置值覆盖默认值。

### 7.2 默认允许的 MIME

普通文件除了检查扩展名，还会检查 MIME：

- `text/*` 直接允许
- 额外允许：PDF、JSON、XML、ZIP、YAML、CSV、Word、Excel、PPT、RTF 等

### 7.3 图片类型探测

入站图片解密后，会根据 magic bytes 识别：

- PNG
- JPEG
- GIF
- WEBP
- BMP

这一步用于：

- 给下载后的临时文件补正确扩展名
- 生成更可信的 `mime_type`

---

## 8. 生命周期：启动、停止、主循环

### 8.1 `start()`

启动流程：

1. 如果已在运行，直接返回
2. 如果既没有 `bot_token`，又没启用二维码登录，直接报错并返回
3. 记录当前事件循环
4. 创建 `state_dir`
5. 初始化 `httpx.AsyncClient`
6. 标记 `_running = True`
7. 订阅 bus 的 outbound 回调 `_on_outbound`
8. 创建后台轮询任务 `_poll_loop()`

### 8.2 `stop()`

停止流程：

1. `_running = False`
2. 取消 outbound 订阅
3. cancel 轮询任务
4. 关闭 HTTP client
5. 记录停止日志

### 8.3 `_poll_loop()`

这是 channel 的主接收循环，伪代码如下：

1. 保证已经认证
2. 调 `getupdates`
3. 检查 `ret/errcode`
4. 如果成功：
   - 更新服务端建议 long-poll timeout
   - 保存新的 `get_updates_buf`
   - 遍历 `msgs`
   - 逐条调用 `_handle_update()`
5. 如果失败：
   - 记录错误
   - sleep `retry_delay`

这是一个典型的“平台轮询适配器”结构。

---

## 9. 请求头与基础信息构造

### 9.1 公共请求头

`_common_headers()` 会生成：

- `iLink-App-ClientVersion`
- `X-WECHAT-UIN`
- 可选 `iLink-App-Id`
- 可选 `SKRouteTag`

其中：

- `iLink-App-ClientVersion` 由 `_build_ilink_client_version()` 把 `1.2.3` 变成整数编码字符串
- `X-WECHAT-UIN` 由 `_build_wechat_uin()` 生成一个随机值并 base64 编码

### 9.2 认证头

`_auth_headers()` 在公共头基础上再加：

- `Authorization: Bearer <bot_token>`
- `AuthorizationType: ilink_bot_token`

### 9.3 `base_info`

大部分请求体还会携带：

```json
{"channel_version": "1.0"}
```

当前由 `_base_info()` 返回。

这意味着如果要从零实现，除了 HTTP 头，**请求体里的 `base_info` 也不能漏**。

---

## 10. 认证与恢复机制

### 10.1 认证来源

当前实现支持两种方式：

1. **直接配置 `bot_token`**
2. **二维码绑定流程获取 `bot_token`**

### 10.2 `_ensure_authenticated()`

调用时机：

- 轮询前
- 发送文本前
- 发送图片前
- 发送文件前

逻辑顺序：

1. 如果已有 `bot_token` 且不是强制重绑，直接成功
2. 如果内存里没有 token，就尝试从 `wechat-auth.json` 加载
3. 如果仍然没有 token：
   - 若未启用二维码登录，则失败
   - 若启用了二维码登录，则进入 `_bind_via_qrcode()`

### 10.3 token 过期恢复

轮询 `getupdates` 如果返回 `errcode == -14`，当前实现认为 token 已失效：

1. 清空 `_bot_token`
2. 清空 `_get_updates_buf`
3. 保存状态
4. 把认证状态写为 `expired`
5. 如果 `auto_rebind_on_expired = true`，自动重新走二维码绑定

### 10.4 二维码绑定 `_bind_via_qrcode()`

流程：

1. 调公开接口 `/ilink/bot/get_bot_qrcode`
2. 拿到：
   - `qrcode`
   - 可选 `qrcode_img_content`
3. 把状态记为 `pending`
4. 轮询 `/ilink/bot/get_qrcode_status`
5. 若状态变成 `confirmed`：
   - 读取 `bot_token`
   - 可选读取 `ilink_bot_id`
   - 持久化到 `wechat-auth.json`
6. 若状态变成 `expired/canceled/invalid/failed`：报错退出
7. 超时则抛 `TimeoutError`

当前实现会把二维码内容打到日志里，因此部署方需要决定日志安全边界。

---

## 11. 状态持久化设计

### 11.1 游标状态：`wechat-getupdates.json`

保存字段：

- `get_updates_buf`

用途：

- 重启后继续从上次轮询位置拉消息
- 避免重复消费历史消息

### 11.2 认证状态：`wechat-auth.json`

保存字段可能包括：

- `status`
- `updated_at`
- `bot_token`
- `ilink_bot_id`
- `qrcode`
- `qrcode_img_content`

用途：

- 重启后恢复 token
- 保存二维码绑定过程中的中间状态

### 11.3 下载目录：`downloads/`

当接收到图片或文件时，channel 会先把解密后的原始内容写到：

- `<state_dir>/downloads/`

这些文件随后会交给 `stage_inbound_files()` 复制进 DeerFlow thread 的 upload 目录。

### 11.4 设计取舍

当前实现把三类内容都放在 `state_dir` 下：

- 轮询游标
- 认证状态
- 下载的入站媒体

好处：

- 目录集中
- 易于调试
- 与示例配置里的 runtime state 目录约定一致

风险：

- 若不做清理，下载目录可能逐渐变大
- token 与二维码信息属于敏感信息，应限制目录权限

---

## 12. 入站消息处理链路

### 12.1 `_handle_update(raw_message)` 的职责

该方法负责把 iLink 原始消息转成 `InboundMessage`。

处理顺序：

1. 只接受 `dict`
2. 只处理 `message_type == 1` 的入站用户消息
3. 解析 `chat_id`
4. 检查 `allowed_users`
5. 提取文本 `_extract_text()`
6. 提取附件 `_extract_inbound_files()`
7. 如果文本和附件都为空，直接忽略
8. 提取 `context_token`
9. 缓存上下文 token
10. 组装 `InboundMessage`
11. 发布到 bus

### 12.2 文本提取 `_extract_text()`

从 `item_list` 中遍历：

- 仅保留 `type == TEXT`
- 读取 `text_item.text`
- 多段文本以换行拼接

这意味着：

- 如果一条 iLink 消息里同时有文本和附件，文本仍然会被单独抽出来
- 非文本 item 不会污染消息正文

### 12.3 命令识别

如果文本以 `/` 开头，则：

- `msg_type = COMMAND`

后续 `ChannelManager` 会把它走命令处理分支。

### 12.4 `thread_ts` 的选择

当前实现：

- 优先用 `context_token`
- 否则用 `client_id`
- 再否则用 `msg_id`

这不是 DeerFlow thread ID，而是平台上下文标识，主要用于：

- 帮助 channel 自己恢复会话上下文
- 在 outbound 发送时找回 `context_token`

### 12.5 `metadata`

当前会放入：

- `context_token`
- `ilink_user_id`
- `ref_msg`
- `raw_message`

其中 `ref_msg` 是从 `item_list[*].ref_msg` 中提取的引用消息结构。

---

## 13. `context_token` 的作用

这是 WeChat channel 中最关键的平台状态之一。

当前实现里它承担至少三件事：

1. **发消息时的上下文路由依据**
2. **typing 接口所需的上下文参数**
3. **把入站与出站关联到同一平台会话**

缓存结构：

- `_context_tokens_by_chat[chat_id] = context_token`
- `_context_tokens_by_thread[thread_ts] = context_token`

发送出站消息时，`_resolve_context_token(msg)` 的查找顺序是：

1. `msg.metadata["context_token"]`
2. `msg.thread_ts` 对应缓存
3. `msg.chat_id` 对应缓存

如果找不到，就直接丢弃出站消息并写警告日志。

这意味着一个重要事实：

> DeerFlow 侧虽然维护自己的 thread，但真正把消息发回微信时，仍需要平台侧的 `context_token`。

---

## 14. 入站图片/文件处理链路

### 14.1 统一入口 `_extract_inbound_files()`

逻辑：

1. 遍历 `item_list`
2. `IMAGE` -> `_extract_image_file()`
3. `FILE` -> `_extract_file_item()`
4. 收集成 `list[dict]`

返回的每个文件对象会继续进入 `InboundMessage.files`。

### 14.2 入站图片 `_extract_image_file()`

处理步骤：

1. 找到 `image_item.media`
2. 取出 `full_url`
3. 解析 AES key
4. 下载 CDN 密文
5. AES-128-ECB 解密
6. 检查图片大小限制
7. 根据 magic bytes 判断扩展名和 MIME
8. 生成安全文件名
9. 落盘到 `downloads/`
10. 返回文件元数据

返回结构大致包含：

- `filename`
- `size`
- `path`
- `mime_type`
- `source = "wechat"`
- `message_item_type = IMAGE`
- `full_url`

### 14.3 入站文件 `_extract_file_item()`

与图片流程类似，但多了两步：

- 先根据 `file_name` 和 MIME 走白名单过滤
- 使用 `_normalize_inbound_filename()` 保持更稳定的文件名

### 14.4 AES key 解析策略

`_resolve_media_aes_key()` 会在多个字段名中尝试解析：

- `aeskey`
- `aes_key_hex`
- `aes_key`
- `aesKey`
- `encrypt_key`
- `encryptKey`
- `media.*` 嵌套字段

同时兼容：

- 直接 hex
- base64
- base64 后再包一层 hex 字符串

这部分非常关键，因为 iLink 上下游字段名和编码形态可能不完全稳定。

### 14.5 暂存文件落盘 `_stage_downloaded_file()`

这里只做一件事：

- 把解密后的字节写到 `state_dir/downloads/<filename>`

它**还没有**进入 DeerFlow thread 的正式 upload 目录。

---

## 15. 入站附件如何进入 Agent 上下文

WeChat channel 自己只负责“下载并暂存文件”。

真正把这些文件暴露给 agent 的关键在 `ChannelManager._build_run_input()`。

### 15.1 `_build_run_input(thread_id, msg)`

逻辑：

1. 如果 `msg.files` 为空，只发送普通 human text
2. 否则调用 `stage_inbound_files(thread_id, msg.files)`
3. 把结果塞进：
   - `messages[0].additional_kwargs.files`

这样就与前端上传文件的结构保持一致。

### 15.2 `stage_inbound_files()` 做了什么

位于 `backend/app/channels/inbound_uploads.py`，职责是：

1. 把 channel 暂存文件复制到当前 thread 的 upload 目录
2. 生成与前端上传一致的元数据：
   - `filename`
   - `size`
   - `path`（虚拟路径）
   - `status = uploaded`
3. 如果是可转换文档，尝试生成 markdown companion
4. 在非本地 sandbox 下，把文件镜像进 sandbox

这一步是 WeChat channel 接入 DeerFlow 现有 UploadsMiddleware 的核心桥梁。

### 15.3 为什么这套桥接重要

因为它避免了为 WeChat 单独发明另一套“文件给 agent”的协议。

也就是说：

> WeChat 的入站文件能力，本质上是在复用 DeerFlow 既有 uploads 流程，而不是自己重写一套 agent 文件注入机制。

---

## 16. 出站文本发送链路

### 16.1 `send(msg)`

这是 WeChat channel 的主出站入口。

处理顺序：

1. 去掉空白，空文本直接返回
2. 保证已认证
3. 解析 `context_token`
4. 根据长度/配置决定是否分块
5. 调 `_send_text_message()` 发送

### 16.2 `_send_text_message()`

发文本时构造的 iLink payload 核心结构如下：

- `msg.from_user_id = ""`
- `msg.to_user_id = chat_id`
- `msg.client_id = 唯一值`
- `msg.message_type = 2`
- `msg.message_state = 2`
- `msg.context_token = context_token`
- `msg.item_list = [{type: TEXT, text_item: {text}}]`
- `base_info = {channel_version}`

然后调用：

- `POST /ilink/bot/sendmessage`

### 16.3 重试策略

`_send_text_message()` 使用指数退避：

- 默认最大重试 3 次
- delay 为 `1s, 2s, 4s` 这种增长方式（实际为 `2**attempt`）

这是文本发送可靠性的基本保障。

---

## 17. 长文本分块回复策略

### 17.1 为什么要分块

微信一类 IM 平台上，超长回复一次性发出会有几个问题：

- 用户感知延迟大
- 可读性差
- 有时平台对超长文本兼容性较差

因此当前实现做了“最终文本分块”，注意它不是 LangGraph 流式输出，而是：

- 等 agent 完全结束后
- 再把最终文本拆成多个小消息发送

### 17.2 触发条件

满足以下条件才会分块：

- `chunked_reply_enabled = true`
- 文本不含代码块标记 `` ``` ``
- 文本长度 >= `chunked_reply_trigger_chars`

代码块不分块是为了避免破坏 markdown/代码结构。

### 17.3 分块算法

分 3 层：

1. 按空行切段落
2. 段落内按句子切
3. 如果句子还是太长，再硬切

随后再做两次修正：

- 合并过短 chunk
- 若 chunk 总数超过上限，从尾部继续合并

### 17.4 失败兜底

- 如果第一块就发送失败：回退到“一次性整段发送”
- 如果已经发送了一部分才失败：把剩余内容并成最后一块发送

这个策略的目标是：

- 尽量分块
- 但不能因为中途失败让用户只拿到半截回复

---

## 18. 出站附件总体策略

WeChat channel 的附件发送依赖 `Channel` 基类的 `_on_outbound()`：

1. 先调 `send(msg)` 发文本
2. 再遍历 `msg.attachments`
3. 每个附件调用 `send_file(msg, attachment)`

### 18.1 `send_file()` 分流

- `attachment.is_image == True` -> `_send_image_attachment()`
- 否则 -> `_send_file_attachment()`

### 18.2 `ResolvedAttachment` 从哪里来

不是 WeChat channel 自己找的，而是 `ChannelManager` 在 agent 执行完成后，基于 artifact 路径解析出来的。

后文详述。

---

## 19. 出站图片发送链路

### 19.1 入口 `_send_image_attachment()`

步骤：

1. 检查图片大小上限
2. 保证已认证
3. 解析 `context_token`
4. 从本地磁盘读原图
5. 生成 16 字节随机 AES key
6. 组装 `getuploadurl` 请求
7. 调 `getuploadurl`
8. 对原图做 AES-128-ECB 加密
9. 上传到 CDN
10. 组装 `image_item`
11. 调 `sendmessage`

### 19.2 `getuploadurl` 请求内容

通过 `_build_upload_request()` 构造，核心字段包括：

- `filekey`
- `media_type = IMAGE`
- `to_user_id`
- `rawsize`
- `rawfilemd5`
- `filesize`（密文大小）
- `aeskey`（hex）
- 可选 `thumb_*`
- 可选 `no_need_thumb = true`

当前图片发送实现直接传 `no_need_thumb=True`。

### 19.3 CDN 上传

`getuploadurl` 可能返回两类信息：

1. 直接给 `upload_full_url`
2. 只给 `upload_param`
   - 此时本地通过 `_build_cdn_upload_url()` 自行拼装上传 URL

上传方法当前默认按 `POST` 处理。

上传成功后，会从响应头读取：

- `x-encrypted-param`

如果存在，就回写到 `upload_data["upload_param"]`，供后续 `sendmessage` 使用。

### 19.4 图片消息体

最终 `image_item` 包含：

- `media.aes_key`
  - 注意：这里不是原始 key，也不是纯 hex
  - 当前实现用 `_encode_outbound_media_aes_key()` 转成 `base64(hex(aes_key))`
- `media.encrypt_type = 1`
- 可选 `media.encrypt_query_param`
- `mid_size = 密文大小`

然后通过 `sendmessage` 发出 `IMAGE` 类型 item。

---

## 20. 出站普通文件发送链路

与图片非常像，但消息体字段不同。

### 20.1 `_send_file_attachment()` 额外检查

发送前会先检查：

- 扩展名是否允许
- MIME 是否允许
- 文件大小是否超限

### 20.2 文件上传和发送流程

1. 读本地文件
2. 生成随机 AES key
3. 调 `getuploadurl(media_type=FILE)`
4. 加密原文件
5. 上传 CDN
6. 组装 `file_item`
7. 调 `sendmessage`

### 20.3 `file_item` 结构

核心包括：

- `media.aes_key`
- `media.encrypt_type = 1`
- 可选 `media.encrypt_query_param`
- `file_name`
- `md5`（原文 MD5）
- `len`（原文长度字符串）

这和图片不同：

- 图片发送关心 `mid_size`
- 文件发送关心原文件名、原文 MD5、原文长度

---

## 21. AES 与媒体编码细节

### 21.1 加密算法

当前实现统一使用：

- AES-128-ECB
- PKCS7 padding

### 21.2 为什么需要两种 key 表示

同一条链路里，AES key 会以多种形式出现：

- 本地内存：原始 16 字节 bytes
- `getuploadurl` 请求：hex 字符串（`aeskey`）
- 出站消息体：`base64(hex(aes_key))`
- 入站媒体字段：可能是 hex，也可能是 base64，甚至嵌套在 `media` 内

如果要重写，**最容易出错的点就是 AES key 的表示转换**。

### 21.3 密文长度

`_encrypted_size_for_aes_128_ecb()` 用于计算加密后尺寸。

对于 `getuploadurl` 这类协议，往往既需要：

- 原文长度
- 也需要密文长度

两者不能混淆。

---

## 22. Typing 能力

### 22.1 `send_typing()`

当前实现是 best-effort 能力，不影响主流程。

发送前提：

- `typing_enabled = true`
- 能拿到 `context_token`
- 能通过 `getconfig` 取得 `typing_ticket`

### 22.2 `_get_typing_ticket()`

先调：

- `POST /ilink/bot/getconfig`

拿到 `typing_ticket`。

### 22.3 再调用 `sendtyping`

- `POST /ilink/bot/sendtyping`
- 参数包括：
  - `ilink_user_id`
  - `typing_ticket`
  - `status = TYPING`
  - `base_info`

### 22.4 与 `ChannelManager` 的配合

`ChannelManager._create_typing_task()` 会：

1. 查看 channel 是否有 `send_typing()`
2. 看 inbound metadata 里是否带 `context_token`
3. 启一个循环任务，按 `typing_refresh_interval` 定时调用 `send_typing()`
4. 当本轮消息处理结束，再取消该任务

也就是说 typing 不是 WeChat channel 自己内部定时调的，而是 **manager 驱动，channel 负责具体平台调用**。

---

## 23. DeerFlow thread 与平台会话的映射

WeChat channel 自己不维护 DeerFlow thread 映射。

这件事由 `ChannelStore` + `ChannelManager` 负责：

1. manager 收到 `InboundMessage`
2. 根据 `channel_name + chat_id (+ topic_id)` 查 thread
3. 找不到就创建新的 LangGraph thread
4. 保存映射

当前 WeChat 实现里：

- `topic_id` 固定是 `None`
- 因此映射粒度基本是 `wechat:chat_id`

这意味着当前行为更接近：

- 每个微信用户一个 DeerFlow 对话线程

如果以后要支持更细的会话分叉，可考虑把 `context_token` 或引用关系映射到 `topic_id`。

---

## 24. Agent 返回结果如何变成微信可发送内容

### 24.1 先收集文本

`ChannelManager` 调 `runs.wait()` 后，通过 `_extract_response_text()` 得到最终文本。

### 24.2 再收集 artifact

artifact 来源有两条：

1. AI 消息里的 `present_files` tool call
2. 如果没有明确 tool call，就对比 thread outputs 目录执行前后差异

这部分由以下函数组成：

- `_extract_artifacts()`
- `_snapshot_outputs_dir()`
- `_infer_artifacts_from_outputs_delta()`
- `_collect_artifacts()`

### 24.3 虚拟路径解析为真实附件

`_resolve_attachments()` 会把 artifact 虚拟路径转成 `ResolvedAttachment`：

- `/mnt/user-data/outputs/...` 直接解析
- `/mnt/user-data/uploads/...` 先复制进 outputs，再发送
- 其它路径一律拒绝

这是一个重要安全边界：

> channel 只允许发送 outputs / uploads 内的文件，避免任意读取 sandbox 里的别的路径。

### 24.4 文本兜底

即使附件能正常解析，`_prepare_artifact_delivery()` 仍会把附件文件名追加到文本里。

这样即便平台上传失败，用户也至少知道 DeerFlow 产生了哪些文件。

---

## 25. WeChat channel 与 artifact 回传的完整闭环

完整闭环如下：

1. 微信用户发文本/图片/文件
2. `WechatChannel` 拉到消息
3. `WechatChannel` 下载并解密入站媒体到本地
4. `InboundMessage.files` 带着这些文件元数据进入 manager
5. `stage_inbound_files()` 把文件复制到 thread uploads
6. agent 在 DeerFlow 里读取这些上传文件并生成结果
7. agent 可能产出 outputs 文件或调用 `present_files`
8. manager 收集 artifact，解析为 `ResolvedAttachment`
9. `WechatChannel.send()` 先发说明文本
10. `WechatChannel.send_file()` 再逐个把图片/文件发回微信

这说明 WeChat channel 已经形成完整的：

- 入站媒体 -> agent 上下文
- agent 产物 -> 微信回传

双向文件链路。

---

## 26. iLink 接口清单

当前实现已明确依赖以下接口：

### 26.1 认证/公开接口

- `GET /ilink/bot/get_bot_qrcode`
- `GET /ilink/bot/get_qrcode_status`

### 26.2 Bot 鉴权接口

- `POST /ilink/bot/getupdates`
- `POST /ilink/bot/sendmessage`
- `POST /ilink/bot/getuploadurl`
- `POST /ilink/bot/getconfig`
- `POST /ilink/bot/sendtyping`

### 26.3 CDN

- 下载：直接 GET `media.full_url`
- 上传：对 `upload_full_url` 或拼出的 CDN URL 做 `POST/PUT`

如果要迁移到别的仓库，以上接口就是最小协议面。

---

## 27. 错误处理策略

当前实现的错误处理大致分 4 层：

### 27.1 可跳过型

例如：

- 某个入站文件下载失败
- 某个附件 MIME 不允许
- 某个文件超大小限制

处理方式：

- 记录 warning
- 跳过该文件
- 不中断整条消息处理

### 27.2 可重试型

例如：

- 文本发送失败

处理方式：

- 指数退避重试

### 27.3 生命周期恢复型

例如：

- `getupdates` 返回 `errcode = -14`

处理方式：

- 清空 token
- 保存状态
- 必要时自动重新二维码绑定

### 27.4 致命型

例如：

- 二维码确认成功但没返回 `bot_token`
- 二维码流程超时
- iLink 返回明确失败并无法恢复

处理方式：

- 抛异常
- 由上层轮询或调用方记录日志并重试/停机

---

## 28. 从零重写时的最小实现顺序

如果完全不参考当前代码，建议按下面顺序实现。

### 阶段 A：最小文本往返

1. 建一个 `Channel` 子类
2. 支持配置 `bot_token`、`polling_timeout`
3. 实现 `getupdates`
4. 能解析入站文本
5. 能生成 `InboundMessage`
6. 能在 outbound 里调用 `sendmessage`
7. 能保存 `context_token`

完成后，应达到：

- 用户发文本
- agent 返回文本
- 微信收到回复

### 阶段 B：状态持久化与恢复

1. 保存 `get_updates_buf`
2. 保存 `bot_token`
3. 重启后自动恢复
4. 识别 token 过期

完成后，应达到：

- 服务重启不丢轮询位置
- token 过期不会直接永久瘫痪

### 阶段 C：出站附件

1. 支持 artifact 路径解析
2. 实现 `getuploadurl`
3. 实现 CDN 加密上传
4. 实现图片发送
5. 实现普通文件发送

完成后，应达到：

- agent 产出的文件能回传到微信

### 阶段 D：入站附件

1. 识别 `item_list` 中的图片/文件
2. 下载 CDN 密文
3. 解密并落盘
4. 用 `stage_inbound_files()` 桥接到 thread uploads

完成后，应达到：

- 微信发来的图片/文件能进入 agent 上下文

### 阶段 E：体验增强

1. typing
2. 长文本分块
3. 二维码绑定
4. 更细的错误提示与监控

---

## 29. 复刻时最容易踩坑的点

### 29.1 忘记 `context_token`

没有它，经常会出现：

- 能收消息
- 但回不去正确会话
- 或根本发不出去

### 29.2 把 AES key 编码搞混

尤其是：

- `getuploadurl` 用 hex
- 出站 `media.aes_key` 用 `base64(hex(aes_key))`
- 入站又可能是别的编码形态

### 29.3 混淆原文大小和密文大小

`rawsize` / `filesize` / `mid_size` / `len` 各自含义不同，写错会直接导致平台拒绝或展示异常。

### 29.4 只做 channel 下载，不接 DeerFlow uploads

如果没有 `stage_inbound_files()` 这一步，agent 不会自动看到微信发来的文件。

### 29.5 让 channel 直接解析任意 artifact 路径

必须限制在：

- outputs
- uploads

否则有越权读文件风险。

### 29.6 二维码信息打日志

当前实现为了调试会打印二维码内容。对外分发或生产化时，需要重新评估日志脱敏策略。

---

## 30. 测试建议

当前仓库已经有补丁包和运行时 smoke 测试，但如果要专门保证 WeChat channel 质量，建议至少覆盖以下层次。

### 30.1 单元测试

- 文本提取 `_extract_text()`
- chunk 分割逻辑
- AES key 解析逻辑
- 图片 magic bytes 检测
- 文件扩展名/MIME 白名单

### 30.2 协议适配测试

- `getupdates` 返回正常消息
- `getupdates` 返回 `errcode=-14`
- `getuploadurl` 返回 `upload_full_url`
- `getuploadurl` 只返回 `upload_param`
- CDN 上传返回 `x-encrypted-param`

### 30.3 集成测试

- 入站文本 -> agent -> 出站文本
- 入站图片 -> agent 可见上传文件
- agent 生成图片 -> 微信收到图片
- agent 生成普通文件 -> 微信收到文件

### 30.4 恢复测试

- 重启后继续轮询
- token 过期后自动重绑
- 无 `context_token` 时出站被安全丢弃

---

## 31. 外部分发镜像的同步注意事项

如果后续还会维护独立分发仓库或镜像载荷，需要把这里的运行时改动同步过去。

建议至少保持以下最小契约一致：

- `class WechatChannel(Channel):`
- `start()` / `stop()` / `send()` 方法存在
- 依赖的基础导入不变

因此：

- 如果修改主仓 `backend/app/channels/wechat.py`
- 也要同步更新外部分发镜像

---

## 32. 推荐的维护原则

如果后续继续演进 WeChat channel，建议遵守以下原则：

1. **平台协议逻辑集中在 `wechat.py`**
   - 不要把 iLink 特殊字段散落到 manager
2. **文件桥接继续复用 `stage_inbound_files()`**
   - 不要再发明另一套 agent 文件注入协议
3. **artifact 安全边界保留在 manager**
   - channel 不应该能发送任意宿主机路径
4. **二维码登录与显式 token 共存**
   - 便于本地调试和实际部署
5. **把 `context_token` 视为一级状态**
   - 丢失它就等于丢了平台会话
6. **外部分发镜像与主仓运行时同步管理**
   - 否则分享给别人后行为会漂移

---

## 33. 给 AI/开发者的“实现任务说明”模板

如果你要把这份文档直接交给 AI 或开发者，可使用下面的任务拆解：

### 目标

在一个 DeerFlow 风格的 channel 架构中实现 `WechatChannel`，要求支持：

- iLink 长轮询收消息
- 文本收发
- 图片/文件入站
- 图片/文件出站
- `context_token` 会话恢复
- `get_updates_buf` 与 `bot_token` 持久化
- 可选二维码绑定
- 与统一 `InboundMessage` / `OutboundMessage` 抽象兼容

### 必须产物

1. `WechatChannel(Channel)` 类
2. 配置解析层
3. iLink 请求头和基础 payload 构造
4. 轮询循环
5. 入站文本解析
6. 入站图片/文件下载解密
7. `stage_inbound_files()` 桥接
8. 出站文本发送
9. `getuploadurl + CDN + sendmessage` 附件链路
10. 状态持久化
11. typing 与 chunked reply（可后置）

### 验收标准

- 发文字能回文字
- 发图片/文件后 agent 能看到上传文件
- agent 产出的图片/文件能回传到微信
- 重启不丢 `get_updates_buf`
- token 过期后能恢复或给出明确失败状态
- 无 `context_token` 时不会误发到错误上下文

---

## 34. 一句话总结

当前 DeerFlow 的 WeChat channel 不是“简单的 webhook 适配层”，而是一个完整的、带状态的 iLink 运行时桥接器：

- 用 `getupdates` 拉消息
- 用 `context_token` 维持平台上下文
- 用 AES-128-ECB + CDN 处理媒体
- 用 `stage_inbound_files()` 把微信附件接进 DeerFlow uploads
- 用 artifact 解析把 agent 产物再发回微信

如果要复刻，实现重点不是 UI，也不是安装器，而是这条 **协议、状态、文件桥接三位一体的运行时链路**。
