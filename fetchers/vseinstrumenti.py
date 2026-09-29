"""ОТСОЕДИНЁН ОТ ОПРОСА 19.08.2026 — НЕ ПОДКЛЮЧАТЬ ОБРАТНО БЕЗ РАЗГОВОРА.

Решение Давида: «слишком много проблем с ней для бесплатного инструмента».
Площадка убрана из FETCHER_BY_PLATFORM в scripts/daily_update.py и app/main.py
и из PLATFORMS_AVAILABLE в app/db.py, поэтому опросить её нельзя ни по
расписанию, ни руками, ни добавив источник через форму.

Файл оставлен целиком, а не удалён: в нём записано, какая именно связка
проходит антибот Servicepipe (patchright + резидентский RU-прокси) и что
именно не работает по отдельности. Это знание добыто эмпирически и стоило
нескольких дней; выбрасывать его вместе с проводкой незачем.

Что было не так, коротко: обход держится на живом браузере и покупном прокси,
ломается после каждого обновления защиты и требует ручной починки — при том
что сама площадка в наборе бесплатная. Цена сопровождения не сходится с
ценностью. Уже собранные 107 отзывов остаются в базе и продолжают
показываться клиенту.
"""

import hashlib
import json
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from .base import Review
from .proxy_pool import get_proxy, track_usage

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PRODUCT_REVIEW_RE = re.compile(r"/product/[a-zA-Z0-9\-]+/otzyvy/")
JSONLD_PRODUCT_RE = re.compile(
    r'<script type="application/ld\+json">(\{.*?"@type":"Product".*?\})</script>', re.S
)
TITLE_SUFFIX_RE = re.compile(
    r"\s*[—-]\s*официальный дилер.*$|\s*[—-]\s*интернет-магазин\s+ВсеИнструменты\.ру\s*$",
    re.IGNORECASE,
)

# vseinstrumenti.ru закрыт антиботом Servicepipe: обычный httpx/curl всегда
# получает 403, headless-браузер без резидентского прокси упирается в
# rotated-image captcha (sp_rotated_captcha), обойти которую подбором пикселей
# не получилось (см. память review-digest-vseinstrumenti-blocked). Рабочая
# комбинация, найденная эмпирически 2026-07-23: patchright (форк Playwright
# с патчами против CDP-фингерпринт-детекта) + купленный резидентский RU-прокси
# (тот же пул, что для Wildberries, см. proxy_pool.py) — вместе они почти
# всегда проходят проверку без капчи вообще. Ни один компонент по отдельности
# не работал надёжно (голый Playwright+прокси зависал в петле капчи, patchright
# без прокси капчу тоже не проходил) — прокси снимает IP-репутационную
# проверку, patchright снимает автоматизационный фингерпринт.
# пул proxy.market общий — та же оговорка про ~20% успешности с первой
# попытки, что и у Wildberries (fetchers/wildberries.py) — доля выходных IP
# уже занята/зафлагована активностью ДРУГИХ клиентов пула. Поэтому попыток
# больше, чем можно было бы ожидать для "нормального" ретрая.
MAX_LOAD_ATTEMPTS = 5
# каталог бренда не пагинируется (берём только первую страницу выдачи) —
# сознательное ограничение объёма, как и лимит по товарам ниже; для среднего
# бренда типа KEOS покрывает весь актуальный ассортимент с отзывами
MAX_PRODUCTS = 50
CAPTCHA_PAGE_MAX_LEN = 20000  # страница капчи ~16 КБ, реальный контент много больше

# Частичная глубина отзывов на товар (2026-07-25): у страницы товара есть
# кнопка "Показать ещё" (data-qa="review-show-more"), клик по которой шлёт
# GET на bff.vseinstrumenti.ru/api/v1/reviews?page=N — то есть нормальная
# постраничная пагинация есть, просто скрыта за JS, а не URL (/otzyvy/2/ даёт
# 404). Полный обход до естественного конца сюда сознательно НЕ добавлен:
# bff-эндпоинт при прямом запросе (в обход уже прошедшей антибот-проверку
# страницы) отдаёт 403 — то есть у него, похоже, СВОЯ отдельная проверка,
# и на сессии с уже отвратительным состоянием прокси-пула (0/9 успешных
# загрузок страницы подряд) клики иногда не добавляли ни одной новой карточки
# даже когда сама страница товара загружалась. Каждый клик — это ещё один
# шанс упереться в эту вторую проверку, поэтому решение (сознательный
# компромисс, а не полный "естественный конец", как для остальных площадок) —
# несколько попыток кликнуть, остановиться сразу же, как только клик не дал
# роста, и не пытаться снова на этом товаре. 4 клика — при полном успехе
# поднимает глубину с ~10 до ~50 на товар за разумное дополнительное время,
# не растягивая и так недешёвый (один успешный проход антибота) фетч товара.
SHOW_MORE_MAX_CLICKS = 4
SHOW_MORE_WAIT_MS = 2500


