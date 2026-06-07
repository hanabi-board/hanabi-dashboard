#!/usr/bin/env python3
"""LINE WORKS Bot 通知モジュール (HANABI / ナイスネイルFC 両対応)

設定ファイル: ~/.config/lineworks/<project>/config.json
  {
    "client_id": "...",
    "client_secret": "...",
    "service_account": "...",
    "bot_id": "...",
    "channel_id": "..."
  }

秘密鍵: ~/.config/lineworks/<project>/private.key (chmod 600)

使い方:
  python3 notify_lineworks.py <project> success
  python3 notify_lineworks.py <project> failure <step> <error_message> [log_file]
  python3 notify_lineworks.py <project> test

<project>: hanabi | nicenail
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import jwt as pyjwt
except ImportError:
    print("✗ PyJWT 未インストール: pip3 install PyJWT cryptography", file=sys.stderr)
    sys.exit(2)

JST = ZoneInfo("Asia/Tokyo")
CONFIG_BASE = Path.home() / ".config" / "lineworks"
SEP = "━━━━━━━━━━━━━━━━━"


# ===================== 共通: 認証 + 送信 =====================
def load_lineworks_config(project: str) -> dict:
    project_dir = CONFIG_BASE / project
    cfg_path = project_dir / "config.json"
    key_path = project_dir / "private.key"
    if not cfg_path.exists():
        raise FileNotFoundError(f"config 未配置: {cfg_path}")
    if not key_path.exists():
        raise FileNotFoundError(f"private key 未配置: {key_path}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["_private_key"] = key_path.read_text(encoding="utf-8")
    return cfg


def get_access_token(cfg: dict) -> str:
    now = int(time.time())
    assertion = pyjwt.encode(
        {"iss": cfg["client_id"], "sub": cfg["service_account"], "iat": now, "exp": now + 3600},
        cfg["_private_key"], algorithm="RS256",
    )
    data = urllib.parse.urlencode({
        "assertion": assertion,
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": cfg["client_id"], "client_secret": cfg["client_secret"], "scope": "bot",
    }).encode()
    req = urllib.request.Request(
        "https://auth.worksmobile.com/oauth2/v2.0/token", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["access_token"]


def send_text(cfg: dict, text: str) -> bool:
    try:
        token = get_access_token(cfg)
    except Exception as e:
        print(f"✗ Token 取得失敗: {e}", file=sys.stderr)
        return False
    req = urllib.request.Request(
        f"https://www.worksapis.com/v1.0/bots/{cfg['bot_id']}/channels/{cfg['channel_id']}/messages",
        data=json.dumps({"content": {"type": "text", "text": text}}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201, 204)
    except urllib.error.HTTPError as e:
        print(f"✗ メッセージ送信失敗: {e.code} {e.read().decode()}", file=sys.stderr)
        return False


# ===================== ヘルパー =====================
def fmt_money(n: int) -> str:
    return f"¥{int(n):,}"


def fmt_money_short(n: int) -> str:
    """大きい金額は M 表記"""
    if abs(n) >= 1_000_000:
        return f"¥{n / 1_000_000:.2f}M"
    if abs(n) >= 100_000:
        return f"¥{n / 1_000_000:.2f}M"
    return f"¥{n:,}"


def fmt_date_label(now: datetime) -> str:
    wd = ["月", "火", "水", "木", "金", "土", "日"][now.weekday()]
    return f"{now.month}/{now.day} ({wd}) {now.strftime('%H:%M')}"


def pace_icon(fc: float) -> str:
    if fc >= 100: return "🏆"
    if fc >= 85: return "🏪"
    return "⚠️"


# ===================== HANABI 集計 =====================
HANABI_DATA = Path("/Users/yoheimizuno/hanabi-dashboard/docs/data.json")
HANABI_URL = "https://dashboard.hanabi2020.co.jp/"


def aggregate_hanabi() -> dict:
    """HANABI: hanabi-dashboard/docs/data.json から実績集計 (店舗名フル + ELLE 部門別)"""
    if not HANABI_DATA.exists():
        return {}
    try:
        d = json.loads(HANABI_DATA.read_text(encoding="utf-8"))
        now = datetime.now(JST)
        ym = f"{now.year}{now.month:02d}"
        mbs = d.get("monthly_by_store", {})
        any_data = any(((mbs.get(s, {}).get(ym, {}) or {}).get("total_sales", 0) > 0)
                       for s in ["tsunashima", "miyakojima", "shinyokohama"])
        if not any_data:
            py = now.year if now.month > 1 else now.year - 1
            pm = now.month - 1 if now.month > 1 else 12
            ym = f"{py}{pm:02d}"
        result = {"ym": ym, "stores": [], "total_sales": 0, "total_customers": 0}
        # 店舗フル名 (要件 B)
        store_meta = [
            ("tsunashima",   "Hanabi綱島店",         False),
            ("miyakojima",   "ELLE by Hanabi宮古島店", True),   # 部門別表示
            ("shinyokohama", "ナイスネイル新横浜店",    False),
        ]
        for sid, label, with_dept in store_meta:
            ms = (mbs.get(sid, {}) or {}).get(ym, {}) or {}
            sales = ms.get("total_sales", 0)
            customers = ms.get("customers", 0)
            store_data = {"label": label, "sales": sales, "customers": customers, "dept": None}
            # ELLE宮古島は部門別 (by_dept) を取得
            if with_dept:
                by_dept = ms.get("by_dept", {}) or {}
                store_data["dept"] = {
                    "ヘア":   by_dept.get("ヘア",   {}),
                    "アイ":   by_dept.get("アイ",   {}),
                    "ネイル": by_dept.get("ネイル", {}),
                }
            result["stores"].append(store_data)
            result["total_sales"] += sales
            result["total_customers"] += customers
        return result
    except Exception as e:
        print(f"  warn: HANABI data.json 読み込み失敗: {e}", file=sys.stderr)
        return {}


def build_hanabi_success(highlights: list[str]) -> str:
    now = datetime.now(JST)
    data = aggregate_hanabi()
    lines = ["✅ HANABI 自動更新 完了", fmt_date_label(now), ""]
    if highlights:
        lines += [SEP, "📌 特記事項", SEP, ""]
        for h in highlights:
            lines.append(h); lines.append("")
    if data:
        ym = data["ym"]
        lines += [SEP, f"📊 当月実績 〜{ym[:4]}/{int(ym[4:6])}", SEP, ""]
        for s in data["stores"]:
            if s["sales"] > 0 or s["customers"] > 0:
                lines.append(f"🏪 {s['label']}")
                lines.append(f"  {fmt_money(s['sales'])}  /  {s['customers']}名")
                # ELLE宮古島 部門別
                if s.get("dept"):
                    for dept_label, dept_data in s["dept"].items():
                        d_sales = dept_data.get("sales", 0)
                        d_customers = dept_data.get("customers", 0)
                        if d_sales > 0 or d_customers > 0:
                            d_icon = {"ヘア": "💇", "アイ": "👁", "ネイル": "💅"}.get(dept_label, "・")
                            lines.append(f"   {d_icon} {dept_label}  {fmt_money(d_sales)} / {d_customers}名")
                lines.append("")
        lines += [
            SEP, "✨ 全社合計",
            f"  {fmt_money(data['total_sales'])}  /  {data['total_customers']}名",
            SEP, "",
        ]
    lines += ["🔗 ダッシュボード", HANABI_URL]
    return "\n".join(lines)


# ===================== ナイスネイル 集計 =====================
NICENAIL_HTML = Path("/Users/yoheimizuno/salon-dashboard/dist/index.html")
NICENAIL_TARGETS = Path("/Users/yoheimizuno/salon-dashboard/data/targets.json")
NICENAIL_URL = "https://salondashboard.denko-japan.co.jp/"


def extract_monthly_records(html: str) -> dict:
    """dist/index.html から window.monthlyRecords を抽出"""
    m = re.search(r"window\.monthlyRecords\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def aggregate_nicenail() -> dict:
    """ナイスネイル: salon-dashboard/dist/index.html から店舗別実績集計"""
    if not NICENAIL_HTML.exists() or not NICENAIL_TARGETS.exists():
        return {}
    try:
        html = NICENAIL_HTML.read_text(encoding="utf-8")
        mr = extract_monthly_records(html)
        if not mr:
            return {}
        now = datetime.now(JST)
        # ナイスネイル の月キー形式: "YYYY-MM"
        ym_key = f"{now.year}-{now.month:02d}"
        records = mr.get(ym_key, [])
        if not records:
            # 前月フォールバック
            py = now.year if now.month > 1 else now.year - 1
            pm = now.month - 1 if now.month > 1 else 12
            ym_key = f"{py}-{pm:02d}"
            records = mr.get(ym_key, [])
        if not records:
            return {}
        # 補正レコード除外
        records = [r for r in records if not r.get("is_cancel_only")]
        # 経過日数 (records 内の最大日)
        max_day = 0
        for r in records:
            d = r.get("date", "")
            if len(d) >= 8:
                day = int(d[6:8])
                if day > max_day:
                    max_day = day
        elapsed = max_day or now.day - 1
        # 月の日数
        if now.month == 12:
            next_m = datetime(now.year + 1, 1, 1, tzinfo=JST)
        else:
            next_m = datetime(now.year, now.month + 1, 1, tzinfo=JST)
        last_d_of_this_m = (next_m.replace(day=1) - __import__("datetime").timedelta(days=1)).day
        days_in_month = last_d_of_this_m

        # ターゲット
        targets = json.loads(NICENAIL_TARGETS.read_text(encoding="utf-8")).get("stores", {})

        # 店舗別集計
        by_store = defaultdict(lambda: {"sales": 0, "visits": 0, "options": 0, "tenhan": 0})
        for r in records:
            s = r.get("store", "")
            by_store[s]["sales"] += r.get("amount", 0)
            by_store[s]["visits"] += 1
            by_store[s]["options"] += r.get("options", 0)
            by_store[s]["tenhan"] += r.get("tenhan", 0)

        stores_result = []
        total = {"sales": 0, "visits": 0, "options": 0, "tenhan": 0, "budget": 0, "target": 0}
        for store_full, agg in by_store.items():
            if store_full not in targets:
                continue
            t = targets[store_full]
            budget = t.get("budget", 0)
            target = t.get("target", 0)
            forecast = int(agg["sales"] * days_in_month / elapsed) if elapsed > 0 else 0
            stores_result.append({
                "store": store_full.replace("店", ""),
                "sales": agg["sales"],
                "visits": agg["visits"],
                "options": agg["options"],
                "budget_fc": forecast / budget * 100 if budget else 0,
                "target_fc": forecast / target * 100 if target else 0,
                "forecast": forecast,
            })
            total["sales"] += agg["sales"]
            total["visits"] += agg["visits"]
            total["options"] += agg["options"]
            total["tenhan"] += agg["tenhan"]
            total["budget"] += budget
            total["target"] += target

        # 目標進捗ペース順
        stores_result.sort(key=lambda x: -x["target_fc"])
        forecast_total = int(total["sales"] * days_in_month / elapsed) if elapsed > 0 else 0

        return {
            "ym": ym_key.replace("-", ""),
            "elapsed": elapsed,
            "days_in_month": days_in_month,
            "stores": stores_result,
            "total": total,
            "forecast_total": forecast_total,
            "budget_fc_total": forecast_total / total["budget"] * 100 if total["budget"] else 0,
            "target_fc_total": forecast_total / total["target"] * 100 if total["target"] else 0,
            "op_pct_total": total["options"] / total["visits"] * 100 if total["visits"] else 0,
            "avg_total": total["sales"] // total["visits"] if total["visits"] else 0,
        }
    except Exception as e:
        print(f"  warn: ナイスネイル aggregate 失敗: {e}", file=sys.stderr)
        return {}


def build_nicenail_success(highlights: list[str]) -> str:
    now = datetime.now(JST)
    data = aggregate_nicenail()
    lines = ["✅ ナイスネイルFC 自動更新 完了", fmt_date_label(now), ""]
    if highlights:
        lines += [SEP, "📌 特記事項", SEP, ""]
        for h in highlights:
            lines.append(h); lines.append("")
    if data:
        ym = data["ym"]
        elapsed = data["elapsed"]
        lines += [
            SEP,
            f"📊 当月実績 〜{ym[:4]}/{int(ym[4:6])}/{elapsed} (進捗ペース順)",
            SEP, "",
            "🏪 店舗別 (予算/目標 進捗ペース)",
        ]
        for r in data["stores"]:
            icon = pace_icon(r["target_fc"])
            lines.append(
                f"{icon} {r['store']:<5} 予算{r['budget_fc']:>3.0f}% / 目標{r['target_fc']:>3.0f}%  {fmt_money(r['sales'])}"
            )
        lines += [
            "", SEP, "✨ 全社サマリー", SEP, "",
            f"売上    {fmt_money_short(data['total']['sales'])}",
            f"予測    {fmt_money_short(data['forecast_total'])} (月末)",
            f"  予算 進捗ペース {data['budget_fc_total']:.0f}% ({pace_icon(data['budget_fc_total'])})",
            f"  目標 進捗ペース {data['target_fc_total']:.0f}% ({pace_icon(data['target_fc_total'])})",
            f"客数    {data['total']['visits']:,}名",
            f"客単価  {fmt_money(data['avg_total'])}",
            f"OP比率  {data['op_pct_total']:.1f}%",
            "", SEP, "",
        ]
    lines += ["🔗 ダッシュボード", NICENAIL_URL]
    return "\n".join(lines)


# ===================== 失敗メッセージ =====================
def build_failure_message(project: str, step: str, error_msg: str, log_file: str = "") -> str:
    now = datetime.now(JST)
    if project == "hanabi":
        name = "HANABI"
        url = HANABI_URL
        recovery = "ターミナルで再実行\n   cd ~/hanabi-dashboard\n   bash scripts/deploy_auto.sh"
    else:
        name = "ナイスネイルFC"
        url = NICENAIL_URL
        recovery = "Chrome 手動で SC にログイン後\n   cd ~/salon-dashboard\n   python3 auto_download.py && ./safe_deploy.sh --yes"

    lines = [
        f"❌ {name} 自動更新 失敗",
        fmt_date_label(now), "",
        SEP, "🚨 失敗内容", SEP, "",
        f"📍 ステップ\n   {step}", "",
        f"⚠️ エラー\n   {error_msg[:200]}", "",
        f"🛠 対処方法\n   {recovery}", "",
    ]
    if log_file:
        lines += [f"📂 詳細ログ\n   {log_file}", ""]
    lines += [SEP, "", "🔗 ダッシュボード (古いデータ表示中)", url]
    return "\n".join(lines)


# ===================== 特記事項 (自動検出) =====================
def detect_highlights(project: str) -> list[str]:
    notes = []
    now = datetime.now(JST)
    if now.day == 1:
        if project == "hanabi":
            notes.append("📅 月初リマインダー\n   Box 原価管理表の入力時期です")
        # ナイスネイル は月初リマインダー特になし (必要時追加)
    # phase 2: 前日比、 達成率、 ゼロ売上 等の検出
    return notes


# ===================== エントリポイント =====================
def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    project = sys.argv[1]
    mode = sys.argv[2]

    if project not in ("hanabi", "nicenail"):
        print(f"未知の project: {project} (hanabi or nicenail)", file=sys.stderr)
        sys.exit(1)

    cfg = load_lineworks_config(project)

    if mode == "success":
        highlights = detect_highlights(project)
        if project == "hanabi":
            msg = build_hanabi_success(highlights)
        else:
            msg = build_nicenail_success(highlights)
        ok = send_text(cfg, msg)
    elif mode == "failure":
        step = sys.argv[3] if len(sys.argv) > 3 else "不明なステップ"
        error_msg = sys.argv[4] if len(sys.argv) > 4 else "不明なエラー"
        log_file = sys.argv[5] if len(sys.argv) > 5 else ""
        msg = build_failure_message(project, step, error_msg, log_file)
        ok = send_text(cfg, msg)
    elif mode == "test":
        msg = f"🧪 {project} 通知テスト  {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')}\nLINE WORKS Bot 接続確認"
        ok = send_text(cfg, msg)
    else:
        print(f"未知の mode: {mode}", file=sys.stderr)
        sys.exit(1)

    if ok:
        print(f"✓ {project} {mode} 通知 送信成功")
    else:
        print(f"✗ {project} {mode} 通知 送信失敗", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
