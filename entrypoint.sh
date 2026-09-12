#!/bin/bash
# =============================================================================
# Container start-up script (runs as root, every time the container starts).
#
#   1. Make sure the state folders on the "hubstate" volume exist.
#   2. Create the Linux accounts:
#        - if the console's account module is available: roster.json is the
#          source of truth (seeded from users.txt on the very first start);
#        - otherwise (transitional): read users.txt directly, as in image 1.0.
#      Existing home directories keep their owner UID, so student files survive
#      a container replacement.
#   3. Put the default logo / branding file in place if the teacher has not
#      uploaded their own yet.
#   4. Check the teacher console can be imported; if not, start without it
#      rather than letting a broken console take the Hub down.
#   5. Start JupyterHub.
# =============================================================================
set -uo pipefail

USERS_FILE="${USERS_FILE:-/etc/jupyterhub/users.txt}"
ADMIN_USERS="${JUPYTERHUB_ADMIN_USERS:-teacher}"
STATE_DIR="/srv/jupyterhub"
SHARED_DIR="/srv/shared"
ROSTER="$STATE_DIR/roster.json"
STUDENTS_GROUP="students"; STUDENTS_GID=3000
ADMINS_GROUP="admins";     ADMINS_GID=3001
FIRST_UID=2000
export PYTHONPATH=/opt/classroom
export CONSOLE_ENABLED="${CONSOLE_ENABLED:-1}"

log() { echo "[entrypoint] $*"; }

# --- 1. folders and groups ----------------------------------------------------
mkdir -p "$STATE_DIR"/{logs,backups,branding,removed,tmp} "$SHARED_DIR"
chmod 700 "$STATE_DIR" "$STATE_DIR/backups" "$STATE_DIR/removed" "$STATE_DIR/tmp"
chmod 755 "$STATE_DIR/branding" "$STATE_DIR/logs"
getent group "$STUDENTS_GROUP" >/dev/null || groupadd -g "$STUDENTS_GID" "$STUDENTS_GROUP"
getent group "$ADMINS_GROUP"   >/dev/null || groupadd -g "$ADMINS_GID"   "$ADMINS_GROUP"

# --- 2. accounts ---------------------------------------------------------------
if [[ -d "$USERS_FILE" ]]; then
  # Docker creates a directory when the host file is missing at "compose up".
  log "WARNING: $USERS_FILE is a directory (users.txt missing on the host); ignoring it"
fi

sync_with_bash() {
  # Transitional path, identical to image 1.0: one "username:password" per line.
  if [[ ! -r "$USERS_FILE" || -d "$USERS_FILE" ]]; then
    log "WARNING: no readable $USERS_FILE; no accounts created"
    return 0
  fi
  local next_uid=$FIRST_UID count=0 raw line user pass home uid owner
  while IFS= read -r raw || [[ -n "$raw" ]]; do
    line="${raw%%#*}"; line="${line//$'\r'/}"
    line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" ]] && continue
    if [[ "$line" != *:* ]]; then log "WARNING: skipping malformed line (expected username:password): '$raw'"; continue; fi
    user="${line%%:*}"; user="${user,,}"; pass="${line#*:}"
    if [[ -z "$user" || -z "$pass" ]]; then log "WARNING: skipping line with empty username or password: '$raw'"; continue; fi
    if [[ ! "$user" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then log "WARNING: skipping '$user': invalid username"; continue; fi
    if [[ "$user" == "root" || "$user" == "jovyan" ]]; then log "WARNING: '$user' is reserved, skipping"; continue; fi
    home="/home/$user"
    if ! id "$user" &>/dev/null; then
      uid=""
      if [[ -d "$home" ]]; then
        owner=$(stat -c %u "$home")
        if (( owner >= FIRST_UID )) && ! getent passwd "$owner" >/dev/null; then uid=$owner; fi
      fi
      if [[ -z "$uid" ]]; then
        while getent passwd "$next_uid" >/dev/null \
           || [[ -n "$(find /home -mindepth 1 -maxdepth 1 -uid "$next_uid" -print -quit)" ]]; do
          next_uid=$((next_uid + 1))
        done
        uid=$next_uid; next_uid=$((next_uid + 1))
      fi
      if [[ -d "$home" ]]; then
        useradd --uid "$uid" --gid "$STUDENTS_GROUP" --shell /bin/bash --home-dir "$home" --no-create-home "$user"
        [[ "$(stat -c %u "$home")" == "$uid" ]] || chown -R "$uid:$STUDENTS_GID" "$home"
        log "account '$user' re-created (uid $uid); existing files kept"
      else
        useradd --uid "$uid" --gid "$STUDENTS_GROUP" --shell /bin/bash --home-dir "$home" --create-home "$user"
        log "account '$user' created (uid $uid)"
      fi
      chmod 750 "$home"
    fi
    echo "$user:$pass" | chpasswd
    ln -sfn "$SHARED_DIR" "$home/shared"
    count=$((count + 1))
  done < "$USERS_FILE"
  (( count == 0 )) && log "WARNING: no valid accounts found in $USERS_FILE; nobody will be able to log in."
  IFS=',' read -ra admins <<< "$ADMIN_USERS"
  for a in "${admins[@]}"; do
    a="${a,,}"; a="${a// /}"; [[ -z "$a" ]] && continue
    if id "$a" &>/dev/null; then usermod -aG "$ADMINS_GROUP" "$a"; else log "WARNING: admin '$a' is not in $USERS_FILE"; fi
  done
  log "$count account(s) ready (users.txt mode)"
}

if python -c "import console.accounts" 2>/dev/null; then
  # roster.json mode: seed once from users.txt, then sync every start.
  python -m console.accounts sync --roster "$ROSTER" --seed-from "$USERS_FILE" --admins "$ADMIN_USERS" \
    || log "WARNING: account sync exited with status $? - starting the Hub anyway; see messages above"
else
  sync_with_bash
fi
chown "root:$ADMINS_GROUP" "$SHARED_DIR"
chmod 2775 "$SHARED_DIR"                       # admins write, students read

# --- 3. branding defaults --------------------------------------------------------
[[ -f "$STATE_DIR/branding/logo.png" ]]      || cp /opt/classroom/branding-defaults/default-logo.png     "$STATE_DIR/branding/logo.png"
[[ -f "$STATE_DIR/branding/branding.json" ]] || cp /opt/classroom/branding-defaults/branding.default.json "$STATE_DIR/branding/branding.json"
chmod 644 "$STATE_DIR/branding/logo.png"
chmod 600 "$STATE_DIR/branding/branding.json"

# --- 4. console pre-flight -------------------------------------------------------
if [[ "$CONSOLE_ENABLED" == "1" ]]; then
  if python -c "import console.app" 2>/tmp/console-import.err; then
    log "teacher console: enabled at /services/console/"
  else
    log "WARNING: teacher console cannot be imported; starting WITHOUT it:"
    sed 's/^/[entrypoint]   /' /tmp/console-import.err | tail -5
    export CONSOLE_ENABLED=0
  fi
else
  log "teacher console: disabled (CONSOLE_ENABLED=$CONSOLE_ENABLED)"
fi

# --- 5. go ------------------------------------------------------------------------
log "Starting JupyterHub on port 8000 ..."
cd "$STATE_DIR"
exec jupyterhub -f /etc/jupyterhub/jupyterhub_config.py
