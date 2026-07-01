#!/usr/bin/env python3
"""
Notion 採用管理表 → data/recruitment.json 同期 (本番版)

2026-07-01 本番化。 Notion「株式会社HANABI 採用管理表」 を正 (source of truth) として、
ダッシュボードの採用ファネルに反映する。

設計:
  - Notion に追加した「ダッシュボード表示」 select 列 (応募/書類選考/面接調整中/面接日確定/
    内定/入社/辞退/不採用) を そのまま recruitment.json の status に流す (変換ロスなし)。
  - 既存の「採用状況(実施前/検討中/採用/不採用)」「内定」 は面接プロセスの詳細管理用にNotion側で維持。
  - 「ダッシュボード表示」 が未設定の候補者は 同期対象外 (= ダッシュボードに出さない)。
  - 入社後の 入社日・プロフィール・スタッフ登録は ダッシュボード側 (staff_profiles) で従来通り編集。

認証: .env の NOTION_TOKEN (Notion 内部インテグレーション、 採用管理表DBに共有済)

使い方:
  python3 scripts/sync_recruitment_from_notion.py          # 本番 recruitment.json を更新
  python3 scripts/sync_recruitment_from_notion.py --dry-run # 更新せず 差分だけ表示
"""
import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
OUT = ROOT / "data" / "recruitment.json"
DB_ID = "45cab31f37ef830ea331017ecbfd1da6"  # 株式会社HANABI 採用管理表

# 表記ゆれ吸収: Notion の値 → ダッシュボード recruitment.json の値
STORE_MAP = {
    "Hanabi綱島店": "Hanabi 綱島店",
    "ELLE by Hanabi宮古島店": "ELLE by Hanabi 宮古島店",
    "Hanabi新店舗": "アイラッシュ新店舗",
    "希望なし": "未定",
}
ROLE_MAP = {
    "スタイリスト": "ヘア（スタイリスト）",
    "アシスタント（ヘア）": "ヘア（アシスタント）",
    "業務委託（ヘア）": "ヘア（スタイリスト）",
    "業務委託（アイ）": "アイリスト",
    "アイリスト": "アイリスト",
    "ネイリスト": "ネイリスト",
}
SOURCE_MAP = {
    "ビューティーワーク": "Beauty Work",
    "リジョブ": "リジョブ",
    "リファラル": "リファラル",
    "その他": "その他",
}
# 経験 区分 → 年数 (代表値)。 Notion は区分select、 ダッシュボードは数値
EXP_MAP = {
    "未経験": 0, "未経験（新卒）": 0, "1年〜3年": 2, "4年〜10年": 5,
}


def load_token() -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("NOTION_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("NOTION_TOKEN が .env にありません")


def notion_query_all(token: str) -> list:
    """採用管理表 全ページを取得 (ページネーション対応)"""
    results = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{DB_ID}/query",
            data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        results.extend(d.get("results", []))
        if not d.get("has_more"):
            break
        cursor = d.get("next_cursor")
    return results


def _title(prop):
    t = (prop or {}).get("title", [])
    return t[0]["plain_text"] if t else ""


def _select(prop):
    s = (prop or {}).get("select")
    return s["name"] if s else ""


def _multi_first(prop):
    m = (prop or {}).get("multi_select", [])
    return m[0]["name"] if m else ""


def _rich(prop):
    r = (prop or {}).get("rich_text", [])
    return "".join(x.get("plain_text", "") for x in r)


def _date(prop):
    dt = (prop or {}).get("date")
    return (dt or {}).get("start", "") if dt else ""


def _slug_id(name: str) -> str:
    return "nk_" + re.sub(r"\s+", "", name)


def transform(pages: list) -> list:
    candidates = []
    for pg in pages:
        p = pg.get("properties", {})
        name = _title(p.get("候補者名")).strip()
        disp = _select(p.get("ダッシュボード表示")).strip()
        if not name or not disp:
            continue  # ダッシュボード表示 未設定は 同期対象外
        store_n = _multi_first(p.get("希望店舗"))
        role_n = _select(p.get("希望職種"))
        src_n = _select(p.get("応募経路"))
        exp_n = _select(p.get("経験"))
        candidates.append({
            "id": _slug_id(name),
            "applied_date": _date(p.get("面接日時")),  # Notionに応募日列がないため面接日時を暫定
            "name": name,
            "role": ROLE_MAP.get(role_n, role_n),
            "store_pref": STORE_MAP.get(store_n, store_n),
            "experience_years": EXP_MAP.get(exp_n, ""),
            "source": SOURCE_MAP.get(src_n, src_n),
            "status": disp,  # ★ ダッシュボード表示 をそのまま (変換なし)
            "notes": _rich(p.get("備考")),
            "notion_url": (pg.get("url") or "").replace("app.notion.com", "www.notion.so"),
        })
    # status のファネル順でソート (応募→...→入社→辞退→不採用)
    order = {s: i for i, s in enumerate(
        ["応募", "書類選考", "面接調整中", "面接日確定", "内定", "入社", "辞退", "不採用"])}
    candidates.sort(key=lambda c: (order.get(c["status"], 99), c["name"]))
    return candidates


def main():
    dry = "--dry-run" in sys.argv
    token = load_token()
    pages = notion_query_all(token)
    candidates = transform(pages)

    base = json.load(open(OUT, encoding="utf-8"))
    new_data = {
        "_comment": "Notion「株式会社HANABI 採用管理表」から同期生成 (sync_recruitment_from_notion.py)。 編集はNotion側で。",
        "_settings": base.get("_settings", {}),
        "candidates": candidates,
    }

    print(f"Notion 取得: {len(pages)}件 → 同期対象 (ダッシュボード表示あり): {len(candidates)}件")
    for c in candidates:
        print(f"  {c['name']:14} {c['status']:8} {c.get('role',''):16} {c.get('store_pref','')}")

    if dry:
        # 既存との差分
        old_names = {c["name"]: c["status"] for c in base.get("candidates", [])}
        print("\n--- 差分 (現行 recruitment.json との比較) ---")
        for c in candidates:
            old = old_names.get(c["name"])
            if old is None:
                print(f"  + 追加: {c['name']} ({c['status']})")
            elif old != c["status"]:
                print(f"  ~ 変更: {c['name']} {old} → {c['status']}")
        for n in old_names:
            if n not in {c["name"] for c in candidates}:
                print(f"  - 削除: {n}")
        print("\n(dry-run: recruitment.json は更新していません)")
        return

    OUT.write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ {OUT} を更新 ({len(candidates)}件)")


if __name__ == "__main__":
    main()
