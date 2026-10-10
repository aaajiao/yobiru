# Codex 桌面本地入口

来信先进入一个固定的 Codex 桌面会话，由该会话查找相关任务、转交请求或派生新任务。会话空闲时不运行模型；后台服务负责扫描和排队。重启使用已保存的同一会话 ID。

```text
Tailscale 身份校验 → Tailmsg inbox
  → wake exec / 定时补扫 → SQLite 去重队列
  → 官方 codex queue --thread <固定 UUID>
  → 固定入口会话 → 查找 / 转交 / 派生任务
```

## 接口与语义

- 使用安装版本提供的 `codex queue --thread UUID --message TEXT`。消息只包含本地消息 ID、路径和可信工作流说明，不把外来正文作为 shell 命令或行动批准。
- `wake now` 立即排队，`batch` 默认延后 30 秒补扫，`none` 只存档。接收端 wake 降级仍生效。
- 收件前后的白名单、认证来源和目标 agent 均检查；投递时重新检查。适配器不会确认原信，也不会自动发送 Tailmsg 回复。
- 状态 `enqueued` 表示官方 CLI 接收入队，不能据此声称模型已处理。结果未知时只核对会话中的消息标记；无法确认时保留 `uncertain`，不盲目重发。
- 桌面关闭时暂停投递。服务继续保留本机队列，打开后重试。不存在替代会话或自动切换到 `--last` 的逻辑。
- 入口工作流见 [CODEX_DISPATCHER.md](CODEX_DISPATCHER.md)。用户授权了任务路由；来信自身不构成执行敏感操作的批准。

这里使用已安装的官方 CLI 本地接口。普通后台脚本直接访问桌面 app-tools socket 会遭到进程授权校验拒绝；本实现不改动或绕过该校验。[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)说明线程读取、恢复和派生接口；本机 `codex queue --help` 是部署版本的排队接口依据。

## 安装

先在桌面创建一个固定入口会话，以本仓库的 `CODEX_DISPATCHER.md` 为工作流。安装器需要它的准确 UUID，不会自行创建或猜测会话。

在该会话中明确授予需要的消息处理、会话搜索、转交及派生权限。工作流模板不构成授权。此安装器面向 macOS，要求已安装提供 `codex queue` 的 Codex 桌面 CLI，以及由 `dev.tailmsg` 管理的 Tailmsg 接收端；其他平台可以继续使用 Tailmsg 本体。

```sh
python3 install_codex_bridge.py --thread-id <固定入口 UUID>
```

安装器复制适配器到 `~/.tailmsg/codex-local/`，保存配置 `~/.tailmsg/codex-bridge.json`、状态 `~/.tailmsg/codex-bridge/bridge.sqlite3`，将 `agents.codex.wake_adapter` 指向快速入队命令，并安装 `dev.tailmsg.codex-bridge` LaunchAgent。既有 receiver 由 `dev.tailmsg` 继续管理，原来的 Codex GUI/Tailscale 门控不变。

更新已有安装时仍使用同一 UUID；安装器拒绝用新 UUID 覆盖已有入口，避免把旧队列投给其他会话。安装器保留旧代码、指令、配置、安装回执和服务文件的时间戳备份，位置记在安装回执中。另一个 codex wake adapter 已存在时会拒绝覆盖。`--no-start` 可只安装而不重新加载服务。

默认 `allow_replies=false`。只有用户明确允许自动回执与结果回传时，才使用 `--allow-replies`。该权限只允许回复对应收到的请求，不允许导出其他会话的私人内容。

## 检查与恢复

```sh
python3 ~/.tailmsg/codex-local/codex_bridge.py --config ~/.tailmsg/codex-bridge.json status
launchctl print gui/$(id -u)/dev.tailmsg.codex-bridge
tail -30 ~/.tailmsg/codex-bridge/service.log
tailmsg inbox --pending --agent codex
```

`wake <inbox JSON路径>` 只入本地队列并快速返回；`scan` 补扫且执行到期投递；`daemon` 循环扫描；`status` 输出队列状态，不输出消息正文。`uncertain` 需要先检查官方入口的消息记录，确认是否入队，不能直接删除状态并重发。

停止适配器：`launchctl bootout gui/$(id -u)/dev.tailmsg.codex-bridge`。完整回滚：先读取 `~/.tailmsg/codex-bridge/installation.json` 的 `backup` 路径，再运行 `python3 install_codex_bridge.py --rollback <backup路径>`。它恢复那一版代码、指令、配置和服务状态，重新加载既有 `dev.tailmsg`；不删除消息、队列、处理记录或入口会话。

## 验收

离线：`python3 -m unittest -v test_codex_bridge test_codex_queue_client test_install_codex_bridge`。

桌面会话继续遵守 Codex 原生权限策略。入口能够请求查找、转交和创建任务；如果原生工具要求审批，消息会保留在该入口等待用户处理，适配器不会代替用户批准。部署与真实验收结果见 [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md)。

真实验收应在独立 Tailmsg home/端口接收一封本机发给本机的消息，不扩展生产白名单。确认真实身份收件 → 同一个入口会话读取 → 派生任务写出指定测试产物；第二封请求找到并继续该已有任务。重复扫描和服务重启不能重复派生。HTTP 202、本地排队成功和模型完成必须分别核对。
