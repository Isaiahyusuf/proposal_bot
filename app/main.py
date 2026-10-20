import re
from datetime import timedelta

from email_validator import EmailNotValidError, validate_email
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import models as m
from . import health
from . import services as s
from .config import get_settings
from .db import get_db
from .dexscreener import DexScreenerClient
from .security import csrf_ok, hash_password, new_csrf_token, verify_password
from .telegram_service import PasswordRequired, TelegramError, TelegramRateLimited, get_gateway
from .templating import DEFAULT_TEMPLATE, SAMPLE_VALUES, SUPPORTED_VARIABLES, render_message, validate_template

app = FastAPI(title="Proposal Bot")
app.add_middleware(SessionMiddleware, secret_key=get_settings().secret_key, same_site="lax",
                   https_only=get_settings().cookie_secure, max_age=14 * 24 * 3600)
templates = Jinja2Templates(directory="app/templates")
STATUS_LABELS = {"delivery_failed": "Delivery Failed", "possible_block": "Possible Block (unconfirmed)",
                 "held": "Held (account paused)", "draft": "Draft", "approved": "Approved", "sent": "Sent",
                 "failed": "Failed (temporary)", "skipped": "Skipped", "sending": "Sending"}
from .discovery import CATEGORY_LABELS  # noqa: E402

templates.env.filters["category_label"] = lambda v: CATEGORY_LABELS.get(v, v)
templates.env.filters["status_label"] = lambda v: STATUS_LABELS.get(v, v)


class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _login_required(request, exc):
    return RedirectResponse("/login", status_code=303)


def get_dex():
    return DexScreenerClient()


def gateway():
    return get_gateway()


def current_user(request: Request, db: Session = Depends(get_db)) -> m.User:
    uid = request.session.get("uid")
    user = db.get(m.User, uid) if uid else None
    if user is None:
        raise LoginRequired()
    return user


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = new_csrf_token()
    return tok


async def verify_csrf(request: Request):
    form = await request.form()
    if not csrf_ok(request.session.get("csrf", ""), str(form.get("csrf_token", ""))):
        raise HTTPException(403, "Invalid CSRF token")


def render(request: Request, name: str, user=None, **kw):
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, {
        "csrf": csrf_token(request), "user": user, "flash": flash,
        "variables": SUPPORTED_VARIABLES, **kw})


def back(request: Request, to: str, msg: str = None, ok: bool = True):
    if msg:
        request.session["flash"] = {"msg": msg, "ok": ok}
    return RedirectResponse(to, status_code=303)


def owned(db, model, id_, user):
    row = db.get(model, id_)
    if row is None or row.user_id != user.id:
        raise HTTPException(404)
    return row


# ---------- auth ----------
@app.get("/")
def index(request: Request):
    return RedirectResponse("/onboarding" if request.session.get("uid") else "/login", status_code=303)


@app.get("/register")
def register_page(request: Request):
    return render(request, "register.html")


