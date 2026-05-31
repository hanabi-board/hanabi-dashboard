#!/usr/bin/env python3
"""月次振り返り文章を Claude CLI で自動生成。

使い方:
    python3 scripts/generate_summaries.py [YYYYMM]
      YYYYMM 省略時は前月を対象 (毎月1日に launchd から呼ばれる前提)
    python3 scripts/generate_summaries.py 202605 --force
      既に生成済でも再生成 (--force)
    python3 scripts/generate_summaries.py 202605 --section tsunashima
      特定セクションのみ生成

設計:
- docs/data.json を読んで セクションごとに 数字+前提+スタッフ動向 を組み立て
- claude CLI に prompt 投げて 3-5文 の振り返り文章を取得
- data/monthly_summaries.json に保存

claude CLI は subprocess で non-interactive 呼び出し (water 設定なし)。
launchd 環境では PATH に /Users/yoheimizuno/.npm-global/bin が必要。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DATA = ROOT / "docs" / "data.json"
SUMMARIES_FILE = DATA_DIR / "monthly_summaries.json"
JST = ZoneInfo("Asia/Tokyo")

# claude CLI のパス候補 (launchd 環境用)
CLAUDE_PATHS = [
    "/Users/yoheimizuno/.npm-global/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "claude",  # PATH から
]

SECTION_LABELS = {
    "company": "全社",
    "tsunashima": "Hanabi 綱島店 (ヘア/アイ)",
    "miyakojima": "ELLE 宮古島店 (ヘア・アイ・ネイル 統合)",
    "miyakojima_hair": "ELLE 宮古島店 ヘア部門",
    "miyakojima_eye": "ELLE 宮古島店 アイ部門",
    "miyakojima_nail": "ELLE 宮古島店 ネイル部門",
    "shinyokohama": "ナイスネイル 新横浜店 (ネイル)",
}

SECTION_ORDER = [
    "company",
    "tsunashima",
    "miyakojima",
    "miyakojima_hair",
    "miyakojima_eye",
    "miyakojima_nail",
    "shinyokohama",
]


def find_claude_cli() -> str:
    """claude CLI の実行パスを探す"""
    for p in CLAUDE_PATHS:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return p
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError("claude CLI が見つかりません。 npm i -g @anthropic-ai/claude-code してください")


def load_data() -> dict:
    if not DOCS_DATA.exists():
        raise FileNotFoundError(f"{DOCS_DATA} が見つかりません。 先に generate.py を実行してください")
    return json.loads(DOCS_DATA.read_text(encoding="utf-8"))


def load_summaries() -> dict:
    if not SUMMARIES_FILE.exists():
        return {"summaries": {}, "generated_at": {}, "model": {}}
    try:
        return json.loads(SUMMARIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"summaries": {}, "generated_at": {}, "model": {}}


def save_summaries(data: dict) -> None:
    data["_comment"] = "Claude CLI で月初に自動生成される 月次振り返り文章のキャッシュ。 scripts/generate_summaries.py が生成。 手動編集禁止 (上書きされます)。"
    data["_format"] = {
        "summaries": "{YYYYMM: {section_id: 振り返り文章}}",
        "generated_at": "{YYYYMM: ISO8601 タイムスタンプ}",
        "model": "{YYYYMM: 使用モデル}",
    }
    SUMMARIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fmt_yen(n: float) -> str:
    if n is None:
        return "—"
    if abs(n) >= 10_000_000:
        return f"{n/10_000_000:.2f}千万円"
    if abs(n) >= 10_000:
        return f"{n/10_000:.1f}万円"
    return f"¥{int(n):,}"


def prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[4:6])
    if m == 1:
        return f"{y - 1}12"
    return f"{y}{m - 1:02d}"


def build_company_stats(data: dict, month: str) -> str:
    """全社の数字データ"""
    lines = []
    total_sales = 0
    total_target = 0
    total_customers = 0
    store_lines = []
    for sid, sname in [("tsunashima", "綱島"), ("miyakojima", "宮古島"), ("shinyokohama", "新横浜")]:
        ms = (data.get("monthly_by_store", {}).get(sid, {}) or {}).get(month, {})
        if not ms:
            continue
        sales = ms.get("total_sales", 0)
        customers = ms.get("customers", 0)
        # 月予算 (annual / 12 簡易)
        b = data.get("budgets", {}).get("monthly", {}).get(sid, {}).get(month, 0)
        total_sales += sales
        total_target += b
        total_customers += customers
        avg = sales / customers if customers else 0
        store_lines.append(f"  - {sname}: 売上 {fmt_yen(sales)} / 予算 {fmt_yen(b)} (達成率 {sales/b*100:.0f}%) · 客数 {customers}名 · 客単価 ¥{int(avg):,}" if b else f"  - {sname}: 売上 {fmt_yen(sales)} · 客数 {customers}名 · 客単価 ¥{int(avg):,}")

    overall_pct = total_sales / total_target * 100 if total_target else 0
    lines.append(f"全社売上 {fmt_yen(total_sales)} / 全社予算 {fmt_yen(total_target)} (達成率 {overall_pct:.0f}%)")
    lines.append(f"全社客数 {total_customers}名")
    lines.append("店舗別内訳:")
    lines.extend(store_lines)

    # 採用ファネル
    recs = (data.get("recruitment", {}).get("candidates", []) or [])
    target_month_prefix = f"{month[:4]}-{month[4:6]}"
    monthly_apps = [c for c in recs if (c.get("applied_date") or "").startswith(target_month_prefix)]
    hires = [c for c in recs if c.get("status") == "入社"]
    lines.append(f"\n採用ファネル: {month[4:6].lstrip('0')}月応募 {len(monthly_apps)}件 / 全体内定〜入社 {len(hires)}件")
    if monthly_apps:
        sources = {}
        for c in monthly_apps:
            sources[c.get("source", "不明")] = sources.get(c.get("source", "不明"), 0) + 1
        lines.append(f"  経路内訳: " + ", ".join(f"{k}:{v}" for k, v in sources.items()))

    return "\n".join(lines)


def build_store_stats(data: dict, month: str, sid: str) -> str:
    """店舗の数字データ"""
    ms = (data.get("monthly_by_store", {}).get(sid, {}) or {}).get(month, {})
    if not ms:
        return "(当月データなし)"

    sales = ms.get("total_sales", 0)
    customers = ms.get("customers", 0)
    avg = sales / customers if customers else 0
    nom = ms.get("tech_customers_nominated", 0)
    free = ms.get("tech_customers_free", 0)
    total_tech = nom + free
    nom_rate = nom / total_tech * 100 if total_tech else 0
    budget = data.get("budgets", {}).get("monthly", {}).get(sid, {}).get(month, 0)

    # 前月比
    pm = prev_month(month)
    pms = (data.get("monthly_by_store", {}).get(sid, {}) or {}).get(pm, {})
    p_sales = pms.get("total_sales", 0) if pms else 0
    p_customers = pms.get("customers", 0) if pms else 0
    p_avg = p_sales / p_customers if p_customers else 0
    sales_yoy = sales / p_sales * 100 if p_sales else 0
    avg_diff = (avg - p_avg) / p_avg * 100 if p_avg else 0

    # リピート率
    vis = (data.get("visits_by_store", {}).get(sid, {}) or {}).get(month, {})
    repeat_rate = (vis.get("repeat", 0) / vis.get("customers", 1) * 100) if vis.get("customers") else 0

    lines = []
    lines.append(f"売上 {fmt_yen(sales)} / 月予算 {fmt_yen(budget)} (達成率 {sales/budget*100:.0f}%)" if budget else f"売上 {fmt_yen(sales)}")
    lines.append(f"客数 {customers}名 · 客単価 ¥{int(avg):,} (前月比 {avg_diff:+.1f}%)")
    if p_sales:
        lines.append(f"前月売上比 {sales_yoy:.0f}% (前月 {fmt_yen(p_sales)})")
    if total_tech:
        lines.append(f"指名率 {nom_rate:.0f}% (指名{nom}/全客{total_tech})")
    if vis.get("customers"):
        lines.append(f"リピート率 {repeat_rate:.0f}% (リピート{vis.get('repeat',0)}/全客{vis.get('customers',0)}, 新規{vis.get('new',0)})")

    return "\n".join(lines)


def build_dept_stats(data: dict, month: str, dept_label: str) -> str:
    """宮古島 部門別 数字"""
    ms = (data.get("monthly_by_store", {}).get("miyakojima", {}) or {}).get(month, {})
    if not ms:
        return "(当月データなし)"
    by_dept = ms.get("by_dept", {})
    d = by_dept.get(dept_label, {})
    sales = d.get("sales", 0)
    customers = d.get("customers", 0)
    avg = sales / customers if customers else 0
    budget = (data.get("budgets", {}).get("monthly_dept_miyakojima", {}) or {}).get(month, {}).get(dept_label, 0)
    pace_pct = sales / budget * 100 if budget else 0

    lines = []
    lines.append(f"{dept_label}部門 売上 {fmt_yen(sales)} / 部門予算 {fmt_yen(budget)} (達成率 {pace_pct:.0f}%)" if budget else f"{dept_label}部門 売上 {fmt_yen(sales)}")
    lines.append(f"{dept_label}部門 客数 {customers}名 · 客単価 ¥{int(avg):,}")

    # スタッフ別 部門売上 (top 3)
    staff_rows = [s for s in data.get("staff_rows", []) if s.get("month") == month and s.get("store") == "miyakojima" and not s.get("hidden")]
    dept_field = {"ヘア": "hair", "アイ": "eye", "ネイル": "nail"}.get(dept_label)
    if dept_field:
        dept_staff = [(s["name"], s.get(f"sales_{dept_field}", 0)) for s in staff_rows if s.get(f"sales_{dept_field}", 0) > 0]
        dept_staff.sort(key=lambda x: -x[1])
        if dept_staff:
            top = dept_staff[:3]
            lines.append(f"{dept_label}部門 売上TOP: " + ", ".join(f"{n}({fmt_yen(v)})" for n, v in top))

    return "\n".join(lines)


def build_cost_ratio_block(data: dict, month: str, section_id: str) -> str:
    """セクション別の原価率データ (規定値 + 実績 + 差分 + FY平均)"""
    cr = data.get("cost_ratios", {}) or {}
    if not cr.get("monthly"):
        return ""
    # cost_ratios の sid マッピング
    cr_sid_map = {
        "tsunashima": "tsunashima",
        "miyakojima": "miyakojima_total",
        "miyakojima_hair": "miyakojima_hair",
        "miyakojima_eye": "miyakojima_eye",
        "miyakojima_nail": "miyakojima_nail",
    }
    cr_sid = cr_sid_map.get(section_id)
    if not cr_sid:
        return ""  # company / shinyokohama (新横浜は原価管理対象外) は省略

    # FY 判定 (5月以降=当年FY、 1-4月=前年FY)
    y, m = int(month[:4]), int(month[4:6])
    fy_year = y if m >= 5 else y - 1
    fy = f"FY{fy_year % 100}"
    targets = (cr.get("targets") or {}).get(fy, {})
    fy_avg = (cr.get("fy_average") or {}).get(fy, {})
    md = (cr.get("monthly") or {}).get(month, {}).get(cr_sid, {})

    target_ratio = targets.get(cr_sid)
    actual_ratio = md.get("ratio")
    fy_avg_ratio = fy_avg.get(cr_sid)

    if target_ratio is None and actual_ratio is None:
        return ""

    lines = ["【原価率】"]
    if target_ratio is not None:
        lines.append(f"{fy} 規定値: {target_ratio*100:.1f}%")
    if actual_ratio is not None:
        diff = (actual_ratio - target_ratio) if target_ratio is not None else None
        diff_str = f" (規定比 {diff*100:+.1f}pt)" if diff is not None else ""
        lines.append(f"{m}月 実績: {actual_ratio*100:.2f}%{diff_str}")
        if md.get("sales") and md.get("cost") is not None:
            lines.append(f"  売上 ¥{md['sales']:,} / 原価 ¥{md['cost']:,}")
    if fy_avg_ratio is not None:
        lines.append(f"{fy} 平均: {fy_avg_ratio*100:.2f}%")
    if section_id == "miyakojima_hair":
        lines.append("※ヘアの原価率はエクステ除外後の値 (在庫管理表ベース)")
    return "\n".join(lines)


def detect_staff_changes(data: dict, month: str, sid: str | None = None) -> str:
    """当月の入退社を検知 (staff_profiles の joined/retired を見る)"""
    profiles = (data.get("staff_profiles", {}) or {}).get("profiles", {})
    if not isinstance(profiles, dict):
        return "(プロフィールデータなし)"
    ym_prefix = f"{month[:4]}-{month[4:6]}"
    joined = []
    retired = []
    for name, p in profiles.items():
        if not isinstance(p, dict):
            continue
        # sid 指定時は staff_rows から所属を確認 (main_store は profile に持たないため)
        if sid:
            attached = any(
                s.get("name") == name and s.get("store") == sid
                for s in data.get("staff_rows", [])
                if s.get("month") == month
            )
            if not attached:
                continue
        if (p.get("joined") or "").startswith(ym_prefix):
            joined.append(name)
        if (p.get("retired") or "").startswith(ym_prefix):
            retired.append(name)
    parts = []
    if joined:
        parts.append(f"今月入社: {', '.join(joined)}")
    if retired:
        parts.append(f"今月退職: {', '.join(retired)}")
    return "\n".join(parts) if parts else "(変動なし)"


def build_prompt(section_id: str, month: str, data: dict, context: str) -> str:
    """セクション別の Claude プロンプトを生成"""
    label = SECTION_LABELS.get(section_id, section_id)
    y, m = month[:4], int(month[4:6])

    if section_id == "company":
        stats = build_company_stats(data, month)
        staff_news = detect_staff_changes(data, month)
    elif section_id in ("tsunashima", "miyakojima", "shinyokohama"):
        stats = build_store_stats(data, month, section_id)
        staff_news = detect_staff_changes(data, month, section_id)
    elif section_id.startswith("miyakojima_"):
        dept_label = {"miyakojima_hair": "ヘア", "miyakojima_eye": "アイ", "miyakojima_nail": "ネイル"}[section_id]
        stats = build_dept_stats(data, month, dept_label)
        staff_news = ""  # dept レベルは省略
    else:
        return ""

    cost_block = build_cost_ratio_block(data, month, section_id)

    prompt = f"""あなたは美容サロン経営の戦略アナリスト。 経営者(水野陽平氏)向けに 月次振り返り文章を作成してください。

