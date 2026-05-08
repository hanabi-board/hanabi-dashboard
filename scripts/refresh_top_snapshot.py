#!/usr/bin/env python3
"""TOP画面のスナップショットだけ取りに行く軽量版。"""
import sys
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from auto_download import load_env, login, capture_top_snapshot

LOG = ROOT / "logs" / "snapshot_refresh"
LOG.mkdir(parents=True, exist_ok=True)
DATA = ROOT / "data"

env = load_env()
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=200)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ja-JP", timezone_id="Asia/Tokyo", accept_downloads=True)
    page = ctx.new_page()
    login(page, env, LOG)
    snap = capture_top_snapshot(page, LOG)
    out_path = DATA / "uregi_top_snapshot.json"
    out_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ saved: {out_path}")
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    time.sleep(1)
    browser.close()
