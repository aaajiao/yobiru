# tailmsg 协议 v1：基于 Tailscale 的极简点对点消息

> 一句话：每台机器跑一个小收信端；谁都可以按同一条规则给任何一台机器发一封 JSON 小信；
> 信里**不放文件本体**，只写“去我这儿哪个内网地址拉、拉到后怎么校验”；收件方自己去拉。
> 没有中心服务器，不调度任务，不跟踪做完没做完，只负责通信。
> 一台机器上可以有多个 agent，用 `机器/agent` 点名，只叫醒被点名的那一个（§5.9）。

## 1. 前提

- 每台机器（开发服务器、家里电脑、Grok 的 box、云实例）都已加入同一个 tailnet。
- 每台机器运行 `tailmsg.py serve`，**只绑定本机 Tailscale IP**，默认端口 `7766`。
- 地址写法：`http://<主机名>.example.ts.net:7766`；没开 MagicDNS 的机器用 `tailscale status --json` 把主机名解析成 100.x 地址。

## 2. 唯一的发信接口

```
POST http://<收件方>:7766/msg
Content-Type: application/json
Body: 信封（见 §3），默认上限 256 KiB
```

| 状态码 | 含义 |
|---|---|
| 202 | 已收下并写入收件箱（**只代表送达，不代表对方已处理**） |
| 200 | 同一 `id` 之前已收过（幂等，可放心重发） |
| 400 | 格式错误 / 未知字段 / `wake` 取值不对 / `agent`、`from_agent` 名字不合法 / `to` 不是我 / 拉取地址不在发件方主机上 |
| 403 | 来源不是 tailnet 地址、不在白名单、或 `from` 与 Tailscale 身份不符 |
| 413 | 太大　　415 不是 JSON |

另有 `GET /health` → `{"ok":true,"v":1,"node":"<fqdn>"}`，只回答 tailnet 地址，用于探活。

## 3. 信封格式

