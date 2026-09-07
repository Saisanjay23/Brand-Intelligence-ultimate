"""Matching a discovered avatar against the client's own reference logos.

PURE LOGIC ONLY -- fingerprinting real bytes and comparing stored hashes,
no Mongo and no network (see this suite's scope note).

WHAT THIS FEATURE IS FOR. The classic impersonation is the RIGHT LOGO with
an off name -- "Reliance Care Support" -- which scores badly on `name_score`
and sinks in triage while wearing the real mark. Matching the picture is
what lifts it back up.

WHAT THESE GUARD

  1. THE HONEST LIMIT. A perceptual hash answers "are these two IMAGES
     near-identical", NOT "does this image CONTAIN this logo". The same mark
     re-drawn on a different background does NOT match, and a test asserts
     that rather than pretending otherwise -- because someone will
     eventually read a 90% recall claim into this tier and be wrong.

  2. THE ANALYST'S FIELD IS NOT OURS. `logo_match` is a human decision
     (scoring.resolve_match reads it). Nothing automated may write it, and
     nothing here may touch `risk_score` or `priority` either: this feature
     ranks and filters, it does not re-score anything that already exists.

  3. KEYWORD SCOPING. A reference attached to one keyword must not be
     compared against profiles found under a different one -- that is what
     stops two unrelated brands under one client matching each other.
"""

import io

import pytest
from PIL import Image, ImageDraw

from backend.services.logo_match import TIER_EXACT, TIER_PHASH, best_match
from backend.shared.imagehashing import (MATCH_MAX_DISTANCE, NEAR_MAX_DISTANCE,
                                          compare, fingerprint)


# ----------------------------------------------------------------- fixtures

