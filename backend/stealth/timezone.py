"""The IANA timezone id a session's browser context claims.

ONE DEFAULT, ON PURPOSE. A claimed timezone that disagrees with the real
egress IP's geography is a worse tell than every session sharing one, and
with no per-session egress there is nothing for a diversified timezone to
agree WITH -- every context leaves from this host's own address, so they
genuinely are all in one place. Claiming so is the honest answer.

(This used to derive the id from a proxy's declared region, which was the
one case where a non-default was safe. Proxy support has since been removed
from the tool, so that branch went with it.)
"""

from __future__ import annotations

DEFAULT_TIMEZONE_ID = "Asia/Kolkata"
