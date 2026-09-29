import datetime
import hashlib
import re
import time
import urllib.parse

from .base import Review

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TITLE_SUFFIX_RE = re.compile(r"\s*[–-]\s*Google\s*Карты\s*$")
LATLNG_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")

MAX_REVIEWS = 20000  # практически без потолка — реальная граница ниже, по времени (MAX_FETCH_SECONDS)
SCROLL_STEP_PX = 700
SCROLL_WAIT_MS = 2200
SCROLL_STABLE_ROUNDS = 18  # см. докстринг fetch_reviews — подгрузка идёт пачками раз в ~6-10 раундов
SCROLL_MAX_ROUNDS = 3000
MAX_FETCH_SECONDS = 25 * 60  # страховка по времени вместо потолка по числу отзывов — см. докстринг fetch_reviews
SEARCH_ATTEMPTS = 4

_FIND_SCROLL_PARENT_JS = """
() => {
  const card = document.querySelector('div.jftiEf[data-review-id]');
  if (!card) return null;
  let el = card.parentElement;
  for (let i = 0; i < 12 && el; i++) {
    const s = getComputedStyle(el);
    if (el.scrollHeight > el.clientHeight + 20 && (s.overflowY === 'auto' || s.overflowY === 'scroll')) {
      return el;
    }
    el = el.parentElement;
  }
  return null;
}
"""
_GET_CARD_IDS_JS = (
    "Array.from(document.querySelectorAll('div.jftiEf[data-review-id]'))"
    ".map(el => el.getAttribute('data-review-id'))"
)
_EXPAND_ALL_JS = (
    "Array.from(document.querySelectorAll('div.jftiEf[data-review-id] button[aria-label=\"Ещё\"]'))"
    ".forEach(b => b.click())"
)

_REL_RE = re.compile(r"(?P<num>\d+)?\s*(?P<unit>секунд|минут|час|дн|ден|недел|месяц|год|лет)")
_UNIT_TO_DAYS = {
    "секунд": 0, "минут": 0, "час": 0,
    "дн": 1, "ден": 1,
    "недел": 7,
    "месяц": 30,
    "год": 365, "лет": 365,
}


def _parse_relative_date(text: str, today: datetime.date | None = None) -> str:
    """Google отдаёт дату отзыва только относительной строкой ("2 месяца назад",
    "неделю назад" и т.п.), без точного значения — парсим приближённо (день не
    важен, ежедневный дельта-фетч и так не требует точности до дня, см. дух
    остальных фетчеров)."""
    today = today or datetime.date.today()
    t = text.strip().lower()
    if "сегодня" in t:
        return today.isoformat()
    if "вчера" in t:
        return (today - datetime.timedelta(days=1)).isoformat()
    m = _REL_RE.search(t)
    if not m:
        return today.isoformat()
    count = int(m.group("num")) if m.group("num") else 1
    per_day = _UNIT_TO_DAYS.get(m.group("unit"), 0)
    return (today - datetime.timedelta(days=count * per_day)).isoformat()


def _make_external_id(author: str, date: str, text: str) -> str:
    digest_input = f"{author}|{date}|{text[:200]}".encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()[:24]


def _new_page(p, block_images: bool = False):
    browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900}, locale="ru-RU")
    if block_images:
        # фото в отзывах не нужны (текст/рейтинг/дата/автор и так в разметке) —
        # блокировка резко снижает память при глубоком скролле по сотням отзывов,
        # без этого длинный скролл на крупных организациях упирался в OOM
        context.route(re.compile(r".*\.(png|jpe?g|webp|gif)(\?.*)?$"), lambda route: route.abort())
    page = context.new_page()
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return browser, page


