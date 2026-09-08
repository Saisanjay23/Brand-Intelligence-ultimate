"""The scan record: one suspect profile, its raw scraped fields, and its
derived score, what a platform's analysis engine builds up field-by-field
over the course of visiting one profile. Every `platforms/*/analysis_engine.py`
adapter constructs and mutates one of these directly (`row.mark(...)`,
`row.note(...)`, `row.has_custom_pic = True`, ...); this exact shape is
part of that contract and must not change without updating every adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.shared.models.scoring import ACTIVE_WINDOW_DAYS, NAME_THRESHOLD, compute_score
from backend.shared.text import contiguous_letters_match


@dataclass
class Row:
    url: str
    target: str
    original_feed: str = ""

    status: str = "PENDING"
    entity_type: str = "profile"
    profile_id: str = ""
    # The PUBLIC handle (@name), as distinct from `profile_id`, which is the
    # platform's internal id. Discovery persists this as the `username`
    # column; before it existed every row stored the id there instead and
    # the UI rendered "@50840430092" for a profile whose handle is
    # "@defnce.app". Platforms whose search payload names the handle set it
    # here; the rest fall back to shared/text.py::handle_from_url.
    username: str = ""
    profile_name: str = ""
    created_iso: str = ""
    followers: Optional[int] = None
    followers_exact: str = ""
    friends: Optional[int] = None
    location: str = ""
    # The profile's bio/description text. Both the Twitter and Instagram
    # engines have always PARSED this (TwitterUser.description,
    # InstagramUser.biography) and then had nowhere to put it, so it never
    # reached the database -- blank on 100% of stored Instagram rows and
    # 97.8% of Twitter ones despite being read every time. It is real
    # impersonation evidence ("Official account of...", a copied tagline),
    # which is why it is worth carrying rather than discarding.
    bio: str = ""
    last_post_iso: str = ""
    posts_seen: str = ""  # yes | no | ""
    profile_pic_url: str = ""
    has_custom_pic: Optional[bool] = None
    # a real platform-issued verification badge, read directly off the
    # platform's own payload/DOM. None means "this platform's analysis
    # engine doesn't check for one", never "not verified".
    verified: Optional[bool] = None
    screenshot: str = ""
    screenshot_bytes: Optional[bytes] = None
    notes: str = ""
    name_score: int = 0
    src: dict[str, str] = field(default_factory=dict)  # field -> where it came from

    def note(self, m: str) -> None:
        if m not in self.notes:
            self.notes = f"{self.notes}; {m}".strip("; ")

    def mark(self, fld: str, source: str) -> None:
        self.src[fld] = source

    # Derived

    @property
    def logo_yes(self) -> str:
        """"Yes" ONLY when a real, account-chosen picture was confirmed.

        A platform's own stock avatar -- the grey silhouette, the letter
        tile, the default egg -- is not a logo, and neither is a picture we
        never managed to look at. Both are "No".

        THIS USED TO READ `if has_custom_pic is False: "No"` else "Yes",
        which made UNKNOWN mean Yes. `has_custom_pic` is deliberately
        tri-state (see its own declaration): True = a real upload was seen,
        False = the platform's placeholder was recognised, None = the engine
        never got an avatar to judge. Folding None in with True asserted a
        custom profile picture on every profile the scraper failed to read.

        That was not a cosmetic default. `risk` and `priority` below are
        both computed from this string, and both are export columns, so an
        unread profile was leaving the tool as "Logo: Yes, Risk 6, High" --
        a verdict about an account nobody had actually seen.

        Each platform decides False for itself, and they do not agree on how
        because the platforms do not behave the same way:
            facebook/instagram/twitter  known stock-avatar URL markers
            youtube                     generated-avatar prefix plus a
                                        two-colour flatness test, which is
                                        what catches the letter tiles
            telegram                    Telethon's own PhotoEmpty check
            tiktok                      presence itself -- TikTok omits the
                                        avatar entirely for a default account
                                        rather than serving a stock URL
        """
        return "Yes" if self.has_custom_pic is True else "No"

    @property
    def active_yes(self) -> str:
        """Yes/No only -- never blank, by explicit product decision.

        "Active" means a post inside ACTIVE_WINDOW_DAYS. Anything we cannot
        show to be inside that window reads as "No", including the case
        where the account HAS posts but no date could be scraped for them.

        That last case is a deliberate accepted cost, not an oversight: it
        means an account we merely failed to date is reported inactive
        rather than blank. The alternative -- a third, empty state -- was
        rejected because a blank cell in the analyst's Active column and in
        the export is not actionable. Where the distinction still matters,
        `last_post_date` is the honest field: it is empty exactly when the
        date is unknown, so "Active=No with no Last Post" is recognisably
        different from "Active=No with a date older than the window".
        """
        if not self.last_post_iso:
            return "No"
        try:
            dt = datetime.strptime(self.last_post_iso[:10], "%Y-%m-%d")
        except ValueError:
            return "No"
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=ACTIVE_WINDOW_DAYS)
        return "Yes" if dt >= cutoff else "No"

    @property
    def name_yes(self) -> str:
        if self.profile_name:
            return "Yes" if self.name_score >= NAME_THRESHOLD else "No"
        return "Yes"

    @property
    def name_exact_run(self) -> bool:
        """The High Match filter's actual criterion: does `profile_name`
        contain `target`'s letters as one contiguous run (punctuation/case
        differences ignored), see shared/text.py::contiguous_letters_match.
        Deliberately independent of `name_score`/`name_yes` above (those
        stay a fuzzy, order-insensitive similarity used for the Risk/
        Priority rubric), a profile can score High on name_score's fuzzy
        token overlap while failing this (reordered words) or vice versa."""
        return contiguous_letters_match(self.profile_name, self.target)

    @property
    def risk(self) -> int:
        """See scoring.py's `compute_score` for the full tiered rubric."""
        return compute_score(
            has_logo=self.logo_yes == "Yes",
            has_name_match=self.name_yes == "Yes",
            has_location=bool(self.location.strip()),
            last_post_iso=self.last_post_iso,
        )

    @property
    def priority(self) -> str:
        # If user logo check is explicitly Yes, it forces High priority regardless of score.
        if self.logo_yes == "Yes":
            return "High"
        
        # Standard score-based threshold
        score = self.risk
        if score >= 5:
            return "High"
        else:
            return "Low"
