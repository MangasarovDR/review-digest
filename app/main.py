import json
import os
import secrets as secrets_module
import urllib.parse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import (
    PLATFORMS,
    PLATFORMS_AVAILABLE,
    add_client,
    add_source,
    count_reviews,
    delete_source,
    get_client_by_card_token,
    get_client_by_slug,
    get_client_sources,
    get_latest_digest,
    get_monthly_rating_breakdown,
    get_previous_digest,
    get_recent_reviews,
    get_recent_reviews_page,
    init_db,
    is_fetch_in_progress,
    list_clients_with_stats,
    toggle_client_scraping,
    toggle_source_active,
)
from fetchers import dvgis, google_maps, yandex_maps
from scripts.daily_update import update_client

_IDENTIFY_BY_PLATFORM = {
    "yandex_maps": yandex_maps,
    "2gis": dvgis,
    "google_maps": google_maps,
}


def _identify(platform: str, url: str) -> str | None:
    """Название/адрес источника — дёргается один раз при добавлении ссылки.
    Не должно ронять сохранение источника, если площадка недоступна/поменялась —
    label просто останется пустым, ссылка всё равно добавится."""
    fetcher = _IDENTIFY_BY_PLATFORM.get(platform)
    if fetcher is None or not hasattr(fetcher, "identify"):
        return None
    try:
        return fetcher.identify(url)
    except Exception:
        return None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "https://reviews.automatiko.ru")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    valid = bool(ADMIN_TOKEN) and secrets_module.compare_digest(credentials.password, ADMIN_TOKEN)
    if not valid:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


def _get_client_or_404(slug: str):
    client = get_client_by_slug(slug)
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    return client


def _render_sources_page(request: Request, client, mode: str):
    """mode: 'admin' (правит агентство через /admin/{slug}) или 'client' (сам клиент через /d/{slug}/sources)."""
    base_path = f"/admin/{client['slug']}" if mode == "admin" else f"/d/{client['slug']}"
    return templates.TemplateResponse(
        request,
        "manage_sources.html",
        {
            "client": client,
            "sources": get_client_sources(client["id"]),
            # Подписи берём из полного списка (иначе уже сохранённый
            # источник отключённой площадки покажется сырым ключом),
            # а выбор в форме — только из доступных.
            "platforms": PLATFORMS,
            "platforms_available": PLATFORMS_AVAILABLE,
            "mode": mode,
            "base_path": base_path,
            "back_url": "/admin" if mode == "admin" else f"/d/{client['slug']}",
            "msg": request.query_params.get("msg"),
        },
    )


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})


REVIEWS_PAGE_SIZE = 30


@app.get("/d/{slug}", response_class=HTMLResponse)
def dashboard(slug: str, request: Request):
    client = _get_client_or_404(slug)

    digest_row = get_latest_digest(client["id"])
    digest = json.loads(digest_row["summary_json"]) if digest_row else None
    digest_date = digest_row["generated_at"][:10] if digest_row else None
    reviews = get_recent_reviews_page(client["id"], offset=0, limit=REVIEWS_PAGE_SIZE)
    monthly_ratings = get_monthly_rating_breakdown(client["id"])
    monthly_ratings_json = json.dumps(
        {
            "months": [e["month"] for e in monthly_ratings["months"]],
            "by_month_platform": monthly_ratings["by_month_platform"],
        }
    ).replace("</", "<\\/")

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "client": client,
            "digest": digest,
            "digest_date": digest_date,
            "reviews": reviews,
            "has_more": len(reviews) == REVIEWS_PAGE_SIZE,
            "monthly_ratings": monthly_ratings,
            "monthly_ratings_json": monthly_ratings_json,
            "platform_labels": PLATFORMS,
        },
    )


@app.get("/d/{slug}/status")
def dashboard_status(slug: str):
    """Лёгкий эндпоинт для баннера «идёт сбор данных» на дашборде — опрашивается
    JS-ом раз в несколько секунд, пока клиент смотрит на страницу сразу после
    подключения площадок."""
    client = _get_client_or_404(slug)
    return JSONResponse({"fetching": is_fetch_in_progress(client["id"])})


