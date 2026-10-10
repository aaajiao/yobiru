#!/usr/bin/env python3
"""tailmsg - minimal peer-to-peer messaging over Tailscale (protocol v1).

Every machine runs `tailmsg.py serve`. Messages are small JSON envelopes POSTed to
http://<peer>:7766/msg. Files are never inside a message: the sender stages them in a
read-only, expiring, unguessable URL on its own tailnet address; the receiver pulls
them itself and verifies sha256. Auth = Tailscale identity (`tailscale whois`) +
per-machine allowlist. Python 3.8+ stdlib only. See SPEC.md.
"""
import argparse, datetime as dt, hashlib, http.client, ipaddress, json, mimetypes, os
import re, secrets, shutil, ssl, stat, subprocess, sys, threading, time, unicodedata, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = 1
DEFAULTS = {
    "port": 7766,
    "allow": [],                    # e.g. ["bot-box", "tag:agent", "user:me@example.com"]
    "tailscale_cmd": ["tailscale"], # e.g. ["sudo", "-n", "tailscale"] if the socket needs root
    "bind": None,                   # default: own Tailscale IPv4
    "wake_adapter": None,           # how to wake the local agent, see SPEC §5.8, e.g.
                                    #   {"type": "exec", "argv": ["/usr/local/bin/wake-codex"]}
                                    #   {"type": "webhook", "url_env": "TAILMSG_WAKE_URL", "secret_env": "TAILMSG_WAKE_SECRET"}
    "on_message": None,             # DEPRECATED alias of {"type":"exec","argv": on_message}
    "hook_timeout_s": 120,          # per-attempt timeout of the wake adapter
    "wake_attempts": 3,             # adapter attempts per message (1 + 2 retries), then give up -> stays pending
    "wake_backoff_s": 2,            # retry delay: backoff * 2**(n-1) seconds
    "outbox_ttl_hours": 24,
    "max_msg_bytes": 256 * 1024,
    "max_fetch_bytes": 1024 ** 3,
    # wake control: the receiver decides the effective level (see SPEC §5.6)
    "wake_policy": {"default_max": "batch", "peers": {}},  # e.g. {"peers": {"bot-box": "now"}}
    "quiet_hours": None,            # e.g. {"start": "23:00", "end": "08:00", "tz": "Asia/Tokyo"}
    "wake_rate": 6,                 # max effective `now` per (sender, agent) per hour; excess -> batch
    # @agent addressing (SPEC §5.9): several agents can share one machine / one tailmsg
    "self_agent": None,             # my own agent name, sent as `from_agent` so replies come back to me
    "default_agent": None,          # agent that gets messages without `agent` (uses top-level wake_adapter)
    "agents": {},                   # name -> {"wake_adapter": {...}, "wake_max": "now|batch|none"}
}
WAKE_LEVELS = ("none", "batch", "now")  # ascending
WAKE_DEFAULT = "batch"
TAILNET_NETS = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")      # \Z, not $: "$" would also accept a trailing newline
MSG_REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")  # a message id or a unique prefix of one (CLI input)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}\Z")
ENV_KEYS = {"v", "id", "from", "to", "ts", "kind", "text", "reply_to", "fetch", "wake", "agent", "from_agent"}
AGENT_MAX = 64
FETCH_KEYS = {"name", "url", "sha256", "size", "mime", "expires"}


# ---------------------------------------------------------------- helpers
def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)

def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)

def die(msg, code=1):
    print("error: " + msg, file=sys.stderr)
    sys.exit(code)

def atomic_write(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    tmp.write_bytes(data)
    os.replace(tmp, path)

def is_tailnet_ip(ip):
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a in n for n in TAILNET_NETS)

def safe_name(name):
    name = Path(str(name).replace("\\", "/")).name
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).lstrip(".").strip()
    return name[:200] or "file"

def agent_name_error(name):
    """None if `name` is a valid agent name: 1-64 chars (Unicode ok), no '/', no control chars,
    no leading/trailing whitespace. Compared after NFC normalization."""
    if not isinstance(name, str) or not name:
        return "agent name must be a non-empty string"
    if len(name) > AGENT_MAX:
        return "agent name longer than %d chars" % AGENT_MAX
    if "/" in name:
        return "agent name must not contain '/'"
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in name):
        return "agent name must not contain control characters"
    if name != name.strip():
        return "agent name must not start or end with whitespace"
    return None

def norm_agent(name):
    return unicodedata.normalize("NFC", name) if isinstance(name, str) else name

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_config(raw):
    """config.json object -> (cfg merged over DEFAULTS, {agent name: spec}). Pure: no files are read or
    created, so tools can validate a config without side effects. Raises RuntimeError if invalid."""
    if not isinstance(raw, dict):
        raise RuntimeError("config must be a JSON object")
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    for k in ("self_agent", "default_agent"):
        if cfg.get(k) is not None:
            err = agent_name_error(cfg[k])
            if err:
                raise RuntimeError("config %s: %s" % (k, err))
            cfg[k] = norm_agent(cfg[k])
    agents = cfg.get("agents") or {}
    if not isinstance(agents, dict):
        raise RuntimeError("config agents must be an object {name: {...}}")
    out = {}
    for name, spec in agents.items():
        err = agent_name_error(name)
        if err:
            raise RuntimeError("config agents[%r]: %s" % (name, err))
        if spec is None:
            spec = {}
        if not isinstance(spec, dict) or set(spec) - {"wake_adapter", "wake_max"}:
            raise RuntimeError("config agents[%r] must be {\"wake_adapter\": ..., \"wake_max\": ...}" % name)
        if spec.get("wake_max") is not None and spec["wake_max"] not in WAKE_LEVELS:
            raise RuntimeError("config agents[%r].wake_max must be one of now/batch/none" % name)
        out[norm_agent(name)] = spec
    return cfg, out


