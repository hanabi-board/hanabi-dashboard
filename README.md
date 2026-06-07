# HANABI Dashboard

HANABI グループ用 経営ダッシュボード。 サロン事業部とは完全独立。
毎朝3分で経営の全体像を把握する CEO ビュー。

🔗 **公開URL**: https://dashboard.hanabi2020.co.jp/

## ローカル起動

```bash
cd ~/hanabi-dashboard
python3 scripts/generate.py     # CSV → docs/data.json 集計
python3 -m http.server 8767 --directory docs
```

ブラウザで http://localhost:8767/ → 本部/管理者 アカウント選択

## ファイル構成

```
~/hanabi-dashboard/
├── data/
│   ├── budgets.json                       # 予算 (本部UIから編集可)
│   ├── staff_profiles.json                # スタッフメタデータ (本部UIから編集可)
│   ├── recruitment.json                   # 採用候補者 (本部UIから編集可)
│   ├── external_targets.json              # HotPepper/Instagram URL
│   ├── daily_sales_YYYYMM_<store>.csv     # 日別×店舗 (Shift-JIS, Uレジから自動DL)
│   ├── staff_ranking_YYYYMM_<store>.csv   # 月別×店舗×スタッフ
│   ├── menu_<period>_<store>.json         # メニュー別 (scrape_menu.py)
│   ├── jouhou_<report>_<period>_<store>.json   # 情報分析 (scrape_jouhou.py)
│   ├── external_<source>_<date>.json      # HotPepper/Instagram
│   └── denpyo_<range>_<store>.json        # 取引明細 (scrape_denpyo.py)
├── scripts/
│   ├── generate.py                # CSV/JSON → docs/data.json 集計
│   ├── auto_download.py           # Uレジ → 4 CSV 自動DL (Playwright)
│   ├── deploy_auto.sh             # 毎朝のorchestration (launchd経由)
│   ├── backup.sh                  # 週次バックアップ
│   ├── backfill.py YYYYMM YYYYMM  # 過去月の一括取得
│   ├── scrape_menu.py             # メニュー別実績
│   ├── scrape_jouhou.py           # 情報分析の各レポート
│   ├── scrape_external.py         # HotPepper/Instagram
│   ├── scrape_denpyo.py           # 伝票明細 (取引粒度)
│   └── com.hanabi-board.*.plist   # launchd 設定
└── docs/
    ├── index.html                 # ダッシュボード本体
    ├── data.json                  # generate.py の出力
    └── assets/                    # ロゴ・スタッフ写真等
```

## 店舗ID

| ID | 名前 | 業態 | 開店日 |
|---|---|---|---|
| `tsunashima` | Hanabi 綱島店 | ヘア専門 | 2022-05-01 |
| `miyakojima` | ELLE by Hanabi 宮古島店 | ヘア / アイ / ネイル | 2025-09-01 |

## タブ構造 (9タブ)

| タブ | 内容 |
|---|---|
| **サマリー** | アラート + 6 KPI + 状態パネル + 月次推移 + 店舗カード + 部門別 + 新規vsリピート |
| **売上・予算** | 店舗カード (予算進捗 + 着地予測 + 部門別) + 日別推移チャート |
| **来客分析** | 店舗別 (新規vsリピート / 指名vsフリー / 男女別) + 月次推移6種 |
| **スタッフ実績** | 店舗別ランキング (並び順切替) + 行クリックで個別詳細モーダル |
| **メニュー別** | 期間/並び/検索 + 5スコープ (綱島/宮古島合算+部門3) + 前月比 |
| **年度レポート** | FY22-26 成長推移 (KPI + 売上 stacked + 客数/客単価/部門推移) + 2年度比較 |
| **マネジメント** | 全社KPI + 売上構成 + ライフサイクル + 24ヶ月推移 + ベンチマーク + 生産性 + 外部メディア |
| **顧客分析** | 失客 / 曜日別 / 年代別 / 再来店 (情報分析 scrape データ) |
| **採用** | 候補者管理 (本部追加・編集・削除可) + ファネル + ソース |

## 自動化 (launchd)

| ジョブ | 時刻 | 内容 |
|---|---|---|
| `com.hanabi-board.daily` | 毎朝 8:15 JST | `deploy_auto.sh`: pull → Uレジ DL → メニュー scrape → generate.py → push |
| `com.hanabi-board.monthly-jouhou` | 毎月 1日 9:30 JST | `monthly_jouhou.sh`: 顧客分析4種 + HotPepper + Instagram |
| `com.hanabi-board.weekly-backup` | 毎週日曜 6:00 JST | `backup.sh`: data/ を `~/Documents/HANABI_backup/` にスナップショット |

## 📅 運用カレンダー (人間がやること)

### 毎日 (自動、 確認のみ)
- 朝 8:15 の auto-deploy が成功してるか (通知なければOK)
- ダッシュボードで前日数字確認

### 毎週 (任意)
- 採用候補者の進捗確認 (麗花さん)
- スタッフ実績ランキング ざっと確認

### 毎月 (月初1-3日)
- 前月確定値の反映確認 (auto-deploy が月初日に前月分を補完取得済)
- 月予算達成率 / 同店舗YoYなどの月末レビュー
- スタッフ入退社あれば プロフィール更新 (採用「入社」→ プロフィール登録 ボタン)

### 四半期 (3ヶ月毎)
- HotPepper評価/Instagram フォロワー の推移確認 (月次自動取得)
- 予算 vs 実績の中間レビュー

