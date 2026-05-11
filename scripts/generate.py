#!/usr/bin/env python3
"""HANABI Dashboard generator.

Reads CSVs from data/ and produces docs/data.json + docs/index.html.

Filename conventions:
    daily_sales_YYYYMM_<store>.csv      日別×店舗
    staff_ranking_YYYYMM_<store>.csv    月別×店舗×スタッフ

Stores supported: tsunashima (Hanabi綱島店), miyakojima (ELLE by Hanabi)
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"

STORES = {
    "tsunashima": {
        "name": "Hanabi 綱島店",
        "short": "綱島",
        "brand": "hanabi",
        "departments": ["ヘア"],
        # 開店日 (実データの最古から推定/設定)。 同店舗 YoY 判定に使用。
        "open_date": "2022-05-01",  # FY22期初時点で既にOPEN
    },
    "miyakojima": {
        "name": "ELLE by Hanabi 宮古島店",
        "short": "宮古島",
        "brand": "elle",
        "departments": ["ヘア", "アイ", "ネイル"],
        "open_date": "2025-09-01",  # ELLE by Hanabi 宮古島店 開店
    },
}

DEPARTMENTS = ("ヘア", "アイ", "ネイル")

# Names whose sales count toward totals but are hidden from staff display/ranking.
# Includes:
#   - Owner/management who bought retail under their name (水野 陽平)
#   - Placeholder names for unassigned transactions (未設定, 指名なし, ネイル スタッフ)
HIDDEN_STAFF_NAMES = {
    "水野 陽平",
    "未設定",
    "指名なし",
    "ネイル スタッフ",
}


def classify_department(col_name: str) -> str:
    """Map a menu category column to a department."""
    if col_name.startswith("(ネイル)") or "ネイル店販" in col_name:
        return "ネイル"
    if col_name.startswith("(アイメニュー)") or "アイ店販" in col_name:
        return "アイ"
    return "ヘア"


def parse_int(s: str) -> int:
    if not s:
        return 0
    s = s.replace(",", "").replace("¥", "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def parse_pct(s: str) -> float:
    if not s:
        return 0.0
    s = s.replace("%", "").replace(",", "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def read_csv_sjis(path: Path) -> list[list[str]]:
    with path.open("r", encoding="shift_jis", errors="replace", newline="") as f:
        return list(csv.reader(f))


def load_daily_sales(path: Path, store_id: str, year_month: str) -> list[dict]:
    """Parse daily_sales_*.csv (日別売上実績)."""
    rows = read_csv_sjis(path)
    if not rows:
        return []
    out = []
    yyyy, mm = year_month[:4], year_month[4:6]
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        date_cell = row[0]
        if date_cell == "TOTAL":
            continue
        m = re.match(r"^(\d{2})/(\d{2})$", date_cell)
        if not m:
            continue
        date_iso = f"{yyyy}-{m.group(1)}-{m.group(2)}"
        out.append({
            "store": store_id,
            "date": date_iso,
            "weekday": row[1] if len(row) > 1 else "",
            "new": parse_int(row[2]),
            "repeat": parse_int(row[3]),
            "referral": parse_int(row[4]),
            "nominated": parse_int(row[5]),
            "customers": parse_int(row[6]),
            "customer_target": parse_int(row[7]),
            "customer_pct": parse_pct(row[8]),
            "sales_grand_menu": parse_int(row[9]),
            "sales_coupon": parse_int(row[10]),
            "sales_other": parse_int(row[11]),
            "sales": parse_int(row[12]),
            "sales_target": parse_int(row[13]),
            "sales_pct": parse_pct(row[14]),
            "spend_per_customer": parse_int(row[15]),
            "reservations_next": parse_int(row[16]) if len(row) > 16 else 0,
        })
    return out


def load_staff_ranking(path: Path, store_id: str, year_month: str) -> list[dict]:
    """Parse staff_ranking_*.csv. Returns list of staff dicts with department breakdown."""
    rows = read_csv_sjis(path)
    if len(rows) < 2:
        return []
    headers = rows[0]
    out = []
    # Determine department per category column once
    cat_dept = []
    for i in range(26, len(headers)):
        cat_dept.append((i, headers[i], classify_department(headers[i])))

    for row in rows[1:]:
        if len(row) < 26:
            continue
        staff_name = row[2].strip()
        if not staff_name or staff_name == "TOTAL":
            continue
        # Department breakdown
        dept_sales = {d: 0 for d in DEPARTMENTS}
        for idx, _col, dept in cat_dept:
            if idx < len(row):
                dept_sales[dept] += parse_int(row[idx])

        # Determine primary dept = max sales
        primary = max(dept_sales.items(), key=lambda x: x[1])[0] if any(dept_sales.values()) else "ヘア"

        out.append({
            "store": store_id,
            "month": year_month,
            "name": staff_name,
            "category": row[1].strip(),
            "work_days": parse_int(row[3]),
            "total_sales": parse_int(row[4]),
            "customers": parse_int(row[5]),
            "spend_per_customer": parse_int(row[6]),
            "tech_sales": parse_int(row[7]),
            "tech_customers": parse_int(row[8]),
            "tech_sales_nominated": parse_int(row[10]),
            "tech_customers_nominated": parse_int(row[11]),
            "tech_sales_free": parse_int(row[13]),
            "tech_customers_free": parse_int(row[14]),
            "tech_sales_male": parse_int(row[16]),
            "tech_customers_male": parse_int(row[17]),
            "tech_sales_female": parse_int(row[19]),
            "tech_customers_female": parse_int(row[20]),
            "shop_sales": parse_int(row[22]),
            "shop_customers": parse_int(row[23]),
            "shop_pct": parse_pct(row[24]),
            "purchase_pct": parse_pct(row[25]),
            "dept_sales": dept_sales,
            "primary_dept": primary,
            "hidden": staff_name in HIDDEN_STAFF_NAMES,
        })
    return out


def discover_csvs() -> tuple[list[tuple[Path, str, str]], list[tuple[Path, str, str]]]:
    """Return (daily, staff) lists of (path, store_id, yyyymm) tuples."""
    daily = []
    staff = []
    for p in DATA.glob("daily_sales_*.csv"):
        m = re.match(r"daily_sales_(\d{6})_([a-z]+)\.csv", p.name)
        if m and m.group(2) in STORES:
            daily.append((p, m.group(2), m.group(1)))
    for p in DATA.glob("staff_ranking_*.csv"):
        m = re.match(r"staff_ranking_(\d{6})_([a-z]+)\.csv", p.name)
        if m and m.group(2) in STORES:
            staff.append((p, m.group(2), m.group(1)))
    return daily, staff


def aggregate_monthly_by_store(staff_rows: list[dict]) -> dict:
    """Aggregate staff rows -> { store: { month: { dept: { sales, customers, staff_count }, total: {...} } } }"""
    out = defaultdict(lambda: defaultdict(lambda: {
        "total_sales": 0,
        "tech_sales": 0,
        "tech_sales_nominated": 0,
        "tech_sales_free": 0,
        "tech_sales_male": 0,
        "tech_sales_female": 0,
        "tech_customers_male": 0,
        "tech_customers_female": 0,
        "shop_sales": 0,
        "customers": 0,
        "tech_customers": 0,
        "tech_customers_nominated": 0,
        "tech_customers_free": 0,
        "staff_count": 0,
        "by_dept": {d: {"sales": 0, "customers": 0, "staff_count": 0} for d in DEPARTMENTS},
        "staff": [],
    }))
    seen_dept_staff = defaultdict(set)  # (store, month, dept) -> set of staff names
    for s in staff_rows:
        bucket = out[s["store"]][s["month"]]
        bucket["total_sales"] += s["total_sales"]
        bucket["tech_sales"] += s["tech_sales"]
        bucket["tech_sales_nominated"] += s.get("tech_sales_nominated", 0)
        bucket["tech_sales_free"] += s.get("tech_sales_free", 0)
        bucket["shop_sales"] += s["shop_sales"]
        bucket["customers"] += s["customers"]
        bucket["tech_customers"] += s.get("tech_customers", 0)
        bucket["tech_customers_nominated"] += s.get("tech_customers_nominated", 0)
        bucket["tech_customers_free"] += s.get("tech_customers_free", 0)
        bucket["tech_sales_male"] += s.get("tech_sales_male", 0)
        bucket["tech_sales_female"] += s.get("tech_sales_female", 0)
        bucket["tech_customers_male"] += s.get("tech_customers_male", 0)
        bucket["tech_customers_female"] += s.get("tech_customers_female", 0)
        bucket["staff_count"] += 1
        bucket["staff"].append(s["name"])
        # by department: assign to primary dept
        d = s["primary_dept"]
        bucket["by_dept"][d]["sales"] += s["total_sales"]
        bucket["by_dept"][d]["customers"] += s["customers"]
        key = (s["store"], s["month"], d)
        if s["name"] not in seen_dept_staff[key]:
            bucket["by_dept"][d]["staff_count"] += 1
            seen_dept_staff[key].add(s["name"])
    return {s: dict(m) for s, m in out.items()}


def aggregate_daily_by_store(daily_rows: list[dict]) -> dict:
    """Aggregate daily rows -> { store: { month: [day rows sorted by date] } }"""
    out = defaultdict(lambda: defaultdict(list))
    for d in daily_rows:
        ym = d["date"][:4] + d["date"][5:7]
        out[d["store"]][ym].append(d)
    for store in out:
        for ym in out[store]:
            out[store][ym].sort(key=lambda x: x["date"])
    return {s: dict(m) for s, m in out.items()}


def aggregate_visitors_by_store(daily_by_store: dict) -> dict:
    """Aggregate { store: { month: { new, repeat, referral, nominated, customers } } }
    from daily rows."""
    out = {}
    for sid, by_month in daily_by_store.items():
        out[sid] = {}
        for ym, days in by_month.items():
            agg = {"new": 0, "repeat": 0, "referral": 0, "nominated": 0, "customers": 0}
            for d in days:
                agg["new"] += d["new"]
                agg["repeat"] += d["repeat"]
                agg["referral"] += d["referral"]
                agg["nominated"] += d["nominated"]
                agg["customers"] += d["customers"]
            out[sid][ym] = agg
    return out


def load_budgets() -> dict:
    p = DATA / "budgets.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def days_in_month(year_month: str) -> int:
    import calendar
    y = int(year_month[:4]); m = int(year_month[4:6])
    return calendar.monthrange(y, m)[1]


def main():
    DOCS.mkdir(exist_ok=True)
    daily_files, staff_files = discover_csvs()
    budgets = load_budgets()

    daily_rows = []
    for path, store_id, ym in daily_files:
        daily_rows.extend(load_daily_sales(path, store_id, ym))

    staff_rows = []
    for path, store_id, ym in staff_files:
        staff_rows.extend(load_staff_ranking(path, store_id, ym))

    monthly = aggregate_monthly_by_store(staff_rows)
    daily_agg = aggregate_daily_by_store(daily_rows)
    visitors = aggregate_visitors_by_store(daily_agg)

    # All months and stores observed
    months = sorted({s["month"] for s in staff_rows} | {d["date"][:4] + d["date"][5:7] for d in daily_rows})
    latest_month = months[-1] if months else None

    # Build summary KPIs for the latest month
    summary = {
        "latest_month": latest_month,
        "stores": STORES,
        "departments": list(DEPARTMENTS),
        "by_store": {},
        "company_total": {"total_sales": 0, "customers": 0, "target": 0},
    }
    if latest_month:
        for store_id in STORES:
            ms = monthly.get(store_id, {}).get(latest_month, None)
            ds = daily_agg.get(store_id, {}).get(latest_month, [])
            day_total_sales = sum(d["sales"] for d in ds)
            day_target = sum(d["sales_target"] for d in ds)
            summary["by_store"][store_id] = {
                "monthly": ms,
                "daily_sum_sales": day_total_sales,
                "daily_sum_target": day_target,
                "days_recorded": len([d for d in ds if d["sales"] > 0]),
            }
            summary["company_total"]["total_sales"] += day_total_sales or (ms["total_sales"] if ms else 0)
            summary["company_total"]["target"] += day_target

    # Build month-aware target view: { store: { month: target } } from FY26 budget
    target_by_store = {}
    if budgets:
        for sid, bm in (budgets.get("monthly") or {}).items():
            target_by_store[sid] = bm

    # Include uregi top snapshot if available
    snapshot = None
    snapshot_path = DATA / "uregi_top_snapshot.json"
    if snapshot_path.exists():
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Load menu_*.json files (scraped from 情報分析→技術の実績画面)
    menu_data = {}  # { label: { store_id: rows } }
    for p in DATA.glob("menu_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            label = d.get("label")
            sid = d.get("store_id")
            if label and sid:
                menu_data.setdefault(label, {})[sid] = d.get("rows", [])
        except Exception as e:
            print(f"  warn: failed to load {p.name}: {e}")

    # Load jouhou_*.json files (scraped from 情報分析の他レポート)
    # 構造: jouhou_<report>_<label>_<store>.json
    # e.g. jouhou_lost_202605_tsunashima.json
    jouhou_data = {}  # { report: { label: { store_id: {rows, ...} } } }
    for p in DATA.glob("jouhou_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            report = d.get("report_type")
            label = d.get("label")
            sid = d.get("store_id")
            if report and label and sid:
                jouhou_data.setdefault(report, {}).setdefault(label, {})[sid] = {
                    "rows": d.get("rows", []),
                    "by_tab": d.get("by_tab", {}),  # サブタブ別データ
                    "date_from": d.get("date_from"),
                    "date_to": d.get("date_to"),
                    "scraped_at": d.get("scraped_at"),
                }
        except Exception as e:
            print(f"  warn: failed to load {p.name}: {e}")

    # Load recruitment data
    recruitment = None
    rec_path = DATA / "recruitment.json"
    if rec_path.exists():
        try:
            recruitment = json.loads(rec_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Load staff profiles data
    staff_profiles = None
    sp_path = DATA / "staff_profiles.json"
    if sp_path.exists():
        try:
            staff_profiles = json.loads(sp_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Load external scrape data (HotPepper / Instagram) — history per source
    # 構造: external_<source>_YYYYMMDD.json — 履歴があれば時系列が取れる
    external = {"hotpepper": [], "instagram": []}
    for p in sorted(DATA.glob("external_*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            src = d.get("source")
            if src in external:
                external[src].append(d)
        except Exception as e:
            print(f"  warn: failed to load {p.name}: {e}")

    # Load denpyo (取引明細) data — 取引粒度のリピート率分析用
    # 構造: denpyo_<date_range>_<store>.json
    denpyo_summary = {}  # { store_id: { period: {row_count, unique_members, repeat_rate, ...} } }
    for p in sorted(DATA.glob("denpyo_*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            sid = d.get("store_id")
            period = f"{d.get('date_from')}-{d.get('date_to')}"
            rows = d.get("rows", [])
            # Aggregate metrics
            members = {}
            new_count = 0
            for r in rows:
                cust = r.get("customer", {})
                member = cust.get("member_name", "").strip()
                if member:
                    members[member] = members.get(member, 0) + 1
                else:
                    new_count += 1  # 会員なし = 新規/ゲスト扱い
            repeat_visits = sum(1 for c in members.values() if c > 1)
            unique_members = len(members)
            total_visits = len(rows)
            repeat_rate = (repeat_visits / unique_members * 100) if unique_members else None
            payment_total = sum((r.get("payment") or {}).get("total", 0) or sum((r.get("payment") or {}).values()) for r in rows)
            denpyo_summary.setdefault(sid, {})[period] = {
                "row_count": total_visits,
                "unique_members": unique_members,
                "guest_count": new_count,
                "repeat_visits": repeat_visits,
                "repeat_rate": repeat_rate,
                "payment_total": payment_total,
                "scraped_at": d.get("scraped_at"),
            }
        except Exception as e:
            print(f"  warn: failed to load {p.name}: {e}")

    out = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "stores": STORES,
        "departments": list(DEPARTMENTS),
        "months": months,
        "latest_month": latest_month,
        "summary": summary,
        "monthly_by_store": monthly,
        "daily_by_store": daily_agg,
        "visitors_by_store": visitors,
        "staff_rows": staff_rows,
        "budgets": budgets,
        "target_by_store": target_by_store,
        "days_in_month": {m: days_in_month(m) for m in months} if months else {},
        "uregi_top_snapshot": snapshot,
        "menu_data": menu_data,
        "recruitment": recruitment,
        "staff_profiles": staff_profiles,
        "jouhou_data": jouhou_data,
        "external": external,
        "denpyo_summary": denpyo_summary,
    }

    out_path = DOCS / "data.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ wrote {out_path} ({out_path.stat().st_size:,} bytes)")
    print(f"  stores={list(STORES.keys())} months={months}")


if __name__ == "__main__":
    main()
