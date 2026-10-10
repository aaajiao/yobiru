#!/usr/bin/env python3
"""Yobiru: portable launcher, setup and doctor around the bundled tailmsg CLI (Python 3.8+, stdlib only).

Yobiru speaks the tailmsg protocol v1. This file locates the bundled tailmsg.py next to itself, so it
works from any plugin install or cache path (including paths with spaces) and from any working
directory. All state lives in the tailmsg data home (--home, $TAILMSG_HOME or ~/.tailmsg), never in
the plugin directory, so existing tailmsg setups keep working unchanged.

Safety contract: read-only unless a command is given --apply. Nothing here starts, stops or restarts
services, changes Tailscale login/ACL/shields-up settings, or sends a message on its own; message
commands are passed to tailmsg.py exactly as given (argument arrays, never a shell).
"""
import argparse
import datetime as dt
import getpass
import hashlib
import http.client
import importlib.util
import json
import os
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.dont_write_bytecode = True   # keep installed plugin directories pristine (no __pycache__)

PRODUCT = "yobiru"
DISPLAY = "Yobiru"
HOME_ENV = "TAILMSG_HOME"        # shared with tailmsg so an existing ~/.tailmsg keeps working
DEFAULT_HOME = "~/.tailmsg"
PYTHON_ENV = "YOBIRU_PYTHON"     # interpreter for the bundled tailmsg.py (default: the one running this file)
MIN_PYTHON = (3, 8)
HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
CORE = HERE / "tailmsg.py"
BRIDGE = HERE / "codex_bridge"
CORE_COMMANDS = ("serve", "send", "reply", "inbox", "show", "fetch", "ack", "wake", "peers")
LEVELS = ("none", "batch", "now")
SERVICE_LABEL = "dev.tailmsg"    # receiver service names stay tailmsg's, matching existing installs
PEER_RULE_RE = re.compile(
    r"^(?:tag:[A-Za-z0-9][A-Za-z0-9_-]{0,62}|user:[^\s/]{1,253}|"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)\Z")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d\Z")

USAGE = """usage: {p} [--home DIR] <command> [args]

{d} sends messages, files and wake-up requests between agents on your own Tailscale network
(tailmsg protocol v1). Data home: --home, ${env} or {home}.

Check and set up (read-only unless --apply):
  paths                      where the plugin, bundled CLI, Python and data home are (JSON)
  doctor [--json]            check Python, Tailscale, config, receiver and wake adapters
  status [--json]            summarize config, agents, wake settings and inbox counts
  setup ... [--apply]        create or extend config.json with an explicit allow list / agent names
  wake-config ... [--apply]  receiver-side wake policy and per-agent wake adapters
  install-runtime [--upgrade] [--apply]
                             copy the bundled tailmsg.py into the data home for a durable `serve`
  service-template {{systemd,systemd-user,launchd,windows}}
                             print a reviewable service definition (never installs it)
  codex-bridge {{status,install,rollback}} ...
                             optional macOS Codex desktop bridge (install/rollback support --dry-run)
  version

Messaging (passed unchanged to the bundled tailmsg.py):
  send PEER[/AGENT] TEXT [--file F]... [--wake now|batch|none] [--agent A] [--kind K]
  reply ID TEXT [--file F]...      inbox [--pending] [--agent A]      show ID      fetch ID
  ack ID [ID...]                   wake ID                            peers        serve
Run `{p} <command> --help` for details.
"""


# ---------------------------------------------------------------- basics
def die(msg, code=1):
    print("%s: error: %s" % (PRODUCT, msg), file=sys.stderr)
    sys.exit(code)


