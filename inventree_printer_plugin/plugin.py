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
from pypdf import PdfReader, PdfWriter, Transformation


# CUPS PPD option keys used by the DevTerm queue.
ATTRIBUTE_TAG_MAP.setdefault("TrimMode", IppTag.KEYWORD)
ATTRIBUTE_TAG_MAP.setdefault("BlankSpace", IppTag.BOOLEAN)
ATTRIBUTE_TAG_MAP.setdefault("FeedWhere", IppTag.KEYWORD)
ATTRIBUTE_TAG_MAP.setdefault("FeedDist", IppTag.KEYWORD)

DEFAULT_JOB_OPTIONS_TEXT = (
    "print-scaling=none\n"
    "TrimMode=Strong\n"
    "BlankSpace=False\n"
    "orientation-requested=none"
)

# Only a small subset of attributes use "none" as "omit this option".
NONE_MEANS_OMIT_KEYS = {"orientation-requested"}
MIN_MEDIA_WIDTH_MM = 72.0 / 25.4
MIN_MEDIA_HEIGHT_MM = 56.0 * 25.4 / 72.0


class InvenTreeDevtermCupsPlugin(LabelPrintingMixin, SettingsMixin, InvenTreePlugin):
    """InvenTree label plugin: send generated PDF label to CUPS queue."""

    NAME = "DevTerm CUPS Label Printer"
    SLUG = "devterm_cups_label_printer"
    TITLE = "DevTerm CUPS Label Printer"
    DESCRIPTION = "Send generated InvenTree PDF labels to CUPS queue on portterm via IPP"
    AUTHOR = "Gray Stone"
    VERSION = "0.2.8"
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
            "name": "Default Label Size",
            "description": "Blank=use queue default, auto=match PDF size, or explicit target size like 30x20 or Custom.30x20mm. The PDF is scaled to fit while preserving aspect ratio.",
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
            "default": DEFAULT_JOB_OPTIONS_TEXT,
        },
    }

    class PrintingOptionsSerializer(serializers.Serializer):
        label_size = serializers.CharField(required=False, allow_blank=True)
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

            # Some attributes use "none" as "omit this override", but options
            # like print-scaling=none must still be sent to CUPS.
            if value.lower() == "none" and key in NONE_MEANS_OMIT_KEYS:
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

    def _effective_job_options(self, override_text: Any) -> str:
        if isinstance(override_text, str) and override_text.strip():
            return override_text

        configured = self.get_setting("JOB_OPTIONS")
        if isinstance(configured, str) and configured.strip():
            return configured

        return DEFAULT_JOB_OPTIONS_TEXT

    def _format_mm(self, value: float) -> str:
        s = f"{value:.2f}"
        s = s.rstrip("0").rstrip(".")
        return s or "0"

    def _mm_to_points(self, value_mm: float) -> float:
        return value_mm * 72.0 / 25.4

    def _pdf_size_mm(self, payload: bytes) -> tuple[float, float] | None:
        try:
            reader = PdfReader(io.BytesIO(payload))
            if reader.pages:
                box = reader.pages[0].mediabox
                width_pt = abs(float(box.right) - float(box.left))
                height_pt = abs(float(box.top) - float(box.bottom))
                width_mm = width_pt * 25.4 / 72.0
                height_mm = height_pt * 25.4 / 72.0
                return (width_mm, height_mm)
        except Exception:
            # Fallback to raw MediaBox regex for malformed/minimal PDFs.
            pass

        m = re.search(
            rb"/MediaBox\s*\[\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\]",
            payload,
        )
        if not m:
            return None
        x1, y1, x2, y2 = (float(v) for v in m.groups())
        width_pt = abs(x2 - x1)
        height_pt = abs(y2 - y1)
        width_mm = width_pt * 25.4 / 72.0
        height_mm = height_pt * 25.4 / 72.0
        return (width_mm, height_mm)

    def _pdf_media_auto(self, payload: bytes) -> str:
        size_mm = self._pdf_size_mm(payload)
        if not size_mm:
            self.logger.warning(
                "devterm_cups_label_printer: media=auto failed to parse PDF page size; media will not be set"
            )
            return ""
        width_mm, height_mm = size_mm
        return f"Custom.{self._format_mm(width_mm)}x{self._format_mm(height_mm)}mm"

    def _parse_label_size_mm(self, value: str) -> tuple[float, float] | None:
        normalized = re.sub(r"\s+", "", value)
        m = re.fullmatch(
            r"(?:Custom\.)?(\d+(?:\.\d+)?)[xX\*×](\d+(?:\.\d+)?)(?:mm)?",
            normalized,
            flags=re.IGNORECASE,
        )
        if not m:
            return None
        return (float(m.group(1)), float(m.group(2)))

    def _media_from_size_mm(self, width_mm: float, height_mm: float) -> str:
        return f"Custom.{self._format_mm(width_mm)}x{self._format_mm(height_mm)}mm"

    def _fit_payload_to_page_size(
        self,
        payload: bytes,
        *,
        fit_width_mm: float,
        fit_height_mm: float,
        media_width_mm: float,
        media_height_mm: float,
    ) -> bytes:
        reader = PdfReader(io.BytesIO(payload))
        if not reader.pages:
            raise ValueError("Label PDF contains no pages")

        writer = PdfWriter()
        fit_width_pt = self._mm_to_points(fit_width_mm)
        fit_height_pt = self._mm_to_points(fit_height_mm)
        media_width_pt = self._mm_to_points(media_width_mm)
        media_height_pt = self._mm_to_points(media_height_mm)

        for page in reader.pages:
            box = page.mediabox
            left = float(box.left)
            bottom = float(box.bottom)
            width_pt = abs(float(box.right) - left)
            height_pt = abs(float(box.top) - bottom)
            if width_pt <= 0 or height_pt <= 0:
                raise ValueError("Label PDF has invalid page dimensions")

            scale = min(fit_width_pt / width_pt, fit_height_pt / height_pt)
            scaled_width_pt = width_pt * scale
            scaled_height_pt = height_pt * scale
            box_offset_x = (media_width_pt - fit_width_pt) / 2.0
            box_offset_y = (media_height_pt - fit_height_pt) / 2.0
            offset_x = box_offset_x + (fit_width_pt - scaled_width_pt) / 2.0 - (left * scale)
            offset_y = box_offset_y + (fit_height_pt - scaled_height_pt) / 2.0 - (bottom * scale)

            target_page = writer.add_blank_page(
                width=media_width_pt, height=media_height_pt
            )
            transform = Transformation().scale(scale).translate(offset_x, offset_y)
            target_page.merge_transformed_page(page, transform, expand=False)

        if reader.metadata:
            writer.add_metadata(
                {k: str(v) for k, v in reader.metadata.items() if v is not None}
            )

        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    def _safe_media_size_mm(self, width_mm: float, height_mm: float) -> tuple[float, float]:
        media_width_mm = max(width_mm, MIN_MEDIA_WIDTH_MM)
        media_height_mm = max(height_mm, MIN_MEDIA_HEIGHT_MM)
        return (media_width_mm, media_height_mm)

    def _resolve_label_output(
        self, label_size_value: str, payload: bytes
    ) -> tuple[bytes, str]:
        label_size = (label_size_value or "").strip()
        if not label_size:
            return (payload, "")

        if label_size.lower() == "auto":
            return (payload, self._pdf_media_auto(payload))

        target_size = self._parse_label_size_mm(label_size)
        if target_size:
            media_size = self._safe_media_size_mm(*target_size)
            if media_size != target_size:
                self.logger.warning(
                    "devterm_cups_label_printer: requested label_size=%sx%smm is below CUPS custom page minimum; using media=%sx%smm and fitting within the requested size",
                    self._format_mm(target_size[0]),
                    self._format_mm(target_size[1]),
                    self._format_mm(media_size[0]),
                    self._format_mm(media_size[1]),
                )
            pdf_size = self._pdf_size_mm(payload)
            if pdf_size:
                width_delta = abs(target_size[0] - pdf_size[0])
                height_delta = abs(target_size[1] - pdf_size[1])
                if width_delta <= 0.2 and height_delta <= 0.2:
                    return (payload, self._media_from_size_mm(*media_size))
                self.logger.info(
                    "devterm_cups_label_printer: fitting PDF size=%sx%smm into label_size=%sx%smm",
                    self._format_mm(pdf_size[0]),
                    self._format_mm(pdf_size[1]),
                    self._format_mm(target_size[0]),
                    self._format_mm(target_size[1]),
                )
            else:
                self.logger.info(
                    "devterm_cups_label_printer: fitting PDF into label_size=%sx%smm",
                    self._format_mm(target_size[0]),
                    self._format_mm(target_size[1]),
                )
            fitted_payload = self._fit_payload_to_page_size(
                payload,
                fit_width_mm=target_size[0],
                fit_height_mm=target_size[1],
                media_width_mm=media_size[0],
                media_height_mm=media_size[1],
            )
            return (fitted_payload, self._media_from_size_mm(*media_size))

        self.logger.warning(
            "devterm_cups_label_printer: treating label_size=%s as raw CUPS media override; aspect-ratio-preserving fit is only supported for values like 30x20 or Custom.30x20mm",
            label_size,
        )
        return (payload, label_size)

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
        label_size: str,
        copies: int,
        title: str,
        feed_after_mm: float,
        job_options_text: str,
    ) -> None:
        uri = self._printer_uri(host, port, queue)
        payload_to_print, media_resolved = self._resolve_label_output(label_size, payload)
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
            "data": payload_to_print,
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

        label_size = (
            options.get("label_size")
            or options.get("media")
            or self.get_setting("DEFAULT_MEDIA")
        )
        copies = int(options.get("copies", 1))
        title = options.get("title") or "inventree-label"
        feed_after_mm = float(
            options.get("feed_after_mm", self.get_setting("DEFAULT_FEED_AFTER_MM") or 0)
        )
        job_options_text = self._effective_job_options(options.get("job_options"))

        cups_host = self.get_setting("CUPS_HOST") or "portterm"
        cups_queue = self.get_setting("CUPS_QUEUE") or "devterm_printer"
        cups_port = int(self.get_setting("CUPS_PORT") or "631")

        self.logger.info(
            "devterm_cups_label_printer: ipp://%s:%s/printers/%s label_size=%s copies=%s title=%s feed_after_mm=%s job_options=%s",
            cups_host,
            cups_port,
            cups_queue,
            label_size,
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
                    label_size=label_size,
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
