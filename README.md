# Proposal Bot (multi-user)

FastAPI + Jinja app. Each user has an isolated workspace: own Telegram connection, message template,
discovery preferences, leads, outreach history, do-not-contact list and rate limits.

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then fill SECRET_KEY and ENCRYPTION_KEY (commands inside the file)
docker compose up -d db  # PostgreSQL (or point DATABASE_URL at your own)
alembic upgrade head     # create/upgrade schema
uvicorn app.main:app --reload   # http://127.0.0.1:8000
```

## Environment variables
| Var | Purpose |
|---|---|
| `DATABASE_URL` | SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@host/db` (SQLite default is dev-only) |
| `SECRET_KEY` | Signs session cookies |
| `ENCRYPTION_KEY` | Fernet key encrypting Telegram API hash, phone and session at rest. Losing it invalidates stored sessions; users must reconnect |
| `COOKIE_SECURE` | `true` when served over HTTPS |

## Migrations
`alembic upgrade head` applies; `alembic revision --autogenerate -m "msg"` creates new ones after model changes.
There was no previous schema, so no data migration is needed. SQLite is used only for tests/dev; PostgreSQL is
recommended for concurrent users (row-level locking, real concurrency, composite-FK enforcement always on).

## Tests
```bash
python -m pytest -q
```
Telegram and DexScreener are mocked; no network needed.

## Security model
- Argon2 password hashes; signed session cookies; CSRF token on every POST; login lockout after repeated failures.
- Every query is scoped by `user_id`; routes return 404 for foreign objects and the service layer re-checks ownership.
- Composite foreign keys `(lead_id, user_id)` / `(candidate_id, user_id)` make cross-user rows impossible at the DB level.
- `UNIQUE(user_id, recipient)` prevents duplicate outreach; do-not-contact is checked at discovery, draft, approve and send.
- Telegram credentials/sessions are Fernet-encrypted, never rendered. Users enter their own API ID/hash and login code,
  which go only to Telegram; a 2FA password is passed straight to Telegram and never stored.

## Telegram limitations
Only admins with public usernames in groups your account can see are discoverable; not all members can be listed or
messaged. Privacy settings, PeerFlood and FloodWait stop sends (flood waits are recorded and respected). There is no
account rotation or limit bypass. Sending is always a manual click after approval, within per-user daily/interval limits.
You are responsible for complying with Telegram's Terms and anti-spam rules.

## Account health and messaging safety
- **Account health** (`/account-health`): per-user state - Healthy, Checking, Restricted, Flood Wait, Connection Error or
  Unknown. **Check Status** sends `/start` to Telegram's official @SpamBot from that user's own account (user-initiated only,
  15-minute cooldown, skipped during a flood wait). No background polling exists.
- **SpamBot parsing is an allow-list.** Telegram's [Spam FAQ](https://telegram.org/faq_spam) documents limits and points to
  @SpamBot but does not publish its reply text, so the patterns in [app/health.py](app/health.py) are unverified against a
  live account. Unrecognized or missing replies show **Unknown**, and never clear a known restriction. Verify the patterns with
  one real check before relying on them.
- **Error classification** ([app/telegram_service.py](app/telegram_service.py)): FloodWait -> Flood Wait (retry time from
  Telegram); PeerFlood/deactivated -> Restricted; revoked session -> Connection Error; privacy/invalid username ->
  Delivery Failed + suppression (Telegram-confirmed); `USER_IS_BLOCKED` -> **Possible Block** (never recorded as a confirmed
  block); other RPC errors -> Delivery Failed; network errors -> temporary, retryable, no account state change.
- **Automatic pause:** an account-level signal holds that user's approved messages (status `held`; drafts and leads untouched)
  and blocks sending until Telegram's wait expires or a check reports healthy. Other users are unaffected. Disconnecting and
  reconnecting the same number does not clear a pause. No account rotation or alternate identities.
- **Opt-outs:** from `/cleanup`, mark a contact Declined / Blocked me / Do not contact. This writes a suppression record,
  cancels all unsent messages to them, hides them from lead lists and blocks rediscovery. Declined/blocked/undeliverable
  records cannot be removed in the UI. Replies are not auto-interpreted; opt-outs are the user's explicit decision.
- **Cleanup:** nothing is deleted automatically and a restriction never triggers deletion. The user selects messages, reviews
  them, and confirms; each deletion is logged in `cleanup_actions` and the outreach record is kept as evidence.

## Group creator and contact discovery
`Inspect group` ([app/telegram_service.py](app/telegram_service.py) `inspect_group`, [app/discovery.py](app/discovery.py))
reads the group with the user's own session. It is read-only and **never joins a group**; private invite links only work if
the account is already a member. Re-inspection is limited to once per 10 minutes per lead.

What Telegram actually exposes, and how it is used:
- **Original creator:** Telegram has no "original creator" field. The only signal is the group's *creation service message*; if
  it names a user account with a public username, that user is **Confirmed creator** (high confidence, verified). Otherwise the
  result says so and no creator is named.
- **Current owner** (Telegram's owner role) is shown as **Possible owner**, never as the creator.
- **Admin titles** are chosen by the group owner, so they are unverified. A **Confirmed developer** needs a developer-style
  title *plus* the username appearing in the project's own published links; a title alone stays **Administrator**.
- **Other contacts:** usernames in the group description, pinned message and official links become **Unverified contact**
  (low confidence; may be a channel or service). Bots, deleted accounts and admins without a username are excluded.
- Each candidate stores username, observed role, evidence text, confidence and verification status. A lead with no confirmed
  creator/developer, or an inaccessible group, is flagged **Manual review needed**. Users can add a public contact themselves
  (recorded as `user_supplied`, never verified).
- `{developer_username}` is filled only for confirmed creators/developers. Sending is still approve-then-click, and the sender
  refuses to message anything that is not a person's account (never posts into a channel the user admins).
- Limits: many groups hide admin lists or are private; owners may have no public username; Telegram behavior can differ by
  group type. Creation-message detection is untested against live Telegram.

## Migrations
`alembic upgrade head` applies 0001 (schema), 0002 (health, suppression sources, cleanup log, new outreach statuses) and
0003 (candidate evidence/classification, lead scan state).
0002 tested on SQLite (up/down/up); the PostgreSQL branch (named CHECK swap) is untested here - run it on a staging copy first.

## Not yet production-ready
Needs: HTTPS deployment, external rate limiting/WAF for login, a security review/pen test, backups and key management
(KMS) for `ENCRYPTION_KEY`, email verification/password reset, and a live end-to-end Telegram test with real credentials.
