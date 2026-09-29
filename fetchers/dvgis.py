import re
import time

import httpx

from .base import Review
from .proxy_pool import get_proxy

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Публичный фронтенд-ключ 2ГИС, встроен в разметку любой страницы 2gis.ru —
# не секрет, используется собственным клиентским JS сайта для чтения отзывов.
REVIEW_API_KEY = "6e7e1929-4ea9-4a5d-8c05-d601860389bd"

ORG_ID_RE = re.compile(r"/firm/(\d+)")


def _extract_org_id(org_url: str, timeout: float = 20.0) -> str:
    match = ORG_ID_RE.search(org_url)
    if match:
        return match.group(1)

    # короткие ссылки-шаринг (go.2gis.com/XXXXX) не содержат id напрямую —
    # id виден уже в первом редиректе (до финальной "museum"-страницы про
    # старый браузер), поэтому ищем по всей цепочке редиректов, а не только
    # по итоговому URL
    resp = httpx.get(
        org_url, headers={"User-Agent": USER_AGENT}, timeout=timeout,
        follow_redirects=True, proxy=get_proxy("2gis"),
    )
    for step in [*resp.history, resp]:
        match = ORG_ID_RE.search(str(step.url))
        if match:
            return match.group(1)

    raise ValueError(f"не удалось найти id организации в ссылке 2ГИС: {org_url}")


# общий потолок постраничного обхода — не число страниц/отзывов, а время:
# API отдаёт meta.next_link, пока не кончится лента, страниц может быть много
# на организации с тысячами отзывов, но каждый запрос дешёвый (JSON, без браузера)
MAX_FETCH_SECONDS = 120


def fetch_reviews(org_url: str, limit: int | None = None, timeout: float = 20.0) -> list[Review]:
    """Отзывы 2ГИС по ссылке на организацию — через публичный Reviews API (3.0).

    Playwright не используется в рантайме: сама страница 2gis.ru — SPA-шелл без
    данных в HTML, но её JS ходит в public-api.reviews.2gis.com с публичным
    фронтенд-ключом, который можно дёргать напрямую.

    API поддерживает нормальную курсорную пагинацию — offset=... и
    meta.next_link с готовой ссылкой на следующую страницу (проверено
    2026-07-25 на организации с сотнями отзывов: третья подряд страница по
    next_link отдаёт новые, более старые отзывы, а не повтор). Раньше limit=50
    без пагинации был искусственным потолком; теперь идём по next_link до
    естественного конца (пустой reviews или отсутствие next_link) — limit,
    если передан явно, просто ранний выход, по умолчанию не ограничивает."""
    org_id = _extract_org_id(org_url, timeout=timeout)

    reviews: list[Review] = []
    url = f"https://public-api.reviews.2gis.com/3.0/branches/{org_id}/reviews"
    params = {"key": REVIEW_API_KEY, "locale": "ru_RU", "limit": 50, "sort_by": "date_created"}
    t0 = time.time()

    while url and time.time() - t0 < MAX_FETCH_SECONDS:
        resp = httpx.get(
            url, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout, proxy=get_proxy("2gis"),
        )
        resp.raise_for_status()
        data = resp.json()
        page_reviews = data.get("reviews", [])
        if not page_reviews:
            break
        reviews.extend(page_reviews)
        if limit is not None and len(reviews) >= limit:
            reviews = reviews[:limit]
            break
        url = (data.get("meta") or {}).get("next_link")
        params = None  # next_link уже содержит все параметры

    out: list[Review] = []
    for r in reviews:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        out.append(
            Review(
                platform="2gis",
                external_id=str(r["id"]),
                author=(r.get("user") or {}).get("name") or "Аноним",
                rating=float(r.get("rating") or 0),
                text=text,
                review_date=r.get("date_created", ""),
            )
        )
    return out


def identify(org_url: str, timeout: float = 20.0) -> str | None:
    """Название/адрес организации — для отображения в списке площадок клиента.
    Дёргается один раз при добавлении ссылки, не при каждом скрапе.

    2ГИС не отдаёт название/адрес через открытые API без полноценного ключа
    каталога (в отличие от отзывов, для которых нашёлся публичный фронтенд-
    ключ) — единственный надёжный источник — заголовок страницы после её
    полной отрисовки. Поэтому здесь, в отличие от fetch_reviews, используется
    Playwright — но только для этого разового вызова при добавлении ссылки.
    """
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            try:
                page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
                page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page.goto(org_url, timeout=int(timeout * 1000), wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                btn = page.query_selector("#acceptRiskButton")
                if btn:
                    btn.click()
                    page.wait_for_timeout(3000)
                title = page.title()
            finally:
                browser.close()
        title = re.sub(r"\s*[—-]\s*2ГИС\s*$", "", title).strip()
        title = re.sub(r"^Отзывы\s+о[бн]?\s+«?", "", title, flags=re.IGNORECASE).strip(" »")
        return title or None
    except Exception:
        return None
