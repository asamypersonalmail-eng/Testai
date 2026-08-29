# Telegram n8n Workflow

An n8n workflow that starts from an incoming Telegram message (via a Telegram
Trigger node) and replies through your bot:

- `/start` → sends a welcome message
- any other message → echoes the text back

## Files

- `workflows/telegram-bot-workflow.json` — the n8n workflow export. Import it
  via n8n's UI: **Workflows → Import from File**.

## Setting up the Telegram credential (do this in n8n, not in git)

The workflow references a credential named `Telegram Bot - Testai` instead of
embedding the bot token directly. This keeps the token out of source control,
since anything committed to this repo is visible to everyone with repo
access and stays in git history forever.

1. In n8n, go to **Credentials → New → Telegram API**.
2. Name it `Telegram Bot - Testai` (or update the workflow's node credentials
   to match whatever name you choose).
3. Paste your bot token (the one you got from **@BotFather**) into the
   **Access Token** field and save.
4. Open the imported workflow, select the **Telegram Trigger** and **Send
   Telegram Reply** nodes, and confirm they point at that credential.
5. Activate the workflow. n8n will register the Telegram webhook
   automatically once it's active.

## Local development

If you run any companion scripts locally, copy `.env.example` to `.env` and
put your token there — `.env` is git-ignored so it never gets committed.

```bash
cp .env.example .env
# then edit .env and set TELEGRAM_BOT_TOKEN
```
