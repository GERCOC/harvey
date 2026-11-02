"""`harvey messengers`: status, a token check, and the WhatsApp webhook."""

import asyncio


def cmd_messengers(args):
    action = getattr(args, "messengers_action", "status")
    if action == "whatsapp-webhook":
        from harvey.messengers.whatsapp_webhook import serve
        serve(host=args.host, port=args.port)
        return

    from harvey.config import load_config, load_env
    from harvey.messengers.bots import configured_bots, missing_keys
    from harvey.messengers.contacts import CHANNEL_LABELS
    from harvey.messengers.store import MessengerStore
    from harvey.state import StateManager

    config = load_config()
    env = load_env()
    cfg = config.channels.messengers

    if action == "login":
        from harvey.messengers.personal import login as personal_login

        async def _login():
            state = StateManager()
            await state.init_db()
            store = MessengerStore(state)
            channel = args.channel
            pcfg = cfg.personal
            if not (pcfg.enabled and pcfg.confirm_risk):
                print(f"\n  Personal sending is off. Set channels.messengers.personal.enabled: true")
                print("  and confirm_risk: true first — you accept that your number can be"
                      " limited or banned (docs/messengers.md).\n")
                return
            await store.set_personal_account(channel, status="waiting_for_qr")
            print(f"\n  Logging {channel} in with your own account. Scan the QR code,"
                  " it stays signed in on this machine.\n")
            ok = await personal_login(channel)
            await store.set_personal_account(
                channel, status="logged_in" if ok else "logged_out",
                last_error="" if ok else "the QR was never scanned")
            print(f"\n  {'Signed in.' if ok else 'Not signed in.'}\n")

        asyncio.run(_login())
        return

    async def _run():
        state = StateManager()
        await state.init_db()
        store = MessengerStore(state)
        print()
        if not cfg.enabled:
            print("  Messengers are off. Set channels.messengers.enabled: true in your config")
            print("  (docs/messengers.md), and confirm MESSENGER_TELEGRAM, MESSENGER_WHATSAPP")
            print("  and MESSENGER_VK with 'harvey signals --confirm ...'.\n")
        for problem in missing_keys(config, env):
            print(f"  ! {problem}")
        bots = configured_bots(config, env)
        if action == "test":
            if not bots:
                print("  No bot is turned on with its key. Nothing to test.\n")
            for channel, bot in bots.items():
                ok, detail = await bot.test()
                print(f"  {CHANNEL_LABELS[channel]:<10} {'OK  ' if ok else 'FAIL'} {detail}")
            print()
            return
        queue = await store.list_cold(("pending_review",))
        approved = await store.list_cold(("approved",))
        scheduled = await store.list_cold(("scheduled",))
        threads = await store.list_threads()
        waiting = [t for t in threads if t["status"] == "needs_human"
                   or any(d["status"] != "approved" for d in t["drafts"])]
        print(f"  First messages waiting for you: {len(queue)}  (follow-ups scheduled: {len(scheduled)})")
        print(f"  Conversations: {len(threads)}  (need you: {len(waiting)})")
        print(f"  Bots on: {', '.join(CHANNEL_LABELS[c] for c in bots) or 'none'}")
        print(f"  Answers: {'wait for approval' if cfg.require_approval else 'autopilot'}")
        pcfg = cfg.personal
        accounts = await store.personal_accounts()
        if pcfg.enabled and pcfg.confirm_risk:
            states = ", ".join(f"{c}: {accounts.get(c, {}).get('status', 'logged_out')}"
                               for c in ("telegram", "whatsapp"))
            print(f"  Personal sending: ON ({states}); approved first messages go out"
                  f" automatically, max {pcfg.max_sends_per_day}/day.")
            print(f"  Waiting to be sent by Harvey: {len(approved)}")
        elif pcfg.enabled:
            print("  ! personal.enabled is true but confirm_risk is false: nothing checks or sends.")
        else:
            print("  Personal sending: off (contacts are checked only while it's on).")
        print("\n  Send first messages and approve answers on the dashboard's Messengers tab.\n")

    asyncio.run(_run())


def add_parser(subparsers):
    sub = subparsers.add_parser(
        "messengers", help="Telegram, WhatsApp and VK: status, token check, WhatsApp webhook")
    sub.add_argument("messengers_action", nargs="?", default="status",
                     choices=["status", "test", "whatsapp-webhook", "login"],
                     help="status (default), test the bot tokens, run the WhatsApp webhook,"
                          " or sign a personal account in with a QR code")
    sub.add_argument("channel", nargs="?", default="", choices=["", "telegram", "whatsapp"],
                     help="login: which personal account to sign in")
    sub.add_argument("--host", default="127.0.0.1",
                     help="whatsapp-webhook: address to listen on (default 127.0.0.1, behind a proxy)")
    sub.add_argument("--port", type=int, default=8787, help="whatsapp-webhook: port (default 8787)")
    sub.set_defaults(func=cmd_messengers)
