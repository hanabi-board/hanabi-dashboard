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
- 売上 = 金額合計 (税抜は SC 側で既に税抜出力)
- 客数 = unique 会計ID
- 指名数 = unique 会計ID where 指名="指名あり"
- OP率 = (オプション付き来店数) / 全来店数
- カテゴリ判定: admin_config.json の menu_categories キーワードで分類
"""

import sys
import csv
import json
import re
from pathlib import Path
from datetime import date
from collections import defaultdict

# === 設定 ===
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
NICENAIL_DATA = Path("/Users/yoheimizuno/salon-dashboard/data")

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


def count_options(menu_name: str, option_keywords: list) -> int:
    """メニュー名内のオプションキーワード出現数を返す"""
    count = 0
    for kw in option_keywords:
        if kw in menu_name:
            count += 1
    return count


def get_target_ym() -> str:
    """対象YYYYMM (引数 or 今月)"""
    if len(sys.argv) > 1 and re.match(r"^\d{6}$", sys.argv[1]):
        return sys.argv[1]
    today = date.today()
    return f"{today.year}{today.month:02d}"


def read_meisai(store_name_jp: str, ym: str) -> list[dict]:
    """ナイスネイル meisai CSV を読み込んで対象YMの取引行リストを返す"""
    path = NICENAIL_DATA / f"meisai_{store_name_jp}.csv"
    if not path.exists():
        print(f"  ⚠️ meisai CSV not found: {path}", file=sys.stderr)
        return []
    rows = []
    try:
        with open(path, encoding="shift_jis", errors="replace") as f:
            reader = csv.DictReader(f)
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
                rows.append({
                    "date": d,  # YYYYMMDD
                    "time": r.get("会計時間", ""),
                    "kaikei_id": r.get("会計ID", ""),
                    "kubun": kubun,  # 施術 / 店販 等
                    "category": r.get("カテゴリ", "").strip(),
                    "menu": menu,
                    "unit_price": _to_int(r.get("単価", "0")),
                    "qty": _to_int(r.get("個数", "1")),
                    "amount": _to_int(r.get("金額", "0")),
                    "staff": r.get("スタッフ", "").strip(),
                    "shimei": r.get("指名", "").strip(),
                    "new_or_repeat": r.get("新規再来", "").strip(),  # 新規 / 再来
                })
    except Exception as e:
        print(f"  ⚠️ failed to read {path}: {e}", file=sys.stderr)
        return []
    return rows


def _to_int(s: str) -> int:
    if not s:
        return 0
    s = str(s).replace(",", "").replace("¥", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return 0


def aggregate_per_kaikei(rows: list[dict]) -> dict:
    """会計ID単位で集約。 {kaikei_id: {date, staff, sales, options, shimei, new}}"""
    kaikei = {}
    options_keywords = load_admin_config().get("option_keywords", [])
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
                "shop_sales": 0,
                "options": 0,
                "shimei": r["shimei"],
                "new_or_repeat": r["new_or_repeat"],
                "menu_items": [],
            }
        kaikei[kid]["sales"] += r["amount"]
        if r["kubun"] == "施術":
            kaikei[kid]["tech_sales"] += r["amount"]
        else:
            kaikei[kid]["shop_sales"] += r["amount"]
        kaikei[kid]["options"] += count_options(r["menu"], options_keywords)
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


def build_staff_ranking_csv(kaikei: dict, store_name_full: str) -> list[list]:
    """スタッフ別 月次 CSV (Uレジ風) を構築"""
    # スタッフ別集計
    staff = defaultdict(lambda: {
        "work_dates": set(),  # 出勤日数 (会計があった日)
        "customers": 0,
        "sales": 0,
        "tech_sales": 0,
        "shop_sales": 0,
        "nominated": 0,
        "options": 0,  # OP数 (1来店あたりの平均OPで使う)
        "nail_sales": 0,  # (ネイル)カテゴリ合算
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
        if v["shimei"] == "指名あり":
            s["nominated"] += 1
        s["options"] += v["options"]
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
        shop_pct = (s["shop_sales"] / s["sales"] * 100) if s["sales"] else 0
        # OP率 = options が付いた来店 / 全来店 (簡略: options数を客数で割って %)
        op_rate = (s["options"] / s["customers"] * 100) if s["customers"] else 0
        rows.append([
            store_name_full, "", name, len(s["work_dates"]),
            s["sales"], s["customers"], spc,
            s["tech_sales"], s["customers"], tech_spc,
            nominated_sales, s["nominated"], nominated_spc,
            free_sales, free_customers, free_spc,
            0, 0, 0,  # 男 (SC明細に性別なし)
            0, 0, 0,  # 女
            s["shop_sales"], 0, f"{shop_pct:.2f}%", "0%",
            s["nail_sales"],
            s["options"], f"{op_rate:.1f}%", len(s["work_dates"]),
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
            "category": v["category"],
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
    total_processed = 0
    for store_name_jp, hanabi_id in NICENAIL_TO_HANABI.items():
        store_name_full = "ナイスネイル新横浜店"  # ハードコード (1店舗のみ想定)
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

        # staff_ranking CSV
        staff_csv = build_staff_ranking_csv(kaikei, store_name_full)
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
