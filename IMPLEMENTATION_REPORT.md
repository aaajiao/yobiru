# Codex 本机唤醒实现

Tailmsg 的 HTTP 202 表示已保存来信；没有 wake adapter 时，不会自动进入模型会话。本次新增可选的本地适配器，将已认证来信排入用户指定的固定 Codex 桌面会话，不修改 Tailmsg 通信协议或既有收件端。

```text
Tailmsg inbox → 快速 wake 入队 / 定时补扫 → SQLite 持久去重队列
  → 官方 codex queue → 固定 Codex 入口会话
  → 根据已有用户授权查找、转交或派生任务
```

| 文件 | 技术职责 |
| --- | --- |
| `codex_bridge.py` | 校验本地认证记录和当前白名单；持久队列；now/batch/none；收件排序；进程锁；重试和未知结果核对 |
| `codex_queue_client.py` | 检查桌面进程（macOS）或同用户本机 Unix socket（Linux，`--remote unix:///…`）；通过官方 CLI 向固定会话入队；按固定的 `CODEX_HOME` 只读核对会话中的通知标记 |
| `install_codex_bridge.py` | 安装 exec adapter 与 macOS LaunchAgent；备份和回滚；保护固定入口 ID |
| `install_codex_bridge_linux.py` | 安装 exec adapter 与 Linux user systemd 服务；dry-run 零写入、`--no-start` 不调用 systemctl；备份、升级、回滚并恢复原服务状态；保护固定入口 ID、队列和 unit 归属 |
| `CODEX_DISPATCHER.md` | 入口工作流模板；会话路由、处理记录、ack 与回复权限边界 |
| `test_*.py` | 离线验证队列、CLI 接口、安装和回滚 |

## 实现边界

- Tailmsg 本体保持 Python 3.8+ 和既有跨平台行为；这个可选适配器面向 macOS 的 Codex 桌面安装，以及 Linux 上用户自行运行的本机 Codex app-server（0.2.0 起）。Linux 部分目前只有离线测试，尚未在真实 app-server 上验收。
- 固定会话 ID 和队列跨进程重启保留。空闲时不持续运行模型；桌面关闭时暂停投递。
- 通知只包含消息 ID 和本地文件、配置、状态目录引用，不将来信正文作为 shell 命令执行。自定义安装目录贯穿处理记录、配置读取和 Tailmsg 命令。
- 同级消息按验证后的收件时间排序，`now` 优先于 `batch`，`none` 不投递。
- CLI 接收入队记为 `enqueued`。超时或结果未知时保留 `uncertain`，只核对真实用户消息标记，不盲目重发。入队不等于请求已处理。
- 适配器不自动 ack。自动回执默认关闭；开启需要用户明确授权。模板本身不授予会话路由或对外通信权限。
- 宿主的原生审批继续生效；请求创建新会话可能停在审批处。

## 验证与使用

离线测试覆盖身份与路径校验、重复收件、收件排序、旧队列迁移、并发迁移、进程重启、已知未发送重试、未知结果不重发、官方 CLI 回执核对、资源释放、安装回滚和入口 ID 保护。

真实本机验证已确认：官方 CLI 和 Python 后台进程均能通知同一固定入口；认证收件能进入该入口；服务重启不会重复投递。子会话创建及已有会话复用的完整验收尚待宿主原生审批，不据此声称请求已经处理或已向远端回复。

个人部署路径、消息/会话 ID 和测试回执保存在本机忽略目录 `.live-test/`，不进入仓库。安装、状态检查、完整回滚和验收步骤见 [CODEX_LOCAL.md](CODEX_LOCAL.md)。
