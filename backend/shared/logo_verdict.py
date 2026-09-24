"""One rule for "does this account use a picture it chose?", however many
places in the pipeline have an opinion about it.

WHY THIS EXISTS. Four different stages look at a profile picture, and each
sees different evidence:

    discovery sweep    the search payload: a URL, sometimes the platform's
                       own no-picture flag (Instagram, X)
    avatar cache       the downloaded bytes: default-avatar hash match,
                       Facebook/YouTube generated letter avatars
    analysis visit     the profile payload: the platform flag again, a
                       URL, or (Facebook) whatever the page rendered
    the analyst        a hand edit, which is `sources.logo = manual`

They used to settle disagreements by ORDER rather than by evidence:
analysis always inherited discovery's verdict (so Instagram's own
`has_anonymous_profile_picture` read during analysis was thrown away in
favour of a URL guess made during search), and every re-save of a
discovered profile wrote the URL rule's answer over the pixel check's (so a
Facebook letter avatar the avatar cache had caught flipped straight back to
"Yes" the next time any keyword found the same profile).

THE RULE: the stronger evidence wins; on a tie, "placeholder" wins. A
confirmed stock avatar is the cheap, safe direction to err in -- a false
"Yes" is the single most expensive mistake the risk rubric can make (a logo
alone forces High priority, see Row.priority), while a false "No" costs one
row a lower rank.

STRENGTHS, strongest first:

    MANUAL     5  an analyst's own edit -- nothing automated overrides it
    FLAG       4  the platform's own statement (Instagram
                  `has_anonymous_profile_picture`, X `default_profile_image`)
    PIXELS     3  the bytes match a known default avatar, or are a
                  platform-generated letter avatar
    MARKER     2  the URL carries a known stock-avatar asset id
    OBSERVED   1  a picture URL exists and nothing says it is stock

`None` (strength 0) is "nobody looked": it never overrides anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

NONE, OBSERVED, MARKER, PIXELS, FLAG, MANUAL = 0, 1, 2, 3, 4, 5

# Source tags (the value `row.mark("logo", ...)` / `sources.logo` carries)
# whose strength is known outright. Anything else is judged by its value:
# a False from a URL rule is a MARKER, a True is merely OBSERVED.
_SOURCE_STRENGTH = {
    "manual": MANUAL,
    # Instagram's has_anonymous_profile_picture, read off the payload
    "api-anonymous-flag": FLAG,
    # X's legacy.default_profile_image
    "graphql-default-flag": FLAG,
    # services/avatar_cache.py, off the downloaded bytes
    "default-avatar-hash": PIXELS,
    "generated-avatar": PIXELS,
}


@dataclass(frozen=True)
class LogoEvidence:
    """One stage's answer and how much it should be believed."""

    value: Optional[bool]
    strength: int = NONE
    source: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None and self.strength > NONE


UNKNOWN = LogoEvidence(None, NONE, "")


def evidence(value: Optional[bool], source: str = "") -> LogoEvidence:
    """A verdict plus the tag of whatever produced it -> LogoEvidence.

    The value decides the strength when the source is not one of the named
    strong ones: a URL-derived False can only have come from a stock-asset
    marker (shared/avatars.py), and a URL-derived True only means "a picture
    exists and nobody recognised it". A named FLAG source keeps FLAG
    strength in BOTH directions -- Instagram saying the picture is NOT
    anonymous is as authoritative as it saying it is.
    """
    if value is None:
        return UNKNOWN
    src = (source or "").strip()
    strength = _SOURCE_STRENGTH.get(src)
    if strength is None:
        strength = MARKER if value is False else OBSERVED
    return LogoEvidence(bool(value), strength, src)


def stronger(a: LogoEvidence, b: LogoEvidence) -> LogoEvidence:
    """The one of `a`/`b` that should stand. Higher strength wins; on a
    tie, the placeholder (False) wins; unknown never wins over known."""
    if not b.known:
        return a if a.known else UNKNOWN
    if not a.known:
        return b
    if a.strength != b.strength:
        return a if a.strength > b.strength else b
    if a.value is False:
        return a
    return b


def resolve(*items: LogoEvidence) -> LogoEvidence:
    """Fold any number of pieces of evidence into the verdict that stands."""
    out = UNKNOWN
    for it in items:
        out = stronger(out, it)
    return out


def row_evidence(row) -> LogoEvidence:
    """The logo evidence a `Row` (shared/models/row.py) carries: its
    `has_custom_pic` and the `src["logo"]` tag that set it."""
    return evidence(getattr(row, "has_custom_pic", None),
                    (getattr(row, "src", None) or {}).get("logo", ""))


def doc_evidence(doc: Optional[dict]) -> LogoEvidence:
    """The logo evidence a stored profile document carries.

    Reads the stored `logo_strength` when present (written by every save
    since this module existed). A document from before it has only
    `has_logo` and `sources.logo`, and is judged by those exactly like a
    fresh row, so no migration is needed for the rule to apply to it.
    """
    if not doc:
        return UNKNOWN
    value = doc.get("has_logo")
    if value is None:
        return UNKNOWN
    src = str(doc.get("logo_source") or ((doc.get("sources") or {}).get("logo")) or "")
    stored = doc.get("logo_strength")
    if isinstance(stored, int) and not isinstance(stored, bool) and stored > NONE:
        return LogoEvidence(bool(value), stored, src)
    return evidence(bool(value), src)
