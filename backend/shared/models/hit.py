"""One search result, in the shape every platform's discovery sweep
produces before it's converted to a `Row` (see shared/models/row.py).

Lives here, not inside any one platform's folder, because it is genuinely
cross-platform infrastructure: Facebook's own internal id-backfill/
reconciliation machinery builds these by the hundred per sweep, and
YouTube's and TikTok's discovery engines -- which have nothing else in
common with Facebook's -- use the exact same shape for their own, much
simpler sweeps. Keeping it in `platforms/facebook/` and having those two
reach across into a sibling platform's file was the wrong home for a type
three unrelated platforms depend on; `shared/` is what platforms/ and
stealth/ already both import from without either being "inside" the other.

Twitter, Instagram and Telegram do NOT use `Hit` -- their search responses
carry enough extra fields (followers, bio, location, verified, creation
date) that each builds a richer platform-specific object of its own and
converts straight to `Row`, never through this narrower shape. See each of
those `discovery_engine.py`'s own `user_to_row`/`entity_to_row`.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.shared.models.row import Row


@dataclass
class Hit:
    """One search result, the fields common to every platform's sweep."""

    entity_id: str
    name: str
    url: str
    avatar: str = ""
    has_custom_pic: bool = False
    verified: bool = False
    entity_type: str = "profile"  # profile | page | channel | group
    keyword: str = ""
    tab: str = ""
    rank: int = 0
    source: str = "graphql"  # graphql | id-backfill | api | dom

    def as_dict(self) -> dict:
        return {
            "entity_id": self.entity_id, "name": self.name, "url": self.url,
            "avatar": self.avatar, "has_custom_pic": self.has_custom_pic,
            "verified": self.verified, "entity_type": self.entity_type,
            "keyword": self.keyword, "tab": self.tab, "rank": self.rank,
            "source": self.source,
        }


def hit_to_row(hit: Hit) -> Row:
    """A `Hit` -> the shared `Row` record discovery persists.

    A straight field rename, not a data-capture change: `Hit` never carries
    more than this to begin with, so there is nothing extra to pull across.
    `row.target` is seeded from `hit.keyword` (the search term this hit was
    found under); a caller that resolves keyword-plan parents (an analyst
    permutation -> the real brand/person name) should overwrite it before
    persisting or scoring.
    """
    row = Row(url=hit.url, target=hit.keyword, entity_type=hit.entity_type,
              profile_id=hit.entity_id, profile_name=hit.name,
              profile_pic_url=hit.avatar, has_custom_pic=hit.has_custom_pic,
              verified=hit.verified)
    for f in ("profile_name", "profile_pic_url", "verified"):
        row.mark(f, f"discovery-search:{hit.source}")
    return row
