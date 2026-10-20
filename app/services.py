import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models as m
from .dexscreener import DexScreenerClient, extract_lead
from .security import decrypt, encrypt
from . import discovery, health
from .telegram_service import (AccountRestricted, Creds, DeliveryFailure, GroupInaccessible, SessionInvalid, TelegramError,
                               TelegramGateway, TelegramRateLimited)
from .templating import DEFAULT_TEMPLATE, render_message, validate_template


class ServiceError(Exception):
    pass


class Forbidden(ServiceError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def normalize_username(u: str) -> str:
    u = (u or "").strip()
    u = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)", "", u, flags=re.I).lstrip("@").split("/")[0]
    if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", u):
        raise ServiceError("Invalid Telegram username.")
    return u.lower()


# ---------- accounts / settings ----------
def create_user(db: Session, email: str, password_hash: str) -> m.User:
    user = m.User(email=email.strip().lower(), password_hash=password_hash)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ServiceError("An account with this email already exists.")
    db.add(m.MessageTemplate(user_id=user.id, body=DEFAULT_TEMPLATE))
    db.add(m.UserPreferences(user_id=user.id))
    db.commit()
    return user


def get_template(db: Session, user: m.User) -> m.MessageTemplate:
    return db.scalar(select(m.MessageTemplate).where(m.MessageTemplate.user_id == user.id))


def save_template(db: Session, user: m.User, body: str) -> m.MessageTemplate:
    err = validate_template(body)
    if err:
        raise ServiceError(err)
    t = get_template(db, user)
    t.body, t.customized = body.strip(), True
    db.commit()
    return t


def reset_template(db: Session, user: m.User) -> m.MessageTemplate:
    t = get_template(db, user)
    t.body, t.customized = DEFAULT_TEMPLATE, False
    db.commit()
    return t


def get_prefs(db: Session, user: m.User) -> m.UserPreferences:
    return db.scalar(select(m.UserPreferences).where(m.UserPreferences.user_id == user.id))


def get_telegram(db: Session, user: m.User) -> Optional[m.TelegramAccount]:
    return db.scalar(select(m.TelegramAccount).where(m.TelegramAccount.user_id == user.id))


def creds_for(acct: m.TelegramAccount, user: m.User) -> Creds:
    if acct.user_id != user.id:
        raise Forbidden("Telegram session belongs to another user.")
    return Creds(acct.api_id, decrypt(acct.api_hash_enc), decrypt(acct.session_enc) if acct.session_enc else "")


# ---------- Telegram connection ----------
def start_telegram_login(db, user, gw: TelegramGateway, api_id: int, api_hash: str, phone: str):
    acct = get_telegram(db, user)
    if acct is None:
        acct = m.TelegramAccount(user_id=user.id, api_id=api_id, api_hash_enc="", phone_enc="")
        db.add(acct)
    creds = Creds(api_id, api_hash)
    code_hash, session = gw.send_code(creds, phone)
    if acct.phone_enc and decrypt(acct.phone_enc) != phone:  # a different number has its own health
        acct.health_state, acct.health_detail, acct.health_source = health.UNKNOWN, None, None
        acct.flood_wait_until = acct.restricted_until = acct.health_checked_at = acct.last_spambot_check_at = None
    acct.api_id, acct.api_hash_enc, acct.phone_enc = api_id, encrypt(api_hash), encrypt(phone)
    acct.session_enc, acct.phone_code_hash_enc = encrypt(session), encrypt(code_hash)
    acct.status = "pending"
    db.commit()


def finish_telegram_login(db, user, gw: TelegramGateway, code: str, password: Optional[str]):
    acct = get_telegram(db, user)
    if not acct or not acct.phone_code_hash_enc:
        raise ServiceError("Start the Telegram connection first.")
    session = gw.sign_in(creds_for(acct, user), decrypt(acct.phone_enc), code,
                         decrypt(acct.phone_code_hash_enc), password)
    acct.session_enc, acct.phone_code_hash_enc, acct.status = encrypt(session), None, "connected"
    db.commit()