def mark(size=512, bg=(255, 255, 255), fg=(0, 90, 180)):
    """A solid brand-mark shape: a filled disc with a bar through it."""
    im = Image.new("RGB", (size, size), bg)
    d = ImageDraw.Draw(im)
    m = size // 6
    d.ellipse([m, m, size - m, size - m], fill=fg)
    d.rectangle([size // 3, size // 2 - size // 12, size - size // 3, size // 2 + size // 12],
                fill=bg)
    return im


def other(size=512):
    im = Image.new("RGB", (size, size), (245, 245, 245))
    ImageDraw.Draw(im).rectangle([60, 60, size - 60, size - 60], fill=(220, 50, 50))
    return im


def enc(im, px=None, fmt="PNG", q=90):
    if px:
        im = im.resize((px, px), Image.LANCZOS)
    b = io.BytesIO()
    if fmt == "JPEG":
        im.convert("RGB").save(b, "JPEG", quality=q)
    else:
        im.save(b, fmt)
    return b.getvalue()


def ref_record(data, *, logo_id="ref1", keyword="acme"):
    import hashlib
    fp = fingerprint(data)
    return {"id": logo_id, "keyword": keyword, "sha": hashlib.sha256(data).hexdigest(),
            "phash": fp.phash, "dhash": fp.dhash}


def candidate(data):
    import hashlib
    fp = fingerprint(data)
    return hashlib.sha256(data).hexdigest(), fp.to_dict()


# -------------------------------------------------------------------- tiers

class TestTiers:
    def test_the_identical_file_is_an_exact_match(self):
        data = enc(mark())
        ref = ref_record(data)
        sha, fp = candidate(data)
        hit = best_match(avatar_sha=sha, fingerprint=fp, references=[ref])
        assert hit is not None
        assert hit.tier == TIER_EXACT
        assert hit.similarity == 100

    def test_exact_wins_without_needing_a_fingerprint(self):
        """An avatar we stored but could not decode still matches on bytes."""
        data = enc(mark())
        ref = ref_record(data)
        sha, _ = candidate(data)
        hit = best_match(avatar_sha=sha, fingerprint=None, references=[ref])
        assert hit is not None and hit.tier == TIER_EXACT

    @pytest.mark.parametrize("px,fmt,q", [(240, "JPEG", 80), (160, "JPEG", 60),
                                          (1024, "PNG", 90), (None, "JPEG", 40)])
    def test_a_re_encoded_copy_matches_on_phash(self, px, fmt, q):
        """The common case: they downloaded the real picture and re-uploaded
        it, so it survives a resize and a recompress but is not byte-equal."""
        ref = ref_record(enc(mark()))
        sha, fp = candidate(enc(mark(), px=px, fmt=fmt, q=q))
        hit = best_match(avatar_sha=sha, fingerprint=fp, references=[ref])
        assert hit is not None, f"{px}px {fmt} q{q} should still match"
        assert hit.tier == TIER_PHASH
        assert hit.similarity >= 80


class TestItDoesNotOverclaim:
    def test_an_unrelated_image_does_not_match(self):
        ref = ref_record(enc(mark()))
        sha, fp = candidate(enc(other()))
        assert best_match(avatar_sha=sha, fingerprint=fp, references=[ref]) is None

    def test_the_same_logo_on_a_new_BACKGROUND_does_not_match(self):
        """THE HONEST LIMIT, pinned deliberately.

        A human sees the same mark; a perceptual hash does not, because it
        fingerprints the whole image and the background dominates it. This
        is exactly the case an embedding tier exists to cover, and this test
        is here so nobody mistakes this tier for one.
        """
        ref = ref_record(enc(mark()))
        sha, fp = candidate(enc(mark(bg=(20, 60, 140))))
        assert best_match(avatar_sha=sha, fingerprint=fp, references=[ref]) is None

    def test_a_near_miss_is_dropped_rather_than_reported_weakly(self):
        """Between the match and near thresholds nothing is returned. A 70%
        'logo match' on an unrelated picture spends analyst trust, and trust
        is the only thing that makes the badge worth showing."""
        assert MATCH_MAX_DISTANCE < NEAR_MAX_DISTANCE
        ref = ref_record(enc(mark()))
        sha, fp = candidate(enc(other()))
        cmp = compare(fp, ref)
        assert cmp is not None and not cmp.is_match


class TestChoosingAmongReferences:
    def test_the_closest_reference_wins(self):
        ref_exact = ref_record(enc(mark()), logo_id="close")
        ref_far = ref_record(enc(other()), logo_id="far")
        sha, fp = candidate(enc(mark(), px=240, fmt="JPEG", q=80))
        hit = best_match(avatar_sha=sha, fingerprint=fp, references=[ref_far, ref_exact])
        assert hit is not None and hit.ref_id == "close"

    def test_an_exact_hit_beats_a_perceptual_one(self):
        data = enc(mark())
        ref_bytes = ref_record(data, logo_id="identical")
        ref_similar = ref_record(enc(mark(), px=300), logo_id="similar")
        sha, fp = candidate(data)
        hit = best_match(avatar_sha=sha, fingerprint=fp, references=[ref_similar, ref_bytes])
        assert hit is not None and hit.ref_id == "identical" and hit.tier == TIER_EXACT

    def test_no_references_means_no_match(self):
        sha, fp = candidate(enc(mark()))
        assert best_match(avatar_sha=sha, fingerprint=fp, references=[]) is None

    def test_a_reference_with_no_id_is_skipped(self):
        ref = ref_record(enc(mark()))
        ref["id"] = ""
        sha, fp = candidate(enc(mark()))
        assert best_match(avatar_sha=sha, fingerprint=fp, references=[ref]) is None


class TestWhatItWritesBack:
    def test_it_only_ever_writes_its_own_three_fields(self):
        """It must not touch `logo_match` (the analyst's call), `has_logo`,
        `risk_score` or `priority`."""
        data = enc(mark())
        sha, fp = candidate(data)
        hit = best_match(avatar_sha=sha, fingerprint=fp, references=[ref_record(data)])
        assert set(hit.as_fields()) == {"logo_similarity", "logo_ref_id", "logo_match_tier"}


class TestFingerprinting:
    def test_transparency_is_flattened_consistently(self):
        """The same mark saved with an alpha channel and without must
        fingerprint the same, or one upload silently fails to match the
        other."""
        opaque = enc(mark())
        rgba = mark().convert("RGBA")
        b = io.BytesIO(); rgba.save(b, "PNG")
        cmp = compare(fingerprint(opaque), fingerprint(b.getvalue()))
        assert cmp is not None and cmp.is_match

    def test_junk_bytes_fingerprint_to_nothing_rather_than_raising(self):
        assert fingerprint(b"not an image") is None
        assert fingerprint(b"") is None


# ---------------------------------------------------------------- tier 2
#
# OPT-IN, and off by default. This suite's contract is pure logic that runs
# in under two seconds; loading CLIP costs ~4s and several hundred MB, which
# would break that for every run. Enable deliberately:
#
#     BI_TEST_EMBEDDING=1 pytest backend/tests/test_logo_match.py
#
# The tier itself is designed to be absent -- `available()` returning False
# degrades every caller to the hash tiers -- so a skipped run here is not a
# gap in coverage of the shipped default behaviour.

import os

_EMBED_TESTS = os.environ.get("BI_TEST_EMBEDDING") == "1"


@pytest.mark.skipif(not _EMBED_TESTS, reason="set BI_TEST_EMBEDDING=1 to run (loads CLIP)")
class TestEmbeddingTier:
    """What the hash tier provably cannot do, and the line it must not cross."""

    def _ref(self, data, logo_id="ref1"):
        from backend.shared.imageembedding import embed
        r = ref_record(data, logo_id=logo_id)
        r["embedding"] = embed(data)
        return r

    def _cand(self, data):
        from backend.shared.imageembedding import embed
        sha, fp = candidate(data)
        return sha, fp, embed(data)

    def test_the_model_is_actually_loadable(self):
        from backend.shared.imageembedding import available
        assert available(), "BI_TEST_EMBEDDING=1 but the model could not load"

    def test_it_catches_the_logo_shrunk_into_a_corner(self):
        """The case the hash tier misses: same mark, re-presented small and
        off-centre. This is the entire reason tier 2 exists."""
        from backend.services.logo_match import TIER_EMBED
        ref = self._ref(enc(mark()))
        sha, fp, vec = self._cand(enc(mark().resize((160, 160)).crop((-100, -100, 412, 412))))
        hit = best_match(avatar_sha=sha, fingerprint=fp, references=[ref], embedding=vec)
        assert hit is not None
        assert hit.tier == TIER_EMBED

    def test_an_unrelated_image_still_does_not_match(self):
        """Tier 2 widens recall; it must not widen it onto the wrong side.
        A semantic model is exactly the thing that could start calling two
        different brands the same, so this is the test that matters most."""
        ref = self._ref(enc(mark()))
        sha, fp, vec = self._cand(enc(other()))
        assert best_match(avatar_sha=sha, fingerprint=fp, references=[ref], embedding=vec) is None

    def test_a_hash_hit_outranks_an_embedding_hit(self):
        """`exact` and `phash` are the more certain claims and must win, so
        the badge shows the strongest evidence available rather than
        whichever tier happened to run last."""
        from backend.services.logo_match import TIER_EXACT
        data = enc(mark())
        ref = self._ref(data)
        sha, fp, vec = self._cand(data)
        hit = best_match(avatar_sha=sha, fingerprint=fp, references=[ref], embedding=vec)
        assert hit is not None and hit.tier == TIER_EXACT

    def test_a_missing_embedding_degrades_instead_of_failing(self):
        """A reference uploaded while the model was unavailable has no
        vector. It must still work through the hash tiers rather than throw."""
        ref = ref_record(enc(mark()))          # no "embedding" key at all
        sha, fp = candidate(enc(mark(), px=240, fmt="JPEG", q=80))
        hit = best_match(avatar_sha=sha, fingerprint=fp, references=[ref], embedding=None)
        assert hit is not None and hit.tier == TIER_PHASH
