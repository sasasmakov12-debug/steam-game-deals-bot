# Steam deals Telegram bot

The bot checks Steam's featured categories every 30 minutes and posts discounted games that also appear in Steam's top sellers. It includes temporary free promotions for paid games when Steam reports them as a 100% discount. The SQLite database prevents the same game from being posted twice.

The Steam Store endpoint does not expose a definitive "counts toward the Game Collector badge" field. This bot uses a practical filter: it excludes games with no original price, and includes paid titles discounted to zero. This is an approximation of Steam's badge eligibility, not a guarantee for every title or account.

## Run locally

1. Install Python 3.11 or newer.
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`, then set the Telegram bot token and channel ID. The bot must be an administrator of the channel with permission to post.
4. Optionally set `ADMIN_USER_IDS` to comma-separated Telegram user IDs allowed to use `/check`.
5. Run `python bot.py`. The bot loads settings from `.env` in the current directory. Use `python bot.py --once` to perform a single check.

## Host on GitHub Actions

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather), add it as an administrator to your channel, and allow it to post messages.
2. In the GitHub repository, open **Settings → Secrets and variables → Actions** and add these repository secrets:
   - `TELEGRAM_BOT_TOKEN`: the bot token from BotFather.
   - `TELEGRAM_CHANNEL_ID`: the channel username such as `@your_channel`, or its numeric chat ID.
3. In **Settings → Actions → General → Workflow permissions**, allow read and write access. The workflow needs this to save its SQLite publication history and prevent duplicate posts.
4. Enable Actions for the repository. The scheduled workflow checks every 30 minutes; you can also start it from **Actions → Publish Steam deals → Run workflow**.

GitHub Actions runs each check on a fresh runner. The workflow commits the small SQLite publication-history file to the private repository after a run, so later runs remember which games were already posted. GitHub's scheduled workflows may start later than the requested time during periods of high load.

The bot also supports `/start` and an administrator-only `/check` when run in polling mode locally.
