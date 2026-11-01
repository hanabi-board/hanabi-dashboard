#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Uレジ 情報分析「再来店の実績」を 担当者別 にスクレイプ (2026-11-01 新設)

ある月 (コホート月) に来店した客が、 その後 N ヶ月以内に何人戻ったか を
客の区分 (新規 / 新規2回目 / 新規3回目 / 再来4〜9回 / 常連10回以上 / 総リピート / リターン) ×
担当者 ごとに取得する。 ダッシュボード側で担当者を部門 (ヘア/アイ/ネイル) に束ねて
「部門別 客のつき具合 (定着率・離脱率・新規比率)」 を出すための元データ。

実行:
  python3 scripts/scrape_retention.py 202609                # 両店舗・6ヶ月窓
  python3 scripts/scrape_retention.py 202609 --store 002    # ELLE のみ
  python3 scripts/scrape_retention.py 202609 --term 3       # 3ヶ月窓

出力: data/retention_<YYYYMM>_<store_id>.json
  {
    "cohort_month": "202609", "store": "miyakojima", "store_code": "002", "term_months": 6,
    "scraped_at": "...", "staff": [ { "code": "0202", "name": "佐久間 理加",
        "segments": { "新規": {"n": 20, "cum": {"202609": 0, "202610": 2, ...}, "no_visit": 18}, ... } }, ...
      + code "ALL" (全担当者 = 店舗合計) ]
  }
注意: 窓が閉じていない月 (まだ来ていない将来月) の cum は 0 のまま入る。 「N ヶ月定着率」 は
      cohort_month + N ヶ月 が完了しているコホートだけで計算すること (generate.py 側で判定)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
sys.path.insert(0, str(ROOT / "scripts"))
from auto_download import load_env, login, STORES  # noqa: E402

URL = "https://owner-beauty.u-regi.com/repeat_result/c_repeat_result/view"
SEGMENTS = ["新規", "新規2回目", "新規3回目", "再来(4回～9回)", "常連客(10回以上)", "総リピート", "リターン"]
TERM_LABEL = {1: "1ヶ月", 2: "2ヶ月", 3: "3ヶ月", 4: "4ヶ月", 5: "5ヶ月", 6: "6ヶ月", 12: "12ヶ月"}
JST = timezone(timedelta(hours=9))


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", t or "")


def read_tables(page) -> list[list[list[str]]]:
    return page.evaluate("""() => Array.from(document.querySelectorAll('table')).map(t =>
        Array.from(t.querySelectorAll('tr')).map(tr => Array.from(tr.querySelectorAll('th,td')).map(c => (c.innerText||'').trim()))
    )""")


def parse_result(tables: list[list[list[str]]]) -> dict | None:
    """表示後のテーブル群 → {segment: {n, cum{ym: count}, no_visit}}。 見つからなければ None"""
    # コホート表: 1行目が ["項目", "2026年09月 ..."], 3行目以降が 区分, 人数, -, -
    cohort = None
    month_tables = []   # (ym, rows)
    no_visit = None
    for rows in tables:
        if not rows or not rows[0]:
            continue
        head = rows[0][0] if rows[0] else ""
        head2 = rows[0][1] if len(rows[0]) > 1 else ""
        if head == "項目" and re.search(r"(\d{4})年(\d{2})月", head2) and len(rows) >= 9:
            body = rows[2:9]
            if all(_norm(r[0]) == _norm(seg) for r, seg in zip(body, SEGMENTS)) and cohort is None:
                m = re.search(r"(\d{4})年(\d{2})月", head2)
                cohort = {"ym": m.group(1) + m.group(2), "n": [int(r[1].replace(",", "") or 0) for r in body]}
            continue
        # ページ下部に空の比較ブロック (同じ構造・全部0) がもう1組あるので、
        # 最初の「来店なし」表を読んだ時点で打ち切る (2026-11-01 二重計上/上書きバグの修正)
        if no_visit is not None:
            break
        m = re.match(r"(\d{4})年(\d{2})月", head)
        if m and len(rows) >= 9 and rows[1][:3] == ["人数", "累計", "再来率"]:
            body = rows[2:9]
            # 列: 人数 (その月に戻った人数) / 累計 (コホート月からの累計) / 再来率 → 累計列をそのまま使う
            month_tables.append((m.group(1) + m.group(2), [int(r[1].replace(",", "") or 0) for r in body], [int(r[0].replace(",", "") or 0) for r in body]))
            continue
        if head.startswith("同月リピート") and len(rows) >= 9 and rows[1][:3] == ["人数", "累計", "再来率"]:
            body = rows[2:9]
            month_tables.append(("same", [int(r[1].replace(",", "") or 0) for r in body], [int(r[0].replace(",", "") or 0) for r in body]))
            continue
        if head == "来店なし" and len(rows) >= 9 and rows[1][:2] == ["人数", "比率"]:
            body = rows[2:9]
            no_visit = [int(r[0].replace(",", "") or 0) for r in body]
    if cohort is None:
        return None
    out = {}
    for i, seg in enumerate(SEGMENTS):
        cum, monthly = {}, {}
        for ym, cums, counts in month_tables:
            key = cohort["ym"] if ym == "same" else ym
            cum[key] = cums[i]
            monthly[key] = counts[i]
        out[seg] = {"n": cohort["n"][i], "cum": cum, "monthly": monthly, "no_visit": (no_visit[i] if no_visit else None)}
    return {"cohort_ym": cohort["ym"], "segments": out}


