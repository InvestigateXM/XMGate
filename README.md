# XM Gate

A shared "not a bot" check for Ingress Telegram chats. When someone asks to join a chat that uses XM Gate, the bot opens a small captcha Mini App on their phone. Passing it once marks the account as verified, and every chat using the bot lets it in straight away after that.

This is the **first prototype**. It only covers the join-request flow, so you can feel how it behaves when the Mini App opens by itself:

- Join request arrives. A verified account is approved at once. An unknown account gets the captcha via `sendChatJoinRequestWebApp`.
- The captcha: "tap the two faction logos" in a 3×4 grid of Ingress item icons. A wrong answer reshuffles and rotates a fresh grid; it never declines. After the third miss there's a short wait before each new grid (5 s, 15 s, 30 s).
- "Ask an admin to approve me instead" answers the request with `queue`. If an admin then approves, the account counts as verified (manual).
- Minimising the app starts a grace period (5 minutes by default). If the user doesn't come back, the request is declined. Solving later still verifies the account, so the next request is instant.
- If the bot isn't the chat's join-request processor, it falls back to a DM with a "Verify me" button and the same grace period.

Flags, `/signals`, suspicious marks, log channels, opt-out and PostgreSQL are in the [design](https://claude.ai/artifact/FHCfpmCLcHoBfUWjDNHwty) but not in this prototype. Storage is SQLite.

| Captcha | Wrong answer | Passed |
| --- | --- | --- |
| ![](docs/screenshots/challenge.png) | ![](docs/screenshots/wrong-answer.png) | ![](docs/screenshots/approved.png) |

The icons are simplified placeholders, not Niantic artwork.

## Try it on a test group

You need a Linux VPS with Docker, a domain (or subdomain) you can point at it, and two Telegram accounts: one that owns the test group, and one to request to join with. Group admins can't request to join their own group.

### 1. Create the bot

1. In [@BotFather](https://t.me/BotFather), send `/newbot` and keep the token.
2. Leave privacy mode on; the prototype only needs join requests and DMs.

### 2. Point a domain at the VPS

Add an A (and AAAA if you have IPv6) record, for example `gate.example.com`, pointing at the VPS. Ports 80 and 443 must be open so Caddy can get a certificate.

### 3. Start it

```sh
git clone https://github.com/InvestigateXM/XMGate.git
cd XMGate
cp .env.example .env
# edit .env: DOMAIN, BASE_URL, BOT_TOKEN, WEBHOOK_SECRET (openssl rand -hex 32)
# tip: GRACE_SECONDS=60 makes the timeout quicker to test
docker compose up -d --build
docker compose logs -f app
```

The log should show `Running as @yourbot`, `Webhook set to https://…/webhook`, and no warning about `supports_join_request_queries`. If that warning appears, the bot isn't allowed to take join-request queries yet, and join requests will use the DM fallback instead of opening the Mini App. Check @BotFather's bot settings for a join-request option.

`https://your-domain/health` should answer `ok`.

### 4. Set up the test group

1. Create a group with the owner account.
2. Turn on join requests. For a public group: *Group settings → Group type → Approve new members*. For a private group: make an invite link with *Request admin approval* on.
3. Add the bot as an admin with **Invite users via link**. Nothing else is needed.
4. Assign the bot to process the group's join requests (in the group's join-request or admin settings). This is what makes Telegram hand the request to the bot with a `query_id`. The exact place in the Telegram app is one of the things this test should confirm.

### 5. Request to join

From the second account, open the group link and request to join. The captcha should open by itself. Things worth trying:

- Solve it straight away. The app closes and you're in.
- Pick wrong tiles a few times. Watch the reshuffle and the waits after the third miss.
- Tap "Ask an admin to approve me instead", then approve from the owner account. The bot counts that as verified.
- Minimise the app and wait for the grace period. The request is declined. Reopen the app and solve it: it says you're verified, and requesting again lets you in at once.
- Leave the group and request again. You're approved without seeing the captcha.

To see the captcha again with the same account, DM the bot `/reset`. It forgets that account completely.

`docker compose logs -f app` shows each step (`opening captcha Mini App`, `app minimised, grace period started`, `approved`, `declined_timeout`, …).

## Open questions this test answers

From the design's "to test" list:

1. Where an admin assigns the bot as join-request processor, and whether @BotFather has a toggle for it.
2. Whether a `query_id` stays answerable for the whole grace period after the Mini App opens. If `answerChatJoinRequestQuery` fails, the bot logs it and falls back to `approveChatJoinRequest` / `declineChatJoinRequest`.
3. Whether Telegram resolves the request itself when the user closes the app.
4. What `chat_member` reports when an admin approves a request the bot queued.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The tests run the whole flow against a fake Telegram: join request, Mini App API, approve, queue, decline on timeout, and the initData checks.

Layout:

- `xmgate/flow.py`: what happens to a join request, and the grace-period sweeper
- `xmgate/api.py`: the endpoints the Mini App calls
- `xmgate/captcha.py`: grid building, tile rendering, retry waits
- `xmgate/initdata.py`: Telegram initData signature check
- `xmgate/handlers.py`: aiogram handlers
- `webapp/`: the Mini App (plain HTML and JS, uses Telegram's theme colours)
