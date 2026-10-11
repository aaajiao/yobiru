# What has been verified

Yobiru 0.4.1. This page separates what has actually been run from what has not. It is not a
security guarantee: the tests show the behaviors listed below, nothing more.

## Branding patch (0.4.1)

This release updates the shared icon, Codex brand color, Cursor logo reference and README images.
The only runtime change is the reported product version; protocol and behavior remain at 0.4.0.
`tools/build_plugin.py check` and all 16 existing packaging tests passed, including the installed
Codex manifest validator and reproducible archive checks. Both release archives carry the same
approved icon as yobiru.com. README image paths and the updated demonstration were inspected.
The runtime and real-network test results below are retained from 0.4.0, not claimed as new runs.

## Automated checks in this repository

For 0.4.0, the packaged/loopback suite and bridge suites passed, as did the 59-check self-test
on one host's real Tailscale interface. Access-policy tests cover the fresh tailnet default,
legacy allow-list preservation, backed-up explicit migration, deny precedence, live policy reload,
recipient-bound attachments, and revocation before each exec/webhook retry and bridge dispatch.
The two-receiver workflow includes a fresh tailnet receiver and a legacy receiver, then migrates
and denies/unblocks peers without restarting either process.

Maintenance tests cover offline recovery across invocations, known-unsent retries with the same
envelope, held uncertain outcomes, blocked-peer recovery, no newly discovered recipients or sessions,
release metadata caching, concurrent runs, cancelled notices and damaged/mismatched saved routes.
They use injected release metadata and loopback receivers. Printed timer definitions were checked;
no operating-system timer was installed and no real peer was upgraded in this verification.