class Ctx:
    def __init__(self, home):
        self.home = Path(home).expanduser()
        self.inbox = self.home / "inbox"
        self.outbox = self.home / "outbox"
        for d in (self.home, self.inbox, self.outbox):
            d.mkdir(parents=True, exist_ok=True)
        cfg_path = self.home / "config.json"
        if not cfg_path.exists():
            atomic_write(cfg_path, json.dumps(DEFAULTS, indent=2).encode())
            log("created default config", cfg_path, "(allow list is empty: nobody can message you yet)")
        self.cfg, self.agents = parse_config(json.loads(cfg_path.read_text(encoding="utf-8")))
        self._whois_cache = {}
        self._now_hits = {}          # (sender node, agent) -> [epoch seconds of effective `now`]
        self._wake_lock = threading.Lock()

    # -------- tailscale CLI (LocalAPI) --------
    def ts(self, *args):
        cmd = list(self.cfg["tailscale_cmd"]) + list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except FileNotFoundError:
            raise RuntimeError("tailscale CLI not found: %r (set tailscale_cmd in config.json)" % cmd[0])
        except subprocess.TimeoutExpired:
            raise RuntimeError("tailscale CLI timed out: %r" % cmd)
        if p.returncode != 0:
            err = (p.stderr or p.stdout).strip()
            if re.search(r"permission denied|access denied|operation not permitted", err, re.I):
                err += ' (hint: set "tailscale_cmd": ["sudo","-n","tailscale"] in config.json)'
            raise RuntimeError("%s: %s" % (" ".join(cmd), err))
        return p.stdout

    def status(self):
        return json.loads(self.ts("status", "--json"))

    def whois(self, ip):
        hit = self._whois_cache.get(ip)
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            d = json.loads(self.ts("whois", "--json", ip))
        except Exception as e:
            log("whois failed for", ip, e)
            return None
        node, up = d.get("Node") or {}, d.get("UserProfile") or {}
        ident = {
            "ip": ip,
            "node": (node.get("Name") or "").rstrip(".").lower(),
            "host": ((node.get("Hostinfo") or {}).get("Hostname") or "").lower(),
            "tags": node.get("Tags") or [],
            "user": up.get("LoginName") or "",
        }
        self._whois_cache[ip] = (time.time() + 60, ident)
        return ident

    def self_node(self):
        s = self.status()["Self"]
        return {"node": s["DNSName"].rstrip(".").lower(), "ips": s.get("TailscaleIPs") or []}

    def resolve(self, peer):
        """'ubuntu' | 'ubuntu.tailXXXX.ts.net' | '100.x.y.z' -> (fqdn, ipv4)"""
        p = peer.lower().rstrip(".")
        st = self.status()
        nodes = [st["Self"]] + list((st.get("Peer") or {}).values())
        for n in nodes:
            fq = (n.get("DNSName") or "").rstrip(".").lower()
            ips = n.get("TailscaleIPs") or []
            if p in (fq, fq.split(".")[0], (n.get("HostName") or "").lower()) or p in ips:
                v4 = [i for i in ips if ":" not in i]
                return fq, (v4 or ips)[0]
        raise RuntimeError("peer %r not found in `tailscale status`" % peer)

    @staticmethod
    def matches(rule, ident):
        """allow-list / wake_policy rule: hostname | fqdn | tag:x | user:login"""
        r = str(rule).lower().rstrip(".")
        if r.startswith("tag:"):
            return r in [t.lower() for t in ident["tags"]]
        if r.startswith("user:"):
            return bool(ident["user"]) and r[5:] == ident["user"].lower()
        return r in {ident["node"], ident["node"].split(".")[0], ident["host"]}

    def allowed(self, ident):
        return any(self.matches(rule, ident) for rule in self.cfg["allow"])

    def resolve_agent(self, requested):
        """-> (agent name or None, status). status: "default" (no `agent` in message), "known", "unknown"."""
        dflt = self.cfg.get("default_agent")
        if requested is None:
            return dflt, "default"
        a = norm_agent(requested)
        if a in self.agents or (dflt is not None and a == dflt):
            return a, "known"
        return a, "unknown"

    def agent_adapter(self, agent):
        """adapter for `agent`: its own agents[].wake_adapter; the default agent (or no agent at all)
        falls back to the top-level wake_adapter / on_message. Raises AdapterError if misconfigured."""
        spec = self.agents.get(agent) if agent is not None else None
        if spec and spec.get("wake_adapter"):
            return adapter_cfg({"wake_adapter": spec["wake_adapter"]})
        if agent is None or agent == self.cfg.get("default_agent"):
            return adapter_cfg(self.cfg)
        return None

    def wake_decision(self, requested, ident, now=None, agent=None, agent_status="default"):
        """-> (effective, reason). Receiver-side: min(requested, sender max, agent wake_max), unknown agent
        -> at most batch, then quiet hours, then rate limit per (sender, agent)."""
        pol = self.cfg.get("wake_policy") or {}
        cap = pol.get("default_max", WAKE_DEFAULT)
        for rule, lvl in (pol.get("peers") or {}).items():   # first matching rule wins
            if self.matches(rule, ident):
                cap = lvl
                break
        if cap not in WAKE_LEVELS:
            cap = WAKE_DEFAULT
        eff, reasons = requested, []
        if WAKE_LEVELS.index(eff) > WAKE_LEVELS.index(cap):
            eff = cap
            reasons.append("capped by wake_policy (max %s)" % cap)
        amax = (self.agents.get(agent) or {}).get("wake_max") if agent is not None else None
        if amax in WAKE_LEVELS and WAKE_LEVELS.index(eff) > WAKE_LEVELS.index(amax):
            eff = amax
            reasons.append("capped by agents[%s].wake_max (%s)" % (agent, amax))
        if agent_status == "unknown" and eff == "now":
            eff = "batch"
            reasons.append("unknown agent %r: nobody woken" % agent)
        if eff == "now" and in_quiet_hours(self.cfg.get("quiet_hours"), now):
            eff = "batch"
            reasons.append("quiet hours")
        if eff == "now":
            t = time.time()
            limit = int(self.cfg.get("wake_rate") or 0)
            with self._wake_lock:
                key = (ident["node"], agent)
                hits = [x for x in self._now_hits.get(key, []) if x > t - 3600]
                if len(hits) >= limit:
                    eff = "batch"
                    reasons.append("rate limit (%d now/hour)" % limit)
                else:
                    hits.append(t)
                self._now_hits[key] = hits
        return eff, "; ".join(reasons) or "as requested"


