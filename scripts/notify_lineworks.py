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

# 残り日数 N 日以下で lastweek セクション 自動追加
LASTWEEK_THRESHOLD_DAYS = 7


def _now() -> datetime:
    """現在日時 (テスト用: NOTIFY_TEST_DATE=YYYY-MM-DD 環境変数で上書き可能)"""
    import os
    test_date = os.environ.get("NOTIFY_TEST_DATE")
    if test_date:
        try:
            dt = datetime.strptime(test_date, "%Y-%m-%d")
            return dt.replace(tzinfo=JST, hour=8, minute=0, second=0)
        except ValueError:
            pass
    return datetime.now(JST)


def _remaining_days(now: datetime) -> tuple[int, int]:
    """残り日数 (今日含む) と 月の総日数 を返す"""
    import datetime as _dt
    if now.month == 12:
        next_m = datetime(now.year + 1, 1, 1, tzinfo=JST)
    else:
        next_m = datetime(now.year, now.month + 1, 1, tzinfo=JST)
    last_d = (next_m - _dt.timedelta(days=1)).day
    remaining = max(0, last_d - now.day + 1)
    return remaining, last_d


def _prev_ym(now: datetime) -> str:
    """前月の YYYYMM を返す"""
    py = now.year if now.month > 1 else now.year - 1
    pm = now.month - 1 if now.month > 1 else 12
    return f"{py}{pm:02d}"


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
HANABI_SUMMARIES = Path("/Users/yoheimizuno/hanabi-dashboard/data/monthly_summaries.json")
HANABI_URL = "https://dashboard.hanabi2020.co.jp/"


