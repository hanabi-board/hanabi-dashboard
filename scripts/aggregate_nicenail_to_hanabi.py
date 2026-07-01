#!/usr/bin/env python3
"""
ナイスネイル SC明細 → HANABI 形式 (Uレジ風) に集約変換するスクリプト

入力: /Users/yoheimizuno/salon-dashboard/data/meisai_<店舗名>.csv (Shift-JIS, 取引明細レベル)
出力 (HANABI 形式):
  - data/daily_sales_YYYYMM_<store_id>.csv   (Uレジ風 日別売上、 Shift-JIS)
  - data/staff_ranking_YYYYMM_<store_id>.csv (Uレジ風 スタッフ別月次、 Shift-JIS)
  - data/menu_YYYYMM_<store_id>.json         (HANABI メニュー別 JSON)
  - data/nicenail_extras_YYYYMM_<store_id>.json (NN固有: OP率/稼働率/1日1名等)

使い方:
  python3 scripts/aggregate_nicenail_to_hanabi.py            # 当月分
  python3 scripts/aggregate_nicenail_to_hanabi.py 202606     # 指定月

設計:
- meisai CSV を 1取引=1行 で読み込み (現状 SC明細形式)
- 同一会計IDの複数行は「1来店」として扱う
- 売上 = 金額合計。 ⚠️ salon-dashboard の明細/monthlyRecords は【税込】。 HANABI は全店【税抜】表示のため、
  取込時に _to_zeinuki() で ÷1.1 して税抜へ統一する (dist経路・明細経路の両方)。
- 客数 = unique 会計ID
- 指名数 = unique 会計ID where 指名="指名あり"
- OP率 = (オプション付き来店数) / 全来店数
- カテゴリ判定: admin_config.json の menu_categories キーワードで分類
"""

import sys
import csv
import json
import re
import os
from pathlib import Path
from datetime import date, datetime
from collections import defaultdict

# === 設定 ===
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
NICENAIL_DATA = Path("/Users/yoheimizuno/salon-dashboard/data")
SALON_DIST = Path("/Users/yoheimizuno/salon-dashboard/dist/index.html")

# ナイスネイル店舗名 → HANABI 店舗ID
NICENAIL_TO_HANABI = {
    "新横浜": "shinyokohama",
}

# 除外メニュー (カウント・売上計算から外す)
EXCLUDE_MENUS = {
    "(削除済みメニュー)",
    "キャンセル料",
    "指名",
}

# 集計対象外: 「削除済みメニュー」の中でも特殊扱い (会計IDから完全除外)
# 今は シンプルに 区分=会計以外 を除外
EXCLUDE_KUBUN = {"返金"}


def load_admin_config():
    """admin_config.json からメニューカテゴリ等を読み込む"""
    p = DATA / "admin_config.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def classify_menu(menu_name: str, categories: list) -> str:
    """メニュー名を カテゴリに分類"""
    for cat in categories:
        for kw in cat.get("keywords", []):
            if cat.get("type") == "contains":
                if kw in menu_name:
                    return cat["name"]
            elif cat.get("type") == "exact":
                if kw == menu_name:
                    return cat["name"]
    return "未分類"


def is_salon_dist_fresh() -> bool:
    """salon-dashboard/dist/index.html が「今日更新済」 か判定。

    2026-06-17 追加 (案2 鮮度ガード):
    salon (NICENAIL) の auto-deploy が当日まだ完了していない (= dist が前日以前のまま) 状態で
    HANABI が読むと、 新横浜の数字が古いまま焼き込まれる事故を検知するため。
    （朝 salon がログイン不調等で遅延すると、 HANABI 8:30 が salon 完了前に走るケースがある）

    判定: dist のファイル更新日 == 今日 なら fresh。
          dist は salon パイプラインの最後 (download → generate_html) で書かれるため、
          dist が今日付なら meisai CSV も dist も最新化済とみなせる。
    """
    if not SALON_DIST.exists():
        return False
    try:
        mtime_date = datetime.fromtimestamp(SALON_DIST.stat().st_mtime).date()
        return mtime_date == datetime.now().date()
    except Exception:
        return False


