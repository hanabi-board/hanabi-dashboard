#!/usr/bin/env python3
"""Uレジから日次CSV4ファイルをPlaywrightで自動ダウンロード。

実行: python3 scripts/auto_download.py [YYYYMM]
出力:
  data/daily_sales_YYYYMM_<store>.csv
  data/staff_ranking_YYYYMM_<store>.csv
  data/uregi_top_snapshot.json (TOP画面リアルタイム)
  logs/download_<ts>/ (スクリーンショット + 生CSV保存)

対象店舗: 001 (Hanabi綱島店) / 002 (ELLE by Hanabi 宮古島店)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright, Download

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

STORES = [
    {"id": "tsunashima", "code": "001", "name": "Hanabi綱島店"},
    {"id": "miyakojima", "code": "002", "name": "ELLE by Hanabi 宮古島店"},
]

REPORT_URLS = {
    "uriage":     "https://owner-beauty.u-regi.com/sales_results/C_sales_results/sales_results_list",
    "staff":      "https://owner-beauty.u-regi.com/staff_ranking/c_staff_ranking/staff_ranking_list",
    "regi_close": "https://owner-beauty.u-regi.com/tenpo_bunseki/c_tenpo_bunseki/tenpo_bunseki_for_base_html",
}


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if not p.exists():
        sys.exit("error: .env not found")
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)$", line)
        if m:
            env[m.group(1)] = m.group(2)
    return env


def login(page: Page, env: dict, log_dir: Path):
    print("→ login")
    page.goto(env["URegi_LOGIN_URL"], wait_until="networkidle")
    page.fill("[name='company_code']", env["URegi_COMPANY"])
    page.fill("[name='user_id']", env["URegi_USER"])
    page.fill("[name='password']", env["URegi_PW"])
    with page.expect_navigation(wait_until="networkidle", timeout=20000):
        page.click("[name='login']")
    page.screenshot(path=str(log_dir / "01_after_login.png"))
    print(f"  ✓ logged in: {page.url}")


def capture_top_snapshot(page: Page, log_dir: Path) -> dict:
    print("→ capture TOP snapshot")
    if "/index" not in page.url and "/top" not in page.url:
        try:
            page.click("text=TOP", timeout=3000)
            page.wait_for_load_state("networkidle")
        except Exception:
            pass
    time.sleep(1)
    page.screenshot(path=str(log_dir / "02_top.png"), full_page=True)
    snapshot = page.evaluate(r"""
        () => {
            const text = document.body.innerText;
            const out = { snapshot_time: null };
            const tsMatch = text.match(/(\d{4}\/\d{2}\/\d{2} \d{2}:\d{2}:\d{2})時点/);
            if (tsMatch) out.snapshot_time = tsMatch[1];
            const grab = (label) => {
                const re = new RegExp(label + "[^¥0-9]*([¥0-9,()/人件円-]+)");
                const m = text.match(re);
                return m ? m[1].trim() : null;
            };
            out.today_sales = grab("売上\\(内 返品額\\)");
            out.today_customers = grab("客数\\(内 返品客数\\)");
            out.today_avg = grab("客単価");
            out.today_active_staff = grab("稼働数/担当者数");
            out.today_reservation_rate = grab("消化数/予約数");
            const storeMatch = text.match(/(00[12]):([^\s]+店)/);
            if (storeMatch) out.current_store_code = storeMatch[1];
            // weekly reservations: find table with 月/日 + 件
            const tables = document.querySelectorAll("table");
            const week = [];
            for (const tbl of tables) {
                const txt = tbl.innerText;
                if (!/件/.test(txt) || !/\d+\/\d+/.test(txt)) continue;
                const allCells = Array.from(tbl.querySelectorAll("th, td")).map(c => c.innerText.trim());
                // pair date with count
                const dates = [];
                const counts = [];
                for (const c of allCells) {
                    if (/^\d+\/\d+/.test(c)) dates.push(c);
                    else if (/^\d+件$/.test(c)) counts.push(c);
                }
                for (let i = 0; i < dates.length && i < counts.length; i++) {
                    week.push({ date: dates[i], count: counts[i] });
                }
                if (week.length) break;
            }
            out.weekly_reservations = week;
            return out;
        }
    """)
    print(f"  ✓ today sales: {snapshot.get('today_sales')}, 来店 {snapshot.get('today_customers')}")
    return snapshot


def configure_and_download(page: Page, report: str, year_month: str, store_code: str, log_dir: Path) -> Path | None:
    """Open report page, configure (store/year/month/税抜), search, click CSV.
    Returns path to downloaded file or None.
    """
    print(f"→ {report}: store={store_code}, ym={year_month}")
    url = REPORT_URLS[report]
    page.goto(url, wait_until="networkidle", timeout=30000)
    time.sleep(2)

    # Configure year + month
    yyyy, mm = year_month[:4], year_month[4:6]
    # uriage form uses #search_year + #search_month selects
    # staff form uses #search_date_from + #search_date_to (YYYYMMDD)
    has_year_month = page.locator("#search_year").count() > 0
    has_date_range = page.locator("#search_date_from").count() > 0
    if has_year_month:
        page.select_option("#search_year", yyyy)
        page.select_option("#search_month", mm)
    if has_date_range:
        import calendar
        last_day = calendar.monthrange(int(yyyy), int(mm))[1]
        page.fill("#search_date_from", f"{yyyy}{mm}01")
        page.fill("#search_date_to", f"{yyyy}{mm}{last_day:02d}")

    # Configure 税抜 (tax_excluded). Default already 税抜 but enforce.
    try:
        page.check("#tax_excluded")
    except Exception:
        pass

    # Configure store: directly set checked via JS (the styled checkbox blocks normal clicks)
    page.evaluate(f"""
        (target) => {{
            document.querySelectorAll("input[type=checkbox][id^='store_search_check_']").forEach(cb => {{
                cb.checked = (cb.value === target);
            }});
        }}
    """, store_code)
    time.sleep(0.3)
    page.screenshot(path=str(log_dir / f"form_{report}_{store_code}_configured.png"))

    # Click 検索
    print("  click 検索")
    try:
        page.evaluate("reload_data(false)")
    except Exception:
        try:
            page.click("button:has-text('検索')")
        except Exception:
            page.get_by_text("検索").first.click()
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    # Wait for grid data + give time for icons to render. 8s padding is critical
    # (debug confirmed 8s succeeds reliably; shorter waits fail intermittently).
    time.sleep(8)
    page.screenshot(path=str(log_dir / f"results_{report}_{store_code}.png"), full_page=True)
    (log_dir / f"results_{report}_{store_code}.html").write_text(page.content(), encoding="utf-8")

    # CSV download icon: ui-icon-disk (16x16, bottom-left) — sigma_grid's save button.
    # Wait for the icon to appear (it only renders after data loads)
    try:
        page.wait_for_selector(".ui-icon.ui-icon-disk", timeout=20000)
    except Exception:
        print("  ! disk icon never appeared (no data?)")
        return None
    download = None
    try:
        with page.expect_download(timeout=30000) as dl_info:
            page.click(".ui-icon.ui-icon-disk", force=True)
        download = dl_info.value
        saved = log_dir / f"raw_{report}_{store_code}_{download.suggested_filename}"
        download.save_as(str(saved))
        print(f"  ✓ downloaded: {saved.name}")
        return saved
    except Exception as e:
        print(f"  ! disk icon click failed: {str(e)[:120]}")

    # Fallback: try the parent <a> if any
    try:
        with page.expect_download(timeout=20000) as dl_info:
            page.evaluate("document.querySelector('.ui-icon.ui-icon-disk').closest('a, button, [onclick]').click()")
        download = dl_info.value
        saved = log_dir / f"raw_{report}_{store_code}_{download.suggested_filename}"
        download.save_as(str(saved))
        print(f"  ✓ downloaded (via parent): {saved.name}")
        return saved
    except Exception as e:
        print(f"  ! parent click failed: {str(e)[:120]}")
    return None


def main():
    env = load_env()
    ym = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m")
    print(f"📅 target year-month: {ym}")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = LOGS / f"download_{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"📂 logs: {log_dir}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=300)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="ja-JP", timezone_id="Asia/Tokyo",
            accept_downloads=True,
        )
        page = ctx.new_page()
        try:
            login(page, env, log_dir)
            top = capture_top_snapshot(page, log_dir)
            (log_dir / "top_snapshot.json").write_text(json.dumps(top, ensure_ascii=False, indent=2), encoding="utf-8")
            (DATA / "uregi_top_snapshot.json").write_text(json.dumps(top, ensure_ascii=False, indent=2), encoding="utf-8")

            # Download all 4 CSVs
            for store in STORES:
                for report in ["uriage", "staff"]:
                    saved = configure_and_download(page, report, ym, store["code"], log_dir)
                    if saved:
                        # rename to canonical
                        dest_name = f"daily_sales_{ym}_{store['id']}.csv" if report == "uriage" else f"staff_ranking_{ym}_{store['id']}.csv"
                        dest = DATA / dest_name
                        shutil.copy(saved, dest)
                        print(f"  → data/{dest_name}")
        finally:
            time.sleep(2)
            browser.close()
    print("\n✅ done")


if __name__ == "__main__":
    main()
