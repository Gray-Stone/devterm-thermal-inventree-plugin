# InvenTree DevTerm CUPS Plugin

Direct InvenTree plugin for label printing to CUPS filtered queue.

No bridge scripts, no extra HTTP service.

## What It Does

- Implements `LabelPrintingMixin` plugin for InvenTree
- Sends generated label payload directly to:
  - host: `portterm` (default)
  - queue: `devterm_printer` (default filtered queue)
- Uses direct IPP submission in Python (`pyipp`), no `lp/lpr` binary required.
- Applies job attributes:
  - from configurable `JOB_OPTIONS` (key=value list)
  - `copies`
  - optional `label_size` (`blank` / `auto` / `30x20` / `30*20` / `Custom.30x20mm`)
- Also sends queue controls:
  - `FeedWhere` / `FeedDist` from feed setting

## Plugin Package

- Python module: `inventree_printer_plugin`
- Entry point:
  - `InvenTreeDevtermCupsPlugin = inventree_printer_plugin:InvenTreeDevtermCupsPlugin`

## Install (inside InvenTree environment)

```bash
cd /path/to/tprint/inventree-printer
pip install -e .
```

Restart InvenTree server + worker after install.

### Install Directly From GitHub Tag

```bash
pip install -U "https://github.com/Gray-Stone/devterm-thermal-inventree-plugin/archive/refs/tags/v0.2.9.tar.gz"
```

For a specific python environment, run it with that environment's `pip`.

## InvenTree Setup

1. Open InvenTree admin plugin settings.
2. Enable `DevTerm CUPS Label Printer`.
3. Configure (optional):
- `CUPS_HOST` (default `portterm`)
- `CUPS_QUEUE` (default `devterm_printer`)
- `CUPS_PORT` (default `631`)
- `Default Label Size` (`DEFAULT_MEDIA`)
  - blank: do not send `media` (queue default is used)
  - `auto`: keep the original PDF size and send matching `media=Custom.WxHmm`
  - explicit target size: `30x20`, `30*20`, `30x20mm`, or `Custom.30x20mm`
  - when an explicit size differs from the PDF, the plugin rescales and centers the label into the target page while preserving aspect ratio
  - if the requested height is smaller than the queue's minimum custom page height, the plugin uses the smallest safe media height and fits within the requested size
- `DEFAULT_FEED_AFTER_MM` (default `0`, range `0..45`)
- `JOB_OPTIONS` (one `key=value` per line, or comma-separated)
  - default:
    - `print-scaling=none`
    - `TrimMode=Strong`
    - `BlankSpace=False`
    - `orientation-requested=none`

Then print labels from normal InvenTree label actions.

## Notes

- This plugin uses IPP directly (no system print command dependency).
- This plugin uses the filtered queue on `portterm`; no raw ESC/POS encoding is needed in plugin code.
- `label_size` is the user-facing print option. The plugin still accepts legacy `media` input as an alias.
- Feed behavior:
  - `feed_after_mm=0` => no extra post-job feed
  - `feed_after_mm>0` => mapped to nearest PPD `FeedDist` 3mm step and `FeedWhere=AfterJob`
- Copies are submitted as separate one-copy IPP jobs so feed is applied between physical labels.
- `orientation-requested=none` means plugin will not send orientation override.
