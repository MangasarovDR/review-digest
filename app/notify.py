import os

import httpx

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram_alert(message: str) -> None:
    """Уведомление в Telegram (тот же бот, что и claude-bot). Не должно ронять
    основной процесс — при любой проблеме с отправкой просто логируем в stdout."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[notify] TELEGRAM_BOT_TOKEN/CHAT_ID не заданы, пропускаю: {message[:200]}")
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10.0,
        )
    except Exception as e:
        print(f"[notify] не удалось отправить уведомление в Telegram: {e}")
