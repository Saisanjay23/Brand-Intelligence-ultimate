"""A platform whose session status cannot be read stays on the list.

GET /sessions used to log the failure and `continue`, so the platform simply
vanished from the Sessions page -- which reads as "nothing to see here", the
silent clean zero. It must come back as an explicit "unknown" row instead,
never as "ready".

Also: a live check must refuse while a re-login is signing the same account
in, for the same two-browsers-on-one-account reason it refuses while a job
holds it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.api import sessions as api
from backend.api.models import SessionPoolList
from backend.sessions import manager as M


@pytest.mark.asyncio
async def test_a_platform_whose_status_raises_is_listed_as_unknown_not_dropped():
    from backend.platforms import registry

    async def status(pid, _health=None):
        if pid == "instagram":
            raise RuntimeError("database is locked")
        return {"platform": pid, "name": pid, "state": "ready", "kind": "cookies"}

    with patch.object(M, "cached_health", AsyncMock(return_value={})), \
         patch.object(M, "status", side_effect=status):
        out = await api.get_all_session_status()

    # Every platform is still there -- none silently missing.
    assert [i["platform"] for i in out["items"]] == list(registry.PLATFORMS)
    broken = next(i for i in out["items"] if i["platform"] == "instagram")
    assert broken["state"] == "unknown"
    assert broken["state"] != "ready"
    assert "database is locked" in broken["message"]
    assert broken["name"] == registry.PLATFORMS["instagram"].name
    # And it validates against the response model the route declares, so
    # the placeholder cannot turn a partial failure into a 500.
    SessionPoolList.model_validate(out)


@pytest.mark.asyncio
async def test_a_check_is_refused_while_a_relogin_is_running_for_that_account():
    item = {"id": "s1", "identifier": "acct-1", "cookies": [{"name": "x"}]}
    verify = AsyncMock(return_value=(True, "ok", True))
    M._relogin_running.add("facebook:s1")
    try:
        with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=item)), \
             patch.object(M, "_session_in_use", return_value=False), \
             patch.object(M, "_linked_claimed", return_value=False), \
             patch.object(M, "verify_session_item", verify):
            with pytest.raises(M.ConflictError, match="signed back in"):
                await M.check_item("facebook", "s1")
    finally:
        M._relogin_running.discard("facebook:s1")
    verify.assert_not_awaited()