def _proxy_config() -> tuple[dict | None, bool]:
    proxy = get_proxy("vseinstrumenti")
    if not proxy:
        return None, False
    pu = urlparse(proxy)
    return (
        {
            "server": f"{pu.scheme}://{pu.hostname}:{pu.port}",
            "username": pu.username,
            "password": pu.password,
        },
        True,
    )


def _new_context(p):
    proxy_cfg, metered = _proxy_config()
    browser = p.chromium.launch(headless=True, proxy=proxy_cfg)
    context = browser.new_context(
        user_agent=USER_AGENT, viewport={"width": 1280, "height": 900}, locale="ru-RU"
    )
    return browser, context, metered


def _load_clean(page, url: str, timeout: float) -> str | None:
    """Открывает страницу, дожидаясь либо реального контента, либо капчи.
    Возвращает HTML, если удалось получить реальный контент за
    MAX_LOAD_ATTEMPTS попыток, иначе None (площадка временно недоступна для
    этого запуска — не роняем весь прогон, просто пропускаем страницу)."""
    for attempt in range(MAX_LOAD_ATTEMPTS):
        try:
            page.goto(url, timeout=int(timeout * 1000), wait_until="load")
        except Exception:
            pass
        html = None
        for _ in range(8):
            time.sleep(1.5)
            try:
                html = page.content()
                if "captcha-root" in html or len(html) > CAPTCHA_PAGE_MAX_LEN:
                    break
            except Exception:
                continue
        if html and "captcha-root" not in html and len(html) > CAPTCHA_PAGE_MAX_LEN:
            return html
        if attempt < MAX_LOAD_ATTEMPTS - 1:
            time.sleep(1.0)
    return None


