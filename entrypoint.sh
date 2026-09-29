#!/bin/sh
# Runs at every container start: pull the newest code, install packages if they changed,
# back up the database, then start the bot. Rolls back to the last good commit if the
# new code keeps crashing.
set -eu

CODE_ROOT="${CODE_ROOT:-/app/code}"
SRC="$CODE_ROOT/src"
VENV="$CODE_ROOT/venv"
STATE="$CODE_ROOT/state"
DATA_DIR="${DATA_DIR:-/app/data}"
GIT_BRANCH="${GIT_BRANCH:-stable}"
DEPLOY_KEY="${DEPLOY_KEY:-/run/deploy_key}"
MAX_CRASHES=3

mkdir -p "$STATE" "$DATA_DIR"
log() { echo "[entrypoint] $*"; }
crashes=$(cat "$STATE/crash_count" 2>/dev/null || echo 0)

if [ -n "${GIT_REPO:-}" ]; then
    if [ -f "$DEPLOY_KEY" ]; then
        # ssh refuses keys that others can read, and the mounted file may be read-only.
        cp "$DEPLOY_KEY" /tmp/deploy_key && chmod 600 /tmp/deploy_key
        export GIT_SSH_COMMAND="ssh -i /tmp/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$CODE_ROOT/known_hosts"
    fi

    if [ ! -d "$SRC/.git" ]; then
        log "Cloning $GIT_REPO ($GIT_BRANCH)"
        git clone --quiet --branch "$GIT_BRANCH" "$GIT_REPO" "$SRC"
    fi

    last_good=$(cat "$STATE/last_good_commit" 2>/dev/null || true)
    bad=$(cat "$STATE/bad_commit" 2>/dev/null || true)

    if [ "$crashes" -ge "$MAX_CRASHES" ] && [ -n "$last_good" ]; then
        failed=$(git -C "$SRC" rev-parse --short HEAD)
        log "Crashed $crashes times on $failed, rolling back to $last_good"
        git -C "$SRC" rev-parse HEAD > "$STATE/bad_commit"
        echo "Version $failed startade inte" > "$STATE/rollback_note"
        git -C "$SRC" checkout --quiet --detach "$last_good"
        crashes=0
    else
        if git -C "$SRC" fetch --quiet origin "$GIT_BRANCH"; then
            target=$(git -C "$SRC" rev-parse "origin/$GIT_BRANCH")
            if [ "$target" = "$bad" ] && [ -n "$last_good" ]; then
                log "Newest commit is the one that failed before; staying on $last_good"
                target="$last_good"
            fi
            git -C "$SRC" checkout --quiet --detach "$target"
        else
            log "Could not reach GitHub; starting the code already on disk"
        fi
    fi
    git -C "$SRC" rev-parse HEAD > "$STATE/candidate_commit"
    log "Running $(git -C "$SRC" log -1 --format='%h %s')"
elif [ -d /app/src ]; then
    SRC=/app/src  # local development: code mounted into the container
else
    log "Set GIT_REPO, or mount the code at /app/src"
    exit 1
fi

# Python packages live on the volume and are reinstalled only when requirements.txt changes.
if [ ! -x "$VENV/bin/python" ]; then
    python -m venv "$VENV"
fi
req_hash=$(sha256sum "$SRC/requirements.txt" | cut -d' ' -f1)
if [ "$req_hash" != "$(cat "$STATE/requirements_hash" 2>/dev/null || true)" ]; then
    log "Installing Python packages"
    "$VENV/bin/pip" install --quiet -r "$SRC/requirements.txt"
    echo "$req_hash" > "$STATE/requirements_hash"
fi

# Keep the 14 newest database backups.
if [ -f "$DATA_DIR/studybuddy.db" ]; then
    mkdir -p "$DATA_DIR/backups"
    cp "$DATA_DIR/studybuddy.db" "$DATA_DIR/backups/studybuddy-$(date +%Y%m%d-%H%M%S).db"
    ls -1t "$DATA_DIR"/backups/studybuddy-*.db | tail -n +15 | xargs -r rm --
fi

# The bot resets this to 0 once it has connected to Telegram.
echo $((crashes + 1)) > "$STATE/crash_count"

export STATE_DIR="$STATE" CODE_DIR="$SRC"
cd "$SRC"
exec "$VENV/bin/python" -m app.main