@app.get("/card/{token}", response_class=HTMLResponse)
def trust_card(token: str, request: Request):
    """Публичная шэрибл-карточка «Индекс доверия» — то, чем клиент делится с
    аудиторией или встраивает на свой сайт.

    Намеренно на отдельном пространстве /card/{token}, НЕ на /d/{slug}/card:
    карточка создана специально для расшаривания и встраивания на сторонние
    сайты, а /d/{slug} — приватный дашборд, из которого /d/{slug}/sources
    позволяет добавлять/удалять площадки клиента. Если бы карточка жила под
    тем же slug, любой получатель публичной ссылки на карточку автоматически
    получал бы и slug приватного дашборда — token здесь отдельный секрет,
    из которого slug вывести нельзя."""
    client = get_client_by_card_token(token)
    if not client:
        raise HTTPException(status_code=404, detail="Карточка не найдена")

    digest_row = get_latest_digest(client["id"])
    digest = json.loads(digest_row["summary_json"]) if digest_row else None
    digest_date = digest_row["generated_at"][:10] if digest_row else None

    trend = None
    if digest:
        prev_row = get_previous_digest(client["id"])
        prev_digest = json.loads(prev_row["summary_json"]) if prev_row else None
        if prev_digest and digest.get("average_rating") is not None and prev_digest.get("average_rating") is not None:
            if digest["average_rating"] > prev_digest["average_rating"]:
                trend = "up"
            elif digest["average_rating"] < prev_digest["average_rating"]:
                trend = "down"

    sources = get_client_sources(client["id"], active_only=True)
    platform_names = sorted({PLATFORMS.get(s["platform"], s["platform"]) for s in sources})

    return templates.TemplateResponse(
        request,
        "card.html",
        {
            "client": client,
            "digest": digest,
            "digest_date": digest_date,
            "trend": trend,
            "total_reviews": count_reviews(client["id"]),
            "platform_names": platform_names,
        },
    )


@app.get("/d/{slug}/reviews", response_class=HTMLResponse)
def dashboard_reviews_fragment(slug: str, request: Request, offset: int = 0):
    """Порция отзывов для догрузки без перезагрузки страницы (кнопка "Показать
    ещё" на дашборде, см. JS в dashboard.html). Возвращает только HTML-фрагмент
    карточек отзывов, без разметки всей страницы."""
    client = _get_client_or_404(slug)
    offset = max(0, offset)
    reviews = get_recent_reviews_page(client["id"], offset=offset, limit=REVIEWS_PAGE_SIZE)
    return templates.TemplateResponse(request, "_reviews_fragment.html", {"reviews": reviews})


