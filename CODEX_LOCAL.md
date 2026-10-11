# Codex 本地入口（macOS 桌面 / Linux app-server）

来信先进入一个固定的 Codex 会话，由该会话查找相关任务、转交请求或派生新任务。会话空闲时不运行模型；后台服务负责扫描和排队。重启使用已保存的同一会话 ID。

```text
Tailscale 身份校验 → Tailmsg inbox
  → wake exec / 定时补扫 → SQLite 去重队列
  → 官方 codex queue --thread <固定 UUID>
     （macOS：桌面应用；Linux：--remote unix:///<本机 app-server socket>）
  → 固定入口会话 → 查找 / 转交 / 派生任务
```

两个平台共用同一份适配器、SQLite 队列、去重、进程锁、重试和 `uncertain` 核对逻辑，区别只在投递通道和服务管理：macOS 用 LaunchAgent 并检查桌面进程；Linux 用 user systemd 服务，并只连接配置中明确写出的、属于当前用户的本机 Unix socket。Linux 部分见下文“Linux（user systemd + app-server）”一节。

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

在该会话中明确授予需要的消息处理、会话搜索、转交及派生权限。工作流模板不构成授权。`install_codex_bridge.py` 面向 macOS，要求已安装提供 `codex queue` 的 Codex 桌面 CLI，以及由 `dev.tailmsg` 管理的 Tailmsg 接收端；Linux 见下文；其他平台可以继续使用 Tailmsg 本体。

```sh
python3 install_codex_bridge.py --thread-id <固定入口 UUID>
```

安装器复制适配器到 `~/.tailmsg/codex-local/`，保存配置 `~/.tailmsg/codex-bridge.json`、状态 `~/.tailmsg/codex-bridge/bridge.sqlite3`，将 `agents.codex.wake_adapter` 指向快速入队命令，并安装 `dev.tailmsg.codex-bridge` LaunchAgent。既有 receiver 由 `dev.tailmsg` 继续管理，原来的 Codex GUI/Tailscale 门控不变。

更新已有安装时仍使用同一 UUID；安装器拒绝用新 UUID 覆盖已有入口，避免把旧队列投给其他会话。安装器保留旧代码、指令、配置、安装回执和服务文件的时间戳备份，位置记在安装回执中。另一个 codex wake adapter 已存在时会拒绝覆盖。`--no-start` 可只安装而不重新加载服务。

新安装默认 `allow_replies=false`。只有用户明确允许自动回执与结果回传时，才使用 `--allow-replies`；升级时保留已授权的设置，`--no-allow-replies` 可显式关闭。未指定 `--codex-path` 时保留已记录的 CLI 路径。该权限只允许回复对应收到的请求，不允许导出其他会话的私人内容。

## 检查与恢复

```sh
python3 ~/.tailmsg/codex-local/codex_bridge.py --config ~/.tailmsg/codex-bridge.json status
launchctl print gui/$(id -u)/dev.tailmsg.codex-bridge
tail -30 ~/.tailmsg/codex-bridge/service.log
tailmsg inbox --pending --agent codex
```

`wake <inbox JSON路径>` 只入本地队列并快速返回；`scan` 补扫且执行到期投递；`daemon` 循环扫描；`status` 输出队列状态，不输出消息正文。`uncertain` 需要先检查官方入口的消息记录，确认是否入队，不能直接删除状态并重发。

停止适配器：`launchctl bootout gui/$(id -u)/dev.tailmsg.codex-bridge`。完整回滚：先读取 `~/.tailmsg/codex-bridge/installation.json` 的 `backup` 路径，再运行 `python3 install_codex_bridge.py --rollback <backup路径>`。它恢复那一版代码、指令、配置和服务状态，重新加载既有 `dev.tailmsg`；不删除消息、队列、处理记录或入口会话。

## Linux（user systemd + app-server）

### 前提

安装器和插件都不会启动 Codex。需要事先具备：

- 已安装提供 `codex queue --remote` 的官方 Codex CLI（在 `PATH` 上，或用 `--codex-path` 指定）。
- 当前用户自己运行的本机 Codex app-server，监听一个 Unix socket（例如用 `codex app-server --listen unix://PATH` 启动的 `PATH`）。只接受 `unix:///绝对路径`；不支持 `ws://`/`wss://` 等网络端点，也不使用 CLI 的隐式默认 `unix://`。
- 该 app-server 中一个已存在的入口会话 UUID。安装器不创建会话，不使用 `--last`，也不接受会话名称。
- 可用的 user systemd 管理器（`systemctl --user` 可用；需要在登出后继续运行时启用 lingering）。非交互 shell 可能还需设置 `XDG_RUNTIME_DIR`。
- 已用 `install-runtime` 安装到数据目录的 Tailmsg 接收端；接收端服务名按 `service-template systemd-user` 的约定为 `tailmsg.service`。

### 快速开始

```sh
Y="/path/to/yobiru/scripts/yobiru"
SOCK=/absolute/path/to/app-server.sock      # 你的 app-server 实际监听的 socket
sh "$Y" codex-bridge install --thread-id <入口会话 UUID> --codex-remote "unix://$SOCK" --dry-run
sh "$Y" codex-bridge install --thread-id <入口会话 UUID> --codex-remote "unix://$SOCK"
sh "$Y" codex-bridge status
```

