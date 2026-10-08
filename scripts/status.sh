#!/usr/bin/env bash
# One-screen health summary for Vitals. Read-only; every check degrades to
# "n/a" or "DOWN" instead of failing.
#
#   bash scripts/status.sh [--base URL] [--public URL]
#
#   --base URL     local service      (default http://127.0.0.1:${VITALS_PORT:-9000})
#   --public URL   public site, for the card check and TLS expiry
#                                     (default ${VITALS_BASE_URL:-https://vitals.43-165-190-110.sslip.io})
# Optional env: VITALS_STATUS_RPC (BSC JSON-RPC for the balance, default
# https://bsc-dataseed.bnbchain.org), VITALS_BNB_RESERVE (default 0.002).
# Needs curl and jq; openssl for the certificate line. Exit 0 when /health is ok.
set -u
export LC_ALL=C

BASE="http://127.0.0.1:${VITALS_PORT:-9000}"
PUBLIC="${VITALS_BASE_URL:-https://vitals.43-165-190-110.sslip.io}"
RPC="${VITALS_STATUS_RPC:-https://bsc-dataseed.bnbchain.org}"
RESERVE="${VITALS_BNB_RESERVE:-0.002}"
UNIT=vitals

while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="${2:?--base needs a URL}"; shift 2 ;;
    --base=*) BASE="${1#*=}"; shift ;;
    --public) PUBLIC="${2:?--public needs a URL}"; shift 2 ;;
    --public=*) PUBLIC="${1#*=}"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done
BASE="${BASE%/}"
PUBLIC="${PUBLIC%/}"
for c in curl jq; do
  command -v "$c" >/dev/null 2>&1 || { echo "status.sh needs $c" >&2; exit 2; }
done

get() { curl -fsS -m "${2:-10}" -H 'Accept: application/json' "$1" 2>/dev/null; }  # body, or empty
jf()  { printf '%s' "$2" | jq -r "$1" 2>/dev/null; }                                  # jq filter, body
row() { printf '%-8s %s\n' "$1" "$2"; }
JQDEFS='def dur: if . == null then "n/a" elif . < 120 then "\(floor)s" elif . < 7200 then "\(. / 60 | floor)m"
             elif . < 172800 then "\(. / 3600 | floor)h" else "\(. / 86400 | floor)d" end;'

printf 'Vitals status  %s  (local %s)\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$BASE"

# --- service --------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1 \
   && [ "$(systemctl show -p LoadState --value "$UNIT" 2>/dev/null)" = "not-found" ]; then
  row service "not installed (no $UNIT.service unit)"
elif command -v systemctl >/dev/null 2>&1; then
  state=$(systemctl is-active "$UNIT" 2>/dev/null)
  since=$(systemctl show -p ActiveEnterTimestamp --value "$UNIT" 2>/dev/null)
  restarts=$(systemctl show -p NRestarts --value "$UNIT" 2>/dev/null)
  row service "${state:-unknown}${since:+  since $since}${restarts:+  restarts $restarts}"
else
  row service "n/a (no systemctl)"
fi

# --- health ---------------------------------------------------------------
ok=0
health=$(get "$BASE/health" 5)
if [ "$(jf '.ok' "$health")" = "true" ]; then
  ok=1
  row health "$(jf "$JQDEFS"' "ok  v\(.version)  up \(.uptimeSeconds | dur)  \(if .live then "LIVE" else "dry run" end)  signer \(.signer)  signerMatchesOwner \(.signerMatchesOwner)  watcher \(if .watcherLastTick then "\(now - .watcherLastTick | dur) ago" else "idle" end)"' "$health")"
else
  row health "DOWN: no ok answer from $BASE/health"
fi

# --- identity and card ----------------------------------------------------
card=""
card_src="local"
[ "$ok" = 1 ] && card=$(get "$BASE/.well-known/agent-card.json" 10)
if [ -z "$card" ]; then card=$(get "$PUBLIC/.well-known/agent-card.json" 10); card_src="public"; fi
agent_id=$(jf '.erc8004.agentId // "pending"' "$card")
provider=$(jf '.erc8183.provider // .erc8004.owner // empty' "$card")
row agent "id ${agent_id:-n/a}  provider ${provider:-n/a}  (from $card_src card)"
pub=$(curl -sS -o /dev/null -m 10 -w 'HTTP %{http_code}, %{time_total}s' "$PUBLIC/.well-known/agent-card.json" 2>/dev/null) \
  || pub="unreachable"