| Check | How to run | What it shows |
|---|---|---|
| Package consistency and hygiene | `python3 tools/build_plugin.py check` | Generated manifests and bundled copies are current; `plugin.json` fits the closed Agent Plugins 1.0.0 schema; the Codex and Cursor manifests and both marketplace files follow the documented fields; skills are well formed; no symlinks; no runtime state, tailnet names, Tailscale addresses, home paths, emails, UUIDs or token-like strings in the plugin or docs. |
| Codex manifest | `python3 <codex skills>/plugin-creator/scripts/validate_plugin.py plugins/yobiru` | Passes the Codex plugin-creator validator (run by `tests/test_build.py` when the validator is installed). |
| Plugin tests | `python3 -m unittest discover -s tests` (148 tests) | Reproducible archives that contain every skill. The unpacked archive, outside the source tree, in a path with spaces, works from any directory and through a symlinked launcher. Read-only commands create nothing. Setup and wake-config keep existing agents, adapters and keys and refuse silent replacements. Unsafe names and message ids are rejected. The launchd plist parses. On Linux, `codex-bridge install` forwards to the Linux installer without writing into the plugin cache, the installed bridge keeps working after the plugin cache is moved away, `status` reports a listening or stale socket, and file-only install and rollback make no systemctl calls. An addressed message with a file wakes only the named agent; the file is fetched and verified, the reply goes back to the sender's agent, and ack stays local. Receivers run on loopback with a fake Tailscale CLI. |
| Codex bridge (macOS and Linux) | `python3 -m unittest -v test_codex_bridge test_codex_queue_client test_install_codex_bridge test_install_codex_bridge_linux` (119 tests) | Queue, official-CLI interface, macOS install, rollback and dry run, all offline. Kind routing (0.2.1): excluded kinds are routed only after path, sender, allow list, target agent, wake and ack checks; only an unacked effective-`now` direct wake runs the local handler, with only the canonical inbox path appended, no shell and empty stdin; none, batch, acked and unverified records never run it; scans and dispatch never queue or forward excluded kinds, including rows queued earlier; malformed routing config fails before any side effect; failing, slow and missing handlers give fixed outcomes, the handler budget ends before the receiver's `hook_timeout_s`, and a receiver-length outer timeout leaves no handler or child process behind. Both installers validate routing in dry runs and keep it, tuned timing, custom dispatcher instructions (path and the bytes of the local file, also after rollback), the recorded Codex CLI and authorized replies on upgrade; the whole existing queue suite also runs again with routing enabled. Linux: a real temporary AF_UNIX socket and a fake `codex` executable run as a subprocess receive the exact `queue --thread UUID --message ... --remote unix:///...` argv and the pinned `CODEX_HOME`; missing, stale, foreign or non-socket endpoints and probe failures are known-unsent retries without running the CLI; failed, timed-out and receipt-less attempts stay uncertain and are never resent; reconciliation reads only the pinned Codex home, without the socket. The installer runs with an isolated HOME and a fake stateful `systemctl`: dry run writes and runs nothing, `--no-start` never calls systemctl, invalid input and unsafe targets are refused before writes, the fixed thread, queue and per-user unit are guarded, paths with spaces, `%` and `$` are escaped, pinned settings survive an upgrade from another ambient account, and upgrade and rollback restore files and service state while keeping inbox and queue (rollback fails closed when the service cannot be confirmed stopped). |
| Onboarding, discovery and updates (0.3.0) | `python3 -m unittest -v test_onboarding test_discovery test_update` in `tests/` | Onboarding per platform with injected facts: missing Tailscale (official Linux script with root/sudo; blocked without privileges or `/dev/net/tun`; native app on macOS/Windows, PowerShell-safe Windows paths), daemon unavailable, permission denied, logged out and device approval, separate delivery and session steps; `onboard` and `doctor` create nothing. `/info`: 403 for peers not on the allow list before any title lookup; projection without paths, argv, URLs, previews or unbound sessions; truncation flag; Codex binding cannot override the bridge. Native names: cache keyed by source, unsupported results never cached, rename seen after the TTL, cold lookups answer `pending`/stale within the wait, timeouts kill the whole process group, bounded output. Clients: legacy 404, 403, unreachable, malformed, hostile escape characters, non-UUID Codex ids, and slow peers (keep-alive, HTTP/1.0 close, slow headers) bounded by a total deadline. Updates: SemVer with prereleases (invalid leading zeros rejected), release parsing that ignores text and foreign URLs, stable/preview channels, no stable release, bounded fetch errors. Notices: dry run writes nothing, verified local upgrade and published release required, 202 receipt with matching id, invalid receipt and interrupted dispatch are uncertain and never resent (reconciliation reuses the identical envelope), 403/400 definite, refused connections stay pending, explicit and remembered offline peers persist without fabricated addresses, legacy receivers keep the explicit agent, stale plans cannot downgrade uncertain state, lock contention refused. Bridge: metadata-only `thread/read` (includeTurns false, never preview), `session-name` creates no queue, macOS and Linux upgrades keep session names. Plus an end-to-end loopback `agents`/`peers` run between two real 0.3.0 receivers. No real Tailscale, GitHub, Codex runtime or peer is contacted. |
| Real Tailscale, one host | `sh selftest.sh` (59 checks) | Temporary receivers on the host's own Tailscale address (ports 7866-7883 and a stub webhook on 127.0.0.1:7879): Tailscale identity and allow list, file transfer with SHA-256, expiry, spoofing and SSRF rejection, wake levels and policies, exec and webhook adapters, @agent addressing, and compatibility with an older receiver (`tests/fixtures/tailmsg_v1_pre_agents.py`). Passed on one Linux host with 0.4.0; production port and data home are not used. |

## Hosts

The Codex results below were observed with Yobiru 0.1.0 and were not repeated for 0.2.x. The 0.2.x
changes (the Codex bridge, its installers and `codex-bridge` in `yobiru.py`) are covered only by the
offline and extracted-package tests above.

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
- The Linux bridge against a real Codex app-server and a real user systemd manager (all Linux bridge
  checks above use fakes).
- A Grok Bot routine webhook wake end to end, and a long-running receiver on a Grok Bot computer.
- 0.4.0 against real systems: a real Tailscale install or sign-in through `onboard`, `/info` between two
  real machines, a native name read from a live Codex app-server, `update check` against the live
  GitHub API, and `update notify` to real peers.

## Reproducing the release archives

`python3 tools/build_plugin.py release` writes `dist/yobiru-<version>-plugin.zip`,
`dist/yobiru-<version>-marketplace.zip` and `dist/SHA256SUMS`. The archives are byte-for-byte
reproducible from the same source tree (see [RELEASING.md](RELEASING.md)), so a published checksum can
be checked by rebuilding.
