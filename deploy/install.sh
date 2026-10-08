#!/usr/bin/env bash
# Install or update Vitals on this host. Idempotent: run it again after every
# update of the checkout.
#
#   sudo bash deploy/install.sh [--src DIR]
#
#   --src DIR   checkout to install from (default: the one holding this script)
#
# It manages exactly these things and nothing else:
#   * system group and user `vitals` (no login shell, home /var/lib/vitals)
#   * /opt/vitals        code and virtualenv, root-owned, world-readable
#   * /var/lib/vitals    service state, vitals:vitals 0750
#   * /etc/vitals        root:vitals 0750; vitals.env is created from
#                        .env.example only when it does not exist yet
#   * /etc/vitals/wallet.key is never created, copied or read: if it exists,
#                        only its owner and mode are set (root:vitals 0640)
#   * /etc/systemd/system/vitals.service, enabled and (re)started
# Caddy, the firewall and every other service are left alone.
set -euo pipefail
umask 022

APP_DIR=/opt/vitals
VENV=$APP_DIR/.venv
DATA_DIR=/var/lib/vitals
ETC_DIR=/etc/vitals
ENV_FILE=$ETC_DIR/vitals.env
KEY_FILE=$ETC_DIR/wallet.key
UNIT_NAME=vitals.service
UNIT_DST=/etc/systemd/system/$UNIT_NAME
SVC_USER=vitals
PYTHON=/usr/bin/python3

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

SRC=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --src) [[ $# -ge 2 ]] || die "--src needs a directory"; SRC=$2; shift 2 ;;
    --src=*) SRC=${1#--src=}; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (usage: sudo bash deploy/install.sh [--src DIR])" ;;
  esac
done
[[ -n $SRC ]] || SRC="$(dirname "${BASH_SOURCE[0]}")/.."
src_arg=$SRC
SRC=$(cd "$src_arg" 2>/dev/null && pwd) || die "source directory not found: $src_arg"

# ---------------------------------------------------------------- preflight
[[ $EUID -eq 0 ]] || die "run as root: sudo bash deploy/install.sh"
for f in vitals/__main__.py config/agent.json pyproject.toml requirements.lock deploy/vitals.service .env.example; do
  [[ -f $SRC/$f ]] || die "$SRC is not a complete Vitals checkout (missing $f)"
done
[[ -x $PYTHON ]] || die "$PYTHON not found"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 12))' || die "Python 3.12 or newer is required"
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null || die "the Python venv module is missing (Ubuntu package python3-venv)"
for c in rsync systemctl getent useradd groupadd; do
  command -v "$c" >/dev/null || die "required command not found: $c"
done

env_get() {  # last value of KEY in the env file, empty when unset
  [[ -f $ENV_FILE ]] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r"'"'"
}

agent_id_of() {  # agentId in an agent.json, empty when null or unreadable
  "$PYTHON" -c 'import json, sys
try:
    v = json.load(open(sys.argv[1], encoding="utf-8")).get("agentId")
except Exception:
    v = None
print("" if v is None else v)' "$1" 2>/dev/null || true
}

# -------------------------------------------------------------- user, dirs
if ! getent group "$SVC_USER" >/dev/null; then
  groupadd --system "$SVC_USER"
  say "created system group $SVC_USER"
fi
if ! getent passwd "$SVC_USER" >/dev/null; then
  useradd --system --gid "$SVC_USER" --home-dir "$DATA_DIR" --no-create-home \
    --shell "$(command -v nologin || echo /usr/sbin/nologin)" --comment "Vitals service" "$SVC_USER"
  say "created system user $SVC_USER"
fi

mkdir -p "$APP_DIR" "$DATA_DIR" "$ETC_DIR"
chown root:root "$APP_DIR";               chmod 0755 "$APP_DIR"
chown -R "$SVC_USER:$SVC_USER" "$DATA_DIR"; chmod 0750 "$DATA_DIR"   # -R: files a root CLI run may have left
chown root:"$SVC_USER" "$ETC_DIR";        chmod 0750 "$ETC_DIR"

# ------------------------------------------------------------------- code
# Only the runtime tree is copied. Never: .git, virtualenvs, local state,
# caches, tests, .env files, key or keystore files.
ITEMS=()
for item in vitals config scripts deploy pyproject.toml requirements.lock README.md LICENSE .env.example; do
  if [[ -e $SRC/$item ]]; then ITEMS+=("$item"); fi
done

# A registered agentId recorded in the installed config/agent.json survives an
# update from a checkout that does not carry it yet.
saved_agent=""
installed_id=$(agent_id_of "$APP_DIR/config/agent.json")
source_id=$(agent_id_of "$SRC/config/agent.json")
if [[ -n $installed_id && -z $source_id ]]; then
  saved_agent=$(cat "$APP_DIR/config/agent.json")
