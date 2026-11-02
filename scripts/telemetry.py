"""Terminal presentation helpers: ANSI console, progress bar, formatting.

Everything degrades gracefully: colours are disabled automatically when the
stream is not a TTY (or ``NO_COLOR`` is set), box drawing falls back to ASCII
when the stream encoding cannot represent it, and progress output is only
emitted for interactive terminals.
"""

from __future__ import annotations

import math
import os
import sys
import time
from typing import TextIO

RESET = "\033[0m"
BOLD = "1"
DIM = "2"
RED = "31"
GREEN = "32"
YELLOW = "33"
BLUE = "34"
MAGENTA = "35"
CYAN = "36"
WHITE = "37"

BOX_UNICODE = ("╭", "╮", "╰", "╯", "─", "│", "✓", "✗", "›", "█", "░")
BOX_ASCII = ("+", "+", "+", "+", "-", "|", "+", "x", ">", "#", "-")


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:  # pragma: no cover - exotic stream objects
        return False


def configure_stdio() -> None:
    """Force UTF-8 on stdout/stderr so box drawing works on legacy consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - detached streams
            pass


def color_enabled(stream: TextIO) -> bool:
    """Decide whether ANSI colour should be used for ``stream``."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    return _isatty(stream)


def _supports_glyphs(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True
    try:
        "".join(BOX_UNICODE).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def format_integer(value: int, *, compact_from: int = 10**15) -> str:
    """Group digits with commas, switching to scientific notation when huge."""
    value = int(value)
    if -compact_from < value < compact_from:
        return f"{value:,}"
    sign = "-" if value < 0 else ""
    digits = str(abs(value))
    exponent = len(digits) - 1
    mantissa = digits[0] + "." + (digits[1:4].rstrip("0") or "0")
    if mantissa.endswith(".0"):
        mantissa = mantissa[:-2]
    return f"{sign}{mantissa}e+{exponent}"


def format_rate(value: float) -> str:
    """Compact rate: ``999``, ``1.24K``, ``6.66M``, ``1.2B``."""
    if value is None or not math.isfinite(value) or value <= 0:
        return "0"
    suffixes = ("", "K", "M", "B", "T", "P", "E")
    for suffix in suffixes:
        if value < 1000 or suffix == suffixes[-1]:
            text = f"{value:.2f}" if value < 10 else f"{value:.1f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return f"{text}{suffix}"
        value /= 1000
    return f"{value:.0f}"  # pragma: no cover - unreachable


def format_duration(seconds: float | None) -> str:
    """Human duration: ``250 ms``, ``12.4 s``, ``3m 04s``, ``2h 07m``."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class Console:
    """Small ANSI-aware writer with consistent layout helpers."""

    def __init__(
        self,
        stream: TextIO,
        *,
        color: bool | None = None,
        glyphs: bool | None = None,
    ) -> None:
        self.stream = stream
        self.color = color_enabled(stream) if color is None else color
        unicode_ok = _supports_glyphs(stream) if glyphs is None else glyphs
        self.glyphs = BOX_UNICODE if unicode_ok else BOX_ASCII
        self.unicode = unicode_ok

    # -- primitives ------------------------------------------------------- #

    @property
    def isatty(self) -> bool:
        return _isatty(self.stream)

    def paint(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(f"\033[{code}m" for code in codes) + text + RESET

    def write(self, text: str = "") -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def clear_screen(self) -> None:
        if self.isatty:
            self.stream.write("\033c")
            self.stream.flush()

    def blank(self) -> None:
        self.write("")

    # -- components ------------------------------------------------------- #

    def banner(self, text: str) -> None:
        self.write(self.paint(text.rstrip("\n"), CYAN))

    def header(self, title: str, subtitle: str = "", *, width: int = 72) -> None:
        tl, tr, bl, br, h, v = self.glyphs[:6]
        inner = max(20, width - 2)
        title = title[: inner - 4]
        top = f"  {tl}{h} {title} " + h * (inner - len(title) - 3) + tr
        self.write(self.paint(top, DIM))
        if subtitle:
            body = subtitle[: inner - 2]
            self.write(
                "  "
                + self.paint(v, DIM)
                + " "
                + body.ljust(inner - 2)
                + " "
                + self.paint(v, DIM)
            )
        bottom = f"  {bl}" + h * inner + br
        self.write(self.paint(bottom, DIM))

    def rule(self, title: str = "", *, width: int = 72) -> None:
        h = self.glyphs[4]
        if title:
            line = f"{h}{h} {title} " + h * max(0, width - len(title) - 6)
        else:
            line = h * width
        self.write("  " + self.paint(line, DIM))

    def section(self, title: str) -> None:
        self.blank()
        self.write("  " + self.paint(title, BOLD, CYAN))

    def field(self, label: str, value: str, hint: str = "", *, label_width: int = 20) -> None:
        label_text = self.paint(label.ljust(label_width), DIM)
        line = f"  {label_text}  {value}"
        if hint:
            line += "  " + self.paint(hint, DIM)
        self.write(line.rstrip())

    def status(self, kind: str, message: str) -> None:
        colors = {"ok": GREEN, "info": CYAN, "warn": YELLOW, "error": RED}
        icons = {
            "ok": self.glyphs[6],
            "info": self.glyphs[8],
            "warn": "!",
            "error": self.glyphs[7],
        }
        icon = self.paint(icons.get(kind, self.glyphs[8]), colors.get(kind, CYAN), BOLD)
        self.write(f"  {icon}  {message}")

    def info(self, message: str) -> None:
        self.status("info", message)

    def success(self, message: str) -> None:
        self.status("ok", message)

    def warn(self, message: str) -> None:
        self.status("warn", message)

    def error(self, message: str) -> None:
        self.status("error", message)

    def hint(self, message: str) -> None:
        self.write("     " + self.paint(message, DIM))

    # -- progress --------------------------------------------------------- #

    def progress_line(self, text: str) -> None:
        if not self.isatty:
            return
        self.stream.write("\r\033[2K" + text)
        self.stream.flush()

    def clear_progress_line(self) -> None:
        if self.isatty:
            self.stream.write("\r\033[2K")
            self.stream.flush()


class ProgressReporter:
    """Throttled live progress bar for the streaming writer.

    Designed to be passed directly as ``progress=`` to
    :func:`scripts.algorithm.write_range` / ``make_list``.
    """

    def __init__(
        self,
        console: Console,
        total: int,
        *,
        enabled: bool | None = None,
        interval: float = 0.15,
        bar_width: int = 24,
    ) -> None:
        self.console = console
        self.total = total
        self.enabled = (console.isatty if enabled is None else enabled) and total > 0
        self.interval = interval
        self.bar_width = bar_width
        self.started = time.perf_counter()
        self._last = 0.0
        self._active = False

    def __call__(self, done: int, total: int, elapsed: float, written: int) -> None:
        if not self.enabled:
            return
        total = total or self.total
        now = time.perf_counter()
        finished = done >= total
        if not finished and now - self._last < self.interval:
            return
        self._last = now

        fraction = (done / total) if total else 1.0
        fraction = min(1.0, max(0.0, fraction))
        filled = int(fraction * self.bar_width)
        bar = self.console.glyphs[9] * filled + self.console.glyphs[10] * (
            self.bar_width - filled
        )

        rate = done / elapsed if elapsed > 0 else 0.0
        megabytes = written / elapsed / 1_000_000 if elapsed > 0 else 0.0
        eta = None
        if 0 < done < total and elapsed > 0:
            eta = elapsed * (total - done) / done

        line = (
            "  "
            + self.console.paint(self.console.glyphs[8], CYAN)
            + " "
            + bar
            + f" {fraction * 100:5.1f}%  "
            + format_integer(done)
            + " / "
            + format_integer(total)
            + "  ·  "
            + format_rate(rate)
            + " words/s  ·  "
            + format_rate(megabytes)
            + " MB/s  ·  ETA "
            + format_duration(eta)
        )
        self._active = True
        self.console.progress_line(line)

    def close(self) -> None:
        if self._active:
            self.console.clear_progress_line()
            self._active = False
