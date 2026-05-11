#!/usr/bin/env python3
"""伝票明細 (取引明細) を Uレジ からスクレイプ。

取引粒度のデータを取得して、 真のリピート率分析・会員別来店間隔・支払方法分布を可能にする。

実行:
  python3 scripts/scrape_denpyo.py 20260501 20260511    # 日付範囲指定
  python3 scripts/scrape_denpyo.py 202605               # 当月 (1日〜今日)
  python3 scripts/scrape_denpyo.py 202604               # 過去月 (1日〜末日)

出力: data/denpyo_<YYYYMMDD-YYYYMMDD>_<store>.json

注意:
- 各取引行を 1 row として取得 (会員番号 / 会計担当 / 支払内訳 / 入退店時刻)
- 行数が多いので時間かかる (月数千件 × 店舗)
- pagination 対応 (UI に「次へ」 ボタンがあれば順次取得)
"""
from __future__ import annotations

import json
import sys
import time
import calendar
from datetime import datetime, date
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT / "scripts"))
from auto_download import load_env, login, STORES  # noqa: E402

DENPYO_URL = "https://owner-beauty.u-regi.com/sales_management/c_sales_management/denpyo"


def configure_filters(page, store_code: str, date_from: str, date_to: str):
    """店舗チェックボックス + 営業日範囲 を設定。"""
    # 店舗のチェックボックスをすべて offに → 対象だけ on
    page.evaluate(f"""
        () => {{
            const target = '{store_code}';
            document.querySelectorAll("input[type='checkbox']").forEach(cb => {{
                if (cb.value === '000' || cb.value === '001' || cb.value === '002') {{
                    cb.checked = (cb.value === target);
                    cb.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
            }});
        }}
    """)
    time.sleep(0.5)
    # 営業日 範囲入力
    page.evaluate(f"""
        () => {{
            const inputs = Array.from(document.querySelectorAll('input.datepicker, input[id*="business_date"]'));
            const visible = inputs.filter(i => i.offsetParent !== null);
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


def click_search(page):
    """検索ボタンをクリック。"""
    try:
        page.click("button:has-text('検索'), input[type='submit'][value='検索']", force=True)
    except Exception:
        page.evaluate("""
            () => {
                const btn = Array.from(document.querySelectorAll('button, input[type=submit]')).find(b => (b.innerText || b.value || '').includes('検索'));
                if (btn) btn.click();
            }
        """)


def scrape_rows(page) -> list[dict]:
    """伝票明細テーブルから 1ページ分のデータを抽出。"""
    return page.evaluate(r"""
        () => {
            // 伝票明細リスト table (店舗ごとに 1 row)
            const tables = document.querySelectorAll('table');
            for (const t of tables) {
                const headers = Array.from(t.querySelectorAll('th')).map(h => h.innerText.trim());
                if (headers.some(h => h.includes('伝票番号'))) {
                    const rows = [];
                    t.querySelectorAll('tbody tr').forEach(tr => {
                        const cells = Array.from(tr.querySelectorAll('td')).map(c => c.innerText.trim());
                        if (cells.length < 3) return;
                        rows.push({
                            denpyo_no: cells[0] ? cells[0].replace(/\s+/g, ' ') : '',
                            store_name: cells[1] || '',
                            status: cells[2] || '',
                            payment_info: cells[3] || '',
                            customer_info: cells[4] || '',
                            in_out_time: cells[5] || '',
                            // raw all-cells for debugging
                            _raw: cells,
                        });
                    });
                    return rows;
                }
            }
            return [];
        }
    """)


def parse_customer_info(text: str) -> dict:
    """お客様情報 text を構造化:
    '営業日:2026/05/08\n人数:1\n会員:山木 秀朝\n会計担当:水野 友里' → dict
    """
    out = {}
    for line in (text or "").split("\n"):
        line = line.strip()
        if ":" in line or "：" in line:
            sep = "：" if "：" in line else ":"
            k, v = line.split(sep, 1)
            k = k.strip()
            v = v.strip()
            key_map = {"営業日": "business_date", "人数": "headcount", "会員": "member_name", "会計担当": "staff"}
            out[key_map.get(k, k)] = v
    return out


def parse_payment_info(text: str) -> dict:
    """お支払情報 text を構造化:
    '現金:¥7,530\nクレジット:¥0\nポイント:¥0\nその他:¥0' → dict
    """
    out = {}
    for line in (text or "").split("\n"):
        line = line.strip()
        if ":" in line or "：" in line:
            sep = "：" if "：" in line else ":"
            k, v = line.split(sep, 1)
            v = v.replace("¥", "").replace(",", "").strip()
            try:
                v_num = int(v)
            except ValueError:
                v_num = 0
            key_map = {"現金": "cash", "クレジット": "credit", "ポイント": "points", "その他": "other", "合計": "total"}
            out[key_map.get(k, k)] = v_num
    return out


def scrape_store(page, store_code: str, store_id: str, date_from: str, date_to: str, log_dir: Path) -> list[dict]:
    print(f"  → {store_id} ({store_code}) {date_from}–{date_to}")
    page.goto(DENPYO_URL, wait_until="networkidle")
    time.sleep(2)
    configure_filters(page, store_code, date_from, date_to)
    time.sleep(0.5)
    click_search(page)
    time.sleep(5)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(1)
    page.screenshot(path=str(log_dir / f"{store_id}_{date_from}_{date_to}.png"), full_page=True)
    (log_dir / f"{store_id}_{date_from}_{date_to}.html").write_text(page.content(), encoding="utf-8")

    rows = scrape_rows(page)
    # Pagination: 「次へ」 ボタンがあれば続き
    page_count = 1
    while True:
        has_next = page.evaluate("""
            () => {
                const next = Array.from(document.querySelectorAll('a, button')).find(b => {
                    const t = (b.innerText || '').trim();
                    return (t === '次へ' || t === '>' || t === '›') && !b.disabled && b.offsetParent;
                });
                if (next) { next.click(); return true; }
                return false;
            }
        """)
        if not has_next: break
        page_count += 1
        time.sleep(3)
        new_rows = scrape_rows(page)
        if not new_rows: break
        rows.extend(new_rows)
        if page_count > 50:  # safety
            print(f"    ⚠️ 50ページ上限到達、 取得停止")
            break

    # Enrich rows with parsed structured data
    for r in rows:
        r["customer"] = parse_customer_info(r.get("customer_info", ""))
        r["payment"] = parse_payment_info(r.get("payment_info", ""))
        # Cleanup raw
        r.pop("_raw", None)

    print(f"    → {len(rows)} 件 ({page_count}ページ)")
    return rows


def main():
    if len(sys.argv) < 2:
        print("usage: scrape_denpyo.py <YYYYMMDD> <YYYYMMDD>  | <YYYYMM>")
        sys.exit(1)
    args = sys.argv[1:]
    today = date.today()
    if len(args) == 2 and len(args[0]) == 8 and len(args[1]) == 8:
        df, dt = args[0], args[1]
    elif len(args[0]) == 6 and args[0].isdigit():
        ym = args[0]
        y, m = int(ym[:4]), int(ym[4:6])
        last = calendar.monthrange(y, m)[1]
        df = f"{ym}01"
        target_end = date(y, m, last)
        dt = today.strftime("%Y%m%d") if target_end > today else f"{ym}{last:02d}"
    else:
        sys.exit("引数が不正です")

    env = load_env()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = LOGS / f"denpyo_{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)

    print(f"📅 伝票明細 scrape: {df}–{dt}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ja-JP", timezone_id="Asia/Tokyo")
        page = ctx.new_page()
        login(page, env, log_dir)

        for store in STORES:
            rows = scrape_store(page, store["code"], store["id"], df, dt, log_dir)
            out = {
                "store_id": store["id"],
                "store_code": store["code"],
                "date_from": df,
                "date_to": dt,
                "scraped_at": datetime.now().isoformat(timespec="seconds"),
                "row_count": len(rows),
                "rows": rows,
            }
            out_path = DATA / f"denpyo_{df}-{dt}_{store['id']}.json"
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  saved → {out_path.name}")

        time.sleep(2)
        browser.close()
    print("✅ done")


if __name__ == "__main__":
    main()