def in_quiet_hours(q, now=None):
    if not q:
        return False
    try:
        tz = None
        if q.get("tz"):
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(q["tz"])
        t = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
        cur = t.hour * 60 + t.minute
        s, e = [int(x.split(":")[0]) * 60 + int(x.split(":")[1]) for x in (q["start"], q["end"])]
    except Exception as ex:
        log("bad quiet_hours config, ignored:", ex)
        return False
    if s == e:
        return False
    return s <= cur < e if s < e else (cur >= s or cur < e)


# ---------------------------------------------------------------- envelope
def validate_env(env):
    if not isinstance(env, dict):
        return "envelope must be an object"
    extra = set(env) - ENV_KEYS
    if extra:
        return "unknown fields: %s" % sorted(extra)
    for k in ("v", "id", "from", "to", "ts"):
        if k not in env:
            return "missing field: " + k
    if env["v"] != VERSION:
        return "unsupported version"
    if not isinstance(env["id"], str) or not ID_RE.match(env["id"]):
        return "bad id"
    try:
        parse_iso(env["ts"])
    except Exception:
        return "bad ts"
    if not isinstance(env.get("kind", "text"), str) or len(env.get("kind", "text")) > 32:
        return "bad kind"
    if not isinstance(env.get("text", ""), str):
        return "bad text"
    if "wake" in env and env["wake"] not in WAKE_LEVELS:
        return "bad wake (must be one of now/batch/none)"
    for k in ("agent", "from_agent"):
        if k in env:
            err = agent_name_error(env[k])
            if err:
                return "bad %s: %s" % (k, err)
    rt = env.get("reply_to")
    if rt is not None and (not isinstance(rt, str) or not ID_RE.match(rt)):
        return "bad reply_to"
    fl = env.get("fetch", [])
    if not isinstance(fl, list) or len(fl) > 100:
        return "bad fetch list"
    for f in fl:
        if not isinstance(f, dict) or set(f) - FETCH_KEYS or not {"url", "sha256", "size"} <= set(f):
            return "bad fetch item"
        if not re.match(r"^[0-9a-f]{64}$", str(f["sha256"])) or not isinstance(f["size"], int) or f["size"] < 0:
            return "bad fetch item hash/size"
        u = urllib.parse.urlparse(str(f["url"]))
        if u.scheme != "http" or (u.hostname or "").lower() != str(env["from"]).lower():
            return "fetch url must be http:// on the sender's own tailnet name"
    return None


