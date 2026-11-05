# Test account setup

Assignment rule: dedicated temporary accounts with fictional identities and synthetic
data only. Never connect a personal or USC mailbox, calendar or Notion workspace.

Secrets go in `~/.a3-kairo/kairo.env` (outside the repo):

```bash
cp .env.example ~/.a3-kairo/kairo.env && chmod 600 ~/.a3-kairo/kairo.env
```

## 1. Gmail test account

1. Create a new Google account with the fictional name "Emily Kim".
2. Turn on 2-Step Verification (required for app passwords).
3. Create an app password named `Kairo` at <https://myaccount.google.com/apppasswords>.
4. Fill `KAIRO_MAIL_USER` and `KAIRO_MAIL_APP_PASSWORD` (paste the 16 characters without spaces).

## 2. Test calendar

1. In the same account, open Google Calendar and create a calendar named `Kairo Test`
   with time zone America/Los_Angeles.
2. Settings → Settings for my calendars → `Kairo Test` → Integrate calendar →
   **Secret address in iCal format**.
3. Fill `KAIRO_CALENDAR_ICS_URL`.

## 3. Notion workspace

1. Sign up to Notion with the test Gmail address. Create a page named `Kairo`.
2. Create an internal integration named `Kairo` at <https://www.notion.so/profile/integrations>
   with Read, Update and Insert content capabilities. Copy its secret into `KAIRO_NOTION_TOKEN`.
3. On the `Kairo` page: ⋯ → Connections → add `Kairo`.
4. Copy the 32-character page ID from the page URL into `KAIRO_NOTION_PARENT_PAGE_ID`.

## 4. Discord bot

1. Create a private Discord server named `Kairo` (Create My Own → For me and my friends)
   with a text channel `#kairo`.
2. At <https://discord.com/developers/applications>: New Application `Kairo` → **Bot** →
   Reset Token, and put it in `DISCORD_BOT_TOKEN`. On the same page turn **off** Public Bot
   and turn **on** Message Content Intent.
3. **OAuth2 → URL Generator**: scope `bot`; permissions View Channels, Send Messages,
   Read Message History, Embed Links, Add Reactions. Open the generated URL and add the bot
   to the `Kairo` server only.
4. User Settings → Advanced → turn on Developer Mode. Right-click your name → Copy User ID
   into `DISCORD_ALLOWED_USERS`; right-click `#kairo` → Copy Channel ID into
   `DISCORD_HOME_CHANNEL`. IDs are not secrets, but the bot token is.

## 5. Tailscale (needed for the cloud step)

Install the macOS app from <https://tailscale.com/download>, sign in, and confirm:

```bash
tailscale ip -4
```

## Never paste into chat or commit

App password, ICS URL, Notion secret, Discord bot token, Tailscale auth key, or any login URL.