### **年度切替 (年1回、 3月-4月)** ⭐ 最重要
- **FY27予算入力** (期初 5/1 までに):
  - マネジメントタブ → **「予算編集」** ボタン (本部のみ)
  - 12ヶ月分 × 2店舗 の月予算
  - + 宮古島 部門別予算 (ヘア/アイ/ネイル)
  - 年間合計は自動計算
  - **GitHub に直接保存** されるので git 操作不要
- 前年度退職者の確認 + 必要なら retired 日付登録

### 不定期 (必要時)
- **GitHub PAT 更新** (1年に1回、 期限90日前に通知 推奨):
  - 水野さんが Settings → Developer settings → Personal access tokens → 新規発行 (`repo` スコープ)
  - 麗花さんに新トークン送付
  - 麗花さんは ヘッダー「GH」→ 新トークンに更新
- **新店舗追加** (出店時、 別途相談):
  - STORES config (`scripts/generate.py`) に追加
  - open_date 設定
  - 予算データ入力
  - Uレジ scrape スクリプトの対応店舗追加
  - HPB/Instagram URL 登録
- **新スタッフ追加** (採用後):
  - 採用タブで「入社」ステータス → 「→ プロフィール登録」 ボタン
  - 写真は ダッシュボードUIから直接アップロード可
- **スタッフ退職** (退職時):
  - スタッフ実績タブ → ✎ → 退職日入力 → 保存
  - 退職月の翌月から自動非表示

## 動作保証

| 状況 | 動作 |
|---|---|
| 月初日 (例: 6/1) | 自動デプロイで 5月最終日+6月初日 両方取得 ✓ |
| 年度切替 (5/1) | 新FYを自動検出、 年度レポートも自動更新 ✓ |
| **新FY予算未入力** | 月達成率が 0% 表示 ← 期初までに予算入力推奨 |
| Uレジ 一時障害 | scrape 失敗時は `\|\| log` で続行、 翌朝再試行 |
| 月次scrape 失敗 | 通知 + 翌月1日に再試行 (もしくは手動で `python3 scripts/scrape_jouhou.py xxx`) |

## GitHub 直接書き込み (本部のみ)

本部権限で以下のフィールドはダッシュボードから直接 GitHub に push できる:
- `data/recruitment.json` (候補者追加・編集・削除)
- `data/budgets.json` (月別予算編集)
- `data/staff_profiles.json` (スタッフ情報追加・編集)

設定: ヘッダー「GH」 ボタン → Personal Access Token (PAT) 入力 (1回のみ)。

**PAT 運用ルール**:
- 期限: **1年** (次回更新: トークン発行日から1年後)
- スコープ: `repo` のみ
- 漏洩疑い時は GitHub Settings → Personal access tokens から即 Revoke
- 期限切れ前に新トークン発行 → 麗花さんに再共有 → ダッシュボードの「GH」 ボタンから更新

## 達成率の表記ルール

| 種類 | 計算式 | 用途 |
|---|---|---|
| **月達成率** | 当月実績 / 月予算 | 期末判断 |
| **ペース達成率** | 当月実績 / (月予算 × 経過日数/月日数) | 月途中の判断 |
| **前年同月比 (同店舗)** | 当月実績 / (前年同月実績 × 経過日数/月日数) | 成長率 (開店12ヶ月以上の店舗のみ対象) |
| バッジ色 | 緑≥100% / 黄80-99% / 赤<80% | |

## 設計方針

### 全社 vs 店舗別 の分離原則
業態違い (綱島=ヘア専門 / 宮古島=トータルビューティ) のため、 平均が混ざる比率指標は店舗別で表示:
- **全社合算 OK**: 売上、 客数、 合計値などビジネス意味のある合算
- **店舗別のみ**: 客単価、 指名率、 リピート率、 男女比、 部門別

### 部門色
- ヘア = ピンク `#E91E63`
- アイ = ブルー `#1E88E5`
- ネイル = イエロー `#F59E0B`

### 同店舗 YoY
店舗の `open_date` から12ヶ月以上経過した場合のみ同店舗扱い。 ELLE宮古島 (2025/9開店) は 2026/9 以降に同店舗化。

### 水野陽平の扱い
スタッフ店販購入を売上計上しているため `HIDDEN_STAFF_NAMES` に登録。 集計には含まれるが、 ランキング・スタッフ表示からは除外。

## キーボードショートカット

- **Cmd+K** (Mac) / **Ctrl+K** (Win): 全画面検索 (タブ/店舗/スタッフ/メニュー/管理動作)
- **Esc**: モーダルを閉じる
- **印刷**: ヘッダー印刷ボタン → サマリー + 売上・予算 の A4 2枚 PDF

## データ拡張スクリプト

| スクリプト | 用途 | 実行例 |
|---|---|---|
| `auto_download.py` | 売上CSV取得 | `python3 scripts/auto_download.py 202605` |
| `backfill.py` | 複数月の一括取得 | `python3 scripts/backfill.py 202205 202504` |
| `scrape_menu.py` | メニュー別実績 | `python3 scripts/scrape_menu.py FY25_MONTHLY` |
| `scrape_jouhou.py` | 情報分析 (失客/曜日別/年代別) | `python3 scripts/scrape_jouhou.py week 202605` |
| `scrape_external.py` | HotPepper/Instagram | `python3 scripts/scrape_external.py all` |
| `scrape_denpyo.py` | 伝票明細 | `python3 scripts/scrape_denpyo.py 202605` |

## 出典

- FY26 予算: Box `050_HANABI/010_運営本部/HABABI_FY26予実管理表.xlsx`
- 認証情報: `.env` (gitignore済、 Uレジログイン用)
- GitHub repo: https://github.com/hanabi-board/hanabi-dashboard