# ---------------------------------------------------------------- server
def make_handler(ctx, me):
    cfg = ctx.cfg

    class H(BaseHTTPRequestHandler):
        server_version = "tailmsg/%d" % VERSION
        sys_version = ""

        def log_message(self, fmt, *a):
            log(self.client_address[0], fmt % a)

        def reply(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def identify(self):
            ip = self.client_address[0]
            if not is_tailnet_ip(ip):
                self.reply(403, {"ok": False, "error": "not a tailnet address"})
                return None
            ident = ctx.whois(ip)
            if not ident or not ident["node"]:
                self.reply(403, {"ok": False, "error": "unknown tailnet peer"})
                return None
            return ident

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/health":
                if not is_tailnet_ip(self.client_address[0]):
                    return self.reply(403, {"ok": False, "error": "not a tailnet address"})
                return self.reply(200, {"ok": True, "v": VERSION, "node": me})
            m = re.match(r"^/f/([^/]+)/([^/]+)$", path)
            if not m:
                return self.reply(404, {"ok": False, "error": "not found"})
            ident = self.identify()
            if not ident:
                return
            token, name = m.group(1), urllib.parse.unquote(m.group(2))
            d = ctx.outbox / token
            if not TOKEN_RE.match(token) or not (d / "meta.json").is_file():
                return self.reply(404, {"ok": False, "error": "not found"})
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            if parse_iso(meta["expires"]) <= now_utc():
                return self.reply(404, {"ok": False, "error": "expired"})
            if ident["node"] != meta["to"]:
                return self.reply(404, {"ok": False, "error": "not found"})
            if name not in meta["files"]:
                return self.reply(404, {"ok": False, "error": "not found"})
            fp = d / "files" / name
            size = fp.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", meta["files"][name].get("mime") or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(fp, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/msg":
                return self.reply(404, {"ok": False, "error": "not found"})
            ident = self.identify()
            if not ident:
                return
            if not ctx.allowed(ident):
                log("rejected (not in allow list):", ident["node"], ident["tags"], ident["user"])
                return self.reply(403, {"ok": False, "error": "peer not in allow list"})
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.reply(415, {"ok": False, "error": "need application/json"})
            try:
                n = int(self.headers.get("Content-Length") or "-1")
            except ValueError:
                n = -1
            if n < 0:
                return self.reply(411, {"ok": False, "error": "need Content-Length"})
            if n > cfg["max_msg_bytes"]:
                self.close_connection = True
                return self.reply(413, {"ok": False, "error": "message too large (max %d bytes)" % cfg["max_msg_bytes"]})
            raw = self.rfile.read(n)
            try:
                env = json.loads(raw.decode("utf-8"))
            except Exception:
                return self.reply(400, {"ok": False, "error": "invalid json"})
            err = validate_env(env)
            if err:
                return self.reply(400, {"ok": False, "error": err})
            if env["from"].lower().rstrip(".") != ident["node"]:
                return self.reply(403, {"ok": False, "error": "'from' does not match tailscale identity %s" % ident["node"]})
            if env["to"].lower().rstrip(".") not in (me, me.split(".")[0]):
                return self.reply(400, {"ok": False, "error": "misdirected: I am %s" % me})
            path = ctx.inbox / (env["id"] + ".json")
            if path.exists():
                return self.reply(200, {"ok": True, "id": env["id"], "duplicate": True})
            requested = env.get("wake", WAKE_DEFAULT)
            agent, astatus = ctx.resolve_agent(env.get("agent"))
            eff, why = ctx.wake_decision(requested, ident, agent=agent, agent_status=astatus)
            rec = {"envelope": env, "received": {"at": iso(now_utc()), "peer": ident,
                   "agent": agent, "agent_status": astatus,
                   "wake": {"requested": requested, "effective": eff, "reason": why}}}
            atomic_write(path, json.dumps(rec, ensure_ascii=False, indent=2).encode("utf-8"))
            log("message", env["id"], "from", ident["node"] + ("/" + env["from_agent"] if env.get("from_agent") else ""),
                "to @%s (%s)" % (agent, astatus), "files=%d" % len(env.get("fetch", [])),
                "wake=%s->%s (%s)" % (requested, eff, why))
            if astatus == "unknown":
                log("WARNING: message", env["id"], "addressed to unknown agent %r: stored, nobody woken" % agent)
            if eff == "now":
                run_hook(ctx, path)
            return self.reply(202, {"ok": True, "id": env["id"]})

    return H


# ---------------------------------------------------------------- wake adapters (SPEC §5.8)
HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
PREVIEW_CHARS = 200
_record_lock = threading.Lock()


class AdapterError(Exception):
    pass


def adapter_cfg(cfg):
    """-> normalized adapter dict or None. `on_message` is a deprecated alias for type exec."""
    a = cfg.get("wake_adapter")
    if a:
        if not isinstance(a, dict) or a.get("type") not in ("exec", "webhook"):
            raise AdapterError("wake_adapter.type must be 'exec' or 'webhook'")
        if a["type"] == "exec" and not (isinstance(a.get("argv"), list) and a["argv"]):
            raise AdapterError("wake_adapter exec needs a non-empty argv list")
        if a["type"] == "webhook":
            if bool(a.get("url_env")) == bool(a.get("url_file")):
                raise AdapterError("wake_adapter webhook needs exactly one of url_env / url_file")
            if a.get("secret_env") and a.get("secret_file"):
                raise AdapterError("wake_adapter webhook: use only one of secret_env / secret_file")
            if not HEADER_RE.match(str(a.get("header", "Authorization"))):
                raise AdapterError("wake_adapter webhook: bad header name")
        return a
    if cfg.get("on_message"):
        return {"type": "exec", "argv": list(cfg["on_message"]), "_alias": "on_message"}
    return None


def describe_adapter(a):
    """log-safe description: names of env vars / files only, never their values."""
    if not a:
        return "none"
    if a["type"] == "exec":
        return "exec %s%s" % (a["argv"][0], " (via deprecated on_message)" if a.get("_alias") else "")
    src = lambda k: ("env $%s" % a[k + "_env"]) if a.get(k + "_env") else ("file %s" % a[k + "_file"]) if a.get(k + "_file") else "none"
    return "webhook url=%s secret=%s header=%s scheme=%r" % (src("url"), src("secret"), a.get("header", "Authorization"),
                                                              a.get("scheme", "Bearer"))


def read_secret_value(a, key):
    """value of <key>_env or <key>_file; files must not be group/world accessible. Never logged."""
    if a.get(key + "_env"):
        v = os.environ.get(a[key + "_env"])
        if not v:
            raise AdapterError("env var $%s is not set in the serve process" % a[key + "_env"])
        return v.strip()
    if a.get(key + "_file"):
        p = Path(a[key + "_file"]).expanduser()
        try:
            st = p.stat()
        except OSError:
            raise AdapterError("%s_file %s not readable" % (key, p))
        if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise AdapterError("%s_file %s must be mode 0600 (chmod 600)" % (key, p))
        v = p.read_text(encoding="utf-8").strip()
        if not v:
            raise AdapterError("%s_file %s is empty" % (key, p))
        return v
    return None


def rec_agent(rec, default=None):
    """agent a stored record is for (records from before @agent support: envelope agent or default)."""
    rv = rec.get("received") or {}
    if "agent" in rv:
        return rv["agent"]
    a = (rec.get("envelope") or {}).get("agent")
    return norm_agent(a) if a is not None else default


def wake_payload(rec):
    e = rec["envelope"]
    d = {"v": 1, "event": "tailmsg.wake", "id": e["id"], "from": rec["received"]["peer"]["node"],
         "kind": e.get("kind", "text"), "text_preview": (e.get("text") or "")[:PREVIEW_CHARS],
         "received_at": rec["received"]["at"]}
    agent = rec_agent(rec)
    if agent is not None:      # only when addressed: keeps the payload identical for single-agent setups
        d["agent"] = agent
    if e.get("from_agent"):
        d["from_agent"] = e["from_agent"]
    return d


def call_webhook(a, rec, timeout):
    url = read_secret_value(a, "url")
    secret = read_secret_value(a, "secret")
    redact = lambda s: s.replace(url, "<url>").replace(secret, "<secret>") if secret else s.replace(url, "<url>")
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise AdapterError("webhook url must be http(s)://")
    headers = {"Content-Type": "application/json", "User-Agent": "tailmsg/%d" % VERSION}
    if secret:
        if re.search(r"[\r\n]", secret):
            raise AdapterError("secret contains a newline")
        scheme = a.get("scheme", "Bearer")
        headers[a.get("header", "Authorization")] = (scheme + " " + secret) if scheme else secret
    body = json.dumps(wake_payload(rec), ensure_ascii=False).encode("utf-8")
    try:
        if u.scheme == "https":
            c = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ssl.create_default_context())
        else:
            c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        c.request("POST", (u.path or "/") + ("?" + u.query if u.query else ""), body=body, headers=headers)
        r = c.getresponse()
        r.read(4096)
        c.close()
    except Exception as e:  # never let the url/secret reach the log via the exception text
        raise AdapterError(redact("%s: %s" % (type(e).__name__, e)))
    if not 200 <= r.status < 300:  # redirects are not followed (would forward the secret)
        raise AdapterError("HTTP %d" % r.status)
    return "HTTP %d" % r.status


