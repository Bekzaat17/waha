import json
import os
import requests
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()
DB_FILE = "/app/data/routes.json"
# Берем ключ из .env (который мы прописали в docker-compose)
API_KEY = os.getenv("GATEWAY_API_KEY", "fallback_key_if_env_is_missing")

routing_map = {}


def load_db():
    global routing_map
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                routing_map = json.load(f)
        except:
            routing_map = {}


def save_db():
    with open(DB_FILE, "w") as f:
        json.dump(routing_map, f)


load_db()


# --- Middleware для защиты ---
@app.middleware("http")
async def verify_api_key(request: Request, call_next):
    # Защищаем методы управления, но оставляем открытым /webhook для WAHA
    if request.url.path in ["/register", "/list", "/remove"]:
        key = request.headers.get("X-Api-Key")
        if key != API_KEY:
            raise HTTPException(status_code=403, detail="Forbidden: Invalid API Key")
    return await call_next(request)


# --- Эндпоинты ---

@app.post("/register")
async def register(request: Request):
    data = await request.json()
    phone = str(data.get("phone"))
    domain = data.get("domain")  # Ожидаем полный URL типа https://pulse.rehubpro.kz
    if phone and domain:
        routing_map[phone] = domain  # Прямая запись номер-домен
        save_db()
    return {"status": "ok"}


@app.get("/list")
async def list_all():
    """Отдать всё для глобальной сверки"""
    return routing_map


@app.get("/list/{domain_query}")
async def list_by_domain(domain_query: str):
    """Отдать номера конкретного домена"""
    return {p: d for p, d in routing_map.items() if domain_query in d}


@app.delete("/remove/{phone}")
async def remove_phone(phone: str):
    if phone in routing_map:
        del routing_map[phone]
        save_db()
    return {"status": "ok"}


@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    # WAHA присылает событие 'message' (или 'message.upsert' в новых версиях)
    if data.get("event") == "message":
        payload = data.get("payload", {})
        sender = payload.get("from", "").split('@')[0]

        target_domain = routing_map.get(sender)
        if target_domain:
            target_url = f"{target_domain.rstrip('/')}/notifications/api/whatsapp/webhook/"
            try:
                # Пересылаем вебхук в конкретный Django
                requests.post(target_url, json=data, timeout=3)
            except Exception as e:
                print(f"Forwarding error: {e}")

    return {"status": "ok"}