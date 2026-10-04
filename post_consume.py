#!/usr/bin/env -S uv run --quiet --script --no-env-file
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "pypaperless>=6,<7",
#     "pydantic-settings>=2.15",
# ]
# ///
# The dependencies are resolved by uv from the block above, so an editor that is
# not pointed at that environment cannot see them.
# pyright: reportMissingImports=false
"""paperless-ngx post-consume hook that queues documents for paperless-gpt.

Wired up through ``PAPERLESS_POST_CONSUME_SCRIPT``. For every consumed document it

1. always adds the paperless-gpt metadata trigger tag, and
2. adds the paperless-gpt OCR trigger tag only when OCR cannot damage the
   document text - see :func:`ocr_gate` for the rule and README.md for the why.

Configuration lives in the ``.env`` file next to this script and is read by the
script itself through pydantic-settings, so it is typed, validated and
independent of the working directory. The process environment always wins over
the file; see README.md for the details and for the settings shared with
paperless-gpt.

Output contract: paperless-ngx captures stdout and stderr and relays them
through its own logger (stdout at INFO, stderr at WARNING) once the process has
exited. This script therefore prints plain lines without log-level labels - the
level and stream are added by the relay - and lets the exit code carry the
outcome.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"

PDF_MIME = "application/pdf"
IMAGE_MIME_PREFIX = "image/"

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_AUTO_TAG = "paperless-gpt-auto"
DEFAULT_OCR_AUTO_TAG = "paperless-gpt-ocr-auto"

# Colour for tags this script creates: olive green, so the trigger tags stand
# apart from paperless-ngx's own default tag colour (#a6cee3).
TAG_COLOR = "#6B8E23"


class Fatal(RuntimeError):
    """A failure that must fail the consume task instead of being swallowed."""


class Config(BaseSettings):
    """Configuration read from the process environment and the .env file.

    The process environment takes priority over the file, and the file over the
    defaults below, so anything exported by paperless-ngx or the systemd unit
    wins. Unknown variables are ignored: the hook inherits a large environment.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    api_url: str = Field(DEFAULT_API_URL, validation_alias="PAPERLESS_API_URL", min_length=1)
    api_token: str = Field(validation_alias="PAPERLESS_API_TOKEN", min_length=1)
    timeout: float = Field(DEFAULT_TIMEOUT_SECONDS, validation_alias="API_TIMEOUT", ge=1)
    auto_tag: str = Field(DEFAULT_AUTO_TAG, validation_alias="AUTO_TAG", min_length=1)
    ocr_tag: str = Field(DEFAULT_OCR_AUTO_TAG, validation_alias="OCR_AUTO_TAG", min_length=1)
    ocr_limit_pages: int = Field(validation_alias="OCR_LIMIT_PAGES", ge=0)
    document_id: int = Field(validation_alias="DOCUMENT_ID", ge=1)

    @field_validator("api_url")
    @classmethod
    def _require_scheme(cls, value: str) -> str:
        """Reject a bare host: without a scheme the client would assume https."""
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"must include the scheme, e.g. {DEFAULT_API_URL}")
        return value


def ocr_gate(
    mime_type: str | None,
    page_count: int | None,
    limit_pages: int,
) -> tuple[bool, str]:
    """Decide whether this document may be handed to paperless-gpt's OCR.

    paperless-gpt *replaces* the document text with its OCR result. Its
    ``OCR_LIMIT_PAGES`` setting only guards the searchable-PDF step, so a
    document with more pages than the limit would silently lose everything past
    the limit. Therefore only queue documents that fit inside the budget.

    ``limit_pages`` of 0 means paperless-gpt has no limit, so nothing can be
    truncated and the page count stops mattering.

    Returns a ``(queue_for_ocr, reason)`` tuple; the reason is logged so that
    every skipped document explains itself.
    """
    if not mime_type:
        return False, "mime type unknown"

    is_image = mime_type.startswith(IMAGE_MIME_PREFIX)
    if mime_type != PDF_MIME and not is_image:
        return False, f"mime type {mime_type} is not OCR-able"

    if limit_pages == 0:
        return True, "paperless-gpt has no OCR page limit"

    # paperless-ngx reports no page count for formats without the concept of
    # pages, so treat a single image as one page.
    pages = page_count if page_count and page_count > 0 else (1 if is_image else None)

    if pages is None:
        return False, f"page count unknown, cannot compare to OCR_LIMIT_PAGES={limit_pages}"
    if pages > limit_pages:
        return False, f"{pages} pages exceed OCR_LIMIT_PAGES={limit_pages}"

    return True, f"{pages} pages fit OCR_LIMIT_PAGES={limit_pages}"


