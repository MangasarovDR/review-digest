"""ОТСОЕДИНЁН ОТ ОПРОСА 25.08.2026 — НЕ ПОДКЛЮЧАТЬ ОБРАТНО БЕЗ РАЗГОВОРА.

Решение Давида. Площадка убрана из FETCHER_BY_PLATFORM в scripts/daily_update.py,
из _IDENTIFY_BY_PLATFORM в app/main.py и из PLATFORMS_AVAILABLE в app/db.py —
опросить её теперь нельзя ни по расписанию, ни руками, ни добавив источник
через форму.

Почему: сбор держится на резидентском прокси (`proxy_pool`), а подписка
pool.proxy.market протухла 19.08.2026 — пул отвечает 407 Proxy Authentication
Required. Без прокси WB блокирует по IP. Это уже второй заход на те же грабли:
в первый раз скрипт продолжал долбить заблокированную площадку клиент за
клиентом, из-за чего и появился предохранитель PLATFORM_FAILURE_THRESHOLD.

Файл оставлен целиком, а не удалён: в нём записана рабочая связка обхода
(nmId/seller из ссылки, обход каталога продавца до естественного конца,
ротация прокси). Знание добыто эмпирически, выбрасывать его вместе с
проводкой незачем.

Уже собранные отзывы остаются в базе и продолжают показываться клиенту:
источники просто перестают опрашиваться.
"""

import json
import re
import subprocess
import time

import httpx

from .base import Review
from .proxy_pool import get_proxy, track_usage

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

NMID_RE = re.compile(r"/catalog/(\d+)/")
SELLER_RE = re.compile(r"/seller/(\d+)")

# страховка по времени вместо магического числа страниц: обходим каталог
# продавца до естественного конца (пустая страница или len(products) >= total,
# см. _seller_products), но не дольше этого бюджета — на случай сломанной
# пагинации/аномально огромного продавца. Каждая страница — дешёвый curl-запрос
# (обычно <2с), несколько минут с запасом хватает на тысячи товаров.
CATALOG_MAX_FETCH_SECONDS = 180


def _extract_nm_id(url: str) -> str | None:
    match = NMID_RE.search(url)
    return match.group(1) if match else None


def _extract_seller_id(url: str) -> str | None:
    match = SELLER_RE.search(url)
    return match.group(1) if match else None


CURL_RETRY_STATUSES = {"429", "403"}
# пул proxy.market общий — заметная доля выходных IP уже словила лимит от
# ДРУГИХ клиентов пула, не от нас; наблюдалось ~20% успешных попыток на
# catalog.wb.ru, поэтому попыток больше, чем можно было бы ожидать для
# "нормального" ретрая. Неудачные ответы маленькие (страница ошибки),
# трафика это почти не ест.
CURL_MAX_ATTEMPTS = 6
CURL_RETRY_DELAY_SECONDS = 1.0


def _curl_json(url: str, timeout: float) -> dict:
    """GET через curl-subprocess вместо httpx — и card.wb.ru, и catalog.wb.ru
    блокируют httpx по TLS-фингерпринту (403 даже с идентичными заголовками и
    HTTP/2), но пропускают curl. feedbacks*.wb.ru так не делает — там httpx.

    Прокси (pool.proxy.market) — общий ротационный пул, часть выходных IP
    уже словили лимит от других клиентов пула, а не от нас; при 429/403
    повторяем запрос — ротация обычно подсовывает рабочий IP за 2-3 попытки.
    """
    proxy = get_proxy("wildberries")
    last_code = None
    for attempt in range(CURL_MAX_ATTEMPTS):
        cmd = [
            "curl", "-s", "--compressed", "--max-time", str(int(timeout)), "-A", USER_AGENT,
            "-w", "\n__HTTP_CODE__:%{http_code}",
        ]
        if proxy:
            cmd += ["-x", proxy]
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        body, _, code_marker = result.stdout.rpartition("\n__HTTP_CODE__:")
        last_code = code_marker.strip()
        if last_code not in CURL_RETRY_STATUSES:
            if proxy:
                track_usage(len(body.encode("utf-8")))
            return json.loads(body)
        if proxy and attempt < CURL_MAX_ATTEMPTS - 1:
            time.sleep(CURL_RETRY_DELAY_SECONDS)
    raise ValueError(f"WB стабильно отдаёт HTTP {last_code} после {CURL_MAX_ATTEMPTS} попыток: {url}")


def _card_detail(nm_id: str, timeout: float) -> dict:
    """Данные товара (root, name, brand, ...) с card.wb.ru — используется и для
    поиска root (fetch_reviews), и для имени/бренда (identify)."""
    url = f"https://card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={nm_id}"
    products = _curl_json(url, timeout).get("products", [])
    if not products:
        raise ValueError(f"товар nmId={nm_id} не найден")
    return products[0]


def _resolve_root(nm_id: str, timeout: float) -> int:
    """nmId (конкретный товар/цвет) -> root (группа товара, по которой считаются отзывы)."""
    return _card_detail(nm_id, timeout)["root"]