def count_options(menu_name: str, option_keywords: list) -> int:
    """メニュー名内のオプションキーワード出現数を返す (素朴版、 後方互換用)"""
    count = 0
    for kw in option_keywords:
        if kw in menu_name:
            count += 1
    return count


def count_options_proper(menu_name: str, date_yyyymmdd: str,
                         option_keywords: list, aliases: dict,
                         exclude_after: dict) -> int:
    """NICENAIL ダッシュボードと一致する OP 数計算 (alias 統合 + 日付別除外 対応)
    例: 'FUJIE' → alias で 'FUJIジェル' に統合、 2025-05-01 以降は除外
    """
    matched = set()
    for kw in option_keywords:
        if kw not in menu_name:
            continue
        cutoff = exclude_after.get(kw)
        if cutoff and date_yyyymmdd and date_yyyymmdd >= cutoff:
            continue
        canonical = aliases.get(kw, kw)
        matched.add(canonical)
    return len(matched)


def is_tenhan(menu_name: str, tenhan_keywords: list) -> bool:
    """メニュー名が 店販キーワード (admin_config) に部分一致するか"""
    return any(kw in menu_name for kw in tenhan_keywords)


def get_target_ym() -> str:
    """対象YYYYMM (引数 or 今月)"""
    if len(sys.argv) > 1 and re.match(r"^\d{6}$", sys.argv[1]):
        return sys.argv[1]
    today = date.today()
    return f"{today.year}{today.month:02d}"


def read_meisai(store_name_jp: str, ym: str) -> list[dict]:
    """ナイスネイル meisai CSV を読み込んで対象YMの取引行リストを返す。

    🚨 2026-06-02 修正: history + 当月版 両方読んで merge (会計ID で dedupe)
       理由:
       - history は 月初に salon-dashboard の monthly_append.py が前月分追記 (月1回)
       - 当月進行中データは 当月版 (毎朝更新)
       - つまり 当月選択時は 当月版を見ないと 当月分が見えない
       - 月末選択時は history が完全 (当月版が空上書きされる事故もある)
       - 両方読んで会計IDで重複排除すれば 全期間の真実が取れる

    優先順位:
    1. 当月版 (進行中の最新データ、 当月分のみ)
    2. history (確定済の過去月分、 完全な累積)
    """
    history_path = NICENAIL_DATA / f"meisai_{store_name_jp}_history.csv"
    current_path = NICENAIL_DATA / f"meisai_{store_name_jp}.csv"

    rows_by_key = {}  # 会計ID::行内ハッシュ → row (重複排除キー)
    sources = []

    for path, label in [(current_path, "当月版"), (history_path, "累積版")]:
        if not path.exists():
            continue
        try:
            with open(path, encoding="shift_jis", errors="replace") as f:
                reader = csv.DictReader(f)
                count = 0
                for r in reader:
                    d = r.get("会計日", "").strip()
                    if not d or len(d) < 6:
                        continue
                    # YYYYMMDD → match YYYYMM
                    if d[:6] != ym:
                        continue
                    kubun = r.get("区分", "").strip()
                    menu = r.get("メニュー・店販・割引・サービス・オプション", "").strip()
                    if menu in EXCLUDE_MENUS:
                        continue
                    # 重複排除キー: 会計ID + メニュー名 + 金額 (同一会計ID 内の複数行も保持)
                    kid = r.get("会計ID", "")
                    dedup_key = f"{kid}::{menu}::{r.get('金額', '0')}"
                    if dedup_key in rows_by_key:
                        continue
                    rows_by_key[dedup_key] = {
                        "date": d,
                        "time": r.get("会計時間", ""),
                        "kaikei_id": kid,
                        "kubun": kubun,
                        "category": r.get("カテゴリ", "").strip(),
                        "menu": menu,
                        "unit_price": _to_zeinuki(_to_int(r.get("単価", "0"))),
                        "qty": _to_int(r.get("個数", "1")),
                        "amount": _to_zeinuki(_to_int(r.get("金額", "0"))),  # 税込→税抜 (HANABI統一)
                        "staff": r.get("スタッフ", "").strip(),
                        "shimei": r.get("指名", "").strip(),
                        "new_or_repeat": r.get("新規再来", "").strip(),
                    }
                    count += 1
                if count > 0:
                    sources.append(f"{path.name} ({label}: {count}件)")
        except Exception as e:
            print(f"  ⚠️ read failed {path.name}: {e}", file=sys.stderr)

    rows = list(rows_by_key.values())
    if sources:
        print(f"  source: {' + '.join(sources)}")
    elif not history_path.exists() and not current_path.exists():
        print(f"  ⚠️ meisai CSV not found: {history_path.name} / {current_path.name}", file=sys.stderr)
    return rows


