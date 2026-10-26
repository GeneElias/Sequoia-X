"""Explicit delivery with local deduplication and secret-safe failures."""

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlparse

import requests

from .report import content_id


class DeliveryError(RuntimeError):
    pass


def with_dashboard(cards: list[dict], url: str | None) -> list[dict]:
    """Optional public-demo entry, included in content hashing for accurate deduplication."""
    if not url:
        return cards
    parsed = urlparse(url)
    if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".github.io")
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.port not in (None, 443)):
        raise DeliveryError("演示看板地址必须为无凭证和查询参数的 GitHub Pages HTTPS 地址")
    result = json.loads(json.dumps(cards))
    for card in result:
        card["card"]["elements"].append({"tag": "action", "actions": [{
            "tag": "button", "text": {"tag": "plain_text", "content": "打开看板（公开演示）"},
            "type": "primary", "url": url}]})
        if len(json.dumps(card, ensure_ascii=False).encode()) > 19000:
            raise DeliveryError("添加看板入口后的消息过长，请减少单卡观察项")
    return result


def validate_webhook(webhook: str) -> None:
    parsed = urlparse(webhook)
    if (parsed.scheme != "https" or parsed.hostname not in {"open.feishu.cn", "open.larksuite.com"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment
            or not parsed.path.startswith("/open-apis/bot/v2/hook/")
            or not parsed.path.rsplit("/", 1)[-1]
            or "your-" in parsed.path):
        raise DeliveryError("需要有效的飞书自定义机器人 HTTPS Webhook，请在本地 .env 安全配置")


def send_cards(cards: list[dict], webhook: str, ledger: Path, secret: str | None = None,
               force: bool = False, post=None, checkpoint=None) -> dict:
    validate_webhook(webhook)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    # Different robots have different delivery history; never store the raw URL.
    destination = hashlib.sha256(webhook.encode()).hexdigest()
    post = post or requests.post
    counts = {"sent": 0, "skipped": 0}
    with sqlite3.connect(ledger) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, state TEXT, updated TEXT)")
        for card in cards:
            key = destination + ":" + content_id(card)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM deliveries WHERE id=?", (key,)).fetchone()
            if row and row[0] == "sent" and not force:
                conn.commit()
                counts["skipped"] += 1
                continue
            if row and row[0] in {"pending", "unknown"} and not force:
                conn.rollback()
                raise DeliveryError("存在结果不明的发送记录，请先检查飞书；确认后可显式 --force-resend")
            conn.execute("INSERT OR REPLACE INTO deliveries VALUES (?, 'pending', ?)",
                         (key, datetime.now(timezone.utc).isoformat()))
            conn.commit()
            if checkpoint:
                try:
                    checkpoint()
                except Exception:
                    raise DeliveryError("发送前远端状态保存失败；未发送消息，不能重置账本绕过") from None
            payload = json.loads(json.dumps(card))
            if secret:
                timestamp = str(int(time.time()))
                payload["timestamp"] = timestamp
                payload["sign"] = base64.b64encode(hmac.new(
                    f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()).decode()
            try:
                # Disable redirects to avoid forwarding the credential-bearing URL elsewhere.
                response = post(webhook, json=payload, timeout=15, allow_redirects=False)
                body = response.json()
                ok = response.status_code == 200 and isinstance(body, dict) and body.get("code") == 0
            except Exception:
                conn.execute("UPDATE deliveries SET state='unknown' WHERE id=?", (key,))
                conn.commit()
                if checkpoint:
                    checkpoint()
                raise DeliveryError("飞书发送结果不明；未自动重试，请检查群消息及本地发送记录") from None
            if not ok:
                # Never print response text: it may echo the webhook or secret.
                conn.execute("UPDATE deliveries SET state='failed' WHERE id=?", (key,))
                conn.commit()
                if checkpoint:
                    checkpoint()
                raise DeliveryError("飞书拒绝消息：请检查机器人关键词、签名、IP限制及网络，响应正文已隐藏")
            conn.execute("UPDATE deliveries SET state='sent' WHERE id=?", (key,))
            conn.commit()
            if checkpoint:
                checkpoint()
            counts["sent"] += 1
    return counts
