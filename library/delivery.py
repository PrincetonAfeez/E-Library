"""Secure digital content delivery: signed reader tokens, watermarking, streaming.
 
This replaces the bare ``content_url`` hand-off with a real reader/player pipeline:

* Content lives in a DB-backed blob store (fully offline/testable); a
  storage-backed store can be swapped in for production.
* Access is granted through short-lived, signed *content tokens* bound to an
  active loan — never a durable public URL.
* Text is delivered chapter-by-chapter with a per-loan **social-DRM watermark**;
  binary formats (PDF/EPUB/audio) carry a per-loan fingerprint header and are
  access-logged for traceability.
* Reading position syncs across devices via ``ReadingProgress``.
"""

from __future__ import annotations

from django.core import signing
from django.utils import timezone

from .models import (
    DigitalAccessLog,
    DigitalAsset,
    DigitalLoan,
    DigitalLoanStatus,
    ReadingProgress,
    StoredBlob,
    stable_patron_hash,
)
from .services import DomainError

# Content tokens are single-capability and short-lived; the reader refreshes
# them from the manifest as needed while the loan remains active.
CONTENT_TOKEN_TTL = 3600
_SALT = "library.digital.reader"


# --------------------------------------------------------------------------- #
# Blob store
# --------------------------------------------------------------------------- #
def prune_orphan_blobs(*, older_than_hours: int = 24, now=None) -> int:
    """Delete stored blobs no longer referenced by any DigitalAsset.

    A grace window avoids racing a just-uploaded blob whose asset row isn't
    wired up yet.
    """
    from datetime import timedelta

    from .models import DigitalAsset

    now = now or timezone.now()
    referenced = set(
        DigitalAsset.objects.exclude(media_key="").values_list("media_key", flat=True)
    )
    orphans = StoredBlob.objects.exclude(key__in=referenced).filter(
        created_at__lt=now - timedelta(hours=older_than_hours)
    )
    count = orphans.count()
    orphans.delete()
    return count


def store_blob(key: str, data: bytes, *, content_type: str = "application/octet-stream") -> StoredBlob:
    blob, _ = StoredBlob.objects.update_or_create(
        key=key,
        defaults={"data": data, "content_type": content_type, "byte_size": len(data)},
    )
    return blob


def read_blob(key: str) -> tuple[bytes, str]:
    blob = StoredBlob.objects.filter(key=key).first()
    if blob is None:
        raise DomainError("Content is not available.")
    return bytes(blob.data), blob.content_type


# --------------------------------------------------------------------------- #
# Signed content tokens
# --------------------------------------------------------------------------- #
def mint_content_token(loan: DigitalLoan, *, locator: str = "") -> str:
    return signing.dumps({"loan": loan.pk, "loc": locator}, salt=_SALT)


def resolve_content_token(token: str, *, max_age: int = CONTENT_TOKEN_TTL) -> tuple[DigitalLoan, str]:
    """Validate a content token and return (active loan, locator) or raise."""
    try:
        payload = signing.loads(token, salt=_SALT, max_age=max_age)
    except signing.SignatureExpired as exc:
        raise DomainError("This reading link has expired; reopen the title.") from exc
    except signing.BadSignature as exc:
        raise DomainError("Invalid content link.") from exc
    loan = (
        DigitalLoan.objects.select_related("license__edition__work", "patron")
        .filter(pk=payload.get("loan"), status=DigitalLoanStatus.ACTIVE)
        .first()
    )
    if loan is None or loan.expires_at <= timezone.now():
        raise DomainError("This digital loan is not active.")
    return loan, payload.get("loc", "")


# --------------------------------------------------------------------------- #
# Watermarking
# --------------------------------------------------------------------------- #
def watermark_label(loan: DigitalLoan) -> str:
    """A human-readable ownership stamp for social DRM (no sensitive PII)."""
    org = loan.organization.name
    card = loan.patron.library_card_number if loan.patron_id else "former patron"
    return f"{org} · card {card} · loan #{loan.pk}"


def apply_text_watermark(body: str, label: str) -> str:
    return f"{body}\n\n———\n{label}\nLicensed copy — not for redistribution."


# --------------------------------------------------------------------------- #
# Access logging & progress
# --------------------------------------------------------------------------- #
def record_access(loan: DigitalLoan, *, action: str, detail: str = "") -> None:
    DigitalAccessLog.objects.create(
        organization=loan.organization,
        loan=loan,
        patron_hash=loan.patron_hash or (stable_patron_hash(loan.patron) if loan.patron_id else ""),
        action=action,
        detail=detail[:255],
    )


def save_progress(loan: DigitalLoan, *, locator: str = "", percent: float = 0.0) -> ReadingProgress:
    progress, _ = ReadingProgress.objects.update_or_create(
        loan=loan,
        defaults={"locator": locator[:255], "percent": max(0.0, min(100.0, float(percent)))},
    )
    return progress


