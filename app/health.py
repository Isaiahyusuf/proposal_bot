"""Per-user Telegram account health. Safety only: it pauses sending and explains why.

It never switches accounts/sessions, never retries around a restriction, and never runs in the
background. Status checks are user-initiated and rate limited.

SpamBot note: Telegram's public Spam FAQ (https://telegram.org/faq_spam) says limits exist and points
to @SpamBot, but does not publish its reply texts. The patterns below are therefore an allow-list;
anything else is reported as Unknown instead of guessed. Verify them against a real reply before
relying on them.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from . import models as m
from .telegram_service import (AccountRestricted, SessionInvalid, TelegramError, TelegramGateway,
                               TelegramRateLimited, TemporaryError)

HEALTHY, CHECKING, RESTRICTED = "healthy", "checking", "restricted"
FLOOD_WAIT, CONNECTION_ERROR, UNKNOWN = "flood_wait", "connection_error", "unknown"
LABELS = {HEALTHY: "Healthy", CHECKING: "Checking", RESTRICTED: "Restricted", FLOOD_WAIT: "Flood Wait",
          CONNECTION_ERROR: "Connection Error", UNKNOWN: "Unknown"}

CHECK_COOLDOWN = timedelta(minutes=15)  # minimum gap between SpamBot queries per user
CHECKING_STALE = timedelta(minutes=5)
MAX_DETAIL = 1000


class HealthError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


# ---------- SpamBot parsing ----------
@dataclass
class ParsedStatus:
    state: str  # healthy | restricted | unknown
    detail: Optional[str]
    until: Optional[datetime] = None


_CLEAN = re.compile(r"no limits are currently applied to your account", re.I)
_LIMITED = re.compile(r"(account (is|was|has been) (now |currently )?(limited|restricted)|"
                      r"(limited|restricted) until|your account is limited)", re.I)
_UNTIL = re.compile(r"(?:limited|restricted) until[:\s]+(.+?)(?:[.\n]|$)", re.I)
_DATE_FORMATS = ("%d %b %Y, %H:%M UTC", "%d %B %Y, %H:%M UTC", "%d %b %Y", "%d %B %Y",
                 "%Y-%m-%d %H:%M UTC", "%Y-%m-%d")


def _parse_until(text: str) -> Optional[datetime]:
    mt = _UNTIL.search(text)
    if not mt:
        return None
    raw = mt.group(1).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None  # date present but in an unrecognized format: do not guess


def parse_spambot(text: Optional[str]) -> ParsedStatus:
    if not text or not text.strip():
        return ParsedStatus(UNKNOWN, "SpamBot did not reply, so the status could not be determined.")
    clean = text.strip()[:MAX_DETAIL]
    if _LIMITED.search(clean):  # restriction wins over any incidental clean-sounding text
        return ParsedStatus(RESTRICTED, clean, _parse_until(clean))
    if _CLEAN.search(clean):
        return ParsedStatus(HEALTHY, clean)
    return ParsedStatus(UNKNOWN, "SpamBot replied with text this app does not recognize: " + clean)


# ---------- state transitions ----------
def hold_approved(db: Session, user_id: int, reason: str) -> int:
    """Stop queued (approved) outreach for this user only. Drafts and leads are untouched."""
    res = db.execute(update(m.OutreachRecord).where(
        m.OutreachRecord.user_id == user_id, m.OutreachRecord.status == "approved"
    ).values(status="held", error="Held: " + reason[:300]))
    return res.rowcount


def release_held(db: Session, user_id: int) -> int:
    res = db.execute(update(m.OutreachRecord).where(
        m.OutreachRecord.user_id == user_id, m.OutreachRecord.status == "held"
    ).values(status="approved", error=None))
    return res.rowcount


def pause(db: Session, acct: m.TelegramAccount, state: str, detail: str, source: str,
          until: Optional[datetime] = None):
    acct.health_state, acct.health_detail, acct.health_source = state, detail[:MAX_DETAIL], source
    acct.health_checked_at = utcnow()
    if state == FLOOD_WAIT:
        acct.flood_wait_until = until
    elif state == RESTRICTED:
        acct.restricted_until = until
    hold_approved(db, acct.user_id, f"{LABELS[state]} - {detail}")
    db.commit()


def refresh(db: Session, acct: m.TelegramAccount) -> m.TelegramAccount:
    """Apply time-based transitions. An expired flood wait is explicit permission from Telegram to
    continue, so sending resumes; the state becomes Unknown (not Healthy) until a check confirms."""
    now = utcnow()
    if acct.health_state == FLOOD_WAIT and (not acct.flood_wait_until or aware(acct.flood_wait_until) <= now):
        acct.health_state, acct.flood_wait_until = UNKNOWN, None
        acct.health_detail, acct.health_source = "Flood wait has expired. Run Check Status to confirm.", None
        release_held(db, acct.user_id)
        db.commit()
    elif acct.health_state == CHECKING and acct.health_checked_at and \
            now - aware(acct.health_checked_at) > CHECKING_STALE:
        acct.health_state, acct.health_detail = UNKNOWN, "A previous check did not finish."
        db.commit()
    return acct


def send_block_reason(db: Session, acct: Optional[m.TelegramAccount]) -> Optional[str]:
    """None when this account may send, else the human-readable reason."""
    if acct is None or acct.status != "connected":
        return "Connect your Telegram account first."
    refresh(db, acct)
    st = acct.health_state
    if st == FLOOD_WAIT:
        return f"Telegram flood wait is active until {aware(acct.flood_wait_until):%Y-%m-%d %H:%M:%S} UTC."
    if st == RESTRICTED:
        return "Sending is paused: " + (acct.health_detail or "Telegram reported a restriction.")
    if st == CONNECTION_ERROR:
        return "Sending is paused: " + (acct.health_detail or "connection problem.") + " Run Check Status."
    if st == CHECKING:
        return "A status check is in progress."
    return None


def next_check_at(acct: m.TelegramAccount) -> Optional[datetime]:
    last = aware(acct.last_spambot_check_at)
    return last + CHECK_COOLDOWN if last else None


def run_check(db: Session, acct: m.TelegramAccount, gw: TelegramGateway, creds) -> m.TelegramAccount:
    if acct.status != "connected":
        raise HealthError("Connect your Telegram account first.")
    refresh(db, acct)
    now = utcnow()
    if acct.health_state == FLOOD_WAIT:
        raise HealthError("A flood wait is active; no check was sent to Telegram. Retry after "
                          f"{aware(acct.flood_wait_until):%Y-%m-%d %H:%M:%S} UTC.")
    nxt = next_check_at(acct)
    if nxt and nxt > now:
        raise HealthError(f"Checked recently. To avoid extra Telegram traffic, the next check is allowed at "
                          f"{nxt:%H:%M:%S} UTC.")
    previous = acct.health_state
    acct.health_state, acct.health_checked_at = CHECKING, now
    db.commit()
    try:
        reply = gw.check_spam_status(creds)
    except TelegramRateLimited as e:
        acct.last_spambot_check_at = now
        pause(db, acct, FLOOD_WAIT, f"Telegram asked to wait {e.seconds}s.", "spambot",
              now + timedelta(seconds=e.seconds))
        return acct
    except AccountRestricted as e:
        acct.last_spambot_check_at = now
        pause(db, acct, RESTRICTED, e.detail, "spambot")
        return acct
    except SessionInvalid as e:
        pause(db, acct, CONNECTION_ERROR, str(e), "spambot")
        return acct
    except TelegramError as e:  # nothing reached SpamBot, so this does not count against the cooldown
        pause(db, acct, CONNECTION_ERROR, str(e), "spambot")
        return acct

    acct.last_spambot_check_at = now
    parsed = parse_spambot(reply.text)
    if parsed.state == HEALTHY:
        acct.health_state, acct.health_detail, acct.health_source = HEALTHY, parsed.detail, "spambot"
        acct.restricted_until, acct.flood_wait_until, acct.health_checked_at = None, None, now
        release_held(db, acct.user_id)
        db.commit()
    elif parsed.state == RESTRICTED:
        pause(db, acct, RESTRICTED, parsed.detail, "spambot", parsed.until)
    else:
        if previous == RESTRICTED:  # an unreadable reply never clears a known restriction
            acct.health_state = RESTRICTED
            acct.health_detail = (acct.health_detail or "") + " | Latest check was inconclusive."
        else:
            acct.health_state, acct.health_detail = UNKNOWN, parsed.detail
        acct.health_source, acct.health_checked_at = "spambot", now
        db.commit()
    return acct


def describe(db: Session, acct: Optional[m.TelegramAccount], prefs: m.UserPreferences) -> dict:
    if acct is None or acct.status != "connected":
        return {"state": UNKNOWN, "label": "Not connected", "connected": False, "sending_enabled": False,
                "action": "Connect your Telegram account.", "detail": None, "retry_at": None,
                "checked_at": None, "next_check_at": None, "pause_reason": "Telegram is not connected."}
    refresh(db, acct)
    st = acct.health_state
    reason = send_block_reason(db, acct)
    if prefs.sending_paused:
        reason = reason or "You paused sending in your settings."
    retry = None
    if st == FLOOD_WAIT:
        retry = aware(acct.flood_wait_until)
    elif st == RESTRICTED:
        retry = aware(acct.restricted_until)  # only present when Telegram stated it
    actions = {
        HEALTHY: "No action needed. Keep volume low and messages relevant.",
        CHECKING: "Wait for the check to finish.",
        RESTRICTED: "Do not send. Wait out the restriction"
                    + (" (Telegram gave a date above)" if retry else "") + ", then run Check Status. "
                    "You can appeal via @SpamBot. Your drafts are preserved.",
        FLOOD_WAIT: "Wait until the retry time. Sending resumes automatically afterwards.",
        CONNECTION_ERROR: "Run Check Status; reconnect Telegram if the session is invalid.",
        UNKNOWN: "Run Check Status to confirm the account status. Do not assume it is clean.",
    }
    return {"state": st, "label": LABELS[st], "connected": True, "sending_enabled": reason is None,
            "pause_reason": reason, "action": actions[st], "detail": acct.health_detail, "retry_at": retry,
            "checked_at": aware(acct.health_checked_at), "next_check_at": next_check_at(acct)}
