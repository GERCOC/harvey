"""The messenger agent: one pass per heartbeat.

1. Read what people wrote to the bot, the community or the WhatsApp number.
2. Answer each new message: opt-outs and anything a person must handle are
   decided by fixed rules first; the model drafts the rest. Drafts wait for
   you unless ``messengers.require_approval`` is off.
3. Send the answers that are approved.
4. Bring follow-ups whose day has come into the queue.
5. Draft first messages for businesses whose site links a messenger. Those
   wait in the dashboard for a person to send from their own account.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from harvey.messengers import gate
from harvey.messengers.bots import NO_TEXT, WhatsAppCloud, configured_bots
from harvey.messengers.contacts import CHANNEL_LABELS, SIGNAL_FOR
from harvey.messengers.store import MessengerStore, _now
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.messengers")

INTENTS = {"interested", "question", "objection", "not_interested", "unsubscribe",
           "escalate", "ooo", "wrong_person"}
REPLY_INTENTS = {"interested", "question", "objection", "wrong_person"}
# Facts that say how to reach a business, not what it is: never an opener.
_NOT_FACTS = ("MESSENGER_", "EMAIL_", "CONTACT_FORM_URL", "CONTACT_FOUND")
SEND_GAP_SECONDS = (1.0, 3.0)
GOALS = {"book_call": "созвон", "start_trial": "пробный период", "get_reply": "ответ"}


def _utc_midnight() -> datetime:
    now = _now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class MessengerAgent:
    def __init__(self, brain, state, config, env, transport=None):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env
        self.store = MessengerStore(state)
        self.bots = configured_bots(config, env, transport=transport)
        self.send_pacing = True

    @property
    def cfg(self):
        return self.config.channels.messengers

    async def run(self) -> dict:
        """One pass. Each part fails on its own: a VK outage doesn't stop
        Telegram, and nothing here stops the email side of the heartbeat."""
        if not self.cfg.enabled:
            return {}
        report: dict = {}
        if self.cfg.require_approval:
            report["requeued"] = await self.store.requeue_autopilot()
        for name, step in (("received", self.read_inbound), ("answered", self.answer_threads),
                           ("sent", self.send_approved), ("released", self.store.release_due),
                           ("drafted", self.draft_first_messages)):
            try:
                report[name] = await step()
            except Exception as e:
                logger.error(f"Messengers: {name} failed: {type(e).__name__}: {e}")
                report[name] = f"error: {e}"
        busy = {k: v for k, v in report.items() if v}
        if busy:
            logger.info(f"Messengers: {busy}")
        return report

    # ── 1. Read ──

    async def _company_for(self, channel: str, handle: str) -> str:
        """A business whose site lists this account, or ""."""
        if not handle:
            return ""
        rows = await self.store._rows(
            """SELECT company_id FROM observations WHERE signal_code = ?
               AND lower(value_text) = lower(?) AND company_id != ''
               ORDER BY observed_at DESC LIMIT 1""",
            (SIGNAL_FOR[channel], handle))
        return rows[0]["company_id"] if rows else ""

    async def read_inbound(self) -> int:
        stored = 0
        for channel, bot in self.bots.items():
            try:
                items = await bot.fetch(self.state)
            except Exception as e:
                logger.warning(f"Messengers: couldn't read {CHANNEL_LABELS[channel]}: {e}")
                continue
            for item in items:
                thread = await self.store.thread_for(
                    channel, "bot", item.chat_id, handle=item.handle,
                    display_name=item.display_name,
                    company_id=await self._company_for(channel, item.handle or item.chat_id))
                if item.is_start:
                    # Pressing Start isn't a message to answer; the welcome
                    # (fixed text, no model) is, once per chat.
                    await self._welcome(bot, thread)
                    continue
                if await self.store.add_inbound(thread["id"], item.body, item.provider_msg_id, item.at):
                    stored += 1
            await bot.commit(self.state)
        return stored

    async def _welcome(self, bot, thread) -> None:
        text = (self.cfg.telegram.welcome or "").strip()
        if not text or thread["status"] == "opted_out":
            return
        sent_before = await self.store._rows(
            "SELECT 1 FROM messenger_messages WHERE thread_id = ? AND direction = 'out' LIMIT 1",
            (thread["id"],))
        if sent_before:
            return
        result = await bot.send(thread["chat_id"], text)
        mid = await self.store.add_outbound(thread["id"], text, "approved", approved_by="welcome")
        if result.ok:
            await self.store.mark_message_sent(mid, result.provider_msg_id)
        else:
            await self.store.mark_message_failed(mid, result.error)

    # ── 2. Answer ──

    async def answer_threads(self) -> int:
        drafted = 0
        for thread in await self.store.threads_to_answer():
            try:
                outcome = await self._answer(thread)
            except BrainPaused as e:
                logger.info(f"Messengers: answers wait, the model is unavailable ({e})")
                break
            drafted += outcome
        return drafted

    async def _new_texts(self, thread_id: str) -> list[dict]:
        return await self.store._rows(
            """SELECT * FROM messenger_messages WHERE thread_id = ? AND direction = 'in'
               AND status = 'received' ORDER BY created_at, rowid""", (thread_id,))

    async def _stop(self, thread, status: str, reason: str, intent: str = "") -> None:
        await self.store.set_thread(thread["id"], status=status, status_reason=reason,
                                    **({"intent": intent} if intent else {}))
        await self.store.cancel_outbound(thread["id"])
        if status in ("opted_out", "closed") and thread.get("company_id"):
            await self.store.cancel_cold_for(thread["company_id"])
        await self.store.mark_inbound_handled(thread["id"])
        await self.state.log_action(f"messenger_{status}", "messengers",
                                    {"thread": thread["id"], "channel": thread["channel"],
                                     "reason": reason})

    async def _answer(self, thread) -> int:
        """1 when a draft was queued, else 0."""
        new = await self._new_texts(thread["id"])
        if not new:
            return 0
        if thread["status"] in ("opted_out", "needs_human"):
            # Kept for you to read, never answered: an opt-out is final, and a
            # conversation handed to you stays yours until you hand it back.
            await self.store.mark_inbound_handled(thread["id"])
            return 0
        text = "\n".join(m["body"] for m in new)

        opt = gate.opt_out_reason(text)
        if opt:
            await self._stop(thread, "opted_out", f"написали «{opt}»", "unsubscribe")
            return 0
        why = gate.escalation_reason(text)
        if why or any(m["body"] == NO_TEXT for m in new):
            reason = f"в сообщении «{why}»" if why else "сообщение без текста"
            await self._stop(thread, "needs_human", reason, "escalate" if why else "")
            return 0

        result = await self._draft_reply(thread, text)
        intent = result.get("intent", "")
        reply = (result.get("reply") or "").strip()
        if intent == "unsubscribe":
            await self._stop(thread, "opted_out", "модель прочитала это как просьбу не писать", intent)
            return 0
        if intent == "escalate" or intent not in INTENTS:
            await self._stop(thread, "needs_human",
                             "модель не смогла ответить" if not intent else "нужен человек",
                             "escalate")
            return 0
        if intent == "not_interested":
            await self._stop(thread, "closed", "отказ", intent)
            return 0
        if intent == "ooo" or intent not in REPLY_INTENTS or not reply:
            await self.store.set_thread(thread["id"], intent=intent)
            await self.store.mark_inbound_handled(thread["id"])
            return 0

        await self.store.cancel_outbound(thread["id"])   # an older draft is out of date
        domain = await self._company_domain(thread.get("company_id", ""))
        blocked = gate.problems(reply, self.config, kind="reply", company_domain=domain)
        held = gate.holds(reply, self.config, company_domain=domain)
        autopilot = not self.cfg.require_approval and thread["transport"] == "bot"
        if blocked:
            status, by, err = "held", "", "; ".join(blocked)
        elif autopilot and held:
            status, by, err = "held", "", "; ".join(held)
        elif autopilot:
            status, by, err = "approved", "autopilot", ""
        else:
            status, by, err = "pending_review", "", "; ".join(held)
        await self.store.add_outbound(thread["id"], reply, status, approved_by=by, error=err)
        await self.store.set_thread(thread["id"], intent=intent, status="open",
                                    stage=self._next_stage(intent, thread.get("stage", "")))
        await self.store.mark_inbound_handled(thread["id"])
        return 1

    @staticmethod
    def _next_stage(intent: str, stage: str) -> str:
        ladder = ["initial_outreach", "engaged", "qualifying", "presenting", "negotiating", "closing"]
        stage = stage if stage in ladder else "initial_outreach"
        if intent in ("interested", "question"):
            return ladder[min(ladder.index(stage) + 1, len(ladder) - 1)]
        if intent == "objection" and ladder.index(stage) < 2:
            return "qualifying"
        return stage

    async def _company_domain(self, company_id: str) -> str:
        if not company_id:
            return ""
        company = await self.state.get_company(company_id)
        return (company.domain if company else "") or ""

    def _product_vars(self) -> dict:
        p, persona = self.config.product, self.config.persona
        return {
            "persona_name": persona.name, "persona_role": persona.role,
            "persona_company": persona.company, "persona_tone": persona.tone,
            "product_name": p.name, "product_description": p.description,
            "product_benefits": "\n".join(f"  - {b}" for b in p.key_benefits),
            "product_pricing": p.pricing,
        }

    def _skills(self) -> str:
        if not hasattr(self.brain, "load_skill"):
            return ""
        parts = [self.brain.load_skill(n) for n in
                 ("messenger_outreach_ru", "objection_handling", "product_knowledge")]
        parts = [x for x in parts if x]
        return ("\n\n---\nЗНАНИЯ И ПРАВИЛА\n\n" + "\n\n---\n\n".join(parts)) if parts else ""

    async def _draft_reply(self, thread, text: str) -> dict:
        offer = self.config.product.offer
        prompt = self.brain.load_prompt(
            "messenger_handler", channel=CHANNEL_LABELS.get(thread["channel"], thread["channel"]),
            stage=thread.get("stage") or "initial_outreach",
            offer_goal=GOALS.get(offer.goal, offer.goal),
            booking=f" (ссылка для записи: {offer.booking_url})" if offer.booking_url else "",
            **self._product_vars())
        history = await self.store.history(thread["id"])
        lines = []
        for m in history:
            who = "Мы" if m["direction"] == "out" else (thread.get("display_name") or "Собеседник")
            lines.append(f"[{who}]\n{m['body']}")
        prompt += self._skills()
        prompt += f"""

