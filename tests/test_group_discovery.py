from datetime import datetime, timezone

import pytest
from telethon import types

from app import discovery as d
from app import models as m
from app import services as s
from app.telegram_service import (AdminInfo, CreationEvent, DeliveryFailure, GroupInaccessible, GroupInspection,
                                  ensure_private_user)
from tests.conftest import user_id
from tests.test_multiuser import make_draft, setup_users


def admin(name, uid, owner=False, rank=None, **kw):
    return AdminInfo(uid, name, owner, rank, **kw)


def by_name(result):
    return {c.username: c for c in result.candidates}


# ---------- pure classification ----------
def test_creator_confirmed_only_from_creation_message():
    insp = GroupInspection("P", "proja", creation=CreationEvent(1, "Founder_X", datetime(2026, 1, 2, tzinfo=timezone.utc),
                                                                "chat_create"),
                           admins=[admin("owner_y", 2, owner=True)])
    r = by_name(d.classify(insp, []))
    assert r["founder_x"].category == d.CONFIRMED_CREATOR and r["founder_x"].verification_status == "verified"
    assert "creation service message" in r["founder_x"].evidence and "2026-01-02" in r["founder_x"].evidence
    assert r["owner_y"].category == d.POSSIBLE_OWNER and r["owner_y"].verification_status == "unverified"


def test_missing_creator_info_never_invents_identity():
    insp = GroupInspection("P", "proja", admins=[admin("owner_y", 2, owner=True), admin("mod_zed", 3)])
    res = d.classify(insp, [])
    assert not any(c.category == d.CONFIRMED_CREATOR for c in res.candidates)
    assert res.needs_manual_review
    assert any("does not expose the original group creator" in n for n in res.notes)
    owner = by_name(res)["owner_y"]
    assert owner.category == d.POSSIBLE_OWNER and "not necessarily the original creator" in owner.evidence


def test_current_owner_is_not_promoted_even_with_owner_title():
    insp = GroupInspection("P", "proja", admins=[admin("owner_y", 2, owner=True, rank="Founder")])
    assert by_name(d.classify(insp, []))["owner_y"].category == d.POSSIBLE_OWNER


def test_ambiguous_admin_roles_stay_unverified():
    insp = GroupInspection("P", "proja", admins=[admin("dev_one", 1, rank="Dev"), admin("founder_two", 2, rank="Co-Founder"),
                                                 admin("plain_three", 3), admin("cto_four", 4, rank="CTO")])
    r = by_name(d.classify(insp, []))
    assert r["dev_one"].category == d.ADMINISTRATOR and r["dev_one"].confidence == "medium"
    assert "unverified" in r["dev_one"].evidence
    assert r["founder_two"].category == d.POSSIBLE_OWNER and r["founder_two"].verification_status == "unverified"
    assert r["plain_three"].category == d.ADMINISTRATOR and r["plain_three"].confidence == "low"
    assert all(c.verification_status == "unverified" for c in r.values())


def test_developer_verified_only_with_official_link_backing():
    insp = GroupInspection("P", "proja", admins=[admin("dev_one", 1, rank="Lead Developer"),
                                                 admin("dev_two", 2, rank="Developer")])
    r = by_name(d.classify(insp, ["https://t.me/proja", "https://t.me/Dev_One", "https://x.com/proj"]))
    assert r["dev_one"].category == d.CONFIRMED_DEVELOPER and r["dev_one"].verification_status == "verified"
    assert r["dev_one"].confidence == "high"
    assert r["dev_two"].category == d.ADMINISTRATOR  # same title, no independent proof


def test_other_public_contacts_from_description_pinned_and_links():
    insp = GroupInspection("P", "proja", about="Official. Support: @help_person, https://t.me/proja", pinned_text="Contact t.me/biz_lead",
                           admins=[])
    r = by_name(d.classify(insp, ["https://t.me/proja", "https://t.me/from_links"]))
    assert set(r) == {"help_person", "biz_lead", "from_links"}
    assert all(c.category == d.UNVERIFIED and c.confidence == "low" for c in r.values())
    assert "description" in r["help_person"].evidence and "pinned" in r["biz_lead"].evidence


def test_bots_deleted_and_usernameless_accounts_excluded():
    insp = GroupInspection("P", "proja", admins=[admin("helper_bot", 1, is_bot=True), admin("gone_user", 2, is_deleted=True),
                                                 AdminInfo(3, None, False, "Dev"), admin("real_one", 4)],
                           about="join @some_bot")
    res = d.classify(insp, [])
    assert set(by_name(res)) == {"real_one"} and res.unmessageable == 1


def test_duplicate_signals_merge_into_one_candidate():
    insp = GroupInspection("P", "proja", about="ask @dev_one", creation=CreationEvent(1, "dev_one", None, "chat_create"),
                           admins=[admin("dev_one", 1, owner=True, rank="Developer")])
    res = d.classify(insp, ["https://t.me/dev_one"])
    assert [c.username for c in res.candidates] == ["dev_one"]
    c = res.candidates[0]
    assert c.category == d.CONFIRMED_CREATOR and "Current owner" in c.role and "description" in c.role


