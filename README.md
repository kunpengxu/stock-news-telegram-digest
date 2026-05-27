# Newsfilter Telegram Digest

This project fetches the public Highlights section from <https://newsfilter.io/>, summarizes and translates the market highlights into concise Chinese with Gemini, groups them by sector, and sends the result to a Telegram chat.

It is designed for broad market monitoring, not watchlist-only stock analysis.

## Files

- `main.py` - scraper, Gemini summarizer, Telegram sender
- `requirements.txt` - Python dependencies
- `.github/workflows/daily.yml` - weekday GitHub Actions schedule
- `tests/test_parser.py` - parser smoke test using mocked HTML

## Required Secrets

Set these in GitHub: **Settings > Secrets and variables > Actions > New repository secret**.

- `GEMINI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional variables:

- `GEMINI_MODEL` defaults to `gemini-1.5-flash`
- `MAX_BULLETS_PER_SECTOR` defaults to `6`
- `DRY_RUN` defaults to `false`

## Telegram Setup

1. In Telegram, open `@BotFather`.
2. Send `/newbot` and follow the prompts.
3. Copy the bot token into the `TELEGRAM_BOT_TOKEN` GitHub secret.
4. Start a chat with your bot and send any message.
5. Visit this URL in a browser, replacing `<TOKEN>`:

   ```text
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

6. Find `chat.id` in the JSON response and save it as `TELEGRAM_CHAT_ID`.

For group chats, add the bot to the group, send a message in the group, then use `getUpdates` the same way.

## Gemini API Key

1. Open Google AI Studio: <https://aistudio.google.com/app/apikey>
2. Create an API key.
3. Save it as the GitHub secret `GEMINI_API_KEY`.

The project uses the official `google-genai` SDK.

## GitHub Actions

The workflow runs every day at `01:00 UTC` and `10:00 UTC`, which correspond to `09:00` and `18:00` Beijing time. It also supports manual runs.

To run manually:

1. Go to **Actions**.
2. Select **Daily Newsfilter Telegram Digest**.
3. Click **Run workflow**.

To adjust the time, edit the cron in `.github/workflows/daily.yml`:

```yaml
schedule:
  - cron: "0 1,10 * * *"
```

GitHub cron schedules use UTC, and scheduled runs may start a few minutes late depending on GitHub Actions queueing.

## Local Run

Use Python 3.11.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
export GEMINI_API_KEY="..."
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python main.py
```

Use dry run mode to print the final message instead of sending Telegram:

```bash
DRY_RUN=true python main.py
```

## Playwright Notes

The script first tries static `requests` + `BeautifulSoup` parsing. If the Highlights content is rendered dynamically, it falls back to Playwright.

Local install:

```bash
python -m playwright install chromium
```

GitHub Actions install:

```bash
python -m playwright install --with-deps chromium
```

## Parser Test

Run the mocked parser test without depending on the live website:

```bash
python -m unittest discover -s tests
```
