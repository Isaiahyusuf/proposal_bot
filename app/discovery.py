"""Classifies who might be the project's creator/developer from data Telegram and the project expose.

Hard rules (kept deliberately conservative):
- Telegram's admin list marks the *current* owner only. It is never reported as the original creator.
- The original creator is "confirmed" only when the group's creation service message names a user account.
- Admin titles are chosen by the group owner, so a "dev" title alone is NOT verification. A developer is
  "confirmed" only when that title is backed by the project's own published links.
- Nothing is invented: unresolved identity yields fewer candidates and a manual-review flag.
"""
import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from .telegram_service import GroupInspection

CONFIRMED_CREATOR, CONFIRMED_DEVELOPER = "confirmed_creator", "confirmed_developer"
POSSIBLE_OWNER, ADMINISTRATOR, UNVERIFIED = "possible_owner", "administrator", "unverified_contact"
CATEGORY_RANK = {CONFIRMED_CREATOR: 5, CONFIRMED_DEVELOPER: 4, POSSIBLE_OWNER: 3, ADMINISTRATOR: 2, UNVERIFIED: 1}
CATEGORY_LABELS = {CONFIRMED_CREATOR: "Confirmed creator", CONFIRMED_DEVELOPER: "Confirmed developer",
                   POSSIBLE_OWNER: "Possible owner", ADMINISTRATOR: "Administrator",
                   UNVERIFIED: "Unverified contact"}
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
CONFIRMED = (CONFIRMED_CREATOR, CONFIRMED_DEVELOPER)

_DEV_TITLE = re.compile(r"\b(dev|devs|developer|engineer|programmer|coder|cto|tech lead|smart contract)\b", re.I)
_OWNER_TITLE = re.compile(r"\b(owner|founder|co-?founder|creator|ceo)\b", re.I)
_USERNAME = re.compile(r"^[A-Za-z0-9_]{4,32}$")
_MENTION = re.compile(r"(?<![\w/])@([A-Za-z0-9_]{4,32})\b|t(?:elegram)?\.me/([A-Za-z0-9_]{4,32})\b", re.I)
_RESERVED = {"joinchat", "addstickers", "share", "proxy", "socks", "c", "s", "iv", "login", "setlanguage"}


@dataclass
class CandidateInfo:
    username: str
    category: str
    role: str
    evidence: str
    confidence: str
    verification_status: str


@dataclass
class DiscoveryResult:
    candidates: List[CandidateInfo]
    notes: List[str]
    needs_manual_review: bool
    unmessageable: int  # people identified who have no public username


def usernames_from_links(links: List[str], exclude: Optional[str] = None) -> Set[str]:
    out = set()
    for link in links or []:
        for m_ in re.finditer(r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z0-9_]{4,32})\b", link or "", re.I):
            out.add(m_.group(1).lower())
    out -= _RESERVED
    if exclude:
        out.discard(exclude.lower())
    return out


def load_links(raw: Optional[str]) -> List[str]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [x for x in data if isinstance(x, str)]


def _mentions(text: Optional[str]) -> Set[str]:
    found = set()
    for a, b in _MENTION.findall(text or ""):
        name = (a or b).lower()
        if name not in _RESERVED:
            found.add(name)
    return found


def _usable(name: Optional[str]) -> bool:
    return bool(name) and bool(_USERNAME.match(name)) and not name.lower().endswith("bot")