def disconnect_telegram(db, user, gw: TelegramGateway):
    """Remove credentials and session. Health/flood state is kept so reconnecting the same number
    cannot be used to skip a pause."""
    acct = get_telegram(db, user)
    if acct:
        if acct.session_enc and acct.status == "connected":
            gw.log_out(creds_for(acct, user))
        acct.session_enc = acct.phone_code_hash_enc = None
        acct.api_hash_enc, acct.status = "", "disconnected"
        db.commit()


# ---------- discovery ----------
def discover_leads(db: Session, user: m.User, client: DexScreenerClient) -> int:
    prefs = get_prefs(db, user)
    chains = {c.strip().lower() for c in prefs.chains.split(",") if c.strip()}
    now_ms = utcnow().timestamp() * 1000
    added = 0
    for profile in client.latest_profiles()[: max(prefs.discovery_limit, 0)]:
        if not profile.get("chainId") or not profile.get("tokenAddress"):
            continue
        if chains and profile["chainId"].lower() not in chains:
            continue
        data = extract_lead(profile, client.token_pairs(profile["chainId"], profile["tokenAddress"]))
        if not data or data["liquidity_usd"] < prefs.min_liquidity_usd:
            continue
        if data["created_ms"] and (now_ms - data["created_ms"]) / 3.6e6 > prefs.max_age_hours:
            continue
        exists = db.scalar(select(m.Lead.id).where(
            m.Lead.user_id == user.id, m.Lead.chain == data["chain"],
            m.Lead.token_address == data["token_address"]))
        if exists:
            continue
        fields = {k: v for k, v in data.items() if k not in ("liquidity_usd", "created_ms", "official_links")}
        db.add(m.Lead(user_id=user.id, official_links=json.dumps(data.get("official_links") or []), **fields))
        added += 1
    db.commit()
    return added


SCAN_COOLDOWN = timedelta(minutes=10)  # limits repeat Telegram lookups per lead


def _group_username(link: str) -> Optional[str]:
    names = discovery.usernames_from_links([link])
    return next(iter(names), None)


def find_candidates(db, user, lead: m.Lead, gw: TelegramGateway) -> int:
    """Inspect the project's Telegram group with this user's account and record evidence-backed contacts."""
    check_owner(lead, user)
    acct = get_telegram(db, user)
    if not acct or acct.status != "connected":
        raise ServiceError("Connect your Telegram account first.")
    if not lead.telegram_link:
        raise ServiceError("This project has no Telegram link.")
    reason = health.send_block_reason(db, acct)
    if reason:
        raise ServiceError(reason)
    last = aware(lead.scanned_at)
    if last and last + SCAN_COOLDOWN > utcnow():
        raise ServiceError("This group was inspected recently. Try again in a few minutes.")
    previous_scan = lead.scanned_at
    lead.scanned_at = utcnow()
    try:
        insp = gw.inspect_group(creds_for(acct, user), lead.telegram_link)
    except GroupInaccessible as e:
        lead.scan_status, lead.needs_manual_review = "inaccessible", True
        lead.scan_note = f"{e} Add a contact manually if you have a legitimate public one."
        db.commit()
        raise ServiceError(lead.scan_note)
    except TelegramError as e:
        # A failed lookup is not a completed scan; account-level errors pause the account instead.
        lead.scanned_at = previous_scan
        apply_account_error(db, acct, e)
        db.commit()
        raise ServiceError(str(e))

    result = discovery.classify(insp, discovery.load_links(lead.official_links), _group_username(lead.telegram_link))
    lead.scan_status, lead.needs_manual_review = "scanned", result.needs_manual_review
    lead.scan_note = " ".join(result.notes + (
        [f"{result.unmessageable} identified account(s) have no public username and cannot be messaged."]
        if result.unmessageable else []))
    added = 0
    for info in result.candidates:
        if is_dnc(db, user, info.username):
            continue  # suppressed contacts are never offered again
        row = db.scalar(select(m.Candidate).where(
            m.Candidate.lead_id == lead.id, m.Candidate.user_id == user.id, m.Candidate.username == info.username))
        if row is None:
            db.add(m.Candidate(user_id=user.id, lead_id=lead.id, username=info.username, role=info.role,
                               category=info.category, evidence=info.evidence, confidence=info.confidence,
                               verification_status=info.verification_status, source="telegram"))
            added += 1
        elif row.source == "telegram":  # refresh evidence; never touch user-supplied contacts
            row.role, row.category, row.evidence = info.role, info.category, info.evidence
            row.confidence, row.verification_status = info.confidence, info.verification_status
    db.commit()
    return added


