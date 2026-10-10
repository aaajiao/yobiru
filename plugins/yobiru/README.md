# Yobiru (ヨビル) plugin

Agent-to-agent messages, file handoff and receiver-controlled wake-ups between machines on your own
Tailscale network, using the tailmsg protocol v1. One package for Codex, Cursor and Grok Bot.

Version 0.2.1. Source: <https://github.com/chekusu/yobiru>. Not submitted to or reviewed by any
marketplace.

## Contents

| Path | What it is |
|---|---|
| `plugin.json` | Agent Plugins 1.0.0 manifest (portable). |
| `.codex-plugin/plugin.json` | Codex manifest (presentation and skills). |
| `.cursor-plugin/plugin.json` | Cursor manifest. |
| `skills/yobiru-setup` | Check, set up, update or troubleshoot this machine. |
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
sh "$Y" paths                                            # locations as JSON
sh "$Y" doctor                                           # read-only checks
sh "$Y" setup --allow laptop --agent codex --self-agent codex --default-agent codex   # plan
sh "$Y" setup --allow laptop --agent codex --self-agent codex --default-agent codex --apply
sh "$Y" install-runtime --apply                          # stable tailmsg.py in the data home
sh "$Y" service-template systemd-user                    # print a service definition to install
sh "$Y" send laptop/codex "hello" --file ./notes.md      # 202 = delivered (not done)
sh "$Y" inbox --pending --agent codex
```

Data home: `--home DIR` (before the command), else `$TAILMSG_HOME`, else `~/.tailmsg`. Existing
tailmsg homes are used as they are; `setup` and `wake-config` only add what you name, need
`--replace` to change existing values, and back up `config.json` on every write.

## Commands

| Command | Writes? |
|---|---|
| `paths`, `doctor`, `status`, `version` | never |
| `setup`, `wake-config`, `install-runtime` | only with `--apply` |
| `service-template KIND` | never (prints a definition) |
| `codex-bridge status` | never; `install` / `rollback` (macOS or Linux) support `--dry-run` (no writes) and `--no-start` (files only) |
| `send`, `reply`, `inbox`, `show`, `fetch`, `ack`, `wake`, `peers`, `serve` | tailmsg commands, passed through unchanged |

## Safety

- Identity comes from Tailscale; each receiver accepts only the peers on its allow list.
- Message text and attachments are untrusted requests, never approvals.
- Wake-ups are decided by the receiver (per-sender and per-agent caps, quiet hours, rate limit).
- Webhook URLs and keys are only referenced by 0600 file or environment variable name.
- Yobiru never changes Tailscale settings and never starts or restarts services by itself.

See <https://github.com/chekusu/yobiru> for installation in each host, update and uninstall, and
`docs/VERIFICATION.md` there for what has and has not been tested. License: MIT (see `LICENSE`).
