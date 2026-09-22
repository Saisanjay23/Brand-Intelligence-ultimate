"""The 2FA code generator, against the standard's own test vectors.

WHY THIS IS TESTED AGAINST RFC 6238 RATHER THAN AGAINST ITSELF. A TOTP
implementation that is subtly wrong -- a time step of 60 instead of 30, a
secret decoded as ASCII instead of base32 -- produces six perfectly
plausible digits that are simply never accepted. There is no error, no
exception and nothing in a log: the login just fails at the 2FA prompt for
ever, and it reads exactly like a wrong password. Checking the library
against the published vectors is what makes "the code is correct" a fact
rather than an assumption.

The vectors below are the SHA-1 rows of RFC 6238 Appendix B, whose shared
secret is the ASCII string "12345678901234567890".
"""

from __future__ import annotations

import base64

import pyotp
import pytest

from backend.stealth.auto_login import totp_now

# RFC 6238 Appendix B's secret, base32-encoded the way an authenticator app
# would present it.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()

# (unix time, expected 8-digit SHA-1 code)
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize("at_time,expected", RFC_VECTORS)
def test_totp_matches_the_rfc_6238_vectors(at_time, expected):
    assert pyotp.TOTP(RFC_SECRET, digits=8).at(at_time) == expected


@pytest.mark.parametrize("at_time,expected", RFC_VECTORS)
def test_the_six_digit_codes_we_actually_send_are_the_same_algorithm(at_time, expected):
    """Platforms ask for six digits, which is the last six of the vector --
    the truncation is the only difference, not a different computation."""
    assert pyotp.TOTP(RFC_SECRET).at(at_time) == expected[-6:]


def test_totp_now_produces_the_current_code():
    assert totp_now(RFC_SECRET) == pyotp.TOTP(RFC_SECRET).now()


def test_a_secret_pasted_from_an_authenticator_app_still_works():
    """Authenticator setup screens show the secret in spaced groups and
    sometimes lower-cased. An operator pastes exactly what they see, and a
    secret that is rejected for its whitespace is a login that fails with no
    usable explanation."""
    spaced = " ".join(RFC_SECRET[i:i + 4] for i in range(0, len(RFC_SECRET), 4))
    assert totp_now(spaced.lower()) == pyotp.TOTP(RFC_SECRET).now()
    assert totp_now(f"  {RFC_SECRET}  ") == pyotp.TOTP(RFC_SECRET).now()
    assert totp_now(RFC_SECRET.replace("GEZ", "GEZ-", 1)) == pyotp.TOTP(RFC_SECRET).now()


def test_an_empty_secret_is_refused_rather_than_guessed():
    """An account with no 2FA secret must not silently send a code derived
    from nothing; the caller needs to know there is nothing to send."""
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            totp_now(bad)  # type: ignore[arg-type]