def add_manual_contact(db, user, lead: m.Lead, username: str, note: str) -> m.Candidate:
    """A contact the user found themselves. Recorded as user-supplied, never as verified."""
    check_owner(lead, user)
    u = normalize_username(username)
    if is_dnc(db, user, u):
        raise ServiceError("That contact is on your do-not-contact list.")
    if db.scalar(select(m.Candidate.id).where(
            m.Candidate.lead_id == lead.id, m.Candidate.user_id == user.id, m.Candidate.username == u)):
        raise ServiceError("This contact is already listed for this project.")
    cand = m.Candidate(user_id=user.id, lead_id=lead.id, username=u, role="Added by you", source="manual",
                       category=discovery.UNVERIFIED, confidence="low", verification_status="user_supplied",
                       evidence="Added manually by you" + (f": {note.strip()[:500]}" if note.strip() else "."))
    db.add(cand)
    db.commit()
    return cand


# ---------- outreach ----------
def check_owner(obj, user: m.User):
    if obj is None or obj.user_id != user.id:
        raise Forbidden("Not found or not yours.")


def is_dnc(db, user, username: str) -> bool:
    return db.scalar(select(m.DoNotContact.id).where(
        m.DoNotContact.user_id == user.id, m.DoNotContact.username == username)) is not None


_SOURCE_RANK = {"manual": 0, "undeliverable": 1, "blocked": 2, "declined": 3}


def add_dnc(db, user, username: str, reason: Optional[str] = None, source: str = "manual"):
    """Suppress a contact for this user. Cancels every unsent message/follow-up to them."""
    u = normalize_username(username)
    row = db.scalar(select(m.DoNotContact).where(m.DoNotContact.user_id == user.id, m.DoNotContact.username == u))
    if row is None:
        db.add(m.DoNotContact(user_id=user.id, username=u, reason=reason, source=source))
    elif _SOURCE_RANK[source] > _SOURCE_RANK[row.source]:
        row.source, row.reason = source, reason or row.reason
    db.execute(update(m.OutreachRecord).where(
        m.OutreachRecord.user_id == user.id, m.OutreachRecord.recipient == u,
        m.OutreachRecord.status.in_(["draft", "approved", "held", "failed"])).values(status="skipped"))
    db.commit()


def remove_dnc(db, user, dnc_id: int):
    row = db.get(m.DoNotContact, dnc_id)
    check_owner(row, user)
    if row.source != "manual":  # opt-outs and Telegram-confirmed failures stay as a suppression record
        raise ServiceError("This is a suppression record (opt-out or undeliverable) and cannot be removed.")
    db.delete(row)
    db.commit()


def apply_account_error(db, acct: m.TelegramAccount, e: Exception) -> bool:
    """Account-level Telegram signals pause only this user's account. Returns True if handled."""
    if isinstance(e, TelegramRateLimited):
        health.pause(db, acct, health.FLOOD_WAIT, f"Telegram asked to wait {e.seconds}s before more requests.",
                     "send_error", utcnow() + timedelta(seconds=e.seconds))
    elif isinstance(e, AccountRestricted):
        health.pause(db, acct, health.RESTRICTED, e.detail, "send_error")
    elif isinstance(e, SessionInvalid):
        health.pause(db, acct, health.CONNECTION_ERROR, str(e), "send_error")
    else:
        return False
    return True


