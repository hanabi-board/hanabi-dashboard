#!/usr/bin/env bash
# 毎朝の自動更新パイプライン
# launchdから呼ばれる: Uレジから最新CSV → JSON生成 → git push → GitHub Pages反映
#
# 環境: ~/hanabi-dashboard/

set -euo pipefail

ROOT="$HOME/hanabi-dashboard"
cd "$ROOT"

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/deploy_$(date +%Y%m%d_%H%M%S).log"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
fail() { log "❌ $*"; exit 1; }

log "==== HANABI dashboard daily auto-deploy ===="

# 1. Uレジから最新CSV DL (current month)
YM=$(date +%Y%m)
log "[1/5] Uレジ自動DL ($YM)"
python3 scripts/auto_download.py "$YM" 2>&1 | tee -a "$LOG_FILE"

# 2. 月初日 (1日) なら前月の最終日も補完取得 (前月CSVが空のままにならないように)
DAY=$(date +%d)
if [ "$DAY" = "01" ]; then
  PREV_YM=$(date -v -1m +%Y%m 2>/dev/null || date -d "-1 month" +%Y%m)
  log "[1b/5] 月初なので前月($PREV_YM)も補完取得"
  python3 scripts/auto_download.py "$PREV_YM" 2>&1 | tee -a "$LOG_FILE"
fi

# 3. メニュー別実績 (情報分析→技術の実績) スクレイプ
#    Note: メニュー別はUレジに公式CSV出力がないためHTMLスクレイプ。
#          失敗してもメインのデータデプロイは続行する (|| true で non-fatal)。
log "[2/5] メニュー別 scrape ($YM)"
python3 scripts/scrape_menu.py "$YM" 2>&1 | tee -a "$LOG_FILE" || log "  ⚠️ menu scrape failed — 続行 (前回データ使用)"

# 月初日のみ前月分メニューも再取得 (月末確定値の反映)
if [ "$DAY" = "01" ]; then
  log "[2b/5] 月初なので前月($PREV_YM)メニューも補完取得"
  python3 scripts/scrape_menu.py "$PREV_YM" 2>&1 | tee -a "$LOG_FILE" || log "  ⚠️ prev menu scrape failed — 続行"
fi

# 4. JSON再生成
log "[3/5] generate.py"
python3 scripts/generate.py 2>&1 | tee -a "$LOG_FILE"

# 5. 差分があればコミット&プッシュ
log "[4/5] git commit if changed"
if git diff --quiet -- data/ docs/data.json; then
  log "  no changes — skipping commit"
else
  git add data/ docs/data.json
  COMMIT_MSG="auto: refresh data $(date '+%Y-%m-%d %H:%M')"
  git -c user.email=hanabi-board@local -c user.name="HANABI Auto" commit -q -m "$COMMIT_MSG"
  log "[5/5] git push"
  git push -q origin main
  log "  ✓ pushed: $COMMIT_MSG"
fi

log "==== done ===="
