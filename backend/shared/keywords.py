"""Parent/child keyword groups: what gets SEARCHED vs what gets MATCHED.

THE PROBLEM THIS SOLVES
    A brand's real name ("Gautam Adani") is a poor search term on its own --
    an impersonator rarely registers under it verbatim. What actually finds
    them is an analyst's own generated permutations: "gautamadani",
    "gautam adani official", "adani gautam", "gautam.adani.hq", and so on.

    But those permutations are terrible things to MATCH against. Scoring a
    discovered profile's name against "gautam.adani.hq" says nothing useful
    about whether it is impersonating Gautam Adani, and filing the result
    under that permutation scatters one investigation across a dozen
    unrelated keyword buckets in the UI.

    So the two jobs are split:

        PARENT   the real name. It is the MATCH target (name_score /
                 name_exact_run), the bucket every hit found by any of its
                 children is filed under, the only keyword the UI's filter
                 dropdown offers, and the name the analysis export reports
                 in its AssetName column.

                 It is searched ONLY when it has no children -- see
                 `build_plans`, which sweeps `children or [parent]`. An
                 analyst who has curated permutations has said what to
                 search for, and the real name is rarely among the terms
                 worth spending a sweep on; one who has curated none still
                 gets their keyword searched, so nothing is ever configured
                 and then silently not run. An analyst who DOES want the
                 exact name queried alongside the permutations adds it as
                 one of them.

        CHILDREN the analyst's permutations. Searched on every platform,
                 and when there is at least one they are the WHOLE search
                 set for that parent. Never scored against, never stored as
                 the hit's keyword, never shown as a filter option.

    One parent's children all roll up into that one parent, so an analyst
    filtering the results grid by "Gautam Adani" sees everything all twelve
    permutations turned up, not twelve separate piles -- and every one of
    those rows exports under "Gautam Adani", not under the permutation
    that happened to surface it.

BACK-COMPATIBILITY IS THE LOAD-BEARING PART
    `name_keywords` / `domain_keywords` on the client document stay exactly
    what they always were: a flat list of strings. They now hold the
    PARENTS, so everything that already reads them keeps working untouched.
    (The three service-layer readers this note used to name --
    `discovery_service`, `incident_publisher`, `scheduler_controller` --
    were deleted with the old backend; `profile_repository`'s
    keyword_match_type bucket filter is the surviving one.)

    A client saved before groups existed has no `keyword_groups` field at
    all. `groups_from_flat` synthesises one group per existing keyword with
    NO children, and a childless parent searches ITSELF (see
    `build_plans`), which is precisely the old behaviour. Such a client
    sweeps identically before and after this feature, with nothing to
    migrate and no backfill step. That is also why the parent-as-fallback
    rule has to be exactly "no children", not "no groups": a client may
    have permutations for one keyword and none for the next, and the second
    must still be swept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

# The two keyword categories the whole pipeline is already split by (per-type
# scrape caps, incident category). Groups are stored per category so a parent
# never has to be re-classified at read time.
INDIVIDUAL = "individual"
DOMAIN = "domain"
KEYWORD_TYPES = (INDIVIDUAL, DOMAIN)

# Which client field holds the flat parent list for each type. These are the
# ORIGINAL field names, deliberately unchanged -- see the module docstring on
# why every existing reader must keep working.
FLAT_FIELD = {INDIVIDUAL: "name_keywords", DOMAIN: "domain_keywords"}

# NOTE: asset names (`asset_name_individual_keywords` /
# `asset_name_domain_keywords`) used to sit here as a second set of MATCH
# targets alongside the parent -- an analyst could protect "Gautam Adani"
# but have hits also scored against "Adani Group". That whole feature was
# removed: the PARENT is now the only thing a hit is scored against and the
# only name it is reported under, which is also what the analysis export's
# AssetName column carries. One name per bucket, everywhere.


@dataclass(frozen=True)
class MatchTarget:
    """One parent a hit could be filed under, and every string a hit's name
    is scored against for it.

    `terms` is the parent AND its children -- see `match_terms_for` for why
    the permutations belong in the match set and not only in the search
    set. The parent stays first because it is the canonical name and wins
    ties, but any child may out-rank it on a given hit.
    """

    parent: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class KeywordPlan:
    """One search this sweep will actually run, and what to do with what it
    finds.

    `search` goes into the platform's search box. `targets` is who the
    resulting hits may be filed under -- normally exactly one, but a
    permutation an analyst listed under two different parents produces a
    single search with two candidate targets, resolved per hit by
    `resolve_parent` below.
    """

    search: str
    kw_type: str
    targets: tuple[MatchTarget, ...]

    @property
    def parent(self) -> str:
        """The default/primary parent, for callers that only need a label
        (progress lines, pending-item previews). Hit-level filing goes
        through `resolve_parent`, which may pick a different target."""
        return self.targets[0].parent if self.targets else self.search


def _clean(value: Any) -> str:
    """One keyword string, trimmed. Non-strings (a malformed document, a
    stray None in a list) collapse to "" rather than raising -- a bad row in
    a client's config must never take the whole sweep down."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _dedup(values: Iterable[str]) -> list[str]:
    """Order-preserving case-insensitive dedup. Order is preserved because
    it is the analyst's own priority ordering, and the UI renders these
    lists back in the order they were typed."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        cleaned = _clean(v)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def normalize_groups(raw: Any) -> dict[str, list[dict]]:
    """Whatever a caller sent -> the canonical
    `{"individual": [{"parent": str, "children": [str]}], "domain": [...]}`.

    Total, never raises: this runs on request bodies and on documents read
    back out of Mongo, and a malformed entry in either has to degrade to
    "that one entry is dropped", not "this client can no longer be loaded".

    A group with a blank parent is dropped entirely (its children have
    nothing to roll up into). A child equal to its own parent is dropped as
    a child, since a childless parent already searches itself and keeping
    both would search the same term twice.
    """
    out: dict[str, list[dict]] = {t: [] for t in KEYWORD_TYPES}
    if not isinstance(raw, dict):
        return out

    for kw_type in KEYWORD_TYPES:
        entries = raw.get(kw_type)
        if not isinstance(entries, list):
            continue
        seen_parents: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            parent = _clean(entry.get("parent"))
            if not parent or parent.lower() in seen_parents:
                continue
            seen_parents.add(parent.lower())
            raw_children = entry.get("children")
            children = _dedup(raw_children if isinstance(raw_children, list) else [])
            children = [c for c in children if c.lower() != parent.lower()]
            out[kw_type].append({"parent": parent, "children": children})
    return out


def groups_from_flat(
    name_keywords: Optional[Iterable[str]],
    domain_keywords: Optional[Iterable[str]],
) -> dict[str, list[dict]]:
    """The synthesised groups for a client that predates this feature: one
    childless parent per existing keyword.

    A childless parent searches itself (`build_plans`), so this reproduces
    the pre-groups behaviour exactly -- which is what makes the whole
    feature a no-op for every client until someone actually adds children.
    """
    return {
        INDIVIDUAL: [{"parent": p, "children": []} for p in _dedup(name_keywords or [])],
        DOMAIN: [{"parent": p, "children": []} for p in _dedup(domain_keywords or [])],
    }


def groups_for_client(client: Optional[dict]) -> dict[str, list[dict]]:
    """The client's keyword groups, synthesising them from the flat lists
    when the document has none (see `groups_from_flat`).

    `keyword_groups` is authoritative whenever it is non-empty; the flat
    lists are only consulted for a document that predates it. A client
    saved through the current form always writes BOTH (the groups, and the
    parent list derived from them via `flat_keywords`), so the two can
    never drift apart.
    """
    client = client or {}
    groups = normalize_groups(client.get("keyword_groups"))
    if any(groups[t] for t in KEYWORD_TYPES):
        return groups
    return groups_from_flat(
        client.get("name_keywords"), client.get("domain_keywords")
    )


def parents_of(groups: dict[str, list[dict]], kw_type: str) -> list[str]:
    """The flat parent list for one type -- exactly what belongs in
    `name_keywords`/`domain_keywords`, which is how every pre-existing
    reader of those fields keeps working unchanged."""
    return [g["parent"] for g in groups.get(kw_type, [])]


def flat_keywords(groups: dict[str, list[dict]]) -> dict[str, list[str]]:
    """`{"name_keywords": [...parents], "domain_keywords": [...parents]}` --
    the derived fields `client_repository.upsert` persists alongside the
    groups themselves, so the two can never disagree."""
    return {FLAT_FIELD[t]: parents_of(groups, t) for t in KEYWORD_TYPES}


def search_terms(groups: dict[str, list[dict]], kw_type: str) -> list[str]:
    """Every string that will actually be typed into a platform's search
    box for one keyword type: the parent itself plus all of its child
    permutations. Used for previews/counts; the sweep itself wants
    `build_plans`, which also carries the match targets."""
    out: list[str] = []
    for group in groups.get(kw_type, []):
        parent = group.get("parent")
        children = group.get("children") or []
        if parent:
            out.append(parent)
        out.extend(children)
    return _dedup(out)


def match_terms_for(parent: str, children: Optional[Iterable[str]] = None) -> tuple[str, ...]:
    """Every string a hit found under `parent` is scored against: the parent
    and its children.

    THE CHILDREN USED TO BE SEARCH-ONLY, AND THAT WAS THE BUG. A hit was
    scored purely against the parent, which meant the High/Medium/Low badge
    an analyst reads answered a question nobody asked. Take parent "Gautam
    Adani" with the permutation "adani gautam": a profile actually named
    "Adani Gautam" reads as a dead-on match for the term the analyst
    curated, yet against the PARENT it is word-order-reversed -- so
    `contiguous_letters_match` says no, it misses High, and it lands in
    Medium next to profiles that merely share a token.

    Worse, the two halves of the badge disagreed about what they were
    measuring. `name_exact_run` compares the name against `Row.target`, the
    permutation actually typed into the search box, while `name_score`
    compared it against the parent. High was judged on the child and
    Medium/Low on the parent, so a hit could be graded against two
    different strings depending on which band it fell into.

    Scoring against the whole set fixes both: the analyst's own
    permutations are the terms they decided were worth looking for, so they
    are the terms a match should be judged on, and one comparison set means
    every band is judged the same way.

    THE PARENT STAYS IN THE SET. It is the real name and therefore the
    strongest possible match; dropping it would demote a profile that is
    literally called "Gautam Adani" on a client whose permutations all
    happen to be handles. Because `best_match` takes the BEST term, adding
    the children can only ever raise a hit's grade, never lower it -- which
    is what makes this safe to apply to a pipeline already in use.

    Deduped case-insensitively: `normalize_groups` already drops a child
    equal to its own parent, and this guards the same case for a group
    built by any other route.
    """
    return tuple(_dedup([parent, *(children or [])]))


def classify_unknown(client: Optional[dict], keyword: str) -> str:
    """INDIVIDUAL or DOMAIN for a keyword the client's groups don't contain.

    Always DOMAIN now. The only signal this ever had was whether the term
    was one of the client's INDIVIDUAL asset names, and asset names are
    gone -- so every genuinely unknown ad-hoc term takes what was already
    the fallback branch. A term the client DOES know is never routed here:
    `build_plans` matches it against the real groups first, where its type
    is recorded explicitly.
    """
    return DOMAIN


def build_plans(
    client: Optional[dict],
    requested: Optional[Iterable[str]] = None,
    requested_types: Optional[dict[str, str]] = None,
) -> list[KeywordPlan]:
    """The searches one sweep should run, resolved from a client's groups.

    Each requested parent contributes its CHILDREN, or -- when it has
    none -- itself. See the module docstring for why permutations
    replace the parent rather than joining it.

    `requested` scopes the sweep to a subset of the client's PARENTS -- the
    keyword list a caller passed to `POST /discovery`, which is always
    parents (that is what the UI shows and what the round-robin engine
    reads out of `name_keywords`/`domain_keywords`). Omitted or empty means
    every parent the client has.

    A requested term that matches no parent is still honoured, as its own
    childless plan: an analyst running an ad-hoc one-off search for a term
    that isn't in the client's saved config must not silently sweep
    nothing. It is classified by `classify_unknown` below, which is now
    always DOMAIN -- the individual-asset-name signal it used to consult is
    gone with that feature.

    Deduped by SEARCH TERM: the same permutation listed under two parents
    is one search, not two, since running the same query twice against the
    same platform costs a real page load and risks the session for nothing.
    When that happens the single plan carries BOTH parents as targets, and
    `resolve_parent` picks per hit.

    `requested_types` maps a lowered term to INDIVIDUAL/DOMAIN and is
    consulted ONLY for a term the client's groups do not contain. Callers
    that already know each term's type -- `POST /discovery` takes two
    separate lists, so it always does -- pass it rather than letting
    `classify_unknown` guess, which would file an ad-hoc individual name
    under the domain caps. A term the client DOES know is never routed
    here: its type comes from its own group.
    """
    groups = groups_for_client(client)
    wanted: Optional[set[str]] = None
    if requested is not None:
        cleaned = _dedup(requested)
        if cleaned:
            wanted = {c.lower() for c in cleaned}

    # search term (lowered) -> {"search", "kw_type", "targets": [MatchTarget]}
    by_search: dict[str, dict] = {}
    order: list[str] = []
    matched_parents: set[str] = set()

    for kw_type in KEYWORD_TYPES:
        for group in groups.get(kw_type, []):
            parent = group["parent"]
            if wanted is not None and parent.lower() not in wanted:
                continue
            matched_parents.add(parent.lower())
            target = MatchTarget(
                parent=parent,
                terms=match_terms_for(parent, group.get("children") or []),
            )
            # PERMUTATIONS REPLACE THE PARENT; the parent is the fallback.
            # A parent that has permutations is NOT searched under its own
            # name -- the analyst's permutations are the search set, and the
            # parent's job is to be what those hits are matched and filed
            # against. A parent with none searches itself, which is what
            # keeps a client who has curated nothing sweeping exactly as
            # before. See this module's docstring.
            children = _dedup(group.get("children") or [])
            terms_to_search = children or [parent]
            for term in terms_to_search:
                key = term.lower()
                if key not in by_search:
                    by_search[key] = {"search": term, "kw_type": kw_type, "targets": [target]}
                    order.append(key)
                elif all(t.parent.lower() != parent.lower() for t in by_search[key]["targets"]):
                    by_search[key]["targets"].append(target)

    # An explicitly requested term the client's config doesn't know about
    # still gets swept, on its own, rather than vanishing.
    if wanted is not None:
        for term in _dedup(requested or []):
            if term.lower() in matched_parents or term.lower() in by_search:
                continue
            kw_type = (requested_types or {}).get(term.lower()) or classify_unknown(client, term)
            by_search[term.lower()] = {
                "search": term, "kw_type": kw_type,
                "targets": [MatchTarget(parent=term, terms=match_terms_for(term))],
            }
            order.append(term.lower())

    return [
        KeywordPlan(
            search=by_search[k]["search"],
            kw_type=by_search[k]["kw_type"],
            targets=tuple(by_search[k]["targets"]),
        )
        for k in order
    ]


def _specificity(term: str) -> int:
    """How much a match term actually pins down: its letters and digits,
    ignoring case, spacing and punctuation -- the same characters
    `contiguous_letters_match` compares. "Jeet Adani" is 9, "adani" is 5.
    Used only to break ties between terms that matched equally well; see
    `best_match`."""
    return sum(1 for ch in (term or "") if ch.isalnum())


@dataclass(frozen=True)
class NameMatch:
    """Everything a discovered hit's name comparison produced, in one pass.

    Separate fields rather than a bare score because the UI's badge is not
    a threshold: High is `exact_run` (a real contiguous letter-run of the
    term inside the name), Medium/Low band on `score` below it. Both come
    from the SAME winning term, which is what makes `term` a truthful
    answer to "why is this card graded like that" -- and what lets the card
    show it.
    """

    parent: str      # which bucket this hit is filed under
    term: str        # the keyword that produced the verdict
    score: int       # 0-100, word-order-insensitive similarity
    exact_run: bool  # the High Match criterion


def best_match(
    plan_or_targets, name: str, scorer, exact_predicate,
) -> NameMatch:
    """The strongest match between `name` and any term of any target.

    RANKED ON (exact_run, score, specificity), IN THAT ORDER.

    `exact_run` leads because that is the order the badge itself is decided
    in: an exact run is High regardless of how the fuzzy score lands, so a
    term that achieves one must out-rank a term that merely scores well, or
    `term` would name a keyword that does not explain the badge sitting
    next to it. `score` breaks ties among equally exact terms.

    `specificity` -- how many letters the matched term actually pins down --
    breaks the ties those two leave, and it is not cosmetic. A permutation
    listed under TWO parents produces one search with both as candidates
    (see `build_plans`), and that shared child matches both of them
    identically by construction. Without a third key the hit files under
    whichever group was saved first: "adani" is a child of both "Gautam
    Adani" and "Jeet Adani", so a profile called "Jeet Adani Official"
    matched "adani" under Gautam's target first and landed in the wrong
    investigation. Comparing on length puts the hit where the most evidence
    points -- "Jeet Adani" is nine letters of agreement, "adani" is five.

    On a genuine tie the earlier term still wins, which puts the parent
    first: the canonical name is the better label when nothing separates
    them.

    Taking the BEST across the set is what makes this safe to switch on
    over existing data: adding the children to the comparison can only
    raise a hit's grade, never lower it.

    Accepts a `KeywordPlan` or a bare tuple of targets so callers that hold
    only the targets (discovery's queue items) need not rebuild a plan.
    `scorer`/`exact_predicate` are injected for the same reason
    `resolve_parent` injects its scorer: this module stays free of the
    text-matching stack and stays testable without it.
    """
    targets = getattr(plan_or_targets, "targets", plan_or_targets) or ()
    if not targets:
        return NameMatch(parent="", term="", score=0, exact_run=False)

    best = NameMatch(parent=targets[0].parent, term="", score=-1, exact_run=False)
    for target in targets:
        for term in target.terms or (target.parent,):
            try:
                score = int(scorer(name or "", term))
            except Exception:                    # noqa: BLE001 - never fatal
                score = 0
            try:
                run = bool(exact_predicate(name or "", term))
            except Exception:                    # noqa: BLE001 - never fatal
                run = False
            rank = (run, score, _specificity(term))
            if rank > (best.exact_run, best.score, _specificity(best.term)):
                best = NameMatch(
                    parent=target.parent, term=term, score=score, exact_run=run)
    return NameMatch(
        parent=best.parent, term=best.term,
        score=max(best.score, 0), exact_run=best.exact_run,
    )


def resolve_parent(plan: KeywordPlan, name: str, scorer) -> tuple[str, int]:
    """`(parent to file this hit under, its name score)`.

    Ordinarily a plan has exactly one target and this just scores the name
    against that target's terms. The interesting case is a permutation an
    analyst listed under two different parents (see `build_plans`): the hit
    is filed under whichever parent's own terms it actually resembles,
    rather than arbitrarily under whichever group happened to be saved
    first.

    Within one target the BEST-scoring term wins but the PARENT is still
    what is returned. With asset names removed each target now has exactly
    one term (its own parent), so that inner loop is a formality today --
    kept because the outer loop over multiple TARGETS, which is the case
    that actually matters, shares it.

    `scorer` is injected (rather than importing `shared.text.name_score`
    here) purely so this stays a pure function testable without pulling in
    the text-matching stack.
    """
    best_parent, best_score = plan.parent, -1
    for target in plan.targets or (MatchTarget(plan.search, (plan.search,)),):
        for term in target.terms or (target.parent,):
            try:
                score = int(scorer(name or "", term))
            except Exception:
                score = 0
            if score > best_score:
                best_parent, best_score = target.parent, score
    return best_parent, max(best_score, 0)


def match_any(plan: KeywordPlan, name: str, predicate) -> bool:
    """True when `name` satisfies `predicate` against ANY match term of any
    of this plan's targets -- the boolean counterpart to `resolve_parent`,
    used for `name_exact_run` (shared/text.py::contiguous_letters_match).

    Same reason for injecting `predicate` as `resolve_parent` injects
    `scorer`: keeps this module free of the text-matching stack.
    """
    for target in plan.targets or (MatchTarget(plan.search, (plan.search,)),):
        for term in target.terms or (target.parent,):
            try:
                if predicate(name or "", term):
                    return True
            except Exception:
                continue
    return False