【対象】 {label}
【対象月】 {y}年{m}月

【前提コンテキスト (経営者が設定した戦略・人事方針)】
{context if context else "(未設定)"}

【数字データ】
{stats}

{cost_block if cost_block else ""}

【スタッフ動向】
{staff_news}

【出力ルール】
- 3〜5文 で簡潔に
- 数字 (売上、客数、客単価、達成率、前月比、原価率、等) を必ず1つ以上引用
- 原価率データがあれば 規定値との比較を1文加える (達成 or 超過、 構造的要因の所感込み)
- 前提コンテキストの戦略・人事方針を踏まえた所感を含める
- 改善アクションは 状況に応じて 必要な時のみ (無理に書かない)
- 太字 (**) や 箇条書き (- や *) や 見出し (#) は禁止、 普通の段落文で
- 「〜です」「〜ます」 調で
- 出力は本文のみ (前置き「以下振り返りです」 や 結語 不要)

振り返り文章:"""
    return prompt


def call_claude(prompt: str, claude_path: str, timeout: int = 120) -> str:
    """claude CLI を non-interactive で呼ぶ"""
    try:
        # stdin に prompt 流し込み、 -p フラグで non-interactive
        # --model sonnet (最新 sonnet)、 --output-format text (デフォルト)
        result = subprocess.run(
            [claude_path, "-p", "--model", "sonnet", "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            print(f"  ✗ claude CLI exit {result.returncode}: {result.stderr[:300]}", file=sys.stderr)
            return ""
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print(f"  ✗ claude CLI timeout ({timeout}秒)", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"  ✗ claude CLI error: {e}", file=sys.stderr)
        return ""


def main():
    parser = argparse.ArgumentParser(description="月次振り返り文章を Claude CLI で自動生成")
    parser.add_argument("month", nargs="?", help="対象月 YYYYMM (省略時は前月)")
    parser.add_argument("--force", action="store_true", help="既に生成済でも再生成")
    parser.add_argument("--section", help="特定セクションのみ生成")
    args = parser.parse_args()

    # 対象月決定
    if args.month:
        month = args.month
    else:
        now = datetime.now(JST)
        month = prev_month(f"{now.year}{now.month:02d}")
    if not (len(month) == 6 and month.isdigit()):
        print(f"✗ 不正な月: {month}", file=sys.stderr)
        sys.exit(1)

    print(f"==== 月次振り返り 自動生成 {month} ====")

    # claude CLI 確認
    claude_path = find_claude_cli()
    print(f"  claude CLI: {claude_path}")

    # データ読込
    data = load_data()
    summaries = load_summaries()
    contexts = (data.get("store_contexts", {}) or {}).get("contexts", {})

    summaries.setdefault("summaries", {}).setdefault(month, {})
    summaries.setdefault("generated_at", {})
    summaries.setdefault("model", {})

    target_secs = [args.section] if args.section else SECTION_ORDER
    generated_count = 0
    skipped_count = 0
    for sid in target_secs:
        if sid not in SECTION_LABELS:
            print(f"  ⚠ 未知のセクション: {sid}, skip")
            continue
        # 既に生成済なら skip (--force 時のみ再生成)
        if not args.force and summaries["summaries"][month].get(sid):
            print(f"  · {sid}: 既に生成済, skip (--force で再生成)")
            skipped_count += 1
            continue
        context = (contexts.get(sid) or "").strip()
        prompt = build_prompt(sid, month, data, context)
        if not prompt:
            print(f"  ⚠ {sid}: prompt 生成失敗")
            continue
        print(f"  → {sid}: Claude 呼出 (prompt {len(prompt)}字) ...", flush=True)
        text = call_claude(prompt, claude_path)
        if text:
            summaries["summaries"][month][sid] = text
            summaries["generated_at"][month] = datetime.now(JST).isoformat()
            summaries["model"][month] = "sonnet"
            generated_count += 1
            print(f"     ✓ {len(text)}字 生成")
        else:
            print(f"     ✗ 生成失敗 — 既存サマリ維持")

    save_summaries(summaries)
    print(f"==== 完了: 生成 {generated_count}件 / スキップ {skipped_count}件 ====")


if __name__ == "__main__":
    main()
