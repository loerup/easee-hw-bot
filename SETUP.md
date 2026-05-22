# CAD-BOT — Setup Guide

## What it does

CAD-BOT manages check-in/check-out of CAD files between **Onshape** (Engineering source of truth) and **Fusion 360** (Research team). It tracks which parts are currently being worked on outside Onshape, by whom, and since when.

**Trigger:** `@CAD-BOT` in any Slack message

| Command | What it does |
|---|---|
| `@CAD-BOT checkout <part> [Onshape link]` | Reserve a part for external editing |
| `@CAD-BOT checkin <part> [notes]` + attach file | Return a modified part |
| `@CAD-BOT status` | Show all currently checked-out parts |
| `@CAD-BOT help` | Show the workflow guide |

---

## Step 1 — Create the Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App → From scratch**
2. Name: `CAD-BOT` — Workspace: **Easee Norway**
3. **OAuth & Permissions → Bot Token Scopes** — add:
   - `app_mentions:read`
   - `channels:history`
   - `chat:write`
   - `reactions:write`
   - `reactions:read`
   - `users:read` *(so the bot can resolve real names)*
   - `files:read` *(so the bot can see attached files on check-in)*
4. **Event Subscriptions → Subscribe to bot events:** `app_mention`
   - Request URL: `https://YOUR-RAILWAY-URL/slack/events` *(fill in after Step 3)*
5. **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-...`)
6. **Basic Information** → copy the **Signing Secret**

---

## Step 2 — Deploy to Railway

### Option A — GitHub (recommended)

1. Push the `easee-hw-bot` folder to a GitHub repo
2. Sign up at [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**
3. Select your repo

### Option B — Railway CLI

```bash
npm install -g @railway/cli
railway login
cd easee-hw-bot
railway init
railway up
```

### Add environment variables in Railway

Go to your project → **Variables** and add:

```
SLACK_BOT_TOKEN       = xoxb-...
SLACK_SIGNING_SECRET  = ...
ANTHROPIC_API_KEY     = sk-ant-...
DB_PATH               = /data/cadbot.db
```

### Add a persistent volume (important!)

The bot stores checkout state in a SQLite database. Without a persistent volume, the database is wiped on every redeploy.

In Railway: **Project → your service → Volumes → Add Volume**
- Mount path: `/data`

---

## Step 3 — Wire up the Slack Event URL

1. Back in [api.slack.com/apps](https://api.slack.com/apps) → **Event Subscriptions**
2. Paste: `https://your-app.up.railway.app/slack/events`
3. Slack sends a challenge — Railway must be running for verification ✅
4. Save and reinstall the app if prompted

---

## Step 4 — Add the bot to the channel

```
/invite @CAD-BOT
```

---

## Step 5 — Test it

```
@CAD-BOT help
```

Then try a checkout:
```
@CAD-BOT checkout Battery Cover https://cad.onshape.com/documents/...
```

And check status:
```
@CAD-BOT status
```

---

## Troubleshooting

| Issue | Fix |
|---|---|
| URL verification fails | Make sure Railway is running; `/health` should return 200 |
| Bot doesn't respond | Check Railway logs; verify all env vars are set |
| Database resets on redeploy | Add persistent volume mounted at `/data` |
| `missing_scope` error | Add the missing scope under OAuth & Permissions and reinstall |
| Part not found on checkin | Run `@CAD-BOT status` to see the exact part name that was checked out |
