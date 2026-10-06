# XM Gate

A shared "not a bot" check for Ingress Telegram chats. When someone asks to join a chat that uses XM Gate, the bot opens a small captcha Mini App on their phone. Passing it once marks the account as verified, and every chat using the bot lets it in straight away after that.

What works so far:

- Join request arrives. A verified account is approved at once. An unknown account gets the captcha via `sendChatJoinRequestWebApp`.
- The captcha: "tap the two faction logos" in a 3×4 grid. Both logos are always there; the other ten tiles are picked at random from the rest of `xmgate/icons/` for every new grid. A wrong answer reshuffles and rotates a fresh grid; it never declines. After the third miss there's a short wait before each new grid (5 s, 15 s, 30 s).
- "Ask an admin to approve me instead" answers the request with `queue`. If an admin then approves, the account counts as verified (manual).
- Minimising the app starts a grace period (5 minutes by default). If the user doesn't come back, the request is declined. Solving later still verifies the account, so the next request is instant.
- If the bot isn't the chat's join-request processor, it falls back to a DM with a "Verify me" button and the same grace period.
- Anyone who messages the bot gets a menu: **Verify me** (the same captcha, which also approves any pending requests), **My data**, **Delete my data**, and **Delete and ignore me**. Opting out deletes everything and keeps only a keyed hash of the user id, `HMAC-SHA256(OPTOUT_PEPPER, id)`. That user's join requests then go straight to the admins and nothing about them is stored or logged. Tapping Verify me later turns processing back on.