def personalize(body: str, lead: m.Lead, candidate: Optional[m.Candidate]) -> str:
    return render_message(body, {
        "project_name": (lead.project_name or "").strip() or lead.token_symbol, "token_symbol": lead.token_symbol, "blockchain": lead.chain,
        "project_website": lead.website,
        "developer_username": ("@" + candidate.username)
        if candidate and candidate.category in discovery.CONFIRMED else None,  # only when verified
    })


def create_draft(db: Session, user: m.User, candidate: m.Candidate) -> m.OutreachRecord:
    check_owner(candidate, user)
    lead = db.get(m.Lead, candidate.lead_id)
    check_owner(lead, user)
    if is_dnc(db, user, candidate.username):
        raise ServiceError("Recipient is on your do-not-contact list.")
    if db.scalar(select(m.OutreachRecord.id).where(
            m.OutreachRecord.user_id == user.id, m.OutreachRecord.recipient == candidate.username)):
        raise ServiceError("You already have outreach for this recipient.")
    body = personalize(get_template(db, user).body, lead, candidate)
    status = "approved" if get_prefs(db, user).auto_approve_drafts else "draft"
    rec = m.OutreachRecord(user_id=user.id, lead_id=lead.id, candidate_id=candidate.id,
                           recipient=candidate.username, body=body, status=status)
    db.add(rec)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ServiceError("You already have outreach for this recipient.")
    return rec


def edit_outreach(db, user, rec: m.OutreachRecord, body: str):
    check_owner(rec, user)
    if rec.status not in ("draft", "approved", "held", "failed"):
        raise ServiceError("This message can no longer be edited.")
    if not body.strip() or len(body) > 4000:
        raise ServiceError("Message must be 1-4000 characters.")
    rec.body, rec.status = body.strip(), "draft"  # edits require re-approval
    db.commit()


def approve(db, user, rec: m.OutreachRecord):
    check_owner(rec, user)
    if rec.status not in ("draft", "failed"):
        raise ServiceError("Only drafts can be approved.")
    if is_dnc(db, user, rec.recipient):
        raise ServiceError("Recipient is on your do-not-contact list.")
    rec.status, rec.error = "approved", None
    db.commit()


def skip(db, user, rec: m.OutreachRecord):
    check_owner(rec, user)
    if rec.status in ("sent", "sending"):
        raise ServiceError("Already sent.")
    if rec.status in ("delivery_failed", "possible_block"):
        raise ServiceError("Delivery already failed; the record is kept as evidence.")
    rec.status = "skipped"
    db.commit()


