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
                 in its AssetName column. It is ALSO searched, alongside
                 its children -- see `build_plans`, which sweeps
                 `[parent, *children]`.

        CHILDREN the analyst's permutations. Searched on every platform.
                 Never scored against, never stored as the hit's keyword,
                 never shown as a filter option.

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
    migrate and no backfill step.
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

    `terms` is currently always just `(parent,)` -- it used to also carry
    that type's configured asset names, an alternate public name the same
    entity was known by, but that feature was removed (see the note by
    FLAT_FIELD). The tuple shape is kept because `resolve_parent` still
    needs to pick a parent per hit when one permutation is listed under
    two different parents, which is a separate concern from how many names
    each parent is scored under.
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


def match_terms_for(client: Optional[dict], parent: str, kw_type: str) -> tuple[str, ...]:
    """Every string a hit found under `parent` is scored against.

    That is now the parent, and only the parent. This used to also return
    the type's configured asset names; that feature was removed (see the
    note by FLAT_FIELD). Kept as a function rather than inlined so
    `MatchTarget` keeps one obvious place to change if a second match name
    is ever reintroduced. `client`/`kw_type` are unused for the same
    reason -- the call sites stay stable.
    """
    return (parent,)


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
) -> list[KeywordPlan]:
    """The searches one sweep should run, resolved from a client's groups.

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
            target = MatchTarget(parent=parent, terms=match_terms_for(client, parent, kw_type))
            terms_to_search = _dedup([parent, *(group.get("children") or [])])
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
            kw_type = classify_unknown(client, term)
            by_search[term.lower()] = {
                "search": term, "kw_type": kw_type,
                "targets": [MatchTarget(parent=term, terms=match_terms_for(client, term, kw_type))],
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
