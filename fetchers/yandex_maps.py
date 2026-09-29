import hashlib
import re
import time

import httpx
from bs4 import BeautifulSoup

from .base import Review
from .proxy_pool import get_proxy

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# страховка по времени вместо магического числа страниц — обычные HTTP-запросы
# без браузера, дёшево идти до естественного конца даже на организации с
# тысячами отзывов; несколько минут с запасом хватает
MAX_FETCH_SECONDS = 120


def _reviews_url(org_url: str, page: int) -> str:
    url = org_url.rstrip("/")
    if not url.endswith("/reviews"):
        url = f"{url}/reviews"
    suffix = f"?ranking=by_time&page={page}" if page > 1 else "?ranking=by_time"
    return f"{url}/{suffix}"


def _make_external_id(author_url: str, date: str, text: str) -> str:
    digest_input = f"{author_url}|{date}|{text[:200]}".encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()[:24]


def fetch_reviews(org_url: str, timeout: float = 20.0) -> list[Review]:
    """Отзывы Yandex Maps по ссылке на организацию — обычные HTTP-запросы,
    без браузера (отзывы отрендерены на сервере в HTML).

    Раньше читалась только первая страница (~50 отзывов, докстринг прямо
    говорил "up to ~50") — не потому что у Яндекса нет пагинации, а потому что
    её просто не реализовали. Эмпирически (2026-07-25) страница поддерживает
    обычный &page=N: проверено на организации с 250 отзывами — page=2 не
    пересекается с page=1 (0 общих отзывов), даты монотонно уходят в прошлое,
    на page=6 (250/50=5 полных страниц) приходит пустой ответ — естественный
    конец. Идём по страницам, пока не придёт пустая, с таймаутом
    MAX_FETCH_SECONDS на случай аномалии/бесконечной пагинации.
    """
    reviews: list[Review] = []
    seen_ids: set[str] = set()
    t0 = time.time()
    page = 1
    while time.time() - t0 < MAX_FETCH_SECONDS:
        resp = httpx.get(
            _reviews_url(org_url, page),
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
            proxy=get_proxy("yandex_maps"),
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        blocks = soup.select('div[itemprop="review"]')
        if not blocks:
            break

        page_had_new = False
        for block in blocks:
            author_tag = block.select_one('[itemprop="author"] [itemprop="name"]')
            author_link = block.select_one('[itemprop="author"] a')
            rating_tag = block.select_one('[itemprop="reviewRating"] [itemprop="ratingValue"]')
            date_tag = block.select_one('[itemprop="datePublished"]')
            body_tag = block.select_one('[itemprop="reviewBody"]')

            if not (author_tag and rating_tag and date_tag and body_tag):
                continue

            author = author_tag.get_text(strip=True)
            author_url = author_link["href"] if author_link and author_link.has_attr("href") else ""
            rating = float(rating_tag.get("content", "0"))
            date = date_tag.get("content", "")
            text = re.sub(r"\s+", " ", body_tag.get_text(" ", strip=True)).strip()
            external_id = _make_external_id(author_url, date, text)

            if external_id in seen_ids:
                continue  # подстраховка от дублей на случай пересечения страниц
            seen_ids.add(external_id)
            page_had_new = True

            reviews.append(
                Review(
                    platform="yandex_maps",
                    external_id=external_id,
                    author=author,
                    rating=rating,
                    text=text,
                    review_date=date,
                )
            )

        if not page_had_new:
            break  # страница пришла, но без новых отзывов — тоже естественный конец
        page += 1

    return reviews


ADDRESS_RE = re.compile(r'"address":"([^"]+)"')


def identify(org_url: str, timeout: float = 20.0) -> str | None:
    """Адрес организации — для отображения в списке площадок клиента.
    Дёргается один раз при добавлении ссылки, не при каждом скрапе."""
    try:
        resp = httpx.get(
            org_url, headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True,
            proxy=get_proxy("yandex_maps"),
        )
        resp.raise_for_status()
        match = ADDRESS_RE.search(resp.text)
        if match:
            return match.group(1)
        title = BeautifulSoup(resp.text, "html.parser").title
        if title:
            return re.sub(r"\s*—\s*(Yandex|Яндекс)\.?\s*Maps?\.?(Карты)?\s*$", "", title.get_text()).strip()
    except Exception:
        pass
    return None
