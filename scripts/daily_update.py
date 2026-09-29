"""Ежедневный cron-джоб: по каждому активному клиенту — скрап новых отзывов,
и только если появилось что-то новое — пересборка LLM-саммари (экономия бюджета).

Usage (внутри контейнера):
    python scripts/daily_update.py

update_client() переиспользуется веб-роутом «Обновить сейчас» в админке
(app/main.py) — та же логика и та же защита от лишних токенов на один клиент.
"""
import datetime
import html
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

from app.db import (
    get_client_sources,
    get_recent_reviews,
    is_platform_paused,
    list_clients,
    mark_fetch_finished,
    mark_fetch_started,
    record_platform_failure,
    record_platform_success,
    save_digest,
    save_reviews,
)
from app.notify import send_telegram_alert
from fetchers import dvgis, google_maps, yandex_maps

# Что опрашивается. Отсутствие площадки здесь — это и есть отключение: цикл
# обхода просто не находит для неё сборщика и пропускает источник.
#
# ozon — не подключён, блокирует по IP, нужен российский прокси.
# vseinstrumenti — отключён 19.08.2026 по решению Давида: антибот Servicepipe
#   требует постоянной возни с прокси и живым браузером и ломается после
#   каждого их обновления, а площадка в наборе бесплатная. Цена не сходится.
# wildberries — отключён 25.08.2026 по решению Давида: сбор держится на
#   резидентском прокси, подписка pool.proxy.market протухла 19.08 (407), без
#   прокси WB блокирует по IP. Подробности — в шапке fetchers/wildberries.py.
FETCHER_BY_PLATFORM = {
    "yandex_maps": yandex_maps,
    "2gis": dvgis,
    "google_maps": google_maps,
}

# пауза между запросами к площадкам — простая защита от залпа запросов при
# росте числа клиентов (см. update_client). Пока грубая и одинаковая для всех
# площадок; при заметном росте базы клиентов стоит сделать её настраиваемой
# по SCRAPE_DELAY_SECONDS env var, чтобы не пересобирать образ ради тюнинга.
SCRAPE_DELAY_SECONDS = float(os.environ.get("SCRAPE_DELAY_SECONDS", "2.0"))

# предохранитель: после стольки подряд ошибок площадка ставится на паузу для
# всех клиентов на cooldown — вместо того чтобы продолжать её долбить (см.
# record_platform_failure в app/db.py). Именно это отсутствие предохранителя
# усугубило блокировку WB в первый раз — скрипт продолжал бить по уже
# заблокированной площадке клиент за клиентом.
PLATFORM_FAILURE_THRESHOLD = int(os.environ.get("PLATFORM_FAILURE_THRESHOLD", "3"))
PLATFORM_COOLDOWN_MINUTES = int(os.environ.get("PLATFORM_COOLDOWN_MINUTES", "120"))

PLATFORM_LABELS = {
    "yandex_maps": "Яндекс.Карты",
    "2gis": "2ГИС",
    "google_maps": "Google Карты",
    "wildberries": "Wildberries (отключена)",
    "vseinstrumenti": "ВсеИнструменты.ру (отключена)",
    "ozon": "Ozon",
}

MODEL = "claude-sonnet-5"

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "top_complaints": {"type": "array", "items": {"type": "string"}},
        "top_praises": {"type": "array", "items": {"type": "string"}},
        "average_rating": {"type": "number"},
        "trend_summary": {"type": "string"},
        "notable_quotes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["top_complaints", "top_praises", "average_rating", "trend_summary", "notable_quotes"],
    "additionalProperties": False,
}


ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL")


