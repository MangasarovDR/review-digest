"""CLI: list clients and their dashboard links (напомнить забытую ссылку).

Usage:
    python scripts/list_clients.py [фильтр-по-имени]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import PLATFORMS, get_client_sources, init_db, list_clients

BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "https://reviews.automatiko.ru")


def main() -> None:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else None
    init_db()
    clients = list_clients(name_filter)
    if not clients:
        print("Клиентов не найдено.")
        return
    for c in clients:
        sources = get_client_sources(c["id"])
        platforms = ", ".join(PLATFORMS.get(s["platform"], s["platform"]) for s in sources) or "нет площадок"
        print(f"{c['name']}  |  {BASE_URL}/d/{c['slug']}  |  {platforms}  |  создан {c['created_at'][:10]}")


if __name__ == "__main__":
    main()
