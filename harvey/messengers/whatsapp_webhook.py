"""Where WhatsApp Business messages arrive: Meta's Cloud API webhook.

Meta delivers messages by calling a public HTTPS address, so unlike the
dashboard this server is meant to be reached from outside, through a
reverse proxy with TLS (docs/messengers.md). It does one thing: store the
text messages Meta signed. Every POST must carry Meta's
X-Hub-Signature-256 over the exact body, made with the app secret; anything
else is refused before it is read as JSON. The heartbeat answers them.
"""

import hashlib
import hmac
import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from harvey.messengers.bots import NO_TEXT, _utc
from harvey.messengers.contacts import normalize_phone
from harvey.messengers.store import MessengerStore

logger = logging.getLogger("harvey.messengers.whatsapp")

MAX_BODY_BYTES = 1_000_000


def signature_ok(secret: str, body: bytes, header: str) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip().lower())


def messages_in(payload: dict) -> list[dict]:
    """[{wa_id, name, id, at, text, has_text}] from one webhook delivery."""
    out = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                     for c in value.get("contacts") or []}
            for msg in value.get("messages") or []:
                wa_id = normalize_phone(msg.get("from", ""))
                if not wa_id or not msg.get("id"):
                    continue
                if msg.get("type") == "text":
                    text = (msg.get("text") or {}).get("body", "")
                elif msg.get("type") == "button":
                    text = (msg.get("button") or {}).get("text", "")
                else:
                    text = ""
                out.append({"wa_id": wa_id, "name": names.get(msg.get("from"), ""),
                            "id": msg["id"], "at": _utc(msg.get("timestamp")),
                            "text": text or NO_TEXT, "has_text": bool(text)})
    return out


def create_app(state, env) -> FastAPI:
    app = FastAPI(title="Harvey WhatsApp webhook", docs_url=None, redoc_url=None, openapi_url=None)
    store = MessengerStore(state)

    @app.get("/webhook")
    async def verify(request: Request):
        q = request.query_params
        if (q.get("hub.mode") == "subscribe" and env.whatsapp_verify_token
                and hmac.compare_digest(q.get("hub.verify_token", ""), env.whatsapp_verify_token)):
            return PlainTextResponse(q.get("hub.challenge", ""))
        return PlainTextResponse("forbidden", status_code=403)

    @app.post("/webhook")
    async def receive(request: Request):
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return PlainTextResponse("too large", status_code=413)
        if not signature_ok(env.whatsapp_app_secret, body,
                            request.headers.get("x-hub-signature-256", "")):
            logger.warning("WhatsApp webhook: a delivery without a valid signature was refused")
            return PlainTextResponse("bad signature", status_code=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return PlainTextResponse("bad json", status_code=400)
        stored = 0
        for m in messages_in(payload if isinstance(payload, dict) else {}):
            thread = await store.thread_for("whatsapp", "bot", m["wa_id"],
                                            handle=m["wa_id"], display_name=m["name"])
            if await store.add_inbound(thread["id"], m["text"], m["id"], m["at"]):
                stored += 1
        # Delivery statuses ("sent", "read") come here too and need nothing.
        return JSONResponse({"ok": True, "stored": stored})

    return app


def serve(host: str = "127.0.0.1", port: int = 8787):
    """Run the webhook. Put it behind an HTTPS reverse proxy; Meta calls
    https://<your-domain>/webhook."""
    import asyncio

    import uvicorn

    from harvey.config import load_env
    from harvey.state import StateManager

    env = load_env()
    if not env.whatsapp_app_secret or not env.whatsapp_verify_token:
        raise SystemExit("Set WHATSAPP_APP_SECRET and WHATSAPP_VERIFY_TOKEN in .env first: "
                         "without them no delivery can be checked.")
    state = StateManager()
    asyncio.run(state.init_db())
    print(f"\n  WhatsApp webhook listening on http://{host}:{port}/webhook")
    print("  Point an HTTPS reverse proxy at it and give Meta https://<your-domain>/webhook.\n")
    uvicorn.run(create_app(state, env), host=host, port=port, log_level="warning",
                server_header=False, date_header=False)
