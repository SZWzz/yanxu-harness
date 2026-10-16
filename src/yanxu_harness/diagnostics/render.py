"""Render one report: safe JSON as the fact format, Markdown as a derived view of it.

Two rules, and both exist because the alternative leaks something:

* **JSON is the fact format.** It is emitted with a stable field order and no NaN/Infinity
  (which are not JSON), so a host can diff two reports and store one as evidence;
* **Markdown is derived from the same report, never from the stores.** If the two views
  needed separate statistics, one of them would eventually disagree with the other.

Everything a report publishes is either an id, a number, a status or a *phrase this build
wrote*. Free text can only enter through ``detail``/``reason``/``note`` fields, and those
came from a record or a host, so the Markdown view escapes them: a summary that rendered a
tool's detail text verbatim could be made to inject HTML into whatever consumes it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .types import RunDiagnosticReport, SourceKind, UsageQuantity

__all__ = [
    "escape_markdown",
    "REDACTED",
    "sanitise_text",
    "to_json",
    "to_markdown",
]

#: What replaces a piece of record-provided text that looks like it carries a secret or a
#: host path. A named marker rather than removal, so a reader can see that something was there.
REDACTED = "[redacted]"

#: How long a free-text value may be in a report. A report is not an export of the record; a
#: long body (an exception traceback, a file's contents, a provider's error page) is truncated
#: and says so.
_MAX_TEXT = 240

#: Shapes that carry credentials or host details *inside otherwise ordinary text*. A blacklist of
#: key *names* is not enough — the plan says so explicitly — because the secret is usually a value.
_SECRET_PATTERNS = (
    # Authorization headers and bearer tokens.
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), REDACTED),
    # Common key shapes: OpenAI-style, Slack, GitHub, AWS access keys.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}"), REDACTED),
    # A prefixed opaque value anywhere in the text (`key=sk-live-…`, `x-api-key=…`), whatever it
    # was assigned to: the shape is the giveaway, not the name.
    (re.compile(r"(?i)\b[a-z0-9_.\-]{0,12}[=:]\s*(sk-[A-Za-z0-9_\-]{8,})"), REDACTED),
    # A credential in a query string: ?token=…, &api_key=…, ?password=…
    (
        re.compile(
            r"(?i)([?&](?:key|token|api[_-]?key|access[_-]?token|secret|password|signature)=)"
            r"[^&\s\"']+"
        ),
        r"\1" + REDACTED,
    ),
    # A credential named and assigned anywhere in the text (`password=…`, `api_key: …`), not
    # only in a query string. Names are matched conservatively so ordinary prose keeps working.
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|client[_-]?secret|api[_-]?key|apikey|access[_-]?token"
            r"|auth[_-]?token|token|signature)\b\s*[:=]\s*[^\s,;&\"']+"
        ),
        r"\1=" + REDACTED,
    ),
    # user:password@host in a URL.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/@\s]+@"), r"\1" + REDACTED + "@"),
    # An absolute path (the host's layout: virtualenvs, home directories, temporary roots).
    (re.compile(r"(?<![\w<])/(?:[\w.\-]+/){2,}[\w.\-]*"), "<path>"),
    (re.compile(r"(?<![\w<])/(?:Users|home|var|tmp|private|Volumes|mnt)/[\w.\-/]*"), "<path>"),
)


def sanitise_text(value: Any) -> str:
    """Bound one free-text value and strip the shapes a report must never publish.

    Kept deliberately conservative in the other direction: ordinary prose passes through, so a
    refusal reason or a reconciliation detail is still readable. What it removes is the class of
    things the plan names — credentials in ordinary text values, and host paths — and what it
    bounds is length, because "unprocessed exception body" usually means a very long string.
    """
    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT].rstrip() + f"… ({len(text) - _MAX_TEXT} more characters)"
    return text

_ESCAPE = {
    "\\": "\\\\",
    "`": "\\`",
    "*": "\\*",
    "_": "\\_",
    "[": "\\[",
    "]": "\\]",
    "<": "&lt;",
    ">": "&gt;",
    "|": "\\|",
    "\n": " ",
    "\r": " ",
}


def escape_markdown(text: Any) -> str:
    """Escape one untrusted value for a Markdown table cell.

    Newlines become spaces (a cell cannot contain a line break), pipes stop splitting the
    row, and angle brackets are entity-escaped so a ``<script>`` in a record cannot become
    markup in a document somebody renders.
    """
    return "".join(_ESCAPE.get(character, character) for character in str(text))


def to_json(report: RunDiagnosticReport) -> str:
    """The report as JSON: sorted keys, UTF-8 text, no NaN."""
    return json.dumps(
        report.to_record(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def to_markdown(report: RunDiagnosticReport) -> str:
    """The same report as Markdown, for a human reading it in a terminal or a PR."""
    lines: list[str] = []
    lines.append(f"# Run `{escape_markdown(report.run_id)}`")
    lines.append("")
    lines.append(
        f"- identity: `{escape_markdown(report.namespace)}` / "
        f"`{escape_markdown(report.session_id)}`"
    )
    lines.append(
        f"- status: **{escape_markdown(report.status or 'unknown')}**"
        + (f" (stop `{escape_markdown(report.stop_code)}`)" if report.stop_code else "")
        + (f" error `{escape_markdown(report.error_code)}`" if report.error_code else "")
    )
    if report.observer_attempt:
        lines.append(f"- read by attempt: `{escape_markdown(report.observer_attempt)}`")
    if report.origin_attempts:
        readable = ", ".join(f"`{escape_markdown(item)}`" for item in report.origin_attempts)
        lines.append(f"- attempts that did the work: {readable}")
    if report.detail:
        lines.append(f"- detail: {escape_markdown(report.detail)}")
    if report.waiting:
        waiting = report.waiting
        lines.append(
            "- waiting for: "
            f"`{escape_markdown(waiting.get('kind'))}` on "
            f"`{escape_markdown(waiting.get('call_id'))}`"
        )
    boundary = report.boundary
    lines.append(
        f"- read boundary: cursor `{boundary.cursor}` → `{boundary.next_cursor}` "
        f"(high water `{boundary.high_water}`), run revision `{boundary.run_revision}`, "
        f"{boundary.scanned_pages} page(s)"
    )
    if boundary.truncated or boundary.scan_limit_hit or boundary.non_atomic:
        lines.append(
            "- boundary caveats: "
            + ", ".join(
                item
                for item in (
                    "truncated" if boundary.truncated else "",
                    "scan limit hit" if boundary.scan_limit_hit else "",
                    "non-atomic view" if boundary.non_atomic else "",
                )
                if item
            )
        )
    lines.append("")

    if report.calls:
        lines.append("## Tool calls")
        lines.append("")
        lines.append("| call | tool | outcome | progress | operation | receipt |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for call in report.calls:
            receipt = call.receipt
            lines.append(
                "| "
                + " | ".join(
                    escape_markdown(item)
                    for item in (
                        call.call_id,
                        call.tool_name,
                        call.outcome,
                        call.progress,
                        call.operation_id,
                        f"{receipt.source.value}: {receipt.detail}" if receipt else "none",
                    )
                )
                + " |"
            )
        lines.append("")

    if report.spans:
        lines.append("## Stages")
        lines.append("")
        lines.append("| stage | attempt | started | duration | note |")
        lines.append("| --- | --- | --- | --- | --- |")
        for span in report.spans:
            if span.duration_ms is not None:
                duration = f"{span.duration_ms:.1f} ms"
            elif span.absent:
                duration = "missing"
            elif span.incomplete:
                duration = "incomplete"
            else:
                duration = "not measured"
            lines.append(
                "| "
                + " | ".join(
                    escape_markdown(item)
                    for item in (
                        span.stage,
                        span.attempt or "unknown",
                        "yes" if span.started is not None else "missing",
                        duration,
                        span.note,
                    )
                )
                + " |"
            )
        lines.append("")

    if report.usage:
        lines.append("## Measurement")
        lines.append("")
        lines.append("| unit | kind | amount | source | reference | note |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in report.usage:
            amount = "unknown" if item.quantity is UsageQuantity.UNKNOWN else f"{item.amount:g}"
            if item.quantity is UsageQuantity.ESTIMATED:
                amount += " (estimated)"
            lines.append(
                "| "
                + " | ".join(
                    escape_markdown(value)
                    for value in (
                        item.unit,
                        item.quantity.value,
                        amount,
                        item.source.value,
                        item.reference,
                        item.note,
                    )
                )
                + " |"
            )
        lines.append("")
        for unit in sorted({item.unit for item in report.usage}):
            coverage = report.usage_coverage(unit=unit)
            if coverage["complete"]:
                continue
            lines.append(
                f"- `{escape_markdown(unit)}`: known subtotal "
                f"{coverage['known_subtotal']:g}, {coverage['unknown']} unknown, "
                f"{coverage['estimated']} estimated — **not a complete total**"
            )
        lines.append("")

    if report.trace:
        lines.append("## Timeline")
        lines.append("")
        lines.append("| reference | kind | attempt | durable | at |")
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in report.trace:
            lines.append(
                "| "
                + " | ".join(
                    escape_markdown(value)
                    for value in (
                        entry.reference,
                        entry.kind,
                        entry.attempt or entry.attempt_source,
                        "yes" if entry.durable else "no",
                        "" if entry.at is None else f"{entry.at:.3f}",
                    )
                )
                + " |"
            )
        lines.append("")

    if report.integrity:
        lines.append("## What this report cannot prove")
        lines.append("")
        for note in report.integrity:
            lines.append(f"- {escape_markdown(note)}")
        lines.append("")
    if report.unsupported:
        lines.append("## Unsupported here")
        lines.append("")
        for note in report.unsupported:
            lines.append(f"- {escape_markdown(note)}")
        lines.append("")

    counts = report.source_counts()
    lines.append("## Sources")
    lines.append("")
    lines.append(
        ", ".join(
            f"{escape_markdown(kind)}={counts.get(kind, 0)}"
            for kind in sorted({item.value for item in SourceKind})
        )
    )
    lines.append("")
    return "\n".join(lines)