def _extract_name_and_coords(page, url: str, timeout: float) -> tuple[str, str | None]:
    page.goto(url, timeout=int(timeout * 1000), wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    name = TITLE_SUFFIX_RE.sub("", page.title()).strip()
    m = LATLNG_RE.search(page.url)
    coords = f"{m.group(1)},{m.group(2)}" if m else None
    return name, coords


def _open_reviews_tab(page, query: str, timeout: float) -> bool:
    """Открывает вкладку отзывов места по поисковому запросу.

    Прямая навигация на страницу места (готовую ссылку из адресной строки или
    !data=... с data-блоком) эмпирически отдаёт "урезанную" версию UI — только
    вкладки "Обзор" и "О месте", без "Меню" и "Отзывы", сколько ни жди
    (проверено вплоть до 9 секунд ожидания и networkidle — не проблема тайминга).
    Тот же самый URL, если до него дойти через поиск (?q=...) и клик по карточке
    результата в списке, стабильно отдаёт полный набор вкладок с "Отзывы о
    месте" — разница именно в способе перехода (in-app клик vs прямая
    навигация/История), а не в самом URL. Поэтому вместо прямой ссылки всегда
    сначала ищем место по названию и кликаем по первому результату.
    """
    for _ in range(SEARCH_ATTEMPTS):
        search_url = f"https://www.google.com/maps?q={urllib.parse.quote(query)}"
        page.goto(search_url, timeout=int(timeout * 1000), wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        first = page.query_selector("a.hfpxzc")
        if first:
            first.click()
            page.wait_for_timeout(3000)
        tab = page.query_selector('button[role="tab"][aria-label*="Отзывы о месте"]')
        if tab:
            tab.click()
            page.wait_for_timeout(2000)
            return True
    return False


def _wait_for_cards(page, min_count: int = 1, timeout_s: float = 10.0) -> int:
    import time as _time

    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        n = page.evaluate("document.querySelectorAll('div.jftiEf[data-review-id]').length")
        if n >= min_count:
            return n
        page.wait_for_timeout(400)
    return 0


def _sort_by_newest(page) -> None:
    """По умолчанию Google сортирует отзывы "по релевантности" — старые
    популярные отзывы годами торчат наверху, из-за чего ежедневный дельта-фетч
    почти никогда не увидел бы ничего нового. Обязательно переключаем на
    "Сначала новые", иначе весь смысл ежедневного опроса теряется."""
    sort_btn = page.query_selector('button[aria-label="Самые релевантные"]')
    if not sort_btn:
        return
    sort_btn.click()
    page.wait_for_timeout(1000)
    newest = page.query_selector('div[role="menuitemradio"]:has-text("Сначала новые")')
    if newest:
        newest.click()
        page.wait_for_timeout(2000)


def fetch_reviews(url: str, limit: int = MAX_REVIEWS, timeout: float = 30.0) -> list[Review]:
    """Отзывы Google Карт по ссылке на организацию (полная ссылка из адресной
    строки, короткая maps.app.goo.gl — Playwright сам пройдёт редирект, или
    ссылка вида /maps/place/...).

    У Google нет публичного фронтенд-API-ключа, как у 2ГИС — данные только
    через Playwright. См. докстринг _open_reviews_tab про обязательный
    поиск+клик вместо прямой навигации.

    Глубина: лента подгружается по скроллу пачками примерно раз в 6-10 раундов
    (не непрерывно) — первая версия останавливалась после нескольких раундов
    без роста и упиралась в ~10 отзывов, ошибочно приняв паузу между пачками
    за конец ленты. Реального потолка на число отзывов больше нет (MAX_REVIEWS
    практически бесконечен) — останов either когда лента правда кончилась
    (SCROLL_STABLE_ROUNDS раундов подряд без роста), либо по стене времени
    MAX_FETCH_SECONDS (25 минут) для действительно огромных организаций —
    в таком случае просто отдаём то, что успели собрать, оно уже отсортировано
    "сначала новые", то есть самое свежее не теряется. Картинки в отзывах при
    этом блокируются (_new_page(block_images=True)) и подсчёт идёт лёгким
    JS-запросом id, а не page.content() целиком на каждом раунде — иначе
    память улетает в OOM на длинном скролле (см. память
    review-digest-google-maps-glubina). Отзывы в DOM не виртуализируются —
    старые карточки не пропадают по мере подгрузки новых, финальное
    извлечение полей одним проходом по всем накопленным карточкам корректно,
    но именно поэтому память растёт монотонно с числом собранных отзывов —
    для организаций с десятками тысяч отзывов это реальный практический
    предел раньше, чем истечёт MAX_FETCH_SECONDS.
    """
    from playwright.sync_api import sync_playwright

    reviews: list[Review] = []
    with sync_playwright() as p:
        browser, page = _new_page(p, block_images=True)
        try:
            name, coords = _extract_name_and_coords(page, url, timeout)
            if not name:
                raise ValueError(f"не удалось определить название организации по ссылке Google Карт: {url}")
            query = f"{name} {coords}" if coords else name

            if not _open_reviews_tab(page, query, timeout):
                raise ValueError(f"не открылась вкладка отзывов Google Карт для: {name}")
            _wait_for_cards(page)

            _sort_by_newest(page)
            _wait_for_cards(page)

            feed_handle = page.evaluate_handle(_FIND_SCROLL_PARENT_JS)
            if feed_handle.as_element() is not None:
                seen_ids: set[str] = set(page.evaluate(_GET_CARD_IDS_JS))
                stable_rounds = 0
                fetch_deadline = time.monotonic() + MAX_FETCH_SECONDS
                for _ in range(SCROLL_MAX_ROUNDS):
                    if len(seen_ids) >= limit:
                        break
                    if time.monotonic() >= fetch_deadline:
                        # не ограничение по числу отзывов — просто не гонимся за
                        # действительно огромными организациями бесконечно, отдаём
                        # что успели собрать (уже отсортировано "сначала новые")
                        break
                    try:
                        page.evaluate(f"(el) => {{ el.scrollTop += {SCROLL_STEP_PX}; }}", feed_handle)
                    except Exception:
                        feed_handle = page.evaluate_handle(_FIND_SCROLL_PARENT_JS)
                        if feed_handle.as_element() is None:
                            break
                        continue
                    page.wait_for_timeout(SCROLL_WAIT_MS)
                    current_ids = set(page.evaluate(_GET_CARD_IDS_JS))
                    if current_ids - seen_ids:
                        seen_ids |= current_ids
                        stable_rounds = 0
                    else:
                        stable_rounds += 1
                        if stable_rounds >= SCROLL_STABLE_ROUNDS:
                            break

            # текст отзыва бывает обрезан кнопкой "Ещё" с реальным укорочением
            # DOM-текста (не просто CSS line-clamp) — раскрываем все разом одним
            # JS-вызовом (быстрее, чем кликать и ждать по каждой карточке отдельно)
            page.evaluate(_EXPAND_ALL_JS)
            page.wait_for_timeout(500)

            cards = page.query_selector_all("div.jftiEf[data-review-id]")
            for card in cards[:limit]:
                text_el = card.query_selector("span.wiI7pd")
                text = re.sub(r"\s+", " ", text_el.inner_text()).strip() if text_el else ""
                if not text:
                    continue  # отзыв без текста (только оценка) — не нужен для саммари

                author_el = card.query_selector(".d4r55.fontTitleMedium")
                author = author_el.inner_text().strip() if author_el else "Аноним"

                rating = 0.0
                rating_el = card.query_selector('span.kvMYJc[role="img"]')
                if rating_el:
                    label = rating_el.get_attribute("aria-label") or ""
                    m = re.search(r"(\d+(?:[.,]\d+)?)", label)
                    if m:
                        rating = float(m.group(1).replace(",", "."))

                date_el = card.query_selector("span.rsqaWe")
                review_date = _parse_relative_date(date_el.inner_text()) if date_el else datetime.date.today().isoformat()

                external_id = card.get_attribute("data-review-id") or _make_external_id(author, review_date, text)

                reviews.append(
                    Review(
                        platform="google_maps",
                        external_id=external_id,
                        author=author,
                        rating=rating,
                        text=text,
                        review_date=review_date,
                    )
                )
        finally:
            browser.close()
    return reviews


def identify(url: str, timeout: float = 30.0) -> str | None:
    """Название организации — для отображения в списке площадок клиента.
    Дёргается один раз при добавлении ссылки, не при каждом скрапе.

    В отличие от fetch_reviews, здесь достаточно прямой навигации на ссылку —
    заголовок страницы доступен даже в "урезанном" виде UI (см. докстринг
    _open_reviews_tab), поиск+клик не нужен."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser, page = _new_page(p)
            try:
                page.goto(url, timeout=int(timeout * 1000), wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                title = page.title()
            finally:
                browser.close()
        return TITLE_SUFFIX_RE.sub("", title).strip() or None
    except Exception:
        return None
