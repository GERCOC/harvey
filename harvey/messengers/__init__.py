"""Messengers: Telegram, VK and WhatsApp.

Three parts, each usable on its own:

- ``contacts``: the messenger contacts a business publishes on its own site
  (t.me, wa.me, vk.com links), collected by the profile stage as signals.
- ``agent`` + ``store``: a first message for each such business, written by
  the model and waiting in the dashboard. A person sends it from their own
  app with one click: Harvey never sends a cold message itself.
- ``bots``: Telegram bot, VK community and WhatsApp Business conversations a
  person started. Harvey reads them, drafts answers, and sends the ones you
  approve (or all of them, with ``messengers.require_approval: false``).
"""