def _parse_date(raw: str) -> str:
    try:
        return datetime.strptime(raw, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_dom_date(raw: str) -> str:
    try:
        return datetime.strptime(raw.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _expand_reviews(page) -> None:
    """Кликает "Показать ещё" до SHOW_MORE_MAX_CLICKS раз, добирая отзывы сверх
    первой страницы (см. докстринг SHOW_MORE_MAX_CLICKS про bff-эндпоинт и его
    отдельную антибот-проверку). Останавливается сразу, как только очередной
    клик не увеличил число карточек в DOM — либо кнопка реально пропала
    (естественный конец), либо запрос за новой страницей не прошёл (тогда
    дальнейшие попытки на этом же товаре тоже, скорее всего, не пройдут)."""
    count_js = 'document.querySelectorAll(\'[data-qa="review-item"]\').length'
    prev = page.evaluate(count_js)
    for _ in range(SHOW_MORE_MAX_CLICKS):
        btn = page.query_selector('[data-qa="review-show-more"]')
        if not btn:
            break
        try:
            btn.click()
        except Exception:
            break
        page.wait_for_timeout(SHOW_MORE_WAIT_MS)
        cur = page.evaluate(count_js)
        if cur <= prev:
            break
        prev = cur


_DOM_REVIEWS_JS = """
() => Array.from(document.querySelectorAll('[data-qa="review-item"]')).map(item => {
  const person = item.querySelector('[data-qa="person-name"]');
  const dateEl = item.querySelector('[data-qa="date-review"]');
  const ratingInput = item.querySelector('input[name="rating"]');
  const clone = item.cloneNode(true);
  clone.querySelectorAll(
    '[data-qa="person-name"], [data-qa="date-review"], [data-qa="group-characteristic-link"], ' +
    '[data-qa="reply-button"], [data-qa="like-button"], button, svg, input'
  ).forEach(el => el.remove());
  return {
    author: person ? person.innerText.trim() : '',
    date: dateEl ? dateEl.innerText.trim() : '',
    rating: ratingInput ? ratingInput.value : '',
    text: clone.innerText.replace(/\\s+/g, ' ').trim(),
  };
})
"""


def _parse_dom_reviews(page, product_path: str, limit: int) -> list[Review]:
    """Отзывы прямо из живого DOM (после _expand_reviews) — карточки со
    стабильными data-qa атрибутами (person-name/date-review/total-stars),
    а не CSS-классы (те — сборочные хэши, могут смениться при любом релизе
    фронтенда vseinstrumenti). Этот путь уже включает первую страницу (те же
    ~10, что отдаёт и JSON-LD) плюс всё, что реально добавили клики — отдельно
    объединять с _parse_reviews не нужно, см. вызов в fetch_reviews."""
    items = page.evaluate(_DOM_REVIEWS_JS)
    out: list[Review] = []
    for it in items[:limit]:
        text = (it.get("text") or "").strip()
        if not text:
            continue
        author = it.get("author") or "Аноним"
        review_date = _parse_dom_date(it.get("date") or "")
        rating = float(it.get("rating") or 0)
        external_id = hashlib.sha256(
            f"{product_path}|{author}|{review_date}|{text[:100]}".encode()
        ).hexdigest()
        out.append(
            Review(
                platform="vseinstrumenti",
                external_id=external_id,
                author=author,
                rating=rating,
                text=text,
                review_date=review_date,
            )
        )
    return out


def _parse_reviews(html: str, product_path: str, limit: int) -> list[Review]:
    """Отзывы товара из JSON-LD (schema.org Product/Review) в SSR-разметке —
    запасной путь на случай, если _parse_dom_reviews не нашёл карточек (см. её
    докстринг) — отдаёт только первую страницу (~10 отзывов), без кликов."""
    match = JSONLD_PRODUCT_RE.search(html)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    out: list[Review] = []
    for r in (data.get("review") or [])[:limit]:
        text = (r.get("description") or "").strip()
        if not text:
            continue
        author = (r.get("author") or {}).get("name") or "Аноним"
        rating = float((r.get("reviewRating") or {}).get("ratingValue") or 0)
        date_raw = r.get("datePublished") or ""
        external_id = hashlib.sha256(
            f"{product_path}|{author}|{date_raw}|{text[:100]}".encode()
        ).hexdigest()
        out.append(
            Review(
                platform="vseinstrumenti",
                external_id=external_id,
                author=author,
                rating=rating,
                text=text,
                review_date=_parse_date(date_raw),
            )
        )
    return out


def fetch_reviews(url: str, limit: int = 200, timeout: float = 30.0) -> list[Review]:
    """Отзывы по ссылке на страницу бренда (напр. /brand/keos-13458/) — обходит
    все товары бренда (первая страница каталога, до MAX_PRODUCTS штук). На
    каждом товаре — частичная глубина (см. SHOW_MORE_MAX_CLICKS): несколько
    кликов "Показать ещё" поверх первой страницы, затем чтение из живого DOM
    (_parse_dom_reviews), с откатом на JSON-LD первой страницы (_parse_reviews)
    если DOM-путь ничего не нашёл. Требует браузер (patchright) из-за антибота
    — см. модульный докстринг выше."""
    from patchright.sync_api import sync_playwright

    reviews: list[Review] = []
    with sync_playwright() as p:
        browser, context, metered = _new_context(p)
        try:
            page = context.new_page()
            brand_html = _load_clean(page, url, timeout)
            if brand_html is None:
                raise ValueError(f"vseinstrumenti.ru не пропустил через антибот: {url}")
            if metered:
                track_usage(len(brand_html.encode("utf-8")))

            base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
            product_paths = sorted(set(PRODUCT_REVIEW_RE.findall(brand_html)))[:MAX_PRODUCTS]

            for path in product_paths:
                product_html = _load_clean(page, base + path, timeout)
                if product_html is None:
                    continue  # площадка отбила эту страницу — пропускаем, не роняем весь прогон
                if metered:
                    track_usage(len(product_html.encode("utf-8")))

                _expand_reviews(page)
                dom_reviews = _parse_dom_reviews(page, path, limit)
                reviews.extend(dom_reviews if dom_reviews else _parse_reviews(product_html, path, limit))
        finally:
            browser.close()
    return reviews[:limit]


def identify(url: str, timeout: float = 30.0) -> str | None:
    """Название бренда — для отображения в списке площадок клиента. Дёргается
    один раз при добавлении ссылки, не при каждом скрапе."""
    from patchright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser, context, metered = _new_context(p)
            try:
                page = context.new_page()
                html = _load_clean(page, url, timeout)
                if html is None:
                    return None
                if metered:
                    track_usage(len(html.encode("utf-8")))
                title = page.title()
            finally:
                browser.close()
        return TITLE_SUFFIX_RE.sub("", title).strip() or None
    except Exception:
        return None
