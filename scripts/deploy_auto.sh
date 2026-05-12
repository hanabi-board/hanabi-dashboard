#!/usr/bin/env bash
# 毎朝の自動更新パイプライン
# launchdから呼ばれる: Uレジから最新CSV → JSON生成 → git push → GitHub Pages反映
#
# 環境: ~/hanabi-dashboard/

set -uo pipefail
# Note: -e は使わない。 個別ステップ失敗時に通知して exit するため、 trap で制御。

ROOT="$HOME/hanabi-dashboard"
cd "$ROOT"

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/deploy_$(date +%Y%m%d_%H%M%S).log"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# macOS Notification Center で通知 (補助、 メインはメール通知)
notify() {
  local title="$1"
  local msg="$2"
  osascript -e "display notification \"$msg\" with title \"$title\" sound name \"Basso\"" 2>/dev/null || true
}

# 失敗時のメール通知
mail_failure() {
  local error_msg="$1"
  python3 "$ROOT/scripts/notify.py" failure "$(date '+%Y-%m-%d %H:%M:%S')" "$error_msg" 2>&1 | tee -a "$LOG_FILE" || true
}

# エラー時のtrap: 通知して終了
on_error() {
  local exit_code=$?
  local lineno=$1
  log "❌ FAILED at line $lineno (exit $exit_code)"
  notify "HANABI Dashboard 自動更新 失敗" "$(date +%H:%M) line $lineno で停止"
  mail_failure "line $lineno で停止 (exit $exit_code)。 logs/deploy_$(date +%Y%m%d)_*.log を確認"
  exit $exit_code
}
trap 'on_error $LINENO' ERR

log "==== HANABI dashboard daily auto-deploy ===="

# 0. 最新リモートを取り込む (ブラウザUI経由の編集を取り逃さないため)
#    例: 麗花さんが UI から recruitment.json 編集 → push 済の状態から始める
log "[0/5] git pull --rebase (未コミット変更があれば stash で退避)"
# 未コミット変更を退避 (前夜の手動編集等が残ってると pull rebase が落ちるため)
STASHED=0
if ! git diff --quiet || ! git diff --cached --quiet; then
  git stash push -u -m "auto-deploy stash $(date +%Y%m%d_%H%M%S)" 2>&1 | tee -a "$LOG_FILE"
  STASHED=1
  log "  ⚠️ 未コミット変更を stash で退避"
fi
if ! git pull --rebase origin main 2>&1 | tee -a "$LOG_FILE"; then
  log "❌ git pull failed (リベース衝突の可能性)"
  notify "HANABI Dashboard 自動更新 失敗" "git pull でリベース失敗"
  mail_failure "git pull --rebase でリベース失敗。 ローカルに push 衝突 commit がある可能性"
  # 強制復旧: rebase abort
  git rebase --abort 2>/dev/null || true
  exit 1
fi
# 退避した変更を戻す (失敗しても続行 — auto-deploy 自身の変更を優先)
if [ "$STASHED" = "1" ]; then
  git stash pop 2>&1 | tee -a "$LOG_FILE" || log "  ⚠️ stash pop に失敗 (手動確認: git stash list)"
fi

# 1. Uレジから最新CSV DL (current month)
YM=$(date +%Y%m)
log "[1/5] Uレジ自動DL ($YM)"
python3 scripts/auto_download.py "$YM" 2>&1 | tee -a "$LOG_FILE"

# 1c. ナイスネイル → HANABI 集約変換 (新横浜店等、 ナイスネイル運営 HANABI 予算管理の店舗)
# ナイスネイル auto-deploy が 8:00 に meisai_*.csv を更新済の前提
log "[1c/5] ナイスネイル → HANABI 集約変換 ($YM)"
python3 scripts/aggregate_nicenail_to_hanabi.py "$YM" 2>&1 | tee -a "$LOG_FILE" || log "  ⚠️ ナイスネイル変換失敗 — 続行 (前回データ使用)"

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
PUSHED="no"
if git diff --quiet -- data/ docs/data.json; then
  log "  no changes — skipping commit"
else
  git add data/ docs/data.json
  COMMIT_MSG="auto: refresh data $(date '+%Y-%m-%d %H:%M')"
  git -c user.email=hanabi-board@local -c user.name="HANABI Auto" commit -q -m "$COMMIT_MSG"
  log "[5/5] git push"
  if ! git push -q origin main; then
    log "❌ git push failed"
    notify "HANABI Dashboard 自動更新 失敗" "git push が失敗"
    mail_failure "git push 失敗 — 認証または network 確認"
    exit 1
  fi
  log "  ✓ pushed: $COMMIT_MSG"
  PUSHED="yes"
fi

# 月初の自動デプロイは前月確定があるので軽く通知
if [ "$DAY" = "01" ]; then
  notify "HANABI Dashboard 月初更新完了" "前月確定値を反映済"
fi

log "==== done ===="

# 成功メール (毎朝必ず送信)
FINISH_TIME=$(date '+%Y-%m-%d %H:%M:%S')
# CSV件数の簡易サマリー (data/ の最新 CSV を数える)
TSU_CSV=$(ls -1 "$ROOT/data/daily_sales_${YM}_tsunashima.csv" 2>/dev/null)
ELE_CSV=$(ls -1 "$ROOT/data/daily_sales_${YM}_miyakojima.csv" 2>/dev/null)
TSU_CNT=$([ -n "$TSU_CSV" ] && wc -l < "$TSU_CSV" | tr -d ' ' || echo "—")
ELE_CNT=$([ -n "$ELE_CSV" ] && wc -l < "$ELE_CSV" | tr -d ' ' || echo "—")
SUMMARY="${YM} CSV: 綱島=${TSU_CNT}行 / 宮古島=${ELE_CNT}行 (git push: ${PUSHED})"
python3 "$ROOT/scripts/notify.py" success "$FINISH_TIME" "$SUMMARY" 2>&1 | tee -a "$LOG_FILE" || log "  ⚠️ メール送信失敗"

# 古いログを削除（30日以上）
find "$LOG_DIR" -name "deploy_*.log" -mtime +30 -delete 2>/dev/null || true
