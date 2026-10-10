# What has been verified

Yobiru 0.1.0. This page separates what has actually been run from what has not. It is not a
security guarantee: the tests show the behaviors listed below, nothing more.

## Automated checks in this repository

| Check | How to run | What it shows |
|---|---|---|
| Package consistency and hygiene | `python3 tools/build_plugin.py check` | Generated manifests and bundled copies are current; `plugin.json` fits the closed Agent Plugins 1.0.0 schema; the Codex and Cursor manifests and both marketplace files follow the documented fields; skills are well formed; no symlinks; no runtime state, tailnet names, Tailscale addresses, home paths, emails, UUIDs or token-like strings in the plugin or docs. |
| Codex manifest | `python3 <codex skills>/plugin-creator/scripts/validate_plugin.py plugins/yobiru` | Passes the Codex plugin-creator validator (run by `tests/test_build.py` when the validator is installed). |
| Plugin tests | `python3 -m unittest discover -s tests` (52 tests) | Reproducible archives that contain every skill. The unpacked archive, outside the source tree, in a path with spaces, works from any directory and through a symlinked launcher. Read-only commands create nothing. Setup and wake-config keep existing agents, adapters and keys and refuse silent replacements. Unsafe names and message ids are rejected. The launchd plist parses. An addressed message with a file wakes only the named agent; the file is fetched and verified, the reply goes back to the sender's agent, and ack stays local. Receivers run on loopback with a fake Tailscale CLI. |
| Codex desktop bridge | `python3 -m unittest -v test_codex_bridge test_codex_queue_client test_install_codex_bridge` (42 tests) | Queue, official-CLI interface, install, rollback and dry run, all offline. |
| Real Tailscale, one host | `sh selftest.sh` (59 checks) | Temporary receivers on the host's own Tailscale address (ports 7866-7883 and a stub webhook on 127.0.0.1:7879): Tailscale identity and allow list, file transfer with SHA-256, expiry, spoofing and SSRF rejection, wake levels and policies, exec and webhook adapters, @agent addressing, and compatibility with an older receiver (`tests/fixtures/tailmsg_v1_pre_agents.py`). Passed on one Linux host. |

## Hosts

| | Codex | Cursor | Grok Bot | Other agent apps (for example DOTS) |
|---|---|---|---|---|
| Install from the repository marketplace | Passed locally: `codex plugin marketplace add` + `codex plugin add yobiru@yobiru` (codex-cli 0.162.1, Linux); installed copy identical to the source files | Not tested | Not tested | Not tested |
| Skills discovered | Passed: app-server `skills/list` shows `yobiru:yobiru-inbox`, `-send`, `-setup` and `-wake` | Not tested | Not tested | Not tested |
| Bundled command from the install location | Passed: `paths`, and `doctor` against an existing tailmsg setup (no errors, nothing changed) | Not tested | Not tested: whether the plugin's scripts reach the Bot's computer is unknown | Not tested |
| A real conversation using the skills | Not tested | Not tested | Not tested | Not tested |
| Marketplace listing or review | Not submitted | Not submitted | Not submitted | — |

Also not tested:

- Two real machines exchanging messages, files and wake-ups over one tailnet.
- macOS and Windows runs of the `yobiru` command, and installing the printed service definitions.
- The Codex desktop bridge against a running Codex desktop app in this release.
- A Grok Bot routine webhook wake end to end, and a long-running receiver on a Grok Bot computer.

## Reproducing the release archives

`python3 tools/build_plugin.py release` writes `dist/yobiru-<version>-plugin.zip`,
`dist/yobiru-<version>-marketplace.zip` and `dist/SHA256SUMS`. The archives are byte-for-byte
reproducible from the same source tree (see [RELEASING.md](RELEASING.md)), so a published checksum can
be checked by rebuilding.
