import json
import os
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse, Response

app = FastAPI()

# Путь к базе данных номеров и доменов
DB_FILE = "/app/data/routes.json"

# API Ключ для защиты управления и для связи с Django
API_KEY = os.getenv("GATEWAY_API_KEY")
if not API_KEY:
    raise RuntimeError("GATEWAY_API_KEY is not set")

# Секрет, который WAHA присылает в заголовке X-Webhook-Secret
WEBHOOK_SECRET = os.getenv("GATEWAY_WEBHOOK_SECRET")

# Для страницы входа по QR
WAHA_INTERNAL_URL = os.getenv("WAHA_INTERNAL_URL", "http://waha_service:3000")
WAHA_API_KEY = os.getenv("WAHA_API_KEY")
WAHA_SESSION = os.getenv("WAHA_SESSION", "default")
LOGIN_TOKEN = os.getenv("GATEWAY_LOGIN_TOKEN")

routing_map = {}


def load_db():
    global routing_map
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                routing_map = json.load(f)
        except Exception as e:
            print(f"Error loading DB: {e}")
            routing_map = {}


def save_db():
    with open(DB_FILE, "w") as f:
        json.dump(routing_map, f)


# Загружаем базу при старте
load_db()


def send_to_backend(domain, data):
    """
    Функция отправки на Django бэкенд.
    Выполняется в фоне, не заставляя WAHA ждать.
    """
    base_url = domain.rstrip('/')
    target_url = f"{base_url}/notifications/api/whatsapp/webhook/"
    try:
        # Ставим таймаут 10, так как в фоне это не мешает работе шлюза
        requests.post(
            target_url,
            json=data,
            headers={"X-Api-Key": API_KEY},
            timeout=10
        )
    except Exception as e:
        # Логируем ошибку, если бэкенд недоступен
        print(f"[ERROR] Failed to send to {domain}: {e}")


@app.middleware("http")
async def verify_api_key(request: Request, call_next):
    # Защищаем эндпоинты управления
    path = request.url.path
    if path in ("/register", "/list") or path.startswith("/remove/"):
        if request.headers.get("X-Api-Key") != API_KEY:
            return JSONResponse({"detail": "Forbidden: Invalid API Key"}, status_code=403)
    elif path == "/webhook" and WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)


# --- Эндпоинты управления ---

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/register")
async def register(request: Request):
    data = await request.json()
    phone = str(data.get("phone"))
    domain = data.get("domain")
    if phone and domain:
        routing_map[phone] = domain
        save_db()
    return {"status": "ok"}


@app.get("/list")
async def list_all():
    return routing_map


@app.delete("/remove/{phone}")
async def remove_phone(phone: str):
    if phone in routing_map:
        del routing_map[phone]
        save_db()
    return {"status": "ok"}


# --- Вход в WhatsApp по QR ---

def waha_request(method, path, **kwargs):
    return requests.request(
        method,
        f"{WAHA_INTERNAL_URL}{path}",
        headers={"X-Api-Key": WAHA_API_KEY},
        timeout=15,
        **kwargs,
    )


def check_login_token(token):
    return bool(LOGIN_TOKEN) and token == LOGIN_TOKEN


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
async def login_page(token: str = ""):
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
async def login_qr(token: str = ""):
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
    except:
        return {"status": "error", "message": "invalid json"}

    # Работаем только с сообщениями
    if data.get("event") not in ["message", "message.upsert"]:
        return {"status": "ignored"}

    payload = data.get("payload", {})
    _data = payload.get("_data", {})
    key = _data.get("key", {})

    # 1. Каскадный поиск реального номера отправителя
    # Сначала remoteJidAlt (самый надежный для обхода LID), потом participant, потом from
    sender_raw = key.get("remoteJidAlt") or payload.get("participant") or payload.get("from", "")

    # Очищаем от тех. суффиксов (@c.us, @s.whatsapp.net, @lid)
    sender = sender_raw.split('@')[0] if sender_raw else None

    # 2. Определяем, кому отправлять (убираем дубли доменов)
    target_domain = routing_map.get(sender)
    if target_domain:
        unique_domains = {target_domain}
    else:
        # Если номер не в базе (LID или новый клиент) — шлем всем уникальным доменам
        unique_domains = set(routing_map.values())

    # 3. Добавляем задачи на отправку в фон
    for domain in unique_domains:
        background_tasks.add_task(send_to_backend, domain, data)

    # МГНОВЕННЫЙ ОТВЕТ: WAHA увидит это и не будет делать повторных попыток (retries)
    return {"status": "ok", "queued_tasks": len(unique_domains)}
