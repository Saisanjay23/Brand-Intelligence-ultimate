"""Reference logos: the client's own brand marks, used to spot impersonators.

    GET    /clients/{client_id}/logos          list the references
    POST   /clients/{client_id}/logos          upload one (multipart)
    GET    /clients/{client_id}/logos/{id}/image   the stored bytes
    DELETE /clients/{client_id}/logos/{id}     remove one

WHAT THIS IS FOR. An analyst configuring a client's keywords can attach the
real brand mark to a parent keyword. Profiles later discovered under that
keyword have their cached avatar compared against it, and a hit lifts them
to the top of triage with a "logo match" badge. It is entirely OPTIONAL --
a client with no logos behaves exactly as before.

WHAT IT DOES NOT DO. A match never sets `logo_match` (the analyst's own
field) and never changes `risk_score` or `priority`. It ranks and filters,
nothing more -- see services/logo_match.py for why that restraint is the
design rather than a limitation.

The upload is fingerprinted ONCE, here, and the hashes stored beside it, so
comparing a whole sweep later is arithmetic over hex strings rather than
image decoding.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, File, Form, Path, Response, UploadFile, status
from pydantic import BaseModel, Field

from backend.database.repositories import client_repository as clients_db
from backend.database.repositories import logo_repository as logos_db
from backend.shared.errors import ValidationError
from backend.shared.imagefetch import MAX_BYTES
from backend.shared.imageembedding import available as embedding_available
from backend.shared.imageembedding import embed
from backend.shared.imagehashing import fingerprint
from backend.shared.logging import get_logger

router = APIRouter(prefix="/clients", tags=["clients"])
log = get_logger("api.logos")

# A brand mark is a small file. The cap is the same one the avatar fetcher
# uses, so nothing that can be stored as an avatar is too big to be a
# reference for it.
MAX_LOGO_BYTES = MAX_BYTES

_ALLOWED_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp")


class LogoOut(BaseModel):
    id: str
    client_id: str
    keyword: str = Field("", description="The parent keyword this mark belongs to. "
                                         "Empty means it applies to every keyword.")
    kind: str = Field("", description="individual | domain, when it came from a keyword group.")
    sha: str = Field(..., description="sha256 of the image bytes.")
    phash: str = ""
    dhash: str = ""
    width: int = 0
    height: int = 0
    bytes: int = 0
    filename: str = ""
    content_type: str = "image/png"


class LogoList(BaseModel):
    items: list[LogoOut]


@router.get("/{client_id}/logos", response_model=LogoList,
            summary="List a client's reference logos")
async def list_logos(client_id: str = Path(...)) -> dict:
    return {"items": await logos_db.list_for_client(client_id)}


@router.post("/{client_id}/logos", response_model=LogoOut,
             status_code=status.HTTP_201_CREATED,
             summary="Upload a reference logo")
async def upload_logo(
    client_id: str = Path(...),
    file: UploadFile = File(..., description="PNG/JPEG/WebP image of the brand mark."),
    keyword: str = Form("", description="Parent keyword to attach it to. Blank = client-wide."),
    kind: str = Form("", description="individual | domain."),
) -> dict:
    """Stores the mark and its perceptual fingerprints.

    404 if the client does not exist -- a logo with no client behind it
    could never be compared against anything, so accepting it would only
    create an orphan.
    """
    await clients_db.get(client_id)          # raises NotFoundError -> 404

    data = await file.read()
    if not data:
        raise ValidationError("the uploaded file is empty")
    if len(data) > MAX_LOGO_BYTES:
        raise ValidationError(f"image is larger than {MAX_LOGO_BYTES // (1024 * 1024)}MB")

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type and content_type not in _ALLOWED_TYPES:
        raise ValidationError(f"{content_type!r} is not a supported image type")

    # Off the event loop: decoding is CPU-bound, and this route shares its
    # loop with any sweep running at the time.
    fp = await asyncio.to_thread(fingerprint, data)
    if fp is None:
        # The bytes did not decode. Refused rather than stored, because a
        # reference without a fingerprint can never match anything and would
        # sit in the UI looking like configured protection that does nothing.
        raise ValidationError("that file could not be read as an image")

    # The embedding for the tier that catches this mark RE-PRESENTED. Also
    # off the event loop (~70ms), and optional: if the model is unavailable
    # the reference still works through the hash tiers, so an upload is never
    # refused for want of it.
    vector = await asyncio.to_thread(embed, data) if embedding_available() else None

    return await logos_db.add(
        client_id, data=data, fingerprint=fp,
        keyword=keyword, kind=kind, embedding=vector,
        filename=file.filename or "", content_type=content_type or "image/png",
    )


@router.get("/{client_id}/logos/{logo_id}/image",
            summary="The stored bytes of one reference logo")
async def logo_image(client_id: str = Path(...), logo_id: str = Path(...)) -> Response:
    """Served from our own origin so the UI can show the reference beside the
    candidate avatar -- which is what lets an analyst confirm or dismiss a
    match by eye in one glance."""
    logo = await logos_db.get(logo_id)
    if logo.get("client_id") != client_id:
        raise ValidationError("that logo does not belong to this client")
    found = await logos_db.read_bytes(logo.get("sha", ""))
    if not found:
        raise ValidationError("the stored image for that logo is missing")
    data, ctype = found
    return Response(
        content=data,
        media_type=ctype,
        headers={
            # Content-addressed, so the bytes behind a given id never change.
            "Cache-Control": "public, max-age=31536000, immutable",
            "Cross-Origin-Resource-Policy": "cross-origin",
        },
    )


@router.delete("/{client_id}/logos/{logo_id}", response_model=LogoOut,
               summary="Remove a reference logo")
async def delete_logo(client_id: str = Path(...), logo_id: str = Path(...)) -> dict:
    logo = await logos_db.get(logo_id)
    if logo.get("client_id") != client_id:
        raise ValidationError("that logo does not belong to this client")
    return await logos_db.delete(logo_id)