def load_config() -> Config:
    """Build the configuration, failing loudly on anything missing or malformed."""
    if not ENV_FILE.is_file():
        raise Fatal(
            f"environment file {ENV_FILE} not found; copy .env.example to .env next to the script",
        )

    try:
        # pydantic-settings reads the process environment first and the .env file
        # second; a file it cannot find is ignored, hence the check above.
        return Config()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise Fatal(f"invalid configuration in {ENV_FILE}: {problems}") from exc


async def resolve_tag(paperless: Any, name: str, *, create: bool) -> int | None:
    """Return the id of the tag called ``name``, creating it when missing.

    Returns None when the tag does not exist and ``create`` is False, which only
    happens during a dry run.
    """
    async with paperless.tags.filter(name__iexact=name) as results:
        async for tag in results:
            return int(tag.id)

    if not create:
        return None

    from pypaperless.models.types import MatchingAlgorithm

    draft = paperless.tags.create(
        name=name,
        color=TAG_COLOR,
        is_inbox_tag=False,
        match="",
        # No automatic matching: these tags are triggers, nothing else.
        matching_algorithm=MatchingAlgorithm.NONE,
        is_insensitive=True,
    )
    return int(await paperless.tags.save(draft))


async def run(*, dry_run: bool) -> None:
    """Tag the document that paperless-ngx just consumed."""
    config = load_config()

    from pypaperless import PaperlessClient

    async with PaperlessClient(
        config.api_url,
        config.api_token,
        timeout=config.timeout,
    ) as paperless:
        document = await paperless.documents(config.document_id)
        current_tag_ids = [int(tag_id) for tag_id in document.tags or []]

        queue_ocr, reason = ocr_gate(
            document.mime_type,
            document.page_count,
            config.ocr_limit_pages,
        )
        wanted = [config.auto_tag, config.ocr_tag] if queue_ocr else [config.auto_tag]

        add_names: list[str] = []
        add_ids: list[int] = []
        for name in wanted:
            tag_id = await resolve_tag(paperless, name, create=not dry_run)
            if tag_id is not None and tag_id in current_tag_ids:
                continue  # already on the document, nothing to do
            add_names.append(name)
            if tag_id is not None:
                add_ids.append(tag_id)

        if add_ids and not dry_run:
            await paperless.documents.bulk_edit.modify_tags(
                [config.document_id],
                add_tags=add_ids,
                remove_tags=[],
            )

        tag_field = "would_add" if dry_run else "tags_added"
        summary = (
            f"document={config.document_id}"
            f" endpoint={config.api_url}"
            f" mime={document.mime_type}"
            f" pages={document.page_count}"
            f" ocr_limit={config.ocr_limit_pages}"
            f" ocr_queued={'yes' if queue_ocr else 'no'}"
            f" reason={reason!r}"
            f" {tag_field}={add_names or 'none'}"
            f" paperless={paperless.host_version} api={paperless.host_api_version}"
            f" dry_run={'yes' if dry_run else 'no'}"
        )

        print(f"post_consume: {summary}")


def main() -> int:
    """Entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="post_consume.py",
        description="Queue a freshly consumed document for paperless-gpt.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without creating tags or writing to the document",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(dry_run=args.dry_run))
    except Fatal as exc:
        print(f"post_consume: failed: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - any failure must fail the consume task
        print("post_consume: failed: unexpected error, traceback follows", file=sys.stderr)
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