Переписка (последнее внизу):
{fence(chr(10).join(lines) or text, "the conversation")}

{UNTRUSTED_RULE}"""
        result = await self.brain.think_json(prompt, session_id="harvey-messengers",
                                             agent="messengers", task="reply")
        if not isinstance(result, dict):
            paused = getattr(self.brain, "paused_reason", "")
            if paused:
                raise BrainPaused(paused)
            return {}
        intent = str(result.get("intent", "")).strip().lower()
        return {"intent": intent, "reply": str(result.get("reply") or "")}

    # ── 3. Send ──

    async def send_approved(self) -> int:
        sent = 0
        paused = await self.state.get_setting("sending_paused")
        if paused:
            return 0   # the one kill switch stops messengers too
        for msg in await self.store.approved_to_send():
            bot = self.bots.get(msg["channel"])
            if msg["thread_status"] == "opted_out":
                await self.store.reject_message(msg["id"])
                continue
            if bot is None:
                await self.store.mark_message_failed(
                    msg["id"], f"{CHANNEL_LABELS.get(msg['channel'])} не подключён")
                continue
            if isinstance(bot, WhatsAppCloud) and not self._in_whatsapp_window(msg["last_inbound_at"]):
                await self.store.mark_message_failed(
                    msg["id"], "WhatsApp: прошло больше 24 часов после их последнего сообщения, "
                               "без одобренного шаблона Meta писать нельзя")
                continue
            domain = await self._company_domain(
                (await self.store.get_thread(msg["thread_id"]) or {}).get("company_id", ""))
            blocked = gate.problems(msg["body"], self.config, kind="reply", company_domain=domain)
            if blocked:
                await self.store.mark_message_failed(msg["id"], "; ".join(blocked))
                continue
            result = await bot.send(msg["chat_id"], msg["body"])
            if result.ok:
                await self.store.mark_message_sent(msg["id"], result.provider_msg_id)
                sent += 1
            else:
                await self.store.mark_message_failed(msg["id"], result.error)
            if self.send_pacing:
                import random
                await asyncio.sleep(random.uniform(*SEND_GAP_SECONDS))
        return sent

    @staticmethod
    def _in_whatsapp_window(last_inbound_at) -> bool:
        if not last_inbound_at:
            return False
        try:
            last = datetime.fromisoformat(str(last_inbound_at))
        except ValueError:
            return False
        return _now() - last < timedelta(hours=WhatsAppCloud.WINDOW_HOURS)

    # ── 5. First messages ──

    async def _facts(self, company_id: str) -> list[str]:
        rows = await self.store._rows(
            """SELECT o.signal_code, COALESCE(sc.label, o.signal_code) AS label,
                      o.value_num, o.value_text, o.observed_at
               FROM observations o LEFT JOIN signal_codes sc ON sc.code = o.signal_code
               WHERE o.company_id = ? ORDER BY o.observed_at DESC, o.rowid DESC""",
            (company_id,))
        seen, out = set(), []
        for r in rows:
            code = r["signal_code"]
            if code in seen or code.startswith(_NOT_FACTS):
                continue
            seen.add(code)
            if r["value_text"]:
                out.append(f"- {r['label']}: {r['value_text']}")
            elif r["value_num"] not in (None, 0):
                out.append(f"- {r['label']}: {r['value_num']:g}" if r["value_num"] != 1
                           else f"- {r['label']}: да")
        return out[:12]

    def _pick(self, contacts: dict) -> tuple[str, str]:
        for channel in self.cfg.channel_order:
            if contacts.get(channel):
                return channel, contacts[channel][0]["handle"]
        return "", ""

    def _with_opt_out(self, text: str) -> str:
        text = (text or "").strip()
        line = self.cfg.opt_out_line
        return f"{text}\n\n{line}" if text and line else text

    async def draft_first_messages(self) -> int:
        if not self.cfg.cold_drafts:
            return 0
        room = self.cfg.max_daily_drafts - await self.store.drafted_since(_utc_midnight())
        if room <= 0:
            return 0
        drafted = 0
        dnc = self.config.compliance.do_not_contact
        for company in await self.store.companies_to_draft(room, self.cfg.skip_if_emailed):
            channel, handle = self._pick(await self.store.contacts_for(company["id"]))
            if not channel:
                continue
            domain = company.get("domain") or ""
            if domain and dnc and gate.do_not_contact_match(f"x@{domain}", dnc):
                await self.store.add_cold(company["id"], channel, handle, "", status="skipped",
                                          error="в списке do-not-contact")
                continue
            draft = await self._write_first(company, channel)
            if draft is None:
                break   # the model is unavailable: try again next cycle
            message = draft.get("message", "").strip()
            if not message:
                await self.store.add_cold(company["id"], channel, handle, "", status="skipped",
                                          error="модель не написала сообщение")
                continue
            followup = draft.get("followup", "").strip() if self.cfg.followup_days > 0 else ""
            body = self._with_opt_out(message)
            issues = gate.problems(body, self.config, kind="cold", company_domain=domain)
            if await self.store.add_cold(company["id"], channel, handle, body,
                                         self._with_opt_out(followup) if followup else "",
                                         error="; ".join(issues)):
                drafted += 1
        return drafted

    async def _write_first(self, company: dict, channel: str) -> dict | None:
        """{"message", "followup"}, {} when the model wrote nothing usable,
        None when the model can't be reached."""
        where = company.get("location") or ""
        facts = await self._facts(company["id"])
        block = "\n".join([
            f"Название: {company.get('name') or company.get('domain') or 'неизвестно'}",
            f"Где: {where or 'неизвестно'}",
            f"Сайт: {company.get('website') or company.get('domain') or 'нет'}",
            "Факты:", *(facts or ["- ничего конкретного"]),
        ])
        prompt = self.brain.load_prompt(
            "messenger_writer", channel=CHANNEL_LABELS.get(channel, channel),
            company_block=fence(block, "the business"), **self._product_vars())
        prompt += self._skills()
        prompt += "\n\n" + UNTRUSTED_RULE
        result = await self.brain.think_json(prompt, session_id="harvey-messengers",
                                             agent="messengers", task="first_message")
        if not isinstance(result, dict):
            if getattr(self.brain, "paused_reason", ""):
                return None
            return {}
        return {"message": str(result.get("message") or ""),
                "followup": str(result.get("followup") or "")}


class BrainPaused(Exception):
    """The model can't be reached: leave messages for the next cycle."""
