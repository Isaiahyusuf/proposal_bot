from app import models as m
from app import services as s
from app.security import decrypt
from app.templating import DEFAULT_TEMPLATE
from tests.conftest import user_id


def setup_users(web, gw):
    a = web().register("a@x.com").connect_telegram("+15550001")
    b = web().register("b@x.com").connect_telegram("+15550002")
    return a, b


def make_draft(w, gw, db, email, approve=True):
    w.post("/leads/discover")
    uid = user_id(db, email)
    lead = db.query(m.Lead).filter_by(user_id=uid, chain="solana").one()
    w.post(f"/leads/{lead.id}/candidates")
    cand = db.query(m.Candidate).filter_by(user_id=uid, username="dev_alice").one()
    w.post(f"/candidates/{cand.id}/draft")
    rec = db.query(m.OutreachRecord).filter_by(user_id=uid, recipient="dev_alice").one()
    if approve:
        w.post(f"/outreach/{rec.id}/approve")
    return lead, cand, rec


def test_users_register_independently_and_duplicate_email_rejected(web, gw, db):
    web().register("a@x.com")
    web().register("b@x.com")
    assert db.query(m.User).count() == 2
    r = web().post("/register", email="A@x.com", password="correct-horse-1")
    assert db.query(m.User).count() == 2 and r.status_code == 303 and r.headers["location"] == "/register"


def test_password_is_hashed(web, db):
    web().register("a@x.com", "correct-horse-1")
    u = db.query(m.User).one()
    assert u.password_hash.startswith("$argon2") and "correct-horse-1" not in u.password_hash


def test_login(web, db):
    web().register("a@x.com")
    w = web()
    assert w.post("/login", email="a@x.com", password="wrong").headers["location"] == "/login"
    assert w.post("/login", email="a@x.com", password="correct-horse-1").headers["location"] == "/onboarding"


def test_csrf_required(web):
    w = web()
    assert w.c.post("/register", data={"email": "a@x.com", "password": "correct-horse-1"}).status_code == 403


def test_unauthenticated_redirected(web):
    assert web().get("/leads").headers["location"] == "/login"


def test_per_user_templates_independent(web, gw, db):
    a, b = setup_users(web, gw)
    a.post("/settings/message", body="Hello {project_name}, A here")
    ta = db.query(m.MessageTemplate).filter_by(user_id=user_id(db, "a@x.com")).one()
    tb = db.query(m.MessageTemplate).filter_by(user_id=user_id(db, "b@x.com")).one()
    assert ta.body == "Hello {project_name}, A here" and ta.customized
    assert tb.body == DEFAULT_TEMPLATE and not tb.customized
    b.post("/settings/message", body="B wording {token_symbol}")
    db.expire_all()
    assert db.get(m.MessageTemplate, ta.id).body == "Hello {project_name}, A here"
    a.post("/settings/message/reset")
    db.expire_all()
    assert db.get(m.MessageTemplate, ta.id).body == DEFAULT_TEMPLATE
    assert db.get(m.MessageTemplate, tb.id).body == "B wording {token_symbol}"


def test_invalid_template_rejected(web, db):
    w = web().register("a@x.com")
    w.post("/settings/message", body="hi {bogus}")
    assert db.query(m.MessageTemplate).one().body == DEFAULT_TEMPLATE


def test_preview_endpoint(web):
    w = web().register("a@x.com")
    r = w.post("/settings/message/preview", body="Hi {project_name} on {blockchain}")
    assert r.json()["preview"] == "Hi MoonCat on solana"


def test_draft_uses_users_own_template_and_verified_data(web, gw, db):
    a, b = setup_users(web, gw)
    a.post("/settings/message", body="Yo {developer_username}, {project_name} on {blockchain} {project_website}")
    _, _, rec = make_draft(a, gw, db, "a@x.com", approve=False)
    assert rec.body == "Yo @dev_alice, Proj TokA on solana https://toka.example"
    _, _, recb = make_draft(b, gw, db, "b@x.com", approve=False)
    assert recb.body.startswith("Hey I came across Proj TokA on DexScreener")


def test_cross_user_leads_and_outreach_inaccessible(web, gw, db):
    a, b = setup_users(web, gw)
    lead, cand, rec = make_draft(a, gw, db, "a@x.com")
    assert "Proj TokA" not in b.get("/leads").text and "dev_alice" not in b.get("/outreach").text
    for path in (f"/leads/{lead.id}/candidates", f"/candidates/{cand.id}/draft", f"/outreach/{rec.id}/approve",
                 f"/outreach/{rec.id}/send", f"/outreach/{rec.id}/skip"):
        assert b.post(path).status_code == 404
    assert b.post(f"/outreach/{rec.id}/edit", body="hijack").status_code == 404
    db.expire_all()
    assert db.get(m.OutreachRecord, rec.id).status == "approved" and gw.sent == []


def test_service_layer_rejects_foreign_user_send(web, gw, db):
    a, b = setup_users(web, gw)
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    other = db.get(m.User, user_id(db, "b@x.com"))
    import pytest
    with pytest.raises(s.Forbidden):
        s.send_outreach(db, other, rec, gw)
    assert gw.sent == []


def test_db_composite_fk_blocks_cross_user_rows(web, gw, db):
    import pytest
    from sqlalchemy.exc import IntegrityError
    a, b = setup_users(web, gw)
    lead, cand, _ = make_draft(a, gw, db, "a@x.com", approve=False)
    with pytest.raises(IntegrityError):
        db.add(m.OutreachRecord(user_id=user_id(db, "b@x.com"), lead_id=lead.id, candidate_id=cand.id,
                                recipient="zzzz", body="x"))
        db.commit()