def classify(insp: GroupInspection, official_links: List[str], group_username: Optional[str] = None) -> DiscoveryResult:
    own = (group_username or insp.username or "").lower() or None
    official = usernames_from_links(official_links, exclude=own)
    notes = list(insp.notes)
    hits: Dict[str, List[CandidateInfo]] = {}
    unmessageable = 0

    def add(username, category, role, evidence, confidence, verification):
        hits.setdefault(username.lower(), []).append(
            CandidateInfo(username.lower(), category, role, evidence, confidence, verification))

    if insp.creation:
        if _usable(insp.creation.username):
            when = f" on {insp.creation.date:%Y-%m-%d}" if insp.creation.date else ""
            add(insp.creation.username, CONFIRMED_CREATOR, "Creator (group creation message)",
                f"The group's creation service message{when} was posted by this account. They may no longer "
                "be the owner.", "high", "verified")
        else:
            unmessageable += 1
            notes.append("The group's creation message names an account without a public username, "
                         "so it cannot be messaged.")
    else:
        notes.append("Telegram does not expose the original group creator here. The current owner is not assumed "
                     "to be the creator.")

    for a in insp.admins:
        if a.is_bot or a.is_deleted:
            continue
        if not _usable(a.username):
            unmessageable += 1
            continue
        name, title = a.username.lower(), (a.rank or "").strip()
        dev_title, owner_title = bool(_DEV_TITLE.search(title)), bool(_OWNER_TITLE.search(title))
        in_official = name in official
        if a.is_owner:
            add(name, POSSIBLE_OWNER, "Current owner (Telegram admin list)"
                + (f", title '{title}'" if title else ""),
                "Listed with Telegram's owner role. The current owner is not necessarily the original creator.",
                "medium", "unverified")
        if dev_title and in_official:
            add(name, CONFIRMED_DEVELOPER, f"Admin, title '{title}'",
                f"Admin title '{title}' (set by the group owner) and @{name} is linked from the project's own "
                "official links.", "high", "verified")
        elif dev_title:
            add(name, ADMINISTRATOR, f"Admin, title '{title}'",
                f"Admin with self-assigned title '{title}'. Titles are set by the group owner and are unverified.",
                "medium", "unverified")
        elif owner_title and not a.is_owner:
            add(name, POSSIBLE_OWNER, f"Admin, title '{title}'",
                f"Admin with title '{title}', set by the group owner and unverified.", "medium", "unverified")
        elif not a.is_owner:
            add(name, ADMINISTRATOR, "Admin" + (f", title '{title}'" if title else ""),
                "Listed as an administrator in the group. No role is verified.", "low", "unverified")
        if in_official and not dev_title:
            add(name, UNVERIFIED, "Linked from official project links",
                "Their Telegram link is published in the project's official links.", "medium", "unverified")

    for source, text in (("group description", insp.about), ("pinned message", insp.pinned_text)):
        for name in sorted(_mentions(text)):
            if name == own or not _usable(name):
                continue
            add(name, UNVERIFIED, f"Mentioned in {source}",
                f"@{name} is mentioned in the {source}. It may be a person, channel or service, and no role is stated.",
                "low", "unverified")
    for name in sorted(official):
        if _usable(name) and name not in hits:
            add(name, UNVERIFIED, "Linked from official project links",
                "Their Telegram link is published in the project's official links. It may be a channel rather "
                "than a person, and no role is stated.", "low", "unverified")

    candidates = [_merge(v) for v in hits.values()]
    candidates.sort(key=lambda c: (-CATEGORY_RANK[c.category], -CONFIDENCE_RANK[c.confidence], c.username))
    if not insp.admins_visible:
        notes.append("Administrator list not visible to this account; only other public signals were used.")
    if not any(c.category in CONFIRMED for c in candidates):
        notes.append("No confirmed creator or developer found: review manually before contacting anyone.")
    if not candidates:
        notes.append("No messageable public contacts were found.")
    return DiscoveryResult(candidates, notes, not any(c.category in CONFIRMED for c in candidates), unmessageable)


def _merge(items: List[CandidateInfo]) -> CandidateInfo:
    best = max(items, key=lambda c: (CATEGORY_RANK[c.category], CONFIDENCE_RANK[c.confidence]))
    roles = list(dict.fromkeys(i.role for i in items))
    evidence = list(dict.fromkeys(i.evidence for i in items))
    conf = max((i.confidence for i in items), key=lambda x: CONFIDENCE_RANK[x])
    verified = "verified" if any(i.verification_status == "verified" for i in items) else "unverified"
    return CandidateInfo(best.username, best.category, "; ".join(roles)[:255], " ".join(evidence), conf, verified)