`--dry-run` 只检查并打印计划（含将写入的 unit 文件），不写任何文件，也不调用 `systemctl`。正式安装把适配器复制到 `<数据目录>/codex-local/`（不从插件缓存运行），写入 `codex-bridge.json`、`agents.codex.wake_adapter`、安装回执和 `~/.config/systemd/user/tailmsg-codex-bridge.service`（遵循绝对路径的 `$XDG_CONFIG_HOME`），然后 `daemon-reload`、`enable`、`restart` 该服务，并对 `tailmsg.service` 执行 `try-restart`（只在它已运行时重启）。路径中的空格、`%` 和 `$` 会按 systemd 规则转义。

`--no-start` 只写文件，完全不调用 `systemctl`；之后自行执行 `systemctl --user daemon-reload && systemctl --user enable tailmsg-codex-bridge.service && systemctl --user restart tailmsg-codex-bridge.service && systemctl --user try-restart tailmsg.service`。用 `restart` 而不是 `enable --now`，这样已运行的服务也会加载升级后的代码。

### 权限与账户

- 只使用属于当前用户的 socket；socket 不存在、不是 socket、属于其他用户、所在目录可被他人替换，或没有进程监听（残留的旧 socket）时，都不会调用 `codex queue`，消息记为已知未发送并自动退避重试。
- 核对（`uncertain` 时读取入口会话）使用短时只读的官方 `codex app-server --listen stdio://`，它从 `CODEX_HOME` 读取会话。为保证读的是目标 app-server 的同一账户和会话存储，安装器总是固定记录 `codex_home`：`--codex-home`，否则已安装的值，首次安装时再看 `$CODEX_HOME`，最后是 `~/.codex`。之后在其他 `CODEX_HOME` 环境里升级或触发 wake 都不会改变它。`codex_path` 同样优先保留已安装的值。
- 安装器新建的运行文件和目录（适配器副本、`codex-bridge.json`、安装回执、备份、状态目录）使用仅当前用户可访问的权限（`umask 077`，文件 0600）；已有的 `config.json` 保留原权限，systemd unit 为 0644（不含机密）。服务以当前用户运行，不需要 root。
- `allow_replies` 首次安装为 false；升级时保留已授权的设置，`--no-allow-replies` 可显式关闭，`--allow-replies` 仅在用户明确授权后使用。
- 既有顶层 exec/webhook adapter、其他 agent 及未知字段原样保留；只设置 `agents.codex.wake_adapter`。已有不同的 codex adapter、不同的入口 UUID、混合 UUID 的队列、或已被另一数据目录占用的 user unit 时，安装在写入任何文件之前拒绝。

### 状态含义与 `uncertain` 排查

`enqueued` 只表示官方 CLI 返回了该会话的入队回执，不表示模型已处理或任务完成；完成与否要看入口会话的记录、产物或回复。

`sh "$Y" codex-bridge status` 给出通道、socket 状态（`listening`、`socket_missing`、`socket_stale` 等）、队列各状态计数、`retry`/`uncertain` 的原因和处理建议，不输出消息正文。`retry` 表示确定没有发出，会自动重试（退避最长约一小时一次）。`uncertain` 表示 `codex queue` 已启动但超时、失败或回执无效：消息可能已进入会话，因此绝不自动重发。适配器会定期在固定入口会话（`codex_home` 对应的账户）中查找 `[TAILMSG:<id>]` 标记，找到即记为 `delivered`。长期找不到时，请在该会话中人工确认后用 `inbox --pending` 处理原信；不要删除队列或状态来强制重发。

```sh
systemctl --user status tailmsg-codex-bridge.service
journalctl --user -u tailmsg-codex-bridge.service -n 30 --no-pager
```

### 回滚

读取 `<数据目录>/codex-bridge/installation.json` 中的 `backup`，然后：

```sh
sh "$Y" codex-bridge rollback <backup 路径> --dry-run
sh "$Y" codex-bridge rollback <backup 路径>
```

回滚只接受该数据目录 `backups/` 下由 Linux 安装器生成的备份，并校验清单中的目标路径。它先 `disable --now` 并确认服务已停止（失败，或服务仍在运行但 unit 文件已不存在时，什么都不恢复），再恢复代码、配置、unit 和回执，按安装前记录的启用/运行状态恢复服务，并 `try-restart` 接收端。若当前 unit 已属于另一个数据目录则拒绝。`--no-start` 只恢复文件、不碰服务（适合已手动停服时）。inbox、SQLite 队列、处理记录和入口会话都不会被删除；保留的队列继续锁定原入口 UUID。

## 按消息类型分流（可选，macOS 与 Linux 通用）

已有部署可以让某些消息类型不进入固定入口会话，而交给另一个本机程序处理。在 `codex-bridge.json` 中设置：

```json
{
  "excluded_kinds": ["example-kind"],
  "excluded_kind_adapter": ["/absolute/path/to/handler", "--fixed-arg"],
  "excluded_kind_timeout_seconds": 60
}
```

