#!/usr/bin/env bash
# 週次バックアップ: data/ と scripts/ を ~/Documents/HANABI_backup/ にタイムスタンプ付きで保存
# launchd 経由で 毎週日曜 6:00 JST 実行 (com.hanabi-board.weekly-backup.plist)
#
# 復旧手順:
#   1. backup フォルダの最新スナップショットを ~/hanabi-dashboard/data/ に展開
#   2. python3 scripts/generate.py
#   3. git push (もしくは GitHub から clone してから上書き)

set -uo pipefail

ROOT="$HOME/hanabi-dashboard"
BACKUP_BASE="$HOME/Documents/HANABI_backup"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$BACKUP_BASE/$TS"

mkdir -p "$BACKUP_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
notify() {
  osascript -e "display notification \"$2\" with title \"$1\"" 2>/dev/null || true
}

log "==== HANABI weekly backup → $BACKUP_DIR ===="

# 1. data/ と scripts/ をコピー (CSV/JSON/Python全部)
log "[1/3] copy data/ + scripts/"
cp -R "$ROOT/data" "$BACKUP_DIR/data" || { notify "HANABI backup FAILED" "data copy 失敗"; exit 1; }
cp -R "$ROOT/scripts" "$BACKUP_DIR/scripts" || { notify "HANABI backup FAILED" "scripts copy 失敗"; exit 1; }
# docs/data.json も別途保存 (generated)
mkdir -p "$BACKUP_DIR/docs"
cp "$ROOT/docs/data.json" "$BACKUP_DIR/docs/data.json" 2>/dev/null || true

# 2. metadata (Gitのrev + サイズ)
log "[2/3] metadata"
{
  echo "Backup taken: $(date)"
  echo "Git commit: $(cd "$ROOT" && git rev-parse HEAD 2>/dev/null || echo 'unknown')"
  echo "Git branch: $(cd "$ROOT" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
  echo ""
  echo "Sizes:"
  du -sh "$BACKUP_DIR/data" "$BACKUP_DIR/scripts" "$BACKUP_DIR/docs" 2>/dev/null
} > "$BACKUP_DIR/BACKUP_INFO.txt"

# 3. 古いバックアップを削除 (10世代以上残ってたら最古を削除)
log "[3/3] rotate (keep latest 10)"
cd "$BACKUP_BASE"
ls -1t | tail -n +11 | while read old; do
  if [ -d "$old" ]; then
    log "  removing old: $old"
    rm -rf "$BACKUP_BASE/$old"
  fi
done

SIZE=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
log "✓ done ($SIZE)"