def test_telegram_sessions_isolated_and_encrypted(web, gw, db):
    a, b = setup_users(web, gw)
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    a.post(f"/outreach/{rec.id}/send")
    assert gw.sent[0][0] == "SESSION-+15550001"  # sent with A's session, not B's
    accts = {x.user_id: x for x in db.query(m.TelegramAccount)}
    assert "SESSION" not in accts[user_id(db, "a@x.com")].session_enc
    assert decrypt(accts[user_id(db, "b@x.com")].session_enc) == "SESSION-+15550002"
    for page in ("/settings/telegram", "/onboarding", "/outreach"):
        assert "SESSION-" not in a.get(page).text and "a" * 32 not in a.get(page).text
    creds_owner = db.get(m.User, user_id(db, "b@x.com"))
    import pytest
    with pytest.raises(s.Forbidden):
        s.creds_for(accts[user_id(db, "a@x.com")], creds_owner)


def test_disconnect_deletes_only_own_session(web, gw, db):
    a, b = setup_users(web, gw)
    a.post("/settings/telegram/disconnect")
    accts = {x.user_id: x for x in db.query(m.TelegramAccount)}
    mine, theirs = accts[user_id(db, "a@x.com")], accts[user_id(db, "b@x.com")]
    assert mine.session_enc is None and mine.status == "disconnected" and mine.api_hash_enc == ""
    assert theirs.session_enc and theirs.status == "connected"
    assert gw.logged_out == ["SESSION-+15550001"]


def test_send_requires_approval_and_prevents_duplicates(web, gw, db):
    a, _ = setup_users(web, gw)
    lead, cand, rec = make_draft(a, gw, db, "a@x.com", approve=False)
    a.post(f"/outreach/{rec.id}/send")
    assert gw.sent == []
    a.post(f"/outreach/{rec.id}/approve")
    a.post(f"/outreach/{rec.id}/send")
    a.post(f"/outreach/{rec.id}/send")  # second attempt is rejected
    assert len(gw.sent) == 1
    a.post(f"/candidates/{cand.id}/draft")  # duplicate draft rejected
    assert db.query(m.OutreachRecord).filter_by(user_id=user_id(db, "a@x.com")).count() == 1


def test_same_recipient_different_users_allowed(web, gw, db):
    a, b = setup_users(web, gw)
    make_draft(a, gw, db, "a@x.com")
    make_draft(b, gw, db, "b@x.com")
    assert db.query(m.OutreachRecord).count() == 2


def test_do_not_contact_enforced(web, gw, db):
    a, b = setup_users(web, gw)
    a.post("/dnc", username="@Dev_Alice")
    a.post("/leads/discover")
    lead = db.query(m.Lead).filter_by(user_id=user_id(db, "a@x.com"), chain="solana").one()
    a.post(f"/leads/{lead.id}/candidates")
    names = {c.username for c in db.query(m.Candidate).filter_by(user_id=user_id(db, "a@x.com"))}
    assert names == {"dev_bob"}  # excluded at discovery
    # B has no such exclusion
    _, _, rec = make_draft(b, gw, db, "b@x.com")
    assert rec.recipient == "dev_alice"


def test_dnc_added_after_approval_blocks_send(web, gw, db):
    a, _ = setup_users(web, gw)
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    a.post("/dnc", username="dev_alice")
    a.post(f"/outreach/{rec.id}/send")
    db.expire_all()
    assert gw.sent == [] and db.get(m.OutreachRecord, rec.id).status == "skipped"


def test_dnc_cannot_be_removed_by_other_user(web, gw, db):
    a, b = setup_users(web, gw)
    a.post("/dnc", username="someone")
    row = db.query(m.DoNotContact).one()
    assert b.post(f"/dnc/{row.id}/delete").status_code == 404
    assert db.query(m.DoNotContact).count() == 1


def test_rate_limits_are_per_user(web, gw, db):
    a, b = setup_users(web, gw)
    a.post("/settings/discovery", daily_limit="1", min_interval_seconds="60", discovery_limit="20",
           max_age_hours="72", min_liquidity_usd="0")
    _, cand, rec = make_draft(a, gw, db, "a@x.com")
    a.post(f"/outreach/{rec.id}/send")
    cb = db.query(m.Candidate).filter_by(user_id=user_id(db, "a@x.com"), username="dev_bob").one()
    a.post(f"/candidates/{cb.id}/draft")
    r2 = db.query(m.OutreachRecord).filter_by(recipient="dev_bob").one()
    a.post(f"/outreach/{r2.id}/approve")
    a.post(f"/outreach/{r2.id}/send")
    assert len(gw.sent) == 1  # A hit its limit
    _, _, recb = make_draft(b, gw, db, "b@x.com")
    b.post(f"/outreach/{recb.id}/send")
    assert len(gw.sent) == 2  # B unaffected


def test_paused_and_flood_wait(web, gw, db):
    a, _ = setup_users(web, gw)
    _, _, rec = make_draft(a, gw, db, "a@x.com")
    gw.flood = 120
    a.post(f"/outreach/{rec.id}/send")
    db.expire_all()
    acct = db.query(m.TelegramAccount).filter_by(user_id=user_id(db, "a@x.com")).one()
    assert acct.flood_wait_until is not None and db.get(m.OutreachRecord, rec.id).status == "held"
    gw.flood = None
    a.post(f"/outreach/{rec.id}/send")  # still blocked by recorded flood wait
    assert gw.sent == []


def test_discovery_isolated_between_users(web, gw, db):
    a, b = setup_users(web, gw)
    a.post("/leads/discover")
    assert db.query(m.Lead).filter_by(user_id=user_id(db, "b@x.com")).count() == 0
    a.post("/leads/discover")  # idempotent
    assert db.query(m.Lead).filter_by(user_id=user_id(db, "a@x.com")).count() == 2