def aggregate_hanabi() -> dict:
    """HANABI: hanabi-dashboard/docs/data.json から実績集計 (店舗名フル + ELLE 部門別)"""
    if not HANABI_DATA.exists():
        return {}
    try:
        d = json.loads(HANABI_DATA.read_text(encoding="utf-8"))
        now = _now()
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
    now = _now()
    # 月初 1日: 前月確定 (monthend) に切替
    if now.day == 1:
        return build_hanabi_monthend(_prev_ym(now))
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
    # 残り日数 ≤ 7 なら ラストスパート セクション 追加
    remaining_days, _ = _remaining_days(now)
    if 0 < remaining_days <= LASTWEEK_THRESHOLD_DAYS:
        lastweek_section = build_hanabi_lastweek_section(now, remaining_days)
        if lastweek_section:
            lines += [lastweek_section, ""]
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
        now = _now()
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

        # 店舗別集計 (visits/options/tenhan は通常集計、 sales は異動考慮で forecast 計算する)
        by_store = defaultdict(lambda: {"sales": 0, "visits": 0, "options": 0, "tenhan": 0})
        # 店舗×スタッフ別 売上集計 (異動考慮 forecast 用)
        sales_by_store_staff = defaultdict(lambda: defaultdict(int))
        for r in records:
            s = r.get("store", "")
            by_store[s]["sales"] += r.get("amount", 0)
            by_store[s]["visits"] += 1
            by_store[s]["options"] += r.get("options", 0)
            by_store[s]["tenhan"] += r.get("tenhan", 0)
            sales_by_store_staff[s][r.get("staff", "")] += r.get("amount", 0)

        # 異動履歴ロード (template.html の forecastStoreSalesForMonthEnd と同じ判定)
        # ロジック: 当月内 since の異動について
        #   - 異動出 (from===store): since前日まで日割り投影、 異動済(since<=elapsed)はMTDのみ
        #   - 異動入 (to===store):   since以降の経過日で実績→月残日へ按分
        #   - 通常スタッフ:           MTD × daysInMonth / elapsed
        admin_cfg_path = Path("/Users/yoheimizuno/salon-dashboard/data/admin_config.json")
        transfer_history = {}
        if admin_cfg_path.exists():
            try:
                transfer_history = json.loads(admin_cfg_path.read_text(encoding="utf-8")).get("transfer_history", {})
            except Exception:
                transfer_history = {}
        _y, _m = ym_key.split("-")
        m_start = f"{_y}{_m}01"
        m_end = f"{_y}{_m}{days_in_month:02d}"
        def _base_name(s: str) -> str:
            return re.sub(r"^[■□●▲★◆☆]+", "", s or "")
        def _display_name(s: str) -> str:
            return re.sub(r"^[■□●▲★◆☆]+", "", s or "")
        def _forecast_store(store_full: str) -> int:
            """ダッシュボードと同じロジックで店舗の月末売上見込みを返す (異動考慮)"""
            if elapsed <= 0:
                return 0
            total_fc = 0
            for staff, sales in sales_by_store_staff.get(store_full, {}).items():
                entries = (
                    transfer_history.get(_base_name(staff))
                    or transfer_history.get(_display_name(staff))
                    or transfer_history.get(staff)
                    or []
                )
                in_month = None
                for e in entries:
                    since = (e.get("since") or "").replace("-", "")
                    if since and m_start <= since <= m_end:
                        in_month = e
                        break
                if in_month and in_month.get("from") == store_full:
                    s_day = int(in_month.get("since", "")[-2:] or "0")
                    days_at_this_store = s_day - 1
                    if days_at_this_store <= 0:
                        total_fc += sales
                    elif days_at_this_store <= elapsed:
                        total_fc += sales  # 既に異動済 → post-transfer 売上は新店舗側
                    else:
                        total_fc += round(sales * days_at_this_store / elapsed)
                elif in_month and in_month.get("to") == store_full:
                    s_day = int(in_month.get("since", "")[-2:] or "0")
                    since_elapsed = max(1, elapsed - s_day + 1)
                    since_total = days_in_month - s_day + 1
                    total_fc += round(sales * since_total / since_elapsed)
                else:
                    total_fc += round(sales * days_in_month / elapsed)
            return total_fc

        stores_result = []
        total = {"sales": 0, "visits": 0, "options": 0, "tenhan": 0, "budget": 0, "target": 0}
        for store_full, agg in by_store.items():
            if store_full not in targets:
                continue
            t = targets[store_full]
            budget = t.get("budget", 0)
            target = t.get("target", 0)
            forecast = _forecast_store(store_full)
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
        # 全社合計は店舗別 forecast (異動考慮済) の合計でダッシュボードと一致させる
        forecast_total = sum(s["forecast"] for s in stores_result)

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
    now = _now()
    # 月初 1日: 前月確定 (monthend) に切替
    if now.day == 1:
        return build_nicenail_monthend(_prev_ym(now))
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
            f"  予算 進捗ペース {data['budget_fc_total']:.1f}% ({pace_icon(data['budget_fc_total'])})",
            f"  目標 進捗ペース {data['target_fc_total']:.1f}% ({pace_icon(data['target_fc_total'])})",
            f"客数    {data['total']['visits']:,}名",
            f"客単価  {fmt_money(data['avg_total'])}",
            f"OP比率  {data['op_pct_total']:.1f}%",
            "", SEP, "",
        ]
    # 残り日数 ≤ 7 なら ラストスパート セクション 追加
    remaining_days, _ = _remaining_days(now)
    if 0 < remaining_days <= LASTWEEK_THRESHOLD_DAYS:
        lastweek_section = build_nicenail_lastweek_section(now, remaining_days)
        if lastweek_section:
            lines += [lastweek_section, ""]
    lines += ["🔗 ダッシュボード", NICENAIL_URL]
    return "\n".join(lines)


# ===================== HANABI 残り1週間アラート =====================
HANABI_BUDGETS = Path("/Users/yoheimizuno/hanabi-dashboard/data/budgets.json")


def get_hanabi_budgets(ym: str) -> dict:
    """HANABI budgets.json から指定月の予算を取得 (ym = 'YYYYMM')"""
    if not HANABI_BUDGETS.exists():
        return {}
    try:
        b = json.loads(HANABI_BUDGETS.read_text(encoding="utf-8"))
        monthly = b.get("monthly", {})
        dept_monthly = b.get("monthly_dept_miyakojima", {})
        result = {
            "tsunashima":   monthly.get("tsunashima", {}).get(ym, 0),
            "miyakojima":   monthly.get("miyakojima", {}).get(ym, 0),
            "shinyokohama": monthly.get("shinyokohama", {}).get(ym, 0),
            "miyakojima_dept": dept_monthly.get(ym, {}),
        }
        return result
    except Exception as e:
        print(f"  warn: HANABI budgets.json 読み込み失敗: {e}", file=sys.stderr)
        return {}