def call_exec(a, path, timeout, agent=None):
    env = dict(os.environ, TAILMSG_MSG_ID=Path(path).stem, TAILMSG_MSG_PATH=str(path))
    env.pop("TAILMSG_AGENT", None)
    if agent is not None:
        env["TAILMSG_AGENT"] = agent
    try:
        p = subprocess.run(list(a["argv"]) + [str(path)], capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise AdapterError("timeout after %ss" % timeout)
    except OSError as e:
        raise AdapterError("%s: %s" % (type(e).__name__, e))
    tail = (p.stderr or "").strip()[-300:]
    if p.returncode != 0:
        raise AdapterError("exit %d %s" % (p.returncode, tail))
    return "exit 0" + (" " + tail if tail else "")


def update_record(path, **fields):
    with _record_lock:
        r = json.loads(Path(path).read_text(encoding="utf-8"))
        r.update(fields)
        atomic_write(path, json.dumps(r, ensure_ascii=False, indent=2).encode("utf-8"))


def deliver_wake(ctx, path):
    """Run the adapter with retries. Returns True on success. Records the outcome (no secrets) in the
    inbox record as `wake_delivery`; on give-up the message simply stays in `inbox --pending`."""
    mid = Path(path).stem
    try:
        agent = rec_agent(json.loads(Path(path).read_text(encoding="utf-8")), ctx.cfg.get("default_agent"))
    except Exception:
        agent = ctx.cfg.get("default_agent")
    try:
        a = ctx.agent_adapter(agent)
    except AdapterError as e:
        log("wake adapter for @%s misconfigured:" % agent, e)
        return False
    if not a:
        log("wake", mid, "@%s has no wake adapter; message stays pending" % agent)
        return False
    attempts = max(1, int(ctx.cfg.get("wake_attempts") or 3))
    backoff = float(ctx.cfg.get("wake_backoff_s") if ctx.cfg.get("wake_backoff_s") is not None else 2)
    timeout = ctx.cfg.get("hook_timeout_s") or 120
    err, n = None, 0
    for n in range(1, attempts + 1):
        try:
            rec = json.loads(Path(path).read_text(encoding="utf-8"))
            res = call_webhook(a, rec, timeout) if a["type"] == "webhook" else call_exec(a, path, timeout, agent)
            log("wake", mid, "@%s" % agent, a["type"], "ok (attempt %d): %s" % (n, res))
            update_record(path, wake_delivery={"adapter": a["type"], "agent": agent, "ok": True, "attempts": n,
                                               "at": iso(now_utc())})
            return True
        except AdapterError as e:
            err = str(e)[-300:]
        except Exception as e:
            err = type(e).__name__
        log("wake", mid, "@%s" % agent, a["type"], "failed (attempt %d/%d): %s" % (n, attempts, err))
        if n < attempts:
            time.sleep(backoff * 2 ** (n - 1))
    log("wake", mid, "gave up after %d attempts; message stays pending in inbox" % n)
    try:
        update_record(path, wake_delivery={"adapter": a["type"], "agent": agent, "ok": False, "attempts": n,
                                           "at": iso(now_utc()), "error": err})
    except Exception as e:
        log("could not record wake_delivery:", e)
    return False


def run_hook(ctx, path):
    threading.Thread(target=deliver_wake, args=(ctx, path), daemon=True).start()


def sweep_outbox(ctx):
    now = now_utc()
    for d in ctx.outbox.iterdir():
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            if parse_iso(meta["expires"]) + dt.timedelta(hours=1) < now:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def cmd_serve(ctx, a):
    me = ctx.self_node()
    bind = ctx.cfg.get("bind") or ctx.ts("ip", "-4").split()[0]
    port = int(ctx.cfg["port"])
    if not ctx.cfg["allow"]:
        log("WARNING: allow list empty - every POST /msg will be rejected")
    names = [None] if not ctx.agents and ctx.cfg.get("default_agent") is None else \
        ([ctx.cfg["default_agent"]] if ctx.cfg.get("default_agent") not in (None, *ctx.agents) else []) + list(ctx.agents)
    for n in names:
        try:
            desc = describe_adapter(ctx.agent_adapter(n))
        except AdapterError as e:
            desc = "MISCONFIGURED, wakes will fail: %s" % e
        tag = "" if n is None else " @%s%s" % (n, " (default)" if n == ctx.cfg.get("default_agent") else "")
        log("wake adapter%s:" % tag, desc)
    if ctx.cfg.get("self_agent"):
        log("self_agent (sent as from_agent):", ctx.cfg["self_agent"])
    srv = ThreadingHTTPServer((bind, port), make_handler(ctx, me["node"]))
    srv.daemon_threads = True

    def sweeper():
        while True:
            sweep_outbox(ctx)
            time.sleep(300)
    threading.Thread(target=sweeper, daemon=True).start()
    log("tailmsg v%d serving %s on %s:%d  home=%s" % (VERSION, me["node"], bind, port, ctx.home))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- client
def http_json(ip, port, method, path, body=None, host=None, timeout=15):
    c = http.client.HTTPConnection(ip, port, timeout=timeout)
    hdrs = {"Host": "%s:%d" % (host or ip, port)}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=hdrs)
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, {"raw": raw[:200].decode("utf-8", "replace")}


