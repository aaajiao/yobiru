#!/bin/sh
# Loopback self-test on one machine: 15 instances on the own Tailscale IP, ports 7866-7877 and 7880-7882
# (7881 runs tests/fixtures/tailmsg_v1_pre_agents.py = an "old" receiver from before @agent addressing), plus a stub webhook receiver on 127.0.0.1:7879 (never 7766 and never ~/.tailmsg, so a live `serve` on
# this machine is not disturbed). Usage: sh selftest.sh
set -u
D=$(cd "$(dirname "$0")" && pwd); W="$D/test-run"; rm -rf "$W"; mkdir -p "$W"; cd "$W"
T="python3 $D/tailmsg.py"; IP=$(tailscale ip -4 | head -1); ME=$(tailscale status --json | python3 -c "import json,sys;print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))"); SHORT=${ME%%.*}
cfg(){ mkdir -p "$1"; echo "$2" > "$1/config.json"; }
cfg A "{\"port\":7866,\"allow\":[\"$SHORT\"]}"
H="\"on_message\":[\"$W/hook.sh\"]"
cfg B "{\"port\":7867,\"allow\":[\"$SHORT\"],\"max_msg_bytes\":4096,$H,\"wake_policy\":{\"default_max\":\"none\",\"peers\":{\"$SHORT\":\"now\"}}}"
cfg C "{\"port\":7868,\"allow\":[\"tag:agent\"]}"
cfg X "{\"port\":7869,\"allow\":[\"$SHORT\"],\"bind\":\"127.0.0.1\"}"
cfg D "{\"port\":7870,\"allow\":[\"$SHORT\"],$H,\"wake_policy\":{\"default_max\":\"now\",\"peers\":{\"$SHORT\":\"batch\"}}}"
QH=$(python3 -c "import datetime as d,zoneinfo as z;t=d.datetime.now(z.ZoneInfo('Asia/Tokyo'));f=lambda x:x.strftime('%H:%M');print('{\"start\":\"%s\",\"end\":\"%s\",\"tz\":\"Asia/Tokyo\"}'%(f(t-d.timedelta(hours=1)),f(t+d.timedelta(hours=1))))")
cfg Q "{\"port\":7871,\"allow\":[\"$SHORT\"],$H,\"wake_policy\":{\"default_max\":\"now\"},\"quiet_hours\":$QH}"
cfg R "{\"port\":7872,\"allow\":[\"$SHORT\"],$H,\"wake_policy\":{\"default_max\":\"now\"},\"wake_rate\":2}"
: > hook.log; printf '#!/bin/sh\necho "argc=$# $1" >> %s/hook.log\n' "$W" > hook.sh; chmod +x hook.sh
# ---- wake adapters: stub webhook (records every request; /ok -> 200, /flaky -> 500 once per id, /down -> 500)
cat > stub.py <<'PY'
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
seen = {}
class S(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        with open(sys.argv[1], "a") as f:
            f.write(json.dumps({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)}) + "\n")
        mid = json.loads(body)["id"]; seen[mid] = seen.get(mid, 0) + 1
        code = 500 if "/down" in self.path or ("/flaky" in self.path and seen[mid] == 1) else 200
        self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()