def _to_int(s: str) -> int:
    if not s:
        return 0
    s = str(s).replace(",", "").replace("¥", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return 0


def _to_zeinuki(v: int) -> int:
    """税込(int) → 税抜(int)。
    salon-dashboard の明細(meisai)/monthlyRecords は税込。 HANABI は全店 税抜 表示なので、
    新横浜(ナイスネイル由来)の金額を ÷1.1 して税抜に統一する。
    salon-dashboard 側の税抜ロジック Math.round(v/1.1) と同一 (template.html の _ex)。"""
    return round((v or 0) / 1.1)


def aggregate_per_kaikei(rows: list[dict]) -> dict:
    """会計ID単位で集約。 {kaikei_id: {date, staff, sales, options, shimei, new}}

    2026-06-08 追加: NICENAIL ダッシュボードと完全一致させるため、 ランキング表示用の
    厳密版フィールドも併設:
    - true_tenhan_sales: tenhan_keywords にマッチした金額のみ (素朴 shop_sales と区別)
    - options_proper: option_keyword_aliases / exclude_after も反映した OP 数
    """
    cfg = load_admin_config()
    options_keywords = cfg.get("option_keywords", [])
    options_aliases = cfg.get("option_keyword_aliases", {})
    options_exclude_after = cfg.get("option_keyword_exclude_after", {})
    tenhan_keywords = cfg.get("tenhan_keywords", [])
    kaikei = {}
    for r in rows:
        kid = r["kaikei_id"]
        if not kid:
            continue
        if kid not in kaikei:
            kaikei[kid] = {
                "date": r["date"],
                "staff": r["staff"],
                "sales": 0,
                "tech_sales": 0,
                "shop_sales": 0,            # 区分 != 施術 全部 (= 後方互換、 daily_sales 合計用)
                "true_tenhan_sales": 0,     # ★ tenhan_keywords マッチのみ (ランキング表示用)
                "options": 0,               # 素朴 OP 数 (後方互換)
                "options_proper": 0,        # ★ alias 統合 + 日付別除外 反映 (ランキング表示用)
                "shimei": r["shimei"],
                "new_or_repeat": r["new_or_repeat"],
                "menu_items": [],
            }
        kaikei[kid]["sales"] += r["amount"]
        if r["kubun"] == "施術":
            kaikei[kid]["tech_sales"] += r["amount"]
        else:
            kaikei[kid]["shop_sales"] += r["amount"]
        # ★ NICENAIL 一致: kubun と独立して、 メニュー名が店販キーワード一致なら true_tenhan_sales に加算
        # SC明細では店販商品 (ラッシュアディクト/ラダメール/N3ガラクナイアシン等) も
        # 区分=施術 で記録されているため、 区分ではなくメニュー名で判定する必要がある。
        if is_tenhan(r["menu"], tenhan_keywords):
            kaikei[kid]["true_tenhan_sales"] += r["amount"]
        kaikei[kid]["options"] += count_options(r["menu"], options_keywords)
        # ★ NICENAIL 一致: alias + exclude_after 適用版
        kaikei[kid]["options_proper"] += count_options_proper(
            r["menu"], r["date"], options_keywords, options_aliases, options_exclude_after
        )
        kaikei[kid]["menu_items"].append(r["menu"])
        # スタッフ・指名は最初の施術行を採用
        if not kaikei[kid].get("staff") or kaikei[kid]["staff"] == "":
            kaikei[kid]["staff"] = r["staff"]
    return kaikei


def build_daily_sales_csv(kaikei: dict, ym: str) -> list[list]:
    """日別売上 CSV (Uレジ風) を構築。 Shift-JIS で保存される"""
    # 日別集計
    daily = defaultdict(lambda: {
        "new": 0, "repeat": 0, "nominated": 0, "customers": 0,
        "sales": 0, "tech_sales": 0, "shop_sales": 0,
    })
    for kid, v in kaikei.items():
        d = v["date"]
        bucket = daily[d]
        bucket["customers"] += 1
        if v["new_or_repeat"] == "新規":
            bucket["new"] += 1
        else:
            bucket["repeat"] += 1
        if v["shimei"] == "指名あり":
            bucket["nominated"] += 1
        bucket["sales"] += v["sales"]
        bucket["tech_sales"] += v["tech_sales"]
        bucket["shop_sales"] += v["shop_sales"]

    # Uレジ風 ヘッダー (17列)
    headers = [
        "日付", "曜日", "新規", "リピート", "紹介", "指名", "客数",
        "客数 目標", "客数 達成率", "グランドメニュー売上", "クーポン売上", "その他売上",
        "合計売上", "売上 目標", "売上 達成率", "客単価", "次月予約数"
    ]
    rows = [headers]
    weekdays_jp = ["月", "火", "水", "木", "金", "土", "日"]
    yyyy, mm = int(ym[:4]), int(ym[4:6])
    # ループは月の全日 (0埋め)
    import calendar
    last_day = calendar.monthrange(yyyy, mm)[1]
    totals = defaultdict(int)
    for day in range(1, last_day + 1):
        d_key = f"{yyyy:04d}{mm:02d}{day:02d}"
        b = daily.get(d_key, {"new": 0, "repeat": 0, "nominated": 0,
                              "customers": 0, "sales": 0, "tech_sales": 0, "shop_sales": 0})
        wd = weekdays_jp[date(yyyy, mm, day).weekday()]
        spend = b["sales"] // b["customers"] if b["customers"] else 0
        rows.append([
            f"{mm:02d}/{day:02d}", wd,
            b["new"], b["repeat"], 0, b["nominated"], b["customers"],
            0, "0%",
            b["tech_sales"], 0, b["shop_sales"],  # コラム9-11: メニュー区分は簡略化 (技術=グランド、 店販=その他)
            b["sales"], 0, "0%", spend, 0
        ])
        # totals
        totals["new"] += b["new"]
        totals["repeat"] += b["repeat"]
        totals["nominated"] += b["nominated"]
        totals["customers"] += b["customers"]
        totals["sales"] += b["sales"]
        totals["tech_sales"] += b["tech_sales"]
        totals["shop_sales"] += b["shop_sales"]
    # TOTAL 行
    total_spend = totals["sales"] // totals["customers"] if totals["customers"] else 0
    rows.append([
        "TOTAL", "",
        totals["new"], totals["repeat"], 0, totals["nominated"], totals["customers"],
        0, "0%",
        totals["tech_sales"], 0, totals["shop_sales"],
        totals["sales"], 0, "0%", total_spend, 0
    ])
    return rows


def build_staff_ranking_csv_from_dist(target_ym: str, store_name_full: str, store_filter_name: str = "新横浜店"):
    """[NEW 2026-06-08] salon-dashboard/dist/index.html の monthlyRecords から
    スタッフランキング CSV を構築。 NICENAIL ダッシュボード表示値と完全一致。

    動作: dist/index.html の window.monthlyRecords を読み、
          指定月・指定店舗の visits を staff 別に集約。

    返り値: 既存 build_staff_ranking_csv と同じ rows (header + データ行)。
            データ取得失敗時は None を返す (呼び出し側で旧版にフォールバック)。
    """
    DIST_HTML = Path("/Users/yoheimizuno/salon-dashboard/dist/index.html")
    if not DIST_HTML.exists():
        print(f"  ℹ️ dist 直読み スキップ: {DIST_HTML} 不存在", file=sys.stderr)
        return None
    try:
        html = DIST_HTML.read_text(encoding="utf-8")
        m = re.search(r"window\.monthlyRecords\s*=\s*(\{.*?\});", html, re.DOTALL)
        if not m:
            print(f"  ⚠️ dist から monthlyRecords 抽出失敗", file=sys.stderr)
            return None
        mr = json.loads(m.group(1))
        ym_key = f"{target_ym[:4]}-{target_ym[4:6]}"
        records = mr.get(ym_key, [])
        # 店舗 + キャンセル除外
        records = [r for r in records
                   if r.get("store") == store_filter_name
                   and not r.get("is_cancel_only")]
        if not records:
            print(f"  ℹ️ dist 直読み: {ym_key} {store_filter_name} レコードなし", file=sys.stderr)
            return None
    except Exception as e:
        print(f"  ⚠️ dist 直読み 失敗: {e}", file=sys.stderr)
        return None

    # スタッフ別集計
    staff = defaultdict(lambda: {
        "work_dates": set(),
        "customers": 0,        # visit count
        "sales": 0,            # amount 合計
        "tenhan_sales": 0,     # NICENAIL の 店販値 (= tenhan フィールド合計)
        "tech_sales": 0,       # = sales - tenhan_sales (= 技術+OP+割引、 = 店販以外)
        "nominated": 0,        # 指名あり 件数
        "nominated_sales": 0,  # 指名あり の amount 合計
        "options": 0,          # OP 数合計 (NICENAIL 同じロジック)
        "nail_sales": 0,       # ネイル系売上 (新横浜は全部ネイルなので tech_sales 全部)
    })
    for r in records:
        name = r.get("staff", "")
        if not name:
            continue
        s = staff[name]
        s["work_dates"].add(r.get("date", ""))
        s["customers"] += 1
        amount = r.get("amount", 0) or 0
        tenhan = r.get("tenhan", 0) or 0
        s["sales"] += amount
        s["tenhan_sales"] += tenhan
        s["tech_sales"] += (amount - tenhan)
        s["nail_sales"] += (amount - tenhan)  # 新横浜は全部ネイル
        if r.get("meimei") == "指名あり":
            s["nominated"] += 1
            s["nominated_sales"] += amount
        s["options"] += (r.get("options", 0) or 0)

    # 税抜化: dist の monthlyRecords amount は税込。 HANABI は全店 税抜 表示なので、
    # スタッフ別に集計した金額フィールドを ÷1.1 して税抜へ統一する
    # (salon-dashboard 自身の税抜ロジック Math.round(total/1.1) と同じ)。
    for s in staff.values():
        for _k in ("sales", "tenhan_sales", "tech_sales", "nominated_sales", "nail_sales"):
            s[_k] = _to_zeinuki(s[_k])

    # ヘッダー (既存 build_staff_ranking_csv と完全一致)
    headers = [
        "店舗名", "担当者分類", "スタッフ名", "稼働日数",
        "総売上", "客数", "客単価",
        "技術売上", "技術客数", "技術客単価",
        "技術売上（指名）", "技術客数（指名）", "技術客単価（指名）",
        "技術売上（フリー）", "技術客数（フリー）", "技術客単価（フリー）",
        "技術売上（男）", "技術客数（男）", "技術客単価（男）",
        "技術売上（女）", "技術客数（女）", "技術客単価（女）",
        "店販売上", "店販客数", "店販比率", "購買比率",
        "(ネイル)ジェル",
        "_nn_op_count", "_nn_op_rate", "_nn_work_days",
    ]
    rows = [headers]
    for name, s in sorted(staff.items(), key=lambda kv: -kv[1]["sales"]):
        if not name:
            continue
        sales = s["sales"]
        cust = s["customers"]
        spc = sales // cust if cust else 0
        tech_spc = s["tech_sales"] // cust if cust else 0
        free_sales = s["tech_sales"] - s["nominated_sales"]
        # nominated_sales には店販分も含まれてるので 技術売上ベースに変換
        # ただし簡略化: 指名売上 ≒ 指名 visits の amount 全部 (店販含む)
        # 必要なら後で正確化。 現状 NICENAIL 表示と同等を目指す。
        nominated_sales = s["nominated_sales"]
        free_customers = cust - s["nominated"]
        nominated_spc = nominated_sales // s["nominated"] if s["nominated"] else 0
        free_spc = free_sales // free_customers if free_customers else 0
        shop_pct = (s["tenhan_sales"] / sales * 100) if sales else 0
        op_rate = (s["options"] / cust * 100) if cust else 0
        rows.append([
            store_name_full, "", name, len(s["work_dates"]),
            sales, cust, spc,
            s["tech_sales"], cust, tech_spc,
            nominated_sales, s["nominated"], nominated_spc,
            free_sales, free_customers, free_spc,
            0, 0, 0,
            0, 0, 0,
            s["tenhan_sales"], 0, f"{shop_pct:.2f}%", "0%",   # ★ NICENAIL 完全一致
            s["nail_sales"],
            s["options"], f"{op_rate:.1f}%", len(s["work_dates"]),  # ★ NICENAIL 完全一致
        ])
    return rows


def build_staff_ranking_csv(kaikei: dict, store_name_full: str) -> list[list]:
    """スタッフ別 月次 CSV (Uレジ風) を構築 (旧版: CSV ベース、 dist 取得失敗時のフォールバック)"""
    # スタッフ別集計
    staff = defaultdict(lambda: {
        "work_dates": set(),  # 出勤日数 (会計があった日)
        "customers": 0,
        "sales": 0,
        "tech_sales": 0,
        "shop_sales": 0,
        "true_tenhan_sales": 0,   # ★ NICENAIL 一致: 店販キーワードマッチのみ
        "nominated": 0,
        "options": 0,             # OP数 素朴版 (後方互換)
        "options_proper": 0,      # ★ NICENAIL 一致: alias + exclude_after 反映
        "nail_sales": 0,          # (ネイル)カテゴリ合算
    })
    for kid, v in kaikei.items():
        if not v["staff"]:
            continue
        s = staff[v["staff"]]
        s["work_dates"].add(v["date"])
        s["customers"] += 1
        s["sales"] += v["sales"]
        s["tech_sales"] += v["tech_sales"]
        s["shop_sales"] += v["shop_sales"]
        s["true_tenhan_sales"] += v.get("true_tenhan_sales", 0)
        if v["shimei"] == "指名あり":
            s["nominated"] += 1
        s["options"] += v["options"]
        s["options_proper"] += v.get("options_proper", 0)
        # ネイル系は全部 (ネイル) カテゴリにマップ
        s["nail_sales"] += v["tech_sales"]

    # ヘッダー: HANABI staff_ranking と同じカラム順 + 末尾に NN拡張 (op_count, op_rate)
    headers = [
        "店舗名", "担当者分類", "スタッフ名", "稼働日数",
        "総売上", "客数", "客単価",
        "技術売上", "技術客数", "技術客単価",
        "技術売上（指名）", "技術客数（指名）", "技術客単価（指名）",
        "技術売上（フリー）", "技術客数（フリー）", "技術客単価（フリー）",
        "技術売上（男）", "技術客数（男）", "技術客単価（男）",
        "技術売上（女）", "技術客数（女）", "技術客単価（女）",
        "店販売上", "店販客数", "店販比率", "購買比率",
        "(ネイル)ジェル",  # = nail_sales (とりあえず全部ジェルに入れる、 詳細はメニュー別側で表示)
        # NN拡張カラム (HANABI generate.py 互換のため最後に追加)
        "_nn_op_count", "_nn_op_rate", "_nn_work_days",
    ]
    rows = [headers]
    for name, s in sorted(staff.items(), key=lambda kv: -kv[1]["sales"]):
        if not name:
            continue
        spc = s["sales"] // s["customers"] if s["customers"] else 0
        tech_spc = s["tech_sales"] // s["customers"] if s["customers"] else 0
        nominated_sales = sum(v["tech_sales"] for v in kaikei.values()
                              if v["staff"] == name and v["shimei"] == "指名あり")
        free_sales = s["tech_sales"] - nominated_sales
        free_customers = s["customers"] - s["nominated"]
        nominated_spc = nominated_sales // s["nominated"] if s["nominated"] else 0
        free_spc = free_sales // free_customers if free_customers else 0
        # ★ NICENAIL 一致: ランキング表示の 店販売上 / 店販比率 / OP数 / OP率 は厳密版を使う
        # (= スタッフ成績ランキングだけ NICENAIL ダッシュボードと完全一致、
        #   全社売上・店舗合計売上などの集計には影響しない)
        shop_sales_display = s["true_tenhan_sales"]
        op_count_display = s["options_proper"]
        shop_pct = (shop_sales_display / s["sales"] * 100) if s["sales"] else 0
        op_rate = (op_count_display / s["customers"] * 100) if s["customers"] else 0
        rows.append([
            store_name_full, "", name, len(s["work_dates"]),
            s["sales"], s["customers"], spc,
            s["tech_sales"], s["customers"], tech_spc,
            nominated_sales, s["nominated"], nominated_spc,
            free_sales, free_customers, free_spc,
            0, 0, 0,  # 男 (SC明細に性別なし)
            0, 0, 0,  # 女
            shop_sales_display, 0, f"{shop_pct:.2f}%", "0%",  # ★ 店販キーワードマッチのみ
            s["nail_sales"],
            op_count_display, f"{op_rate:.1f}%", len(s["work_dates"]),  # ★ alias + exclude_after 反映
        ])
    return rows


def build_menu_json(kaikei: dict, rows: list[dict]) -> list[dict]:
    """メニュー別 JSON を構築 (HANABI menu_*.json 互換)"""
    categories = load_admin_config().get("menu_categories", [])
    menu_agg = defaultdict(lambda: {"count": 0, "total_price": 0, "category": "未分類"})
    for r in rows:
        m = r["menu"]
        amt = r["amount"]
        qty = r["qty"]
        menu_agg[m]["count"] += qty
        menu_agg[m]["total_price"] += amt
        menu_agg[m]["category"] = classify_menu(m, categories)
    out = []
    for menu_name, v in sorted(menu_agg.items(), key=lambda kv: -kv[1]["total_price"]):
        if v["count"] == 0:
            continue
        unit_price = v["total_price"] // v["count"] if v["count"] else 0
        out.append({
            "commodity_name": menu_name,
            "count": v["count"],
            "total_price": v["total_price"],
            "unit_price": unit_price,
            # HANABI menu format expects category_status_name (Uレジカラム名)
            "category_status_name": v["category"],
            "category": v["category"],  # 後方互換 + 検索用
            "year_on_year_price": "",
            "year_on_year_count": "",
        })
    return out


def build_extras_json(kaikei: dict, ym: str) -> dict:
    """NN固有 KPI (店舗レベル): 稼働率, 1日1名売上, OP率"""
    if not kaikei:
        return {"sales": 0, "customers": 0, "op_count": 0, "op_rate": 0,
                "daily_per_staff": 0, "kadou_rate": 0,
                "active_days": 0, "staff_days": 0}
    total_sales = sum(v["sales"] for v in kaikei.values())
    total_customers = len(kaikei)
    total_options = sum(v["options"] for v in kaikei.values())
    # 延べ稼働ペア (staff, date)
    staff_days = set()
    for v in kaikei.values():
        if v["staff"]:
            staff_days.add((v["staff"], v["date"]))
    # 実際の営業日数
    active_days = set(v["date"] for v in kaikei.values())
    # 1日1名売上 = 売上 / staff_days
    daily_per_staff = total_sales / len(staff_days) if staff_days else 0
    # 稼働率 (NN式) = 来客数 / staff_days = 1日1名あたり来客数
    kadou_rate = total_customers / len(staff_days) if staff_days else 0
    # OP率 = OP数 / 来客数 * 100 (%)
    op_rate = total_options / total_customers * 100 if total_customers else 0
    return {
        "sales": total_sales,
        "customers": total_customers,
        "op_count": total_options,
        "op_rate": round(op_rate, 1),
        "daily_per_staff": int(daily_per_staff),
        "kadou_rate": round(kadou_rate, 2),
        "active_days": len(active_days),
        "staff_days": len(staff_days),
        "ym": ym,
    }


def write_csv_sjis(path: Path, rows: list[list]):
    """CSV を Shift-JIS で保存"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="shift_jis", errors="replace", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerows(rows)


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ym = get_target_ym()
    print(f"=== ナイスネイル → HANABI 集約変換 (対象月: {ym}) ===")
    # 案2 鮮度ガード: salon dist が本日未更新なら 警告 (= 新横浜が古い可能性、 案3 が後で自己修復)
    salon_fresh = is_salon_dist_fresh()
    if not salon_fresh:
        print("  ⚠️ salon dist が本日未更新です (= NICENAIL の auto-deploy がまだ完了していない可能性)")
        print("     → 新横浜の数字は前日のままになる場合があります。 selfheal.sh が salon 完了後に自動修復します。")
    total_processed = 0
    for store_name_jp, hanabi_id in NICENAIL_TO_HANABI.items():
        store_name_full = "ナイスネイル 新横浜店"  # ハードコード (1店舗のみ想定)
        print(f"\n→ {store_name_jp} → {hanabi_id}")
        rows = read_meisai(store_name_jp, ym)
        if not rows:
            print(f"  ℹ️ meisai データなし - スキップ ({store_name_jp})")
            continue
        print(f"  ✓ meisai 取引行: {len(rows)}")
        kaikei = aggregate_per_kaikei(rows)
        print(f"  ✓ 会計ID集約: {len(kaikei)}")

        # daily_sales CSV
        daily_csv = build_daily_sales_csv(kaikei, ym)
        write_csv_sjis(DATA / f"daily_sales_{ym}_{hanabi_id}.csv", daily_csv)
        print(f"  ✓ daily_sales_{ym}_{hanabi_id}.csv")

        # staff_ranking CSV (★ NICENAIL ダッシュボード完全一致版を優先試行、 失敗時は旧版にフォールバック)
        staff_csv = build_staff_ranking_csv_from_dist(ym, store_name_full, store_filter_name=f"{store_name_jp}店")
        if staff_csv is None:
            print(f"  ↩ dist 直読み 利用不可 → meisai CSV ベースに フォールバック")
            staff_csv = build_staff_ranking_csv(kaikei, store_name_full)
        else:
            print(f"  ✨ dist 直読み 利用 (NICENAIL 値と完全一致)")
        write_csv_sjis(DATA / f"staff_ranking_{ym}_{hanabi_id}.csv", staff_csv)
        print(f"  ✓ staff_ranking_{ym}_{hanabi_id}.csv ({len(staff_csv)-1} staff)")

        # menu JSON (HANABI menu format: { label, store_id, rows })
        menu_rows = build_menu_json(kaikei, rows)
        menu_wrapper = {
            "label": ym,
            "store_id": hanabi_id,
            "period_start": f"{ym}01",
            "rows": menu_rows,
            "source": "nicenail_meisai",
        }
        write_json(DATA / f"menu_{ym}_{hanabi_id}.json", menu_wrapper)
        print(f"  ✓ menu_{ym}_{hanabi_id}.json ({len(menu_rows)} menus)")

        # NN拡張 KPI JSON
        extras = build_extras_json(kaikei, ym)
        write_json(DATA / f"nicenail_extras_{ym}_{hanabi_id}.json", extras)
        print(f"  ✓ nicenail_extras_{ym}_{hanabi_id}.json (op_rate={extras['op_rate']}%, kadou={extras['kadou_rate']}人/日)")
        total_processed += 1
    print(f"\n=== 完了: {total_processed} 店舗 ===")


if __name__ == "__main__":
    main()
