from datetime import datetime, timedelta, timezone

import pytest
from telethon import errors as E

from app import health
from app import models as m
from app import services as s
from app.telegram_service import (AccountRestricted, DeliveryFailure, SessionInvalid, TemporaryError,
                                  classify_exception)
from tests.conftest import user_id
from tests.test_multiuser import make_draft, setup_users

CLEAN = "Good news, no limits are currently applied to your account. You’re free as a bird!"
LIMITED = ("Unfortunately, your account is limited until 5 Nov 2026, 14:30 UTC. "
           "During this period you can only message mutual contacts.")


def acct_of(db, email):
    db.expire_all()
    return db.query(m.TelegramAccount).filter_by(user_id=user_id(db, email)).one()


def rec_status(db, rid):
    db.expire_all()
    return db.get(m.OutreachRecord, rid).status


def second_draft(w, db, email, username="dev_bob", approve=True):
    uid = user_id(db, email)
    cand = db.query(m.Candidate).filter_by(user_id=uid, username=username).one()
    w.post(f"/candidates/{cand.id}/draft")
    rec = db.query(m.OutreachRecord).filter_by(user_id=uid, recipient=username).one()
    if approve:
        w.post(f"/outreach/{rec.id}/approve")
    return rec


# ---------- SpamBot parsing ----------
def test_parser_clean_restricted_unknown():
    assert health.parse_spambot(CLEAN).state == "healthy"
    p = health.parse_spambot(LIMITED)
    assert p.state == "restricted" and p.until == datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)
    assert health.parse_spambot("Hello! I am @SpamBot.").state == "unknown"
    assert health.parse_spambot(None).state == "unknown"
    assert health.parse_spambot("   ").state == "unknown"


def test_parser_does_not_guess_unrecognized_date():
    p = health.parse_spambot("Your account is limited until sometime soon.")
    assert p.state == "restricted" and p.until is None


# ---------- error classification ----------
def test_classification_distinguishes_causes():
    assert classify_exception(E.FloodWaitError(request=None, capture=42)).seconds == 42
    assert isinstance(classify_exception(E.PeerFloodError(request=None)), AccountRestricted)
    assert isinstance(classify_exception(E.AuthKeyUnregisteredError(request=None)), SessionInvalid)
    assert classify_exception(E.UserPrivacyRestrictedError(request=None)).kind == "privacy"
    assert classify_exception(E.UsernameNotOccupiedError(request=None)).kind == "invalid_recipient"
    assert classify_exception(E.UserIsBlockedError(request=None)).kind == "possible_block"
    assert classify_exception(E.ChatWriteForbiddenError(request=None)).kind == "ambiguous"
    assert isinstance(classify_exception(ConnectionError("x")), TemporaryError)


