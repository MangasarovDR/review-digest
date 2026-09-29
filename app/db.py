import os
import secrets
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timezone

DATABASE_PATH = os.environ.get("DATABASE_PATH", "/data/review_digest.db")

# Все площадки, которые встречаются в базе, — нужны ТОЛЬКО чтобы подписать
# уже сохранённые отзывы человеческим именем. Наличие ключа здесь не значит,
# что площадку опрашивают.
PLATFORMS = {
    "yandex_maps": "Яндекс.Карты",
    "2gis": "2ГИС",
    "google_maps": "Google Карты",
    "wildberries": "Wildberries",
    "vseinstrumenti": "ВсеИнструменты.ру (отключена)",
    "ozon": "Ozon (не опрашивается)",
}

# Площадки, которые МОЖНО добавить и которые опрашиваются. Раньше этого
# разделения не было, и один и тот же список отвечал сразу на два вопроса:
# «как называется площадка» и «можно ли её опрашивать». Из-за этого убрать
# площадку из опроса, не потеряв подписи у сохранённых отзывов, было нельзя.
#
# vseinstrumenti отключена 19.08.2026 по решению Давида: сайт закрыт антиботом
# Servicepipe, обход требует постоянной возни с прокси и живым браузером и
# ломается снова после каждого их обновления. Для бесплатной площадки в
# наборе это несоразмерная цена. Код сборщика оставлен в fetchers/ как есть —
# он не удалён, а отсоединён, и в его шапке написано, почему.
#
# ozon не опрашивался и раньше: блокирует по IP, нужен российский прокси.
PLATFORMS_AVAILABLE = {
    k: v for k, v in PLATFORMS.items()
    if k not in ("vseinstrumenti", "wildberries", "ozon")
}

# legacy: до введения client_sources ссылки хранились по одной на площадку
# прямо в таблице clients — используется только миграцией при апгрейде
_LEGACY_URL_COLUMNS = [
    ("yandex_maps_url", "yandex_maps"),
    ("dvgis_url", "2gis"),
    ("wb_url", "wildberries"),
    ("ozon_url", "ozon"),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    scraping_enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS client_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    platform TEXT NOT NULL,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    label TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    source_id INTEGER REFERENCES client_sources(id),
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    author TEXT,
    rating REAL,
    text TEXT,
    review_date TEXT,
    fetched_at TEXT NOT NULL,
    UNIQUE(client_id, platform, external_id)
);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    generated_at TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    summary_json TEXT NOT NULL
);

-- "предохранитель" на площадку: если она массово отдаёт ошибки, сама
-- приостанавливается на cooldown вместо того, чтобы долбить её дальше по
-- всем оставшимся клиентам одного прогона (см. record_platform_failure)
CREATE TABLE IF NOT EXISTS platform_status (
    platform TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    paused_until TEXT
);

-- счётчик трафика через платный прокси (pool.proxy.market, ограниченный
-- пакет ГБ) — единственная строка (id=1), чтобы вовремя предупредить о
-- приближении к лимиту (см. record_proxy_usage)
CREATE TABLE IF NOT EXISTS proxy_usage (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    bytes_used INTEGER NOT NULL DEFAULT 0,
    alert_sent INTEGER NOT NULL DEFAULT 0
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def get_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_legacy_urls(conn: sqlite3.Connection) -> None:
    """Разовая идемпотентная миграция: старые url-колонки в clients -> client_sources."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
    if "yandex_maps_url" not in cols:
        return  # уже смигрировано (или свежая база без legacy-колонок)

    for col, platform in _LEGACY_URL_COLUMNS:
        for row in conn.execute(f"SELECT id, {col} AS url FROM clients WHERE {col} IS NOT NULL AND {col} != ''"):
            exists = conn.execute(
                "SELECT 1 FROM client_sources WHERE client_id=? AND platform=? AND url=?",
                (row["id"], platform, row["url"]),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO client_sources (client_id, platform, url, created_at) VALUES (?,?,?,?)",
                    (row["id"], platform, row["url"], _now()),
                )

    try:
        for col, _ in _LEGACY_URL_COLUMNS:
            conn.execute(f"ALTER TABLE clients DROP COLUMN {col}")
    except sqlite3.OperationalError:
        pass  # старый SQLite без поддержки DROP COLUMN — колонки останутся, но не используются


def _migrate_source_is_active(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(client_sources)")}
    if "is_active" not in cols:
        conn.execute("ALTER TABLE client_sources ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")


def _migrate_client_scraping_enabled(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
    if "scraping_enabled" not in cols:
        conn.execute("ALTER TABLE clients ADD COLUMN scraping_enabled INTEGER NOT NULL DEFAULT 1")


def _migrate_source_label(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(client_sources)")}
    if "label" not in cols:
        conn.execute("ALTER TABLE client_sources ADD COLUMN label TEXT")


def _migrate_fetch_status(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
    if "fetching_since" not in cols:
        conn.execute("ALTER TABLE clients ADD COLUMN fetching_since TEXT")


def _migrate_card_token(conn: sqlite3.Connection) -> None:
    """Токен публичной карточки (/card/{token}) — намеренно отдельный секрет
    от slug приватного дашборда ({slug} даёт доступ к /d/{slug} и управлению
    площадками в /d/{slug}/sources). Карточка создана специально для того,
    чтобы её шэрили и встраивали на сайты — если бы она жила на /d/{slug}/card,
    любой получатель ссылки на карточку получал бы заодно и slug, а с ним
    доступ к приватному дашборду и возможность менять/удалять площадки
    клиента. Отдельный токен не даёт вывести slug из card_token и наоборот."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
    if "card_token" not in cols:
        conn.execute("ALTER TABLE clients ADD COLUMN card_token TEXT")
    rows = conn.execute("SELECT id FROM clients WHERE card_token IS NULL").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE clients SET card_token=? WHERE id=?",
            (secrets.token_urlsafe(16), row["id"]),
        )


