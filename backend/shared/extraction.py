"""Ordered extraction strategies with precise failure reporting.

Every scraper here reads the same field more than one way, the platform's
own network payload first, the rendered DOM second, a page-title/regex
scrape last, because any one of them breaks on its own schedule. The
logic existed; what did not exist was any record of WHICH way was used, or
of what exactly went wrong when one stopped working.

That gap is what made a parser break invisible: Facebook rotates a GraphQL
doc id, the payload branch quietly recognises nothing, the DOM branch finds
nothing either, and the sweep reports 0 results as a clean success; the
same output as "this client genuinely has no impersonators". By the time
anyone noticed, days of coverage were gone (see
services/discovery_service.py's parser-drift canary, which detects the
symptom; this module reports the cause).

`run_strategies` runs each strategy in order, takes the first that yields
anything, and, crucially, records for every one that failed:

    * which strategy it was,
    * the exception, if it raised,
    * the FILE and LINE of the deepest frame inside this project (not the
      framework frame, not a frame inside playwright/json), i.e. the line an
      engineer actually has to open and change,
    * the source text of that line.

A strategy that returns nothing without raising is recorded too, pointed at
its own definition, silent drift is the common case, not the exception.
"""

from __future__ import annotations

import asyncio
import inspect
import linecache
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence, Union

from backend.shared.logging import get_logger

log = get_logger("shared.extraction")

# Frames outside this package are library internals, reporting
# "json/decoder.py line 355" tells nobody which parser to fix.
_PROJECT_MARKER = "backend"

StrategyFn = Callable[[], Union[Any, Awaitable[Any]]]


def _default_is_empty(value: Any) -> bool:
    """Nothing-was-extracted, without swallowing legitimate falsy data.

    0 followers and False are real readings and must NOT count as a failed
    strategy, or a genuinely-zero-follower profile would fall through to a
    weaker strategy and get overwritten by a worse guess.
    """
    if value is None:
        return True
    if isinstance(value, (str, bytes, list, tuple, dict, set, frozenset)):
        return len(value) == 0
    return False


@dataclass
class StrategyFailure:
    """Why one extraction method did not produce a value."""

    strategy: str
    reason: str  # "raised KeyError: 'edges'" | "returned nothing"
    file: str = ""  # repo-relative, e.g. backend/platforms/facebook/...
    line: int = 0
    function: str = ""
    source: str = ""  # the text of that line, stripped

    def where(self) -> str:
        if not self.file:
            return "location unknown"
        base = f"{self.file}:{self.line}"
        return f"{base} in {self.function}()" if self.function else base

    def describe(self) -> str:
        out = f"[{self.strategy}] {self.reason} -- {self.where()}"
        if self.source:
            out += f"\n      {self.source}"
        return out


@dataclass
class ExtractionResult:
    """What came back, how, and what had to fail first to get there."""

    value: Any = None
    strategy: str = ""
    failures: list[StrategyFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.strategy)

    @property
    def degraded(self) -> bool:
        """Succeeded, but not on the preferred method, the early warning
        that the primary path is rotting while output still looks fine."""
        return self.ok and bool(self.failures)

    @property
    def all_empty(self) -> bool:
        """Every strategy ran fine and simply had nothing to return.

        THE DISTINCTION THIS EXISTS FOR, and it cost real diagnostic time:
        "the extraction chain is broken" and "this query genuinely matched
        nothing" used to be the same outcome here -- both end with no value
        and every strategy sitting in `failures`. They are not the same
        thing, and conflating them made a search for a brand nobody is
        impersonating indistinguishable from a doc-id rotation that had
        silently blinded the sweep.

        A strategy that RAISED is a real failure and keeps this False, so a
        chain that is genuinely rotting still reports as one.
        """
        return (
            not self.ok
            and bool(self.failures)
            and all(f.reason.startswith("returned nothing") for f in self.failures)
        )


    def report(self) -> str:
        """Multi-line, aimed at whoever has to fix it, goes into the
        incident body and therefore the alert email."""
        lines = [f.describe() for f in self.failures]
        if self.ok:
            lines.insert(0, f"Recovered using fallback strategy: {self.strategy}")
        elif self.all_empty:
            lines.insert(0, "No data to extract -- every strategy ran and found nothing.")
        else:
            lines.insert(0, "Every extraction strategy failed.")
        return "\n".join(f"  {ln}" for ln in lines)