def build_hanabi_lastweek_section(now: datetime = None, remaining_days: int = None) -> str:
    """HANABI ラストスパートセクション (success に embed 用、 ヘッダーなし内側のみ)"""
    if now is None:
        now = _now()
    data = aggregate_hanabi()
    if not data:
        return ""
    ym = data["ym"]
    if remaining_days is None:
        remaining_days, _ = _remaining_days(now)
    budgets = get_hanabi_budgets(ym)
    lines = [
        SEP,
        f"🔥 ラストスパート (残り{remaining_days}日)",
        SEP,
        "",
        "🏪 店舗別 予算まで残り",
    ]
    total_sales = 0
    total_budget = 0
    sid_map = {"Hanabi綱島店": "tsunashima", "ELLE by Hanabi宮古島店": "miyakojima", "ナイスネイル新横浜店": "shinyokohama"}
    for s in data["stores"]:
        sid = sid_map.get(s["label"], "")
        budget = budgets.get(sid, 0)
        sales = s["sales"]
        remaining = max(0, budget - sales)
        per_day = int(remaining / remaining_days) if remaining_days > 0 else 0
        pct = sales / budget * 100 if budget else 0
        total_sales += sales
        total_budget += budget
        lines.append(f"🏪 {s['label']}  ({pct:.1f}%)")
        lines.append(f"   残り {fmt_money(remaining)} → {fmt_money(per_day)}/日")
        if s.get("dept"):
            dept_budgets = budgets.get("miyakojima_dept", {})
            for dept_label, dept_data in s["dept"].items():
                d_sales = dept_data.get("sales", 0)
                d_budget = dept_budgets.get(dept_label, 0)
                d_remaining = max(0, d_budget - d_sales)
                d_per_day = int(d_remaining / remaining_days) if remaining_days > 0 else 0
                d_pct = d_sales / d_budget * 100 if d_budget else 0
                d_icon = {"ヘア": "💇", "アイ": "👁", "ネイル": "💅"}.get(dept_label, "・")
                lines.append(f"     {d_icon} {dept_label} ({d_pct:.1f}%) 残り {fmt_money(d_remaining)} → {fmt_money(d_per_day)}/日")
        lines.append("")
    total_remaining = max(0, total_budget - total_sales)
    total_per_day = int(total_remaining / remaining_days) if remaining_days > 0 else 0
    total_pct = total_sales / total_budget * 100 if total_budget else 0
    lines += [
        f"✨ 全社  予算 {total_pct:.1f}%  残り {fmt_money_short(total_remaining)} → {fmt_money(total_per_day)}/日",
    ]
    return "\n".join(lines)


def build_hanabi_lastweek() -> str:
    """HANABI 残り1週間 アラート (standalone モード用、 ヘッダー付き)"""
    now = _now()
    remaining_days, _ = _remaining_days(now)
    section = build_hanabi_lastweek_section(now, remaining_days)
    if not section:
        return ""
    lines = [
        f"🔥 HANABI ラストスパート (残り{remaining_days}日)",
        fmt_date_label(now),
        "",
        section,
        "",
        "🔗 ダッシュボード",
        HANABI_URL,
    ]
    return "\n".join(lines)


