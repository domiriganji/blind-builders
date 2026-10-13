"""One detector for passwords, PINs, one-time codes, API keys and card numbers in a member's text.

The site never asks for, stores or sends these (docs/DEVELOPING.md), so every section that sends a member's
words to the AI refuses text that looks like one, with the same rules everywhere.
"""
from __future__ import annotations

import re

# The one wording for every refusal (docs/VOICE.md, 4.9). It allows for the detector being wrong, because
# accessibility reports say "PIN" and "password" all the time. Add PUBLIC_SUFFIX where the text would be public.
SECRET_MESSAGE = ("This looks like it includes a password, PIN, code, card number or API key, so it was not saved or "
                  "sent to the AI. Please take it out: this site never needs them. If it is not one, change the "
                  "wording a little, for example \"the PIN box is not announced\", and send it again.")
PUBLIC_SUFFIX = " Everyone can read this page."

_SECRET_WORD = r"(?:passwords?|passwd|passcodes?|passphrases?|pin(?:\s*(?:code|number))?)"
# "password: x", "PIN = 1234"
_SECRET_ASSIGN = re.compile(rf"\b{_SECRET_WORD}\s*[:=]\s*\S", re.IGNORECASE)
# "my password is Tr0ub4dor", "passcode was 'seagull'" (but not "my password is wrong")
_SECRET_IS = re.compile(rf"\b{_SECRET_WORD}\s+(?:is|was)\s+(?:[\"'\u2018\u201c](?P<quoted>[^\"'\u2019\u201d]+)|(?P<word>\S+))",
                        re.IGNORECASE)
_SECRET_VALUE_CHARS = re.compile(r"[\d!@#$%^&*()+=?<>{}\[\]|\\/~`_:;]")
# "PIN 4821", "my pin number 123456"
_PIN_DIGITS = re.compile(r"\bpin(?:\s*(?:code|number))?\s*(?:-\s*)?\d{4,8}\b", re.IGNORECASE)
# "my password Sunflower77": a secret word straight before a word with both letters and digits
_SECRET_BARE = re.compile(rf"\b{_SECRET_WORD}\s+(?=\S*[^\W\d_])(?=\S*\d)\S{{4,}}", re.IGNORECASE)
# "the code they sent me is 482913", "they texted me a code: 4829"
_SENT_CODE = re.compile(
    r"\bcode\b[^.\n]{0,40}?\b(?:sent|texted|emailed|got|received)\b[^.\n]{0,30}?\b\d{4,8}\b"
    r"|\b(?:sent|texted|emailed|got|received)\b[^.\n]{0,30}?\bcode\b[^.\n]{0,30}?\b\d{4,8}\b", re.IGNORECASE)
# Codes that are not secrets: "discount code 1234", "zip code 90210", "tracking code ..."
_HARMLESS_CODE = re.compile(
    r"\b(?:discount|promo|promotion|voucher|coupon|gift|zip|post|area|dialling|dialing|country|tracking|"
    r"reference|product|error|tax|course|booking|order|sort)\s+code\b", re.IGNORECASE)
# "verification code 482913", "security code: 123", "text code is 12 34 56"
_ONE_TIME = re.compile(
    r"\b(?:one[- ]time|verification|security|authentication|authenticator|auth|login|log-in|sign-in|sms|text|access)"
    r"\s*(?:code|pin|passcode|password)\s*(?:is|was|:|=|-)?\s*"
    r"(?:\d{3}[\s-]\d{3}\b|(?=[A-Za-z0-9-]{3,})[A-Za-z0-9-]*\d[A-Za-z0-9-]*)", re.IGNORECASE)
# "my OTP is 123456", "2FA: 4821"
_OTP_WORD = re.compile(r"\b(?:otp|2fa|mfa)\b\D{0,20}\d{4,8}\b", re.IGNORECASE)
_API_KEY = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}|\bsk-[A-Za-z0-9_-]{20,}")
_DIGIT_GROUPS = re.compile(r"\d+(?:[ \-]\d+)*")


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _has_card_number(text: str) -> bool:
    """True if the text has 13 to 19 digits that pass the Luhn check, written together or in groups of three
    or more (only the last group may be shorter). Dates, phone numbers and numbered steps are not refused."""
    for run in _DIGIT_GROUPS.finditer(text):
        groups = re.split(r"[ \-]", run.group(0))
        for start in range(len(groups)):
            if len(groups[start]) < 3:
                continue
            joined = ""
            for group in groups[start:]:
                joined += group
                if len(joined) > 19:
                    break
                if len(joined) >= 13 and luhn_ok(joined):
                    return True
                if len(group) < 3:
                    break
    return False


def find_secret(text: str) -> str | None:
    """A plain message if the text looks like it holds a password, a PIN, a one-time code, an API key or a
    card number, otherwise None."""
    if not text:
        return None
    if _API_KEY.search(text):
        return ("This looks like it includes an API key. Please take it out. Connect your Claude API key on "
                "your account page instead; the AI must not see it.")
    if _SECRET_ASSIGN.search(text) or _PIN_DIGITS.search(text) or _SECRET_BARE.search(text):
        return ("This looks like it includes a password or PIN. Please take it out. "
                "This site never needs your passwords, and the AI must not see them.")
    for m in _SECRET_IS.finditer(text):
        value = m.group("quoted") or (m.group("word") or "").rstrip(".,;!?")
        if m.group("quoted") or _SECRET_VALUE_CHARS.search(value):
            return ("This looks like it includes a password or PIN. Please take it out. "
                    "This site never needs your passwords, and the AI must not see them.")
    if (_ONE_TIME.search(text) or _OTP_WORD.search(text)
            or (_SENT_CODE.search(text) and not _HARMLESS_CODE.search(text))):
        return ("This looks like it includes a one-time code or security code. Please take it out. "
                "This site never needs your codes, and the AI must not see them.")
    if _has_card_number(text):
        return ("This looks like it includes a card number. Please take it out. "
                "This site never needs your card details, and the AI must not see them.")
    return None
