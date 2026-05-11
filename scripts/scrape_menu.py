#!/usr/bin/env python3
"""メニュー別実績 (情報分析→技術の実績) をHTMLスクレイプ。
Uレジに公式CSV出力がないため、結果テーブルをHTMLから直接抽出する。

実行:
  python3 scripts/scrape_menu.py FY22        # FY22 年間 (2022/05–2023/04)
  python3 scripts/scrape_menu.py FY25        # FY25年間 (2店舗)
  python3 scripts/scrape_menu.py FY25_MONTHLY  # FY25 月別 12ヶ月分
  python3 scripts/scrape_menu.py FY22_MONTHLY  # FY22 月別 12ヶ月分
  python3 scripts/scrape_menu.py 202605MTD   # 5月1日〜今日 (2店舗)
  python3 scripts/scrape_menu.py 202505      # 単月 (2店舗)

FY<NN>: 5月1日〜翌4月30日, FY<NN>_MONTHLY: その12ヶ月別
出力: data/menu_<period>_<store>.json
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

MENU_URL = "https://owner-beauty.u-regi.com/tech_sales_result/c_tech_sales_result/view"


def configure_period_range(page, date_from: str, date_to: str):
    """Switch to 期間指定 tab and set date range. Uses JS to bypass styled radio overlay."""
    page.evaluate("""
        () => {
            // Select 期間指定 tab (radio with value='fromto')
            const radios = document.querySelectorAll("input[name='report_type']");
            radios.forEach(r => { r.checked = (r.value === 'fromto'); });
            radios.forEach(r => r.dispatchEvent(new Event('change', {bubbles: true})));
            // Trigger any change handler attached to the segment
            const evt = new Event('click', {bubbles: true});
            const target = Array.from(radios).find(r => r.value === 'fromto');
            if (target) target.dispatchEvent(evt);
        }
    """)
    time.sleep(1)
    # Set date inputs (look for visible inputs with date format)
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
    """Set store via dropdown <select>."""
    # Identify store select
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


def load_all_rows(page):
    """Scroll the result table to force all rows to render (jqGrid virtual scroll)."""
    page.evaluate("""
        async () => {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            // Scroll the page first
            window.scrollTo(0, 0);
            await sleep(300);
            for (let y = 0; y < document.body.scrollHeight; y += 400) {
                window.scrollTo(0, y);
                await sleep(150);
            }
            window.scrollTo(0, 0);
            // Also scroll any inner scrollable container
            const containers = document.querySelectorAll('.ui-jqgrid-bdiv, .gridtable, [class*=grid]');
            for (const c of containers) {
                if (c.scrollHeight > c.clientHeight) {
                    for (let y = 0; y < c.scrollHeight; y += 200) {
                        c.scrollTop = y;
                        await sleep(100);
                    }
                    c.scrollTop = 0;
                }
            }
        }
    """)
    time.sleep(2)


def scrape_table(page) -> dict:
    """Extract menu data via jqGrid API or grid_data global."""
    return page.evaluate(r"""
        () => {
            // Method 1: jqGrid API - getRowData on #sample1
            try {
                if (typeof jQuery !== 'undefined' && jQuery('#sample1').length) {
                    const data = jQuery('#sample1').jqGrid('getRowData');
                    if (data && data.length) {
                        return { rows: data, debug: 'jqgrid-api', total_rows: data.length };
                    }
                }
            } catch (e) {}
            // Method 2: global grid_data variable
            try {
                if (typeof grid_data !== 'undefined' && Array.isArray(grid_data)) {
                    return { rows: grid_data, debug: 'global', total_rows: grid_data.length };
                }
            } catch (e) {}
            // Method 3: scrape DOM rows from #sample1 directly
            const grid = document.querySelector('#sample1');
            if (grid) {
                const rows = grid.querySelectorAll('tbody tr');
                const out = [];
                for (const r of rows) {
                    const cells = Array.from(r.querySelectorAll('td')).map(c => c.innerText.trim());
                    if (cells.length >= 3) out.push({ cells });
                }
                return { rows: out, debug: 'dom-sample1', total_rows: rows.length };
            }
            return { rows: [], debug: 'none' };
        }
    """)


def scrape_period(page, label: str, store_code: str, date_from: str, date_to: str, log_dir: Path) -> list[dict]:
    print(f"  {label} / {store_code}: {date_from}–{date_to}")
    page.goto(MENU_URL, wait_until="networkidle")
    time.sleep(2)
    configure_store(page, store_code)
    time.sleep(0.5)
    configure_period_range(page, date_from, date_to)
    # Ensure メニュー順 + show categories ON for richer breakdown
    try:
        page.click("button:has-text('表示')", force=True)
    except Exception:
        pass
    time.sleep(8)
    load_all_rows(page)
    page.screenshot(path=str(log_dir / f"{label}_{store_code}.png"), full_page=True)
    (log_dir / f"{label}_{store_code}.html").write_text(page.content(), encoding="utf-8")
    result = scrape_table(page)
    rows = result.get("rows", [])
    print(f"    → {len(rows)} rows  [{result.get('debug', '?')}, total_tr={result.get('total_rows', '?')}]")
    return rows


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: scrape_menu.py <FY25|FY25_MONTHLY|202605MTD|YYYYMM>")
    mode = sys.argv[1]
    env = load_env()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = LOGS / f"menu_scrape_{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Build period list
    periods = []  # (label, date_from, date_to)
    import calendar, re as _re

    def _fy_months(fy_num: int) -> list[str]:
        # FY<NN>: starts 5月 of year (2000+NN), 12 months
        start_year = 2000 + fy_num
        out = []
        for i in range(12):
            mi = (4 + i) % 12  # 0-indexed month relative to May
            m = mi + 1
            y = start_year if i < 8 else start_year + 1
            out.append(f"{y}{m:02d}")
        return out

    fy_match = _re.match(r"^FY(\d{2})(_MONTHLY)?$", mode)
    if mode == "FY25":  # legacy alias
        periods.append(("FY25_annual", "20250501", "20260430"))
    elif fy_match:
        fy_num = int(fy_match.group(1))
        is_monthly = bool(fy_match.group(2))
        months = _fy_months(fy_num)
        if is_monthly:
            for ym in months:
                y, m = int(ym[:4]), int(ym[4:6])
                last = calendar.monthrange(y, m)[1]
                periods.append((ym, f"{ym}01", f"{ym}{last:02d}"))
        else:
            start = months[0]
            end = months[-1]
            ey, em = int(end[:4]), int(end[4:6])
            last = calendar.monthrange(ey, em)[1]
            periods.append((f"FY{fy_num}_annual", f"{start}01", f"{end}{last:02d}"))
    elif mode == "FY25_MONTHLY":  # legacy alias
        for ym in ["202505","202506","202507","202508","202509","202510","202511","202512","202601","202602","202603","202604"]:
            y, m = int(ym[:4]), int(ym[4:6])
            last = calendar.monthrange(y, m)[1]
            periods.append((ym, f"{ym}01", f"{ym}{last:02d}"))
    elif mode == "202605MTD":
        today = date.today()
        periods.append(("202605MTD", "20260501", today.strftime("%Y%m%d")))
    elif len(mode) == 6 and mode.isdigit():
        ym = mode
        y, m = int(ym[:4]), int(ym[4:6])
        last = calendar.monthrange(y, m)[1]
        # 進行中月 (month_end が今日より未来) の場合は date_to を today に丸める
        # Uレジに未来日を渡すと意図しない挙動になる可能性があるため
        today = date.today()
        target_end_date = date(y, m, last)
        if target_end_date > today:
            periods.append((ym, f"{ym}01", today.strftime("%Y%m%d")))
        else:
            periods.append((ym, f"{ym}01", f"{ym}{last:02d}"))
    else:
        sys.exit(f"unknown mode: {mode}")

    print(f"📅 scraping {len(periods)} period(s) × 2 stores")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ja-JP", timezone_id="Asia/Tokyo")
        page = ctx.new_page()
        login(page, env, log_dir)

        for label, df, dt in periods:
            for store in STORES:
                rows = scrape_period(page, label, store["code"], df, dt, log_dir)
                out = {
                    "label": label,
                    "store_id": store["id"],
                    "store_code": store["code"],
                    "date_from": df,
                    "date_to": dt,
                    "scraped_at": datetime.now().isoformat(timespec="seconds"),
                    "rows": rows,
                }
                out_path = DATA / f"menu_{label}_{store['id']}.json"
                out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"    saved → {out_path.name}")
        time.sleep(2)
        browser.close()
    print("✅ done")


if __name__ == "__main__":
    main()