def send_outreach(db: Session, user: m.User, rec: m.OutreachRecord, gw: TelegramGateway) -> m.OutreachRecord:
    check_owner(rec, user)
    prefs = get_prefs(db, user)
    acct = get_telegram(db, user)
    if rec.status == "held":
        raise ServiceError(rec.error or "This message is held while your account is paused.")
    if rec.status != "approved":
        raise ServiceError("Message must be approved before sending.")
    if prefs.sending_paused:
        raise ServiceError("Sending is paused in your settings.")
    reason = health.send_block_reason(db, acct)
    if reason:
        raise ServiceError(reason)
    if is_dnc(db, user, rec.recipient):
        raise ServiceError("Recipient is on your do-not-contact list.")
    now = utcnow()
    recent = db.scalars(select(m.OutreachRecord.sent_at).where(
        m.OutreachRecord.user_id == user.id, m.OutreachRecord.status == "sent",
        m.OutreachRecord.sent_at >= now - timedelta(hours=24))).all()
    if len(recent) >= prefs.daily_limit:
        raise ServiceError("Your daily sending limit has been reached.")
    if recent:
        wait = (aware(max(recent)) + timedelta(seconds=prefs.min_interval_seconds) - now).total_seconds()
        if wait > 0:
            raise ServiceError(f"Please wait {int(wait) + 1}s between messages (your interval setting).")
    claimed = db.execute(update(m.OutreachRecord).where(
        m.OutreachRecord.id == rec.id, m.OutreachRecord.user_id == user.id,
        m.OutreachRecord.status == "approved").values(status="sending")).rowcount
    db.commit()
    if claimed != 1:
        raise ServiceError("Message is already being sent.")
    db.refresh(rec)
    try:
        msg_id = gw.send_message(creds_for(acct, user), rec.recipient, rec.body)
        rec.status, rec.sent_at, rec.error, rec.failure_kind = "sent", utcnow(), None, None
        rec.telegram_message_id = msg_id
        db.commit()
    except DeliveryFailure as e:
        rec.error, rec.failure_kind = e.detail, e.kind
        if e.kind == "possible_block":
            rec.status = "possible_block"  # inconclusive: never recorded as a confirmed block
        else:
            rec.status = "delivery_failed"
        db.commit()
        if e.kind in ("privacy", "invalid_recipient"):  # Telegram-confirmed: this recipient cannot receive it
            add_dnc(db, user, rec.recipient, e.detail, "undeliverable")
    except TelegramError as e:
        if isinstance(e, (TelegramRateLimited, AccountRestricted, SessionInvalid)):
            rec.status, rec.error = "held", "Held: " + str(e)  # message kept; account paused below
            db.commit()
            apply_account_error(db, acct, e)
        else:  # transport/unknown: says nothing about the account or recipient
            rec.status, rec.error, rec.failure_kind = "failed", str(e), "temporary"
            db.commit()
    return rec


# ---------- contact outcomes and cleanup ----------
def mark_contact(db, user, rec: m.OutreachRecord, outcome: str) -> None:
    """User-reported outcomes. These are explicit user decisions, never inferred from errors."""
    check_owner(rec, user)
    mapping = {"declined": ("Recipient asked not to be contacted", "declined"),
               "blocked": ("User reports the recipient blocked them", "blocked"),
               "dnc": ("Marked do-not-contact by user", "manual")}
    if outcome not in mapping:
        raise ServiceError("Unknown action.")
    reason, source = mapping[outcome]
    add_dnc(db, user, rec.recipient, reason, source)


def delete_sent_messages(db, user, rec_ids, gw: TelegramGateway, revoke: bool, confirmed: bool):
    """Delete only messages this app sent, only after explicit confirmation, one audited action each."""
    if not confirmed:
        raise ServiceError("Deletion requires explicit confirmation.")
    acct = get_telegram(db, user)
    if not acct or acct.status != "connected":
        raise ServiceError("Connect your Telegram account first.")
    health.refresh(db, acct)
    if acct.health_state == health.FLOOD_WAIT:
        raise ServiceError("Telegram flood wait is active; deletion is unavailable until it ends.")
    results = []
    for rid in rec_ids:
        rec = db.get(m.OutreachRecord, rid)
        check_owner(rec, user)
        stop = False
        if rec.status != "sent" or not rec.telegram_message_id or rec.deleted_at:
            res, detail = "failed", "Not eligible (not sent, no message id, or already deleted)."
        else:
            try:
                gw.delete_messages(creds_for(acct, user), rec.recipient, [rec.telegram_message_id], revoke)
                rec.deleted_at, res, detail = utcnow(), "deleted", None
            except TelegramError as e:
                res, detail = "failed", str(e)
                stop = isinstance(e, TelegramRateLimited)
                if stop:
                    apply_account_error(db, acct, e)
        db.add(m.CleanupAction(user_id=user.id, outreach_id=rec.id, telegram_message_id=rec.telegram_message_id,
                               revoke=revoke, result=res, detail=detail))
        db.commit()
        results.append((rec.id, res, detail))
        if stop:  # respect the flood wait: stop issuing requests
            break
    return results


def sent_today(db, user) -> int:
    return db.scalar(select(func.count()).select_from(m.OutreachRecord).where(
        m.OutreachRecord.user_id == user.id, m.OutreachRecord.status == "sent",
        m.OutreachRecord.sent_at >= utcnow() - timedelta(hours=24))) or 0