@app.get("/d/{slug}/manifest.json")
def manifest(slug: str):
    client = _get_client_or_404(slug)
    return JSONResponse({
        "name": f"Отзывы — {client['name']}",
        "short_name": "Отзывы",
        "start_url": f"/d/{slug}",
        "scope": f"/d/{slug}",
        "display": "standalone",
        "background_color": "#f5f6f8",
        "theme_color": "#2563EB",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


@app.get("/sw.js")
def service_worker():
    return FileResponse(os.path.join(BASE_DIR, "static", "sw.js"), media_type="application/javascript")


# --- клиент сам управляет своими источниками — доступ по секретной ссылке, без отдельного логина ---

@app.get("/d/{slug}/sources", response_class=HTMLResponse)
def client_sources_page(slug: str, request: Request):
    client = _get_client_or_404(slug)
    return _render_sources_page(request, client, mode="client")


@app.post("/d/{slug}/sources")
def client_sources_add(slug: str, platform: str = Form(...), url: str = Form(...)):
    client = _get_client_or_404(slug)
    if platform not in PLATFORMS_AVAILABLE:
        raise HTTPException(status_code=422,
                            detail="Площадка недоступна для опроса")
    url = url.strip()
    add_source(client["id"], platform, url, label=_identify(platform, url))
    return RedirectResponse(url=f"/d/{slug}/sources", status_code=303)


@app.post("/d/{slug}/sources/{source_id}/delete")
def client_sources_delete(slug: str, source_id: int):
    client = _get_client_or_404(slug)
    delete_source(source_id, client["id"])
    return RedirectResponse(url=f"/d/{slug}/sources", status_code=303)


# --- админка агентства ---

@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_list(request: Request):
    clients = list_clients_with_stats()
    return templates.TemplateResponse(
        request,
        "admin_list.html",
        {"clients": clients, "dashboard_base_url": DASHBOARD_BASE_URL},
    )


@app.get("/admin/new", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_new_form(request: Request):
    return templates.TemplateResponse(request, "admin_new.html", {"error": None})


@app.post("/admin/new", dependencies=[Depends(require_admin)])
def admin_new_submit(name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Укажите название")
    slug = add_client(name=name)
    # дальше площадки добавляются на странице управления клиентом — той же, что при редактировании
    return RedirectResponse(url=f"/admin/{slug}", status_code=303)


@app.get("/admin/{slug}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_client_page(slug: str, request: Request):
    client = _get_client_or_404(slug)
    return _render_sources_page(request, client, mode="admin")


@app.post("/admin/{slug}/sources", dependencies=[Depends(require_admin)])
def admin_sources_add(slug: str, platform: str = Form(...), url: str = Form(...)):
    client = _get_client_or_404(slug)
    if platform not in PLATFORMS_AVAILABLE:
        raise HTTPException(status_code=422,
                            detail="Площадка недоступна для опроса")
    url = url.strip()
    add_source(client["id"], platform, url, label=_identify(platform, url))
    return RedirectResponse(url=f"/admin/{slug}", status_code=303)


@app.post("/admin/{slug}/sources/{source_id}/delete", dependencies=[Depends(require_admin)])
def admin_sources_delete(slug: str, source_id: int):
    client = _get_client_or_404(slug)
    delete_source(source_id, client["id"])
    return RedirectResponse(url=f"/admin/{slug}", status_code=303)


@app.post("/admin/{slug}/sources/{source_id}/toggle", dependencies=[Depends(require_admin)])
def admin_sources_toggle(slug: str, source_id: int):
    client = _get_client_or_404(slug)
    toggle_source_active(source_id, client["id"])
    return RedirectResponse(url=f"/admin/{slug}", status_code=303)


@app.post("/admin/{slug}/toggle-client", dependencies=[Depends(require_admin)])
def admin_client_toggle(slug: str):
    """Пауза всего клиента разом — cron его пропускает, дашборд остаётся доступен."""
    client = _get_client_or_404(slug)
    toggle_client_scraping(client["id"])
    return RedirectResponse(url=f"/admin/{slug}", status_code=303)


@app.post("/admin/{slug}/refresh", dependencies=[Depends(require_admin)])
def admin_client_refresh(slug: str):
    """Ручной прогон скрапа+саммари для одного клиента — та же логика и та же
    защита от лишних токенов (саммари пересобирается только если есть новое),
    что и в ночном cron. Клиентам такой кнопки нет — только админке."""
    client = _get_client_or_404(slug)
    result = update_client(client)
    if result["errors"]:
        msg = f"Готово с ошибками: новых отзывов {result['new_count']}, саммари {'обновлено' if result['summary_updated'] else 'не обновлено'}. {'; '.join(result['errors'])}"
    elif result["new_count"] == 0:
        msg = "Новых отзывов нет — саммари не трогали"
    elif result["summary_updated"]:
        msg = f"Найдено новых отзывов: {result['new_count']}, саммари обновлено"
    else:
        msg = f"Найдено новых отзывов: {result['new_count']}, но саммари обновить не удалось"
    return RedirectResponse(url=f"/admin/{slug}?msg={urllib.parse.quote(msg)}", status_code=303)