def sha256_file(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plugin_version():
    try:
        return json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8")).get("version") or "unknown"
    except (OSError, ValueError):
        return "unknown"


_core = None


def core():
    """The bundled tailmsg.py as a module (definitions only; its main() is not run)."""
    global _core
    if _core is None:
        if not CORE.is_file():
            die("bundled tailmsg.py is missing (expected %s); reinstall the plugin" % CORE, 2)
        spec = importlib.util.spec_from_file_location("yobiru_bundled_tailmsg", str(CORE))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _core = mod
    return _core


def resolve_home(arg):
    if arg:
        return Path(arg).expanduser().absolute(), "--home"
    if os.environ.get(HOME_ENV):
        return Path(os.environ[HOME_ENV]).expanduser().absolute(), "$" + HOME_ENV
    return Path(DEFAULT_HOME).expanduser().absolute(), "default"


def read_config(home):
    """-> (raw dict or None, error or None). Never creates anything."""
    path = home / "config.json"
    if not path.is_file():
        return None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, "cannot read %s: %s" % (path, e)
    if not isinstance(raw, dict):
        return None, "%s must contain a JSON object" % path
    return raw, None


def config_problems(cfg):
    """-> (fatal, warnings) for values tailmsg would fail on or silently ignore. cfg = parse_config() output."""
    fatal, warn = [], []
    port = cfg.get("port")
    try:
        ok = not isinstance(port, bool) and 1 <= int(port) <= 65535
    except (TypeError, ValueError):
        ok = False
    if not ok:
        fatal.append("port must be an integer 1-65535 (got %s)" % json.dumps(port))
    allow = cfg.get("allow")
    if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
        fatal.append("allow must be a list of strings (got %s)" % json.dumps(allow, ensure_ascii=False)[:80])
    tc = cfg.get("tailscale_cmd")
    if not isinstance(tc, list) or not tc or not all(isinstance(x, str) and x for x in tc):
        fatal.append("tailscale_cmd must be a non-empty list of strings")
    if cfg.get("bind") is not None and not isinstance(cfg.get("bind"), str):
        fatal.append("bind must be a string or null")
    pol = cfg.get("wake_policy")
    if pol is not None and not isinstance(pol, dict):
        warn.append("wake_policy is not an object; tailmsg caps every sender at batch")
    elif pol:
        if pol.get("default_max", "batch") not in LEVELS:
            warn.append("wake_policy.default_max is not now/batch/none; tailmsg uses batch")
        peers = pol.get("peers") or {}
        if not isinstance(peers, dict) or not all(v in LEVELS for v in peers.values()):
            warn.append("wake_policy.peers values must be now/batch/none; invalid ones act as batch")
    q = cfg.get("quiet_hours")
    if q is not None and (not isinstance(q, dict) or not HHMM_RE.match(str(q.get("start")))
                          or not HHMM_RE.match(str(q.get("end")))):
        warn.append("quiet_hours must be {start: HH:MM, end: HH:MM[, tz]}; tailmsg ignores it as written")
    for key in ("wake_rate", "wake_attempts", "hook_timeout_s", "wake_backoff_s", "outbox_ttl_hours",
                "max_msg_bytes", "max_fetch_bytes"):
        v = cfg.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            warn.append("%s must be a non-negative number (got %s)" % (key, json.dumps(v)))
    return fatal, warn


def find_exe(name):
    if os.sep in name or (os.altsep and os.altsep in name):
        return name if os.path.isfile(name) and os.access(name, os.X_OK) else None
    return shutil.which(name)


def run_tailscale(cmd, args, timeout=10):
    """-> (stdout or None, error or None). Read-only queries only."""
    try:
        p = subprocess.run(list(cmd) + list(args), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "not found"
    except subprocess.TimeoutExpired:
        return None, "timed out after %ss" % timeout
    except OSError as e:
        return None, "%s: %s" % (type(e).__name__, e)
    if p.returncode != 0:
        return None, ((p.stderr or p.stdout).strip()[-300:] or "exit %d" % p.returncode)
    return p.stdout, None


def probe_health(host, port, timeout=2.0):
    try:
        c = http.client.HTTPConnection(host, int(port), timeout=timeout)
        c.request("GET", "/health")
        r = c.getresponse()
        body = r.read(4096)
        c.close()
    except (OSError, http.client.HTTPException, ValueError) as e:
        return {"url": "http://%s:%s/health" % (host, port), "reachable": False, "error": type(e).__name__}
    out = {"url": "http://%s:%s/health" % (host, port), "reachable": True, "http_status": r.status}
    try:
        j = json.loads(body.decode("utf-8"))
        if isinstance(j, dict):
            out.update({k: j[k] for k in ("ok", "v", "node") if k in j})
    except ValueError:
        pass
    return out


def agent_names(cfg, agents):
    """agents the receiver wakes (same order as tailmsg serve's startup log); [None] = single-agent setup."""
    dflt = cfg.get("default_agent")
    if not agents and dflt is None:
        return [None]
    return ([dflt] if dflt is not None and dflt not in agents else []) + list(agents)


def secret_source_checks(adapter):
    """Checks for webhook url/secret sources without ever reading their values."""
    notes = []
    for key in ("url", "secret"):
        if adapter.get(key + "_file"):
            p = Path(adapter[key + "_file"]).expanduser()
            try:
                st = p.stat()
            except OSError:
                notes.append(("error", "%s_file %s does not exist" % (key, p)))
                continue
            if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                notes.append(("error", "%s_file %s must be mode 0600 (chmod 600)" % (key, p)))
            elif st.st_size == 0:
                notes.append(("error", "%s_file %s is empty" % (key, p)))
        if adapter.get(key + "_env") and not os.environ.get(adapter[key + "_env"]):
            notes.append(("warn", "$%s is not set in this shell; it must be set in the serve process "
                                  "environment" % adapter[key + "_env"]))
    if adapter.get("type") == "exec":
        exe = str((adapter.get("argv") or [""])[0])
        if not find_exe(exe):
            notes.append(("error", "exec adapter %s is not an executable file" % exe))
    return notes


def inbox_stats(home, default_agent):
    s = {"messages": 0, "pending": 0, "wake_failed": 0, "unknown_agent": 0, "unreadable": 0, "by_agent": {}}
    inbox = home / "inbox"
    if not inbox.is_dir():
        return s
    for p in sorted(inbox.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            env, rv = r["envelope"], r.get("received") or {}
        except (OSError, ValueError, KeyError, TypeError):
            s["unreadable"] += 1
            continue
        s["messages"] += 1
        eff = (rv.get("wake") or {}).get("effective", "batch")
        if eff != "none" and not r.get("acked_at"):
            s["pending"] += 1
            agent = rv["agent"] if "agent" in rv else (env.get("agent") or default_agent)
            key = agent if agent is not None else "(none)"
            s["by_agent"][key] = s["by_agent"].get(key, 0) + 1
        if (r.get("wake_delivery") or {}).get("ok") is False:
            s["wake_failed"] += 1
        if rv.get("agent_status") == "unknown":
            s["unknown_agent"] += 1
    return s


# ---------------------------------------------------------------- facts shared by doctor/status/paths
def gather(home, home_source, probe=True):
    f = {"product": PRODUCT, "plugin_version": plugin_version(), "plugin_root": str(PLUGIN_ROOT),
         "core": str(CORE), "core_present": CORE.is_file(),
         "python": {"executable": sys.executable, "version": "%d.%d.%d" % sys.version_info[:3]},
         "platform": sys.platform, "home": str(home), "home_source": home_source,
         "home_exists": home.is_dir(), "config_path": str(home / "config.json")}
    raw, err = read_config(home)
    f["config_exists"] = (home / "config.json").is_file()
    f["config_error"] = err
    cfg, agents = None, {}
    if raw is not None:
        try:
            cfg, agents = core().parse_config(raw)
        except RuntimeError as e:
            f["config_error"] = str(e)
    f["config_warnings"] = []
    if cfg is not None:
        fatal, f["config_warnings"] = config_problems(cfg)
        if fatal:
            f["config_error"] = "; ".join(fatal)
            cfg = None
    f["cfg"] = cfg
    if cfg is not None:
        f["port"] = int(cfg["port"])
        f["allow"] = list(cfg["allow"])
        f["self_agent"], f["default_agent"] = cfg.get("self_agent"), cfg.get("default_agent")
        f["wake_policy"] = cfg.get("wake_policy")
        f["quiet_hours"] = cfg.get("quiet_hours")
        f["wake_rate"] = cfg.get("wake_rate")
        f["adapters"] = []
        for name in agent_names(cfg, agents):
            entry = {"agent": name, "default": name is not None and name == cfg.get("default_agent"),
                     "wake_max": (agents.get(name) or {}).get("wake_max") if name is not None else None}
            try:
                a = core().Ctx.agent_adapter(_AdapterView(cfg, agents), name)
                entry["adapter"] = core().describe_adapter(a)
                entry["checks"] = secret_source_checks(a) if a else []
            except core().AdapterError as e:
                entry["adapter"] = "MISCONFIGURED: %s" % e
                entry["checks"] = [("error", str(e))]
            f["adapters"].append(entry)
        f["inbox"] = inbox_stats(home, cfg.get("default_agent"))
    ts = {"cmd": None, "found": None, "error": None, "probed": False}
    cmd = (cfg or {}).get("tailscale_cmd") or ["tailscale"]
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
        ts["error"] = "tailscale_cmd in config.json must be a non-empty list of strings"
    else:
        ts["cmd"] = cmd
        ts["found"] = find_exe(cmd[0])
        if not ts["found"]:
            ts["error"] = "%s not found" % cmd[0]
        elif probe:
            ts["probed"] = True
            out, e = run_tailscale(cmd, ["status", "--json"])
            if e:
                ts["error"] = "tailscale status failed: %s" % e
            else:
                try:
                    st = json.loads(out)
                    me = st.get("Self") or {}
                    ts["backend"] = st.get("BackendState")
                    ts["node"] = (me.get("DNSName") or "").rstrip(".").lower() or None
                    ts["ips"] = me.get("TailscaleIPs") or []
                except ValueError:
                    ts["error"] = "tailscale status returned invalid JSON"
            if not ts["error"]:
                out, e = run_tailscale(cmd, ["debug", "prefs"])
                try:
                    ts["shields_up"] = bool(json.loads(out).get("ShieldsUp")) if out else None
                except (ValueError, AttributeError):
                    ts["shields_up"] = None
    f["tailscale"] = ts
    if probe and cfg is not None:
        host = cfg.get("bind") or next((i for i in ts.get("ips") or [] if ":" not in i), None)
        f["receiver"] = probe_health(host, f["port"]) if host else {"reachable": False,
                                                                     "error": "no Tailscale IPv4 address"}
    runtime = home / "tailmsg.py"
    f["runtime"] = {"path": str(runtime), "present": runtime.is_file()}
    if runtime.is_file() and CORE.is_file():
        f["runtime"]["same_as_bundled"] = sha256_file(runtime) == sha256_file(CORE)
    f["codex_bridge_installed"] = (home / "codex-bridge.json").is_file()
    return f


class _AdapterView(object):
    """Just enough of tailmsg's Ctx for Ctx.agent_adapter (which only reads cfg and agents)."""

    def __init__(self, cfg, agents):
        self.cfg, self.agents = cfg, agents


# ---------------------------------------------------------------- paths / doctor / status
def cmd_paths(home, home_source, argv):
    argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " paths", description="print resolved locations (JSON)").parse_args(argv)
    print(json.dumps({
        "plugin_root": str(PLUGIN_ROOT), "plugin_version": plugin_version(),
        "cli": str(HERE / PRODUCT), "python_cli": str(Path(__file__).resolve()), "core": str(CORE),
        "codex_bridge": str(BRIDGE), "skills": str(PLUGIN_ROOT / "skills"),
        "python": sys.executable, "python_version": "%d.%d.%d" % sys.version_info[:3],
        "home": str(home), "home_source": home_source, "config": str(home / "config.json"),
        "config_exists": (home / "config.json").is_file(),
    }, indent=2, ensure_ascii=False))
    return 0


def doctor_checks(f):
    checks = []

    def add(status, name, detail, fix=None):
        checks.append({"status": status, "check": name, "detail": detail, "fix": fix})

    py = f["python"]
    add("ok" if sys.version_info[:2] >= MIN_PYTHON else "error", "python",
        "%s at %s" % (py["version"], py["executable"]),
        None if sys.version_info[:2] >= MIN_PYTHON else "install Python 3.8 or newer")
    add("ok" if f["core_present"] else "error", "plugin",
        "%s %s at %s" % (DISPLAY, f["plugin_version"], f["plugin_root"]),
        None if f["core_present"] else "the bundled tailmsg.py is missing; reinstall the plugin")
    if f["config_error"]:
        add("error", "config", f["config_error"], "fix config.json by hand; %s never overwrites it" % PRODUCT)
    elif not f["config_exists"]:
        add("error", "config", "not set up: no %s (nothing was created)" % f["config_path"],
            "%s setup --allow <peer> --agent <name>   # dry run; add --apply after review" % PRODUCT)
    else:
        add("ok", "config", "%s (port %d, home from %s)" % (f["config_path"], f["port"], f["home_source"]))
        for w in f.get("config_warnings") or []:
            add("warn", "config", w)
        if f["allow"]:
            add("ok", "allow", "accepts messages from: %s" % ", ".join(map(str, f["allow"])))
        else:
            add("warn", "allow", "allow list is empty: every incoming message is rejected",
                "%s setup --allow <peer>   # then --apply" % PRODUCT)
    ts = f["tailscale"]
    if ts.get("cmd") is None:
        add("error", "tailscale", ts["error"], "fix tailscale_cmd in config.json by hand, or use "
            "`%s setup --tailscale-cmd PATH --replace tailscale_cmd`" % PRODUCT)
    elif ts.get("error") and not ts.get("found"):
        hint = "install Tailscale and sign this machine in (https://tailscale.com/download)"
        if sys.platform == "darwin" and Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale").exists():
            hint = ("the Tailscale app CLI exists; run `%s setup --tailscale-cmd "
                    "/Applications/Tailscale.app/Contents/MacOS/Tailscale`" % PRODUCT)
        add("error", "tailscale", ts["error"], hint)
    elif ts.get("error"):
        fix = "make sure tailscaled is running and this machine is signed in (`tailscale up`)"
        if re.search(r"permission denied|access denied|not permitted", ts["error"], re.I):
            fix = "allow your user to query tailscale, or set tailscale_cmd to [\"sudo\",\"-n\",\"tailscale\"]"
        add("error", "tailscale", ts["error"], fix)
    elif not ts.get("probed"):
        add("info", "tailscale", "found %s (state not checked: --no-probe)" % ts["found"])
    else:
        state = ts.get("backend")
        add("ok" if state == "Running" else "error", "tailscale",
            "%s, node %s, ips %s" % (state, ts.get("node"), ", ".join(ts.get("ips") or [])),
            None if state == "Running" else "sign this machine in to Tailscale (`tailscale up`)")
        if ts.get("shields_up") is True:
            add("error", "shields-up", "Tailscale shields-up blocks all incoming connections",
                "only if this machine should receive: `tailscale set --shields-up=false` (the owner's "
                "decision; %s never changes it)" % PRODUCT)
        elif ts.get("shields_up") is None:
            add("info", "shields-up", "could not read Tailscale prefs; check `tailscale debug prefs` yourself")
    if "receiver" in f:
        r = f["receiver"]
        if r.get("reachable") and r.get("ok"):
            add("ok", "receiver", "serving tailmsg v%s as %s at %s" % (r.get("v"), r.get("node"), r["url"]))
        elif r.get("reachable"):
            add("warn", "receiver", "something answered %s with HTTP %s" % (r["url"], r.get("http_status")))
        else:
            add("warn", "receiver", "no receiver at %s (%s)" % (r.get("url", "-"), r.get("error")),
                "to receive messages run the receiver as a service: `%s install-runtime`, then install "
                "the definition printed by `%s service-template ...`" % (PRODUCT, PRODUCT))
    rt = f["runtime"]
    if rt["present"] and rt.get("same_as_bundled") is False:
        add("info", "runtime", "%s differs from the bundled tailmsg.py" % rt["path"],
            "`%s install-runtime --upgrade` (backs up the old one; restart your service yourself)" % PRODUCT)
    elif rt["present"]:
        add("ok", "runtime", "%s matches the bundled tailmsg.py" % rt["path"])
    elif f["config_exists"]:
        add("info", "runtime", "no durable runtime at %s" % rt["path"],
            "`%s install-runtime` before installing a receiver service" % PRODUCT)
    for a in f.get("adapters", []):
        name = "wake" + ("" if a["agent"] is None else " @%s" % a["agent"])
        worst = "error" if any(s == "error" for s, _ in a["checks"]) else \
            "warn" if any(s == "warn" for s, _ in a["checks"]) else "ok"
        detail = a["adapter"] + ("" if not a["checks"] else "; " + "; ".join(m for _, m in a["checks"]))
        add(worst if a["adapter"] != "none" else "info", name, detail,
            None if a["adapter"] != "none" else "messages wait in `inbox --pending`; see `%s wake-config`" % PRODUCT)
    if f.get("inbox"):
        i = f["inbox"]
        add("info", "inbox", "%d messages, %d pending, %d failed wake-ups, %d for unknown agents"
            % (i["messages"], i["pending"], i["wake_failed"], i["unknown_agent"]))
    return checks


def cmd_doctor(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " doctor",
                                 description="read-only checks of Python, Tailscale, config, receiver and wake adapters")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-probe", action="store_true", help="skip Tailscale queries and the receiver health probe")
    a = ap.parse_args(argv)
    try:
        checks = doctor_checks(gather(home, home_source, probe=not a.no_probe))
    except Exception as e:  # a diagnostic must report, not crash
        checks = [{"status": "error", "check": "doctor", "detail": "unexpected %s: %s" % (type(e).__name__, e),
                   "fix": "please report this; nothing was changed"}]
    f = {"home": str(home)}
    errors = sum(c["status"] == "error" for c in checks)
    warns = sum(c["status"] == "warn" for c in checks)
    if a.json:
        print(json.dumps({"ok": errors == 0, "errors": errors, "warnings": warns, "home": f["home"],
                          "checks": checks, "changed": False}, indent=2, ensure_ascii=False))
    else:
        print("%s doctor (read-only, nothing is changed)" % DISPLAY)
        for c in checks:
            print("  %-6s %-14s %s" % (c["status"], c["check"], c["detail"]))
            if c["fix"]:
                print("  %-6s %-14s fix: %s" % ("", "", c["fix"]))
        print("result: %d error(s), %d warning(s)" % (errors, warns))
    return 1 if errors else 0


def cmd_status(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " status", description="read-only summary")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-probe", action="store_true", help="skip Tailscale queries and the receiver health probe")
    a = ap.parse_args(argv)
    f = gather(home, home_source, probe=not a.no_probe)
    out = {k: f.get(k) for k in ("plugin_version", "home", "home_source", "config_path", "config_exists",
                                 "config_error", "port", "allow", "self_agent", "default_agent", "wake_policy",
                                 "quiet_hours", "wake_rate", "receiver", "runtime", "inbox",
                                 "codex_bridge_installed") if k in f}
    ts = f["tailscale"]
    out["tailscale"] = {k: ts.get(k) for k in ("backend", "node", "ips", "shields_up", "error") if k in ts}
    out["agents"] = [{"agent": x["agent"], "default": x["default"], "wake_max": x["wake_max"],
                      "wake_adapter": x["adapter"]} for x in f.get("adapters", [])]
    if a.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    print("%s %s  home %s (%s)" % (DISPLAY, f["plugin_version"], f["home"], f["home_source"]))
    if not f["config_exists"]:
        print("  not set up yet (no config.json); run `%s doctor`" % PRODUCT)
    elif f["config_error"]:
        print("  config error: %s" % f["config_error"])
    else:
        print("  node      %s   port %s" % (ts.get("node") or "?", f["port"]))
        print("  allow     %s" % (", ".join(map(str, f["allow"])) or "(nobody)"))
        print("  identity  self_agent=%s default_agent=%s" % (f["self_agent"], f["default_agent"]))
        for x in out["agents"]:
            print("  agent     %s%s: wake %s%s" % ("-" if x["agent"] is None else "@" + x["agent"],
                                                   " (default)" if x["default"] else "", x["wake_adapter"],
                                                   " max %s" % x["wake_max"] if x["wake_max"] else ""))
        r = f.get("receiver") or {}
        print("  receiver  %s" % ("serving v%s" % r.get("v") if r.get("ok") else "not reachable"))
        i = f["inbox"]
        print("  inbox     %d messages, %d pending %s" % (i["messages"], i["pending"],
                                                         json.dumps(i["by_agent"], ensure_ascii=False)))
    return 0


# ---------------------------------------------------------------- config planning (setup / wake-config)
class Plan(object):
    """Collects intended config.json changes; writes only on --apply, with a backup of the old file."""

    def __init__(self, home, title):
        self.home, self.title = home, title
        self.path = home / "config.json"
        self.exists = self.path.is_file()
        self.raw_bytes = self.path.read_bytes() if self.exists else None
        self.before = None
        if self.exists:
            try:
                self.before = json.loads(self.raw_bytes.decode("utf-8"))
            except ValueError as e:
                die("%s is not valid JSON (%s); fix it by hand, %s will not overwrite it" % (self.path, e, PRODUCT))
            if not isinstance(self.before, dict):
                die("%s must contain a JSON object; %s will not overwrite it" % (self.path, PRODUCT))
        self.after = json.loads(json.dumps(self.before)) if self.before is not None else {}
        self.changes, self.errors, self.notes = [], [], []

    def change(self, text):
        self.changes.append(text)

    def error(self, text):
        self.errors.append(text)

    def note(self, text):
        self.notes.append(text)

    def finish(self, apply, as_json):
        if not self.errors and self.changes:
            try:
                cfg, agents = core().parse_config(self.after)
                for name in agent_names(cfg, agents):
                    core().Ctx.agent_adapter(_AdapterView(cfg, agents), name)
            except (RuntimeError, core().AdapterError) as e:
                self.error("resulting config would be invalid: %s" % e)
        action = "none" if not self.changes else ("update" if self.exists else "create")
        written, backup = False, None
        if apply and self.changes and not self.errors:
            backup = self._write()
            written = True
        result = {"command": self.title, "config": str(self.path), "action": action, "dry_run": not apply,
                  "written": written, "backup": backup, "changes": self.changes, "errors": self.errors,
                  "notes": self.notes}
        if as_json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            head = "applied" if written else "refused, nothing written" if self.errors else \
                "dry run, nothing written" if self.changes else "nothing to change"
            print("%s %s (%s)" % (PRODUCT, self.title, head))
            print("  config: %s (%s)" % (self.path, {"none": "unchanged", "update": "existing file",
                                                     "create": "new file"}[action]))
            for c in self.changes:
                print("  change: %s" % c)
            for e in self.errors:
                print("  error:  %s" % e)
            for n in self.notes:
                print("  note:   %s" % n)
            if backup:
                print("  backup: %s" % backup)
            if self.changes and not apply and not self.errors:
                print("  review the changes above, then re-run the same command with --apply")
        return 1 if self.errors else 0

    def _write(self):
        if self.exists and self.path.read_bytes() != self.raw_bytes:
            die("%s changed while planning; nothing written, re-run the command" % self.path)
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = (json.dumps(self.after, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        mode = stat.S_IMODE(self.path.stat().st_mode) if self.exists else 0o600
        backup = None
        if self.exists:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = self.path.with_name("config.json.bak-" + stamp)
            n = 1
            while backup.exists():
                backup = self.path.with_name("config.json.bak-%s-%d" % (stamp, n))
                n += 1
            fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(self.raw_bytes)
        tmp = self.path.with_name(".config.json.%s-%d.tmp" % (PRODUCT, os.getpid()))
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(str(tmp), mode)
        os.replace(str(tmp), str(self.path))
        return str(backup) if backup else None


def valid_agent(name, what="agent name"):
    err = core().agent_name_error(name)
    if err:
        die("bad %s %r: %s" % (what, name, err), 2)
    return unicodedata.normalize("NFC", name)


def valid_peer(rule, what="peer"):
    if not isinstance(rule, str) or len(rule) > 253 or not PEER_RULE_RE.match(rule):
        die("bad %s %r: use a Tailscale hostname or FQDN, tag:<name> or user:<login>" % (what, rule), 2)
    return rule


def set_scalar(plan, key, value, replace, label=None):
    label = label or key
    cur = plan.after.get(key)
    if cur == value or (isinstance(cur, str) and unicodedata.normalize("NFC", cur) == value):
        return
    if cur is not None and not replace:
        plan.error("%s is already %s; pass --replace %s to change it to %s"
                   % (label, json.dumps(cur, ensure_ascii=False), key, json.dumps(value, ensure_ascii=False)))
        return
    plan.after[key] = value
    plan.change("%s: %s -> %s" % (label, json.dumps(cur, ensure_ascii=False), json.dumps(value, ensure_ascii=False)))


def cmd_setup(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False,
        prog=PRODUCT + " setup",
        description="Create or extend config.json in the data home. Dry run unless --apply. Only adds what you "
                    "name: existing allow entries, agents, adapters and unknown keys are kept as they are.")
    ap.add_argument("--allow", action="append", default=[], metavar="PEER",
                    help="who may message this machine: hostname, FQDN, tag:<name> or user:<login> (repeatable)")
    ap.add_argument("--send-only", action="store_true",
                    help="create a config that accepts no incoming messages (empty allow list)")
    ap.add_argument("--agent", action="append", default=[], metavar="NAME",
                    help="a local agent that can be addressed as <this-machine>/NAME (repeatable)")
    ap.add_argument("--self-agent", metavar="NAME", help="this agent's name, sent as from_agent so replies come back")
    ap.add_argument("--default-agent", metavar="NAME", help="agent that receives messages that name no agent")
    ap.add_argument("--port", type=int, help="receiver port (default 7766)")
    ap.add_argument("--tailscale-cmd", metavar="CMD",
                    help='tailscale CLI path, or a JSON list such as ["sudo","-n","tailscale"]')
    ap.add_argument("--replace", action="append", default=[],
                    choices=("self_agent", "default_agent", "port", "tailscale_cmd"),
                    help="allow changing this already-set field (repeatable)")
    ap.add_argument("--apply", action="store_true", help="write the change (a backup of the old file is kept)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    plan = Plan(home, "setup")
    if not plan.exists:
        if not a.allow and not a.send_only:
            die("choose who may message this machine: --allow <peer> (repeatable), or --send-only", 2)
        plan.after = {"port": 7766, "allow": []}
        plan.change("create %s with port 7766" % plan.path)
    elif a.send_only:
        plan.note("--send-only only applies to a new config; the existing allow list is unchanged")
    allow = plan.after.get("allow")
    if allow is None:
        allow = []
    elif not isinstance(allow, list):
        die("config allow must be a list; fix it by hand", 2)
    grew = False
    for rule in a.allow:
        rule = valid_peer(rule)
        if rule.lower() in [str(x).lower().rstrip(".") for x in allow]:
            continue
        allow.append(rule)
        grew = True
        plan.change("allow += %s" % rule)
    if grew or "allow" in plan.after:
        plan.after["allow"] = allow
    agents = plan.after.get("agents") if plan.after.get("agents") is not None else {}
    if not isinstance(agents, dict):
        die("config agents must be an object; fix it by hand", 2)
    for name in a.agent:
        name = valid_agent(name)
        if name not in agents and name not in [unicodedata.normalize("NFC", k) for k in agents]:
            agents[name] = {}
            plan.change("agents.%s = {} (addressable, no wake adapter yet)" % name)
    if agents or "agents" in plan.after:
        plan.after["agents"] = agents
    if a.self_agent is not None:
        set_scalar(plan, "self_agent", valid_agent(a.self_agent, "self agent"), "self_agent" in a.replace)
    if a.default_agent is not None:
        set_scalar(plan, "default_agent", valid_agent(a.default_agent, "default agent"),
                   "default_agent" in a.replace)
    if a.port is not None:
        if not 1 <= a.port <= 65535:
            die("--port must be 1-65535", 2)
        if plan.exists:
            set_scalar(plan, "port", a.port, "port" in a.replace)
            if plan.before.get("port", 7766) != a.port:
                plan.note("peers address this machine by port; a new port needs `host:port` on their side")
        elif a.port != 7766:
            plan.after["port"] = a.port
            plan.changes[0] = "create %s with port %d" % (plan.path, a.port)
    if a.tailscale_cmd is not None:
        cmd = a.tailscale_cmd
        if cmd.startswith("["):
            try:
                cmd = json.loads(cmd)
            except ValueError:
                die("--tailscale-cmd JSON list is invalid", 2)
        else:
            cmd = [cmd]
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x and "\n" not in x for x in cmd):
            die("--tailscale-cmd must be a path or a JSON list of strings", 2)
        set_scalar(plan, "tailscale_cmd", cmd, "tailscale_cmd" in a.replace)
    if plan.changes:
        plan.note("a running receiver reads config.json only at start: restart it to apply changes (%s never restarts services)" % PRODUCT)
        plan.note("Tailscale ACLs and shields-up are not touched: the tailnet policy must let these peers reach "
                  "tcp:%s" % plan.after.get("port", 7766))
    return plan.finish(a.apply, a.json)


def check_secret_file(plan, key, value):
    p = Path(value).expanduser()
    if not p.is_absolute():
        plan.error("--%s-file must be an absolute path (got %s)" % (key, value))
        return None
    try:
        st = p.stat()
    except OSError:
        plan.error("%s file %s does not exist; create it first with `umask 077` (never paste the value into a "
                   "command line or chat)" % (key, p))
        return None
    if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        plan.error("%s file %s must be mode 0600: chmod 600 '%s'" % (key, p, p))
    return str(p)


def parse_level(value, what):
    if value not in LEVELS:
        die("%s must be one of now/batch/none" % what, 2)
    return value


def adapter_for_agent(plan, a, spec, where, name, before_eff, new_adapter):
    own = spec.get("wake_adapter")
    if a.remove_adapter:
        if own is None:
            plan.note("%s is not set; nothing to remove (@%s is woken by: %s)" % (where, name, describe(before_eff)))
            return
        del spec["wake_adapter"]
        after_eff = effective_adapter(plan.after, name)
        if after_eff is not None:
            plan.change("%s removed; @%s now falls back to the top-level adapter: %s"
                        % (where, name, describe(after_eff)))
            plan.note("to stop waking @%s entirely use --wake-max batch (its messages then wait in "
                      "`inbox --pending`)" % name)
        else:
            plan.change("%s removed; nobody is woken for @%s: its messages wait in `inbox --pending`"
                        % (where, name))
        return
    if same_adapter(own, new_adapter):
        return
    if before_eff is not None and not same_adapter(before_eff, new_adapter) and not a.replace:
        plan.error("@%s is already woken by %s adapter (%s); pass --replace to change it"
                   % (name, "its own" if own is not None else "the inherited top-level", describe(before_eff)))
        return
    spec["wake_adapter"] = new_adapter
    plan.change("%s = %s" % (where, describe(new_adapter)))


def adapter_top_level(plan, a, cfg, new_adapter):
    cur = cfg.get("wake_adapter")
    if cur is None and cfg.get("on_message"):
        cur = {"type": "exec", "argv": cfg["on_message"], "_alias": "on_message"}
    dflt = cfg.get("default_agent")
    if a.remove_adapter:
        if cur is None:
            plan.note("no top-level adapter is set; nothing to remove")
            return
        cfg.pop("wake_adapter", None)
        if cfg.get("on_message"):
            cfg.pop("on_message")
            plan.change("on_message removed (deprecated alias of the top-level exec adapter)")
        after_eff = effective_adapter(plan.after, unicodedata.normalize("NFC", dflt) if dflt else None)
        plan.change("wake_adapter removed; %s" % (
            "default agent @%s keeps its own adapter: %s" % (dflt, describe(after_eff)) if after_eff is not None
            else "messages for the default agent (or with no agent) now wait in `inbox --pending`"))
        return
    if same_adapter(cur, new_adapter):
        return
    if cur is not None and not a.replace:
        plan.error("the top-level adapter is already set (%s); pass --replace to change it" % describe(cur))
        return
    cfg["wake_adapter"] = new_adapter
    if cfg.get("on_message"):
        plan.note("deprecated on_message stays in the file but wake_adapter now takes precedence")
    plan.change("wake_adapter = %s" % describe(new_adapter))


def cmd_wake_config(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False,
        prog=PRODUCT + " wake-config",
        description="Receiver-side wake settings. The receiver always decides; a sender's --wake is only a request. "
                    "Dry run unless --apply. Only the agent or fields you name are touched; replacing an "
                    "existing value needs --replace. Webhook URLs and secrets are referenced by file or "
                    "environment variable name and are never read, printed or stored by this command.")
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--agent", metavar="NAME", help="configure this local agent's adapter / wake_max")
    target.add_argument("--top-level", action="store_true",
                        help="configure the top-level adapter (used by the default agent or single-agent setups)")
    kind = ap.add_mutually_exclusive_group()
    kind.add_argument("--webhook", action="store_true", help="wake by HTTP POST (for example a Grok Bot routine)")
    kind.add_argument("--exec", dest="exec_path", metavar="PATH", help="wake by running this local program")
    kind.add_argument("--remove-adapter", action="store_true",
                      help="remove this adapter; the output says what wakes the agent afterwards (the default agent "
                           "falls back to the top-level adapter if one is set; otherwise its messages stay pending). "
                           "To stop waking an agent entirely use --wake-max batch")
    ap.add_argument("--url-file", help="0600 file holding the webhook URL")
    ap.add_argument("--url-env", help="environment variable holding the webhook URL (in the serve process)")
    ap.add_argument("--secret-file", help="0600 file holding the webhook key")
    ap.add_argument("--secret-env", help="environment variable holding the webhook key")
    ap.add_argument("--header", help="auth header name (default Authorization)")
    ap.add_argument("--scheme", help='auth scheme (default Bearer; "" sends the raw key)')
    ap.add_argument("--exec-arg", action="append", default=[], metavar="ARG",
                    help="extra argument for the --exec program (repeatable)")
    ap.add_argument("--wake-max", metavar="LEVEL", help="cap for this agent: now, batch or none")
    ap.add_argument("--peer-max", action="append", default=[], metavar="PEER=LEVEL",
                    help="cap for a sender (wake_policy.peers), e.g. bot-box=now (repeatable)")
    ap.add_argument("--default-max", metavar="LEVEL", help="cap for every other sender (wake_policy.default_max)")
    ap.add_argument("--quiet-hours", metavar="HH:MM-HH:MM|off", help="downgrade now to batch in this window")
    ap.add_argument("--tz", metavar="ZONE", help="IANA time zone for --quiet-hours (default: local time)")
    ap.add_argument("--rate", type=int, metavar="N", help="max effective `now` per sender and agent per hour")
    ap.add_argument("--replace", action="store_true", help="allow replacing values this command touches")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    plan = Plan(home, "wake-config")
    if not plan.exists:
        die("no config at %s; run `%s setup` first" % (plan.path, PRODUCT), 2)
    cfg = plan.after
    wants_adapter = a.webhook or a.exec_path or a.remove_adapter
    if (wants_adapter or a.wake_max) and not (a.agent or a.top_level):
        die("name the target: --agent NAME (or --top-level for the default adapter)", 2)
    if a.wake_max and a.top_level:
        die("--wake-max is per agent: use --agent NAME", 2)
    if not any([wants_adapter, a.wake_max, a.peer_max, a.default_max, a.quiet_hours, a.rate is not None]):
        die("nothing to change; see `%s wake-config --help`" % PRODUCT, 2)
    if not a.webhook and any(x is not None for x in (a.url_file, a.url_env, a.secret_file, a.secret_env,
                                                     a.header, a.scheme)):
        die("--url-*/--secret-*/--header/--scheme belong to --webhook", 2)
    if a.exec_arg and not a.exec_path:
        die("--exec-arg belongs to --exec", 2)
    spec, key, name, before_eff = None, None, None, None
    if a.agent:
        name = valid_agent(a.agent)
        agents = cfg.get("agents") if cfg.get("agents") is not None else {}
        if not isinstance(agents, dict):
            die("config agents must be an object; fix it by hand", 2)
        same = [k for k in agents if unicodedata.normalize("NFC", k) == name]
        if len(same) > 1:
            die("config has %d spellings of agent %r (%s); merge them by hand first"
                % (len(same), name, ", ".join(json.dumps(k) for k in same)), 2)
        key = same[0] if same else name   # tailmsg compares names after NFC: reuse the existing spelling
        before_eff = effective_adapter(plan.before, name)
        if not same:
            agents[key] = {}
            plan.change("agents.%s = {} (new addressable agent)" % key)
        if agents[key] is None:
            agents[key] = {}
        cfg["agents"] = agents
        spec = agents[key]
        where = "agents.%s.wake_adapter" % key
    else:
        spec = cfg
        where = "wake_adapter"
    new_adapter = None
    if a.webhook:
        if bool(a.url_file) == bool(a.url_env):
            die("--webhook needs exactly one of --url-file / --url-env", 2)
        if a.secret_file and a.secret_env:
            die("use only one of --secret-file / --secret-env", 2)
        new_adapter = {"type": "webhook"}
        if a.url_file:
            new_adapter["url_file"] = check_secret_file(plan, "url", a.url_file)
        else:
            if not ENV_NAME_RE.match(a.url_env):
                die("bad --url-env name", 2)
            new_adapter["url_env"] = a.url_env
        if a.secret_file:
            new_adapter["secret_file"] = check_secret_file(plan, "secret", a.secret_file)
        elif a.secret_env:
            if not ENV_NAME_RE.match(a.secret_env):
                die("bad --secret-env name", 2)
            new_adapter["secret_env"] = a.secret_env
        else:
            plan.note("no secret configured: the webhook is called without an auth header")
        for env_key in ("url_env", "secret_env"):
            if new_adapter.get(env_key) and not os.environ.get(new_adapter[env_key]):
                plan.note("$%s must be set in the environment of the serve process" % new_adapter[env_key])
        if a.header is not None:
            new_adapter["header"] = a.header
        if a.scheme is not None:
            if len(a.scheme) > 32 or re.search(r"[\r\n\s]", a.scheme):
                die("bad --scheme", 2)
            new_adapter["scheme"] = a.scheme
    elif a.exec_path:
        path = os.path.expanduser(a.exec_path)
        if not os.path.isabs(path):
            die("--exec needs an absolute path (the serve process may have a different PATH and cwd)", 2)
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            plan.error("%s is not an executable file" % path)
        if any("\x00" in x or "\n" in x for x in a.exec_arg):
            die("bad --exec-arg", 2)
        new_adapter = {"type": "exec", "argv": [path] + list(a.exec_arg)}
    if new_adapter is not None or a.remove_adapter:
        if new_adapter is not None and not any(v is None for v in new_adapter.values()):
            try:
                core().adapter_cfg({"wake_adapter": new_adapter})
            except core().AdapterError as e:
                plan.error(str(e))
        if a.agent:
            adapter_for_agent(plan, a, spec, where, name, before_eff, new_adapter)
        else:
            adapter_top_level(plan, a, cfg, new_adapter)
    if a.wake_max:
        level = parse_level(a.wake_max, "--wake-max")
        cur = spec.get("wake_max")
        if cur != level:
            if cur is not None and not a.replace:
                plan.error("agents.%s.wake_max is already %s; pass --replace" % (key, cur))
            else:
                spec["wake_max"] = level
                plan.change("agents.%s.wake_max: %s -> %s" % (key, cur, level))
    if a.peer_max or a.default_max:
        pol = cfg.get("wake_policy") if cfg.get("wake_policy") is not None else {}
        if not isinstance(pol, dict):
            die("config wake_policy must be an object; fix it by hand", 2)
        peers = pol.get("peers") if pol.get("peers") is not None else {}
        if not isinstance(peers, dict):
            die("config wake_policy.peers must be an object; fix it by hand", 2)
        for item in a.peer_max:
            if "=" not in item:
                die("--peer-max wants PEER=LEVEL", 2)
            peer, level = item.rsplit("=", 1)
            peer, level = valid_peer(peer), parse_level(level, "--peer-max level")
            existing = [k for k in peers if str(k).lower().rstrip(".") == peer.lower()]
            k = existing[0] if existing else peer
            if peers.get(k) == level:
                continue
            if existing and not a.replace:
                plan.error("wake_policy.peers.%s is already %s; pass --replace" % (k, peers[k]))
                continue
            plan.change("wake_policy.peers.%s: %s -> %s" % (k, peers.get(k), level))
            peers[k] = level
        if a.default_max:
            level = parse_level(a.default_max, "--default-max")
            cur = pol.get("default_max")
            if cur != level:
                if cur is not None and not a.replace:
                    plan.error("wake_policy.default_max is already %s; pass --replace" % cur)
                else:
                    pol["default_max"] = level
                    plan.change("wake_policy.default_max: %s -> %s" % (cur, level))
                    if level == "now":
                        plan.note("default_max now lets every allowed sender wake this machine immediately")
        if peers or "peers" in pol:
            pol["peers"] = peers
        cfg["wake_policy"] = pol
    if a.quiet_hours:
        if a.quiet_hours == "off":
            q = None
        else:
            m = re.match(r"^(\d\d:\d\d)-(\d\d:\d\d)\Z", a.quiet_hours)
            if not m or not HHMM_RE.match(m.group(1)) or not HHMM_RE.match(m.group(2)):
                die("--quiet-hours wants HH:MM-HH:MM (24h) or off", 2)
            q = {"start": m.group(1), "end": m.group(2)}
            if a.tz:
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(a.tz)
                except ImportError:
                    plan.note("cannot validate --tz on this Python; tailmsg ignores an unknown zone")
                except Exception:
                    die("unknown time zone %r" % a.tz, 2)
                q["tz"] = a.tz
        if cfg.get("quiet_hours") != q:
            if cfg.get("quiet_hours") is not None and not a.replace:
                plan.error("quiet_hours is already %s; pass --replace" % json.dumps(cfg["quiet_hours"]))
            else:
                plan.change("quiet_hours: %s -> %s" % (json.dumps(cfg.get("quiet_hours")), json.dumps(q)))
                cfg["quiet_hours"] = q
    elif a.tz:
        die("--tz belongs to --quiet-hours", 2)
    if a.rate is not None:
        if not 0 <= a.rate <= 1000:
            die("--rate must be 0-1000", 2)
        if cfg.get("wake_rate") != a.rate:
            if cfg.get("wake_rate") is not None and not a.replace:
                plan.error("wake_rate is already %s; pass --replace" % cfg["wake_rate"])
            else:
                plan.change("wake_rate: %s -> %s" % (cfg.get("wake_rate"), a.rate))
                cfg["wake_rate"] = a.rate
    if plan.changes:
        plan.note("restart the running receiver to load the new settings (%s never restarts services)" % PRODUCT)
        plan.note("a wake adapter only notifies an agent; message text is never a command or an approval")
    return plan.finish(a.apply, a.json)


def effective_adapter(raw, agent):
    """The adapter tailmsg would call for `agent` (NFC name or None) under config object `raw`: the agent's own
    adapter, else (default agent / no agent only) the top-level wake_adapter or on_message. None = nobody is
    woken. A misconfigured adapter is returned as {"_invalid": reason} so it still counts as configured."""
    try:
        cfg, agents = core().parse_config(raw if raw is not None else {})
        return core().Ctx.agent_adapter(_AdapterView(cfg, agents), agent)
    except core().AdapterError as e:
        return {"_invalid": str(e)}
    except RuntimeError as e:
        return {"_invalid": "config: %s" % e}


def same_adapter(x, y):
    strip = lambda d: None if d is None else {k: v for k, v in d.items() if not str(k).startswith("_")}
    return strip(x) == strip(y)


def describe(adapter):
    if adapter is None:
        return "none"
    if adapter.get("_invalid"):
        return "misconfigured (%s)" % adapter["_invalid"]
    return core().describe_adapter(_described(adapter))


def _described(adapter):
    a = dict(adapter)
    if a.get("type") == "exec" and not a.get("argv"):
        a["argv"] = ["?"]
    return a


# ---------------------------------------------------------------- runtime + services
def cmd_install_runtime(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False,
        prog=PRODUCT + " install-runtime",
        description="Copy the bundled tailmsg.py to <home>/tailmsg.py so a receiver service runs from a stable "
                    "path that plugin updates and uninstalls do not move. Dry run unless --apply. Never replaces "
                    "a different existing copy without --upgrade, and never restarts anything.")
    ap.add_argument("--upgrade", action="store_true", help="replace a different existing copy (kept as a backup)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not CORE.is_file():
        die("bundled tailmsg.py is missing; reinstall the plugin", 2)
    target = home / "tailmsg.py"
    if not home.is_dir():
        die("no data home at %s; run `%s setup` first" % (home, PRODUCT), 2)
    new_hash = sha256_file(CORE)
    res = {"command": "install-runtime", "target": str(target), "bundled_sha256": new_hash, "dry_run": not a.apply,
           "written": False, "backup": None}
    if target.is_file():
        old_hash = sha256_file(target)
        res["existing_sha256"] = old_hash
        if old_hash == new_hash:
            res["action"] = "none"
        elif not a.upgrade:
            res["action"] = "refused"
            res["error"] = ("%s already exists and differs from the bundled copy; it may be a running receiver. "
                            "Re-run with --upgrade (and --apply) to replace it with a backup." % target)
        else:
            res["action"] = "upgrade"
    else:
        res["action"] = "install"
    if a.apply and res["action"] in ("install", "upgrade"):
        if res["action"] == "upgrade":
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = target.with_name("tailmsg.py.bak-" + stamp)
            shutil.copy2(str(target), str(backup))
            res["backup"] = str(backup)
        tmp = target.with_name(".tailmsg.py.%s-%d.tmp" % (PRODUCT, os.getpid()))
        shutil.copyfile(str(CORE), str(tmp))
        os.chmod(str(tmp), 0o644)
        os.replace(str(tmp), str(target))
        res["written"] = True
        res["next"] = "restart the receiver service if one is running (%s never restarts services)" % PRODUCT
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        verb = {"none": "already current", "refused": "refused", "install": "install", "upgrade": "upgrade"}
        print("%s install-runtime: %s %s%s" % (PRODUCT, verb[res["action"]], target,
                                               "" if res["written"] or res["action"] in ("none", "refused")
                                               else " (dry run; add --apply)"))
        for k in ("error", "backup", "next"):
            if res.get(k):
                print("  %s: %s" % (k, res[k]))
    return 1 if res["action"] == "refused" else 0


def systemd_quote(arg):
    return '"%s"' % arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")


def cmd_service_template(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " service-template",
                                 description="Print a receiver service definition to review and install yourself. "
                                             "Nothing is written or started.")
    ap.add_argument("kind", choices=("systemd", "systemd-user", "launchd", "windows"))
    ap.add_argument("--python", default=None, help="Python for the service (default: this one)")
    a = ap.parse_args(argv)
    python = a.python or os.path.realpath(sys.executable)
    runtime = home / "tailmsg.py"
    args = [python, str(runtime)]
    if home != Path(DEFAULT_HOME).expanduser().absolute():
        args += ["--home", str(home)]
    args.append("serve")
    warn = [] if runtime.is_file() else ["%s does not exist yet: run `%s install-runtime --apply` first"
                                         % (runtime, PRODUCT)]
    if a.kind in ("systemd", "systemd-user"):
        user = a.kind == "systemd-user"
        lines = ["# %s receiver (tailmsg protocol v1). Review, then install it yourself:" % DISPLAY]
        if user:
            lines += ["#   ~/.config/systemd/user/tailmsg.service, then: systemctl --user daemon-reload &&",
                      "#   systemctl --user enable --now tailmsg   (loginctl enable-linger keeps it running "
                      "after logout)"]
        else:
            lines += ["#   /etc/systemd/system/tailmsg.service, then: sudo systemctl daemon-reload &&",
                      "#   sudo systemctl enable --now tailmsg"]
        lines += ["# WARNING: %s" % w for w in warn]
        lines += ["[Unit]", "Description=tailmsg receiver (%s)" % DISPLAY,
                  "After=network-online.target" + ("" if user else " tailscaled.service")]
        if not user:
            lines.append("Wants=tailscaled.service")
        lines += ["", "[Service]"]
        if not user:
            lines.append("User=%s" % getpass.getuser())
        lines += ["ExecStart=%s" % " ".join(systemd_quote(x) for x in args), "Restart=always", "RestartSec=5",
                  "", "[Install]", "WantedBy=%s" % ("default.target" if user else "multi-user.target")]
        print("\n".join(lines))
    elif a.kind == "launchd":
        # stdout must stay a valid plist (the XML declaration has to come first): guidance goes to stderr
        for w in warn:
            print("WARNING: %s" % w, file=sys.stderr)
        print("Review, save stdout as ~/Library/LaunchAgents/%s.plist, then: launchctl load -w <that file>"
              % SERVICE_LABEL, file=sys.stderr)
        sys.stdout.write(plistlib.dumps({
            "Label": SERVICE_LABEL, "ProgramArguments": args, "RunAtLoad": True, "KeepAlive": True,
            "StandardErrorPath": str(home / "serve.log")}).decode("utf-8"))
    else:
        def ps(s):
            return "'" + s.replace("'", "''") + "'"
        inner = " ".join('"%s"' % x for x in args[1:])
        for w in warn:
            print("# WARNING: %s" % w)
        print("# Review, then run these PowerShell lines yourself (starts the receiver at logon):")
        print("$a = New-ScheduledTaskAction -Execute %s -Argument %s" % (ps(python), ps(inner)))
        print("$s = New-ScheduledTaskSettingsSet -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) "
              "-ExecutionTimeLimit 0")
        print("Register-ScheduledTask -TaskName tailmsg -Action $a -Trigger (New-ScheduledTaskTrigger -AtLogOn) "
              "-Settings $s")
    return 0


# ---------------------------------------------------------------- optional Codex desktop bridge
def cmd_codex_bridge(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False,
        prog=PRODUCT + " codex-bridge",
        description="Optional: queue authenticated messages into one fixed Codex desktop session (macOS). "
                    "Install and rollback run the bundled installer, which supports --dry-run and refuses to "
                    "replace an existing dispatcher session or a different codex wake adapter.")
    sub = ap.add_subparsers(dest="action")
    sub.required = True
    sub.add_parser("status", help="read-only queue summary")
    pi = sub.add_parser("install", help="install the bridge (macOS)")
    pi.add_argument("--thread-id", required=True, help="existing Codex desktop dispatcher session UUID")
    pi.add_argument("--codex-path", help="path to the Codex CLI that provides `codex queue`")
    pi.add_argument("--allow-replies", action="store_true", help="only with the user's explicit authorization")
    pi.add_argument("--no-start", action="store_true", help="do not reload services")
    pi.add_argument("--dry-run", action="store_true", help="run all checks and print the plan only")
    pr = sub.add_parser("rollback", help="restore an installation backup (macOS)")
    pr.add_argument("backup")
    pr.add_argument("--no-start", action="store_true")
    pr.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.action == "status":
        return bridge_status(home)
    if sys.platform != "darwin":
        die("host capability missing: the Codex desktop bridge manages a macOS LaunchAgent and the Codex desktop "
            "app; on %s use `%s wake-config --agent codex --exec ...` instead" % (sys.platform, PRODUCT), 3)
    if not (home / "tailmsg.py").is_file():
        die("the bridge expects a durable receiver at %s; run `%s install-runtime` first"
            % (home / "tailmsg.py", PRODUCT), 2)
    cmd = [sys.executable, str(BRIDGE / "install_codex_bridge.py"), "--tailmsg-home", str(home)]
    if a.action == "install":
        cmd += ["--thread-id", a.thread_id]
        if a.codex_path:
            cmd += ["--codex-path", a.codex_path]
        if a.allow_replies:
            cmd.append("--allow-replies")
    else:
        cmd += ["--rollback", a.backup]
    if a.no_start:
        cmd.append("--no-start")
    if a.dry_run:
        cmd.append("--dry-run")
    return subprocess.call(cmd)


def bridge_status(home):
    cfg_path = home / "codex-bridge.json"
    out = {"installed": cfg_path.is_file(), "config": str(cfg_path)}
    if out["installed"]:
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            out["dispatcher_thread_id"] = cfg.get("dispatcher_thread_id")
            out["allow_replies"] = bool(cfg.get("allow_replies"))
            db = Path(cfg.get("bridge_home") or str(home / "codex-bridge")) / "bridge.sqlite3"
        except (OSError, ValueError) as e:
            out["error"] = "cannot read bridge config: %s" % e
            db = None
        if db is not None and db.is_file():
            try:
                con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)
                try:
                    out["queue"] = dict(con.execute("SELECT state, COUNT(*) FROM messages GROUP BY state").fetchall())
                finally:
                    con.close()
            except sqlite3.Error as e:
                out["queue_error"] = str(e)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- dispatch
def run_core(home, cmd, rest):
    if not (home / "config.json").is_file():
        die("not set up: no %s (nothing was created). Run `%s doctor`, then `%s setup --allow <peer>` "
            "(dry run, then --apply)." % (home / "config.json", PRODUCT, PRODUCT), 2)
    if not CORE.is_file():
        die("bundled tailmsg.py is missing; reinstall the plugin", 2)
    python = os.environ.get(PYTHON_ENV) or sys.executable
    argv = [python, str(CORE), "--home", str(home), cmd] + list(rest)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "posix":
        try:
            os.execvp(python, argv)
        except OSError as e:
            die("cannot run %s: %s" % (python, e), 127)
    return subprocess.call(argv)


PLUGIN_COMMANDS = {"paths": cmd_paths, "doctor": cmd_doctor, "status": cmd_status, "setup": cmd_setup,
                   "wake-config": cmd_wake_config, "install-runtime": cmd_install_runtime,
                   "service-template": cmd_service_template, "codex-bridge": cmd_codex_bridge}


def main(argv=None):
    if sys.version_info[:2] < MIN_PYTHON:
        die("Python %d.%d+ is required (this is %s)" % (MIN_PYTHON + (sys.version.split()[0],)), 127)
    argv = list(sys.argv[1:] if argv is None else argv)
    home_arg, i = None, 0
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "--home":
            if i + 1 >= len(argv):
                die("--home needs a directory", 2)
            home_arg, i = argv[i + 1], i + 2
        elif argv[i].startswith("--home="):
            home_arg, i = argv[i][len("--home="):], i + 1
        elif argv[i] in ("-h", "--help"):
            print(USAGE.format(p=PRODUCT, d=DISPLAY, env=HOME_ENV, home=DEFAULT_HOME))
            return 0
        elif argv[i] == "--version":
            argv[i] = "version"
            break
        else:
            die("unknown option %s (see `%s --help`)" % (argv[i], PRODUCT), 2)
    if i >= len(argv):
        print(USAGE.format(p=PRODUCT, d=DISPLAY, env=HOME_ENV, home=DEFAULT_HOME), file=sys.stderr)
        return 2
    cmd, rest = argv[i], argv[i + 1:]
    home, source = resolve_home(home_arg)
    if cmd == "version":
        print("%s %s (tailmsg protocol v1; bundled tailmsg.py sha256 %s)"
              % (PRODUCT, plugin_version(), sha256_file(CORE)[:16] if CORE.is_file() else "missing"))
        return 0
    if cmd in CORE_COMMANDS:
        return run_core(home, cmd, rest)
    if cmd in PLUGIN_COMMANDS:
        return PLUGIN_COMMANDS[cmd](home, source, rest)
    die("unknown command %r (see `%s --help`)" % (cmd, PRODUCT), 2)


if __name__ == "__main__":
    sys.exit(main())