# ===================== ナイスネイル 残り1週間アラート =====================
def build_nicenail_lastweek_section(now: datetime = None, remaining_days: int = None) -> str:
    """ナイスネイル ラストスパートセクション (success に embed 用、 ヘッダーなし)"""
    if now is None:
        now = _now()
    data = aggregate_nicenail()
    if not data:
        return ""
    if remaining_days is None:
        remaining_days, _ = _remaining_days(now)
    lines = [
        SEP,
        f"🔥 ラストスパート (残り{remaining_days}日)",
        SEP,
        "",
        "🏪 店舗別 予算 / 目標まで残り",
    ]
    for r in data["stores"]:
        sales = r["sales"]
        budget = int(r["forecast"] / (r["budget_fc"] / 100)) if r["budget_fc"] > 0 else 0
        target = int(r["forecast"] / (r["target_fc"] / 100)) if r["target_fc"] > 0 else 0
        budget_remaining = max(0, budget - sales)
        target_remaining = max(0, target - sales)
        budget_per_day = int(budget_remaining / remaining_days) if remaining_days > 0 else 0
        target_per_day = int(target_remaining / remaining_days) if remaining_days > 0 else 0
        lines.append(f"🏪 {r['store']}")
        lines.append(f"   予算まで {fmt_money(budget_remaining)} → {fmt_money(budget_per_day)}/日")
        lines.append(f"   目標まで {fmt_money(target_remaining)} → {fmt_money(target_per_day)}/日")
    total = data["total"]
    total_budget_remaining = max(0, total["budget"] - total["sales"])
    total_target_remaining = max(0, total["target"] - total["sales"])
    total_budget_per_day = int(total_budget_remaining / remaining_days) if remaining_days > 0 else 0
    total_target_per_day = int(total_target_remaining / remaining_days) if remaining_days > 0 else 0
    lines += [
        "",
        f"✨ 全社  予算まで {fmt_money_short(total_budget_remaining)} → {fmt_money(total_budget_per_day)}/日",
        f"       目標まで {fmt_money_short(total_target_remaining)} → {fmt_money(total_target_per_day)}/日",
    ]
    return "\n".join(lines)


def build_nicenail_lastweek() -> str:
    """ナイスネイル 残り1週間 アラート (standalone モード用、 ヘッダー付き)"""
    now = _now()
    remaining_days, _ = _remaining_days(now)
    section = build_nicenail_lastweek_section(now, remaining_days)
    if not section:
        return ""
    lines = [
        f"🔥 ナイスネイルFC ラストスパート (残り{remaining_days}日)",
        fmt_date_label(now),
        "",
        section,
        "",
        "🔗 ダッシュボード",
        NICENAIL_URL,
    ]
    return "\n".join(lines)


# ===================== HANABI 月終了サマリー =====================
def _prev_ym_str(ym: str) -> str:
    """YYYYMM の前月 YYYYMM"""
    yy, mm = int(ym[:4]), int(ym[4:6])
    if mm == 1:
        return f"{yy - 1}12"
    return f"{yy}{mm - 1:02d}"


def aggregate_hanabi_specific_month(target_ym: str) -> dict:
    """HANABI: 指定月の実績を集計 (target_ym = 'YYYYMM')。
    充実版: 店舗別に 客単価・指名率・前月比、 全社の前月売上も返す。"""
    if not HANABI_DATA.exists():
        return {}
    try:
        d = json.loads(HANABI_DATA.read_text(encoding="utf-8"))
        mbs = d.get("monthly_by_store", {})
        prev_ym = _prev_ym_str(target_ym)
        result = {"ym": target_ym, "stores": [], "total_sales": 0,
                  "total_customers": 0, "total_prev_sales": 0}
        store_meta = [
            ("tsunashima",   "Hanabi綱島店",         False),
            ("miyakojima",   "ELLE by Hanabi宮古島店", True),
            ("shinyokohama", "ナイスネイル新横浜店",    False),
        ]
        for sid, label, with_dept in store_meta:
            ms = (mbs.get(sid, {}) or {}).get(target_ym, {}) or {}
            pms = (mbs.get(sid, {}) or {}).get(prev_ym, {}) or {}
            sales = ms.get("total_sales", 0)
            customers = ms.get("customers", 0)
            prev_sales = pms.get("total_sales", 0)
            tc = ms.get("tech_customers", 0)
            tn = ms.get("tech_customers_nominated", 0)
            avg = sales // customers if customers else 0
            nom_rate = tn / tc * 100 if tc else 0
            # 前月比: 前月売上が十分あるときのみ (新店の立ち上がりは無意味 → None)
            mom = (sales / prev_sales - 1) * 100 if prev_sales >= 500000 else None
            store_data = {"label": label, "sid": sid, "sales": sales,
                          "customers": customers, "avg": avg, "nom_rate": nom_rate,
                          "mom": mom, "prev_sales": prev_sales, "dept": None}
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
            result["total_prev_sales"] += prev_sales
        return result
    except Exception as e:
        print(f"  warn: HANABI 月別集計失敗: {e}", file=sys.stderr)
        return {}


