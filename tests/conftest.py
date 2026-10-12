"""Test fixtures. The AI is replaced by FakeAI, which returns canned answers per `purpose`."""
from __future__ import annotations

import copy
import re

import pytest
from fastapi.testclient import TestClient

from studio import ai as ai_module
from studio.app import create_app
from studio.config import Settings

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class FakeAI:
    """Stands in for studio.ai.AIClient. Register answers with .on(purpose, value_or_callable)."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.responses: dict[str, object] = {}

    def on(self, purpose: str, value) -> "FakeAI":
        self.responses[purpose] = value
        return self

    def _answer(self, kind: str, purpose: str, kwargs: dict):
        self.calls.append((kind, purpose, kwargs))
        if purpose not in self.responses:
            raise AssertionError(f"FakeAI has no answer registered for purpose {purpose!r}")
        value = self.responses[purpose]
        if isinstance(value, Exception):
            raise value
        return value(**kwargs) if callable(value) else copy.deepcopy(value)

    def structured(self, *, purpose: str, **kwargs):
        return self._answer("structured", purpose, kwargs)

    def text(self, *, purpose: str, **kwargs):
        return self._answer("text", purpose, kwargs)

    def research(self, *, purpose: str, **kwargs):
        return self._answer("research", purpose, kwargs)

    def purposes(self) -> list[str]:
        return [p for _, p, _ in self.calls]


@pytest.fixture
def fake_ai() -> FakeAI:
    return FakeAI()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(secret_key="test-secret-key", db_path=tmp_path / "test.db", jobs_inline=True,
                    base_url="http://testserver")


@pytest.fixture
def app(settings, fake_ai):
    application = create_app(settings)
    application.state.ai_factory = lambda user: fake_ai
    return application


@pytest.fixture
def no_ai_app(settings):
    """An app where no AI is available, to test the explanation people get."""
    application = create_app(settings)

    def factory(user):
        raise ai_module.AINotAvailable("To use the AI, connect your own Claude API key on your account page.")

    application.state.ai_factory = factory
    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def csrf(client: TestClient) -> str:
    """The session's CSRF token (it is the same for every form in a session)."""
    # Signed out, /login shows a form; signed in, it redirects home, where the sign-out form has the token.
    page = client.get("/login")
    match = CSRF_RE.search(page.text)
    assert match, "no csrf token on page"
    return match.group(1)


def post(client: TestClient, url: str, data: dict | None = None, **kwargs):
    """POST a form with the CSRF token added."""
    payload = dict(data or {})
    payload["csrf_token"] = csrf(client)
    return client.post(url, data=payload, **kwargs)


def register(client: TestClient, email: str = "amina@example.com", name: str = "Amina") -> None:
    response = post(client, "/register", {"email": email, "display_name": name, "password": "correct horse battery",
                                          "screen_reader": "NVDA", "device": "Windows laptop"})
    assert response.status_code == 200, response.text


@pytest.fixture
def user_client(client) -> TestClient:
    register(client)
    return client


def user_id(email: str = "amina@example.com") -> int:
    from studio import db
    with db.session() as conn:
        return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
