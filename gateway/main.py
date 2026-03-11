import json
import os
import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks

app = FastAPI()

# Путь к базе данных номеров и доменов
DB_FILE = "/app/data/routes.json"

# API Ключ для защиты управления и для связи с Django
API_KEY = os.getenv("GATEWAY_API_KEY", "fallback_key_if_env_is_missing")

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
    if request.url.path in ["/register", "/list", "/remove"]:
        key = request.headers.get("X-Api-Key")
        if key != API_KEY:
            raise HTTPException(status_code=403, detail="Forbidden: Invalid API Key")
    return await call_next(request)


# --- Эндпоинты управления ---

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