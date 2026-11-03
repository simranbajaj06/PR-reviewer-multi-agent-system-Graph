"""Decides whether a review-thread comment is addressed to the bot.

Pure functions, no I/O -- easy to unit-test.

Rules
-----
* The comment must contain an @mention of the bot (e.g. ``@pr-reviewer``).
* A comment that mentions only *someone else* (``@alice can you check?``) is
  NOT answered -- it is a conversation between humans.
* A comment with no mention at all is NOT answered.
* Mentions inside code spans / code blocks / quoted lines (``> ...``) and
  e-mail addresses are not real mentions and are ignored.
* If the bot AND other people are mentioned, the bot answers by default
  (set ``ignore_if_others_mentioned=True`` to stay silent instead).
* The text used to answer ("the required text") is the comment with the bot's
  own @mention removed.
"""
import re
from dataclasses import dataclass

_CODE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)
_QUOTE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)
# @user, @org/team. Not preceded by a word char (e-mails), '@' or '/'.
_MENTION = re.compile(
    r"(?<![\w@/])@([A-Za-z0-9][A-Za-z0-9-]{0,38}(?:/[A-Za-z0-9_.-]+)?)(?![\w-])"
)


def normalize_login(login: str) -> str:
    """'PR-Reviewer[bot]' -> 'pr-reviewer' (GitHub logins are case-insensitive)."""
    login = (login or "").strip().lstrip("@").lower()
    if login.endswith("[bot]"):
        login = login[: -len("[bot]")]
    return login.rstrip("-")


def same_login(a: str, b: str) -> bool:
    return normalize_login(a) == normalize_login(b)


def _strip_non_prose(text: str) -> str:
    return _QUOTE.sub("", _CODE.sub("", text))


def extract_mentions(text: str) -> list[str]:
    """All distinct @mentions in prose (normalized, in order of appearance)."""
    seen: list[str] = []
    for m in _MENTION.finditer(_strip_non_prose(text or "")):
        login = normalize_login(m.group(1))
        if login and login not in seen:
            seen.append(login)
    return seen


@dataclass(frozen=True)
class MentionDecision:
    should_respond: bool
    reason: str
    question: str = ""


def decide(
    body: str,
    bot_username: str,
    ignore_if_others_mentioned: bool = False,
) -> MentionDecision:
    bot = normalize_login(bot_username)
    mentions = extract_mentions(body)

    if not mentions:
        return MentionDecision(False, "no @mention in comment")

    others = [m for m in mentions if m != bot]
    if bot not in mentions:
        return MentionDecision(False, f"addressed to someone else (@{', @'.join(others)})")
    if others and ignore_if_others_mentioned:
        return MentionDecision(False, f"also mentions @{', @'.join(others)}; staying silent")

    question = _MENTION.sub(
        lambda m: "" if normalize_login(m.group(1)) == bot else m.group(0), body
    )
    question = re.sub(r"[ \t]+([,.:;!?])", r"\1", question)   # "Hey , x" -> "Hey, x"
    question = re.sub(r"[ \t]{2,}", " ", question).strip(" \t,:;-\n")
    if not question:
        return MentionDecision(False, "bot mentioned but no question was asked")
    return MentionDecision(True, "bot mentioned", question)