# ---------- health checks ----------
def test_healthy_account(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    gw.spambot_reply = CLEAN
    a.post("/account-health/check")
    acct = acct_of(db, "a@x.com")
    assert acct.health_state == "healthy" and acct.health_checked_at is not None
    page = a.get("/account-health").text
    assert "Healthy" in page and "Enabled" in page and gw.spambot_calls == ["SESSION-+15550001"]


def test_new_account_is_unknown_not_clean(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    page = a.get("/account-health").text
    assert acct_of(db, "a@x.com").health_state == "unknown" and "Unknown" in page and "Healthy" not in page


def test_confirmed_restriction_pauses_and_shows_details(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    gw.spambot_reply = LIMITED
    a.post("/account-health/check")
    acct = acct_of(db, "a@x.com")
    assert acct.health_state == "restricted" and "limited until 5 Nov 2026" in acct.health_detail
    page = a.get("/account-health").text
    assert "Restricted" in page and "Paused" in page and "2026-11-05 14:30:00 UTC" in page


def test_flood_wait_from_check(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    gw.spambot_error = classify_exception(E.FloodWaitError(request=None, capture=300))
    a.post("/account-health/check")
    acct = acct_of(db, "a@x.com")
    assert acct.health_state == "flood_wait" and acct.flood_wait_until is not None
    assert "Flood Wait" in a.get("/account-health").text
    gw.spambot_error = None
    a.post("/account-health/check")  # no traffic to Telegram while a flood wait is active
    assert len(gw.spambot_calls) == 1


def test_connection_failure(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    gw.spambot_error = TemporaryError("Could not reach Telegram (OSError).")
    a.post("/account-health/check")
    assert acct_of(db, "a@x.com").health_state == "connection_error"
    assert "Connection Error" in a.get("/account-health").text
    # a network failure never reached SpamBot, so a retry is allowed immediately
    gw.spambot_error, gw.spambot_reply = None, CLEAN
    a.post("/account-health/check")
    assert acct_of(db, "a@x.com").health_state == "healthy"


def test_unknown_spambot_reply_is_not_reported_clean(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    gw.spambot_reply = "Please choose a language."
    a.post("/account-health/check")
    acct = acct_of(db, "a@x.com")
    assert acct.health_state == "unknown" and "Please choose a language." in acct.health_detail
    acct.last_spambot_check_at = None
    db.commit()
    gw.spambot_reply = None  # no reply at all
    a.post("/account-health/check")
    assert acct_of(db, "a@x.com").health_state == "unknown"


def test_unknown_reply_does_not_clear_restriction(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    gw.spambot_reply = LIMITED
    a.post("/account-health/check")
    acct = acct_of(db, "a@x.com")
    acct.last_spambot_check_at = None
    db.commit()
    gw.spambot_reply = "???"
    a.post("/account-health/check")
    assert acct_of(db, "a@x.com").health_state == "restricted"


def test_check_cooldown_limits_telegram_traffic(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    gw.spambot_reply = CLEAN
    a.post("/account-health/check")
    a.post("/account-health/check")
    a.post("/account-health/check")
    assert len(gw.spambot_calls) == 1
    acct = acct_of(db, "a@x.com")
    acct.last_spambot_check_at = datetime.now(timezone.utc) - timedelta(minutes=16)
    db.commit()
    a.post("/account-health/check")
    assert len(gw.spambot_calls) == 2


# ---------- automatic sending protection ----------
def test_restriction_on_send_holds_queue_and_preserves_drafts(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    lead, cand, first = make_draft(a, gw, db, "a@x.com")
    second = second_draft(a, db, "a@x.com", approve=True)
    third_cand = db.query(m.Candidate).filter_by(user_id=user_id(db, "a@x.com"), username="dev_bob").one()
    gw.admins = ["dev_alice", "dev_bob", "dev_carol"]
    db.query(m.Lead).update({"scanned_at": None})  # bypass the re-scan cooldown for this scenario
    db.commit()
    a.post(f"/leads/{lead.id}/candidates")
    carol = db.query(m.Candidate).filter_by(user_id=user_id(db, "a@x.com"), username="dev_carol").one()
    a.post(f"/candidates/{carol.id}/draft")
    draft = db.query(m.OutreachRecord).filter_by(recipient="dev_carol").one()
    assert draft.status == "draft"

    gw.send_error = AccountRestricted("PEER_FLOOD")
    a.post(f"/outreach/{first.id}/send")
    acct = acct_of(db, "a@x.com")
    assert acct.health_state == "restricted" and gw.sent == []
    assert rec_status(db, first.id) == "held" and rec_status(db, second.id) == "held"
    assert rec_status(db, draft.id) == "draft"  # preserved untouched
    assert db.query(m.Lead).filter_by(user_id=acct.user_id).count() == 2

    gw.send_error = None
    a.post(f"/outreach/{second.id}/send")  # nothing is sent while paused
    assert gw.sent == [] and rec_status(db, second.id) == "held"
    assert "Paused" in a.get("/account-health").text and "PEER_FLOOD" in a.get("/account-health").text

    gw.spambot_reply = CLEAN  # resume only after a healthy confirmation
    a.post("/account-health/check")
    assert rec_status(db, first.id) == "approved" and rec_status(db, second.id) == "approved"
    a.post(f"/outreach/{first.id}/send")
    assert len(gw.sent) == 1


def test_flood_wait_on_send_pauses_and_resumes_after_expiry(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    gw.flood = 90
    a.post(f"/outreach/{rec.id}/send")
    acct = acct_of(db, "a@x.com")
    assert acct.health_state == "flood_wait" and rec_status(db, rec.id) == "held"
    gw.flood = None
    a.post(f"/outreach/{rec.id}/send")
    assert gw.sent == []
    acct.flood_wait_until = datetime.now(timezone.utc) - timedelta(seconds=1)  # Telegram's wait has passed
    db.commit()
    a.get("/account-health")
    assert acct_of(db, "a@x.com").health_state == "unknown"  # not claimed healthy
    assert rec_status(db, rec.id) == "approved"
    a.post(f"/outreach/{rec.id}/send")
    assert len(gw.sent) == 1


def test_session_invalid_pauses_with_connection_error(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    gw.send_error = SessionInvalid("The Telegram session is no longer valid.")
    a.post(f"/outreach/{rec.id}/send")
    assert acct_of(db, "a@x.com").health_state == "connection_error" and rec_status(db, rec.id) == "held"


def test_restriction_is_isolated_per_user(web, gw, db):
    a, b = setup_users(web, gw)
    _, _, ra = make_draft(a, gw, db, "a@x.com")
    _, _, rb = make_draft(b, gw, db, "b@x.com")
    gw.send_error = AccountRestricted("PEER_FLOOD")
    a.post(f"/outreach/{ra.id}/send")
    gw.send_error = None
    assert acct_of(db, "a@x.com").health_state == "restricted"
    assert acct_of(db, "b@x.com").health_state == "unknown"
    assert rec_status(db, rb.id) == "approved"
    b.post(f"/outreach/{rb.id}/send")
    assert [x[0] for x in gw.sent] == ["SESSION-+15550002"]
    assert "Enabled" in b.get("/account-health").text and "Paused" in a.get("/account-health").text


def test_disconnect_and_reconnect_does_not_clear_pause(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    gw.flood = 600
    a.post(f"/outreach/{rec.id}/send")
    a.post("/settings/telegram/disconnect")
    a.connect_telegram()
    gw.flood = None
    assert acct_of(db, "a@x.com").health_state == "flood_wait"
    a.post(f"/outreach/{rec.id}/send")
    assert gw.sent == []


# ---------- delivery failures ----------
def test_ambiguous_failure_is_not_a_confirmed_block(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    gw.send_error = DeliveryFailure("possible_block", "Telegram returned USER_IS_BLOCKED.")
    a.post(f"/outreach/{rec.id}/send")
    db.expire_all()
    r = db.get(m.OutreachRecord, rec.id)
    assert r.status == "possible_block" and r.failure_kind == "possible_block"
    assert db.query(m.DoNotContact).count() == 0  # nothing labelled blocked
    page = a.get("/outreach").text
    assert "Possible Block" in page and "does not definitively confirm" in page
    assert acct_of(db, "a@x.com").health_state == "unknown"  # a recipient issue is not an account issue
    a.post(f"/outreach/{rec.id}/send")
    assert gw.sent == []


def test_unclassified_telegram_error_is_delivery_failed_only(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    gw.send_error = DeliveryFailure("ambiguous", "Telegram returned ChatWriteForbiddenError.")
    a.post(f"/outreach/{rec.id}/send")
    assert rec_status(db, rec.id) == "delivery_failed" and db.query(m.DoNotContact).count() == 0
    assert acct_of(db, "a@x.com").health_state == "unknown"


def test_temporary_error_is_retryable_and_not_account_wide(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    gw.send_error = TemporaryError("Could not reach Telegram.")
    a.post(f"/outreach/{rec.id}/send")
    db.expire_all()
    assert db.get(m.OutreachRecord, rec.id).status == "failed"
    assert acct_of(db, "a@x.com").health_state == "unknown"
    gw.send_error = None
    a.post(f"/outreach/{rec.id}/approve")
    a.post(f"/outreach/{rec.id}/send")
    assert len(gw.sent) == 1


def test_privacy_restricted_is_confirmed_undeliverable_and_suppressed(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    gw.send_error = DeliveryFailure("privacy", "The recipient's privacy settings do not allow this message.")
    a.post(f"/outreach/{rec.id}/send")
    row = db.query(m.DoNotContact).one()
    assert rec_status(db, rec.id) == "delivery_failed" and row.source == "undeliverable"


# ---------- opt-out and suppression ----------
def test_explicit_opt_out_suppresses_and_cancels_everything_pending(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    lead, cand, rec = make_draft(a, gw, db, "a@x.com")  # approved, unsent
    a.post(f"/conversations/{rec.id}/mark", outcome="declined")
    row = db.query(m.DoNotContact).one()
    assert row.source == "declined" and rec_status(db, rec.id) == "skipped"
    a.post(f"/outreach/{rec.id}/send")
    assert gw.sent == []


def test_suppressed_contact_cannot_be_recontacted_or_rediscovered(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    lead, cand, rec = make_draft(a, gw, db, "a@x.com")
    a.post(f"/conversations/{rec.id}/mark", outcome="blocked")
    uid = user_id(db, "a@x.com")
    # not offered again on the leads page
    assert "@dev_alice" not in a.get("/leads").text
    # rediscovery on another lead creates no candidate
    other = db.query(m.Lead).filter_by(user_id=uid, chain="ethereum").one()
    other.telegram_link = "https://t.me/projb"
    db.commit()
    a.post(f"/leads/{other.id}/candidates")
    assert db.query(m.Candidate).filter_by(user_id=uid, username="dev_alice").count() == 1
    # a draft cannot be re-created even from the old candidate row
    user = db.get(m.User, uid)
    with pytest.raises(s.ServiceError):
        s.create_draft(db, user, cand)
    # the suppression record cannot be removed from the UI
    assert a.post(f"/dnc/{row_id(db)}/delete").status_code == 303
    assert db.query(m.DoNotContact).count() == 1
    # direct service-level send of a forced-approved record is still blocked
    db.expire_all()
    r = db.get(m.OutreachRecord, rec.id)
    r.status = "approved"
    db.commit()
    with pytest.raises(s.ServiceError):
        s.send_outreach(db, user, r, gw)
    assert gw.sent == []


def row_id(db):
    return db.query(m.DoNotContact).one().id


def test_opt_out_is_per_user(web, gw, db):
    a, b = setup_users(web, gw)
    _, _, ra = make_draft(a, gw, db, "a@x.com")
    _, _, rb = make_draft(b, gw, db, "b@x.com")
    a.post(f"/conversations/{ra.id}/mark", outcome="declined")
    assert b.post(f"/conversations/{ra.id}/mark", outcome="declined").status_code == 404
    assert rec_status(db, rb.id) == "approved"
    b.post(f"/outreach/{rb.id}/send")
    assert len(gw.sent) == 1


# ---------- cleanup ----------
def sent_record(db, gw, a):
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    a.post(f"/outreach/{rec.id}/send")
    db.expire_all()
    return db.get(m.OutreachRecord, rec.id)


def test_deletion_requires_explicit_confirmation(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    rec = sent_record(db, gw, a)
    assert rec.status == "sent" and rec.telegram_message_id
    # review step deletes nothing
    page = a.post("/cleanup/review", ids=str(rec.id)).text
    assert "Review before deleting" in page and gw.deleted == []
    # confirm without the confirmation flag deletes nothing
    a.post("/cleanup/confirm", ids=str(rec.id))
    assert gw.deleted == [] and db.query(m.CleanupAction).count() == 0
    # explicit confirmation deletes, logs, and keeps the evidence row
    a.post("/cleanup/confirm", ids=str(rec.id), confirm="yes", revoke="yes")
    assert gw.deleted == [("SESSION-+15550001", "dev_alice", [rec.telegram_message_id], True)]
    act = db.query(m.CleanupAction).one()
    assert act.result == "deleted" and act.outreach_id == rec.id
    db.expire_all()
    kept = db.get(m.OutreachRecord, rec.id)
    assert kept.deleted_at is not None and kept.status == "sent" and kept.body
    # cannot be deleted twice
    a.post("/cleanup/confirm", ids=str(rec.id), confirm="yes")
    assert len(gw.deleted) == 1 and db.query(m.CleanupAction).count() == 2


def test_deletion_failure_is_recorded(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    rec = sent_record(db, gw, a)
    gw.delete_error = DeliveryFailure("ambiguous", "Telegram returned MessageIdInvalidError.")
    a.post("/cleanup/confirm", ids=str(rec.id), confirm="yes")
    act = db.query(m.CleanupAction).one()
    assert act.result == "failed" and "MessageIdInvalidError" in act.detail
    db.expire_all()
    assert db.get(m.OutreachRecord, rec.id).deleted_at is None


def test_deletion_only_for_own_messages(web, gw, db):
    a, b = setup_users(web, gw)
    rec = sent_record(db, gw, a)
    assert b.post("/cleanup/confirm", ids=str(rec.id), confirm="yes").status_code == 404
    assert b.post("/cleanup/review", ids=str(rec.id)).status_code == 404
    assert gw.deleted == [] and "dev_alice" not in b.get("/cleanup").text


def test_restriction_never_triggers_deletion(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    rec = sent_record(db, gw, a)
    gw.spambot_reply = LIMITED
    a.post("/account-health/check")
    gw.send_error = AccountRestricted("PEER_FLOOD")
    assert gw.deleted == [] and db.query(m.CleanupAction).count() == 0
    db.expire_all()
    assert db.get(m.OutreachRecord, rec.id).deleted_at is None
