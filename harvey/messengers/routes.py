"""Dashboard API for the Messengers tab. Mounted by harvey/dashboard.py, so
the same token, Host and Origin checks guard it as every other route."""

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from harvey.messengers import gate
from harvey.messengers.bots import missing_keys
from harvey.messengers.contacts import CHANNEL_LABELS, open_link, prefills_text
from harvey.messengers.store import MessengerStore

router = APIRouter()


def _refuse(message: str, status: int = 409) -> JSONResponse:
    return JSONResponse({"success": False, "message": message}, status_code=status)


async def _ctx():
    """(store, config) against the dashboard's database and config file."""
    from harvey import dashboard as dash
    from harvey.config import load_config
    state = dash._state()
    await state.init_db()
    config = load_config(str(dash._resolve_config_file()))
    return MessengerStore(state), config, state


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _today() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _view(item: dict) -> dict:
    channel, handle = item.get("channel", ""), item.get("handle", "")
    return {**item, "channel_label": CHANNEL_LABELS.get(channel, channel),
            "prefills": prefills_text(channel, handle)}


@router.get("/api/messengers")
async def messengers_overview():
    try:
        store, config, _ = await _ctx()
    except Exception as e:
        return {"error": f"Couldn't read the config: {e}"}
    from harvey.config import load_env
    cfg = config.channels.messengers
    env = load_env()
    sent_today = await store.sent_since(_today())
    return {
        "enabled": cfg.enabled,
        "require_approval": cfg.require_approval,
        "bots": {
            "telegram": cfg.telegram.enabled, "vk": cfg.vk.enabled,
            "whatsapp": cfg.whatsapp.enabled,
        },
        "problems": missing_keys(config, env),
        "sent_today": sent_today,
        "daily_cap": cfg.max_daily_manual_sends,
        "queue": [_view(i) for i in await store.list_cold(("pending_review",))],
        "scheduled": [_view(i) for i in await store.list_cold(("scheduled",))],
        "done": [_view(i) for i in await store.list_cold(("sent", "skipped", "cancelled"),
                                                         limit=30, newest_first=True)],
        "threads": await store.list_threads(),
    }


@router.post("/api/messengers/cold/{item_id}/open")
async def cold_open(item_id: str):
    """The link that opens this chat, once the message passes the checks and
    today's limit has room. Nothing is marked sent: you send it, then say so."""
    store, config, _ = await _ctx()
    item = await store.get_cold(item_id)
    if not item or item["status"] != "pending_review":
        return _refuse("This message isn't waiting to be sent.")
    cfg = config.channels.messengers
    if await store.sent_since(_today()) >= cfg.max_daily_manual_sends:
        return _refuse(f"Today's limit of {cfg.max_daily_manual_sends} first messages is reached "
                       "(channels.messengers.max_daily_manual_sends). More tomorrow: one account "
                       "writing to many strangers in a day is what gets it limited.")
    company = await store._rows("SELECT domain FROM companies WHERE id = ?", (item["company_id"],))
    domain = company[0]["domain"] if company else ""
    issues = gate.problems(item["body"], config, kind="cold", company_domain=domain)
    if issues:
        return _refuse("Fix this first: " + "; ".join(issues))
    link = open_link(item["channel"], item["handle"], item["body"])
    if not link:
        return _refuse("Harvey can't build a link for this contact.")
    return {"success": True, "link": link, "text": item["body"],
            "prefills": prefills_text(item["channel"], item["handle"])}


@router.post("/api/messengers/cold/{item_id}/sent")
async def cold_sent(item_id: str):
    store, config, state = await _ctx()
    if not await store.mark_cold_sent(item_id, config.channels.messengers.followup_days):
        return _refuse("This message isn't waiting to be sent.")
    await state.log_action("messenger_manual_sent", "dashboard", {"item": item_id})
    return {"success": True}


@router.post("/api/messengers/cold/{item_id}/skip")
async def cold_skip(item_id: str):
    store, _, _ = await _ctx()
    if not await store.skip_cold(item_id):
        return _refuse("This message can't be skipped now.")
    return {"success": True}


