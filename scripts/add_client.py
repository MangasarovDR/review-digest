"""CLI: onboard a new client, print their dashboard link.

Usage:
    python scripts/add_client.py "Название бизнеса" --yandex https://yandex.ru/maps/org/.../123456/
    # можно указать площадку несколько раз, если у клиента больше одного аккаунта:
    python scripts/add_client.py "Сеть кофеен" --yandex URL1 --yandex URL2 --wb URL3
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import add_client, add_source, get_client_by_slug, init_db

BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "https://reviews.automatiko.ru")

FLAG_TO_PLATFORM = {
    "yandex": "yandex_maps",
    "dvgis": "2gis",
    "wb": "wildberries",
    "ozon": "ozon",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--yandex", action="append", default=[])
    parser.add_argument("--2gis", dest="dvgis", action="append", default=[])
    parser.add_argument("--wb", action="append", default=[])
    parser.add_argument("--ozon", action="append", default=[])
    args = parser.parse_args()

    sources = [(FLAG_TO_PLATFORM[flag], url) for flag in FLAG_TO_PLATFORM for url in getattr(args, flag)]
    if not sources:
        parser.error("укажите хотя бы одну площадку (--yandex / --2gis / --wb / --ozon), можно несколько раз")

    init_db()
    slug = add_client(name=args.name)
    client = get_client_by_slug(slug)
    for platform, url in sources:
        add_source(client["id"], platform, url)

    print(f"Клиент «{args.name}» добавлен, площадок: {len(sources)}.")
    print(f"Ссылка на дашборд: {BASE_URL}/d/{slug}")


if __name__ == "__main__":
    main()
