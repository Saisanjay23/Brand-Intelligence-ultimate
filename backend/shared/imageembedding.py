"""CLIP image embeddings -- the tier that catches a logo RE-PRESENTED.

WHY THIS EXISTS. A perceptual hash (shared/imagehashing.py) fingerprints the
whole image, so the same brand mark on a new background is a different
fingerprint: measured at 6% similarity for a case a human calls identical.
That is not a bug in the hash, it is the question it answers. An embedding
answers the other one -- "is this the same THING" -- and catches the
impersonator who put the real logo on a blue square.

WHY IT IS THE SECOND TIER AND NOT THE ONLY ONE. It is ~70ms an image against
~11ms, it needs a 600MB model, and it is SEMANTIC, which cuts both ways: it
groups things that look alike to a person, including two different companies
that both use a navy polygon. The hash tier is exact and free of that failure
mode, so it runs first and this only handles what it could not.

THE THRESHOLD IS MEASURED, NOT ASSUMED. Calibrated over five brand
identities, each re-presented five ways (background swap, greyscale ground,
recolour, shrunk into a corner, re-encode), scored against every cross-brand
pair -- 25 positives, 100 negatives:

        worst positive          0.883
        strongest negative      0.832      (a featureless disc vs another disc)
        chosen threshold        0.88   ->  100% recall, 100% precision, 0 FP

0.88 sits in the empty band between the two, biased toward the negatives'
side of the midpoint because real logos vary more than synthetic ones and a
false "logo match" costs analyst trust that a miss does not.

WHERE IT IS WEAK, HONESTLY. Separation depends on the mark being
DISTINCTIVE. Two plain coloured discs from different companies scored 0.898
against each other in an earlier run -- above where a corner-placed true
match landed. For a brand whose logo is a bare geometric shape with no
wordmark, this tier cannot reliably tell it from a lookalike, and the
threshold is set to refuse rather than guess. Distinctive marks (shape plus
wordmark) separated by 0.24, which is the case it serves well.

OPTIONAL BY CONSTRUCTION. If torch, transformers or the model weights are
absent, `available()` is False and every caller degrades to the hash tier
alone. Nothing here may make discovery fail to start.
"""

from __future__ import annotations

import io
import threading
from typing import Optional

from backend.shared.logging import get_logger

log = get_logger("imageembedding")

MODEL_ID = "openai/clip-vit-base-patch32"

# Cosine similarity at or above which two images are the same mark. See the
# calibration in this module's docstring.
MATCH_MIN_SIMILARITY = 0.88

_model = None
_processor = None
_dim: Optional[int] = None
_load_failed = False
# Loading is not thread-safe, and neither is sharing one torch module across
# the avatar-cache worker threads. Inference is serialised: at ~70ms an image
# a whole sweep costs a few seconds, off the critical path, and predictable
# beats fast here.
_lock = threading.Lock()


def _load() -> bool:
    """Load the model once, on first real use. NOT at import: it costs ~4s
    and several hundred MB, and a process that never scores a logo (every
    process, until a client uploads a reference) must not pay either."""
    global _model, _processor, _dim, _load_failed
    if _model is not None:
        return True
    if _load_failed:
        return False
    try:
        import torch                                    # noqa: F401
        from transformers import CLIPImageProcessor, CLIPModel

        model = CLIPModel.from_pretrained(MODEL_ID).eval()
        processor = CLIPImageProcessor.from_pretrained(MODEL_ID)
        _model, _processor = model, processor
        _dim = int(getattr(model.config, "projection_dim", 512))
        log.info(f"logo embedding model ready: {MODEL_ID} (dim {_dim})")
        return True
    except Exception as e:                               # noqa: BLE001
        # Missing package, missing weights, no disk, no network on first
        # fetch. All the same outcome: this tier is simply off.
        _load_failed = True
        log.warning(f"logo embedding unavailable ({type(e).__name__}: {e}) -- "
                    "falling back to perceptual hashing alone")
        return False


def available() -> bool:
    """Whether the embedding tier can run at all. Cheap after the first call."""
    with _lock:
        return _load()


def embed(data: bytes) -> Optional[list[float]]:
    """A unit-length embedding of these image bytes, or None.

    SYNCHRONOUS AND CPU-BOUND -- callers must run it in a thread. On the
    event loop it would stall the sweep sharing that loop, the same hazard
    documented on shared/imagehashing.py, only ~6x worse per image.

    Unit-length on purpose: cosine similarity is then a plain dot product,
    so comparing a stored candidate against stored references needs no
    normalisation and no model at query time.
    """
    if not data:
        return None
    with _lock:
        if not _load():
            return None
        try:
            import torch
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                rgb = im.convert("RGB")
                with torch.no_grad():
                    x = _processor(images=rgb, return_tensors="pt")
                    out = _model.get_image_features(**x)
                    # transformers 4 returns a tensor; 5 returns a pooled
                    # output object. Handle both rather than pin a version.
                    v = out if torch.is_tensor(out) else out.pooler_output
                    if v.shape[-1] != _dim:
                        v = _model.visual_projection(v)
                    v = v[0]
                    v = v / v.norm()
                    return [float(f) for f in v.tolist()]
        except Exception as e:                           # noqa: BLE001
            log.warning(f"embed failed: {type(e).__name__}: {e}")
            return None


def similarity(a: Optional[list[float]], b: Optional[list[float]]) -> Optional[float]:
    """Cosine similarity of two stored embeddings, or None if either is
    missing or malformed. Both are unit-length, so this is a dot product --
    pure arithmetic, no model, no decoding."""
    if not a or not b or len(a) != len(b):
        return None
    return float(sum(x * y for x, y in zip(a, b)))


def is_match(score: Optional[float]) -> bool:
    return score is not None and score >= MATCH_MIN_SIMILARITY
