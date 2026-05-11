#!/usr/bin/env bash
# 月1回 (毎月1日 9:30 JST) 顧客分析データを Uレジ からスクレイプ。
# launchd com.hanabi-board.monthly-jouhou から呼ばれる。
# 失敗してもメインの auto-deploy には影響しない。

set -uo pipefail

ROOT="$HOME/hanabi-dashboard"
cd "$ROOT"

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/monthly_jouhou_$(date +%Y%m%d_%H%M%S).log"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
notify() {
  osascript -e "display notification \"$2\" with title \"$1\"" 2>/dev/null || true
}

log "==== HANABI 月次 顧客分析 + 外部メディア scrape ===="

# 前月を取得 (月初日に走るので、 当日では「前月の確定値」 を取りに行く)
PREV_YM=$(date -v -1m +%Y%m 2>/dev/null || date -d "-1 month" +%Y%m)
log "対象期間: $PREV_YM"

# Uレジ 4 レポートを順次scrape
# 各 ~2-5分、 失敗しても次へ進む
REPORTS="week age lost repeat"
SUCCESS=0
FAILED=0
for r in $REPORTS; do
  log "[scrape jouhou] $r $PREV_YM"
  if python3 scripts/scrape_jouhou.py "$r" "$PREV_YM" 2>&1 | tee -a "$LOG_FILE"; then
    SUCCESS=$((SUCCESS + 1))
  else
    FAILED=$((FAILED + 1))
    log "  ⚠️ $r failed — 続行"
  fi
  # Uレジ bot detection 回避のため間隔を空ける
  sleep 30
done

# 外部メディア (HotPepper Beauty + Instagram) scrape
# 月1回で十分 (評価点・フォロワーは日次変化が小さい)
log "[scrape external] HotPepper + Instagram"
if python3 scripts/scrape_external.py all 2>&1 | tee -a "$LOG_FILE"; then
  SUCCESS=$((SUCCESS + 1))
else
  FAILED=$((FAILED + 1))
  log "  ⚠️ external failed — 続行"
fi

log "[generate.py]"
python3 scripts/generate.py 2>&1 | tee -a "$LOG_FILE"

log "[git pull + commit if changed]"
git pull --rebase origin main >/dev/null 2>&1 || { git rebase --abort 2>/dev/null; log "❌ pull失敗"; exit 1; }
if git diff --quiet -- data/ docs/data.json; then
  log "  no changes — skipping commit"
else
  git add data/ docs/data.json
  COMMIT_MSG="auto: monthly 顧客分析 refresh ($PREV_YM)"
  git -c user.email=hanabi-board@local -c user.name="HANABI Auto" commit -q -m "$COMMIT_MSG"
  if git push -q origin main; then
    log "  ✓ pushed: $COMMIT_MSG"
  else
    log "❌ push失敗"
    notify "HANABI 月次 jouhou" "push 失敗"
    exit 1
  fi
fi

log "==== done ($SUCCESS 成功 / $FAILED 失敗) ===="
notify "HANABI 月次 顧客分析" "$SUCCESS 成功 / $FAILED 失敗 ($PREV_YM)"