def get_hanabi_kakugen(target_ym: str) -> str:
    """月次振り返り生成時に作られた「今月の格言」(AI生成) を読む。 無ければ空文字。"""
    try:
        if not HANABI_SUMMARIES.exists():
            return ""
        d = json.loads(HANABI_SUMMARIES.read_text(encoding="utf-8"))
        return ((d.get("kakugen", {}) or {}).get(target_ym, "") or "").strip()
    except Exception:
        return ""


def rule_kakugen(target_ym: str, achieved: int, total_stores: int, total_pct: float) -> str:
    """AI格言が無い時のフォールバック (A調: 格言・ことわざ風)。 毎月確実に何か出す。"""
    m = int(target_ym[4:6])
    n = f"{m}月"
    if total_stores and achieved == total_stores:
        return ("「好調は、守りに入った時に崩れる。攻めの手を緩めるな。」\n"
                f"— {n}、全店予算達成。次の天井へ。")
    if total_pct >= 100:
        return ("「席数は売上の天井なり。天井を上げるは、人なり。」\n"
                f"— {n}、全社{total_pct:.0f}%達成。伸びしろは採用にあり。")
    if total_pct >= 90:
        return ("「あと一歩は、日々の一手の積み重ねが埋める。」\n"
                f"— {n}、全社{total_pct:.0f}%。目標は届く距離にあり。")
    return ("「逆風こそ、実力を鍛える追い風なり。」\n"
            f"— {n}、全社{total_pct:.0f}%。ここからが正念場。")


