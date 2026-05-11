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
            // 口コミ件数 — HPB は '口コミ ◯件' フォーマット (ページ全体から探す)
            const fullText = document.body.innerText;
            const m1 = fullText.match(/口コミ[\s]*([0-9,]+)\s*件/);
            if (m1) out.review_count = parseInt(m1[1].replace(/,/g, ''));
            // ブログ件数 (参考)
            const m2 = fullText.match(/ブログ[\s]*([0-9,]+)\s*件/);
            if (m2) out.blog_count = parseInt(m2[1].replace(/,/g, ''));
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
    Note: Instagram は bot 検出が厳しい。 公開HTMLの og:description / meta description /
    本文中の数値 から フォロワー数取得を試みる。
    """
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(4)
    return page.evaluate(r"""
        () => {
            const out = {};
            // username
            const url = location.pathname;
            const m = url.match(/^\/([^\/]+)/);
            if (m) out.username = m[1];
            // 1) meta description (英語): "456 Followers, 123 Following, 789 Posts"
            // 2) meta og:description (日本語版あり): "フォロワー456人 ..."
            const metas = document.querySelectorAll('meta[name="description"], meta[property="og:description"]');
            for (const meta of metas) {
                const c = meta.getAttribute('content') || '';
                // 英語パターン
                const folEn = c.match(/([\d,\.]+[KMkm]?)\s*Followers?/i);
                const fwgEn = c.match(/([\d,\.]+[KMkm]?)\s*Following/i);
                const postEn = c.match(/([\d,\.]+[KMkm]?)\s*Posts?/i);
                // 日本語パターン (人/件)
                const folJa = c.match(/フォロワー(\d+\.?\d*[万千]?)/);
                const fwgJa = c.match(/フォロー中(\d+\.?\d*[万千]?)/);
                const postJa = c.match(/(\d+\.?\d*[万千]?)\s*件の投稿/);
                const parseNum = s => {
                    if (!s) return null;
                    s = s.replace(/,/g, '').trim();
                    let mult = 1;
                    if (/k$/i.test(s)) { mult = 1000; s = s.replace(/k$/i, ''); }
                    else if (/m$/i.test(s)) { mult = 1000000; s = s.replace(/m$/i, ''); }
                    else if (s.endsWith('万')) { mult = 10000; s = s.replace('万', ''); }
                    else if (s.endsWith('千')) { mult = 1000; s = s.replace('千', ''); }
                    const n = parseFloat(s);
                    return isNaN(n) ? null : Math.round(n * mult);
                };
                if (!out.followers && (folEn || folJa)) out.followers = parseNum((folEn || folJa)[1]);
                if (!out.following && (fwgEn || fwgJa)) out.following = parseNum((fwgEn || fwgJa)[1]);
                if (!out.posts && (postEn || postJa)) out.posts = parseNum((postEn || postJa)[1]);
            }
            // meta の値が取れなかった場合 ページのDOM内 (header section の数値) を試す
            if (!out.followers) {
                const lis = document.querySelectorAll('header ul li, header section li');
                for (const li of lis) {
                    const txt = li.innerText || '';
                    const m = txt.match(/([\d,\.]+[KMkm万千]?)\s*(?:followers?|フォロワー)/i);
                    if (m) {
                        const s = m[1].replace(/,/g, '');
                        let mult = 1, val = s;
                        if (/k$/i.test(s)) { mult = 1000; val = s.replace(/k$/i, ''); }
                        else if (s.endsWith('万')) { mult = 10000; val = s.replace('万', ''); }
                        const n = parseFloat(val);
                        if (!isNaN(n)) { out.followers = Math.round(n * mult); break; }
                    }
                }
            }
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
                url_field = urls.get(source, "")
                # 配列 or 文字列の両方対応 (hotpepper は複数掲載 listing 想定)
                url_list = []
                if isinstance(url_field, list):
                    url_list = [u.strip() for u in url_field if u and isinstance(u, str)]
                elif isinstance(url_field, str) and url_field.strip():
                    url_list = [url_field.strip()]
                if not url_list:
                    print(f"  {store_id}: skip (URL未設定)")
                    continue
                # 1店舗で複数URL ある場合は全件 scrape して 配列で保存
                listings = []
                for i, url in enumerate(url_list):
                    print(f"  {store_id}[{i}]: {url}")
                    try:
                        if source == "hotpepper":
                            data = scrape_hotpepper(page, url)
                        elif source == "instagram":
                            data = scrape_instagram(page, url)
                        else:
                            data = {}
                        data["url"] = url
                        print(f"    → rating={data.get('rating')}, reviews={data.get('review_count')}, reviews_fetched={len(data.get('recent_reviews',[]))}" if source == "hotpepper" else f"    → {data}")
                        listings.append(data)
                        page.screenshot(path=str(log_dir / f"{source}_{store_id}_{i}.png"), full_page=False)
                    except Exception as e:
                        print(f"    ⚠️ failed: {e}")
                        listings.append({"url": url, "error": str(e)})
                # 後方互換: 1件なら dict, 複数なら 1件目をベース + listings 配列
                if len(listings) == 1:
                    result["stores"][store_id] = listings[0]
                else:
                    primary = dict(listings[0])  # 1件目をコピー (循環参照回避)
                    primary["listings"] = listings  # 全件 (配列)
                    primary["total_review_count"] = sum((l.get("review_count") or 0) for l in listings)
                    # ratings の単純平均 (重み付け改善余地あり)
                    rating_vals = [l.get("rating") for l in listings if l.get("rating") is not None]
                    if rating_vals:
                        primary["avg_rating"] = sum(rating_vals) / len(rating_vals)
                    result["stores"][store_id] = primary
            out_path = DATA / f"external_{source}_{date_str}.json"
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  saved → {out_path.name}")

        time.sleep(1)
        browser.close()
    print("\n✅ done")


if __name__ == "__main__":
    main()