@router.post("/api/messengers/cold/{item_id}/edit")
async def cold_edit(item_id: str, request: Request):
    body = str((await _body(request)).get("body") or "").strip()
    if not body:
        return _refuse("The message can't be empty.", 400)
    store, _, _ = await _ctx()
    if not await store.edit_cold(item_id, body):
        return _refuse("Only a message not yet sent can be edited.")
    return {"success": True}


@router.post("/api/messengers/cold/{item_id}/replied")
async def cold_replied(item_id: str, request: Request):
    """They answered in your own app. The follow-up is dropped, and if you
    paste their answer Harvey drafts what to say back."""
    text = str((await _body(request)).get("text") or "").strip()
    store, _, state = await _ctx()
    item = await store.get_cold(item_id)
    if not item:
        return _refuse("No such message.", 404)
    await store.cancel_cold_for(item["company_id"])
    thread = await store.thread_for(item["channel"], "manual",
                                    f"{item['channel']}:{item['handle']}",
                                    handle=item["handle"], company_id=item["company_id"])
    sent = await store._rows(
        "SELECT body, sent_at FROM messenger_outbox WHERE company_id = ? AND status = 'sent' ORDER BY step",
        (item["company_id"],))
    known = await store.history(thread["id"])
    if not known:
        for row in sent:   # what you sent them, so the draft knows the conversation
            mid = await store.add_outbound(thread["id"], row["body"], "approved", approved_by="you")
            await store.mark_message_sent(mid)
    if text:
        await store.add_inbound(thread["id"], text)
    await state.log_action("messenger_manual_reply", "dashboard", {"item": item_id})
    return {"success": True, "thread_id": thread["id"]}


@router.post("/api/messengers/threads/{thread_id}/paste")
async def thread_paste(thread_id: str, request: Request):
    text = str((await _body(request)).get("text") or "").strip()
    if not text:
        return _refuse("Paste what they wrote.", 400)
    store, _, _ = await _ctx()
    thread = await store.get_thread(thread_id)
    if not thread or thread["transport"] != "manual":
        return _refuse("Only a conversation in your own account takes pasted messages.")
    await store.add_inbound(thread_id, text)
    return {"success": True}


@router.post("/api/messengers/threads/{thread_id}/status")
async def thread_status(thread_id: str, request: Request):
    status = (await _body(request)).get("status")
    if status not in ("open", "closed", "opted_out"):
        return _refuse('expected {"status": "open" | "closed" | "opted_out"}', 400)
    store, _, _ = await _ctx()
    thread = await store.get_thread(thread_id)
    if not thread:
        return _refuse("No such conversation.", 404)
    await store.set_thread(thread_id, status=status, status_reason="you")
    if status != "open":
        await store.cancel_outbound(thread_id)
        if thread.get("company_id"):
            await store.cancel_cold_for(thread["company_id"])
    return {"success": True}


@router.post("/api/messengers/messages/{message_id}/{action}")
async def message_action(message_id: str, action: str, request: Request):
    store, _, _ = await _ctx()
    msg = await store.get_message(message_id)
    if not msg or msg["direction"] != "out":
        return _refuse("No such draft.", 404)
    if action == "approve":
        if msg["transport"] != "bot":
            return _refuse("This conversation is in your own account: copy the text and send it there.")
        ok = await store.approve_message(message_id, by="you")
    elif action == "copied":
        if msg["transport"] != "manual" or msg["status"] not in ("pending_review", "held"):
            return _refuse("Only a draft for your own account is marked sent this way.")
        ok = await store.mark_message_sent(message_id)
    elif action == "reject":
        ok = await store.reject_message(message_id)
    elif action == "edit":
        body = str((await _body(request)).get("body") or "").strip()
        if not body:
            return _refuse("The message can't be empty.", 400)
        ok = await store.edit_message(message_id, body)
    else:
        return _refuse("unknown action", 400)
    if not ok:
        return _refuse("That can't be done to this message now.")
    return {"success": True}
