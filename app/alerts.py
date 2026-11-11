"""Notification channels: Email, Discord, Slack, Telegram, ntfy, generic webhook."""
import json
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage

EMOJI = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
COLORS = {"info": 0x3B82F6, "warning": 0xF59E0B, "critical": 0xEF4444}


def _post_json(url, payload, headers=None, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "DriveSentinel", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def _email(cfg, title, body):
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = cfg["smtp_from"] or cfg["smtp_user"]
    msg["To"] = cfg["smtp_to"]
    msg.set_content(body)
    sec = cfg["smtp_security"]
    if sec == "ssl":
        server = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=20,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=20)
        if sec == "starttls":
            server.starttls(context=ssl.create_default_context())
    try:
        if cfg["smtp_user"]:
            server.login(cfg["smtp_user"], cfg["smtp_pass"])
        server.send_message(msg)
    finally:
        server.quit()


def channels(cfg):
    out = []
    if cfg["smtp_host"] and cfg["smtp_to"]:
        out.append("email")
    for key, name in (("discord_webhook", "discord"), ("slack_webhook", "slack"),
                      ("ntfy_url", "ntfy"), ("webhook_url", "webhook")):
        if cfg[key]:
            out.append(name)
    if cfg["telegram_token"] and cfg["telegram_chat_id"]:
        out.append("telegram")
    return out


def send(cfg, level, title, message, extra=None):
    """Send to every configured channel. Returns {channel: 'ok' | error string}."""
    results = {}
    full_title = f"{EMOJI.get(level, '')} [DriveSentinel] {title}".strip()
    for ch in channels(cfg):
        try:
            if ch == "email":
                _email(cfg, full_title, message)
            elif ch == "discord":
                _post_json(cfg["discord_webhook"], {"embeds": [{
                    "title": full_title[:256], "description": message[:4000],
                    "color": COLORS.get(level, 0x888888)}]})
            elif ch == "slack":
                _post_json(cfg["slack_webhook"], {"text": f"*{full_title}*\n{message}"})
            elif ch == "telegram":
                _post_json(f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage",
                           {"chat_id": cfg["telegram_chat_id"], "text": f"{full_title}\n\n{message}"})
            elif ch == "ntfy":
                headers = {"Title": full_title.encode("ascii", "ignore").decode().strip(),
                           "Priority": {"critical": "5", "warning": "4"}.get(level, "3"),
                           "Tags": {"critical": "rotating_light", "warning": "warning"}.get(level, "floppy_disk")}
                if cfg["ntfy_token"]:
                    headers["Authorization"] = f"Bearer {cfg['ntfy_token']}"
                req = urllib.request.Request(cfg["ntfy_url"], data=message.encode(), method="POST",
                                             headers=headers)
                urllib.request.urlopen(req, timeout=15).close()
            elif ch == "webhook":
                _post_json(cfg["webhook_url"], {"level": level, "title": title,
                                                "message": message, **(extra or {})})
            results[ch] = "ok"
        except Exception as e:  # noqa: BLE001 - report any delivery failure
            results[ch] = f"error: {e}"
    return results