ThreadingHTTPServer(("127.0.0.1", 7879), S).serve_forever()
PY
: > stub.log; python3 stub.py "$W/stub.log" & STUBPID=$!
TOK=$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')       # appears only in the URL
SEC=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')       # the webhook secret
umask 077; echo "$SEC" > secret.txt; echo "$SEC" > secret-open.txt; echo "http://127.0.0.1:7879/flaky/$TOK" > v.url
umask 022; chmod 644 secret-open.txt
AC=',"wake_policy":{"default_max":"now"},"wake_backoff_s":0.2,"hook_timeout_s":5'
cfg E "{\"port\":7873,\"allow\":[\"$SHORT\"],\"wake_adapter\":{\"type\":\"exec\",\"argv\":[\"$W/hook.sh\"]}$AC}"
cfg F "{\"port\":7874,\"allow\":[\"$SHORT\"],\"wake_adapter\":{\"type\":\"exec\",\"argv\":[\"$W/fail.sh\"]}$AC}"
cfg W "{\"port\":7875,\"allow\":[\"$SHORT\"],\"wake_adapter\":{\"type\":\"webhook\",\"url_env\":\"TM_TEST_WAKE_URL\",\"secret_file\":\"$W/secret.txt\"}$AC}"
cfg V "{\"port\":7876,\"allow\":[\"$SHORT\"],\"wake_adapter\":{\"type\":\"webhook\",\"url_file\":\"$W/v.url\",\"secret_env\":\"TM_TEST_WAKE_SECRET\",\"header\":\"X-Wake-Token\",\"scheme\":\"\"}$AC}"
cfg G "{\"port\":7877,\"allow\":[\"$SHORT\"],\"wake_adapter\":{\"type\":\"webhook\",\"url_env\":\"TM_TEST_WAKE_URL\",\"secret_file\":\"$W/secret-open.txt\"}$AC}"
printf '#!/bin/sh\necho "$TAILMSG_MSG_ID" >> %s/fail.log\nexit 3\n' "$W" > fail.sh; chmod +x fail.sh; : > fail.log
# ---- @agent: M has agents grok (default, top-level adapter), codex (exec), 私信联系人 (webhook), quiet (wake_max batch)
printf '#!/bin/sh\necho "$1 $(basename "$2" .json) $TAILMSG_AGENT" >> %s/agents.log\n' "$W" > agenthook.sh; chmod +x agenthook.sh; : > agents.log
AH="\"type\":\"exec\",\"argv\":[\"$W/agenthook.sh\""
cfg M "{\"port\":7880,\"allow\":[\"$SHORT\"]$AC,\"wake_rate\":2,\"default_agent\":\"grok\",\"wake_adapter\":{$AH,\"top\"]},
 \"agents\":{\"grok\":{},\"codex\":{\"wake_adapter\":{$AH,\"codex\"]}},\"quiet\":{\"wake_adapter\":{$AH,\"quiet\"]},\"wake_max\":\"batch\"},
 \"私信联系人\":{\"wake_adapter\":{\"type\":\"webhook\",\"url_env\":\"TM_TEST_WAKE_URL2\",\"secret_file\":\"$W/secret.txt\"}}}}"
