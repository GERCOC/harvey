"""The messenger tables (state.py migration v15), read and written here.

Kept apart from StateManager so the email code paths stay exactly as they
were; it opens connections the same way (StateManager._connect).
"""

import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from harvey.messengers.contacts import CHANNEL_FOR, SIGNAL_FOR

# First-message statuses that still lead somewhere.
COLD_OPEN = ("waiting", "scheduled", "pending_review")
# A thread in one of these gets nothing more from Harvey.
THREAD_STOPPED = ("opted_out", "closed")


def _now() -> datetime:
    """Naive UTC, as every table here stores times. Tests pin it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _id() -> str:
    return uuid.uuid4().hex[:12]


class MessengerStore:
    def __init__(self, state):
        self.state = state

    async def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def _run(self, sql: str, params: tuple = ()) -> int:
        async with self.state._connect() as db:
            cur = await db.execute(sql, params)
            await db.commit()
            return cur.rowcount

    # ── Contacts the profile stage found ──

    async def contacts_for(self, company_id: str) -> dict[str, list[dict]]:
        """{channel: [{"handle", "evidence_url"}]} from the company's latest
        profile reading of each messenger: an account the site stopped
        linking to is no longer offered."""
        out: dict[str, list[dict]] = {}
        for channel, code in SIGNAL_FOR.items():
            rows = await self._rows(
                """SELECT value_text, evidence_url, run_id FROM observations
                   WHERE company_id = ? AND signal_code = ? AND value_text != ''
                   ORDER BY observed_at DESC, rowid DESC""",
                (company_id, code))
            if not rows:
                continue
            latest = rows[0]["run_id"]
            seen = []
            for r in rows:
                if r["run_id"] != latest:
                    break
                if r["value_text"] not in [s["handle"] for s in seen]:
                    seen.append({"handle": r["value_text"], "evidence_url": r["evidence_url"]})
            out[channel] = list(reversed(seen))   # the order the page gave
        return out

    async def companies_to_draft(self, limit: int, skip_if_emailed: bool = True) -> list[dict]:
        """Companies with a messenger contact and no first message yet, newest
        contact first. Businesses already emailed are left out unless asked."""
        codes = tuple(CHANNEL_FOR)
        ph = ",".join("?" for _ in codes)
        emailed = (
            """AND NOT EXISTS (
                   SELECT 1 FROM prospects p WHERE p.company_id = c.id
                   AND p.status NOT IN ('new', 'queued', 'write_failed'))
               AND NOT EXISTS (
                   SELECT 1 FROM outbox o JOIN prospects p ON p.id = o.prospect_id
                   WHERE p.company_id = c.id AND o.status IN ('sent', 'sending'))"""
            if skip_if_emailed else "")
        return await self._rows(
            f"""SELECT c.*, MAX(o.observed_at) AS contact_seen FROM companies c
                JOIN observations o ON o.company_id = c.id
                WHERE o.signal_code IN ({ph}) AND o.value_text != ''
                  AND NOT EXISTS (SELECT 1 FROM messenger_outbox m WHERE m.company_id = c.id)
                  AND NOT EXISTS (SELECT 1 FROM messenger_threads t
                                  WHERE t.company_id = c.id AND t.status = 'opted_out')
                  {emailed}
                GROUP BY c.id ORDER BY contact_seen DESC LIMIT ?""",
            (*codes, int(limit)))

    # ── First messages (sent by a person, from their own app) ──

    async def add_cold(self, company_id: str, channel: str, handle: str,
                       body: str, followup: str = "", status: str = "pending_review",
                       error: str = "") -> str | None:
        """Queue a first message (and its follow-up, waiting until the first
        is sent). None if the business already has one."""
        first = _id()
        async with self.state._connect() as db:
            cur = await db.execute(
                """INSERT OR IGNORE INTO messenger_outbox
                   (id, company_id, channel, handle, step, body, status, error)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
                (first, company_id, channel, handle, body, status, error))
            if not cur.rowcount:
                return None
            if followup:
                await db.execute(
                    """INSERT OR IGNORE INTO messenger_outbox
                       (id, company_id, channel, handle, step, body, status)
                       VALUES (?, ?, ?, ?, 2, ?, 'waiting')""",
                    (_id(), company_id, channel, handle, followup))
            await db.commit()
        return first

    async def drafted_since(self, since: datetime) -> int:
        rows = await self._rows(
            "SELECT COUNT(*) AS n FROM messenger_outbox WHERE step = 1 AND created_at >= ?",
            (_ts(since),))
        return rows[0]["n"] if rows else 0

    async def sent_since(self, since: datetime) -> int:
        rows = await self._rows(
            "SELECT COUNT(*) AS n FROM messenger_outbox WHERE status = 'sent' AND sent_at >= ?",
            (_ts(since),))
        return rows[0]["n"] if rows else 0

    async def get_cold(self, item_id: str) -> dict | None:
        rows = await self._rows("SELECT * FROM messenger_outbox WHERE id = ?", (item_id,))
        return rows[0] if rows else None

    async def list_cold(self, statuses: tuple[str, ...], limit: int = 100,
                        newest_first: bool = False) -> list[dict]:
        ph = ",".join("?" for _ in statuses)
        order = "COALESCE(m.sent_at, m.updated_at) DESC" if newest_first else "m.step, m.created_at"
        return await self._rows(
            f"""SELECT m.*, c.name AS company_name, c.domain AS company_domain,
                       c.website AS company_website, c.location AS company_location
                FROM messenger_outbox m LEFT JOIN companies c ON c.id = m.company_id
                WHERE m.status IN ({ph}) ORDER BY {order} LIMIT ?""",
            (*statuses, int(limit)))

    async def edit_cold(self, item_id: str, body: str) -> bool:
        return bool(await self._run(
            """UPDATE messenger_outbox SET body = ?, error = '', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status IN ('pending_review', 'waiting', 'scheduled')""",
            (body, item_id)))

    async def mark_cold_sent(self, item_id: str, followup_days: int) -> bool:
        """You sent it. Its follow-up (if any) is due ``followup_days`` later."""
        now = _now()
        async with self.state._connect() as db:
            cur = await db.execute(
                """UPDATE messenger_outbox SET status = 'sent', sent_at = ?, error = '',
                          updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = 'pending_review'""",
                (_ts(now), item_id))
            if not cur.rowcount:
                return False
            async with db.execute(
                    "SELECT company_id, step FROM messenger_outbox WHERE id = ?", (item_id,)) as c:
                company_id, step = await c.fetchone()
            if step == 1:
                if followup_days > 0:
                    await db.execute(
                        """UPDATE messenger_outbox SET status = 'scheduled', send_at = ?,
                                  updated_at = CURRENT_TIMESTAMP
                           WHERE company_id = ? AND step = 2 AND status = 'waiting'""",
                        (_ts(now + timedelta(days=followup_days)), company_id))
                else:
                    await db.execute(
                        """UPDATE messenger_outbox SET status = 'cancelled',
                                  updated_at = CURRENT_TIMESTAMP
                           WHERE company_id = ? AND step = 2 AND status = 'waiting'""",
                        (company_id,))
            await db.commit()
        return True

    async def skip_cold(self, item_id: str) -> bool:
        """Not sending it. Skipping a first message drops its follow-up too."""
        item = await self.get_cold(item_id)
        if not item or item["status"] not in COLD_OPEN:
            return False
        await self._run(
            "UPDATE messenger_outbox SET status = 'skipped', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (item_id,))
        if item["step"] == 1:
            await self.cancel_cold_for(item["company_id"])
        return True

    async def cancel_cold_for(self, company_id: str) -> int:
        """Everything still to send to this business, stopped."""
        if not company_id:
            return 0
        ph = ",".join("?" for _ in COLD_OPEN)
        return await self._run(
            f"""UPDATE messenger_outbox SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE company_id = ? AND status IN ({ph})""",
            (company_id, *COLD_OPEN))

    async def release_due(self) -> int:
        """Follow-ups whose day has come move to the queue."""
        return await self._run(
            """UPDATE messenger_outbox SET status = 'pending_review', updated_at = CURRENT_TIMESTAMP
               WHERE status = 'scheduled' AND send_at <= ?""",
            (_ts(_now()),))

    # ── Conversations ──

    async def get_thread(self, thread_id: str) -> dict | None:
        rows = await self._rows("SELECT * FROM messenger_threads WHERE id = ?", (thread_id,))
        return rows[0] if rows else None

    async def thread_for(self, channel: str, transport: str, chat_id: str, *,
                         handle: str = "", display_name: str = "",
                         company_id: str = "") -> dict:
        """The thread for this chat, created on first contact."""
        rows = await self._rows(
            "SELECT * FROM messenger_threads WHERE channel = ? AND transport = ? AND chat_id = ?",
            (channel, transport, chat_id))
        if rows:
            thread = rows[0]
            updates = {k: v for k, v in (("handle", handle), ("display_name", display_name),
                                         ("company_id", company_id))
                       if v and not thread.get(k)}
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                await self._run(
                    f"UPDATE messenger_threads SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (*updates.values(), thread["id"]))
                thread.update(updates)
            return thread
        tid = _id()
        await self._run(
            """INSERT OR IGNORE INTO messenger_threads
               (id, channel, transport, chat_id, handle, display_name, company_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tid, channel, transport, chat_id, handle, display_name, company_id))
        return await self.thread_for(channel, transport, chat_id)

    async def add_inbound(self, thread_id: str, body: str, provider_msg_id: str = "",
                          at: datetime | None = None) -> bool:
        """Record a message they sent. False if it was already recorded."""
        at = at or _now()
        async with self.state._connect() as db:
            cur = await db.execute(
                """INSERT OR IGNORE INTO messenger_messages
                   (id, thread_id, direction, body, status, provider_msg_id, created_at)
                   VALUES (?, ?, 'in', ?, 'received', ?, ?)""",
                (_id(), thread_id, body, provider_msg_id, _ts(at)))
            if not cur.rowcount:
                return False
            # A closed conversation they write in again opens again; an
            # opt-out stays one.
            await db.execute(
                """UPDATE messenger_threads SET last_inbound_at = ?, updated_at = CURRENT_TIMESTAMP,
                          status = CASE WHEN status = 'closed' THEN 'open' ELSE status END
                   WHERE id = ?""",
                (_ts(at), thread_id))
            await db.commit()
        return True

    async def threads_to_answer(self, limit: int = 20) -> list[dict]:
        """Threads holding a message nobody has handled yet."""
        return await self._rows(
            """SELECT t.* FROM messenger_threads t
               WHERE EXISTS (SELECT 1 FROM messenger_messages m WHERE m.thread_id = t.id
                             AND m.direction = 'in' AND m.status = 'received')
               ORDER BY t.last_inbound_at LIMIT ?""",
            (int(limit),))

    async def mark_inbound_handled(self, thread_id: str) -> None:
        await self._run(
            """UPDATE messenger_messages SET status = 'handled', updated_at = CURRENT_TIMESTAMP
               WHERE thread_id = ? AND direction = 'in' AND status = 'received'""",
            (thread_id,))

    async def history(self, thread_id: str, limit: int = 40) -> list[dict]:
        """The conversation, oldest first: what they wrote and what was sent
        to them (drafts nobody sent left out)."""
        rows = await self._rows(
            """SELECT * FROM messenger_messages WHERE thread_id = ?
               AND (direction = 'in' OR status = 'sent')
               ORDER BY created_at DESC, rowid DESC LIMIT ?""",
            (thread_id, int(limit)))
        return list(reversed(rows))

    async def add_outbound(self, thread_id: str, body: str, status: str,
                           approved_by: str = "", error: str = "") -> str:
        mid = _id()
        await self._run(
            """INSERT INTO messenger_messages
               (id, thread_id, direction, body, status, approved_by, error)
               VALUES (?, ?, 'out', ?, ?, ?, ?)""",
            (mid, thread_id, body, status, approved_by, error))
        return mid

    async def get_message(self, message_id: str) -> dict | None:
        rows = await self._rows(
            """SELECT m.*, t.channel, t.transport, t.chat_id, t.status AS thread_status,
                      t.last_inbound_at
               FROM messenger_messages m JOIN messenger_threads t ON t.id = m.thread_id
               WHERE m.id = ?""", (message_id,))
        return rows[0] if rows else None

    async def approve_message(self, message_id: str, by: str = "you") -> bool:
        return bool(await self._run(
            """UPDATE messenger_messages SET status = 'approved', approved_by = ?,
                      updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND direction = 'out' AND status IN ('pending_review', 'held', 'failed')
                 AND thread_id NOT IN (SELECT id FROM messenger_threads WHERE status = 'opted_out')""",
            (by, message_id)))

    async def reject_message(self, message_id: str) -> bool:
        return bool(await self._run(
            """UPDATE messenger_messages SET status = 'rejected', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND direction = 'out'
                 AND status IN ('pending_review', 'held', 'approved', 'failed')""",
            (message_id,)))

    async def edit_message(self, message_id: str, body: str) -> bool:
        """Rewrite a draft. Never approves it."""
        return bool(await self._run(
            """UPDATE messenger_messages SET body = ?, error = '', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND direction = 'out' AND status IN ('pending_review', 'held', 'failed')""",
            (body, message_id)))

    async def mark_message_sent(self, message_id: str, provider_msg_id: str = "") -> bool:
        return bool(await self._run(
            """UPDATE messenger_messages SET status = 'sent', sent_at = ?, provider_msg_id = ?,
                      error = '', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND direction = 'out'""",
            (_ts(_now()), provider_msg_id, message_id)))

    async def mark_message_failed(self, message_id: str, error: str) -> None:
        await self._run(
            """UPDATE messenger_messages SET status = 'failed', error = ?,
                      updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND direction = 'out'""",
            (error[:500], message_id))

    async def approved_to_send(self, limit: int = 20) -> list[dict]:
        """Approved answers in bot conversations, oldest first."""
        return await self._rows(
            """SELECT m.*, t.channel, t.chat_id, t.last_inbound_at, t.status AS thread_status
               FROM messenger_messages m JOIN messenger_threads t ON t.id = m.thread_id
               WHERE m.direction = 'out' AND m.status = 'approved' AND t.transport = 'bot'
               ORDER BY m.created_at LIMIT ?""",
            (int(limit),))

    async def requeue_autopilot(self) -> int:
        """Approval switched back on: what autopilot approved and hasn't sent
        comes back to you."""
        return await self._run(
            """UPDATE messenger_messages SET status = 'pending_review', approved_by = '',
                      updated_at = CURRENT_TIMESTAMP
               WHERE direction = 'out' AND status = 'approved' AND approved_by = 'autopilot'""")

    async def cancel_outbound(self, thread_id: str) -> int:
        return await self._run(
            """UPDATE messenger_messages SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
               WHERE thread_id = ? AND direction = 'out'
                 AND status IN ('pending_review', 'held', 'approved', 'failed')""",
            (thread_id,))

    async def set_thread(self, thread_id: str, **fields) -> None:
        allowed = {"status", "status_reason", "intent", "stage", "company_id", "handle"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self._run(
            f"UPDATE messenger_threads SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (*fields.values(), thread_id))

    async def list_threads(self, limit: int = 100) -> list[dict]:
        """Every conversation, newest first, with its messages and any draft."""
        threads = await self._rows(
            """SELECT t.*, c.name AS company_name FROM messenger_threads t
               LEFT JOIN companies c ON c.id = t.company_id
               ORDER BY COALESCE(t.last_inbound_at, t.updated_at) DESC LIMIT ?""",
            (int(limit),))
        for t in threads:
            msgs = await self._rows(
                """SELECT * FROM messenger_messages WHERE thread_id = ?
                   ORDER BY created_at, rowid""", (t["id"],))
            t["messages"] = [m for m in msgs
                             if m["direction"] == "in" or m["status"] == "sent"]
            t["drafts"] = [m for m in msgs if m["direction"] == "out"
                           and m["status"] in ("pending_review", "held", "approved", "failed")]
        return threads