- `excluded_kinds`：信封 `kind` 字符串列表（每项 1–32 个字符，无控制字符）。`excluded_kind_adapter`：可信本机程序的 argv，第一项必须是绝对路径。`excluded_kind_timeout_seconds`：可选，默认 60，范围 (0, 300]。实际时限取它与“接收端 `hook_timeout_s`（默认 120）减 10 秒”中的较小值，保证适配器在接收端终止它之前先终止处理程序；剩余时间不足 1 秒时不启动处理程序。只读取、不修改用户配置。任一字段无效时，适配器和安装器在任何副作用之前拒绝运行。
- 先完成与普通消息相同的校验（inbox 路径、Tailscale 来源、白名单、目标 agent `codex`、实际 wake 级别、是否已 ack），之后才按 `kind` 分流。被排除的消息从不进入 Codex 队列：`wake`、`scan` 都不会入队；在配置前已入队的 pending/retry/uncertain 记录，在投递时重新校验并记为 `skipped`（`excluded_kind`）。
- 只有接收端对一封未 ack、实际级别为 `now` 的消息直接调用 `wake` 时，才运行该程序：argv 原样，只追加经过校验的规范 inbox 路径；不经 shell，stdin 为空，不展开任何正文。`none`、`batch`、已 ack，或未配置程序时只记为 skipped，消息留在 inbox。`scan`、定时投递、`status` 和核对都不会运行它。
- 结果写到标准输出（JSON），并在标准错误输出一行固定状态，接收端会把它记入 wake 日志：`forwarded`（程序退出码 0）；`forward_failed`（程序非零退出，`exit_code` 记录其值）；`forward_timeout`（超时，程序及其整个进程组被终止）。这三种情况程序都已启动过，适配器一律以 0 退出，接收端不会重放它；程序失败或超时都不会被自动重试，消息仍留在 inbox 中，由该程序或用户处理。程序未能启动时（`forward_not_started`：可执行文件不可用为 127，没有可用时限为 1）以非零退出，接收端可能按 `wake_attempts` 再试，这是安全的，因为程序从未运行。程序可能在失败或超时前已部分执行，去重与重试由它自己以消息 ID 为键负责；这里不承诺恰好一次。
- 没有对应的 CLI 选项：直接编辑 `codex-bridge.json`，再用 `codex-bridge install ... --dry-run` 预览（会校验这些字段，并显示 `excluded_kinds`）。两个安装器升级时都保留这些字段，以及调整过的轮询/重试时间、自定义入口指令路径、已记录的 Codex CLI 路径、固定的 Codex 账户设置和已授权的 `allow_replies`。迁移已有部署时，先把原有分流设置写入 `codex-bridge.json`，确认预览无误后再安装，避免消息被误投到入口会话。`codex-bridge status` 会列出当前分流设置，但不会运行程序。

## 会话名称与发现（0.3.0）

安装桥接后，`yobiru agents` 和其他机器的发现（`GET /info`，仅对白名单内的机器）会自动报告固定入口会话的 UUID。原生名称由 `codex_bridge.py session-name` 只读查询：短时官方 app-server 执行 `thread/read`（`includeTurns: false`），只取 `Thread.name`，从不读取 `preview`、回合或会话列表，不入队、不创建队列数据库、不启动模型回合；接收端在整组进程超时清理和输出上限下调用它并缓存约 5 分钟。别名用 `yobiru session bind --agent codex --display-name 名称` 记录在 `sessions.json`；与桥接入口不同的 UUID 会被拒绝。两个安装器升级时不碰 `sessions.json`，名称因此保留。

## 入口指令的升级

两个安装器在首次安装时把公开模板复制为 `<数据目录>/codex-local/CODEX_DISPATCHER.md`。之后升级时，若该文件已存在，就原样保留其内容（可能是用户自己的工作流），只更新代码；安装输出和 `--dry-run` 会在模板有差异时提示。`dispatcher_instructions` 指向其他路径时，安装器既不写该文件，也不改该配置。升级前的文件会进入备份，回滚恢复的正是原来的内容。要采用新模板，请自行比较插件中的 `scripts/codex_bridge/CODEX_DISPATCHER.md` 与本地文件，审阅后手动合并。

## 验收

离线：`python3 -m unittest -v test_codex_bridge test_codex_queue_client test_install_codex_bridge test_install_codex_bridge_linux`。Linux 测试使用临时 AF_UNIX socket、假的 `codex` 与 `systemctl` 可执行文件和隔离的 HOME，不连接真实 app-server、不调用真实 systemd。

桌面会话继续遵守 Codex 原生权限策略。入口能够请求查找、转交和创建任务；如果原生工具要求审批，消息会保留在该入口等待用户处理，适配器不会代替用户批准。部署与真实验收结果见 [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md)。

真实验收应在独立 Tailmsg home/端口接收一封本机发给本机的消息，不扩展生产白名单。确认真实身份收件 → 同一个入口会话读取 → 派生任务写出指定测试产物；第二封请求找到并继续该已有任务。重复扫描和服务重启不能重复派生。HTTP 202、本地排队成功和模型完成必须分别核对。
