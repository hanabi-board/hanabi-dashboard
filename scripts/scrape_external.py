#!/usr/bin/env python3
"""外部メディア (HotPepper Beauty / Instagram) からの指標をスクレイプ。

実行:
  python3 scripts/scrape_external.py hotpepper       # 全店舗 HotPepper評価
  python3 scripts/scrape_external.py instagram       # 全店舗 IG フォロワー数
  python3 scripts/scrape_external.py all             # 両方

設定:
  data/external_targets.json (店舗別 URL を管理):
  {
    "tsunashima": {
      "hotpepper": "https://beauty.hotpepper.jp/...",
      "instagram": "https://www.instagram.com/hanabi_tsunashima/"
    },
    "miyakojima": {
      "hotpepper": "https://beauty.hotpepper.jp/...",
      "instagram": "https://www.instagram.com/elle_by_hanabi/"
    }
  }

出力: data/external_<source>_<YYYYMMDD>.json
  履歴を残すことで時系列推移を取れる。
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)


def load_targets() -> dict:
    p = DATA / "external_targets.json"
    if not p.exists():
        # Create template
        template = {
            "_comment": "店舗別の外部メディア URL を管理。 URL未設定の店舗は scrape スキップ。",
            "tsunashima": {
                "hotpepper": "",
                "instagram": ""
            },
            "miyakojima": {
                "hotpepper": "",
                "instagram": ""
            }
        }
        p.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"⚠️ created template at {p} — URL を手動入力後に再実行してください")
        sys.exit(0)
    return json.loads(p.read_text(encoding="utf-8"))


def scrape_hotpepper(page, url: str) -> dict:
    """HotPepper Beauty の店舗ページから 評価 + 口コミ件数 + 直近口コミ を取得。"""
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(2)
    result = page.evaluate(r"""
        () => {
            const out = {};
            // 評価点 (例: 4.7)
            const rateEl = document.querySelector('.point') || document.querySelector('[class*="rating"]') || document.querySelector('.s_rate');
            if (rateEl) {
                const m = rateEl.textContent.match(/[\d.]+/);
                if (m) out.rating = parseFloat(m[0]);
            }
            // 口コミ件数
            const cntEl = document.querySelector('[class*="review"]') || document.querySelector('.comeReview');
            if (cntEl) {
                const m = cntEl.textContent.match(/(\d+)/);
                if (m) out.review_count = parseInt(m[1]);
            }
            // 店舗名
            const nameEl = document.querySelector('h1, .salon-name, [class*="salonName"]');
            if (nameEl) out.salon_name = nameEl.textContent.trim().slice(0, 50);
            return out;
        }
    """)
    # 口コミタブへ移動して最新口コミ N件取得 (URL末尾に /review/ を付ければレビュータブ)
    try:
        review_url = url.rstrip("/") + "/review/"
        page.goto(review_url, wait_until="domcontentloaded", timeout=15000)
        time.sleep(2)
        reviews = page.evaluate(r"""
            () => {
                // 口コミブロック (HotPepperのHTML構造に依存)
                const blocks = document.querySelectorAll('[class*="review"], .reviewBlock, .cFix > section');
                const out = [];
                for (const b of blocks) {
                    const textEl = b.querySelector('p, .reviewBody, [class*="reviewText"]');
                    const dateEl = b.querySelector('time, [class*="date"], .reviewDate');
                    const ratingEl = b.querySelector('[class*="rating"], .reviewRate');
                    if (!textEl) continue;
                    const text = textEl.textContent.trim().slice(0, 200);
                    if (!text || text.length < 10) continue;
                    out.push({
                        text,
                        date: dateEl ? dateEl.textContent.trim().slice(0, 20) : "",
                        rating: ratingEl ? parseFloat((ratingEl.textContent.match(/[\d.]+/) || [""])[0]) || null : null,
                    });
                    if (out.length >= 10) break;
                }
                return out;
            }
        """)
        result["recent_reviews"] = reviews
    except Exception as e:
        print(f"    warn: reviews fetch failed: {e}")
        result["recent_reviews"] = []
    return result


def scrape_instagram(page, url: str) -> dict:
    """Instagram の公開プロフィールページから フォロワー数 / 投稿数 を取得。
    Note: Instagram は bot 検出が厳しい。 ログインなしの public view から
    meta タグ経由でフォロワー数取得できる場合のみ動作。
    """
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(3)
    return page.evaluate(r"""
        () => {
            const out = {};
            // meta description 例: "456 Followers, 123 Following, 789 Posts - ..."
            const meta = document.querySelector('meta[name="description"]');
            if (meta) {
                const c = meta.getAttribute('content') || '';
                const fol = c.match(/([\d,]+)\s*Followers?/i);
                const fwg = c.match(/([\d,]+)\s*Following/i);
                const post = c.match(/([\d,]+)\s*Posts?/i);
                if (fol) out.followers = parseInt(fol[1].replace(/,/g, ''));
                if (fwg) out.following = parseInt(fwg[1].replace(/,/g, ''));
                if (post) out.posts = parseInt(post[1].replace(/,/g, ''));
            }
            // username
            const url = location.pathname;
            const m = url.match(/^\/([^\/]+)/);
            if (m) out.username = m[1];
            return out;
        }
    """)


def main():
    if len(sys.argv) < 2:
        print("usage: scrape_external.py <hotpepper|instagram|all>")
        sys.exit(1)
    target = sys.argv[1]
    if target not in ("hotpepper", "instagram", "all"):
        sys.exit(f"unknown target: {target}")

    targets = load_targets()
    sources = ["hotpepper", "instagram"] if target == "all" else [target]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    date_str = datetime.now().strftime("%Y%m%d")
    log_dir = LOGS / f"external_{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # 公開ページなので headless OK
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="ja-JP",
                                  user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
        page = ctx.new_page()

        for source in sources:
            print(f"\n📅 scraping {source}")
            result = {"source": source, "scraped_at": datetime.now().isoformat(timespec="seconds"), "stores": {}}
            for store_id, urls in targets.items():
                if store_id.startswith("_"):
                    continue
                url = urls.get(source, "").strip()
                if not url:
                    print(f"  {store_id}: skip (URL未設定)")
                    continue
                print(f"  {store_id}: {url}")
                try:
                    if source == "hotpepper":
                        data = scrape_hotpepper(page, url)
                    elif source == "instagram":
                        data = scrape_instagram(page, url)
                    else:
                        data = {}
                    print(f"    → {data}")
                    result["stores"][store_id] = data
                    page.screenshot(path=str(log_dir / f"{source}_{store_id}.png"), full_page=False)
                except Exception as e:
                    print(f"    ⚠️ failed: {e}")
                    result["stores"][store_id] = {"error": str(e)}
            out_path = DATA / f"external_{source}_{date_str}.json"
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  saved → {out_path.name}")

        time.sleep(1)
        browser.close()
    print("\n✅ done")


if __name__ == "__main__":
    main()