@app.post("/register", dependencies=[Depends(verify_csrf)])
def register(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    try:
        email = validate_email(email, check_deliverability=False).normalized
    except EmailNotValidError:
        return back(request, "/register", "Enter a valid email.", False)
    if len(password) < 10:
        return back(request, "/register", "Password must be at least 10 characters.", False)
    try:
        user = s.create_user(db, email, hash_password(password))
    except s.ServiceError as e:
        return back(request, "/register", str(e), False)
    request.session.clear()
    request.session["uid"] = user.id
    return back(request, "/onboarding", "Account created.")


@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login", dependencies=[Depends(verify_csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    cfg = get_settings()
    user = db.scalar(select(m.User).where(m.User.email == email.strip().lower()))
    bad = "Invalid email or password."
    if user is None:
        verify_password(hash_password("x"), password)  # equalize timing
        return back(request, "/login", bad, False)
    if user.locked_until and s.aware(user.locked_until) > s.utcnow():
        return back(request, "/login", "Too many attempts. Try again later.", False)
    if not verify_password(user.password_hash, password):
        user.failed_logins += 1
        if user.failed_logins >= cfg.max_failed_logins:
            user.locked_until, user.failed_logins = s.utcnow() + timedelta(minutes=cfg.lockout_minutes), 0
        db.commit()
        return back(request, "/login", bad, False)
    user.failed_logins, user.locked_until = 0, None
    db.commit()
    request.session.clear()
    request.session["uid"] = user.id
    return back(request, "/onboarding")


@app.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- onboarding ----------
@app.get("/onboarding")
def onboarding(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    acct = s.get_telegram(db, user)
    steps = [
        ("Create an account", True, None),
        ("Customize your opening message", s.get_template(db, user).customized, "/settings/message"),
        ("Connect your Telegram account", bool(acct and acct.status == "connected"), "/settings/telegram"),
        ("Configure discovery filters", s.get_prefs(db, user).configured, "/settings/discovery"),
        ("Discover projects", db.scalar(select(m.Lead.id).where(m.Lead.user_id == user.id).limit(1)) is not None,
         "/leads"),
        ("Review leads and approve outreach", db.scalar(select(m.OutreachRecord.id).where(
            m.OutreachRecord.user_id == user.id).limit(1)) is not None, "/outreach"),
    ]
    return render(request, "onboarding.html", user, steps=steps)


# ---------- message settings ----------
def _preview(body: str) -> str:
    return render_message(body, SAMPLE_VALUES)


@app.get("/settings/message")
def message_settings(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    t = s.get_template(db, user)
    return render(request, "message_settings.html", user, body=t.body, preview=_preview(t.body),
                  default_template=DEFAULT_TEMPLATE)


@app.post("/settings/message/preview", dependencies=[Depends(verify_csrf)])
def message_preview(body: str = Form(""), user=Depends(current_user)):
    err = validate_template(body)
    return JSONResponse({"preview": _preview(body), "error": err})


@app.post("/settings/message", dependencies=[Depends(verify_csrf)])
def message_save(request: Request, body: str = Form(...), user=Depends(current_user),
                 db: Session = Depends(get_db)):
    try:
        s.save_template(db, user, body)
    except s.ServiceError as e:
        return back(request, "/settings/message", str(e), False)
    return back(request, "/settings/message", "Message saved.")


@app.post("/settings/message/reset", dependencies=[Depends(verify_csrf)])
def message_reset(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    s.reset_template(db, user)
    return back(request, "/settings/message", "Default template restored.")


# ---------- telegram ----------
@app.get("/settings/telegram")
def telegram_page(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "telegram.html", user, acct=s.get_telegram(db, user))


def _tg_call(request, fn):
    try:
        fn()
        return None
    except PasswordRequired:
        return back(request, "/settings/telegram", "Two-step verification password required. Enter it below.", False)
    except TelegramRateLimited as e:
        return back(request, "/settings/telegram", str(e), False)
    except (TelegramError, s.ServiceError) as e:
        return back(request, "/settings/telegram", str(e), False)


@app.post("/settings/telegram/start", dependencies=[Depends(verify_csrf)])
def telegram_start(request: Request, api_id: int = Form(...), api_hash: str = Form(...), phone: str = Form(...),
                   user=Depends(current_user), db: Session = Depends(get_db), gw=Depends(gateway)):
    if not re.fullmatch(r"\+\d{7,15}", phone.strip()) or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash.strip()):
        return back(request, "/settings/telegram", "Check phone (+countrycode…) and API hash format.", False)
    err = _tg_call(request, lambda: s.start_telegram_login(db, user, gw, api_id, api_hash.strip(), phone.strip()))
    return err or back(request, "/settings/telegram", "Login code sent via Telegram.")


@app.post("/settings/telegram/verify", dependencies=[Depends(verify_csrf)])
def telegram_verify(request: Request, code: str = Form(...), password: str = Form(""),
                    user=Depends(current_user), db: Session = Depends(get_db), gw=Depends(gateway)):
    err = _tg_call(request, lambda: s.finish_telegram_login(db, user, gw, code.strip(), password or None))
    return err or back(request, "/settings/telegram", "Telegram connected.")


@app.post("/settings/telegram/disconnect", dependencies=[Depends(verify_csrf)])
def telegram_disconnect(request: Request, user=Depends(current_user), db: Session = Depends(get_db),
                        gw=Depends(gateway)):
    s.disconnect_telegram(db, user, gw)
    return back(request, "/settings/telegram", "Telegram disconnected and session deleted.")


# ---------- discovery preferences ----------
@app.get("/settings/discovery")
def discovery_page(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "discovery.html", user, p=s.get_prefs(db, user))


@app.post("/settings/discovery", dependencies=[Depends(verify_csrf)])
def discovery_save(request: Request, chains: str = Form(""), min_liquidity_usd: int = Form(0),
                   max_age_hours: int = Form(72), discovery_limit: int = Form(20),
                   daily_limit: int = Form(10), min_interval_seconds: int = Form(60),
                   auto_approve_drafts: bool = Form(False), sending_paused: bool = Form(False),
                   user=Depends(current_user), db: Session = Depends(get_db)):
    if not (0 <= daily_limit <= 50 and min_interval_seconds >= 30 and 1 <= discovery_limit <= 50
            and min_liquidity_usd >= 0 and 1 <= max_age_hours <= 24 * 365):
        return back(request, "/settings/discovery", "Value out of range (daily limit 0-50, interval ≥30s, "
                    "discovery 1-50).", False)
    p = s.get_prefs(db, user)
    p.chains = ",".join(c.strip().lower() for c in chains.split(",") if re.fullmatch(r"\s*[A-Za-z0-9_-]+\s*", c))
    p.min_liquidity_usd, p.max_age_hours, p.discovery_limit = min_liquidity_usd, max_age_hours, discovery_limit
    p.daily_limit, p.min_interval_seconds = daily_limit, min_interval_seconds
    p.auto_approve_drafts, p.sending_paused, p.configured = auto_approve_drafts, sending_paused, True
    db.commit()
    return back(request, "/settings/discovery", "Preferences saved.")


# ---------- leads ----------
@app.get("/leads")
def leads_page(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    leads = db.scalars(select(m.Lead).where(m.Lead.user_id == user.id).order_by(m.Lead.id.desc())).all()
    cands = db.scalars(select(m.Candidate).where(m.Candidate.user_id == user.id)).all()
    drafted = {r.recipient for r in db.scalars(select(m.OutreachRecord).where(m.OutreachRecord.user_id == user.id))}
    suppressed = {d.username for d in db.scalars(select(m.DoNotContact).where(m.DoNotContact.user_id == user.id))}
    body = s.get_template(db, user).body
    by_lead = {}
    for c in cands:
        if c.username in suppressed:  # never offered as an eligible target again
            continue
        lead = next(l for l in leads if l.id == c.lead_id)
        by_lead.setdefault(c.lead_id, []).append((c, s.personalize(body, lead, c), c.username in drafted))
    return render(request, "leads.html", user, leads=leads, by_lead=by_lead)


@app.post("/leads/discover", dependencies=[Depends(verify_csrf)])
def leads_discover(request: Request, user=Depends(current_user), db: Session = Depends(get_db),
                   dex=Depends(get_dex)):
    try:
        n = s.discover_leads(db, user, dex)
    except Exception:
        return back(request, "/leads", "DexScreener request failed. Try again shortly.", False)
    return back(request, "/leads", f"Found {n} new project(s).")


@app.post("/leads/{lead_id}/candidates", dependencies=[Depends(verify_csrf)])
def lead_candidates(request: Request, lead_id: int, user=Depends(current_user), db: Session = Depends(get_db),
                    gw=Depends(gateway)):
    lead = owned(db, m.Lead, lead_id, user)
    try:
        n = s.find_candidates(db, user, lead, gw)
    except s.ServiceError as e:
        return back(request, "/leads", str(e), False)
    return back(request, "/leads", f"Inspection complete: {n} new public contact(s) recorded. Telegram "
                "only exposes administrators and public signals; creator identity is shown only where Telegram "
                "provides it.")


@app.post("/leads/{lead_id}/contacts", dependencies=[Depends(verify_csrf)])
def lead_manual_contact(request: Request, lead_id: int, username: str = Form(...), note: str = Form(""),
                        user=Depends(current_user), db: Session = Depends(get_db)):
    lead = owned(db, m.Lead, lead_id, user)
    try:
        s.add_manual_contact(db, user, lead, username, note)
    except s.ServiceError as e:
        return back(request, "/leads", str(e), False)
    return back(request, "/leads", "Contact added (marked as user-supplied, not verified).")


@app.post("/candidates/{cid}/draft", dependencies=[Depends(verify_csrf)])
def candidate_draft(request: Request, cid: int, user=Depends(current_user), db: Session = Depends(get_db)):
    c = owned(db, m.Candidate, cid, user)
    try:
        s.create_draft(db, user, c)
    except s.ServiceError as e:
        return back(request, "/leads", str(e), False)
    return back(request, "/outreach", "Draft created. Review it before approving.")


# ---------- outreach ----------
@app.get("/outreach")
def outreach_page(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    recs = db.scalars(select(m.OutreachRecord).where(m.OutreachRecord.user_id == user.id)
                      .order_by(m.OutreachRecord.id.desc())).all()
    leads = {l.id: l for l in db.scalars(select(m.Lead).where(m.Lead.user_id == user.id))}
    cands = {c.id: c for c in db.scalars(select(m.Candidate).where(m.Candidate.user_id == user.id))}
    return render(request, "outreach.html", user, recs=recs, leads=leads, cands=cands, sent_today=s.sent_today(db, user),
                  prefs=s.get_prefs(db, user))


def _action(request, rec_id, user, db, fn, ok_msg):
    rec = owned(db, m.OutreachRecord, rec_id, user)
    try:
        fn(rec)
    except s.ServiceError as e:
        return back(request, "/outreach", str(e), False)
    return back(request, "/outreach", ok_msg)


@app.post("/outreach/{rid}/edit", dependencies=[Depends(verify_csrf)])
def outreach_edit(request: Request, rid: int, body: str = Form(...), user=Depends(current_user),
                  db: Session = Depends(get_db)):
    return _action(request, rid, user, db, lambda r: s.edit_outreach(db, user, r, body), "Saved. Re-approve to send.")


@app.post("/outreach/{rid}/approve", dependencies=[Depends(verify_csrf)])
def outreach_approve(request: Request, rid: int, user=Depends(current_user), db: Session = Depends(get_db)):
    return _action(request, rid, user, db, lambda r: s.approve(db, user, r), "Approved.")


@app.post("/outreach/{rid}/skip", dependencies=[Depends(verify_csrf)])
def outreach_skip(request: Request, rid: int, user=Depends(current_user), db: Session = Depends(get_db)):
    return _action(request, rid, user, db, lambda r: s.skip(db, user, r), "Skipped.")


@app.post("/outreach/{rid}/send", dependencies=[Depends(verify_csrf)])
def outreach_send(request: Request, rid: int, user=Depends(current_user), db: Session = Depends(get_db),
                  gw=Depends(gateway)):
    rec = owned(db, m.OutreachRecord, rid, user)
    try:
        rec = s.send_outreach(db, user, rec, gw)
    except s.ServiceError as e:
        return back(request, "/outreach", str(e), False)
    return back(request, "/outreach", "Message sent." if rec.status == "sent" else (rec.error or "Not sent."),
                rec.status == "sent")


# ---------- do not contact ----------
@app.get("/dnc")
def dnc_page(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(m.DoNotContact).where(m.DoNotContact.user_id == user.id)).all()
    return render(request, "dnc.html", user, rows=rows)


@app.post("/dnc", dependencies=[Depends(verify_csrf)])
def dnc_add(request: Request, username: str = Form(...), reason: str = Form(""), user=Depends(current_user),
            db: Session = Depends(get_db)):
    try:
        s.add_dnc(db, user, username, reason[:255] or None)
    except s.ServiceError as e:
        return back(request, "/dnc", str(e), False)
    return back(request, "/dnc", "Added.")


@app.post("/dnc/{did}/delete", dependencies=[Depends(verify_csrf)])
def dnc_delete(request: Request, did: int, user=Depends(current_user), db: Session = Depends(get_db)):
    owned(db, m.DoNotContact, did, user)
    try:
        s.remove_dnc(db, user, did)
    except s.ServiceError as e:
        return back(request, "/dnc", str(e), False)
    return back(request, "/dnc", "Removed.")


# ---------- account health ----------
@app.get("/account-health")
def health_page(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    info = health.describe(db, s.get_telegram(db, user), s.get_prefs(db, user))
    held = db.scalar(select(func.count()).select_from(m.OutreachRecord).where(
        m.OutreachRecord.user_id == user.id, m.OutreachRecord.status == "held")) or 0
    return render(request, "health.html", user, h=info, held=held)


@app.post("/account-health/check", dependencies=[Depends(verify_csrf)])
def health_check(request: Request, user=Depends(current_user), db: Session = Depends(get_db), gw=Depends(gateway)):
    acct = s.get_telegram(db, user)
    if acct is None:
        return back(request, "/account-health", "Connect your Telegram account first.", False)
    try:
        acct = health.run_check(db, acct, gw, s.creds_for(acct, user))
    except health.HealthError as e:
        return back(request, "/account-health", str(e), False)
    return back(request, "/account-health", f"Result: {health.LABELS[acct.health_state]}.",
                acct.health_state == health.HEALTHY)


# ---------- conversations and cleanup ----------
@app.get("/cleanup")
def cleanup_page(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    recs = db.scalars(select(m.OutreachRecord).where(
        m.OutreachRecord.user_id == user.id, m.OutreachRecord.status.in_(
            ["sent", "delivery_failed", "possible_block"])).order_by(m.OutreachRecord.id.desc())).all()
    sup = {d.username: d for d in db.scalars(select(m.DoNotContact).where(m.DoNotContact.user_id == user.id))}
    actions = db.scalars(select(m.CleanupAction).where(m.CleanupAction.user_id == user.id)
                         .order_by(m.CleanupAction.id.desc()).limit(50)).all()
    return render(request, "cleanup.html", user, recs=recs, sup=sup, actions=actions)


@app.post("/conversations/{rid}/mark", dependencies=[Depends(verify_csrf)])
def conversation_mark(request: Request, rid: int, outcome: str = Form(...), user=Depends(current_user),
                      db: Session = Depends(get_db)):
    rec = owned(db, m.OutreachRecord, rid, user)
    try:
        s.mark_contact(db, user, rec, outcome)
    except s.ServiceError as e:
        return back(request, "/cleanup", str(e), False)
    return back(request, "/cleanup", "Contact marked. All pending messages to them were cancelled.")


@app.post("/cleanup/review", dependencies=[Depends(verify_csrf)])
async def cleanup_review(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    recs = [owned(db, m.OutreachRecord, i, user) for i in ids]
    recs = [r for r in recs if r.status == "sent" and r.telegram_message_id and not r.deleted_at]
    if not recs:
        return back(request, "/cleanup", "Select at least one eligible sent message.", False)
    return render(request, "cleanup_review.html", user, recs=recs)


@app.post("/cleanup/confirm", dependencies=[Depends(verify_csrf)])
async def cleanup_confirm(request: Request, user=Depends(current_user), db: Session = Depends(get_db),
                          gw=Depends(gateway)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    for i in ids:
        owned(db, m.OutreachRecord, i, user)
    try:
        results = s.delete_sent_messages(db, user, ids, gw, revoke=form.get("revoke") == "yes",
                                         confirmed=form.get("confirm") == "yes")
    except s.ServiceError as e:
        return back(request, "/cleanup", str(e), False)
    ok = sum(1 for _, r, _ in results if r == "deleted")
    return back(request, "/cleanup", f"{ok} of {len(results)} message(s) deleted. See the log below.", ok == len(results))