def _seller_products(seller_id: str, timeout: float) -> list[dict]:
    """Все товары продавца (постранично) — публичный каталожный эндпоинт,
    тот же, которым пользуется страница продавца на сайте. Идём до
    естественного конца (пустая страница или набрали total), не по числу
    страниц — см. CATALOG_MAX_FETCH_SECONDS про страховку по времени."""
    products: list[dict] = []
    page = 0
    t0 = time.time()
    while time.time() - t0 < CATALOG_MAX_FETCH_SECONDS:
        page += 1
        url = (
            "https://catalog.wb.ru/sellers/v4/catalog"
            f"?ab_testing=false&appType=1&curr=rub&dest=-1257786&sort=popular"
            f"&spp=30&supplier={seller_id}&page={page}"
        )
        try:
            data = _curl_json(url, timeout)
        except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError):
            break
        page_products = data.get("products", [])
        if not page_products:
            break
        products.extend(page_products)
        total = data.get("total")
        if total is not None and len(products) >= total:
            break
    return products


def _fetch_reviews_for_root(root: int, client: httpx.Client, limit: int, metered: bool) -> list[Review]:
    """feedbacks/v1 и v2 у WB — не постраничные API, а фиксированная выдача
    "последние N отзывов" без поддержки пагинации: ?skip=/&take= эмпирически
    проверены (2026-07-25) и полностью игнорируются — три запроса с разным
    skip на товаре с 47967 отзывами вернули идентичный набор. v1 отдаёт ровно
    1000, v2 — чуть больше (~1039, на разных товарах слегка плавает), это и
    есть настоящий потолок на стороне WB, обойти нечем. limit здесь — просто
    подстраховка на случай, если WB когда-нибудь начнёт отдавать больше;
    сам код ничего не обрезает раньше времени."""
    reviews: list[Review] = []
    for host in ("feedbacks2.wb.ru", "feedbacks1.wb.ru"):
        resp = client.get(f"https://{host}/feedbacks/v2/{root}")
        if resp.status_code != 200:
            continue
        if metered:
            track_usage(len(resp.content))
        data = resp.json()
        for fb in (data.get("feedbacks") or [])[:limit]:
            text = " ".join(filter(None, [fb.get("text"), fb.get("pros"), fb.get("cons")])).strip()
            if not text:
                continue
            reviews.append(
                Review(
                    platform="wildberries",
                    external_id=fb["id"],
                    author=(fb.get("wbUserDetails") or {}).get("name") or "Аноним",
                    rating=float(fb.get("productValuation", 0)),
                    text=text,
                    review_date=fb.get("createdDate", ""),
                )
            )
        if reviews:
            break  # нашли отзывы на этом шарде, второй не нужен
    return reviews


def fetch_reviews(url: str, limit: int = 1500, timeout: float = 20.0) -> list[Review]:
    """Отзывы Wildberries — прозрачно по ссылке на карточку товара ИЛИ на
    магазин продавца целиком. Публичные JSON-эндпоинты, без браузера.

    Ссылка на товар (/catalog/{nmId}/) -> отзывы этого товара.
    Ссылка на продавца (/seller/{sellerId}) -> отзывы по всем его товарам.

    limit=1500 — заведомо выше настоящего потолка API (~1000-1039 на root,
    см. докстринг _fetch_reviews_for_root), то есть на практике не обрезает
    ничего; это просто разумная защита типа сигнатуры, а не рабочий лимит.
    Для продавца с несколькими товарами общий объём органически больше —
    там ограничивает только время обхода каталога (CATALOG_MAX_FETCH_SECONDS).
    """
    nm_id = _extract_nm_id(url)
    seller_id = None if nm_id is not None else _extract_seller_id(url)

    if nm_id is None and seller_id is None:
        raise ValueError(
            f"не удалось распознать ссылку Wildberries (ни /catalog/{{id}}/, ни /seller/{{id}}): {url}"
        )

    proxy = get_proxy("wildberries")
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, proxy=proxy) as client:
        if nm_id is not None:
            root = _resolve_root(nm_id, timeout)
            return _fetch_reviews_for_root(root, client, limit, metered=bool(proxy))

        products = _seller_products(seller_id, timeout)
        if not products:
            raise ValueError(f"у продавца {seller_id} не нашлось товаров")
        reviews: list[Review] = []
        for p in products:
            reviews.extend(_fetch_reviews_for_root(p["root"], client, limit, metered=bool(proxy)))
        return reviews


def identify(url: str, timeout: float = 20.0) -> str | None:
    """Название+бренд товара (для ссылки на карточку) либо название магазина
    продавца (для ссылки на весь каталог продавца) — для отображения в списке
    площадок клиента. Дёргается один раз при добавлении ссылки."""
    try:
        nm_id = _extract_nm_id(url)
        if nm_id is not None:
            card = _card_detail(nm_id, timeout)
            brand, name = card.get("brand"), card.get("name")
            return f"{brand} — {name}" if brand and name else (name or brand)

        seller_id = _extract_seller_id(url)
        if seller_id is not None:
            products = _seller_products(seller_id, timeout)
            if products:
                return f"Магазин: {products[0].get('supplier') or seller_id}"
    except Exception:
        pass
    return None
