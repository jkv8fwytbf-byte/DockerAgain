#!/bin/bash
# =============================================================================
# Container start-up script (runs as root, every time the container starts).
#
#   1. Read /etc/jupyterhub/users.txt   (one "username:password" per line)
#   2. Create a Linux account for every user that does not exist yet.
#      If /home/<user> already exists (from an earlier run) its owner UID is
#      re-used so the student keeps ownership of their files.
#   3. (Re)set every password from the file.
#        -> edit users.txt + restart the container = password reset
#   4. Link the shared folder into every home; admins get write access to it.
#   5. Start JupyterHub.
# =============================================================================
set -euo pipefail

USERS_FILE="${USERS_FILE:-/etc/jupyterhub/users.txt}"
ADMIN_USERS="${JUPYTERHUB_ADMIN_USERS:-teacher}"
SHARED_DIR="/srv/shared"
STATE_DIR="/srv/jupyterhub"
STUDENTS_GROUP="students"; STUDENTS_GID=3000
ADMINS_GROUP="admins";     ADMINS_GID=3001
FIRST_UID=2000

log() { echo "[entrypoint] $*"; }

if [[ ! -r "$USERS_FILE" ]]; then
  log "ERROR: $USERS_FILE not found or unreadable."
  log "       Mount your users.txt there (compose.yaml already does this)."
  exit 1
fi

getent group "$STUDENTS_GROUP" >/dev/null || groupadd -g "$STUDENTS_GID" "$STUDENTS_GROUP"
getent group "$ADMINS_GROUP"   >/dev/null || groupadd -g "$ADMINS_GID"   "$ADMINS_GROUP"
mkdir -p "$SHARED_DIR" "$STATE_DIR"
chmod 700 "$STATE_DIR"

next_uid=$FIRST_UID
count=0

while IFS= read -r raw || [[ -n "$raw" ]]; do
  line="${raw%%#*}"                          # drop comments
  line="${line//$'\r'/}"                     # drop Windows line endings
  line="${line#"${line%%[![:space:]]*}"}"    # trim leading whitespace
  line="${line%"${line##*[![:space:]]}"}"    # trim trailing whitespace
  [[ -z "$line" ]] && continue

  if [[ "$line" != *:* ]]; then
    log "WARNING: skipping malformed line (expected username:password): '$raw'"
    continue
  fi
  user="${line%%:*}"; user="${user,,}"       # JupyterHub lower-cases usernames
  pass="${line#*:}"
  if [[ -z "$user" || -z "$pass" ]]; then
    log "WARNING: skipping line with empty username or password: '$raw'"
    continue
  fi
  if [[ ! "$user" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
    log "WARNING: skipping '$user': usernames must be lowercase letters, digits, _ or - (max 32 chars)"
    continue
  fi
  if [[ "$user" == "root" || "$user" == "jovyan" ]]; then
    log "WARNING: '$user' is reserved, skipping"
    continue
  fi

  home="/home/$user"
  if ! id "$user" &>/dev/null; then
    uid=""
    if [[ -d "$home" ]]; then
      owner=$(stat -c %u "$home")
      if (( owner >= FIRST_UID )) && ! getent passwd "$owner" >/dev/null; then
        uid=$owner
      fi
    fi
    if [[ -z "$uid" ]]; then
      while getent passwd "$next_uid" >/dev/null \
         || [[ -n "$(find /home -mindepth 1 -maxdepth 1 -uid "$next_uid" -print -quit)" ]]; do
        next_uid=$((next_uid + 1))
      done
      uid=$next_uid; next_uid=$((next_uid + 1))
    fi

    if [[ -d "$home" ]]; then
      useradd --uid "$uid" --gid "$STUDENTS_GROUP" --shell /bin/bash \
              --home-dir "$home" --no-create-home "$user"
      if [[ "$(stat -c %u "$home")" != "$uid" ]]; then
        chown -R "$uid:$STUDENTS_GID" "$home"
      fi
      log "account '$user' re-created (uid $uid); existing files kept"
    else
      useradd --uid "$uid" --gid "$STUDENTS_GROUP" --shell /bin/bash \
              --home-dir "$home" --create-home "$user"
      log "account '$user' created (uid $uid)"
    fi
    chmod 750 "$home"                        # students cannot browse each other's files
  fi

  echo "$user:$pass" | chpasswd
  ln -sfn "$SHARED_DIR" "$home/shared"
  count=$((count + 1))
done < "$USERS_FILE"

if (( count == 0 )); then
  log "WARNING: no valid accounts found in $USERS_FILE; nobody will be able to log in."
fi

# --- admins: must also be listed in users.txt; they get write access to /srv/shared
IFS=',' read -ra admins <<< "$ADMIN_USERS"
for a in "${admins[@]}"; do
  a="${a,,}"; a="${a// /}"
  [[ -z "$a" ]] && continue
  if id "$a" &>/dev/null; then
    usermod -aG "$ADMINS_GROUP" "$a"
  else
    log "WARNING: admin '$a' is not in $USERS_FILE, so nobody can log in as that admin"
  fi
done
chown "root:$ADMINS_GROUP" "$SHARED_DIR"
chmod 2775 "$SHARED_DIR"                     # admins write, students read

log "$count account(s) ready. Starting JupyterHub on port 8000 ..."
cd "$STATE_DIR"
exec jupyterhub -f /etc/jupyterhub/jupyterhub_config.py
