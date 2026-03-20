#!/bin/bash
# FutureC GitHub Backup — Daily incremental backup of OpenClaw config/memory
# Runs on VM-150 as chucknemo, pushes to github.com:CharlesMLETS/futurec-backup
# Schedule: 02:00 daily via systemd timer

set -uo pipefail

OPENCLAW_DIR="/home/openclaw/.openclaw"
REPO_DIR="/home/chucknemo/futurec-backup"
LOG_TAG="futurec-backup"
NTFY_URL="http://192.168.18.162"
NTFY_TOPIC="backups"
TODAY=$(date +%Y-%m-%d)
ERRORS=0

log() { logger -t "$LOG_TAG" "$1"; echo "$(date '+%Y-%m-%d %H:%M:%S') $1"; }

notify() {
    local title="$1" message="$2" priority="${3:-3}" tags="${4:-floppy_disk}"
    curl -s -o /dev/null \
        -H "Authorization: Bearer $NTFY_TOKEN" \
        -H "Title: $title" \
        -H "Priority: $priority" \
        -H "Tags: $tags" \
        -d "$message" \
        "$NTFY_URL/$NTFY_TOPIC" 2>/dev/null || log "WARNING: ntfy notification failed"
}

# --- Preflight ---
log "=== FutureC GitHub backup starting ==="

if ! sudo test -d "$OPENCLAW_DIR"; then
    log "CRITICAL: $OPENCLAW_DIR does not exist"
    notify "FutureC Backup FAILED" "Source directory missing" 5 "x"
    exit 1
fi

cd "$REPO_DIR" || exit 1

# --- SQLite handling ---
SQLITE_SRC="$OPENCLAW_DIR/memory/main.sqlite"
SQLITE_DEST="$REPO_DIR/memory"
mkdir -p "$SQLITE_DEST"
SQLITE_SIZE="n/a"

if sudo test -f "$SQLITE_SRC"; then
    INTEGRITY=$(sudo sqlite3 "$SQLITE_SRC" "PRAGMA integrity_check;" 2>&1)
    if [ "$INTEGRITY" = "ok" ]; then
        log "SQLite integrity OK"
        sudo sqlite3 "$SQLITE_SRC" ".backup '$SQLITE_DEST/main.sqlite'"
        sudo sqlite3 "$SQLITE_SRC" ".dump" > "$SQLITE_DEST/main.sql"
        SQLITE_SIZE=$(du -h "$SQLITE_DEST/main.sqlite" | cut -f1)
        log "SQLite backed up ($SQLITE_SIZE)"
    else
        log "WARNING: SQLite integrity check failed: $INTEGRITY"
        sudo cp "$SQLITE_SRC" "$SQLITE_DEST/main.sqlite"
        ((ERRORS++))
    fi
else
    log "WARNING: SQLite database not found at $SQLITE_SRC"
fi

# --- Rsync included files ---
log "Syncing files from $OPENCLAW_DIR..."

# workspace/ (personality files + memory subdirectory, exclude .git)
sudo rsync -a --delete --exclude='.git' "$OPENCLAW_DIR/workspace/" "$REPO_DIR/workspace/"


# identity/
mkdir -p "$REPO_DIR/identity"
sudo test -f "$OPENCLAW_DIR/identity/device.json" && sudo cp "$OPENCLAW_DIR/identity/device.json" "$REPO_DIR/identity/"

# cron/
mkdir -p "$REPO_DIR/cron"
sudo test -f "$OPENCLAW_DIR/cron/jobs.json" && sudo cp "$OPENCLAW_DIR/cron/jobs.json" "$REPO_DIR/cron/"

# scripts/
mkdir -p "$REPO_DIR/scripts"
sudo test -f "$OPENCLAW_DIR/scripts/transcribe.py" && sudo cp "$OPENCLAW_DIR/scripts/transcribe.py" "$REPO_DIR/scripts/"

# devices/
sudo test -d "$OPENCLAW_DIR/devices" && sudo rsync -a --delete "$OPENCLAW_DIR/devices/" "$REPO_DIR/devices/"

# logs/
mkdir -p "$REPO_DIR/logs"
sudo test -f "$OPENCLAW_DIR/logs/config-audit.jsonl" && sudo cp "$OPENCLAW_DIR/logs/config-audit.jsonl" "$REPO_DIR/logs/"

# Fix ownership (sudo copies are owned by root)
sudo chown -R chucknemo:chucknemo "$REPO_DIR"

log "File sync complete"

# --- Git operations ---
git add -A

if git diff --cached --quiet; then
    log "No changes detected — skipping commit"
    notify "FutureC Backup OK" "No changes to back up ($TODAY)" 2 "white_check_mark"
    exit 0
fi

# Generate commit message
CHANGED_COUNT=$(git diff --cached --numstat | wc -l | tr -d ' ')
SUMMARY_PARTS=()

git diff --cached --name-only | grep -q "^workspace/" && SUMMARY_PARTS+=("workspace")
git diff --cached --name-only | grep -q "^memory/" && SUMMARY_PARTS+=("memory")
git diff --cached --name-only | grep -q "^devices/" && SUMMARY_PARTS+=("devices")
git diff --cached --name-only | grep -q "^logs/" && SUMMARY_PARTS+=("audit log")

SUMMARY=$(IFS=', '; echo "${SUMMARY_PARTS[*]}")
[ -z "$SUMMARY" ] && SUMMARY="config"

git commit -m "backup($TODAY): $SUMMARY

Files changed: $CHANGED_COUNT
Memory DB size: $SQLITE_SIZE"

if git push origin main 2>&1 | while read -r line; do log "git: $line"; done; then
    log "Push to GitHub successful"
else
    log "ERROR: git push failed"
    ((ERRORS++))
fi

# --- Summary ---
if [ "$ERRORS" -gt 0 ]; then
    log "=== FutureC backup completed with $ERRORS error(s) ==="
    notify "FutureC Backup Partial" "$ERRORS error(s). $CHANGED_COUNT files changed ($TODAY). Check journalctl -u futurec-backup." 4 "warning"
    exit 1
else
    log "=== FutureC backup complete ==="
    notify "FutureC Backup OK" "$CHANGED_COUNT files backed up to GitHub ($TODAY). Changes: $SUMMARY" 2 "white_check_mark"
fi
