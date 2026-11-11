"""Instagram and Threads can be ONE account. This module says when they are.

WHY IT EXISTS. Threads has no logins of its own: it signs in with an
Instagram account. This app keeps the two in separate session pools (they
are separate sites with separate cookies), so without this the tool saw two
accounts where Meta sees one -- an Instagram warning left Threads working
at full speed on the same account, and the two could run at the same time,
which reads as one account on two devices.

HOW TWO SESSIONS ARE KNOWN TO BE THE SAME ACCOUNT. Both sites set a
`ds_user_id` cookie carrying the signed-in account's numeric id. Equal ids
are the same account; that is the only evidence used. A session with no
`ds_user_id` is linked to nothing -- never guessed at.

WHAT IS SHARED REGARDLESS OF ACCOUNTS. The pacing governor
(shared/pushback.py): Instagram and Threads are one company's
infrastructure, and every session leaves from this host's one IP, so a
throttle on either is evidence about both -- the same reasoning that already
makes one platform's governor shared across all of its accounts.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# The platforms that sign in with an Instagram account.
META_PLATFORMS = ("instagram", "threads")


def is_meta(platform_id: str) -> bool:
    return (platform_id or "").strip().lower() in META_PLATFORMS


def siblings(platform_id: str) -> tuple[str, ...]:
    """The OTHER Meta platforms a session on `platform_id` may share an
    account with."""
    p = (platform_id or "").strip().lower()
    return tuple(x for x in META_PLATFORMS if x != p) if p in META_PLATFORMS else ()


def _cookie(cookies: Optional[Iterable[Any]], name: str) -> str:
    for c in cookies or ():
        if isinstance(c, dict) and c.get("name") == name:
            return str(c.get("value") or "").strip()
    return ""


def account_key(platform_id: str, cookies: Optional[Iterable[Any]]) -> str:
    """"meta:<id>" for a Meta session whose cookies name its account, else
    "". Two sessions with the same non-empty key are one account."""
    if not is_meta(platform_id):
        return ""
    uid = _cookie(cookies, "ds_user_id")
    return f"meta:{uid}" if uid.isdigit() else ""


def budget_key(platform_id: str, session_id: str, cookies: Optional[Iterable[Any]]) -> str:
    """The key an hourly budget is counted under: the real account when it
    is known, so Instagram and Threads on one account share one count;
    otherwise this one session."""
    return account_key(platform_id, cookies) or f"{platform_id}:{session_id or 'anonymous'}"