def _repo_relative(path: str) -> str:
    p = Path(path)
    parts = p.parts
    if _PROJECT_MARKER in parts:
        return "/".join(parts[parts.index(_PROJECT_MARKER):])
    return p.name


def _blame_frame(exc: BaseException) -> tuple[str, int, str, str]:
    """The deepest frame inside this project, the line to actually change.

    Deliberately not the outermost frame (that is this module) and not the
    innermost frame overall (that is usually inside json/ or playwright/,
    which nobody here can edit).
    """
    frames = traceback.extract_tb(exc.__traceback__)
    chosen = None
    for fr in frames:
        if _PROJECT_MARKER in Path(fr.filename).parts:
            chosen = fr
    if chosen is None:
        chosen = frames[-1] if frames else None
    if chosen is None:
        return "", 0, "", ""
    src = (chosen.line or linecache.getline(chosen.filename, chosen.lineno) or "").strip()
    return _repo_relative(chosen.filename), chosen.lineno or 0, chosen.name or "", src


def _definition_site(fn: Callable) -> tuple[str, int, str, str]:
    """Where a silently-empty strategy is defined. A strategy that returns
    nothing never raises, so there is no traceback to blame; its own
    definition is the honest pointer."""
    try:
        target = inspect.unwrap(fn)
        file = inspect.getsourcefile(target) or ""
        _, lineno = inspect.getsourcelines(target)
        name = getattr(target, "__qualname__", getattr(target, "__name__", ""))
        src = linecache.getline(file, lineno).strip() if file else ""
        return (_repo_relative(file) if file else ""), lineno, name, src
    except (OSError, TypeError):
        return "", 0, getattr(fn, "__name__", ""), ""


async def run_strategies(
    label: str,
    strategies: Sequence[tuple[str, StrategyFn]],
    *,
    is_empty: Optional[Callable[[Any], bool]] = None,
) -> ExtractionResult:
    """Run each strategy in order; first one to yield something wins.

    `strategies` is an ordered [(name, callable)], by convention the
    platform's own network payload first, then the DOM, then anything
    cruder. Callables may be sync or async. CancelledError is never
    swallowed: a cancelled job must stay cancelled, not silently fall
    through to the next strategy and keep the browser busy.
    """
    empty = is_empty or _default_is_empty
    result = ExtractionResult()

    for name, fn in strategies:
        try:
            value = fn()
            if inspect.isawaitable(value):
                value = await value
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # strategy blowing up is exactly the case the next one exists for
            file, line, func, src = _blame_frame(e)
            result.failures.append(StrategyFailure(
                strategy=name, reason=f"raised {type(e).__name__}: {e}",
                file=file, line=line, function=func, source=src,
            ))
            continue

        if empty(value):
            file, line, func, src = _definition_site(fn)
            result.failures.append(StrategyFailure(
                strategy=name, reason="returned nothing",
                file=file, line=line, function=func, source=src,
            ))
            continue

        result.value = value
        result.strategy = name
        if result.failures:
            log.warning(
                f"{label}: recovered on fallback {name!r} after "
                f"{len(result.failures)} failed strateg(ies) -- "
                + "; ".join(f.where() for f in result.failures)
            )
        return result

    if result.all_empty:
        # NOT an error, and logging it as one taught the reader to
        # distrust this line. Every strategy ran cleanly and found
        # nothing, which is the ordinary answer for a keyword nobody
        # is impersonating -- 28 of these were logged as ERROR against
        # real brand keywords AND against a deliberately nonsense test
        # keyword before this told the two apart. A genuinely broken
        # chain (any strategy RAISING) still takes the error branch.
        log.info(f"{label}: no results\n{result.report()}")
    else:
        log.error(f"{label}: every strategy failed\n{result.report()}")
    return result
