import hmac
import json
import logging
import os
import re
import shutil
import threading
import time

import requests
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")

app = FastAPI()

# Путь к базе данных номеров и доменов
DB_FILE = "/app/data/routes.json"
DB_BACKUP = DB_FILE + ".bak"

# Файлы сессии WAHA (только чтение): lid-mapping-<LID>_reverse.json хранит номер телефона
LID_DIR = os.getenv("WAHA_LID_DIR", "/app/waha_lids")

# API Ключ для защиты управления и для связи с Django
API_KEY = os.getenv("GATEWAY_API_KEY")
if not API_KEY:
    raise RuntimeError("GATEWAY_API_KEY is not set")

# Секрет, который WAHA присылает в заголовке X-Webhook-Secret
WEBHOOK_SECRET = os.getenv("GATEWAY_WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    raise RuntimeError("GATEWAY_WEBHOOK_SECRET is not set")

# Для страницы входа по QR
WAHA_INTERNAL_URL = os.getenv("WAHA_INTERNAL_URL", "http://waha_service:3000")
WAHA_API_KEY = os.getenv("WAHA_API_KEY")
WAHA_SESSION = os.getenv("WAHA_SESSION", "default")
LOGIN_TOKEN = os.getenv("GATEWAY_LOGIN_TOKEN")

# Повторы доставки в Django, если бэкенд временно недоступен
BACKEND_RETRY_DELAYS = (2, 10, 30)

routing_map = {}
db_lock = threading.Lock()


def secure_equals(a, b):
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


def normalize_phone(value):
    """Та же нормализация, что в Django PhoneService.normalize: 7XXXXXXXXXX или None."""
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return digits
    return None


def read_routes(path):
    with open(path, "r") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("routes must be a JSON object")
    return data


def load_db():
    global routing_map
    for path in (DB_FILE, DB_BACKUP):
        if not os.path.exists(path):
            continue
        try:
            routing_map = read_routes(path)
            log.info("Loaded %d routes from %s", len(routing_map), path)
            return
        except Exception as e:
            log.error("Error loading %s: %s", path, e)
    routing_map = {}
    log.warning("Routes are empty")


def save_db():
    # Атомарная запись: сначала во временный файл, потом rename.
    # Если процесс упадёт посреди записи, старый routes.json останется целым.
    with db_lock:
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(routing_map, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(DB_FILE):
            shutil.copy2(DB_FILE, DB_BACKUP)
        os.replace(tmp, DB_FILE)


# Загружаем базу при старте
load_db()


def lid_to_phone(lid):
    """Номер телефона по LID из файлов сессии WAHA (без включения NOWEB store)."""
    if not lid.isdigit():
        return None
    try:
        with open(os.path.join(LID_DIR, f"lid-mapping-{lid}_reverse.json")) as f:
            return normalize_phone(json.load(f))
    except (OSError, ValueError):
        return None


def resolve_sender(payload):
    """
    Возвращает номер отправителя 7XXXXXXXXXX или None.
    Порядок: remoteJidAlt (самый надежный для обхода LID), participant, from.
    """
    key = (payload.get("_data") or {}).get("key") or {}
    for raw in (key.get("remoteJidAlt"), payload.get("participant"), payload.get("from")):
        if not raw:
            continue
        user, _, server = str(raw).partition("@")
        if server == "lid":
            phone = lid_to_phone(user)
        elif server == "g.us":
            continue
        else:
            phone = normalize_phone(user)
        if phone:
            return phone
    return None


def send_to_backend(domain, data):
    """
    Функция отправки на Django бэкенд.
    Выполняется в фоне, не заставляя WAHA ждать. При сетевой ошибке или 5xx — повторяем.
    """
    target_url = f"{domain.rstrip('/')}/notifications/api/whatsapp/webhook/"
    for attempt, delay in enumerate((0,) + BACKEND_RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            r = requests.post(target_url, json=data, headers={"X-Api-Key": API_KEY}, timeout=10)
            if r.status_code < 500:
                if r.status_code >= 400:
                    log.warning("Backend %s answered %s: %s", domain, r.status_code, r.text[:200])
                return
            log.warning("Backend %s answered %s (attempt %d)", domain, r.status_code, attempt)
        except Exception as e:
            log.warning("Failed to send to %s (attempt %d): %s", domain, attempt, e)
    log.error("Giving up delivering message to %s", domain)


@app.middleware("http")
async def verify_api_key(request: Request, call_next):
    # Защищаем эндпоинты управления
    path = request.url.path
    if path in ("/register", "/list") or path.startswith("/remove/"):
        if not secure_equals(request.headers.get("X-Api-Key"), API_KEY):
            return JSONResponse({"detail": "Forbidden: Invalid API Key"}, status_code=403)
    elif path == "/webhook":
        if not secure_equals(request.headers.get("X-Webhook-Secret"), WEBHOOK_SECRET):
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)


# --- Эндпоинты управления ---

@app.get("/health")
async def health():
    return {"status": "ok", "routes": len(routing_map)}


@app.post("/register")
async def register(request: Request):
    data = await request.json()
    phone = normalize_phone(data.get("phone"))
    domain = str(data.get("domain") or "").rstrip("/")
    if not phone or not domain.startswith("https://") or "localhost" in domain:
        log.warning("Rejected register: phone=%r domain=%r", data.get("phone"), data.get("domain"))
        return JSONResponse({"status": "error", "detail": "invalid phone or domain"}, status_code=400)
    if routing_map.get(phone) != domain:
        old = routing_map.get(phone)
        routing_map[phone] = domain
        save_db()
        log.info("Registered %s -> %s%s", phone, domain, f" (was {old})" if old else "")
    return {"status": "ok"}


@app.get("/list")
async def list_all():
    return routing_map


@app.delete("/remove/{phone}")
async def remove_phone(phone: str):
    if phone not in routing_map:
        phone = normalize_phone(phone) or phone
    if phone in routing_map:
        del routing_map[phone]
        save_db()
        log.info("Removed %s", phone)
    return {"status": "ok"}


# --- Вход в WhatsApp по QR ---
# Обычные (не async) функции: FastAPI выполняет их в пуле потоков,
# поэтому медленный ответ WAHA не блокирует приём вебхуков.

def waha_request(method, path, **kwargs):
    return requests.request(
        method,
        f"{WAHA_INTERNAL_URL}{path}",
        headers={"X-Api-Key": WAHA_API_KEY},
        timeout=15,
        **kwargs,
    )


def check_login_token(token):
    return secure_equals(token, LOGIN_TOKEN)


LOGIN_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход в WhatsApp</title>
<style>body{font-family:system-ui,sans-serif;background:#f4f5f7;color:#222;display:flex;
justify-content:center;padding:24px 16px;margin:0}.card{background:#fff;border-radius:12px;
padding:24px;max-width:420px;width:100%;text-align:center;box-shadow:0 2px 12px rgba(0,0,0,.08)}
img{width:100%;max-width:320px}.ok{color:#128c3e;font-size:20px}.muted{color:#666;font-size:14px}</style>
</head><body><div class="card"><h2>WhatsApp — RehubPro</h2>{body}</div>
<script>setTimeout(function(){location.reload()},{refresh})</script></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(token: str = ""):
    if not check_login_token(token):
        return HTMLResponse("Forbidden", status_code=403)
    try:
        status = waha_request("GET", f"/api/sessions/{WAHA_SESSION}").json().get("status")
    except Exception:
        status = None

    if status == "WORKING":
        body = '<p class="ok">✅ Подключено, всё работает</p><p class="muted">Эту страницу можно закрыть.</p>'
        refresh = 60000
    elif status == "SCAN_QR_CODE":
        body = (f'<img src="/login/qr.png?token={token}" alt="QR">'
                '<p>WhatsApp на телефоне → Настройки → Связанные устройства → Привязка устройства.</p>'
                '<p class="muted">QR обновляется автоматически.</p>')
        refresh = 15000
    else:
        if status in ("FAILED", "STOPPED"):
            try:
                action = "start" if status == "STOPPED" else "restart"
                waha_request("POST", f"/api/sessions/{WAHA_SESSION}/{action}")
            except Exception:
                pass
        body = f'<p>Запускаю сессию… ({status or "WAHA недоступна"})</p><p class="muted">Страница обновится сама.</p>'
        refresh = 5000
    return LOGIN_PAGE.replace("{body}", body).replace("{refresh}", str(refresh))


@app.get("/login/qr.png")
def login_qr(token: str = ""):
    if not check_login_token(token):
        return Response(status_code=403)
    try:
        r = waha_request("GET", f"/api/{WAHA_SESSION}/auth/qr", params={"format": "image"})
    except Exception:
        return Response(status_code=502)
    if r.status_code != 200:
        return Response(status_code=404)
    return Response(r.content, media_type=r.headers.get("content-type", "image/png"),
                    headers={"Cache-Control": "no-store"})


# --- Основной Webhook ---

@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
    except Exception:
        return {"status": "error", "message": "invalid json"}

    # Работаем только с сообщениями
    if data.get("event") not in ["message", "message.upsert"]:
        return {"status": "ignored"}

    payload = data.get("payload") or {}
    phone = resolve_sender(payload)
    if not phone:
        log.info("Unresolved sender from=%s participant=%s", payload.get("from"), payload.get("participant"))
        return {"status": "ignored", "reason": "unresolved sender"}

    # Подставляем настоящий номер, чтобы Django не споткнулся об LID
    payload.setdefault("_data", {}).setdefault("key", {})["remoteJidAlt"] = f"{phone}@s.whatsapp.net"

    # Шлём только тому клиенту, за которым закреплён номер.
    # Неизвестные номера никуда не пересылаем — чтобы сообщения не попадали в чужие системы.
    domain = routing_map.get(phone)
    if not domain:
        log.info("No route for %s, message dropped", phone)
        return {"status": "ignored", "reason": "no route"}

    background_tasks.add_task(send_to_backend, domain, data)

    # МГНОВЕННЫЙ ОТВЕТ: WAHA увидит это и не будет делать повторных попыток (retries)
    return {"status": "ok", "queued_tasks": 1}
