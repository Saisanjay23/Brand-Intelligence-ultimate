"""TikTok's Users tab keeps the accounts it found.

THE DEFECT THIS PINS, found by reading and reproduced before it was fixed.

`_users_for` reads the rendered result cards into `ordered`, then folds in
the richer `/api/search/user` XHR payloads on top. That fold called
`iter_users(blob, probe)` against a name that was never bound in the
function -- not a parameter, not a local, not a module global.

Python raises an unbound name only when the line actually runs, and that
line runs only inside `for text in bodies`. So the failure was invisible
until TikTok served the very payload the code exists to read:

    no payload served  -> the loop body never runs -> DOM cards returned
    payload served     -> NameError -> the function's own broad `except`
                          logged a warning and returned [], throwing away
                          `ordered` and every card already parsed into it

The richer the data TikTok returned, the more certainly all of it was
discarded. And `_merge_user_accounts` reads an empty list as "this keyword
matched no accounts", so nothing was merged and nothing looked wrong: the
Users tab is where the NAME-MATCHED accounts come from -- the ones this
engine ranks first and an analyst actually acts on -- and what survived was
the Top tab's incidental video authors.

A clean zero, from the one path that had no test and no drift detector.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.platforms.tiktok import discovery_engine as T

CARDS = [
    {"username": "adani_official", "nickname": "Adani Group",
     "followers": "12.5K", "likes": "300", "avatar": "https://cdn/a.jpg"},
    {"username": "adani_fan", "nickname": "Adani Fan",
     "followers": "900", "likes": "20", "avatar": "https://cdn/b.jpg"},
]

XHR_BODY = json.dumps({
    "user_list": [
        {"user_info": {"unique_id": "adani_official", "nickname": "Adani Group",
                       "id": "777", "verified": True, "follower_count": 12500}},
    ]
})


class _Resp:
    url = "https://www.tiktok.com/api/search/user/full/?q=adani"

    async def text(self) -> str:
        return XHR_BODY


class _Page:
    """The two things _users_for drives: a response hook and evaluate()."""

    def __init__(self, serve_xhr: bool):
        self.serve_xhr = serve_xhr
        self._handler = None

    def on(self, _event, handler):
        self._handler = handler

    async def goto(self, *a, **k):
        if self.serve_xhr and self._handler:
            self._handler(_Resp())
            await asyncio.sleep(0)

    async def wait_for_timeout(self, _ms):
        await asyncio.sleep(0)

    async def evaluate(self, js):
        # JS_USER_CARDS reads the result cards; JS_SCROLL_RESULTS scrolls.
        return CARDS if "a[href^=" in js else True

    async def close(self):
        pass


class _Ctx:
    def __init__(self, serve_xhr: bool):
        self.serve_xhr = serve_xhr

    async def new_page(self):
        return _Page(self.serve_xhr)


@pytest.mark.asyncio
@pytest.mark.parametrize("serve_xhr", [False, True])
async def test_the_users_tab_returns_its_accounts_either_way(serve_xhr):
    """Whether or not TikTok serves the XHR, the accounts on screen come
    back. Before the fix the second case returned []."""
    users = await T._users_for(_Ctx(serve_xhr), "adani", timeout_s=1)

    assert [u.username for u in users] == ["adani_official", "adani_fan"], (
        "accounts rendered on the Users tab were dropped"
    )
    assert all(u.match_kind == "account" for u in users)


@pytest.mark.asyncio
async def test_the_payload_enriches_rather_than_replaces_the_cards():
    """The whole point of the fold: the XHR carries the verified flag, the
    entity id and exact counts that the rendered card does not."""
    users = await T._users_for(_Ctx(True), "adani", timeout_s=1)
    best = next(u for u in users if u.username == "adani_official")

    assert best.verified is True, "the payload's verified flag was not folded in"
    assert best.entity_id == "777", "the payload's entity id was not folded in"
    assert best.follower_count == 12500
    # and the card-only account is still there beside it
    assert any(u.username == "adani_fan" for u in users)


@pytest.mark.asyncio
async def test_a_failed_fold_says_how_many_accounts_it_is_discarding(caplog):
    """Returning [] is indistinguishable from "no accounts matched" to
    every caller. The count being discarded is the one fact that tells the
    two apart -- and its absence is a large part of why the unbound name
    survived as long as it did."""

    class _Boom(_Ctx):
        async def new_page(self):
            page = _Page(True)
            calls = {"n": 0}

            async def _evaluate(js):
                if "a[href^=" in js:
                    calls["n"] += 1
                    if calls["n"] > 1:      # the cards are read, then it breaks
                        raise RuntimeError("cards exploded")
                    return CARDS
                return True

            page.evaluate = _evaluate
            return page

    with caplog.at_level("WARNING"):
        users = await T._users_for(_Boom(True), "adani", timeout_s=1)

    assert users == []
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "discarding" in said, "the loss was not reported at all"
    assert "matched no accounts" in said, (
        "nothing said this keyword will now read as having matched nothing")


def test_the_probe_is_a_real_parameter_not_an_unbound_global():
    """The regression itself, checked structurally: `probe` has to be bound
    in this function's own scope. A free global here compiles fine, passes
    an import check, and raises only on the one line that reads the XHR."""
    code = T._users_for.__code__
    bound = set(code.co_varnames)
    assert "probe" in bound, (
        "`probe` is not a parameter or local of _users_for -- it resolves as "
        "a module global, which does not exist, so the XHR fold raises "
        "NameError into the broad except and returns []"
    )
    assert not hasattr(T, "probe"), (
        "a module-level `probe` would mask this in tests while still being "
        "the wrong object at runtime"
    )
