"""The one secrets refusal (docs/VOICE.md 4.9) lives in studio/sensitive.py, and every section uses it."""
from pathlib import Path

import pytest

from studio import sensitive
from studio.features import build, find, learn, research

VOICE_WORDING = ("This looks like it includes a password, PIN, code, card number or API key, so it was not saved or "
                 "sent to the AI. Please take it out: this site never needs them. If it is not one, change the "
                 "wording a little, for example \"the PIN box is not announced\", and send it again.")


def test_the_shared_wording_is_the_voice_guide_wording():
    assert sensitive.SECRET_MESSAGE == VOICE_WORDING
    assert sensitive.PUBLIC_SUFFIX == "Everyone can read this page."
    assert sensitive.PUBLIC_SECRET_MESSAGE == VOICE_WORDING + " Everyone can read this page."
    assert sensitive.API_KEY_MESSAGE == (VOICE_WORDING + " To use your own Claude API key, connect it on "
                                         "Your account.")


@pytest.mark.parametrize("text", [
    "password: hunter22",
    "my PIN 4821",
    "my password is Sunflower77",
    "the verification code 482913",
    "my OTP is 123456",
    "card 4111 1111 1111 1111",
    "key sk-proj-abcdefghijklmnopqrstuvwxyz",
])
def test_every_kind_of_secret_gets_the_same_message(text):
    assert sensitive.find_secret(text) == sensitive.SECRET_MESSAGE


def test_a_claude_api_key_also_says_where_a_key_belongs():
    assert sensitive.find_secret("here it is sk-ant-api03-Ab12Cd34Ef56") == sensitive.API_KEY_MESSAGE


def test_ordinary_text_is_not_refused():
    for text in ["", "The PIN box is not announced.", "My password is wrong.", "Step 1 of 12, 13 October 2026"]:
        assert sensitive.find_secret(text) is None, text


def test_sections_use_the_shared_constants():
    assert build.SECRET_MESSAGE is sensitive.SECRET_MESSAGE
    assert research.SECRET_MESSAGE is sensitive.SECRET_MESSAGE
    assert find.SECRET_MESSAGE is sensitive.SECRET_MESSAGE
    assert find.PUBLIC_SECRET_MESSAGE is sensitive.PUBLIC_SECRET_MESSAGE
    assert learn.SECRET_MESSAGE is sensitive.PUBLIC_SECRET_MESSAGE


def test_no_section_keeps_its_own_copy_of_the_wording():
    root = Path(sensitive.__file__).resolve().parent
    for path in root.rglob("*.py"):
        if path.name == "sensitive.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "this site never needs them" not in text, path
        assert "Everyone can read this page" not in text, path
        assert 'getattr(sensitive, "SECRET_MESSAGE"' not in text, path