cfg S "{\"port\":7882,\"allow\":[\"$SHORT\"],\"self_agent\":\"grok\",\"agents\":{\"grok\":{}}}"
cfg O "{\"port\":7881,\"allow\":[\"$SHORT\"]}"
python3 "$D/tests/fixtures/tailmsg_v1_pre_agents.py" --home "$W/O" serve > O.log 2>&1 & OPID=$!
export TM_TEST_WAKE_URL="http://127.0.0.1:7879/ok/$TOK" TM_TEST_WAKE_SECRET="$SEC" TM_TEST_WAKE_URL2="http://127.0.0.1:7879/ok/dm/$TOK"
PIDS="$STUBPID $OPID"; for i in A B C X D Q R E F W V G M S; do $T --home "$W/$i" serve > $i.log 2>&1 & PIDS="$PIDS $!"; done; trap 'kill $PIDS 2>/dev/null' EXIT; sleep 1.5
unset TM_TEST_WAKE_URL TM_TEST_WAKE_SECRET TM_TEST_WAKE_URL2
P=0; F=0; ok(){ if [ "$1" = "$2" ]; then echo "PASS  $3"; P=$((P+1)); else echo "FAIL  $3 (got $1, want $2)"; F=$((F+1)); fi; }
st(){ python3 -c "import json,sys;print(json.loads(sys.stdin.read().splitlines()[-1])['status'])"; }
mid(){ python3 -c "import json;print(json.load(open('$1'))['id'])"; }
post(){ curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: ${3:-application/json}" --data-binary "$2" "http://$1/msg"; }
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ok "$($T --home A send $SHORT:7867 '你好 text only' | st)" 202 "send text"
head -c 300000 /dev/urandom > blob.bin; echo "报告内容" > 报告.md
$T --home A send $SHORT:7867 "two files" --file blob.bin --file 报告.md > s2.json; ID=$(mid s2.json)
$T --home B fetch "$ID" >/dev/null; ok $? 0 "send with files + fetch + sha256 verify"
ok "$(sha256sum < blob.bin)" "$(sha256sum < B/inbox/$ID/files/blob.bin)" "fetched bytes identical"
ok "$($T --home B reply "$ID" --port 7866 'got it' | st)" 202 "reply"
ok "$(python3 -c "import json,glob;print([json.load(open(f))['envelope'].get('reply_to') for f in glob.glob('A/inbox/*.json')][0])")" "$ID" "reply_to recorded"
ok "$($T --home A send $SHORT:7868 'nope' 2>/dev/null | st)" 403 "non-allowlisted peer rejected"
ok "$(post 127.0.0.1:7869 '{}')" 403 "non-tailnet source IP rejected"
$T --home A send $SHORT:7867 "ttl" --file 报告.md --ttl-hours 0.0005 > s7.json; sleep 3
ok "$($T --home B fetch $(mid s7.json) | grep -c 'HTTP 404')" 1 "expired file -> 404"
ok "$($T --home A send $SHORT:7867 "$(python3 -c 'print("x"*10000)')" 2>/dev/null | st)" 413 "oversized message -> 413"
$T --home A send $SHORT:7867 "tamper" --file 报告.md > s9.json; ID9=$(mid s9.json)
for m in A/outbox/*/meta.json; do grep -q "$ID9" "$m" && echo evil > "$(dirname "$m")/files/报告.md"; done
ok "$($T --home B fetch $ID9 | grep -c mismatch)" 1 "tampered file -> sha256 mismatch, discarded"
ok "$(post $IP:7867 "{\"v\":1,\"id\":\"spoof0001\",\"from\":\"other.example.ts.net\",\"to\":\"$ME\",\"ts\":\"$NOW\"}")" 403 "spoofed 'from' rejected"
ok "$(post $IP:7867 "{\"v\":1,\"id\":\"ssrf00001\",\"from\":\"$ME\",\"to\":\"$ME\",\"ts\":\"$NOW\",\"fetch\":[{\"url\":\"http://169.254.169.254/x\",\"sha256\":\"$(python3 -c 'print("0"*64)')\",\"size\":1}]}")" 400 "fetch url not on sender host rejected"
ok "$(post $IP:7867 "{\"v\":1,\"id\":\"unk000001\",\"from\":\"$ME\",\"to\":\"$ME\",\"ts\":\"$NOW\",\"cmd\":\"x\"}")" 400 "unknown field rejected"
ok "$(post $IP:7867 '{}' text/plain)" 415 "wrong content-type rejected"
E="{\"v\":1,\"id\":\"dup000001\",\"from\":\"$ME\",\"to\":\"$ME\",\"ts\":\"$NOW\"}"; post $IP:7867 "$E" >/dev/null
ok "$(post $IP:7867 "$E")" 200 "duplicate id -> 200"
ok "$(curl -s -o /dev/null -w '%{http_code}' http://$IP:7866/f/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/x)" 404 "guessed token -> 404"
# ---- wake control
eff(){ python3 -c "import json;r=json.load(open('$1/inbox/'+json.loads(open('$2').read().splitlines()[-1])['id']+'.json'));print(r['received']['wake']['effective'])"; }
hooks(){ sleep 0.5; grep -c "/$1/inbox/" hook.log; }
ok "$(hooks B)" 0 "default wake=batch (field omitted): stored, hook not run"
ok "$(grep -c '"wake"' B/inbox/$ID.json)" 1 "received.wake recorded"
ok "$(python3 -c "import json;print('wake' in json.load(open('B/inbox/$ID.json'))['envelope'])")" False "send omits wake when not given (old receivers stay compatible)"
$T --home A send $SHORT:7867 "need you" --wake now > w1.json
ok "$(eff B w1.json)/$(hooks B)/$(grep -c "argc=1 .*$(mid w1.json)" hook.log)" "now/1/1" "wake=now triggers on_message once, 1 arg"
$T --home A send $SHORT:7867 "fyi" --wake none > w2.json
ok "$(eff B w2.json)/$(hooks B)" "none/1" "wake=none: archived, no hook"
$T --home A send $SHORT:7870 "urgent?" --wake now > w3.json
ok "$(eff D w3.json)/$(hooks D)/$(grep -c 'capped by wake_policy' D/inbox/$(mid w3.json).json)" "batch/0/1" "now from sender capped to batch: no hook"
$T --home A send $SHORT:7871 "late" --wake now > w4.json
ok "$(eff Q w4.json)/$(hooks Q)/$(grep -c 'quiet hours' Q/inbox/$(mid w4.json).json)" "batch/0/1" "quiet hours downgrade now->batch"
for k in 1 2 3; do $T --home A send $SHORT:7872 "r$k" --wake now > r$k.json; done
ok "$(eff R r1.json)$(eff R r2.json)$(eff R r3.json)/$(hooks R)/$(grep -c 'rate limit' R/inbox/$(mid r3.json).json)" "nownowbatch/2/1" "rate limit: 3rd now in an hour -> batch"
ok "$(post $IP:7867 "{\"v\":1,\"id\":\"wake00001\",\"from\":\"$ME\",\"to\":\"$ME\",\"ts\":\"$NOW\",\"wake\":\"loud\"}")" 400 "invalid wake value -> 400"
N1=$($T --home B inbox --pending | wc -l)
ok "$($T --home B inbox --pending | grep -c "$(mid w2.json)")" 0 "inbox --pending excludes wake=none"
$T --home B ack "$(mid w1.json)" "$ID" >/dev/null
ok "$(($N1 - $($T --home B inbox --pending | wc -l)))/$(grep -c acked_at B/inbox/$ID.json)" "2/1" "ack removes messages from --pending"
# ---- wake adapters
ok "$(grep -c 'via deprecated on_message' B.log)" 1 "on_message still works as deprecated alias of exec adapter"
ok "$(python3 -c "import json;print(json.load(open('B/inbox/$(mid w1.json).json'))['wake_delivery']['ok'])")" True "alias: wake_delivery recorded ok"
$T --home A send $SHORT:7873 "exec adapter" --wake now > e1.json; sleep 0.8
ok "$(grep -c "argc=1 .*/E/inbox/$(mid e1.json).json" hook.log)" 1 "exec adapter: argv + [msg path], runs once"
$T --home A send $SHORT:7874 "exec fails" --wake now > f1.json; sleep 1.5
ok "$(grep -c "$(mid f1.json)" fail.log)/$($T --home F inbox --pending | grep -c "$(mid f1.json).*now\*!")" "3/1" "exec non-zero exit: 3 attempts, then left pending (marked !)"
LONG=$(python3 -c 'print("长"*250)')
$T --home A send $SHORT:7875 "$LONG" --kind ping --wake now > h1.json; H1=$(mid h1.json); sleep 0.8
req(){ python3 -c "import json;r=[json.loads(l) for l in open('stub.log') if '$1' in l];print($2)"; }
ok "$(req $H1 'len(r)')" 1 "webhook: one POST on wake=now"
ok "$(req $H1 "r[0]['headers'].get('Authorization')=='Bearer $SEC'")" True "webhook: Authorization: Bearer <secret from 0600 file>"
ok "$(req $H1 "sorted(r[0]['body'])")" "['event', 'from', 'id', 'kind', 'received_at', 'text_preview', 'v']" "webhook: body has exactly the documented keys"
ok "$(req $H1 "(r[0]['body']['event'],r[0]['body']['kind'],r[0]['body']['from']=='$ME',len(r[0]['body']['text_preview']))")" "('tailmsg.wake', 'ping', True, 200)" "webhook: event/kind/from, text_preview cut to 200 chars"
$T --home A send $SHORT:7875 "batch only" > h2.json; sleep 0.6
ok "$(req $(mid h2.json) 'len(r)')" 0 "webhook: wake=batch does not call adapter"
$T --home A send $SHORT:7876 "flaky" --wake now > v1.json; V1=$(mid v1.json); sleep 1.2
ok "$(req $V1 'len(r)')/$(req $V1 "[x['headers'].get('X-Wake-Token')=='$SEC' and 'Authorization' not in x['headers'] for x in r]")" "2/[True, True]" "webhook: custom header, raw secret (scheme \"\"), secret from env"
ok "$(python3 -c "import json;d=json.load(open('V/inbox/$V1.json'))['wake_delivery'];print(d['ok'],d['attempts'])")" "True 2" "webhook: HTTP 500 retried, then success"
echo "http://127.0.0.1:7879/down/$TOK" > v.url
$T --home A send $SHORT:7876 "down" --wake now > v2.json; V2=$(mid v2.json); sleep 1.5
ok "$(req $V2 'len(r)')/$(python3 -c "import json;d=json.load(open('V/inbox/$V2.json'))['wake_delivery'];print(d['ok'],d['attempts'],d['error'])")" "3/False 3 HTTP 500" "webhook: gives up after 3 failures"
ok "$($T --home V inbox --pending | grep -c "$V2")" 1 "webhook: failed wake leaves message pending"
echo "http://127.0.0.1:7879/ok/$TOK" > v.url
ok "$(TM_TEST_WAKE_SECRET="$SEC" $T --home V wake $V2 2>/dev/null)/$(req $V2 'len(r)')" "woke $V2/4" "tailmsg wake <id>: manual retry (url_file re-read at call time)"
$T --home A send $SHORT:7877 "open secret" --wake now > g1.json; sleep 1.2
ok "$(req $(mid g1.json) 'len(r)')/$(grep -c 'must be mode 0600' G.log)" "0/3" "webhook: group/world-readable secret_file refused, nothing sent"
ok "$(cat */config.json */inbox/*.json */*.log 2>/dev/null | grep -c -e "$SEC" -e "$TOK")" 0 "secret and webhook URL never in config, inbox or logs"
# ---- @agent addressing (SPEC §5.9)
rv(){ python3 -c "import json;r=json.load(open('$1/inbox/$2.json'));print($3)"; }
al(){ sleep 0.8; grep -c " $1 " agents.log; }
$T --home A send $SHORT:7880/codex "build broke" --wake now > a1.json; A1=$(mid a1.json)
ok "$(al $A1)/$(grep " $A1 " agents.log)/$(rv M $A1 "r['received']['agent'],r['received']['agent_status'],r['envelope']['agent']")" \
   "1/codex $A1 codex/codex known codex" "@agent: send host/agent wakes only that agent's adapter (TAILMSG_AGENT set)"
$T --home A send $SHORT:7880 "no agent" --wake now > a2.json; A2=$(mid a2.json)
ok "$(al $A2)/$(grep " $A2 " agents.log)/$(rv M $A2 "r['received']['agent'],r['received']['agent_status'],'agent' in r['envelope']")" \
   "1/top $A2 grok/grok default False" "@agent: no agent -> default_agent, top-level wake_adapter"
$T --home A send $SHORT:7880 "who?" --agent nobody --wake now > a3.json; A3=$(mid a3.json)
ok "$(sed -n 's/.*"status": \([0-9]*\).*/\1/p' a3.json)/$(al $A3)/$(rv M $A3 "r['received']['agent_status'],r['received']['wake']['effective']")/$(grep -c "unknown agent 'nobody'" M.log)" \
   "202/0/unknown batch/2" "@agent: unknown agent -> stored (202), agent_status unknown, nobody woken, logged"
$T --home A send $SHORT:7880/codex "r2" --wake now > a4.json; $T --home A send $SHORT:7880/codex "r3" --wake now > a5.json
$T --home A send "$SHORT:7880/私信联系人" "看一下" --wake now > a6.json; A6=$(mid a6.json); sleep 0.8
ok "$(eff M a4.json)$(eff M a5.json)/$(eff M a6.json)/$(grep -c 'rate limit' M/inbox/$(mid a5.json).json)" "nowbatch/now/1" "@agent: rate limit counted per (sender, agent)"
ok "$(req $A6 "len(r),r[0]['path'].startswith('/ok/dm/'),r[0]['body'].get('agent'),r[0]['headers'].get('Authorization')=='Bearer $SEC'")" \
   "1 True 私信联系人 True" "@agent: Unicode agent via own webhook adapter, payload carries agent"
$T --home A send $SHORT:7880 "cap" --agent quiet --wake now > a7.json
ok "$(eff M a7.json)/$(al $(mid a7.json))" "batch/0" "@agent: agents[].wake_max caps the level"
$T --home S send $SHORT:7880/codex "from grok" > a8.json; A8=$(mid a8.json)
ok "$(rv M $A8 "r['envelope']['from_agent']")" "grok" "@agent: self_agent sent as from_agent"
$T --home M reply $A8 --port 7882 "ack from codex" > a9.json; A9=$(mid a9.json)
ok "$(rv S $A9 "r['envelope']['agent'],r['envelope']['reply_to']=='$A8',r['received']['agent_status']")" "grok True known" "@agent: reply routes back to from_agent"
$T --home S send $SHORT:7880 "explicit as" --as tester > a10.json
ok "$(rv M $(mid a10.json) "r['envelope']['from_agent']")" "tester" "@agent: --as overrides self_agent"
ok "$($T --home M inbox --agent codex | grep -c '^[0-9]')/$($T --home M inbox --agent codex | grep -c ' @codex ')/$($T --home M inbox --agent grok | grep -c "$A2")/$($T --home M inbox --pending --agent 私信联系人 | grep -c "$A6")/$($T --home M inbox | grep -c '@nobody?')" \
   "4/4/1/1/1" "@agent: inbox --agent filter (default agent, --pending, unknown marked ?)"
L65=$(python3 -c 'print("a"*65)')
ok "$(post $IP:7880 "{\"v\":1,\"id\":\"agbad0001\",\"from\":\"$ME\",\"to\":\"$ME\",\"ts\":\"$NOW\",\"agent\":\"a/b\"}")$(post $IP:7880 "{\"v\":1,\"id\":\"agbad0002\",\"from\":\"$ME\",\"to\":\"$ME\",\"ts\":\"$NOW\",\"agent\":\"$L65\"}")$(post $IP:7880 "{\"v\":1,\"id\":\"agbad0003\",\"from\":\"$ME\",\"to\":\"$ME\",\"ts\":\"$NOW\",\"from_agent\":\"x\\ny\"}")$(post $IP:7880 "{\"v\":1,\"id\":\"agbad0004\",\"from\":\"$ME\",\"to\":\"$ME\",\"ts\":\"$NOW\",\"agent\":\"\"}")" \
   "400400400400" "@agent: invalid agent names ('/', >64 chars, control char, empty) -> 400"
ok "$(post $IP:7880 "{\"v\":1,\"id\":\"oldstyle1\",\"from\":\"$ME\",\"to\":\"$ME\",\"ts\":\"$NOW\",\"text\":\"v1 without agent\"}")/$(rv M oldstyle1 "r['received']['agent'],r['received']['agent_status']")" \
   "202/grok default" "@agent: old-style envelope still accepted, goes to default agent"
ok "$($T --home S send $SHORT:7881 'to old receiver' 2>s11.err | st)/$(grep -c 'does not understand from_agent' s11.err)/$(python3 -c "import json,glob;print(sum('from_agent' in json.load(open(f))['envelope'] for f in glob.glob('O/inbox/*.json')), len(glob.glob('O/inbox/*.json')))")" \
   "202/1/0 1" "@agent: old receiver: implicit from_agent auto-dropped, message delivered"
ok "$($T --home A send $SHORT:7881/codex 'explicit agent' 2>/dev/null | st)" 400 "@agent: old receiver: explicit @agent is not silently dropped (400)"
ok "$($T --home A send $SHORT:7880/codex x --agent grok 2>&1 | grep -c 'conflicting agent')" 1 "@agent: host/agent and --agent must agree"
cfg BADC '{"port":7883,"allow":[],"agents":{"a/b":{}}}'
ok "$($T --home BADC inbox 2>&1 | grep -c "agents\['a/b'\]")" 1 "@agent: invalid agent name in config refused"
echo "passed=$P failed=$F"; [ "$F" -eq 0 ]
