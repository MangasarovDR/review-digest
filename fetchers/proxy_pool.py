"""Заготовка под ротацию прокси по площадкам.

Пока ни у одной площадки прокси не куплены — get_proxy() возвращает None,
и все фетчеры работают как раньше, напрямую. Когда прокси появятся (см.
память proxy-market-ru-provider), достаточно прописать список в .env —
код фетчеров менять не придётся, пересборка образа не нужна.

Формат env-переменной (запятая между адресами, авторизация — в самом URL):
    WILDBERRIES_PROXIES=http://user:pass@host1:port,http://user:pass@host2:port
    OZON_PROXIES=...
    YANDEX_MAPS_PROXIES=...
    2GIS_PROXIES=...
Общий пул на все площадки — PROXIES (используется, если для площадки нет
своей переменной).

Ротация — простой round-robin в памяти процесса; для cron (короткоживущий
процесс на один прогон) этого достаточно, чтобы размазать запросы по пулу.
"""
import itertools
import os

from app.db import add_proxy_bytes, is_proxy_alert_sent, mark_proxy_alert_sent
from app.notify import send_telegram_alert

# у купленного пакета (pool.proxy.market) ограниченный трафик — предупреждаем
# заранее, чтобы не улететь в 0 и не остаться без прокси посреди дня
PROXY_BUDGET_MB = float(os.environ.get("PROXY_BUDGET_MB", "1024"))
PROXY_ALERT_AT_MB = float(os.environ.get("PROXY_ALERT_AT_MB", "900"))

_pools: dict[str, "itertools.cycle[str]"] = {}


def _load_pool(platform: str) -> list[str]:
    env_key = f"{platform.upper()}_PROXIES"
    raw = os.environ.get(env_key) or os.environ.get("PROXIES", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def get_proxy(platform: str) -> str | None:
    """Следующий прокси из пула площадки (round-robin), либо None, если пул пуст."""
    if platform not in _pools:
        pool = _load_pool(platform)
        _pools[platform] = itertools.cycle(pool) if pool else None
    cycler = _pools[platform]
    return next(cycler) if cycler else None


def track_usage(nbytes: int) -> None:
    """Учёт трафика, прошедшего через платный прокси. Вызывать только когда
    запрос реально шёл через прокси (get_proxy() вернул не None) — иначе
    посчитаем прямые запросы против чужого лимита."""
    if nbytes <= 0:
        return
    total = add_proxy_bytes(nbytes)
    total_mb = total / (1024 * 1024)
    if total_mb >= PROXY_ALERT_AT_MB and not is_proxy_alert_sent():
        mark_proxy_alert_sent()
        send_telegram_alert(
            f"⚠️ <b>Прокси pool.proxy.market — израсходовано {total_mb:.0f} МБ из {PROXY_BUDGET_MB:.0f} МБ</b>\n"
            "Лимит трафика близко. Проверь баланс на ru.dashboard.proxy.market "
            "и пополни/расширь пакет, иначе Wildberries снова начнёт виснуть без прокси."
        )