def split_addr(addr):
    """'host[:port][/agent]' -> ('host[:port]', agent or None)"""
    if "/" in addr:
        host, agent = addr.split("/", 1)
        return host, (agent or None)
    return addr, None


def split_peer(peer, default_port):
    if peer.count(":") == 1:
        h, p = peer.split(":")
        return h, int(p)
    return peer, default_port


def cmd_send(ctx, a, reply_to=None, peer=None, agent=None):
    addr, addr_agent = split_addr(peer or a.peer)
    opt_agent = getattr(a, "agent", None)
    if addr_agent and opt_agent and norm_agent(addr_agent) != norm_agent(opt_agent):
        die("conflicting agent: %r in address vs --agent %r" % (addr_agent, opt_agent))
    agent = opt_agent or addr_agent or agent
    from_agent = getattr(a, "as_agent", None)
    implicit_from = from_agent is None
    if implicit_from:
        from_agent = ctx.cfg.get("self_agent")
    for k, v in (("agent", agent), ("from_agent", from_agent)):
        if v is not None and agent_name_error(v):
            die("bad %s %r: %s" % (k, v, agent_name_error(v)))
    me = ctx.self_node()["node"]
    host, port = split_peer(addr, int(ctx.cfg["port"]))
    to_fqdn, to_ip = ctx.resolve(host)
    msg_id = secrets.token_hex(8)  # hex: never starts with "-"
    env = {"v": VERSION, "id": msg_id, "from": me, "to": to_fqdn, "ts": iso(now_utc()),
           "kind": a.kind, "text": a.text}
    if reply_to or a.reply_to:
        env["reply_to"] = reply_to or a.reply_to
    if a.wake:  # omitted unless given, so receivers older than the wake field still accept it
        env["wake"] = a.wake
    if agent is not None:  # omitted unless addressed: older receivers reject unknown fields
        env["agent"] = norm_agent(agent)
    if from_agent is not None:
        env["from_agent"] = norm_agent(from_agent)
    if a.file:
        ttl = a.ttl_hours if a.ttl_hours is not None else ctx.cfg["outbox_ttl_hours"]
        expires = iso(now_utc() + dt.timedelta(seconds=int(ttl * 3600)))
        token = secrets.token_urlsafe(32)
        d = ctx.outbox / token
        (d / "files").mkdir(parents=True)
        meta = {"id": msg_id, "to": to_fqdn, "expires": expires, "files": {}}
        items = []
        for f in a.file:
            src = Path(f)
            if not src.is_file():
                die("not a file: %s" % f)
            name = safe_name(src.name)
            while name in meta["files"]:
                name = "_" + name
            dst = d / "files" / name
            shutil.copyfile(src, dst)  # snapshot: hash stays valid even if the original changes
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            item = {"name": name, "size": dst.stat().st_size, "sha256": sha256_file(dst), "mime": mime,
                    "url": "http://%s:%d/f/%s/%s" % (me, int(ctx.cfg["port"]), token, urllib.parse.quote(name)),
                    "expires": expires}
            meta["files"][name] = item
            items.append(item)
        atomic_write(d / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
        env["fetch"] = items
    status, resp = http_json(to_ip, port, "POST", "/msg", env, host=to_fqdn)
    if status == 400 and implicit_from and "from_agent" in env and "from_agent" in str(resp.get("error", "")):
        # receiver predates @agent: from_agent only came from config, so drop it and resend (same id;
        # the rejected attempt was not stored). An explicit --agent / --as is never dropped silently.
        print("note: %s does not understand from_agent (old tailmsg); resent without it" % to_fqdn, file=sys.stderr)
        del env["from_agent"]
        status, resp = http_json(to_ip, port, "POST", "/msg", env, host=to_fqdn)
    out = {"status": status, "response": resp, "id": msg_id, "to": to_fqdn, "files": len(env.get("fetch", []))}
    if "agent" in env:
        out["agent"] = env["agent"]
    print(json.dumps(out, ensure_ascii=False))
    if status not in (200, 202):
        sys.exit(2)


def load_msg(ctx, mid):
    # ids (or unique id prefixes) only: never a path or glob that could reach outside the inbox
    if not MSG_REF_RE.match(mid):
        die("bad message id %r (expected letters, digits, '_' or '-')" % mid)
    p = ctx.inbox / (mid + ".json")
    if not p.is_file():
        hits = [x for x in ctx.inbox.glob(mid + "*.json")]
        if len(hits) != 1:
            die("no unique message matching %r" % mid)
        p = hits[0]
    return p, json.loads(p.read_text(encoding="utf-8"))


def eff_wake(r):
    return ((r.get("received") or {}).get("wake") or {}).get("effective", WAKE_DEFAULT)


def cmd_inbox(ctx, a):
    rows = []
    want = norm_agent(a.agent) if getattr(a, "agent", None) else None
    dflt = ctx.cfg.get("default_agent")
    for p in ctx.inbox.glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        w, acked = eff_wake(r), bool(r.get("acked_at"))
        if a.pending and (w == "none" or acked):
            continue
        ag = rec_agent(r, dflt)
        if want is not None and ag != want:
            continue
        if (r.get("received") or {}).get("agent_status") == "unknown":
            ag = "%s?" % ag
        e = r["envelope"]
        nf = len(e.get("fetch", []))
        got = sum((ctx.inbox / e["id"] / "files" / safe_name(f.get("name") or "file")).is_file() for f in e.get("fetch", []))
        wd = r.get("wake_delivery") or {}
        mark = ("" if acked else "*") + ("!" if wd and not wd.get("ok") else "")
        frm = e["from"].split(".")[0] + ("/" + e["from_agent"] if e.get("from_agent") else "")
        rows.append((r["received"]["at"], e["id"], frm, "@" + ag if ag else "-", w + mark, nf, got,
                     e.get("reply_to") or "", (e.get("text") or "").replace("\n", " ")[:60]))
    for at, mid, frm, ag, w, nf, got, rt, txt in sorted(rows):
        files = "%d/%d files" % (got, nf) if nf else ""
        local = parse_iso(at).astimezone().strftime("%m-%d %H:%M %Z")
        print("%s  %-16s %-20s %-10s %-6s %-10s %s%s" % (local, mid, frm, ag, w, files,
                                                        ("re:" + rt + " ") if rt else "", txt))
    if not rows:
        print("(no pending messages)" if a.pending else "(inbox empty)")
    elif not a.pending:
        print("(* = not acked; ! = wake adapter failed; wake column is the effective level; @x? = unknown agent)")


def cmd_ack(ctx, a):
    for mid in a.id:
        p, r = load_msg(ctx, mid)
        if not r.get("acked_at"):
            r["acked_at"] = iso(now_utc())
            atomic_write(p, json.dumps(r, ensure_ascii=False, indent=2).encode("utf-8"))
        print("acked", r["envelope"]["id"])


def cmd_wake(ctx, a):
    """re-run the wake adapter for a stored message (manual retry / adapter test), in the foreground."""
    p, r = load_msg(ctx, a.id)
    agent = rec_agent(r, ctx.cfg.get("default_agent"))
    if not ctx.agent_adapter(agent):
        die("no wake adapter configured for @%s" % agent if agent is not None else "no wake_adapter configured")
    if not deliver_wake(ctx, p):
        sys.exit(2)
    print("woke", p.stem)


def cmd_show(ctx, a):
    _, r = load_msg(ctx, a.id)
    print(json.dumps(r, ensure_ascii=False, indent=2))


def cmd_fetch(ctx, a):
    _, r = load_msg(ctx, a.id)
    env, sender = r["envelope"], r["received"]["peer"]["node"]
    out = ctx.inbox / env["id"] / "files"
    out.mkdir(parents=True, exist_ok=True)
    ok = True
    for f in env.get("fetch", []):
        name = safe_name(f.get("name") or Path(urllib.parse.urlparse(f["url"]).path).name)
        dst = out / name
        u = urllib.parse.urlparse(f["url"])
        try:
            if (u.hostname or "").lower() != sender or u.scheme != "http":
                raise RuntimeError("url host is not the verified sender %s" % sender)
            if f["size"] > ctx.cfg["max_fetch_bytes"]:
                raise RuntimeError("declared size exceeds max_fetch_bytes")
            if dst.is_file() and sha256_file(dst) == f["sha256"]:
                print("OK    %s (already fetched)" % name)
                continue
            _, ip = ctx.resolve(u.hostname)
            if not is_tailnet_ip(ip):
                raise RuntimeError("resolved to non-tailnet ip %s" % ip)
            c = http.client.HTTPConnection(ip, u.port or 80, timeout=30)
            c.request("GET", u.path, headers={"Host": u.netloc})
            resp = c.getresponse()
            if resp.status != 200:
                raise RuntimeError("HTTP %d %s" % (resp.status, resp.read(200).decode("utf-8", "replace")))
            h, n = hashlib.sha256(), 0
            part = dst.with_name(dst.name + ".part")
            with open(part, "wb") as w:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > f["size"]:
                        break
                    h.update(chunk)
                    w.write(chunk)
            if n != f["size"] or h.hexdigest() != f["sha256"]:
                part.unlink()
                raise RuntimeError("sha256/size mismatch - discarded")
            os.replace(part, dst)
            print("OK    %s  %d bytes  sha256 verified -> %s" % (name, n, dst))
        except Exception as e:
            ok = False
            print("FAIL  %s  %s" % (name, e))
    if not ok:
        sys.exit(2)


def cmd_reply(ctx, a):
    _, r = load_msg(ctx, a.id)
    # route back to the sender's agent (from_agent) unless --agent overrides
    cmd_send(ctx, a, reply_to=r["envelope"]["id"], peer=r["received"]["peer"]["node"] +
             (":%d" % a.port if a.port else ""), agent=r["envelope"].get("from_agent"))


def cmd_peers(ctx, a):
    st = ctx.status()
    port = int(ctx.cfg["port"])
    peers = list((st.get("Peer") or {}).values())
    res = {}

    def probe(n):
        ip = (n.get("TailscaleIPs") or ["?"])[0]
        try:
            s, j = http_json(ip, port, "GET", "/health", timeout=3)
            res[ip] = "yes (v%s)" % j.get("v") if s == 200 else "HTTP %d" % s
        except Exception:
            res[ip] = "no"
    ths = [threading.Thread(target=probe, args=(n,)) for n in peers if n.get("Online")]
    [t.start() for t in ths]
    [t.join() for t in ths]
    print("%-16s %-28s %-8s %-8s %s" % ("IP", "NAME", "OS", "ONLINE", "TAILMSG:%d" % port))
    for n in sorted(peers, key=lambda n: (not n.get("Online"), n.get("DNSName", ""))):
        ip = (n.get("TailscaleIPs") or ["?"])[0]
        print("%-16s %-28s %-8s %-8s %s" % (ip, (n.get("DNSName") or "").split(".")[0], n.get("OS", ""),
                                            "online" if n.get("Online") else "offline",
                                            res.get(ip, "-") + ("  " + ",".join(n.get("Tags") or []))))


def main():
    ap = argparse.ArgumentParser(prog="tailmsg", description="minimal P2P messages over Tailscale")
    ap.add_argument("--home", default=os.environ.get("TAILMSG_HOME", "~/.tailmsg"))
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("serve", help="run receiver + file server on the Tailscale IP")
    for name in ("send", "reply"):
        p = sp.add_parser(name, help="send a message" if name == "send" else "reply to a received message")
        if name == "send":
            p.add_argument("peer", help="hostname | fqdn | tailscale ip, optional :port, optional /agent "
                                        "(e.g. dev-server/codex)")
        else:
            p.add_argument("id")
            p.add_argument("--port", type=int)
        p.add_argument("text")
        p.add_argument("--file", action="append", default=[])
        p.add_argument("--kind", default="text")
        p.add_argument("--ttl-hours", type=float)
        p.add_argument("--wake", choices=WAKE_LEVELS,
                       help="requested wake level (default: omitted = batch); receiver may downgrade")
        p.add_argument("--agent", help="address this agent on the receiver (reply: default = original from_agent)")
        p.add_argument("--as", dest="as_agent", metavar="NAME",
                       help="my agent name, sent as from_agent (default: config self_agent; omitted if unset)")
        if name == "send":
            p.add_argument("--reply-to")
    pi = sp.add_parser("inbox", help="list received messages")
    pi.add_argument("--pending", action="store_true", help="only un-acked messages with effective wake now/batch")
    pi.add_argument("--agent", help="only messages for this agent (no `agent` in message = default_agent)")
    sp.add_parser("ack", help="mark message(s) as processed").add_argument("id", nargs="+")
    for name in ("show", "fetch"):
        sp.add_parser(name).add_argument("id")
    sp.add_parser("wake", help="re-run the wake adapter for a message (manual retry)").add_argument("id")
    sp.add_parser("peers", help="online peers and whether they run tailmsg")
    a = ap.parse_args()
    if a.cmd == "reply":
        a.reply_to = None
    try:
        ctx = Ctx(a.home)
        {"serve": cmd_serve, "send": cmd_send, "reply": cmd_reply, "inbox": cmd_inbox, "show": cmd_show,
         "fetch": cmd_fetch, "peers": cmd_peers, "ack": cmd_ack, "wake": cmd_wake}[a.cmd](ctx, a)
    except (RuntimeError, OSError, AdapterError) as e:
        die(str(e))


if __name__ == "__main__":
    main()