def _extract_json_object(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели.

    Прокси-провайдер (ANTHROPIC_BASE_URL) молча игнорирует output_config со
    структурированным выводом и отдаёт обычную прозу: фразу, а следом JSON в
    ```-блоке. Поэтому json.loads(text) применять нельзя — он падал с
    «Expecting value: line 1 column 1 (char 0)» на первом же слове ответа.
    """
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"в ответе модели нет JSON-объекта, начало ответа: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def _validate_summary(summary) -> dict:
    """Проверяет саммари по SUMMARY_SCHEMA вручную.

    Схему проверяет провайдер только при работающем структурированном выводе;
    у прокси её не проверяет никто, а битое саммари уедет в save_digest и
    сломает шаблон дашборда уже у клиента на глазах.
    """
    if not isinstance(summary, dict):
        raise ValueError(f"ожидался JSON-объект, пришёл {type(summary).__name__}")
    missing = [k for k in SUMMARY_SCHEMA["required"] if k not in summary]
    if missing:
        raise ValueError(f"в JSON нет обязательных полей: {', '.join(missing)}")
    for key in ("top_complaints", "top_praises", "notable_quotes"):
        if not isinstance(summary[key], list) or not all(isinstance(x, str) for x in summary[key]):
            raise ValueError(f"поле {key} должно быть списком строк")
    if not isinstance(summary["trend_summary"], str):
        raise ValueError("поле trend_summary должно быть строкой")
    if isinstance(summary["average_rating"], bool) or not isinstance(summary["average_rating"], (int, float)):
        raise ValueError("поле average_rating должно быть числом")
    return summary


def build_summary(client_name: str, reviews: list, previous_error: str = "") -> dict:
    client = anthropic.Anthropic(base_url=ANTHROPIC_BASE_URL) if ANTHROPIC_BASE_URL else anthropic.Anthropic()
    reviews_text = "\n\n".join(
        f"[{r['platform']}] рейтинг {r['rating']}/5, {r['review_date']}: {r['text']}"
        for r in reviews
    )
    # формат описываем словами, а не только через output_config: прокси его
    # игнорирует, и без явного требования модель отвечает прозой с ```-блоком
    retry_hint = (
        f"\n\nПредыдущая попытка не разобралась: {previous_error}. Верни строго корректный JSON.\n"
        if previous_error else ""
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        output_config={"format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
        messages=[{
            "role": "user",
            "content": (
                f"Ниже отзывы о бизнесе «{client_name}» с разных площадок. "
                "Составь саммари: главные жалобы (top_complaints), главные похвалы (top_praises), "
                "средний рейтинг (average_rating), краткий тренд/динамика (trend_summary), "
                "и 2-3 показательные цитаты из отзывов (notable_quotes) — БЕЗ кавычек вокруг цитаты, "
                "просто текст, кавычки добавит шаблон отображения. Пиши по-русски, кратко и по делу.\n\n"
                "В ответе — ТОЛЬКО JSON-объект по схеме ниже: без пояснений до и после, "
                "без markdown-обёртки, без ```.\n"
                f"Схема: {json.dumps(SUMMARY_SCHEMA, ensure_ascii=False)}"
                f"{retry_hint}\n\n"
                f"{reviews_text}"
            ),
        }],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    summary = _validate_summary(_extract_json_object(text))
    summary["notable_quotes"] = [q.strip().strip('"«»“”') for q in summary["notable_quotes"]]
    return summary


def _notify_errors(client_name: str, new_count: int, summary_updated: bool, errors: list) -> None:
    # имя клиента — жирным и первой строкой, чтобы сразу было видно, о ком речь,
    # даже пролистывая ленту уведомлений по диагонали
    lines = [
        f"🔴 <b>{html.escape(client_name)}</b>",
        "review-digest: проблема при обновлении отзывов",
        "",
        f"Время: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} UTC",
        f"Новых отзывов найдено: {new_count}",
        f"Саммари обновлено: {'да' if summary_updated else 'нет'}",
        "",
        "Что пошло не так:",
    ] + [f"• {html.escape(e)}" for e in errors]
    send_telegram_alert("\n".join(lines))


def _notify_platform_paused(platform: str, paused_until: str) -> None:
    label = PLATFORM_LABELS.get(platform, platform)
    lines = [
        f"⚠️ <b>Площадка {html.escape(label)} приостановлена</b>",
        "review-digest: предохранитель сработал",
        "",
        f"Подряд ошибок: {PLATFORM_FAILURE_THRESHOLD}+",
        f"Опрос {html.escape(label)} остановлен для всех клиентов до {paused_until[:16].replace('T', ' ')} UTC",
        "",
        "Площадка, похоже, банит/лимитит запросы. Дальше опрос возобновится "
        "автоматически после паузы; если ошибки продолжатся — предохранитель "
        "сработает снова.",
    ]
    send_telegram_alert("\n".join(lines))


def update_client(client) -> dict:
    """Скрап + (при новых отзывах) пересборка саммари для одного клиента.

    Возвращает {"new_count": int, "summary_updated": bool, "errors": [str, ...]}.
    Скрапинг — обычные HTTP-запросы, токены не тратит. Токены уходят только
    на build_summary(), а он вызывается только если new_count > 0.

    При любой ошибке (скрапинг конкретного источника или саммари) шлёт
    подробное уведомление в Telegram — чтобы узнать сразу, а не от клиента.
    """
    client_id = client["id"]
    name = client["name"]
    today = datetime.date.today().isoformat()

    mark_fetch_started(client_id)
    try:
        return _do_update_client(client_id, name, today)
    finally:
        # снимаем флаг даже при исключении — иначе баннер "идёт сбор" на
        # дашборде клиента зависнет до истечения stale_after_minutes в
        # is_fetch_in_progress()
        mark_fetch_finished(client_id)


def _do_update_client(client_id: int, name: str, today: str) -> dict:
    new_count = 0
    errors = []
    for source in get_client_sources(client_id, active_only=True):
        platform = source["platform"]
        fetcher = FETCHER_BY_PLATFORM.get(platform)
        if fetcher is None:
            continue  # напр. ozon — источник сохранён, но фетчер ещё не подключён

        paused_until = is_platform_paused(platform)
        if paused_until:
            # предохранитель уже сработал в этом (или недавнем) прогоне —
            # не долбим площадку дальше, не шлём алерт повторно на каждого клиента
            print(f"[{name}] {platform} на паузе предохранителя до {paused_until}, пропускаю")
            continue

        try:
            reviews = fetcher.fetch_reviews(source["url"])
            new_count += save_reviews(client_id, source["id"], reviews)
            record_platform_success(platform)
        except Exception as e:
            errors.append(f"{source['platform']} ({source['url']}): {e}")
            print(f"[{name}] ошибка скрапинга {platform} ({source['url']}): {e}")
            just_paused = record_platform_failure(
                platform, threshold=PLATFORM_FAILURE_THRESHOLD, cooldown_minutes=PLATFORM_COOLDOWN_MINUTES
            )
            if just_paused:
                _notify_platform_paused(platform, just_paused)
        # пауза между запросами — при росте числа клиентов защищает от залпа
        # запросов на одну площадку за пару минут (похоже, именно это словил WB)
        time.sleep(SCRAPE_DELAY_SECONDS)

    summary_updated = False
    if new_count == 0:
        print(f"[{name}] новых отзывов нет — саммари не пересобирается")
    else:
        print(f"[{name}] новых отзывов: {new_count} — пересобираю саммари")
        recent = get_recent_reviews(client_id, limit=100)
        # прокси-провайдер иногда отдаёт временную ошибку — отзывы уже помечены
        # увиденными, без ретрая шанс на саммари был бы потерян до следующих новых отзывов
        last_error = None
        for attempt in range(3):
            try:
                # текст прошлой ошибки уходит в промпт: три одинаковых запроса
                # на детерминированной ошибке разбора — просто трата токенов
                summary = build_summary(name, recent, previous_error=str(last_error or ""))
                save_digest(client_id, json.dumps(summary, ensure_ascii=False), period_start="", period_end=today)
                print(f"[{name}] саммари обновлено")
                summary_updated = True
                break
            except Exception as e:
                last_error = e
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
        else:
            print(f"[{name}] ошибка саммари после 3 попыток: {last_error}")
            errors.append(f"саммари: {last_error}")

    if errors:
        _notify_errors(name, new_count, summary_updated, errors)

    return {"new_count": new_count, "summary_updated": summary_updated, "errors": errors}


def run() -> None:
    from app.db import init_db
    init_db()
    for client in list_clients(scraping_enabled_only=True):
        update_client(client)


if __name__ == "__main__":
    run()