row card "$PUBLIC/.well-known/agent-card.json  ($pub)"

# --- wallet balance -------------------------------------------------------
if [ -n "$provider" ]; then
  payload=$(jq -nc --arg a "$provider" '{jsonrpc: "2.0", id: 1, method: "eth_getBalance", params: [$a, "latest"]}')
  bal=$(curl -fsS -m 10 -H 'Content-Type: application/json' -d "$payload" "$RPC" 2>/dev/null | jq -r '
          def hex: ltrimstr("0x") | ascii_downcase | explode
                   | reduce .[] as $c (0; . * 16 + (if $c >= 97 then $c - 87 else $c - 48 end));
          .result // empty | hex / 1e18' 2>/dev/null)
  rpc_host="${RPC#*://}"; rpc_host="${rpc_host%%/*}"
  if [ -n "$bal" ]; then
    note=""
    if awk -v b="$bal" -v r="$RESERVE" 'BEGIN { exit !(b < r) }'; then note="  BELOW RESERVE $RESERVE: writes are refused"; fi
    row wallet "$(printf '%.6f' "$bal") BNB  (reserve $RESERVE, via $rpc_host)$note"
  else
    row wallet "balance n/a ($rpc_host did not answer)"
  fi
else
  row wallet "n/a (no provider address: card unavailable)"
fi

# --- keeper ---------------------------------------------------------------
if [ "$ok" = 1 ]; then
  keeper=$(get "$BASE/api/keeper/status" 25)
  line=$(jf '"HF \(if .healthFactor == null then "n/a" else (.healthFactor * 10000 | round / 10000) end)  \(if .live then "LIVE" else "dry run" end)  last tx \(if .lastTxAgeHours == null then "never" else "\(.lastTxAgeHours) h ago\(if .lastTxAgeHours > 26 then " (STALE)" else "" end)" end)  next \(.nextDecision.action // "n/a")\(if .nextDecision.reason then ": " + (.nextDecision.reason | .[0:48]) else "" end)\(if .error then "  error: " + (.error | .[0:60]) else "" end)"' "$keeper")
  row keeper "${line:-n/a (no answer from /api/keeper/status)}"
else
  row keeper "n/a (service down)"
fi

# --- ERC-8183 jobs --------------------------------------------------------
if [ "$ok" = 1 ]; then
  jobs=$(get "$BASE/api/jobs" 15)
  line=$(jf '(.counts // {}) as $c | ([$c[]] | add // 0) as $t
             | ([$c | to_entries[] | select(.key | IN("completed", "rejected", "expired", "skipped") | not) | .value] | add // 0) as $o
             | "open \($o) / total \($t)  quotes issued \(.quotesIssued // 0)"
               + (if $t > 0 then "  (" + ($c | to_entries | map("\(.key) \(.value)") | join(", ")) + ")" else "" end)' "$jobs")
  row jobs "${line:-n/a (no answer from /api/jobs)}"
else
  row jobs "n/a (service down)"
fi

# --- TLS certificate of the public host ----------------------------------
host="${PUBLIC#*://}"; host="${host%%/*}"; port=443
case "$host" in *:*) port="${host##*:}"; host="${host%:*}" ;; esac
if [ "${PUBLIC%%://*}" != "https" ]; then
  row tls "n/a ($PUBLIC is not https)"
elif ! command -v openssl >/dev/null 2>&1; then
  row tls "n/a (no openssl)"
else
  to=""; command -v timeout >/dev/null 2>&1 && to="timeout 10"
  end=$(echo | $to openssl s_client -connect "$host:$port" -servername "$host" 2>/dev/null \
          | openssl x509 -noout -enddate 2>/dev/null | sed 's/^notAfter=//')
  if [ -n "$end" ]; then
    exp=$(date -u -d "$end" +%s 2>/dev/null)
    left=""; [ -n "$exp" ] && left="  ($(( (exp - $(date -u +%s)) / 86400 )) days left)"
    row tls "$host expires $end$left"
  else
    row tls "$host: no certificate (TLS handshake failed)"
  fi
fi

[ "$ok" = 1 ]