def build_hanabi_monthend(target_ym: str = None) -> str:
    """HANABI 月終了サマリー (前月確定値)"""
    now = _now()
    if target_ym is None:
        # 前月を自動判定
        py = now.year if now.month > 1 else now.year - 1
        pm = now.month - 1 if now.month > 1 else 12
        target_ym = f"{py}{pm:02d}"
    data = aggregate_hanabi_specific_month(target_ym)
    if not data:
        return ""
    budgets = get_hanabi_budgets(target_ym)
    y, m = target_ym[:4], int(target_ym[4:6])

    # --- 全社集計 ---
    achieved = 0
    total_stores_with_budget = 0
    total_budget = 0
    for s in data["stores"]:
        budget = budgets.get(s["sid"], 0)
        if budget > 0:
            total_stores_with_budget += 1
            total_budget += budget
            if s["sales"] / budget * 100 >= 100:
                achieved += 1
    total_pct = data["total_sales"] / total_budget * 100 if total_budget else 0
    total_avg = data["total_sales"] // data["total_customers"] if data["total_customers"] else 0
    total_mom = ((data["total_sales"] / data["total_prev_sales"] - 1) * 100
                 if data.get("total_prev_sales", 0) >= 500000 else None)

    # --- 全社合計 (先頭) ---
    sales_line = f"売上     {fmt_money_short(data['total_sales'])}"
    if total_mom is not None:
        sales_line += f"  (前月比 {total_mom:+.1f}%)"
    lines = [
        f"🏁 HANABI {y}年{m}月 確定",
        fmt_date_label(now),
        "",
        SEP,
        "✨ 全社合計",
        SEP,
        "",
        sales_line,
    ]
    if total_budget > 0:
        lines.append(f"予算     {fmt_money_short(total_budget)}  ({total_pct:.1f}%)")
        lines.append(f"予算達成 {achieved}/{total_stores_with_budget} 店舗")
    lines += [
        f"客数     {data['total_customers']:,}名",
        f"客単価   {fmt_money(total_avg)}",
        "",
        SEP,
        "🏪 店舗別",
        SEP,
        "",
    ]

    # --- 店舗別 (充実版: 前月比・客数・客単価・指名率) ---
    for s in data["stores"]:
        budget = budgets.get(s["sid"], 0)
        sales = s["sales"]
        if budget > 0:
            pct = sales / budget * 100
            icon = "🏆" if pct >= 100 else "🏪" if pct >= 85 else "⚠️"
        else:
            pct = 0
            icon = "🏪"
        lines.append(f"{icon} {s['label']}")
        if budget > 0:
            lines.append(f"   売上   {fmt_money(sales)}  (予算比 {pct:.1f}%)")
        else:
            lines.append(f"   売上   {fmt_money(sales)}")
        if s["mom"] is not None:
            # 通常店: 前月比・客数 → 客単価・指名率
            lines.append(f"   前月比 {s['mom']:+.1f}% ・ 客数 {s['customers']}名")
            lines.append(f"   客単価 {fmt_money(s['avg'])} ・ 指名率 {s['nom_rate']:.0f}%")
        else:
            # 新店 (前月比が無意味): 客数・客単価 + 注記
            lines.append(f"   客数 {s['customers']}名 ・ 客単価 {fmt_money(s['avg'])}")
            lines.append("   （オープン初期）")
        if s.get("dept"):
            dept_budgets = budgets.get("miyakojima_dept", {})
            for dept_label, dept_data in s["dept"].items():
                d_sales = dept_data.get("sales", 0)
                d_budget = dept_budgets.get(dept_label, 0)
                d_pct = d_sales / d_budget * 100 if d_budget else 0
                d_icon_pct = "🏆" if d_pct >= 100 else "  " if d_pct >= 85 else "⚠️"
                d_icon = {"ヘア": "💇", "アイ": "👁", "ネイル": "💅"}.get(dept_label, "・")
                lines.append(f"     {d_icon} {dept_label} {fmt_money(d_sales)} ({d_pct:.1f}%) {d_icon_pct}")
        lines.append("")

    # --- 今月の格言 (AI生成優先、 無ければルール式フォールバック) ---
    kakugen = get_hanabi_kakugen(target_ym) or rule_kakugen(
        target_ym, achieved, total_stores_with_budget, total_pct)
    if kakugen:
        klines = kakugen.split("\n")
        klines[0] = f"🎓 {klines[0]}"  # 格言本体の頭に 🎓
        lines += [SEP, "💬 今月の格言", SEP, "", "\n".join(klines), ""]

    lines += [
        SEP,
        "",
        "🔗 ダッシュボード",
        HANABI_URL,
    ]
    return "\n".join(lines)


