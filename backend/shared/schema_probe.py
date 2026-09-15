"""Which exact attribute stopped matching, named, the first time it happens.

WHAT THIS ADDS TO WHAT WAS ALREADY THERE. Three layers already watch for a
platform changing under us, and each answers a different question:

    shared/extraction.py    WHICH STRATEGY failed -- "network:graphql-search
                            returned nothing, dom:results-page carried it" --
                            with the file, line and source text to change.
    services/engine_health  WHETHER THE ENGINE still works at all, by
                            comparing this week's yield per platform against
                            last week's.
    this module             WHICH ATTRIBUTE changed. Not "the GraphQL parse
                            failed" but "`edge.rendering_strategy.view_model`
                            matched 0 of 240 edges this week and 240 of 240
                            last week".

The gap was the last one. A strategy failing tells you the parser broke; it
does not tell you WHERE, and on a payload of several thousand nested keys
that is most of the work. An engineer opening `iter_results` after an alert
still has to diff a live capture against the code by hand to find the one
renamed key.

HOW IT WORKS. Every attribute this engine deliberately targets is probed at
the point it is read: `hit` when it was there, `miss` when the surrounding
object existed but the key did not. The distinction is the whole design --
a miss is only recorded when its PARENT was present, so "Facebook returned
no search results at all" produces no misses, while "Facebook returned 240
results and none of them had the key we read" produces 240. The first is an
empty search. The second is a rename, and only the second is interesting.

Counts ride out on the Sweep, into the rolling telemetry, and the detector
compares each key's hit rate against its own baseline. A key that used to
match and has stopped is named in the incident and in the email.

DELIBERATELY CHEAP. Two dict lookups and an integer increment per probe, on
a path that already does JSON parsing and network I/O -- so it can sit
inside the per-edge loop without anyone having to think about whether it
belongs there. No strings are formatted and no objects are allocated per
probe; the key names are module-level constants in the engines.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional


class SchemaProbe:
    """Per-sweep tally of which targeted attributes were found and missed.

    One instance per sweep, threaded through the parse helpers. Not
    thread-safe and does not need to be: a sweep is one coroutine on one
    event loop, and the response handlers that feed it are serialised by
    that loop.
    """

    __slots__ = ("hits", "misses")

    def __init__(self) -> None:
        self.hits: dict[str, int] = {}
        self.misses: dict[str, int] = {}

    def hit(self, key: str) -> None:
        """This attribute was present and usable."""
        self.hits[key] = self.hits.get(key, 0) + 1

    def miss(self, key: str) -> None:
        """This attribute was absent FROM AN OBJECT THAT SHOULD HAVE HAD IT.

        Only ever call this when the parent object was found, or an empty
        search would look identical to a renamed field -- which is the one
        distinction this module exists to make.
        """
        self.misses[key] = self.misses.get(key, 0) + 1

    def check(self, obj: Any, key: str, probe_key: str) -> Any:
        """`obj[key]`, tallied. Returns None when absent or falsy.

        The convenience form for the common case: a dict that is known to
        be the right kind of object, and one key on it that this engine
        depends on.
        """
        value = obj.get(key) if isinstance(obj, dict) else None
        if value:
            self.hit(probe_key)
        else:
            self.miss(probe_key)
        return value

    def first_of(self, obj: Any, keys: Iterable[str], probe_key: str) -> Any:
        """The first of several spellings that is present, tallied once.

        For a field the platform has migrated and may migrate again: X has
        been moving attributes out of `legacy` into sibling objects one at a
        time, so every read there already tries two or three spellings. This
        records a hit if ANY spelling worked -- the engine is fine -- and a
        miss only when every one of them failed, which is the case worth
        waking somebody for.
        """
        if isinstance(obj, dict):
            for k in keys:
                value = obj.get(k)
                if value:
                    self.hit(probe_key)
                    return value
        self.miss(probe_key)
        return None

    def merge(self, other: Optional["SchemaProbe"]) -> None:
        """Fold another probe's tallies in, for an engine that parses
        several responses into one sweep."""
        if other is None:
            return
        for k, n in other.hits.items():
            self.hits[k] = self.hits.get(k, 0) + n
        for k, n in other.misses.items():
            self.misses[k] = self.misses.get(k, 0) + n

    def report(self) -> dict[str, list[int]]:
        """`{attribute: [hits, misses]}`, for storing on the Sweep.

        A plain dict of small lists rather than a nested object, because
        this goes straight into Mongo and back out again, and a shape with
        no custom types cannot develop a serialisation bug.
        """
        return {
            key: [self.hits.get(key, 0), self.misses.get(key, 0)]
            for key in (self.hits.keys() | self.misses.keys())
        }

    def broken_keys(self, min_samples: int = 5) -> list[str]:
        """Attributes that were looked for and never once found.

        The in-sweep version of the question, for a log line at the moment
        it happens. `min_samples` keeps a single malformed object from
        reading as a rename.
        """
        return sorted(
            key for key, missed in self.misses.items()
            if missed >= min_samples and not self.hits.get(key)
        )


# A probe that records nothing, for a caller that has none to give. Lets
# every parse helper take `probe` unconditionally instead of guarding each
# call site with `if probe is not None`, which on a per-edge loop would be
# the only reason any of them needed a branch.
class _NullProbe(SchemaProbe):
    __slots__ = ()

    def hit(self, key: str) -> None:
        pass

    def miss(self, key: str) -> None:
        pass


NULL_PROBE = _NullProbe()


def probe_or_null(probe: Optional[SchemaProbe]) -> SchemaProbe:
    return probe if probe is not None else NULL_PROBE
