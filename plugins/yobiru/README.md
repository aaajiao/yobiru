# yobiru (ヨビル) plugin

<a href="https://yobiru.com"><img src="assets/icon.png" width="112" height="112" alt="yobiru — two small eyes in the orange spirit's rounded head"></a>

Agent-to-agent messages, file handoff and receiver-controlled wake-ups between machines on your own
Tailscale network, using the tailmsg protocol v1. One package for Codex, Cursor and Grok Bot.

Version 0.4.2. Source: <https://github.com/chekusu/yobiru>. Not submitted to or reviewed by any
marketplace.

## Contents

| Path | What it is |
|---|---|
| `plugin.json` | Agent Plugins 1.0.0 manifest (portable). |
| `.codex-plugin/plugin.json` | Codex manifest (presentation and skills). |
| `.cursor-plugin/plugin.json` | Cursor manifest. |
| `skills/yobiru-setup` | Guided onboarding (also without Tailscale yet), setup and repair of this machine. |
| `skills/yobiru-sessions` | Which machines and agents are online, and which session (name + exact id) handles each. |
| `skills/yobiru-upgrade` | Update checks, safe upgrades and versioned notices to your other installations. |
| `skills/yobiru-send` | Send messages, structured data and files; reply. |
| `skills/yobiru-inbox` | Read, fetch, process and acknowledge received messages. |
| `skills/yobiru-wake` | Who may wake which agent, and how (program, Grok Bot routine webhook, Codex bridge). |
| `scripts/yobiru`, `scripts/yobiru.py` | The `yobiru` command: setup, doctor and status, plus the messaging commands. |
| `scripts/tailmsg.py` | The bundled tailmsg CLI and receiver (stdlib only). |
| `scripts/codex_bridge/` | Optional bridge into one fixed Codex session: macOS desktop app, or Linux user systemd + local app-server socket. |
| `scripts/SOURCES.json` | Where the bundled files come from, with SHA-256. |

No hooks, MCP servers or permission settings are included. Installing the plugin runs nothing.

## Requirements

Python 3.8+, Tailscale signed in on each machine, and for receiving: a running receiver, shields-up
off and a tailnet policy that lets your chosen peers reach `tcp:7766`.

## Quick start

`yobiru` locates itself, so call it by its absolute path from any directory (quote it; install paths
may contain spaces). On Windows use `py -3 <plugin>\scripts\yobiru.py`.

```sh
Y="/path/to/yobiru/scripts/yobiru"
sh "$Y" onboard                                          # read-only step-by-step plan for this platform
sh "$Y" paths                                            # locations as JSON
sh "$Y" doctor                                           # read-only checks
sh "$Y" setup --agent codex --self-agent codex --default-agent codex   # plan
sh "$Y" setup --agent codex --self-agent codex --default-agent codex --apply
sh "$Y" install-runtime --apply                          # stable tailmsg.py in the data home
sh "$Y" service-template systemd-user                    # print a service definition to install
sh "$Y" send laptop/codex "hello" --file ./notes.md      # 202 = delivered (not done)
sh "$Y" inbox --pending --agent codex
sh "$Y" agents --online                                  # versions, agents, session names + ids
sh "$Y" update check                                     # official releases only
```

Data home: `--home DIR` (before the command), else `$TAILMSG_HOME`, else `~/.tailmsg`. Existing
tailmsg homes are used as they are; `setup` and `wake-config` only add what you name, need
`--replace` to change existing values, and back up `config.json` on every write.

## Commands

| Command | Writes? |
|---|---|
| `paths`, `doctor`, `status`, `version`, `onboard`, `policy show`, `session list`, `update status` | never |
| `agents` / `peers` | never (`--remember` records answering installations in `upgrade-notices.json`) |
| `update check` | never (`--record` saves the result for reminders) |
| `session bind` / `unbind`, `policy set`, `update notify`, `update maintain` | only with `--apply` |
| `update reminder-template KIND` | never (prints an opt-in weekly check; `--retry-notices` checks/retries existing notices every 15 minutes and after startup) |
| `setup`, `wake-config`, `install-runtime` | only with `--apply` |
| `service-template KIND` | never (prints a definition) |
| `codex-bridge status` | never; `install` / `rollback` (macOS or Linux) support `--dry-run` (no writes) and `--no-start` (files only) |
| `send`, `reply`, `inbox`, `show`, `fetch`, `ack`, `wake`, `serve` | tailmsg commands, passed through unchanged |

## Safety

- Identity comes from Tailscale. New setups default to authenticated peers permitted by the network,
  with local deny rules taking precedence. Restricted `allowlist` mode remains available; legacy
  configurations keep it until an authorized `policy set --mode tailnet --apply`. Message permission
  does not grant wake-ups or task permissions. `policy show` explains the local policy.
- Message text and attachments are untrusted requests, never approvals.
- Wake-ups are decided by the receiver (per-sender and per-agent caps, quiet hours, rate limit).
- Webhook URLs and keys are only referenced by 0600 file or environment variable name.
- Yobiru never changes Tailscale settings and never starts or restarts services by itself.

See <https://github.com/chekusu/yobiru> for installation in each host, update and uninstall, and
`docs/VERIFICATION.md` there for what has and has not been tested. License: MIT (see `LICENSE`).