def test_inaccessible_admin_list_is_reported():
    insp = GroupInspection("P", "proja", admins_visible=False, about="Reach @helper_main",
                           notes=["Telegram did not let this account read the administrator list."])
    res = d.classify(insp, [])
    assert res.needs_manual_review and any("not visible" in n for n in res.notes)
    assert set(by_name(res)) == {"helper_main"}


def test_channel_or_bot_entities_are_never_message_targets():
    ensure_private_user(types.User(id=1, first_name="x"))
    for bad in (types.Channel(id=1, title="c", photo=types.ChatPhotoEmpty(), date=None),
                types.User(id=2, bot=True), types.User(id=3, deleted=True)):
        with pytest.raises(DeliveryFailure) as e:
            ensure_private_user(bad)
        assert e.value.kind == "invalid_recipient"


# ---------- workflow (HTTP + services) ----------
def inspect_lead(w, db, email, chain="solana"):
    w.post("/leads/discover")
    lead = db.query(m.Lead).filter_by(user_id=user_id(db, email), chain=chain).one()
    w.post(f"/leads/{lead.id}/candidates")
    db.expire_all()
    return db.get(m.Lead, lead.id)


def test_inaccessible_group_flags_lead_for_manual_review(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    gw.inspect_error = GroupInaccessible("Telegram says this group is not accessible (ChannelPrivateError).")
    lead = inspect_lead(a, db, "a@x.com")
    assert lead.scan_status == "inaccessible" and lead.needs_manual_review
    assert db.query(m.Candidate).count() == 0
    page = a.get("/leads").text
    assert "Group not accessible" in page and "ChannelPrivateError" in page


def test_missing_creator_flags_review_but_keeps_public_contacts(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    gw.inspection = GroupInspection("P", "proja", admins=[admin("owner_y", 1, owner=True), admin("mod_zed", 2)])
    lead = inspect_lead(a, db, "a@x.com")
    assert lead.scan_status == "scanned" and lead.needs_manual_review
    cats = {c.username: c.category for c in db.query(m.Candidate)}
    # dev_alice comes only from the project's own links: listed, but with no role claimed
    assert cats == {"owner_y": "possible_owner", "mod_zed": "administrator", "dev_alice": "unverified_contact"}
    assert not any(v in ("confirmed_creator", "confirmed_developer") for v in cats.values())
    page = a.get("/leads").text
    assert "Manual review needed" in page and "Evidence:" in page and "Possible owner" in page


def test_verified_developer_is_listed_with_evidence_and_username_variable(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    a.post("/settings/message", body="Hi {developer_username}, about {project_name}")
    lead = inspect_lead(a, db, "a@x.com")  # default fake: dev_alice has "Lead Dev" + official link
    alice = db.query(m.Candidate).filter_by(username="dev_alice").one()
    bob = db.query(m.Candidate).filter_by(username="dev_bob").one()
    assert alice.category == "confirmed_developer" and alice.verification_status == "verified"
    assert not lead.needs_manual_review
    assert s.personalize("Hi {developer_username}", lead, alice) == "Hi @dev_alice"
    assert s.personalize("Hi {developer_username}", lead, bob) == "Hi"  # unverified: variable not filled


def test_review_page_shows_project_recipient_evidence_and_message(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    lead, cand, rec = make_draft(a, gw, db, "a@x.com", approve=False)
    page = a.get("/outreach").text
    assert "Proj TokA" in page and "@dev_alice" in page and "Confirmed developer" in page
    assert "Evidence:" in page and "Lead Dev" in page and "Hey I came across Proj TokA on DexScreener" in page
    assert gw.sent == []  # nothing sent without approval


def test_unverified_recipient_shows_warning_before_approval(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    lead = inspect_lead(a, db, "a@x.com")
    bob = db.query(m.Candidate).filter_by(username="dev_bob").one()
    a.post(f"/candidates/{bob.id}/draft")
    assert "role is not verified" in a.get("/outreach").text


def test_sending_requires_approval_and_uses_own_session(web, gw, db):
    a, b = setup_users(web, gw)
    _, _, rec = make_draft(a, gw, db, "a@x.com", approve=False)
    a.post(f"/outreach/{rec.id}/send")
    assert gw.sent == []
    assert b.post(f"/outreach/{rec.id}/approve").status_code == 404
    a.post(f"/outreach/{rec.id}/approve")
    b.post(f"/outreach/{rec.id}/send")  # not B's record
    assert gw.sent == []
    a.post(f"/outreach/{rec.id}/send")
    assert [x[0] for x in gw.sent] == ["SESSION-+15550001"]


def test_duplicate_contact_across_projects_gets_one_message(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    lead, cand, rec = make_draft(a, gw, db, "a@x.com")
    other = db.query(m.Lead).filter_by(user_id=user_id(db, "a@x.com"), chain="ethereum").one()
    other.telegram_link = "https://t.me/projb"
    db.commit()
    a.post(f"/leads/{other.id}/candidates")
    same = db.query(m.Candidate).filter_by(lead_id=other.id, username="dev_alice").one()  # same person, other project
    a.post(f"/outreach/{rec.id}/send")
    a.post(f"/candidates/{same.id}/draft")
    assert db.query(m.OutreachRecord).filter_by(recipient="dev_alice").count() == 1
    assert len(gw.sent) == 1


def test_rescan_does_not_duplicate_and_is_rate_limited(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    lead = inspect_lead(a, db, "a@x.com")
    n = db.query(m.Candidate).count()
    a.post(f"/leads/{lead.id}/candidates")  # immediate re-scan is refused
    assert len(gw.inspected) == 1
    lead.scanned_at = None
    db.commit()
    a.post(f"/leads/{lead.id}/candidates")
    assert db.query(m.Candidate).count() == n and len(gw.inspected) == 2


def test_opt_out_contact_is_not_rediscovered_and_stays_stopped(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    lead, cand, rec = make_draft(a, gw, db, "a@x.com")
    a.post(f"/conversations/{rec.id}/mark", outcome="declined")
    db.query(m.Candidate).delete()
    db.query(m.Lead).update({"scanned_at": None})
    db.commit()
    a.post(f"/leads/{lead.id}/candidates")
    assert "dev_alice" not in {c.username for c in db.query(m.Candidate)}


def test_manual_contact_is_user_supplied_not_verified(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    a.post("/leads/discover")
    lead = db.query(m.Lead).filter_by(chain="solana").one()
    a.post(f"/leads/{lead.id}/contacts", username="@Some_Dev", note="listed on their website")
    c = db.query(m.Candidate).one()
    assert c.username == "some_dev" and c.source == "manual" and c.verification_status == "user_supplied"
    assert c.category == "unverified_contact" and "listed on their website" in c.evidence
    a.post("/dnc", username="blocked_person")
    a.post(f"/leads/{lead.id}/contacts", username="blocked_person")
    assert db.query(m.Candidate).count() == 1


def test_candidates_and_inspection_isolated_between_users(web, gw, db):
    a, b = setup_users(web, gw)
    lead_a = inspect_lead(a, db, "a@x.com")
    assert "dev_alice" not in b.get("/leads").text
    assert b.post(f"/leads/{lead_a.id}/candidates").status_code == 404
    assert b.post(f"/leads/{lead_a.id}/contacts", username="intruder").status_code == 404
    assert db.query(m.Candidate).filter_by(user_id=user_id(db, "b@x.com")).count() == 0
    assert [x[0] for x in gw.inspected] == ["SESSION-+15550001"]


def test_inspection_blocked_while_account_paused(web, gw, db):
    a = web().register("a@x.com").connect_telegram()
    acct = db.query(m.TelegramAccount).one()
    acct.health_state = "restricted"
    db.commit()
    inspect_lead(a, db, "a@x.com")
    assert getattr(gw, "inspected", []) == []


def test_transport_error_does_not_start_scan_cooldown(web, gw, db):
    from app.telegram_service import TelegramError
    a = web().register("a@x.com").connect_telegram()
    a.post("/leads/discover")
    lead = db.query(m.Lead).filter_by(chain="solana").one()
    gw.inspect_error = TelegramError("network hiccup")
    a.post(f"/leads/{lead.id}/candidates")
    db.refresh(lead)
    assert lead.scanned_at is None
    gw.inspect_error = None
    a.post(f"/leads/{lead.id}/candidates")  # allowed straight away: the first lookup never completed
    assert db.query(m.Candidate).count() > 0


def test_project_name_always_rendered_falls_back_to_symbol():
    from app.templating import DEFAULT_TEMPLATE
    lead = m.Lead(project_name="  ", token_symbol="MCAT", chain="solana")
    out = s.personalize(DEFAULT_TEMPLATE, lead, None)
    assert "MCAT" in out and "{" not in out
    lead.project_name = "MoonCat"
    assert "came across MoonCat on DexScreener" in s.personalize(DEFAULT_TEMPLATE, lead, None)


def test_gateway_connects_without_interactive_start(monkeypatch):
    """Regression: `async with TelegramClient` calls start(), which prompts on stdin and hangs login."""
    from app.telegram_service import Creds, TelethonGateway
    events = []

    class FakeClient:
        session = type("S", (), {"save": staticmethod(lambda: "sess")})()

        async def connect(self): events.append("connect")
        async def disconnect(self): events.append("disconnect")
        async def start(self, *a, **k): raise AssertionError("start() must never be called")
        async def __aenter__(self): return await self.start()
        async def send_code_request(self, phone):
            return type("Sent", (), {"phone_code_hash": "h"})()

    gw = TelethonGateway()
    monkeypatch.setattr(gw, "_client", lambda creds: FakeClient())
    assert gw.send_code(Creds(1, "hash"), "+15550001") == ("h", "sess")
    assert events == ["connect", "disconnect"]
