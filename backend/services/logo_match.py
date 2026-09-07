"""Comparing a discovered avatar against the client's own reference logos.

WHAT IT PRODUCES, AND WHAT IT DELIBERATELY DOES NOT.

It writes three fields onto a discovery row -- `logo_similarity`,
`logo_ref_id`, `logo_match_tier` -- and nothing else. In particular:

  * It does NOT set `logo_match`. That field is the ANALYST'S own call (see
    scoring.resolve_match), and an automated guess overwriting a human
    decision is the one thing that would make this feature worse than not
    having it.
  * It does NOT feed `risk_score` or `priority`. Those rubrics are unchanged;
    a logo hit is a RANKING and FILTERING signal laid on top, so nothing that
    exists today scores differently because this shipped.

That restraint is the design. Keyword discovery still finds what it finds;
this only reorders the queue so the profile wearing the client's mark is the
first thing the analyst sees, and gives them a filter to isolate those.
Because it is additive, a miss costs nothing -- the profile stays exactly
where keyword ranking already put it.

THE TIERS, cheapest and most certain first:

    exact   the identical file. `avatar_sha == logo.sha`, already computed
            on both sides, so this costs one dictionary lookup and cannot
            produce a false positive.
    phash   near-identical image: re-encoded, resized, recompressed. High
            precision, blind to the same logo re-presented on a new
            background -- see shared/imagehashing.py.
    embed   the same MARK, re-presented: new background, recoloured, shrunk
            into a corner. A CLIP embedding, cosine-compared. Slower (~70ms
            an image against ~11ms) and semantic, so it is asked last and
            only about what the cheaper tiers could not settle. See
            shared/imageembedding.py for its measured threshold and for the
            case it still cannot serve (bare geometric marks with no
            wordmark, where a rival lookalike scores as high as a true
            match, and it is set to refuse rather than guess).

WHERE IT RUNS. Behind the sweep, in the avatar-caching task that already
fetches the bytes -- never on discovery's critical path. The fingerprinting
itself is CPU-bound and is handed to a thread; doing it inline stalls the
event loop the sweep is running on (measured: up to 3.4s), which would slow
discovery itself.
"""

from __future__ import annotations

from typing import Iterable, Optional

from backend.database.repositories import logo_repository as logos_db
from backend.database.repositories import profile_repository as profiles_db
from backend.shared.imageembedding import is_match, similarity
from backend.shared.imagehashing import compare
from backend.shared.logging import get_logger

log = get_logger("services.logo_match")

TIER_EXACT = "exact"
TIER_PHASH = "phash"
TIER_EMBED = "embed"


class MatchResult:
    """The best reference this avatar resembles, if any is close enough."""

    __slots__ = ("ref_id", "similarity", "tier", "distance")

    def __init__(self, ref_id: str, similarity: int, tier: str, distance: int) -> None:
        self.ref_id = ref_id
        self.similarity = similarity
        self.tier = tier
        self.distance = distance

    def as_fields(self) -> dict:
        return {
            "logo_similarity": self.similarity,
            "logo_ref_id": self.ref_id,
            "logo_match_tier": self.tier,
        }


def best_match(
    *, avatar_sha: str, fingerprint: Optional[dict], references: Iterable[dict],
    embedding: Optional[list] = None,
) -> Optional[MatchResult]:
    """The closest reference to this avatar, or None if nothing is close.

    Pure and synchronous: stored hex strings in, arithmetic out, no decoding
    and no I/O. ~4 microseconds per comparison, so a sweep's worth of
    avatars against a client's references costs about a millisecond.

    An exact digest match wins outright and short-circuits -- there is
    nothing a perceptual score can add once the bytes are known to be
    identical.
    """
    best: Optional[MatchResult] = None
    for ref in references or ():
        ref_id = str(ref.get("id") or "")
        if not ref_id:
            continue

        if avatar_sha and ref.get("sha") == avatar_sha:
            return MatchResult(ref_id, 100, TIER_EXACT, 0)

        if fingerprint:
            cmp = compare(fingerprint, ref)
            # `is_near` results are deliberately dropped rather than stored
            # as a weak match: showing an analyst a 70% "logo match" on an
            # unrelated picture spends their trust, and trust is what makes
            # the badge worth having at all.
            if cmp is not None and cmp.is_match:
                if best is None or best.tier == TIER_EMBED or cmp.distance < best.distance:
                    best = MatchResult(ref_id, cmp.similarity, TIER_PHASH, cmp.distance)
                continue

        # Tier 2, only for references the hash could not settle. `distance`
        # is carried as a rank-comparable integer (lower is closer) so one
        # "best so far" works across tiers; a hash hit always outranks an
        # embedding hit, because it is the more certain claim.
        score = similarity(embedding, ref.get("embedding"))
        if not is_match(score):
            continue
        pseudo_distance = int(round((1.0 - score) * 1000))
        if best is None or (best.tier == TIER_EMBED and pseudo_distance < best.distance):
            best = MatchResult(ref_id, int(round(score * 100)), TIER_EMBED, pseudo_distance)
    return best


async def match_profiles(client_id: str, platform: str, items: list[dict]) -> int:
    """Score a batch of just-saved discovery rows against the client's
    references and write the result onto each. Returns how many matched.

    Never raises. This runs behind a sweep whose profiles are already saved
    and correct without it.
    """
    if not client_id or not items:
        return 0

    # One read of the client's references for the whole batch. The keywords
    # come off the rows themselves, so only the references that could
    # possibly apply are loaded.
    keywords = {
        (it.get("keyword") or "").strip()
        for it in items if (it.get("keyword") or "").strip()
    }
    try:
        references = await logos_db.list_for_keywords(client_id, sorted(keywords))
    except Exception as e:                       # noqa: BLE001 - never fatal
        log.warning(f"logo reference load failed: {type(e).__name__}: {e}")
        return 0
    if not references:
        return 0

    # Group references by keyword so a row is only ever compared against its
    # own keyword's marks plus the client-wide ones -- the scoping that stops
    # two unrelated brands under one client matching each other.
    by_keyword: dict[str, list[dict]] = {}
    client_wide: list[dict] = []
    for ref in references:
        kw = (ref.get("keyword") or "").strip().lower()
        if kw:
            by_keyword.setdefault(kw, []).append(ref)
        else:
            client_wide.append(ref)

    matched = 0
    for it in items:
        url = (it.get("url") or "").strip()
        if not url:
            continue
        sha = (it.get("avatar_sha") or "").strip()
        fp = it.get("avatar_fingerprint")
        if not sha and not fp:
            continue
        kw = (it.get("keyword") or "").strip().lower()
        refs = by_keyword.get(kw, []) + client_wide
        if not refs:
            continue

        hit = best_match(avatar_sha=sha, fingerprint=fp, references=refs,
                         embedding=it.get("avatar_embedding"))
        if hit is None:
            continue
        try:
            ok = await profiles_db.set_logo_match(
                client_id, platform, hit.as_fields(),
                url=url, entity_id=(it.get("entity_id") or "").strip(),
            )
        except Exception as e:                   # noqa: BLE001 - never fatal
            log.warning(f"logo match write failed: {type(e).__name__}: {e}")
            continue
        if ok:
            matched += 1
    return matched