```json
{
  "v": 1,
  "id": "9f2c4e1ab07d33c5",
  "from": "bot-box.example.ts.net",
  "to": "dev-server.example.ts.net",
  "ts": "2026-10-08T14:00:00Z",
  "kind": "text",
  "text": "今天的书签简报好了，PDF 在附件里。",
  "reply_to": "1a2b3c4d5e6f7a8b",
  "wake": "batch",
  "agent": "codex",
  "from_agent": "grok",
  "fetch": [
    {
      "name": "briefing-2026-10-08.pdf",
      "url": "http://bot-box.example.ts.net:7766/f/<43位随机token>/briefing-2026-10-08.pdf",
      "sha256": "<64位小写hex>",
      "size": 183422,
      "mime": "application/pdf",
      "expires": "2026-10-09T14:00:00Z"
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `v` | 是 | 固定 `1` |
| `id` | 是 | 发件方生成的随机串，`[A-Za-z0-9_-]{8,64}`，用于去重和回复 |
| `from` / `to` | 是 | 双方的 tailnet 全名（FQDN）；`to` 也可写短主机名 |
| `ts` | 是 | UTC，`YYYY-MM-DDTHH:MM:SSZ` |
| `kind` | 否 | 自由短标签（≤32 字符），默认 `text`，例如 `notice` / `request` / `status` |
| `text` | 否 | 正文，纯文本 |
| `reply_to` | 否 | 回复哪封信的 `id` |
| `fetch` | 否 | 文件拉取规则列表，每项必须有 `url`、`sha256`、`size` |
| `wake` | 否 | 发件方**希望**的提醒级别：`now` / `batch` / `none`，缺省等于 `batch`，其他值 400。见 §5.6 |
| `agent` | 否 | 收件机器上**点名哪个 agent**，例如 `codex`、`grok`、`私信联系人`。不写 = 收件方的 `default_agent`。见 §5.9 |
| `from_agent` | 否 | 发件方自己是哪个 agent，用于回信时自动点名回来。见 §5.9 |

agent 名字规则：1–64 个字符，可以用中文等任意 Unicode；不能含 `/`、控制字符（含换行），首尾不能有空白。比较前做 NFC 规范化，区分大小写。

出现其他字段一律 400，避免各端悄悄扩展出不兼容的格式。

## 4. 文件怎么给（发件方）

1. 发信前把文件**复制**一份到 `~/.tailmsg/outbox/<token>/files/`，token 是 32 字节随机数（URL 安全编码，不可猜）。复制而不是引用原文件，所以发出后原文件再改也不影响校验。
2. 发件方自己的 `serve` 只读地提供 `GET /f/<token>/<name>`，只在本机 Tailscale IP 上。
3. 每个 token 目录记录 `to` 和 `expires`（默认 24 小时）：
   - 过期了一律 404，过期 1 小时后自动删除；
   - 只有 `to` 那台机器（按 `tailscale whois` 判断）能拉，其他人拿到链接也是 404。
4. `url` 的主机必须就是 `from` 本身。收件方遇到指向别处的链接会直接拒收整封信，也就不会被诱导去访问第三方地址。

## 5. 收件方的行为

1. **鉴权顺序**：
   - 来源 IP 必须在 `100.64.0.0/10` 或 `fd7a:115c:a1e0::/48` 里，否则 403；
   - 用 `tailscale whois --json <IP>`（LocalAPI）查出对方节点名、tag、登录用户；
   - 对照本机白名单 `allow`，没命中 403；
   - 信封里的 `from` 必须等于 whois 查到的节点名，否则 403（防冒名）。
2. **存储**：原样写入 `~/.tailmsg/inbox/<id>.json`，内容为 `{"envelope": …, "received": {"at", "peer"}}`。`peer` 是 whois 查到的身份，以它为准，不信信封自报的身份。
3. **拉文件**：由收件方决定什么时候拉（`tailmsg fetch <id>`）。拉下来先存成 `.part`，大小和 sha256 都对得上才改名，放进 `inbox/<id>/files/`；对不上就删掉并报错。
4. **绝不执行**消息里的任何内容。消息是“请求或信息”，**永远不是批准**：不能替你批准命令、改权限、改模型或改配置。
5. **唤醒接口**：收到一封新信、且**实际 wake 级别为 `now`** 时（见 §5.6），调用本机配置的唤醒适配器 `wake_adapter`，把它交给本地 agent。怎么叫醒由各家 agent 自己实现，契约见 §5.8。旧配置 `on_message` 仍然有效，等同于 `exec` 适配器（已弃用）。

6. **提醒级别 `wake`**（只决定什么时候提醒，**不改变消息的性质**：消息依然只是请求，永远不构成批准）：

   | 值 | 含义 | 典型用途 |
   |---|---|---|
   | `now` | 马上叫醒收件方（调用唤醒适配器，§5.8） | 在等对方回复、出错了、需要人来决定 |
   | `batch` | 不叫醒，存进收件箱，等对方下次醒来一起处理（**缺省**） | 日报回执、状态更新 |
   | `none` | 只存档，不提醒，不出现在待处理列表里 | 日志、留底 |

   **最终级别由收件方决定**，按以下顺序计算，只会降级、不会升级：
   1. 上限：`wake_policy` 里给这个发件方设的上限（匹配规则同 `allow`：主机名 / FQDN / `tag:` / `user:`，按配置顺序第一条命中生效；都没命中用 `default_max`，缺省 `batch`）。实际 = min(请求, 上限)，顺序为 `none < batch < now`。
   2. 夜间静音：当前时间落在 `quiet_hours` 里时，`now` 降为 `batch`。
   3. 频率限制：同一发件方**对同一个 agent** 每小时最多 `wake_rate` 次实际 `now`（缺省 6），超出的降为 `batch`。按（发件方, agent）分别计数，所以叫醒 `codex` 不会占掉叫醒 `grok` 的额度。计数只在内存里，重启清零。

   另外两条和 agent 有关（§5.9）：`agents.<名字>.wake_max` 再设一道上限；点名了不存在的 agent，`now` 降为 `batch`，谁都不叫醒。

   收件方把结果记在 `received.wake` 里：`{"requested": "now", "effective": "batch", "reason": "quiet hours"}`。只有 `effective == "now"` 才调用唤醒适配器；`batch` / `none` 只存储。
7. **待处理与确认**：`tailmsg inbox --pending` 列出实际级别为 `now` / `batch` 且尚未确认的信；处理完用 `tailmsg ack <id> [...]` 标记，记录里会多一个顶层字段 `acked_at`。这只是本机记账，**不会通知发件方**；要回执就回一封 `reply_to` 的信。

8. **唤醒接口（wake adapter）**

   tailmsg 只定义一个“虚拟接口”：**有一封需要马上处理的信，请叫醒你的 agent**。具体怎么叫醒（Codex 的 app-server、Claude Code 的 session、Grok 的 routine……）由每台机器自己实现一个适配器，在 `config.json` 的 `wake_adapter` 里配置。

   **契约**（所有适配器都必须遵守）：

   | 项 | 规定 |
   |---|---|
   | 何时调用 | 只在实际 wake == `now` 时（已经过上限、夜间静音、频率限制），每封新信最多触发一轮 |
   | 输入 | 信的 `id` + 收件箱 JSON 路径 `inbox/<id>.json`（webhook 只给 id 和摘要，见下） |
   | 幂等 | 可能被重试、也可能被手动 `tailmsg wake <id>` 再调一次，同一 `id` 重复调用不能产生重复副作用 |
   | 要快 | 只负责“通知”，不在适配器里干活；单次超时 `hook_timeout_s`（缺省 120 秒） |
   | 成功 | `exec`：退出码 0；`webhook`：HTTP 2xx（3xx 不跟随，算失败，避免把密钥转发到别处） |
   | 失败 | 按 `wake_backoff_s × 2^(n-1)` 退避重试，总共最多 `wake_attempts` 次（缺省 3 次：立即、2 秒后、4 秒后），仍失败就放弃并写日志。信**仍在收件箱里**、未 ack，所以 `inbox --pending` 能看到（标记 `!`），agent 下次醒来照样会处理；也可以手动 `tailmsg wake <id>` 重试 |
   | 记录 | 结果写进收件箱记录的顶层字段 `wake_delivery`：`{"adapter","ok","attempts","at","error"}`，不含任何密钥或 URL |
   | 安全 | 适配器**只通知，不执行信里的内容**；正文不进入命令行、不经过 shell。信永远只是请求，不是批准 |

   **两种内置类型**：

   - `exec`：`{"type": "exec", "argv": ["/path/to/wake-script"]}`。执行 `argv + [信的 JSON 路径]`，不经过 shell；另外设置环境变量 `TAILMSG_MSG_ID`、`TAILMSG_MSG_PATH`。
   - `webhook`：
     ```json
     {"type": "webhook",
      "url_env": "TAILMSG_WAKE_URL",        // 或 "url_file": "/home/me/.secrets/tailmsg-wake-url"
      "secret_env": "TAILMSG_WAKE_SECRET",  // 或 "secret_file": "...", 也可都不写（不带鉴权头）
      "header": "Authorization",            // 缺省 Authorization
      "scheme": "Bearer"}                   // 缺省 Bearer，发 "Bearer <secret>"；写 "" 则直接发原始密钥
     ```
     向 URL `POST` 一个很小的 JSON（`Content-Type: application/json`）：
     ```json
     {"v": 1, "event": "tailmsg.wake", "id": "9f2c4e1ab07d33c5", "from": "dev-server.example.ts.net",
      "kind": "request", "text_preview": "正文前 200 个字符", "received_at": "2026-10-09T02:00:00Z"}
     ```
     `from` 取 Tailscale 认证过的身份，不是信封自报的。信点名了 agent 时（或收件方配了 `default_agent`），请求体多一个 `"agent"`；信里有 `from_agent` 时再多一个 `"from_agent"`。单 agent 的旧配置请求体不变。被叫醒的一方拿到 `id` 后，通过 tailmsg 自己去读整封信、拉附件。

     **URL 和密钥只从环境变量或权限为 0600 的文件里读**（文件权限更宽会直接拒绝），每次调用时现读，换密钥不用重启；它们**绝不写进** config、日志或收件箱，出错信息里也会被替换成 `<url>` / `<secret>`。

   **各家 agent 的适配思路**：内置的是通用 `exec` / `webhook` 传输；Grok Bot 端到端尚未验证。可选 Codex macOS 适配器见 [CODEX_LOCAL.md](CODEX_LOCAL.md)。

   - **Grok**：`webhook` 类型，URL 是一个由 webhook 触发的 Grok routine，密钥放在 routine 要求的请求头里。Grok 被叫醒后在自己的 box 上运行 `tailmsg inbox --pending` / `show` / `fetch` 处理信件，处理完 `ack`。
   - **Codex**（服务器）：`exec` 类型，一个小脚本读取 `TAILMSG_MSG_ID`，通过 Codex app-server 用 `thread/queue/add` 把“有一封 tailmsg 新信 `<id>`，请用 `tailmsg show <id>` 查看”排进既定线程，再 `thread/queue/start`；不要用 `turn/start`，不自动批准命令，按 `id` 去重保证幂等。
   - **Claude Code**（家里电脑）：`exec` 类型，脚本把同样一句通知推进 Claude Code 的 channel；或者跑 `claude -p --resume <session> "有一封 tailmsg 新信 <id>…"`。权限按 Claude Code 自己的设置，不要在脚本里放宽。

9. **点名 agent（@agent）**

   一台机器上往往不止一个 agent（比如 Grok 的 box 上有本助手 `grok` 和 `私信联系人`）。它们共用同一个 tailmsg、同一个收件箱目录，**不做隔离**（同一台机器、同一个系统用户，本来就互相读得到）；点名只解决一件事：**只叫醒被点名的那一个**。

   - **地址写法**：`<机器>[:端口]/<agent>`，例如 `dev-server/codex`、`bot-box/私信联系人`。等价于 `send <机器> --agent <agent>`；两种都写且不一致时报错。
   - **收件方解析**（结果记进 `received.agent` 和 `received.agent_status`）：

     | 信里的 `agent` | `received.agent` | `agent_status` | 叫醒谁 |
     |---|---|---|---|
     | 没写 | `default_agent`（没配就是 `null`） | `default` | 默认 agent 的适配器 |
     | 写了，且在 `agents` 里或等于 `default_agent` | 该名字 | `known` | 该 agent 的适配器 |
     | 写了，但本机没有这个 agent | 该名字 | `unknown` | **谁都不叫醒**（`now` 降为 `batch`），照常 202 存下并写日志 |

   - **选适配器**：用 `agents.<名字>.wake_adapter`；没配的话，**只有默认 agent** 回退到顶层 `wake_adapter` / `on_message`（兼容单 agent 的旧配置），其他 agent 没有适配器就不叫醒，信留在 `inbox --pending`。`exec` 适配器多一个环境变量 `TAILMSG_AGENT`。
   - **回信**：发件方配了 `self_agent`（或用 `--as 名字`）时，信封带上 `from_agent`。收件方 `tailmsg reply <id>` 会自动把回信的 `agent` 设成原信的 `from_agent`，于是只叫醒当初发信的那个 agent。
   - **收件箱**：仍然是一个目录。`tailmsg inbox --agent <名字>`（可加 `--pending`）只看点名给它的信；没写 `agent` 的信算默认 agent 的。点名了未知 agent 的信显示为 `@名字?`。
   - 点名只决定叫醒谁，**不是权限边界**：谁能发信仍然只看 `allow`，信依然只是请求，不是批准。

   示例：

   ```text
   bot-box（Grok 的 box）     agents: grok（本助手，default）、私信联系人   各自一个 Grok routine 的 webhook 适配器
   dev-server（开发服务器）  agents: codex（default）                     exec 适配器 → Codex app-server thread/queue/add

   grok 发：  tailmsg send dev-server/codex "今天的书签简报" --file report.json   # from_agent=grok
   codex 回： tailmsg reply <id> "已入队"                                          # 自动 agent=grok，只叫醒 grok
   发给私信联系人： tailmsg send bot-box/私信联系人 "帮我约周五" --wake now
   ```

### 兼容性

`wake` 是 v1 里新增的**可选**字段，`v` 仍是 `1`，没带 `wake` 的旧发件方照常工作。但**旧版收件方会把含 `wake` 的信当成未知字段返回 400**。所以：
- `tailmsg send` / `reply` 只有显式加了 `--wake` 才会写这个字段，不加就不写（效果等于 `batch`）；
- 确认对方已升级之前，不要给它发 `--wake`；被 400 时去掉 `--wake` 重发即可。

`agent` / `from_agent` 同理，也是 v1 的可选字段，`v` 仍是 `1`：
- **对方升级到支持 @agent 的版本之前，不要点名 agent**（`机器/agent` 或 `--agent`），旧版会 400。显式点名的信被 400 时 tailmsg 不会偷偷去掉 `agent` 重发，免得叫错人。
- `from_agent` 只在配了 `self_agent` 或加了 `--as` 时才写。由 `self_agent` 自动带上的 `from_agent` 如果被旧版收件方以未知字段拒收，`send` 会去掉它、用同一个 `id` 重发一次（第一次没被存下），并在 stderr 提示；`--as` 显式指定的不会被去掉。
- 没写 `agent` 的旧信，新版收件方照常接收，按默认 agent 处理。

## 6. 配置 `~/.tailmsg/config.json`

```json
{
  "port": 7766,
  "allow": ["bot-box", "dev-server", "tag:agent", "user:you@example.com"],
  "tailscale_cmd": ["tailscale"],
  "bind": null,
  "wake_adapter": null,
  "hook_timeout_s": 120,
  "wake_attempts": 3,
  "wake_backoff_s": 2,
  "outbox_ttl_hours": 24,
  "max_msg_bytes": 262144,
  "max_fetch_bytes": 1073741824,
  "wake_policy": {"default_max": "batch", "peers": {"dev-server": "now", "bot-box": "now"}},
  "quiet_hours": {"start": "23:00", "end": "08:00", "tz": "Asia/Tokyo"},
  "wake_rate": 6,
  "self_agent": "grok",
  "default_agent": "grok",
  "agents": {
    "grok": {"wake_adapter": {"type": "webhook", "url_file": "~/.secrets/tailmsg-wake-url-grok",
                              "secret_file": "~/.secrets/tailmsg-wake-secret-grok"}},
    "私信联系人": {"wake_adapter": {"type": "webhook", "url_file": "~/.secrets/tailmsg-wake-url-dm",
                                 "secret_file": "~/.secrets/tailmsg-wake-secret-dm"}, "wake_max": "now"}
  }
}
```

- `allow` 支持三种写法：短主机名或 FQDN、`tag:xxx`、`user:登录邮箱`。为空表示谁的信都不收。
- `tailscale_cmd`：如果 CLI 需要 root，改成 `["sudo","-n","tailscale"]`；macOS 的 App 版改成 `["/Applications/Tailscale.app/Contents/MacOS/Tailscale"]`。
- `wake_adapter`：唤醒适配器，见 §5.8。`null` 表示不叫醒，`now` 的信也只是存起来。旧字段 `on_message: [...]` 等同 `{"type":"exec","argv":[...]}`，两者都写时以 `wake_adapter` 为准。
- `hook_timeout_s` / `wake_attempts` / `wake_backoff_s`：适配器单次超时、总尝试次数、退避基数（秒）。
- `wake_policy`：每个发件方最多能把你叫到哪一级（`now` / `batch` / `none`）。不写就是谁都最多 `batch`，即默认不会叫醒。
- `quiet_hours`：夜间静音窗口，可以跨午夜；`tz` 用 IANA 时区名，不写用本机时区；`null` 表示不静音。
- `wake_rate`：每个（发件方, agent）每小时最多几次实际 `now`。
- `self_agent`：本机发信时自己的 agent 名，作为 `from_agent` 发出去，方便对方 `reply` 点名回来。不写就不带。
- `default_agent`：没点名的信归谁。它可以不在 `agents` 里；没在 `agents` 里配适配器时用顶层 `wake_adapter`。
- `agents`：`名字 → {"wake_adapter": {...}, "wake_max": "now|batch|none"}`，两项都可省略（`{}` 表示这个 agent 存在、能收信，但暂时不叫醒）。适配器格式同 §5.8。名字不合法时 tailmsg 拒绝启动。
- 通信本身不需要任何密钥，身份全部来自 Tailscale。唯一可能用到的密钥是 webhook 适配器访问本地 agent 的凭证，只放在环境变量或 0600 文件里。

## 7. 网络层要求（tailnet 策略）

- 收件机器上 Tailscale 不能开 shields-up（`tailscale set --shields-up=false`），否则别的机器连不进来。
- 建议在 ACL / grants 里只放行 `tcp:7766`，并且只允许互相通信的这几台机器访问。Tailscale 的访问策略和 tailmsg 自己的 `allow` 白名单是两道独立的门。

## 8. 刻意不做的事

不做中心服务器，不做任务队列，不做已读回执（`ack` 只是本机记账），不做“完成”状态，不做消息重投（只有唤醒适配器会有限重试），也不做内容加密：WireGuard 已经加密了，身份由 Tailscale 保证。需要回执的话，就回一封 `reply_to` 的信。
