"""Parsing and formatting helpers shared by every platform adapter's
extraction code (payload parsing, name-match scoring, date formatting).
Ported unchanged from `backend/core/text.py`.

Lives in `shared/` because every platform's discovery/analysis code depends
on it, and none of them owns it.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterator, Optional
from urllib.parse import ParseResult, urlparse

try:
    from rapidfuzz.fuzz import token_set_ratio as _tsr
    HAVE_RF = True
except ImportError:
    HAVE_RF = False

# oldest plausible timestamp, anything before this is not a real post date
EPOCH_FLOOR = 1075593600

MONTHS = (
    "January February March April May June July August September "
    "October November December"
).split()


def iter_dicts(obj: Any, depth: int = 0) -> Iterator[dict]:
    if depth > 45:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_dicts(v, depth + 1)
    elif isinstance(obj, list):
        for it in obj:
            yield from iter_dicts(it, depth + 1)


def iter_kv(obj: Any, depth: int = 0) -> Iterator[tuple[str, Any]]:
    if depth > 45:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from iter_kv(v, depth + 1)
    elif isinstance(obj, list):
        for it in obj:
            yield from iter_kv(it, depth + 1)


def find_ints(text: str, keys) -> list[int]:
    out = []
    for k in keys:
        out += [
            int(m.group(1)) for m in re.finditer(rf'"{k}"\s*:\s*(\d{{1,13}})', text)
        ]
    return out


# A dotted thousands group, the way most non-English locales render a
# follower count: "1.234.567". Every group after the first is exactly three
# digits and there is no K/M/B suffix, so an English "1.2K" can never match
# this and the two readings never collide.
_DOTTED_THOUSANDS = re.compile(r"^\d{1,3}(?:\.\d{3})+$")


def parse_count(raw: str):
    r"""(value, is_exact) for a scraped count, or (None, False) if it is not
    one. TOTAL: never raises, whatever the page put in front of it.

    The `float()` here used to be unguarded, and every caller feeds it raw
    scraped text -- Facebook's own RE_FOLLOWERS/RE_CHIP capture `[\d.,\s]+`,
    and TikTok passes DOM strings through untouched. So a page rendering
    "1.234.567 followers" (German, Spanish, Portuguese, Indonesian, ... --
    Facebook serves that to a large share of locales) raised an uncaught
    ValueError and took the whole visit down. Now such a count is READ
    rather than merely survived; anything genuinely unparseable ("1.2.3")
    returns None, which every caller already handles.
    """
    s = re.sub(r"[\s,]", "", raw.strip())
    if _DOTTED_THOUSANDS.match(s):
        return int(s.replace(".", "")), True
    m = re.match(r"^([\d.]+)([KMB])?$", s, re.I)
    if not m:
        return None, False
    try:
        val = float(m.group(1))
    except ValueError:
        return None, False
    suf = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suf]
    return int(val * mult), (suf == "" and "." not in m.group(1))


def epoch_to_dt(ts: Any) -> Optional[datetime]:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    if ts > 10**12:
        ts //= 1000
    if not (EPOCH_FLOOR < ts < int(time.time()) + 86400):
        return None
    return datetime.fromtimestamp(ts, timezone.utc)


def parse_joined(raw: str) -> str:
    """'June 2025' | 'July 16, 2026' -> ISO 'YYYY-MM' or 'YYYY-MM-DD'."""
    raw = raw.strip().rstrip(".")
    for fmt, out in (
        ("%B %d, %Y", "%Y-%m-%d"),
        ("%B %d %Y", "%Y-%m-%d"),
        ("%b %d, %Y", "%Y-%m-%d"),
        ("%B %Y", "%Y-%m"),
        ("%b %Y", "%Y-%m"),
    ):
        try:
            return datetime.strptime(raw, fmt).strftime(out)
        except ValueError:
            continue
    return ""


def fmt_created(iso: str) -> str:
    """ISO -> 'Jun-25' (month-year) or 'DD-MM-YYYY' when a day is known."""
    if not iso:
        return ""
    try:
        if len(iso) == 7:
            dt = datetime.strptime(iso, "%Y-%m")
            return f"{MONTHS[dt.month - 1][:3]}-{dt.strftime('%y')}"
        dt = datetime.strptime(iso, "%Y-%m-%d")
        return dt.strftime("%d-%m-%Y")
    except ValueError:
        return iso


def parse_normalized_url(url: str, extra_schemes: tuple[str, ...] = ()) -> Optional[ParseResult]:
    """Strip whitespace/quotes and default to an https:// scheme; the
    common preamble every platform's own `normalize_url()` builds on before
    applying its own host canonicalization and path formatting. Returns
    None for an empty input, so callers can early-return ""."""
    url = (url or "").strip().strip("\"'")
    if not url:
        return None
    if not url.startswith(("http://", "https://", *extra_schemes)):
        url = "https://" + url
    return urlparse(url)


def normalized_host(parsed: ParseResult) -> str:
    return parsed.netloc.lower().split(":")[0]


# Path segments that introduce an id or a page type rather than being a
# handle themselves -- `facebook.com/profile.php?id=N`, `t.me/c/<internal
# id>`, `youtube.com/channel/UC...`, `facebook.com/groups/<id>`. For these
# the URL simply does not carry a handle and callers fall back to the id.
_NON_HANDLE_SEGMENTS = frozenset({
    "profile.php", "people", "pages", "groups", "channel", "c", "watch",
    "video", "reel", "reels", "p", "story", "stories", "hashtag", "explore",
    "search", "share",
})


def handle_from_url(url: str) -> str:
    """The public handle a profile URL carries, or "" when it carries none.

        https://www.instagram.com/defnce.app/     -> "defnce.app"
        https://x.com/CyfirmaDev                  -> "CyfirmaDev"
        https://www.tiktok.com/@someone           -> "someone"
        https://www.youtube.com/user/LegacyName   -> "LegacyName"
        https://www.facebook.com/llaudreyisabelcc -> "llaudreyisabelcc"
        https://www.facebook.com/profile.php?id=6 -> ""   (id, not a handle)
        https://t.me/c/8925111777                 -> ""   (internal id)
        https://www.youtube.com/channel/UCabc     -> ""   (channel id)

    WHY THIS EXISTS. Discovery stores a `username` per profile, but `Row`
    only ever carried `profile_id` -- so every platform's rows were saved
    with the numeric/opaque id in the username field, and the UI rendered
    a real handle as "@50840430092". Platforms whose search payload names
    the handle now set `Row.username` directly; this recovers it for the
    ones that don't (Facebook and YouTube go through `Hit`, which has no
    handle field at all) from the URL we already store.
    """
    parsed = parse_normalized_url(url)
    if parsed is None:
        return ""
    segments = [seg for seg in parsed.path.split("/") if seg]
    if not segments:
        return ""
    first = segments[0]
    # `youtube.com/@handle`, `tiktok.com/@handle`
    if first.startswith("@"):
        return first[1:].strip()
    # `youtube.com/user/<legacy handle>` -- the handle is the NEXT segment
    if first.lower() == "user":
        return segments[1].strip() if len(segments) > 1 else ""
    if first.lower() in _NON_HANDLE_SEGMENTS:
        return ""
    return first.strip()


def is_place(v: str) -> bool:
    """Reject JSON fragments and other debris that key-scraping drags in."""
    v = (v or "").strip()
    return (
        bool(v)
        and len(v) < 70
        and not re.search(r'[\[\]{}"\\]', v)
        and bool(re.search(r"[A-Za-z]", v))
    )


TOKEN_MATCH = 0.82  # how close two tokens must be to count as the same


def _norm_name(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


def _covers(target_token: str, candidate_tokens: set[str]) -> bool:
    """Is this target token present in the candidate, typos allowed?

    Fuzzy on purpose: impersonators misspell deliberately ("Gautamm Adani"),
    and an exact-token rule would score those lowest when they should score
    highest.
    """
    return any(
        t == target_token
        or SequenceMatcher(None, target_token, t).ratio() >= TOKEN_MATCH
        for t in candidate_tokens
    )


def name_score(candidate: str, target: str) -> int:
    """0-100 similarity, weighted by how much of the target is accounted for.

    Token-set alone is not enough: it rates "Gautam" against "Gautam Adani" a
    perfect 100, because one is a subset of the other. Scaling by target
    coverage fixes it while keeping word order irrelevant:

        Adani Gautam          -> 100   (same words, reordered)
        Gautam Adani Official -> 100   (target fully covered, extra words ok)
        Gautamm Adani         ->  ~95  (typo-squat: still a match)
        Gautam                ->   50  (half the target: not a match)
    """
    a, b = _norm_name(candidate), _norm_name(target)
    if not a or not b:
        return 0

    ta, tb = set(a.split()), set(b.split())
    if HAVE_RF:
        base = float(_tsr(a, b))
    else:
        inter = ta & tb
        overlap = 100 * len(inter) / max(min(len(ta), len(tb)), 1)
        seq = (
            100
            * SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
        )
        base = max(overlap, seq)

    coverage = sum(_covers(t, ta) for t in tb) / len(tb)
    return int(base * coverage)


def _letters_only(s: str) -> str:
    """Lowercase, every character that is not a letter or digit stripped
    (not just collapsed to a space, REMOVED), so "Gautam Adani",
    "gautam.adani", "GAUTAM_ADANI", and "GautamAdani" all normalize to the
    identical `gautamadani` -- whatever separator (or none) an impersonator
    happened to put between the words disappears before the comparison
    ever runs."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def contiguous_letters_match(candidate: str, keyword: str) -> bool:
    """True High Match: `keyword`'s letters/digits appear in `candidate`, in
    that exact order, with nothing else interleaved between them -- a real
    contiguous run, not just "the same words somewhere in some order" the
    way token-set based `name_score` counts a match.

        candidate="GautamAdaniOfficial", keyword="Gautam Adani"   -> True
        candidate="gautam.adani.fan.page", keyword="Gautam Adani" -> True
        (both punctuation-stripped down to the same "gautamadani" run)
        candidate="Adani Gautam", keyword="Gautam Adani"          -> False
        (same two words, but reversed -- not a contiguous run of the
        keyword's own letter order)
        candidate="Gautam A", keyword="Gautam Adani"              -> False
        (candidate doesn't contain the keyword's full letter run)

    Deliberately simple substring containment over fuzzy scoring: an
    analyst asking for "High Match" wants a literal, explainable reason a
    profile qualifies, not a threshold on a fuzzy ratio that can't be
    pointed at. Medium/Low stay on the existing name_score bands for
    everything that doesn't clear this bar.
    """
    cand, kw = _letters_only(candidate), _letters_only(keyword)
    return bool(kw) and bool(cand) and kw in cand