# --------------------------------------------------------------------------- #
# Asset selection & manifest
# --------------------------------------------------------------------------- #
def select_asset(edition, organization=None) -> DigitalAsset | None:
    """Select an asset for this tenant, with global legacy content as fallback."""
    assets = DigitalAsset.objects.filter(edition=edition)
    if organization is not None:
        scoped = list(assets.filter(organization=organization))
        assets = scoped or list(assets.filter(organization__isnull=True))
    else:
        assets = list(assets.filter(organization__isnull=True))
    if not assets:
        return None
    assets.sort(key=lambda a: 0 if a.fmt == "text" else 1)
    return assets[0]


def build_manifest(loan: DigitalLoan) -> dict:
    """A device-agnostic reading manifest for an active loan."""
    edition = loan.license.edition
    work = edition.work
    asset = select_asset(edition, loan.organization)
    label = watermark_label(loan)
    progress = getattr(loan, "reading_progress", None)
    manifest = {
        "loan_id": loan.pk,
        "title": (asset.title if asset and asset.title else work.canonical_title),
        "watermark": label,
        "expires_at": loan.expires_at,
        "progress": {
            "locator": progress.locator if progress else "",
            "percent": progress.percent if progress else 0.0,
        },
    }
    if asset is None:
        # External licenses are proxied through a short-lived content token —
        # never hand the durable vendor URL to the client.
        manifest["format"] = "external"
        if loan.license.content_url:
            manifest["content_token"] = mint_content_token(loan, locator="external")
        return manifest

    manifest["format"] = asset.fmt
    if asset.fmt == "text":
        manifest["chapters"] = [
            {
                "index": i,
                "title": ch.get("title", f"Chapter {i + 1}"),
                "content_token": mint_content_token(loan, locator=f"chapter:{i}"),
            }
            for i, ch in enumerate(asset.text_content)
        ]
    else:
        manifest["content_token"] = mint_content_token(loan, locator=asset.fmt)
        manifest["content_type"] = asset.content_type or "application/octet-stream"
        manifest["byte_size"] = asset.byte_size
        manifest["duration_seconds"] = asset.duration_seconds
    return manifest


def access_manifest(*, access_token: str) -> dict:
    """Resolve a loan by its durable access token and build a reading manifest."""
    loan = (
        DigitalLoan.objects.select_related("license__edition__work", "patron", "organization")
        .filter(access_token=access_token, status=DigitalLoanStatus.ACTIVE)
        .first()
    )
    if loan is None or loan.expires_at <= timezone.now():
        raise DomainError("This digital loan is not active.")
    return build_manifest(loan)


# --------------------------------------------------------------------------- #
# Content retrieval
# --------------------------------------------------------------------------- #
def read_text_chapter(loan: DigitalLoan, index: int) -> tuple[str, str]:
    """Return (watermarked chapter body, chapter title) for a text asset."""
    asset = select_asset(loan.license.edition, loan.organization)
    if asset is not None and asset.fmt != "text":
        asset = None
    if asset is None or not (0 <= index < len(asset.text_content)):
        raise DomainError("Chapter not found.")
    chapter = asset.text_content[index]
    record_access(loan, action="read", detail=f"chapter:{index}")
    body = apply_text_watermark(chapter.get("body", ""), watermark_label(loan))
    return body, chapter.get("title", f"Chapter {index + 1}")


def fetch_external(loan: DigitalLoan) -> tuple[bytes, str, dict]:
    """Proxy an externally hosted license URL through the reader (no durable leak)."""
    from .net import safe_urlopen

    url = (loan.license.content_url or "").strip()
    if not url:
        raise DomainError("Content is not available.")
    with safe_urlopen(url, method="GET", timeout=20) as response:
        data = response.read()
        content_type = response.headers.get_content_type() or "application/octet-stream"
    record_access(loan, action="stream", detail="external")
    headers = {
        "X-Content-Watermark": watermark_label(loan),
        "Cache-Control": "private, no-store",
    }
    return data, content_type, headers


def fetch_binary(loan: DigitalLoan, locator: str) -> tuple[bytes, str, dict]:
    """Return (bytes, content_type, drm_headers) for a binary asset.

    The token's locator names the exact format it was minted for, so a token for
    the audiobook never serves the PDF (or vice-versa) when an edition has both.
    """
    if locator == "external":
        return fetch_external(loan)
    assets = DigitalAsset.objects.filter(edition=loan.license.edition).exclude(fmt="text")
    scoped_assets = assets.filter(organization=loan.organization)
    asset = (
        scoped_assets.filter(fmt=locator).first()
        if scoped_assets.exists()
        else assets.filter(organization__isnull=True, fmt=locator).first()
    )
    if asset is None or not asset.media_key:
        raise DomainError("Content is not available.")
    data, content_type = read_blob(asset.media_key)
    record_access(loan, action="stream", detail=locator or asset.fmt)
    headers = {
        "X-Content-Watermark": watermark_label(loan),
        "Cache-Control": "private, no-store",
    }
    return data, asset.content_type or content_type, headers
