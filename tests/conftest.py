import os
import re
import tempfile

from cryptography.fernet import Fernet

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models as m  # noqa: E402
from app.db import get_engine, get_sessionmaker  # noqa: E402
from app.main import app, gateway, get_dex  # noqa: E402
from app.telegram_service import Creds  # noqa: E402


class FakeGateway:
    """Scripted Telegram. Set `send_error`/`spambot_reply`/`spambot_error` to simulate Telegram."""

    def __init__(self):
        self.sent = []          # (session, username, text)
        self.admins = ["dev_alice", "dev_bob"]
        self.inspection = None      # explicit GroupInspection to return
        self.inspect_error = None
        self.flood = None
        self.send_error = None      # exception raised by send_message
        self.spambot_reply = None   # text returned by check_spam_status
        self.spambot_error = None
        self.spambot_calls = []     # sessions used for checks
        self.deleted = []           # (session, username, ids, revoke)
        self.delete_error = None
        self.logged_out = []
        self._next_id = 100

    def send_code(self, creds, phone):
        return "hash-" + phone, "tmp-session-" + phone

    def sign_in(self, creds, phone, code, phone_code_hash, password):
        assert code == "12345" and phone_code_hash == "hash-" + phone
        return "SESSION-" + phone

    def inspect_group(self, creds, link):
        from app.telegram_service import AdminInfo, GroupInspection
        self.inspected = getattr(self, "inspected", []) + [(creds.session, link)]
        if self.inspect_error:
            raise self.inspect_error
        if self.inspection is not None:
            return self.inspection
        # default: dev_alice carries a developer title; everyone else is a plain admin
        return GroupInspection(title="Proj", username=None, admins=[
            AdminInfo(i, n, False, "Lead Dev" if n == "dev_alice" else None) for i, n in enumerate(self.admins, 1)])

    def send_message(self, creds, username, text):
        from app.telegram_service import TelegramRateLimited
        if self.flood:
            raise TelegramRateLimited(self.flood)
        if self.send_error:
            raise self.send_error
        self.sent.append((creds.session, username, text))
        self._next_id += 1
        return self._next_id

    def delete_messages(self, creds, username, ids, revoke):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append((creds.session, username, list(ids), revoke))

    def check_spam_status(self, creds):
        from app.telegram_service import SpamBotReply
        self.spambot_calls.append(creds.session)
        if self.spambot_error:
            raise self.spambot_error
        return SpamBotReply(self.spambot_reply)

    def log_out(self, creds):
        self.logged_out.append(creds.session)


class FakeDex:
    def latest_profiles(self):
        return [{"chainId": "solana", "tokenAddress": "TokA", "links": [{"type": "telegram", "url": "https://t.me/proja"},
                                                         {"type": "telegram", "url": "https://t.me/dev_alice"}]},
                {"chainId": "ethereum", "tokenAddress": "TokB"}]

    def token_pairs(self, chain, address):
        return [{"baseToken": {"name": "Proj " + address, "symbol": "P" + address[-1]},
                 "liquidity": {"usd": 5000}, "url": "https://dexscreener.com/x",
                 "info": {"websites": [{"url": "https://" + address.lower() + ".example"}]}}]


@pytest.fixture(autouse=True)
def fresh_db():
    m.Base.metadata.drop_all(get_engine())
    m.Base.metadata.create_all(get_engine())
    yield


@pytest.fixture
def db():
    s = get_sessionmaker()()
    yield s
    s.close()


@pytest.fixture
def gw():
    g = FakeGateway()
    app.dependency_overrides[gateway] = lambda: g
    app.dependency_overrides[get_dex] = lambda: FakeDex()
    yield g
    app.dependency_overrides.clear()


class Web:
    def __init__(self):
        self.c = TestClient(app, follow_redirects=False)

    def token(self):
        html = self.c.get("/login").text
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def post(self, path, **data):
        return self.c.post(path, data={"csrf_token": self.token(), **data})

    def get(self, path):
        return self.c.get(path)

    def register(self, email, password="correct-horse-1"):
        r = self.post("/register", email=email, password=password)
        assert r.status_code == 303 and r.headers["location"] == "/onboarding", r.text
        return self

    def connect_telegram(self, phone="+15550001"):
        self.post("/settings/telegram/start", api_id="123", api_hash="a" * 32, phone=phone)
        self.post("/settings/telegram/verify", code="12345")
        return self


@pytest.fixture
def web():
    return Web


def user_id(db, email):
    return db.query(m.User).filter_by(email=email).one().id