# ===================== ナイスネイル 月終了サマリー =====================
def aggregate_nicenail_specific_month(target_ym: str) -> dict:
    """ナイスネイル: 指定月 (YYYYMM) の実績を集計"""
    if not NICENAIL_HTML.exists() or not NICENAIL_TARGETS.exists():
        return {}
    try:
        html = NICENAIL_HTML.read_text(encoding="utf-8")
        mr = extract_monthly_records(html)
        if not mr:
            return {}
        ym_key = f"{target_ym[:4]}-{target_ym[4:6]}"
        records = mr.get(ym_key, [])
        if not records:
            return {}
        records = [r for r in records if not r.get("is_cancel_only")]
        targets_data = json.loads(NICENAIL_TARGETS.read_text(encoding="utf-8"))
        # 月別 targets: stores の history か stores 直
        history = targets_data.get("history", {})
        if target_ym in history:
            targets = history[target_ym].get("stores", {})
        else:
            targets = targets_data.get("stores", {})

        by_store = defaultdict(lambda: {"sales": 0, "visits": 0, "options": 0, "tenhan": 0})
        for r in records:
            s = r.get("store", "")
            by_store[s]["sales"] += r.get("amount", 0)
            by_store[s]["visits"] += 1
            by_store[s]["options"] += r.get("options", 0)
            by_store[s]["tenhan"] += r.get("tenhan", 0)

        stores_result = []
        total = {"sales": 0, "visits": 0, "options": 0, "budget": 0, "target": 0}
        achieved_budget = 0
        achieved_target = 0
        for store_full, agg in by_store.items():
            if store_full not in targets:
                continue
            t = targets[store_full]
            budget = t.get("budget", 0)
            target = t.get("target", 0)
            budget_pct = agg["sales"] / budget * 100 if budget else 0
            target_pct = agg["sales"] / target * 100 if target else 0
            if budget_pct >= 100: achieved_budget += 1
            if target_pct >= 100: achieved_target += 1
            stores_result.append({
                "store": store_full.replace("店", ""),
                "sales": agg["sales"],
                "visits": agg["visits"],
                "options": agg["options"],
                "budget": budget,
                "target": target,
                "budget_pct": budget_pct,
                "target_pct": target_pct,
            })
            total["sales"] += agg["sales"]
            total["visits"] += agg["visits"]
            total["options"] += agg["options"]
            total["budget"] += budget
            total["target"] += target
        # 予算達成率順
        stores_result.sort(key=lambda x: -x["budget_pct"])
        return {
            "ym": target_ym,
            "stores": stores_result,
            "total": total,
            "achieved_budget": achieved_budget,
            "achieved_target": achieved_target,
            "store_count": len(stores_result),
            "avg_total": total["sales"] // total["visits"] if total["visits"] else 0,
            "op_pct_total": total["options"] / total["visits"] * 100 if total["visits"] else 0,
        }
    except Exception as e:
        print(f"  warn: ナイスネイル 月別集計失敗: {e}", file=sys.stderr)
        return {}


