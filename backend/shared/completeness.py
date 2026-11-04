"""What "fully scraped" means for one analysed profile, in one place.

THE GAP THIS CLOSES
    `profile_repository.RETRYABLE_ANALYSIS_STATUSES` already re-queues a
    row whose analysis STATUS says the profile was never actually looked
    at (ERROR/CHECKPOINT/LOGIN_REQUIRED/PARTIAL). It has no opinion at all
    about a row that came back status OK carrying half its fields, and
    that is the shape almost every real loss takes: the page loaded, the
    name and follower count came through, and the last-post date (or the
    evidence screenshot) simply was not on screen yet when it was read.

    Measured on one 663-URL run before this existed: 76 of 148 Twitter
    rows had no last-post date, 82 of 211 Instagram rows had none, and 28
    Instagram rows had no screenshot -- every one of them status OK or
    PARTIAL, none of them ever retried, because nothing in the pipeline
    could tell "this profile genuinely has no posts" apart from "we did
    not manage to read them this time".

WHY IT IS SAFE TO RETRY ON THIS
    `profile_repository.save()` writes only non-blank values
    (`v not in (None, "", {})`), so attempts MERGE: a second visit can
    fill a field the first one missed and can never blank one the first
    one got. Re-attempting an incomplete row is therefore monotonic -- it
    either improves the row or leaves it exactly as it was.

WHY EVERY RULE BELOW IS CONSERVATIVE
    A field is only required when the platform genuinely publishes it for
    that KIND of profile. "Missing" has to mean "we failed to read it",
    never "this account legitimately has none", or a private account with
    no visible posts would burn its whole retry budget on every sweep
    forever and then surface as a false coverage gap. Hence the explicit
    carve-outs for confirmed-zero-post accounts, private/protected
    profiles, groups, and profiles that are already gone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.shared.models.row import Row

# Which platforms' analysis engines actually READ a location, established by
# reading the engines rather than assuming: facebook, twitter and youtube
# each have a location reader; instagram, tiktok and telegram have none at
# all. That matches the live data exactly -- location is blank on 100% of
# instagram (309/309) and tiktok (11/11) rows, and only sometimes blank on
# twitter (52%) and facebook (83%), which is the signature of a field that
# IS read but is genuinely optional for the account holder to set.
#
# The distinction matters for reporting, not for retry: a blank location on
# instagram is not a miss to chase, it is a field this pipeline never claims
# to collect there. Without this, 320 rows read as "data missing" forever
# and no amount of re-analysis would ever change one of them.
#
# Instagram JOINED this set on 2026-08-22. It was correctly excluded before:
# the engine genuinely had no location reader, so 309/309 blank rows were
# honestly "not-collected". It now reads `city_name` off the same
# `data.user` object it reads followers and biography from (confirmed live
# -- the key is present on every profile payload, populated on
# professional/business accounts and empty on most personal ones). A blank
# location on Instagram therefore now means what it already meant on
# Facebook and Twitter: the account holder did not set one. That changes how
# a blank is EXPLAINED (see field_report below), not whether it triggers a
# retry -- location is never one of the fields `missing_fields` checks for
# on ANY platform, cookie-authed or not, because its absence is never, on
# its own, evidence the scraper failed: a platform publishes it only if the
# account holder filled it in. Re-visiting a profile that simply has no
# location set would burn its retry budget on something no visit could ever
# produce, so it is left out of that function entirely rather than gated by
# a lookup table.
PLATFORMS_WITH_LOCATION = frozenset({"facebook", "twitter", "youtube", "instagram"})

# The platforms whose analysis engine can produce an evidence screenshot at
# all. Established by reading the engines, not assumed: facebook, instagram,
# twitter and tiktok each define `Scraper.screenshot`; youtube (official Data
# API) and telegram (MTProto) define NO such method, because neither one
# opens a page to capture.
#
# `want_screenshot` is a run-level setting (`settings.capture_evidence`), so
# with capture on, every YouTube and Telegram row was judged against a field
# its platform is structurally incapable of producing. Measured on
# 2026-08-23, out of 86 rows sitting in the retry queue:
#
#   telegram  10 rows whose ONLY unmet field was `screenshot` -- name,
#             followers and last-post all read
#   youtube    1 row, same shape (followers 148000, last post read)
#
# All 11 were status OK, permanently `analysis_complete: False`, and were
# re-queued on every sweep to chase a capture that could never happen.
PLATFORMS_WITH_SCREENSHOT = frozenset({"facebook", "instagram", "twitter", "tiktok", "threads"})

# Statuses where re-reading cannot help: the profile is gone, so there is
# nothing further to scrape and its blank fields are the honest answer.
TERMINAL_STATUSES = ("GONE",)

# A note that explains a legitimately unreadable timeline. Both are set by
# the engines themselves (twitter: "protected account -- posts not
# visible"; instagram: private accounts), and both mean the posts are
# hidden from this session by the platform, not missed by the scraper.
_HIDDEN_TIMELINE_MARKERS = ("protected", "private")

# A note meaning the platform publishes no audience number for this profile
# at all -- not that we failed to read one. Set by
# facebook/analysis_engine.py::read_counts (see NO_AUDIENCE_NOTE there)
# only after every count tier came up empty on a profile whose entity DID
# resolve, which live checking showed to be a real, permanent property of
# brand-new locked-down personal profiles rather than a scraping failure:
# the number is absent from the timeline, from /friends, from /about, from
# /about_profile_transparency, and from the GraphQL payload.
#
# Without this, 28 such rows counted as missing their followers on every
# single sweep -- permanently re-queued for a retry that could never
# succeed, and permanently reported to the analyst as data loss.
_NO_AUDIENCE_MARKERS = ("publishes no audience count",)


def _blank(value) -> bool:
    """Numeric 0 is a real reading (a brand-new account really can have 0
    followers); only None counts as never-read. Mirrors the same
    distinction `analysis_service._field_blank` already draws."""
    if value is None:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return False
    return not str(value).strip()


def _timeline_hidden(row: "Row") -> bool:
    notes = (row.notes or "").lower()
    return any(m in notes for m in _HIDDEN_TIMELINE_MARKERS)


def _no_audience_published(row: "Row") -> bool:
    notes = (row.notes or "").lower()
    return any(m in notes for m in _NO_AUDIENCE_MARKERS)


def _can_screenshot(platform_id: str, want_screenshot: bool) -> bool:
    """Is a screenshot both switched on for this run AND possible on this
    platform? See PLATFORMS_WITH_SCREENSHOT."""
    return want_screenshot and platform_id in PLATFORMS_WITH_SCREENSHOT


def _telegram_user(platform_id: str, row: "Row") -> bool:
    """A Telegram USER account, as opposed to a channel or a group.

    Telegram publishes neither of two things for a user, and both are
    properties of the PROTOCOL rather than of the account:

      * no member count. `entity_from` reads it as
        `getattr(obj, "participants_count", None)`, and a Telethon `User`
        object simply has no such attribute -- only Channel/Chat do.
      * no readable feed. `Telegram.last_post` says it outright in its own
        docstring: "Users' own messages are not readable; channels' are."

    So a user row comes back with blank followers and blank last-post no
    matter how many times it is visited. The module already encodes the
    third member of this family -- `created_iso` is guarded by
    `if kind != "profile"` in entity_from, and analysis_engine.py notes
    "telegram exposes no creation date for user accounts" -- this extends
    the same honesty to the other two.

    Measured 2026-08-23: t.me/CA_NITIN_MURARKA6, entity_type "profile",
    sat at 6 attempts with followers, last_post and screenshot all marked
    MISSED. All three are structural; none was ever obtainable.
    """
    return platform_id == "telegram" and row.entity_type == "profile"


def missing_fields(platform_id: str, row: "Row", *, want_screenshot: bool) -> list[str]:
    """The fields this row should have carried and did not.

    Empty list means "nothing further to gain from another visit" -- which
    includes a genuinely empty profile, not only a perfectly full one.
    `want_screenshot` is the run's evidence setting (see
    `analysis_service._evidence_dir`): with capture switched off, a missing
    screenshot is configuration, not a gap.
    """
    if row.status in TERMINAL_STATUSES:
        return []

    missing: list[str] = []

    if _blank(row.profile_name):
        missing.append("display name")

    # Facebook groups publish a member count under neither `followers` nor
    # `friends`; every other entity publishes one of the two (a Page its
    # followers, a personal profile its friends -- see
    # facebook/analysis_engine.py::followers_from_friends).
    if (
        row.entity_type != "group"
        and _blank(row.followers)
        and _blank(row.friends)
        and not _no_audience_published(row)
        and not _telegram_user(platform_id, row)
    ):
        missing.append("followers")

    # "no posts at all" (posts_seen == "no") is a real, extracted answer,
    # and a hidden timeline is the platform's decision, not a miss.
    if (
        _blank(row.last_post_iso)
        and row.posts_seen != "no"
        and not _timeline_hidden(row)
        and not _telegram_user(platform_id, row)
    ):
        missing.append("last post date")

    if _can_screenshot(platform_id, want_screenshot) and _blank(row.screenshot):
        missing.append("screenshot")

    return missing


def is_complete(platform_id: str, row: "Row", *, want_screenshot: bool) -> bool:
    return not missing_fields(platform_id, row, want_screenshot=want_screenshot)


def field_report(platform_id: str, row: "Row", *, want_screenshot: bool) -> dict[str, str]:
    """Why each field is or isn't populated -- one verdict per field, so a
    blank cell downstream can always say WHICH of the two very different
    things it means.

    This is the whole point of the exercise: "no last post date" currently
    reads the same in the database whether the account has never posted or
    the timeline failed to load, and those demand opposite responses (leave
    it alone / go get it). Every verdict here is derived from a signal the
    engine already produced during the visit and, until now, discarded.

        "read"          the value is there
        "none-exist"    confirmed absent AT the profile -- the account really
                        has no posts (posts_seen == "no"), or its timeline is
                        private/protected so the platform itself withholds
                        them. NOT a miss; re-visiting cannot change it.
        "not-collected" this platform's engine does not read this field at
                        all (e.g. location on instagram/tiktok/telegram), or
                        the run had evidence capture switched off. Also not a
                        miss -- nothing ever promised to fill it.
        "unknown"       the profile is gone, so its blanks are simply the
                        honest answer and cannot be improved on.
        "MISSED"        the platform does publish this and the account does
                        have it, and we still came away empty. This is the
                        only verdict that means real data loss, and the only
                        one worth an analyst's or a retry's attention.
    """
    gone = row.status in TERMINAL_STATUSES
    missed = set() if gone else set(missing_fields(
        platform_id, row, want_screenshot=want_screenshot))

    def verdict(field: str, value, *, collected: bool = True, none_exist: bool = False) -> str:
        if not _blank(value):
            return "read"
        if gone:
            return "unknown"
        if not collected:
            return "not-collected"
        if none_exist:
            return "none-exist"
        return "MISSED" if field in missed else "none-exist"

    return {
        "display_name": verdict("display name", row.profile_name),
        "followers": verdict(
            "followers",
            row.followers if not _blank(row.followers) else row.friends,
            # Facebook groups publish a member count under neither field,
            # and Telegram publishes none for a user account.
            collected=(row.entity_type != "group"
                       and not _telegram_user(platform_id, row)),
            # A profile the platform publishes no audience number for is
            # confirmed-absent at the source, not missed by the scraper.
            none_exist=_no_audience_published(row),
        ),
        "last_post_date": verdict(
            "last post date", row.last_post_iso,
            # A Telegram user's own messages are not readable over MTProto
            # at all, so this is a field never collected there rather than
            # one that went missing.
            collected=not _telegram_user(platform_id, row),
            # The two carve-outs missing_fields already honours, named
            # explicitly here instead of silently folded into "not missing":
            # an account with confirmed zero posts, and one whose timeline
            # the platform hides from this session.
            none_exist=row.posts_seen == "no" or _timeline_hidden(row),
        ),
        "location": verdict(
            "location", row.location,
            collected=platform_id in PLATFORMS_WITH_LOCATION,
            # Read where supported, but never required: an account that
            # simply never set one is indistinguishable from a failed read,
            # so this is honestly reported as absent rather than as a miss.
            none_exist=True,
        ),
        "screenshot": verdict(
            "screenshot", row.screenshot,
            collected=_can_screenshot(platform_id, want_screenshot)),
    }
