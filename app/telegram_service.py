"""Telegram integration. Each call uses the calling user's own credentials/session only.

Limitations (Telegram API, not this app): admin lists are only readable for groups/channels the
connected account can see; many admins have no public username and cannot be messaged; members'
privacy settings may block messages; flood waits and PeerFlood limits apply. No account rotation
or limit-bypass logic is implemented, by design.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Protocol, Tuple


class TelegramError(Exception):
    pass


class TelegramRateLimited(TelegramError):
    def __init__(self, seconds: int):
        super().__init__(f"Telegram flood wait: retry in {seconds}s")
        self.seconds = seconds


class PasswordRequired(TelegramError):
    pass


class AccountRestricted(TelegramError):
    """Account-level restriction reported by Telegram (PeerFlood, deactivated/banned account)."""
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class SessionInvalid(TelegramError):
    """The stored session is no longer valid; the user must reconnect."""


class TemporaryError(TelegramError):
    """Network/transport problem. Says nothing about the account or recipient."""


class DeliveryFailure(TelegramError):
    """Recipient-specific failure. kind: privacy | invalid_recipient | possible_block | ambiguous."""
    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class GroupInaccessible(TelegramError):
    """The group could not be inspected with this account (private, invalid, banned, not resolvable)."""


@dataclass
class AdminInfo:
    user_id: int
    username: Optional[str]
    is_owner: bool          # Telegram's current-owner role; NOT proof of original creator
    rank: Optional[str]     # custom title chosen by the group owner (unverified)
    is_bot: bool = False
    is_deleted: bool = False


@dataclass
class CreationEvent:
    user_id: int
    username: Optional[str]
    date: Optional[datetime]
    kind: str  # chat_create | channel_create


@dataclass
class GroupInspection:
    title: Optional[str]
    username: Optional[str]
    about: Optional[str] = None
    pinned_text: Optional[str] = None
    admins: List[AdminInfo] = field(default_factory=list)
    admins_visible: bool = True
    creation: Optional[CreationEvent] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class SpamBotReply:
    text: Optional[str]  # None when SpamBot did not answer in time


@dataclass
class Creds:
    api_id: int
    api_hash: str
    session: str = ""


class TelegramGateway(Protocol):
    def send_code(self, creds: Creds, phone: str) -> Tuple[str, str]: ...  # (phone_code_hash, session)
    def sign_in(self, creds: Creds, phone: str, code: str, phone_code_hash: str,
                password: Optional[str]) -> str: ...  # returns session string
    def inspect_group(self, creds: Creds, group_link: str) -> GroupInspection: ...
    def send_message(self, creds: Creds, username: str, text: str) -> Optional[int]: ...  # message id
    def delete_messages(self, creds: Creds, username: str, message_ids: List[int], revoke: bool) -> None: ...
    def check_spam_status(self, creds: Creds) -> SpamBotReply: ...
    def log_out(self, creds: Creds) -> None: ...


class TelethonGateway:
    def _run(self, coro):
        return asyncio.run(coro)

    def _client(self, creds: Creds):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        return TelegramClient(StringSession(creds.session or None), creds.api_id, creds.api_hash)

    def send_code(self, creds, phone):
        async def go():
            async with self._client(creds) as c:
                sent = await c.send_code_request(phone)
                return sent.phone_code_hash, c.session.save()
        return self._wrap(go)

    def sign_in(self, creds, phone, code, phone_code_hash, password):
        from telethon.errors import SessionPasswordNeededError

        async def go():
            async with self._client(creds) as c:
                try:
                    await c.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                except SessionPasswordNeededError:
                    if not password:
                        raise PasswordRequired("Two-step verification password required.")
                    await c.sign_in(password=password)  # sent to Telegram only; never stored
                return c.session.save()
        return self._wrap(go)

    def inspect_group(self, creds, group_link):
        """Read-only inspection using only what this account is already allowed to see.
        It never joins a group. Anything Telegram does not expose is reported, not guessed."""
        from telethon import errors as E
        from telethon import functions, types

        async def go():
            async with self._client(creds) as c:
                try:
                    entity = await c.get_entity(group_link)
                except (E.ChannelPrivateError, E.ChannelInvalidError, E.InviteHashExpiredError,
                        E.InviteHashInvalidError, E.UsernameNotOccupiedError, E.UsernameInvalidError,
                        E.ChannelBannedError) as e:
                    raise GroupInaccessible(f"Telegram says this group is not accessible ({type(e).__name__}).")
                except ValueError:
                    raise GroupInaccessible("This link could not be resolved. Private invite links are only "
                                            "inspectable if the account is already a member.")
                if not isinstance(entity, (types.Channel, types.Chat)):
                    raise GroupInaccessible("This link points to a user or bot, not a group or channel.")
                insp = GroupInspection(title=getattr(entity, "title", None),
                                       username=getattr(entity, "username", None))

                async def soft(coro, note):
                    try:
                        return await coro
                    except (E.FloodWaitError, E.AuthKeyUnregisteredError, E.SessionRevokedError,
                            E.UserDeactivatedBanError, E.PeerFloodError):
                        raise
                    except Exception as e:  # noqa: BLE001
                        insp.notes.append(f"{note} ({type(e).__name__}).")
                        return None

                if isinstance(entity, types.Channel):
                    full = await soft(c(functions.channels.GetFullChannelRequest(entity)), "Group description unavailable")
                else:
                    full = await soft(c(functions.messages.GetFullChatRequest(entity.id)), "Group description unavailable")
                if full is not None:
                    insp.about = getattr(full.full_chat, "about", None)

                async def admins():
                    out = []
                    async for u in c.iter_participants(entity, filter=types.ChannelParticipantsAdmins):
                        part = getattr(u, "participant", None)
                        owner = isinstance(part, (types.ChannelParticipantCreator, types.ChatParticipantCreator))
                        is_admin = isinstance(part, (types.ChannelParticipantAdmin, types.ChatParticipantAdmin))
                        if not (owner or is_admin):
                            continue  # basic groups return every member; keep only owner/admin roles
                        out.append(AdminInfo(u.id, u.username, owner, getattr(part, "rank", None),
                                             bool(u.bot), bool(u.deleted)))
                    return out
                got = await soft(admins(), "Administrator list unavailable")
                if got is None:
                    insp.admins_visible = False
                    insp.notes.append("Telegram did not let this account read the administrator list.")
                else:
                    insp.admins = got

                msg = await soft(c.get_messages(entity, ids=1), "Group creation message unavailable")
                action = getattr(msg, "action", None)
                if isinstance(action, (types.MessageActionChatCreate, types.MessageActionChannelCreate)):
                    sender = await soft(msg.get_sender(), "Creation message sender unavailable")
                    if isinstance(sender, types.User):
                        insp.creation = CreationEvent(
                            sender.id, sender.username, msg.date,
                            "chat_create" if isinstance(action, types.MessageActionChatCreate) else "channel_create")

                pinned = await soft(c.get_messages(entity, limit=1, filter=types.InputMessagesFilterPinned),
                                    "Pinned message unavailable")
                if pinned:
                    insp.pinned_text = (pinned[0].raw_text or "")[:1000]
                return insp
        return self._wrap(go)

    def send_message(self, creds, username, text):
        async def go():
            async with self._client(creds) as c:
                entity = await c.get_entity(username)
                ensure_private_user(entity)  # never post into a channel/group the user happens to admin
                msg = await c.send_message(entity, text)
                return getattr(msg, "id", None)
        return self._wrap(go)

    def delete_messages(self, creds, username, message_ids, revoke):
        async def go():
            async with self._client(creds) as c:
                await c.delete_messages(username, message_ids, revoke=revoke)
        return self._wrap(go)

    def check_spam_status(self, creds):
        """Ask Telegram's official @SpamBot for the account's status (a normal /start chat)."""
        async def go():
            async with self._client(creds) as c:
                if not await c.is_user_authorized():
                    raise SessionInvalid("Telegram session is no longer authorized.")
                try:
                    async with c.conversation("SpamBot", timeout=30) as conv:
                        await conv.send_message("/start")
                        resp = await conv.get_response()
                        return SpamBotReply(resp.raw_text)
                except asyncio.TimeoutError:
                    return SpamBotReply(None)
        return self._wrap(go)

    def log_out(self, creds):
        async def go():
            async with self._client(creds) as c:
                await c.log_out()
        try:
            self._wrap(go)
        except TelegramError:
            pass

    def _wrap(self, factory):
        try:
            return self._run(factory())
        except TelegramError:
            raise
        except Exception as e:  # noqa: BLE001 - classified below
            raise classify_exception(e)


def ensure_private_user(entity) -> None:
    from telethon import types
    if not isinstance(entity, types.User) or entity.bot or entity.deleted:
        raise DeliveryFailure("invalid_recipient", "This username is not a person's account "
                              "(it is a channel, group, bot or deleted account), so nothing was sent.")


def classify_exception(e: Exception) -> TelegramError:
    """Map Telethon errors to distinct categories. Only explicit Telegram signals count as
    restrictions/blocks; anything unrecognized is 'ambiguous' and never treated as confirmed."""
    from telethon import errors as E
    if isinstance(e, E.FloodWaitError):
        return TelegramRateLimited(int(e.seconds))
    if isinstance(e, E.PeerFloodError):
        return AccountRestricted("Telegram returned PEER_FLOOD: this account is limited in contacting "
                                 "people who are not contacts. Run Check Status for details.")
    if isinstance(e, (E.UserDeactivatedBanError, E.UserDeactivatedError, E.PhoneNumberBannedError)):
        return AccountRestricted(f"Telegram reports the account is deactivated or banned ({type(e).__name__}).")
    if isinstance(e, (E.AuthKeyUnregisteredError, E.SessionRevokedError, E.SessionExpiredError,
                      E.AuthKeyDuplicatedError)):
        return SessionInvalid("The Telegram session is no longer valid. Reconnect your account.")
    if isinstance(e, E.UserPrivacyRestrictedError):
        return DeliveryFailure("privacy", "The recipient's privacy settings do not allow this message.")
    if isinstance(e, (E.UsernameNotOccupiedError, E.UsernameInvalidError, E.InputUserDeactivatedError,
                      E.UserIdInvalidError, E.PeerIdInvalidError)):
        return DeliveryFailure("invalid_recipient", "Telegram says this recipient does not exist or is deactivated.")
    if isinstance(e, E.UserIsBlockedError):
        return DeliveryFailure("possible_block", "Telegram returned USER_IS_BLOCKED. This may mean the recipient "
                               "blocked you, but Telegram does not definitively confirm it.")
    if isinstance(e, E.YouBlockedUserError):
        return DeliveryFailure("ambiguous", "You have blocked this Telegram user.")
    if isinstance(e, (E.PhoneCodeInvalidError, E.PhoneCodeExpiredError, E.PasswordHashInvalidError)):
        return TelegramError("Invalid or expired login code/password.")
    if isinstance(e, E.RPCError):
        return DeliveryFailure("ambiguous", f"Telegram returned {type(e).__name__}; the cause is not conclusive.")
    if isinstance(e, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)):
        return TemporaryError(f"Could not reach Telegram ({type(e).__name__}).")
    if isinstance(e, ValueError):
        return DeliveryFailure("invalid_recipient", "Could not resolve this Telegram user or link.")
    return TemporaryError(f"Unexpected Telegram client error ({type(e).__name__}).")


_gateway: TelegramGateway = TelethonGateway()


def get_gateway() -> TelegramGateway:
    return _gateway