def build_nicenail_monthend(target_ym: str = None) -> str:
    """ナイスネイル 月終了サマリー (前月確定値)"""
    now = _now()
    if target_ym is None:
        py = now.year if now.month > 1 else now.year - 1
        pm = now.month - 1 if now.month > 1 else 12
        target_ym = f"{py}{pm:02d}"
    data = aggregate_nicenail_specific_month(target_ym)
    if not data:
        return ""
    y, m = target_ym[:4], int(target_ym[4:6])
    lines = [
        f"🏁 ナイスネイルFC {y}/{m}月 確定",
        fmt_date_label(now),
        "",
        SEP,
        f"📊 {y}/{m}月 最終結果 (予算達成率順)",
        SEP,
        "",
        "🏪 店舗別",
    ]
    for r in data["stores"]:
        icon = "🏆" if r["budget_pct"] >= 100 else "🏪" if r["budget_pct"] >= 85 else "⚠️"
        # 🆕 2026-07-01 小数1桁表示 (99.6%が「100%」と丸まって未達アイコンと矛盾するのを解消)
        lines.append(f"{icon} {r['store']:<5} 予算 {r['budget_pct']:>5.1f}% / 目標 {r['target_pct']:>5.1f}%  {fmt_money(r['sales'])}")
    total = data["total"]
    total_budget_pct = total["sales"] / total["budget"] * 100 if total["budget"] else 0
    total_target_pct = total["sales"] / total["target"] * 100 if total["target"] else 0
    lines += [
        "",
        SEP,
        "✨ 全社サマリー",
        SEP,
        "",
        f"売上       {fmt_money_short(total['sales'])}",
        f"予算       {fmt_money_short(total['budget'])}  ({total_budget_pct:.1f}%)",
        f"目標       {fmt_money_short(total['target'])}  ({total_target_pct:.1f}%)",
        f"予算達成   {data['achieved_budget']}/{data['store_count']} 店舗",
        f"目標達成   {data['achieved_target']}/{data['store_count']} 店舗",
        f"客数       {total['visits']:,}名",
        f"客単価     {fmt_money(data['avg_total'])}",
        f"OP比率     {data['op_pct_total']:.1f}%",
        "",
        SEP,
        "",
        "🔗 ダッシュボード",
        NICENAIL_URL,
    ]
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
    # --dry-run フラグ抽出 (sys.argv から除去)
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        sys.argv = [a for a in sys.argv if a != "--dry-run"]

    if len(sys.argv) < 3:
        print(__doc__)
        print("\n使い方:")
        print("  notify_lineworks.py {hanabi|nicenail} {success|failure|test|lastweek|monthend} [args] [--dry-run]")
        print("  --dry-run: 送信せず stdout にメッセージを出力 (確認用)")
        sys.exit(1)

    project = sys.argv[1]
    mode = sys.argv[2]

    if project not in ("hanabi", "nicenail"):
        print(f"未知の project: {project} (hanabi or nicenail)", file=sys.stderr)
        sys.exit(1)

    msg = None
    if mode == "success":
        highlights = detect_highlights(project)
        if project == "hanabi":
            msg = build_hanabi_success(highlights)
        else:
            msg = build_nicenail_success(highlights)
    elif mode == "failure":
        step = sys.argv[3] if len(sys.argv) > 3 else "不明なステップ"
        error_msg = sys.argv[4] if len(sys.argv) > 4 else "不明なエラー"
        log_file = sys.argv[5] if len(sys.argv) > 5 else ""
        msg = build_failure_message(project, step, error_msg, log_file)
    elif mode == "test":
        msg = f"🧪 {project} 通知テスト  {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')}\nLINE WORKS Bot 接続確認"
    elif mode == "lastweek":
        # 残り1週間 アラート
        if project == "hanabi":
            msg = build_hanabi_lastweek()
        else:
            msg = build_nicenail_lastweek()
    elif mode == "monthend":
        # 月終了サマリー (前月分)
        target_ym = sys.argv[3] if len(sys.argv) > 3 else None
        if project == "hanabi":
            msg = build_hanabi_monthend(target_ym)
        else:
            msg = build_nicenail_monthend(target_ym)
    elif mode == "selfheal":
        # 新横浜 自己修復 通知 (= 朝 salon 未完了で古かった数字を後から最新化した時)
        # 2026-07-24 改修 (水野要望): 修復対象の新横浜だけでなく 全店舗+全社合計を表示。
        #   「新横浜しか載ってない=他店はどうなってるの?」 という混乱を防ぐ。
        now = _now()
        data = aggregate_hanabi()
        lines = [
            "🔧 新横浜 自動修復 完了",
            fmt_date_label(now),
            "",
            "朝の更新時に NICENAIL 側がまだ完了しておらず、",
            "新横浜の数字が前日のままになっていたため、",
            "最新データで自動的に再集計しました。",
            "",
        ]
        if data:
            ym = data["ym"]
            lines += [SEP, f"📊 当月実績 〜{ym[:4]}/{int(ym[4:6])} (修復後)", SEP, ""]
            for s in data["stores"]:
                if s["sales"] > 0 or s["customers"] > 0:
                    icon = "🔧" if s.get("label") == "ナイスネイル新横浜店" else "🏪"
                    suffix = " (今回修復)" if icon == "🔧" else ""
                    lines.append(f"{icon} {s['label']}{suffix}")
                    lines.append(f"  {fmt_money(s['sales'])}  /  {s['customers']}名")
                    lines.append("")
            lines += [
                SEP, "✨ 全社合計",
                f"  {fmt_money(data['total_sales'])}  /  {data['total_customers']}名",
                SEP, "",
                "※ 修復対象は新横浜のみ。 他店は直近の自動更新時点の数字です",
                "",
            ]
        lines += ["🔗 ダッシュボード", HANABI_URL]
        msg = "\n".join(lines)
    else:
        print(f"未知の mode: {mode}", file=sys.stderr)
        sys.exit(1)

    if not msg:
        print(f"✗ {project} {mode} メッセージ生成失敗 (データ不足等)", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(msg)
        print(f"\n--- (dry-run: 上記メッセージは送信していません) ---")
        return

    cfg = load_lineworks_config(project)
    ok = send_text(cfg, msg)
    if ok:
        print(f"✓ {project} {mode} 通知 送信成功")
    else:
        print(f"✗ {project} {mode} 通知 送信失敗", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
