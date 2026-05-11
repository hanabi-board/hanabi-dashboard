#!/usr/bin/env python3
"""情報分析 各種レポートをHTMLスクレイプ。

メニュー別 (scrape_menu.py) と同じパターンで、 他のレポートも統一的に取得。

実行:
  python3 scripts/scrape_jouhou.py week 202605       # 曜日別 単月
  python3 scripts/scrape_jouhou.py age 202605        # 年代別 単月
  python3 scripts/scrape_jouhou.py lost 202605       # 失客分析 単月
  python3 scripts/scrape_jouhou.py week FY25_ANNUAL  # FY25 年間 (全レポート対応)

出力: data/jouhou_<report>_<period>_<store>.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, date
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT / "scripts"))
from auto_download import load_env, login, STORES  # noqa: E402

# 各レポートのURL
REPORTS = {
    "week":  "https://owner-beauty.u-regi.com/week_report/c_week_report/view",
    "age":   "https://owner-beauty.u-regi.com/age_result/c_age_result/view",
    "lost":  "https://owner-beauty.u-regi.com/lost_customer_report/c_lost_customer_report/view",
    "repeat": "https://owner-beauty.u-regi.com/repeat_result/c_repeat_result/view",
    "visit_interval": "https://owner-beauty.u-regi.com/visit_interval/c_visit_interval/view",
    "karte": "https://owner-beauty.u-regi.com/karte_results/c_karte_results/view",
    "area":  "https://owner-beauty.u-regi.com/area_report/c_area_report/view",
}


def configure_period_range(page, date_from: str, date_to: str):
    """期間指定 tab に切替 + 日付セット (scrape_menu.py と同じパターン)。"""
    page.evaluate("""
        () => {
            const radios = document.querySelectorAll("input[name='report_type']");
            radios.forEach(r => { r.checked = (r.value === 'fromto'); });
            radios.forEach(r => r.dispatchEvent(new Event('change', {bubbles: true})));
            const target = Array.from(radios).find(r => r.value === 'fromto');
            if (target) target.dispatchEvent(new Event('click', {bubbles: true}));
        }
    """)
    time.sleep(1)
    page.evaluate(f"""
        () => {{
            const all = Array.from(document.querySelectorAll('input.datepicker, input[id*="date"], input[type=number][maxlength="8"]'));
            const visible = all.filter(i => i.offsetParent !== null);
            if (visible.length >= 2) {{
                visible[0].value = "{date_from}";
                visible[1].value = "{date_to}";
                visible.forEach(i => {{
                    i.dispatchEvent(new Event('input', {{bubbles: true}}));
                    i.dispatchEvent(new Event('change', {{bubbles: true}}));
                }});
            }}
        }}
    """)
    time.sleep(0.5)


def configure_store(page, store_code: str):
    """店舗ドロップダウン選択。"""
    selects = page.evaluate("""
        () => Array.from(document.querySelectorAll('select')).map(s => ({
            id: s.id, name: s.name, options: Array.from(s.options).map(o => o.value + ':' + o.text)
        }))
    """)
    target = None
    for s in selects:
        for op in s["options"]:
            if op.startswith(store_code + ":"):
                target = s["id"] or s["name"]
                break
        if target:
            break
    if target:
        sel = f"#{target}" if target == selects[0]["id"] else f"[name='{target}']"
        try:
            page.select_option(sel, store_code)
        except Exception as e:
            print(f"  warn: store select: {e}")


def scrape_grid(page) -> dict:
    """jqGrid からデータを取得 (scrape_menu.py と同じパターン)。"""
    return page.evaluate(r"""
        () => {
            // Method 1: jqGrid API - try common grid IDs
            const candidates = ['#sample1', '.ui-jqgrid table'];
            for (const sel of candidates) {
                try {
                    if (typeof jQuery !== 'undefined') {
                        const el = jQuery(sel);
                        if (el.length && el[0].id) {
                            const data = jQuery('#' + el[0].id).jqGrid('getRowData');
                            if (data && data.length) {
                                return { rows: data, debug: 'jqgrid:' + el[0].id };
                            }
                        }
                    }
                } catch (e) {}
            }
            // Method 2: global grid_data
            try {
                if (typeof grid_data !== 'undefined' && Array.isArray(grid_data)) {
                    return { rows: grid_data, debug: 'global grid_data' };
                }
            } catch (e) {}
            // Method 3: DOM scrape (fallback)
            const grids = document.querySelectorAll('.ui-jqgrid');
            const out = [];
            for (const g of grids) {
                const rows = g.querySelectorAll('tbody tr');
                for (const r of rows) {
                    const cells = Array.from(r.querySelectorAll('td')).map(c => c.innerText.trim());
                    if (cells.length >= 2) out.push({ cells });
                }
            }
            return { rows: out, debug: 'dom-fallback', total: out.length };
        }
    """)


def scrape_one(page, report_type: str, label: str, store_code: str, date_from: str, date_to: str, log_dir: Path) -> list:
    print(f"  {report_type} / {label} / {store_code}: {date_from}–{date_to}")
    url = REPORTS[report_type]
    page.goto(url, wait_until="networkidle")
    time.sleep(2)
    configure_store(page, store_code)
    time.sleep(0.5)
    configure_period_range(page, date_from, date_to)
    try:
        page.click("button:has-text('表示')", force=True)
    except Exception:
        pass
    time.sleep(8)
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1)
    page.screenshot(path=str(log_dir / f"{report_type}_{label}_{store_code}.png"), full_page=True)
    (log_dir / f"{report_type}_{label}_{store_code}.html").write_text(page.content(), encoding="utf-8")
    result = scrape_grid(page)
    rows = result.get("rows", [])
    print(f"    → {len(rows)} rows  [{result.get('debug', '?')}]")
    return rows


def main():
    if len(sys.argv) < 3:
        print(f"usage: scrape_jouhou.py <report> <period>")
        print(f"  reports: {', '.join(REPORTS.keys())}")
        print(f"  period: YYYYMM | FY<NN>_ANNUAL")
        sys.exit(1)
    report_type = sys.argv[1]
    if report_type not in REPORTS:
        sys.exit(f"unknown report: {report_type}. choices: {list(REPORTS.keys())}")
    period_arg = sys.argv[2]

    env = load_env()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = LOGS / f"jouhou_{report_type}_{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Build period list
    periods = []
    if period_arg.startswith("FY") and period_arg.endswith("_ANNUAL"):
        fy_num = int(period_arg.replace("FY", "").replace("_ANNUAL", ""))
        start_year = 2000 + fy_num
        periods.append((period_arg, f"{start_year}0501", f"{start_year+1}0430"))
    elif len(period_arg) == 6 and period_arg.isdigit():
        import calendar
        y, m = int(period_arg[:4]), int(period_arg[4:6])
        last = calendar.monthrange(y, m)[1]
        today = date.today()
        target_end = date(y, m, last)
        end_str = today.strftime("%Y%m%d") if target_end > today else f"{period_arg}{last:02d}"
        periods.append((period_arg, f"{period_arg}01", end_str))
    else:
        sys.exit(f"unknown period: {period_arg}")

    print(f"📅 {report_type} scraping {len(periods)} period(s) × 2 stores")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ja-JP", timezone_id="Asia/Tokyo")
        page = ctx.new_page()
        login(page, env, log_dir)

        for label, df, dt in periods:
            for store in STORES:
                rows = scrape_one(page, report_type, label, store["code"], df, dt, log_dir)
                out = {
                    "report_type": report_type,
                    "label": label,
                    "store_id": store["id"],
                    "store_code": store["code"],
                    "date_from": df,
                    "date_to": dt,
                    "scraped_at": datetime.now().isoformat(timespec="seconds"),
                    "rows": rows,
                }
                out_path = DATA / f"jouhou_{report_type}_{label}_{store['id']}.json"
                out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"    saved → {out_path.name}")

        time.sleep(2)
        browser.close()
    print("✅ done")


if __name__ == "__main__":
    main()