fi

if [[ $SRC == "$APP_DIR" ]]; then
  say "source is $APP_DIR itself; skipping the copy"
else
  say "copying ${ITEMS[*]} from $SRC to $APP_DIR"
  (cd "$SRC" && rsync -rlpt --delete --safe-links --chmod=u+rwX,go+rX,go-w \
    --include=.env.example \
    --exclude=.git/ --exclude='.venv*/' --exclude=/.dev/ --exclude=/data/ --exclude=/tests/ \
    --exclude=__pycache__/ --exclude='*.pyc' --exclude=.pytest_cache/ --exclude=.mypy_cache/ \
    --exclude=.ruff_cache/ --exclude='*.egg-info/' \
    --exclude=.env --exclude='.env.*' --exclude='*.key' --exclude='*.pem' --exclude='*keystore*' \
    -- "${ITEMS[@]}" "$APP_DIR/")
  (cd "$APP_DIR" && chown -R root:root -- "${ITEMS[@]}")
fi
if [[ -n $saved_agent ]]; then
  printf '%s\n' "$saved_agent" > "$APP_DIR/config/agent.json"
  warn "kept the installed config/agent.json (agentId $installed_id): the checkout has none yet." \
       "Commit it to the repository or set VITALS_AGENT_ID in $ENV_FILE."
fi

# ------------------------------------------------------------------ venv
# Vitals itself is not pip-installed: it runs from /opt/vitals (WorkingDirectory
# plus `python -m vitals`), because config.py locates config/agent.json
# relative to the package source. The venv holds only the pinned third-party
# closure from requirements.lock; --no-deps keeps pip from resolving anything
# beyond it and `pip check` proves the set is consistent.
want_ver=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
have_ver=""
if [[ -x $VENV/bin/python ]] && "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
  have_ver=$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
fi
if [[ $have_ver != "$want_ver" ]]; then
  say "creating virtualenv $VENV (Python $want_ver)"
  "$PYTHON" -m venv --clear "$VENV"
fi
say "installing pinned dependencies from requirements.lock"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --no-input --no-cache-dir \
  --no-deps -r "$APP_DIR/requirements.lock"
"$VENV/bin/python" -m pip check --disable-pip-version-check >/dev/null || die "pip check failed: requirements.lock is not a consistent set"
"$VENV/bin/python" -m compileall -q "$APP_DIR/vitals" >/dev/null
(cd "$APP_DIR" && "$VENV/bin/python" -c 'import vitals.cli, vitals.server') \
  || die "import check failed; the service was not restarted"

# ---------------------------------------------------- unit, env, key file
install -m 0644 -o root -g root "$APP_DIR/deploy/vitals.service" "$UNIT_DST"

env_created=0
if [[ -L $ENV_FILE ]]; then
  warn "$ENV_FILE is a symlink; leaving it as it is"
elif [[ -e $ENV_FILE ]]; then
  chown root:"$SVC_USER" "$ENV_FILE"; chmod 0640 "$ENV_FILE"
else
  install -m 0640 -o root -g "$SVC_USER" "$SRC/.env.example" "$ENV_FILE"
  env_created=1
fi
missing_vars=$(comm -23 <(grep -oE '^VITALS_[A-Z0-9_]+' "$SRC/.env.example" 2>/dev/null | sort -u) \
                        <(grep -oE '^VITALS_[A-Z0-9_]+' "$ENV_FILE" 2>/dev/null | sort -u) | tr '\n' ' ')
if [[ -n $missing_vars ]]; then
  warn "$ENV_FILE lacks variables listed in .env.example (built-in defaults apply): $missing_vars"
fi

key_state=missing
if [[ -L $KEY_FILE ]]; then
  key_state="symlink, left untouched"
  warn "$KEY_FILE is a symlink; replace it with a regular file owned root:$SVC_USER, mode 0640"
elif [[ -f $KEY_FILE ]]; then
  chown root:"$SVC_USER" "$KEY_FILE"; chmod 0640 "$KEY_FILE"
  key_state="present, root:$SVC_USER 0640"
else
  warn "$KEY_FILE is missing: the operator places it (root:$SVC_USER 0640); no script ever writes it"
fi

# --------------------------------------------------------------- service
port=$(env_get VITALS_PORT); port=${port:-9000}
cfg_key=$(env_get VITALS_KEY_FILE)
start_blocked=""
if [[ -n $cfg_key && ! -e $cfg_key ]]; then
  start_blocked="VITALS_KEY_FILE=$cfg_key does not exist (the service would crash-loop). Place the key, or set VITALS_KEY_FILE= (empty) for a read-only start, then re-run this installer."
