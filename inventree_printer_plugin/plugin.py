from __future__ import annotations

import asyncio
import io
import os
import re
from typing import Any

from plugin import InvenTreePlugin
from plugin.mixins import LabelPrintingMixin, SettingsMixin
from rest_framework import serializers
import logging
from pyipp import IPP
from pyipp.ipp import IppOperation
from pyipp.serializer import ATTRIBUTE_TAG_MAP
from pyipp.tags import IppTag
from pypdf import PdfReader


# CUPS PPD option keys used by the DevTerm queue.
ATTRIBUTE_TAG_MAP.setdefault("TrimMode", IppTag.KEYWORD)
ATTRIBUTE_TAG_MAP.setdefault("BlankSpace", IppTag.BOOLEAN)
ATTRIBUTE_TAG_MAP.setdefault("FeedWhere", IppTag.KEYWORD)
ATTRIBUTE_TAG_MAP.setdefault("FeedDist", IppTag.KEYWORD)


class InvenTreeDevtermCupsPlugin(LabelPrintingMixin, SettingsMixin, InvenTreePlugin):
    """InvenTree label plugin: send generated PDF label to CUPS queue."""

    NAME = "DevTerm CUPS Label Printer"
    SLUG = "devterm_cups_label_printer"
    TITLE = "DevTerm CUPS Label Printer"
    DESCRIPTION = "Send generated InvenTree PDF labels to CUPS queue on portterm via IPP"
    AUTHOR = "Gray Stone"
    VERSION = "0.2.5"
    logger = logging.getLogger("inventree.devterm_cups_label_printer")

    BLOCKING_PRINT = True

    SETTINGS = {
        "CUPS_HOST": {
            "name": "CUPS Host",
            "description": "CUPS server hostname",
            "default": "portterm",
        },
        "CUPS_QUEUE": {
            "name": "CUPS Queue",
            "description": "Filtered CUPS queue name",
            "default": "devterm_printer",
        },
        "CUPS_PORT": {
            "name": "CUPS Port",
            "description": "CUPS server port",
            "default": "631",
        },
        "DEFAULT_MEDIA": {
            "name": "Default Media",
            "description": "Media override: blank=queue default, auto=derive from PDF, or explicit (e.g. Custom.48x30mm)",
            "default": "auto",
        },
        "DEFAULT_FEED_AFTER_MM": {
            "name": "Default Feed After (mm)",
            "description": "Extra feed after print, 0 disables (valid range 0-45)",
            "default": "0",
        },
        "JOB_OPTIONS": {
            "name": "IPP Job Options",
            "description": "Lines of key=value options. Use orientation-requested=none to preserve PDF orientation.",
            "default": "print-scaling=none\nTrimMode=Strong\nBlankSpace=False\norientation-requested=none",
        },
    }

    class PrintingOptionsSerializer(serializers.Serializer):
        media = serializers.CharField(required=False, allow_blank=True)
        copies = serializers.IntegerField(required=False, min_value=1, default=1)
        title = serializers.CharField(required=False, allow_blank=True)
        feed_after_mm = serializers.FloatField(required=False, min_value=0.0)
        job_options = serializers.CharField(required=False, allow_blank=True)

    def _as_bytes(self, label: Any) -> bytes:
        if label is None:
            raise ValueError("No label payload provided")

        if isinstance(label, bytes):
            return label

        if isinstance(label, bytearray):
            return bytes(label)

        if isinstance(label, str):
            if os.path.exists(label):
                with open(label, "rb") as f:
                    return f.read()
            return label.encode("utf-8")

        if hasattr(label, "read"):
            payload = label.read()
            if isinstance(payload, str):
                return payload.encode("utf-8")
            return payload

        raise TypeError(f"Unsupported label payload type: {type(label)}")

    def _printer_uri(self, host: str, port: int, queue: str) -> str:
        return f"ipp://{host}:{port}/printers/{queue}"

    def _parse_job_options(self, text: str) -> dict[str, object]:
        result: dict[str, object] = {}
        if not text:
            return result

        raw_lines = []
        for line in text.splitlines():
            raw_lines.extend([part.strip() for part in line.split(",")])

        for token in raw_lines:
            if not token or "=" not in token:
                continue
            key, value = token.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue

            # "none" means do not send this attribute at all.
            if value.lower() == "none":
                continue

            v_lower = value.lower()
            if v_lower == "true":
                parsed: object = True
            elif v_lower == "false":
                parsed = False
            elif re.fullmatch(r"-?\d+", value):
                parsed = int(value)
            else:
                parsed = value

            # For custom keys, treat as keyword unless pyipp already knows the key.
            ATTRIBUTE_TAG_MAP.setdefault(key, IppTag.KEYWORD)
            result[key] = parsed

        return result

    def _format_mm(self, value: float) -> str:
        s = f"{value:.2f}"
        s = s.rstrip("0").rstrip(".")
        return s or "0"

    def _pdf_media_auto(self, payload: bytes) -> str:
        try:
            reader = PdfReader(io.BytesIO(payload))
            if reader.pages:
                box = reader.pages[0].mediabox
                width_pt = abs(float(box.right) - float(box.left))
                height_pt = abs(float(box.top) - float(box.bottom))
                width_mm = width_pt * 25.4 / 72.0
                height_mm = height_pt * 25.4 / 72.0
                return f"Custom.{self._format_mm(width_mm)}x{self._format_mm(height_mm)}mm"
        except Exception:
            # Fallback to raw MediaBox regex for malformed/minimal PDFs.
            pass

        m = re.search(
            rb"/MediaBox\s*\[\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\]",
            payload,
        )
        if not m:
            self.logger.warning(
                "devterm_cups_label_printer: media=auto failed to parse PDF page size; media will not be set"
            )
            return ""
        x1, y1, x2, y2 = (float(v) for v in m.groups())
        width_pt = abs(x2 - x1)
        height_pt = abs(y2 - y1)
        width_mm = width_pt * 25.4 / 72.0
        height_mm = height_pt * 25.4 / 72.0
        return f"Custom.{self._format_mm(width_mm)}x{self._format_mm(height_mm)}mm"

    def _resolve_media(self, media_value: str, payload: bytes) -> str:
        media = (media_value or "").strip()
        if not media:
            return ""
        if media.lower() == "auto":
            return self._pdf_media_auto(payload)
        return media

    def _feed_options(self, feed_after_mm: float) -> dict[str, object]:
        if feed_after_mm <= 0:
            return {"FeedWhere": "None"}

        # PPD supports discrete 3mm steps from 3..45mm.
        steps = max(1, min(15, int(round(feed_after_mm / 3.0))))
        dist_mm = steps * 3
        dist_key = f"{steps - 1}feed{dist_mm}mm"
        return {
            "FeedWhere": "AfterJob",
            "FeedDist": dist_key,
        }

    async def _print_via_ipp(
        self,
        *,
        host: str,
        port: int,
        queue: str,
        payload: bytes,
        media: str,
        copies: int,
        title: str,
        feed_after_mm: float,
        job_options_text: str,
    ) -> None:
        uri = self._printer_uri(host, port, queue)
        media_resolved = self._resolve_media(media, payload)
        job_attrs = {
            "job-name": title,
            "copies": copies,
        }
        job_attrs.update(self._parse_job_options(job_options_text))
        job_attrs.update(self._feed_options(feed_after_mm))
        if media_resolved:
            job_attrs["media"] = media_resolved

        message = {
            "operation-attributes-tag": {
                "document-format": "application/pdf",
            },
            "job-attributes-tag": job_attrs,
            "data": payload,
        }

        async with IPP(uri) as ipp:
            await ipp.execute(IppOperation.PRINT_JOB, message)

    def print_label(self, **kwargs):
        label = (
            kwargs.get("pdf_data")
            or kwargs.get("label")
            or kwargs.get("data")
            or kwargs.get("payload")
        )
        options = kwargs.get("printing_options", {}) or {}

        media = options.get("media") or self.get_setting("DEFAULT_MEDIA")
        copies = int(options.get("copies", 1))
        title = options.get("title") or "inventree-label"
        feed_after_mm = float(
            options.get("feed_after_mm", self.get_setting("DEFAULT_FEED_AFTER_MM") or 0)
        )
        job_options_text = options.get("job_options") or self.get_setting("JOB_OPTIONS") or ""

        cups_host = self.get_setting("CUPS_HOST") or "portterm"
        cups_queue = self.get_setting("CUPS_QUEUE") or "devterm_printer"
        cups_port = int(self.get_setting("CUPS_PORT") or "631")

        self.logger.info(
            "devterm_cups_label_printer: ipp://%s:%s/printers/%s media=%s copies=%s title=%s feed_after_mm=%s job_options=%s",
            cups_host,
            cups_port,
            cups_queue,
            media,
            copies,
            title,
            feed_after_mm,
            job_options_text,
        )

        payload = self._as_bytes(label)
        try:
            asyncio.run(
                self._print_via_ipp(
                    host=cups_host,
                    port=cups_port,
                    queue=cups_queue,
                    payload=payload,
                    media=media,
                    copies=copies,
                    title=title,
                    feed_after_mm=feed_after_mm,
                    job_options_text=job_options_text,
                )
            )
        except Exception as exc:
            self.logger.exception("devterm_cups_label_printer: ipp print failed")
            raise RuntimeError(f"IPP print failed: {exc}") from exc

        self.logger.info("devterm_cups_label_printer: ipp submitted successfully")
