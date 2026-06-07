#!/usr/bin/env python3
"""
Gmail SMTP経由でメール通知を送信する。

使い方:
  python3 notify.py success "2026-05-12 08:15" "ELLE宮古島: 850件 / 綱島: 620件"
  python3 notify.py failure "2026-05-12 08:15" "CSVダウンロード失敗"
  python3 notify.py test
"""

import smtplib
import sys
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_URL = "https://dashboard.hanabi2020.co.jp/"


def load_env():
    """.envファイルを読み込む"""
    env_path = os.path.join(BASE_DIR, ".env")
    config = {}
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()
    return config


def send_mail(subject, body):
    """Gmail SMTP経由でメール送信"""
    config = load_env()
    smtp_user = config["SMTP_USER"]
    smtp_pass = config["SMTP_PASS"]
    recipients = [addr.strip() for addr in config["NOTIFY_TO"].split(",")]

    msg = MIMEMultipart()
    msg["From"] = f"HANABI ダッシュボード <{smtp_user}>"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)

    print(f"メール送信完了: {subject}")


def main():
    if len(sys.argv) < 2:
        print("使い方: notify.py [success|failure|test] [args...]")
        sys.exit(1)

    mode = sys.argv[1]

    if mode == "success":
        timestamp = sys.argv[2] if len(sys.argv) > 2 else "不明"
        summary = sys.argv[3] if len(sys.argv) > 3 else ""
        send_mail(
            "✅ HANABI ダッシュボード更新完了",
            f"更新日時：{timestamp}\n"
            f"対象店舗：Hanabi 綱島店 / ELLE by Hanabi 宮古島店\n"
            f"{summary}\n"
            f"\n🔗 {DASHBOARD_URL}",
        )

    elif mode == "failure":
        timestamp = sys.argv[2] if len(sys.argv) > 2 else "不明"
        error_msg = sys.argv[3] if len(sys.argv) > 3 else "不明なエラー"
        send_mail(
            "❌ HANABI ダッシュボード更新失敗",
            f"エラー日時：{timestamp}\n"
            f"エラー内容：{error_msg}\n"
            f"\nUレジへの再ログインが必要、もしくは git の状態確認が必要な可能性があります。\n"
            f"\n🔗 {DASHBOARD_URL}",
        )

    elif mode == "test":
        send_mail(
            "🔔 HANABI ダッシュボード通知テスト",
            f"メール通知のテスト送信です。\n\n🔗 {DASHBOARD_URL}",
        )

    else:
        print(f"不明なモード: {mode}")
        sys.exit(1)


if __name__ == "__main__":
    main()
