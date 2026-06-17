#!/usr/bin/env bash
# ============================================================================
# HANABI 新横浜 自己修復スクリプト (案3、 2026-06-17 追加)
#
# 目的:
#   朝の HANABI auto-deploy (8:30) が salon (NICENAIL) の完了前に走ると、
#   新横浜の数字が前日のまま焼き込まれる (= 2026-06-17 に発生した事故)。
#   salon が遅れて完了した後に この スクリプトが走り、 salon が最新なら
#   新横浜だけ再集計して 差分があれば自動で push する。
#
# 動作:
#   1. salon dist が「今日更新済」 か確認 (= salon 完了したか)
#      未完了なら 何もせず終了 (= まだ修復のしようがない)
#   2. git pull → 新横浜 再集計 → generate.py
#   3. 差分があれば commit + push + LINE WORKS に軽く通知
#      差分なければ no-op (= 朝の時点で既に最新だった = 正常)
#
# 安全性:
#   - salon dist が古い時は何もしない (= stale で上書きしない)
#   - 差分が無ければ commit しない (= idempotent、 何度走っても安全)
#   - 新横浜の集約 + data.json 再生成のみ。 綱島・宮古島は触らない
#
# launchd: com.hanabi-board.selfheal (毎日 10:30)
# ============================================================================
set -uo pipefail

ROOT="$HOME/hanabi-dashboard"
cd "$ROOT" || exit 1

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/selfheal_$(date +%Y%m%d_%H%M%S).log"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

# 対象月 (月初1日は前月、 HANABI 月初運用に合わせる)
YM=$(date +%Y%m)
DAY=$(date +%d)
if [ "$DAY" = "01" ]; then
  YM=$(date -v -1m +%Y%m 2>/dev/null || date -d "-1 month" +%Y%m)
fi

log "==== 新横浜 自己修復チェック ($YM) ===="

# --- Step 1: salon dist が今日更新済か ---
DIST="/Users/yoheimizuno/salon-dashboard/dist/index.html"
if [ ! -f "$DIST" ]; then
  log "  salon dist が存在しない → スキップ"
  exit 0
fi
DIST_DATE=$(date -r "$DIST" +%Y%m%d)
TODAY=$(date +%Y%m%d)
if [ "$DIST_DATE" != "$TODAY" ]; then
  log "  salon dist が本日($TODAY)未更新 (dist=$DIST_DATE) → salon まだ未完了、 スキップ"
  exit 0
fi
log "  ✓ salon dist は本日更新済 → 新横浜の鮮度を確認"

# --- Step 2: 最新を取り込んで 新横浜 再集計 ---
# 未コミット変更があれば退避 (前夜の手動編集等が残ってると pull rebase が落ちるため)
STASHED=0
if ! git diff --quiet || ! git diff --cached --quiet; then
  git stash push -u -m "selfheal stash $(date +%Y%m%d_%H%M%S)" 2>&1 | tee -a "$LOG_FILE"
  STASHED=1
  log "  ⚠️ 未コミット変更を stash で退避"
fi
if ! git pull --rebase origin main 2>&1 | tee -a "$LOG_FILE"; then
  log "  ❌ git pull 失敗 (rebase 衝突の可能性) → abort して終了"
  git rebase --abort 2>/dev/null || true
  [ "$STASHED" = "1" ] && git stash pop 2>/dev/null || true
  exit 1
fi
# 退避した変更を戻す (selfheal は data を再生成するので、 戻せなくても致命ではない)
if [ "$STASHED" = "1" ]; then
  git stash pop 2>&1 | tee -a "$LOG_FILE" || log "  ⚠️ stash pop 失敗 (手動確認: git stash list)"
fi

# 新横浜 再集計 (= daily_sales/staff_ranking/menu/extras を salon 最新で再生成)
python3 scripts/aggregate_nicenail_to_hanabi.py "$YM" 2>&1 | tee -a "$LOG_FILE"

# --- Step 3: 新横浜の「実データ」 に差分があるか判定 ---
# 🚨 重要: docs/data.json は generate.py が毎回 generated_at (timestamp) を書き換えるため、
#   data.json で差分判定すると 中身が同じでも毎日 誤発火 + 誤通知する。
#   そこで 新横浜の集約結果 (staff_ranking/daily_sales/menu/extras) だけで判定し、
#   実データが本当に変わった時のみ generate.py → push する。
SRC_FILES=(
  "data/daily_sales_${YM}_shinyokohama.csv"
  "data/staff_ranking_${YM}_shinyokohama.csv"
  "data/menu_${YM}_shinyokohama.json"
  "data/nicenail_extras_${YM}_shinyokohama.json"
)
if git diff --quiet -- "${SRC_FILES[@]}"; then
  log "  差分なし → 朝の時点で既に最新だった (正常)、 何もしない"
  exit 0
fi

log "  🔧 新横浜の実データに差分あり → 朝は古かった。 data.json 再生成して push"
python3 scripts/generate.py 2>&1 | tee -a "$LOG_FILE"
git add "${SRC_FILES[@]}" docs/data.json
git -c user.email=hanabi-board@local -c user.name="HANABI SelfHeal" \
    commit -q -m "auto(selfheal): 新横浜 最新 salon データで再集計 $(date '+%Y-%m-%d %H:%M')"
if git push -q origin main; then
  log "  ✓ push 完了 — 新横浜の数字を最新化しました"
  # 修復した時だけ LINE WORKS に通知 (= 見える化)
  python3 scripts/notify_lineworks.py hanabi selfheal 2>&1 | tee -a "$LOG_FILE" || log "  通知スキップ"
else
  log "  ❌ push 失敗"
  exit 1
fi

log "==== done ===="

# 古いログ削除 (30日)
find "$LOG_DIR" -name "selfheal_*.log" -mtime +30 -delete 2>/dev/null || true