Storage is PostgreSQL. Flags, `/signals`, suspicious marks and log channels are in the [design](https://claude.ai/artifact/FHCfpmCLcHoBfUWjDNHwty) but not built yet.

| Captcha | Wrong answer | Passed |
| --- | --- | --- |
| ![](docs/screenshots/challenge.png) | ![](docs/screenshots/wrong-answer.png) | ![](docs/screenshots/approved.png) |

The icons are in `xmgate/icons/`. The two faction logos are `avatar-faction-enlightened.png` and `avatar-faction-resistance.png`; every other PNG there is a decoy, so adding or removing one changes the pool. The server recolours each grid to a single ink on a dark tile, so the logos can't be picked out by colour, and rotates, scales and moves each icon over background noise. Tiles go to the phone as freshly rendered PNGs, never the original files.

## Try it on a test group

You need a Linux VPS running Docker and [Nginx Proxy Manager](https://nginxproxymanager.com/) (NPM), a subdomain for the bot, and two Telegram accounts: one that owns the test group, and one to request to join with. Group admins can't request to join their own group.

No reverse proxy on the server? Skip NPM and use `docker compose -f docker-compose.caddy.yml up -d --build` with `DOMAIN` set in `.env`; Caddy then takes ports 80 and 443 itself.

### 1. Create the bot

1. In [@BotFather](https://t.me/BotFather), send `/newbot` and keep the token.
2. Leave privacy mode on; the prototype only needs join requests and DMs.

### 2. Point the subdomain at the VPS

Add a DNS A record (and AAAA if you use IPv6), for example `gate.example.com`, pointing at the VPS's IP, the same as your other NPM sites. NPM already listens on 80 and 443 and picks the right site by hostname, so nothing else on the server changes.

### 3. Find NPM's Docker network

The bot joins NPM's Docker network so NPM can reach it by name, without opening a port on the host.

```sh
docker ps --format '{{.Names}}' | grep -i -E 'npm|proxy'     # find the NPM container
docker inspect <npm-container> --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```

That prints the network name, often `npm_default` or `nginx-proxy-manager_default`. Put it in `NPM_NETWORK` in the next step.

### 4. Start the bot

```sh
git clone https://github.com/InvestigateXM/XMGate.git
cd XMGate
cp .env.example .env
# edit .env: BASE_URL, BOT_TOKEN, NPM_NETWORK, and three secrets:
#   WEBHOOK_SECRET, POSTGRES_PASSWORD and OPTOUT_PEPPER (each: openssl rand -hex 32)
# tip: GRACE_SECONDS=60 makes the timeout quicker to test
docker compose up -d --build
docker compose logs -f app
```

Until step 5 is done it may log `Could not set webhook` every 30 seconds, because Telegram can't reach the address yet. That's expected, and it carries on by itself once the proxy host works. Telegram also holds back updates it couldn't deliver and retries them.

**Already running the SQLite prototype?** Add `POSTGRES_PASSWORD` and `OPTOUT_PEPPER` to your `.env` (see `.env.example`), then `git pull && docker compose up -d --build`. The test data isn't carried over, so test accounts see the captcha again. The old data volume is no longer used and can go: `docker volume rm xmgate_data`.

Keep a copy of `OPTOUT_PEPPER` somewhere safe and never change it. Without the same pepper, opted-out users can't be recognised and would be processed again.

### 5. Add the proxy host in NPM

In NPM, *Hosts → Proxy Hosts → Add Proxy Host*:

| Tab | Setting | Value |
| --- | --- | --- |
| Details | Domain Names | `gate.example.com` |
| Details | Scheme | `http` |
| Details | Forward Hostname / IP | `xmgate` |
| Details | Forward Port | `8080` |
| Details | Cache Assets | off |
| Details | Block Common Exploits | on (fine either way) |
| Details | Websockets Support | off (not used) |
| SSL | SSL Certificate | Request a new SSL Certificate (Let's Encrypt) |
| SSL | Force SSL | on |
| SSL | HTTP/2 Support | on |

The Advanced tab needs nothing. Leave Cache Assets off so a changed Mini App reaches phones straight away.

If NPM is installed directly on the host rather than in Docker, it can't join the network. Then add `ports: ["127.0.0.1:8080:8080"]` to the `app` service, delete its `networks:` block and the one at the bottom, and forward NPM to `127.0.0.1` port `8080`.

Once the proxy host is saved, the log should show `Running as @yourbot`, `Webhook set to https://…/webhook`, and no warning about `supports_join_request_queries`. If that warning appears, the bot isn't allowed to take join-request queries yet, and join requests will use the DM fallback instead of opening the Mini App. Check @BotFather's bot settings for a join-request option.

`https://gate.example.com/health` should answer `ok` in a browser.

### 6. Set up the test group

1. Create a group with the owner account.
2. Turn on join requests. For a public group: *Group settings → Group type → Approve new members*. For a private group: make an invite link with *Request admin approval* on.
3. Add the bot as an admin with **Invite users via link**. Nothing else is needed.
4. Assign the bot to process the group's join requests (in the group's join-request or admin settings). This is what makes Telegram hand the request to the bot with a `query_id`. The exact place in the Telegram app is one of the things this test should confirm.

### 7. Request to join

From the second account, open the group link and request to join. The captcha should open by itself. Things worth trying:

- Solve it straight away. The app closes and you're in.
- Pick wrong tiles a few times. Watch the reshuffle and the waits after the third miss.
- Tap "Ask an admin to approve me instead", then approve from the owner account. The bot counts that as verified.
- Minimise the app and wait for the grace period. The request is declined. Reopen the app and solve it: it says you're verified, and requesting again lets you in at once.
- Leave the group and request again. You're approved without seeing the captcha.

To see the captcha again with the same account, open the bot's DM menu and tap **Delete my data**.

`docker compose logs -f app` shows each step (`opening captcha Mini App`, `app minimised, grace period started`, `approved`, `declined_timeout`, …).

## Open questions this test answers

From the design's "to test" list:

1. Where an admin assigns the bot as join-request processor, and whether @BotFather has a toggle for it.
2. Whether a `query_id` stays answerable for the whole grace period after the Mini App opens. If `answerChatJoinRequestQuery` fails, the bot logs it and falls back to `approveChatJoinRequest` / `declineChatJoinRequest`.
3. Whether Telegram resolves the request itself when the user closes the app.
4. What `chat_member` reports when an admin approves a request the bot queued.

## Development

The flow tests need a PostgreSQL they may wipe:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
docker run -d --name xmgate-test-db -e POSTGRES_PASSWORD=test -p 5433:5432 postgres:16-alpine
TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/postgres pytest
```

They run the whole flow against a fake Telegram: join request, Mini App API, approve, queue, decline on timeout, the initData checks, self-verification, deleting data and opting out. Without `TEST_DATABASE_URL` the database tests are skipped.

The schema is in `xmgate/schema.sql` and is applied at every start; each statement there must be safe to run again.

Layout:

- `xmgate/flow.py`: what happens to a join request, and the grace-period sweeper
- `xmgate/api.py`: the endpoints the Mini App calls
- `xmgate/captcha.py`: grid building, tile rendering, retry waits
- `xmgate/initdata.py`: Telegram initData signature check
- `xmgate/handlers.py`: aiogram handlers and the DM menu
- `xmgate/db.py`, `xmgate/schema.sql`: PostgreSQL storage
- `webapp/`: the Mini App (plain HTML and JS, uses Telegram's theme colours)