def _migrate_review_source_id(conn: sqlite3.Connection) -> None:
    """Отзывы, сохранённые до введения client_sources-привязки, останутся с
    source_id=NULL — на дашборде для них просто не покажется метка кабинета,
    без ложных догадок, если у клиента несколько источников одной площадки."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(reviews)")}
    if "source_id" not in cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN source_id INTEGER REFERENCES client_sources(id)")


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(_SCHEMA)
        _migrate_legacy_urls(conn)
        _migrate_source_is_active(conn)
        _migrate_client_scraping_enabled(conn)
        _migrate_source_label(conn)
        _migrate_review_source_id(conn)
        _migrate_fetch_status(conn)
        _migrate_card_token(conn)


def add_client(name: str) -> str:
    slug = secrets.token_urlsafe(12)
    card_token = secrets.token_urlsafe(16)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO clients (slug, name, created_at, card_token) VALUES (?, ?, ?, ?)",
            (slug, name, _now(), card_token),
        )
    return slug


def list_clients(name_filter: str | None = None, scraping_enabled_only: bool = False) -> list[sqlite3.Row]:
    with get_db() as conn:
        query = "SELECT * FROM clients WHERE is_active = 1"
        params: list = []
        if scraping_enabled_only:
            query += " AND scraping_enabled = 1"
        if name_filter:
            query += " AND name LIKE ?"
            params.append(f"%{name_filter}%")
        query += " ORDER BY created_at DESC"
        return conn.execute(query, params).fetchall()


def list_clients_with_stats() -> list[dict]:
    """Клиенты + площадки + количество отзывов и дата последнего саммари — для админки."""
    with get_db() as conn:
        clients = [dict(r) for r in conn.execute(
            "SELECT * FROM clients WHERE is_active = 1 ORDER BY created_at DESC"
        ).fetchall()]
        for c in clients:
            c["platforms"] = sorted({
                row["platform"] for row in
                conn.execute("SELECT DISTINCT platform FROM client_sources WHERE client_id=?", (c["id"],))
            })
            c["review_count"] = conn.execute(
                "SELECT COUNT(*) FROM reviews WHERE client_id=?", (c["id"],)
            ).fetchone()[0]
            c["last_digest_at"] = conn.execute(
                "SELECT MAX(generated_at) FROM digests WHERE client_id=?", (c["id"],)
            ).fetchone()[0]
        return clients


def get_client_by_slug(slug: str) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM clients WHERE slug = ? AND is_active = 1", (slug,)
        ).fetchone()


def get_client_by_card_token(token: str) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM clients WHERE card_token = ? AND is_active = 1", (token,)
        ).fetchone()


def toggle_client_scraping(client_id: int) -> None:
    """Пауза целиком клиента — cron его пропускает, но дашборд/ссылки остаются доступны."""
    with get_db() as conn:
        conn.execute(
            "UPDATE clients SET scraping_enabled = 1 - scraping_enabled WHERE id=?",
            (client_id,),
        )


def mark_fetch_started(client_id: int) -> None:
    with get_db() as conn:
        conn.execute("UPDATE clients SET fetching_since=? WHERE id=?", (_now(), client_id))


def mark_fetch_finished(client_id: int) -> None:
    with get_db() as conn:
        conn.execute("UPDATE clients SET fetching_since=NULL WHERE id=?", (client_id,))


def is_fetch_in_progress(client_id: int, stale_after_minutes: int = 60) -> bool:
    """Считаем сбор завершённым, если флаг не сброшен дольше разумного —
    защита от «зависшего» баннера, если процесс упал и не дошёл до finally."""
    with get_db() as conn:
        row = conn.execute("SELECT fetching_since FROM clients WHERE id=?", (client_id,)).fetchone()
    if not row or not row["fetching_since"]:
        return False
    try:
        started = datetime.fromisoformat(row["fetching_since"])
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - started.replace(tzinfo=started.tzinfo or timezone.utc)).total_seconds() < stale_after_minutes * 60


def get_client_sources(client_id: int, active_only: bool = False) -> list[sqlite3.Row]:
    with get_db() as conn:
        query = "SELECT * FROM client_sources WHERE client_id=?"
        if active_only:
            query += " AND is_active=1"
        query += " ORDER BY platform, created_at"
        return conn.execute(query, (client_id,)).fetchall()


def add_source(client_id: int, platform: str, url: str, label: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO client_sources (client_id, platform, url, created_at, label) VALUES (?,?,?,?,?)",
            (client_id, platform, url, _now(), label),
        )


def delete_source(source_id: int, client_id: int) -> None:
    """client_id обязателен — чтобы клиент/админ не мог удалить чужой источник по угаданному id."""
    with get_db() as conn:
        conn.execute("DELETE FROM client_sources WHERE id=? AND client_id=?", (source_id, client_id))


def toggle_source_active(source_id: int, client_id: int) -> None:
    """client_id обязателен — та же защита от чужого id, что и в delete_source."""
    with get_db() as conn:
        conn.execute(
            "UPDATE client_sources SET is_active = 1 - is_active WHERE id=? AND client_id=?",
            (source_id, client_id),
        )


def save_reviews(client_id: int, source_id: int | None, reviews: list) -> int:
    """Insert reviews, skipping duplicates. Returns count of newly inserted reviews."""
    inserted = 0
    with get_db() as conn:
        for r in reviews:
            cur = conn.execute(
                "INSERT OR IGNORE INTO reviews "
                "(client_id, source_id, platform, external_id, author, rating, text, review_date, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (client_id, source_id, r.platform, r.external_id, r.author, r.rating, r.text, r.review_date, _now()),
            )
            if cur.rowcount:
                inserted += 1
    return inserted


def get_recent_reviews_page(client_id: int, offset: int = 0, limit: int = 30) -> list[sqlite3.Row]:
    """Как get_recent_reviews, но со сдвигом — для догрузки следующей порции
    (кнопка "Показать ещё" на дашборде, без перезагрузки страницы)."""
    with get_db() as conn:
        return conn.execute(
            "SELECT reviews.*, client_sources.label AS source_label, client_sources.url AS source_url "
            "FROM reviews LEFT JOIN client_sources ON reviews.source_id = client_sources.id "
            "WHERE reviews.client_id = ? ORDER BY reviews.review_date DESC LIMIT ? OFFSET ?",
            (client_id, limit, offset),
        ).fetchall()


def get_recent_reviews(client_id: int, limit: int = 100) -> list[sqlite3.Row]:
    """source_label/source_url — краткая идентификация кабинета/точки, откуда
    отзыв (для показа на дашборде рядом с отзывом). NULL у отзывов, сохранённых
    до появления source_id, и у источников без успешного identify()."""
    with get_db() as conn:
        return conn.execute(
            "SELECT reviews.*, client_sources.label AS source_label, client_sources.url AS source_url "
            "FROM reviews LEFT JOIN client_sources ON reviews.source_id = client_sources.id "
            "WHERE reviews.client_id = ? ORDER BY reviews.review_date DESC LIMIT ?",
            (client_id, limit),
        ).fetchall()


def count_reviews(client_id: int) -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM reviews WHERE client_id=?", (client_id,)).fetchone()
        return row["n"] if row else 0


def get_previous_digest(client_id: int) -> sqlite3.Row | None:
    """Предпоследнее саммари — для сравнения тренда (вырос/упал индекс) на
    публичной карточке (/d/{slug}/card). Не путать с get_latest_digest."""
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM digests WHERE client_id = ? ORDER BY generated_at DESC LIMIT 1 OFFSET 1",
            (client_id,),
        ).fetchone()


def get_monthly_rating_breakdown(client_id: int, months: int = 36) -> dict:
    """Помесячная разбивка отзывов по рейтингу (1..5 звёзд) — для мини-графика
    динамики на дашборде. У каждого месяца: pct (доля 1..5 звёзд, в процентах
    от отзывов месяца) и bar_height_pct (высота столбца относительно самого
    "плотного" по числу отзывов месяца — иначе месяц с 1 отзывом и месяц с
    100 отзывами выглядели бы одинаково). Отзывы без рейтинга (0/None) не учитываются.

    Дополнительно возвращает `platforms` (площадки, встретившиеся у клиента) и
    `by_month_platform` (сырые счётчики по звёздам на каждую площадку в каждом
    месяце) — сервер не решает, что показывать, а отдаёт данные для фильтра по
    каналам на дашборде: JS пересчитывает total/pct/bar_height_pct на лету по
    выбранному подмножеству площадок, без повторных запросов к серверу."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT review_date, rating, platform FROM reviews "
            "WHERE client_id=? AND review_date IS NOT NULL AND review_date != '' AND rating > 0",
            (client_id,),
        ).fetchall()

    counts: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    platform_counts: dict[str, dict[str, dict[int, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    platforms_seen: set[str] = set()
    for r in rows:
        month = r["review_date"][:7]  # "YYYY-MM"
        if len(month) != 7:
            continue
        star = min(5, max(1, round(r["rating"])))
        counts[month][star] += 1
        platform_counts[month][r["platform"]][star] += 1
        platforms_seen.add(r["platform"])

    today = date.today().replace(day=1)
    month_keys = []
    y, m = today.year, today.month
    for _ in range(months):
        month_keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    month_keys.reverse()

    entries = []
    by_month_platform = {}
    for month in month_keys:
        star_counts = counts.get(month, {})
        total = sum(star_counts.values())
        pct = {s: (star_counts.get(s, 0) / total * 100 if total else 0) for s in range(1, 6)}
        entries.append({"month": month, "total": total, "pct": pct})
        by_month_platform[month] = {
            platform: {star: platform_counts[month][platform].get(star, 0) for star in range(1, 6)}
            for platform in platforms_seen
        }

    max_total = max((e["total"] for e in entries), default=0)
    for e in entries:
        e["bar_height_pct"] = (e["total"] / max_total * 100) if max_total else 0

    return {
        "months": entries,
        "max_total": max_total,
        "platforms": sorted(platforms_seen),
        "by_month_platform": by_month_platform,
    }


def save_digest(client_id: int, summary_json: str, period_start: str, period_end: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO digests (client_id, generated_at, period_start, period_end, summary_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (client_id, _now(), period_start, period_end, summary_json),
        )


def get_latest_digest(client_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM digests WHERE client_id = ? ORDER BY generated_at DESC LIMIT 1",
            (client_id,),
        ).fetchone()


def is_platform_paused(platform: str) -> str | None:
    """Возвращает paused_until (ISO-строку), если площадка сейчас на паузе
    предохранителя, иначе None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT paused_until FROM platform_status WHERE platform=?", (platform,)
        ).fetchone()
        if row and row["paused_until"] and row["paused_until"] > _now():
            return row["paused_until"]
        return None


def record_platform_success(platform: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO platform_status (platform, consecutive_failures, paused_until) VALUES (?, 0, NULL) "
            "ON CONFLICT(platform) DO UPDATE SET consecutive_failures=0, paused_until=NULL",
            (platform,),
        )


def record_platform_failure(platform: str, threshold: int = 3, cooldown_minutes: int = 120) -> str | None:
    """Увеличивает счётчик подряд идущих ошибок площадки; при достижении
    threshold — ставит паузу на cooldown_minutes и возвращает paused_until
    (сигнал вызывающему коду отправить отдельное алерт-уведомление).
    Если порог ещё не достигнут — возвращает None."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO platform_status (platform, consecutive_failures, paused_until) VALUES (?, 1, NULL) "
            "ON CONFLICT(platform) DO UPDATE SET consecutive_failures = consecutive_failures + 1",
            (platform,),
        )
        failures = conn.execute(
            "SELECT consecutive_failures FROM platform_status WHERE platform=?", (platform,)
        ).fetchone()["consecutive_failures"]
        if failures >= threshold:
            paused_until = (datetime.now(timezone.utc).timestamp() + cooldown_minutes * 60)
            paused_until_iso = datetime.fromtimestamp(paused_until, tz=timezone.utc).isoformat()
            conn.execute(
                "UPDATE platform_status SET paused_until=? WHERE platform=?",
                (paused_until_iso, platform),
            )
            return paused_until_iso
        return None


def add_proxy_bytes(n: int) -> int:
    """Прибавляет n байт к счётчику трафика через платный прокси, возвращает новый итог."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO proxy_usage (id, bytes_used, alert_sent) VALUES (1, ?, 0) "
            "ON CONFLICT(id) DO UPDATE SET bytes_used = bytes_used + ?",
            (n, n),
        )
        return conn.execute("SELECT bytes_used FROM proxy_usage WHERE id=1").fetchone()["bytes_used"]


def is_proxy_alert_sent() -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT alert_sent FROM proxy_usage WHERE id=1").fetchone()
        return bool(row and row["alert_sent"])


def mark_proxy_alert_sent() -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO proxy_usage (id, bytes_used, alert_sent) VALUES (1, 0, 1) "
            "ON CONFLICT(id) DO UPDATE SET alert_sent = 1"
        )
