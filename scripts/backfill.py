#!/usr/bin/env python3
"""指定した複数月のCSVを一気にDLするバックフィルスクリプト。

実行例:
  # FY25 (2025/05〜2026/04) を全部DL
  python3 scripts/backfill.py 202505 202604

  # 特定月だけ
  python3 scripts/backfill.py 202508 202508
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

# Reuse functions from auto_download.py
sys.path.insert(0, str(ROOT / "scripts"))
from auto_download import load_env, login, configure_and_download, STORES  # noqa: E402


def iter_months(start_ym: str, end_ym: str):
    sy, sm = int(start_ym[:4]), int(start_ym[4:6])
    ey, em = int(end_ym[:4]), int(end_ym[4:6])
    while (sy, sm) <= (ey, em):
        yield f"{sy:04d}{sm:02d}"
        sm += 1
        if sm > 12:
            sm = 1
            sy += 1


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: backfill.py START_YYYYMM END_YYYYMM (e.g. 202505 202604)")
    start_ym, end_ym = sys.argv[1], sys.argv[2]
    months = list(iter_months(start_ym, end_ym))
    print(f"📅 backfill: {len(months)} months  ({months[0]} → {months[-1]})")

    env = load_env()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = LOGS / f"backfill_{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)

    summary = {"success": [], "fail": []}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="ja-JP", timezone_id="Asia/Tokyo",
            accept_downloads=True,
        )
        page = ctx.new_page()
        try:
            login(page, env, log_dir)
            for ym in months:
                for store in STORES:
                    for report in ["uriage", "staff"]:
                        try:
                            saved = configure_and_download(page, report, ym, store["code"], log_dir)
                            if saved:
                                dest_name = f"daily_sales_{ym}_{store['id']}.csv" if report == "uriage" else f"staff_ranking_{ym}_{store['id']}.csv"
                                dest = DATA / dest_name
                                shutil.copy(saved, dest)
                                summary["success"].append(dest_name)
                                print(f"    ✓ {dest_name}")
                            else:
                                summary["fail"].append(f"{ym}/{store['id']}/{report}")
                                print(f"    ✗ no download for {ym}/{store['id']}/{report}")
                        except Exception as e:
                            summary["fail"].append(f"{ym}/{store['id']}/{report}: {str(e)[:60]}")
                            print(f"    ✗ error {ym}/{store['id']}/{report}: {e}")
        finally:
            time.sleep(2)
            browser.close()

    print(f"\n=== Summary ===")
    print(f"  success: {len(summary['success'])}")
    print(f"  fail:    {len(summary['fail'])}")
    for f in summary["fail"][:10]:
        print(f"    - {f}")
    (log_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