def click_show(page):
    page.click("button:has-text('表示')", timeout=10000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    time.sleep(1.5)


def scrape_store(page, store: dict, ym: str, term: int, log_dir: Path) -> dict:
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    time.sleep(1.5)
    # 店舗選択 → 担当者リスト再読込
    page.select_option("#store_code", store["code"])
    time.sleep(2)
    page.select_option("#year", label=f"{ym[:4]}年")
    page.select_option("#month", label=f"{int(ym[4:6])}月")
    page.select_option("#term", label=TERM_LABEL[term])
    staff_opts = page.evaluate("() => Array.from(document.querySelectorAll('#user_id option')).map(o => ({value: o.value, text: o.text}))")
    targets = [{"code": "ALL", "name": "全担当者", "value": ""}] + [
        {"code": o["value"], "name": (o["text"].split("：", 1)[1] if "：" in o["text"] else o["text"]).strip(), "value": o["value"]}
        for o in staff_opts if o["value"]
    ]
    result = {"cohort_month": ym, "store": store["id"], "store_code": store["code"], "term_months": term,
              "scraped_at": datetime.now(JST).isoformat(timespec="seconds"), "staff": []}
    for t in targets:
        for attempt in range(2):
            try:
                page.select_option("#user_id", t["value"])
                click_show(page)
                parsed = parse_result(read_tables(page))
                if parsed is None:
                    raise RuntimeError("結果表が見つからない")
                result["staff"].append({"code": t["code"], "name": t["name"], **parsed})
                total = parsed["segments"]["総リピート"]
                print(f"  ✓ {store['id']} {ym} {t['name']:<10} 総{total['n']:>4}名 cum={list(total['cum'].values())}", flush=True)
                break
            except Exception as e:
                print(f"  ⚠️ {store['id']} {ym} {t['name']} 試行{attempt+1}: {type(e).__name__}: {str(e)[:80]}", flush=True)
                page.screenshot(path=str(log_dir / f"err_{store['id']}_{ym}_{t['code']}.png"))
                if attempt == 0:
                    page.goto(URL, wait_until="domcontentloaded", timeout=45000); time.sleep(1.5)
                    page.select_option("#store_code", store["code"]); time.sleep(2)
                    page.select_option("#year", label=f"{ym[:4]}年"); page.select_option("#month", label=f"{int(ym[4:6])}月")
                    page.select_option("#term", label=TERM_LABEL[term])
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("months", nargs="+", help="コホート月 YYYYMM (複数可)")
    ap.add_argument("--store", help="店舗コード 001/002 (省略時は両方)")
    ap.add_argument("--term", type=int, default=6, choices=sorted(TERM_LABEL))
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    stores = [s for s in STORES if not args.store or s["code"] == args.store]
    env = load_env()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = LOGS / f"retention_{ts}"; log_dir.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(exist_ok=True)
    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ja-JP", timezone_id="Asia/Tokyo")
        page = ctx.new_page()
        try:
            login(page, env, log_dir)
            for ym in args.months:
                for store in stores:
                    try:
                        res = scrape_store(page, store, ym, args.term, log_dir)
                        out = DATA / f"retention_{ym}_{store['id']}.json"
                        out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
                        print(f"→ {out.relative_to(ROOT)} ({len(res['staff'])}名)", flush=True)
                    except Exception as e:
                        failures.append(f"{store['id']}/{ym}: {type(e).__name__}: {str(e)[:100]}")
                        print(f"  ❌ {store['id']} {ym}: {e}", flush=True)
        finally:
            browser.close()
    if failures:
        print("\n❌ 失敗:", *failures, sep="\n  ")
        sys.exit(1)
    print("\n✅ done")


if __name__ == "__main__":
    main()
