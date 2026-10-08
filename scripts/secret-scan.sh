#!/usr/bin/env bash
# Secret scan for a repository that is published.
#
#   scripts/secret-scan.sh            scan the working tree (tracked + untracked, not ignored)
#   scripts/secret-scan.sh --staged   scan what is staged for the next commit
#   scripts/secret-scan.sh --history  scan every commit reachable from any ref
#
# Fails (exit 1) on:
#   * 0x-prefixed 64-hex strings, unless listed in scripts/secret-scan.allow or
#     found in tests/fixtures/ on a line that names a hash, topic or tx
#   * a PEM private-key marker, GitHub/OpenAI/Slack style tokens
#   * mnemonic-like runs of 12 or more BIP-39 words
#   * .env files (other than .env.example) and KEY/SECRET/TOKEN/PASSWORD assignments with values
#   * host-private paths and retired project names
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
MODE="${1:-tree}"
ALLOW="scripts/secret-scan.allow"
SELF="scripts/secret-scan.sh"
fail=0

report() { echo "secret-scan: $*" >&2; fail=1; }

# Patterns are written so that this file does not match itself.
HEX64='0x[0-9a-fA-F]{64}'
TOKENS='(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})'
PRIV='PRIVATE[ ]KEY'
ENVLINE='^[[:space:]]*(export[[:space:]]+)?[A-Z0-9_]*(SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|MNEMONIC)[A-Z0-9_]*[[:space:]]*=[[:space:]]*[^[:space:]#"'"'"'<]{6,}'
PRIVATE_PATHS='(/home/[u]buntu|/root/\.|\.her[m]es|\.x4[2]9)'
OLD_NAMES='([Jj][a]g[a]|J[A]GA_HF|j[a]gah[f])'

list_files() {
  case "$MODE" in
    --staged) git diff --cached --name-only --diff-filter=ACMR ;;
    --history) : ;;
    *) git ls-files --cached --others --exclude-standard ;;
  esac
}

content_of() {
  case "$MODE" in
    --staged) git show ":$1" 2>/dev/null || true ;;
    *) cat -- "$1" 2>/dev/null || true ;;
  esac
}

allowed_hex() {
  [ -f "$ALLOW" ] && grep -qiF -- "$1" "$ALLOW"
}

bip39_check() {
  # stdin: text; prints offending runs. Uses the BIP-39 list shipped with eth-account when present.
  python3 -c '
import glob, re, sys
words = set()
for path in glob.glob(".venv*/lib/python3*/site-packages/eth_account/hdaccount/wordlist/english.txt"):
    words = set(open(path).read().split())
    break
for line in sys.stdin.read().splitlines():
    run = 0
    for t in re.split(r"[\s,\"\x27]+", line.strip()):
        hit = (t in words) if words else bool(re.fullmatch(r"[a-z]{3,8}", t))
        run = run + 1 if hit else 0
        if run >= 12:
            print(line.strip()[:120])
            break
'
}

scan_blob() {  # $1 = label (path or commit:path), stdin = content
  local label="$1" body
  body="$(cat)"
  [ -z "$body" ] && return 0
  local path="${label#*:}"
  [ "$MODE" = "--history" ] || path="$label"
  if printf '%s' "$body" | grep -qE "$PRIV"; then report "$label: contains a private-key marker"; fi
  if printf '%s' "$body" | grep -qE "$TOKENS"; then report "$label: contains an API token"; fi
  if printf '%s' "$body" | grep -qE "$PRIVATE_PATHS"; then report "$label: contains a host-private path"; fi
  if printf '%s' "$body" | grep -qE "$OLD_NAMES"; then report "$label: contains a retired project name"; fi
  if [[ "$path" != ".env.example" ]] && \
     printf '%s' "$body" | grep -qE "$ENVLINE"; then
    report "$label: contains a KEY/SECRET/TOKEN assignment with a value"
  fi
  while IFS= read -r line; do
    while IFS= read -r hex; do
      [ -z "$hex" ] && continue
      if allowed_hex "$hex"; then continue; fi
      if [[ "$path" == tests/fixtures/* ]] && printf '%s' "$line" | grep -qiE 'hash|topic|tx|block|deliverable|digest|sig'; then continue; fi
      report "$label: unlisted 64-hex value ${hex:0:10}..."
    done < <(printf '%s' "$line" | grep -oE "$HEX64" || true)
  done < <(printf '%s\n' "$body" | grep -E "$HEX64" || true)
  local runs
  runs="$(printf '%s' "$body" | bip39_check || true)"
  if [ -n "$runs" ]; then report "$label: mnemonic-like word run"; fi
}

check_name() {
  local p="$1"
  case "$(basename "$p")" in
    .env.example) ;;
    .env|.env.*|*.key|*.pem|*keystore*) report "$p: file type must never be committed" ;;
  esac
}

if [ "$MODE" = "--history" ]; then
  revs="$(git rev-list --all 2>/dev/null || true)"
  if [ -z "$revs" ]; then echo "secret-scan: no commits yet"; exit 0; fi
  for rev in $revs; do
    while IFS= read -r p; do
      [ -z "$p" ] && continue
      check_name "$p"
      scan_blob "${rev:0:8}:$p" < <(git show "$rev:$p" 2>/dev/null || true)
    done < <(git diff-tree --no-commit-id --name-only -r --root --diff-filter=ACMR "$rev")
  done
else
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    [ -f "$p" ] || continue
    check_name "$p"
    case "$p" in *.png|*.jpg|*.ico|*.woff*|*.gz) continue ;; esac
    scan_blob "$p" < <(content_of "$p")
  done < <(list_files)
fi

if [ "$fail" -ne 0 ]; then
  echo "secret-scan: FAILED" >&2
  exit 1
fi
echo "secret-scan: clean ($MODE)"