elif ! systemctl is-active --quiet "$UNIT_NAME"; then
  listeners=$(ss -Hltn "sport = :$port" 2>/dev/null || true)
  if [[ -n $listeners ]]; then
    start_blocked="port $port is already in use by another process (a dev server?). Stop it, then: systemctl start $UNIT_NAME"
  fi
fi

systemctl daemon-reload
systemctl enable --quiet "$UNIT_NAME"
health="not checked"
if [[ -n $start_blocked ]]; then
  warn "not starting $UNIT_NAME: $start_blocked"
  service_state="enabled, NOT started"
else
  say "restarting $UNIT_NAME"
  systemctl restart "$UNIT_NAME"
  health="no answer after 30 s (journalctl -u $UNIT_NAME -n 50)"
  for _ in $(seq 1 30); do
    if "$PYTHON" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2)' \
         "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      health="ok"
      break
    fi
    sleep 1
  done
  service_state="enabled, $(systemctl is-active "$UNIT_NAME" || true)"
fi

# --------------------------------------------------------------- summary
commit=$(git -c safe.directory="$SRC" -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
reg_cmd=""
for f in scripts/register.py scripts/register scripts/register.sh; do
  [[ -f $APP_DIR/$f ]] || continue
  case "$f" in
    *.py) reg_cmd=".venv/bin/python $f" ;;
    *.sh) reg_cmd="bash $f" ;;
    *) if [[ $(head -n 1 "$APP_DIR/$f") == *python* ]]; then reg_cmd=".venv/bin/python $f"; else reg_cmd="bash $f"; fi ;;
  esac
  break
done

cat <<EOF

Vitals install summary
  code     $APP_DIR (from $SRC, commit $commit)
  venv     $VENV (Python $want_ver, pinned by requirements.lock)
  data     $DATA_DIR
  env      $ENV_FILE ($([[ $env_created == 1 ]] && echo "created from .env.example" || echo "kept"))
  key      $KEY_FILE ($key_state)
  unit     $UNIT_DST ($service_state)
  health   http://127.0.0.1:$port/health: $health

Next steps
EOF
if [[ $env_created == 1 ]]; then
  echo "  * EDIT $ENV_FILE now (sudoedit). The template is a dry run with the keeper off."
fi
if [[ $key_state == missing ]]; then
  echo "  * Place the agent wallet key at $KEY_FILE (root:$SVC_USER 0640), then re-run this installer."
fi
cat <<'EOF'
  1. Helper for one-off commands with the service environment (paste into your shell;
     VX_USER=root runs it as root, needed when a command writes under /opt/vitals):
       vx() { sudo -u "${VX_USER:-vitals}" bash -c 'cd /opt/vitals && export VITALS_DATA_DIR=/var/lib/vitals && set -a && . /etc/vitals/vitals.env && set +a && exec "$@"' vx "$@"; }
EOF
echo "  2. Check the signer: curl -s http://127.0.0.1:$port/health  (\"signer\": true, \"signerMatchesOwner\": true)"
if [[ -n $reg_cmd ]]; then
  cat <<EOF
  3. ERC-8004 registration, dry run:  vx $reg_cmd
     Send (records the agentId in config/agent.json):  VX_USER=root vx env VITALS_LIVE=1 $reg_cmd --send
     Then put VITALS_AGENT_ID=<id> in $ENV_FILE, commit config/agent.json, systemctl restart $UNIT_NAME
EOF
else
  cat <<EOF
  3. ERC-8004 registration: scripts/register is not in this checkout yet. Update the checkout,
     re-run this installer, then dry run it with vx before sending with --send and VITALS_LIVE=1.
EOF
fi
cat <<'EOF'
  4. Keeper position, dry run:  vx .venv/bin/python -m vitals keeper open --collateral-bnb <BNB>
     Live:                      vx env VITALS_LIVE=1 .venv/bin/python -m vitals keeper open --collateral-bnb <BNB>
     Then VITALS_LIVE=1 and VITALS_KEEPER=1 in /etc/vitals/vitals.env and: sudo systemctl restart vitals
  5. Caddy: deploy/Caddyfile.vitals is the reference site block (the current block already proxies
     to 127.0.0.1:9000); swap it in by hand, then caddy validate and systemctl reload caddy.
  6. Status at any time:  bash /opt/vitals/scripts/status.sh
EOF

if [[ -n $start_blocked || $health != ok ]]; then
  exit 1
fi
