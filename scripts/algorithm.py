"""AdvancedPermuter :: radix-style zero-allocation combinatorial unranking engine.

The engine enumerates every word of a fixed-length sequence over an alphabet
without materialising a tree, a list or a lookup table.  Index ``k`` is mapped
onto base-``N`` digits (``N = len(alphabet)``) with ``divmod`` and the digits
are used as direct table indices into the alphabet::

    k = d0 * N^(L-1) + d1 * N^(L-2) + ... + d(L-1)
    word(k) = alphabet[d0] + alphabet[d1] + ... + alphabet[d(L-1)]

Consequences:

* ``unrank()`` is O(L) time and O(L) scratch space - no dependence on ``N^L``.
* ``stream_range()`` is a generator: memory stays O(L), never O(N^L).
* ``write_range()`` writes through a single 64 KiB block buffer.

The public surface is intentionally dependency free (standard library only).
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import time
import traceback
from collections import deque
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator, NamedTuple, Sequence

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

BUFFER_SIZE = 64 * 1024
"""Size of the single write buffer used by the streaming engine (64 KiB)."""

DEFAULT_OUTPUT_DIR = "AdvancedPermuter_Out"
DEFAULT_EXTENSION = ".txt"

SI_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")
IEC_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")

SI_BASE = 1000
IEC_BASE = 1024

ENGINES = ("auto", "odometer", "divmod")

# progress(done, total, elapsed_seconds, bytes_written)
ProgressCallback = Callable[[int, int, float, int], None]
StopCheck = Callable[[], bool]


# --------------------------------------------------------------------------- #
# Human readable sizes (pure Python, exact integer scaling)
# --------------------------------------------------------------------------- #


def format_bytes(num_bytes: int, system: str = "SI", precision: int = 3) -> str:
    """Format a byte count using SI (1000) or IEC (1024) prefixes.

    The conversion is performed with integer arithmetic only, so no floating
    point rounding error can leak into the displayed value::

        >>> format_bytes(1536)
        '1.536 KB'
        >>> format_bytes(1536, "IEC")
        '1.5 KiB'
        >>> format_bytes(10**18)
        '1000 PB'

    Parameters
    ----------
    num_bytes:
        Non-negative number of bytes.
    system:
        ``"SI"`` (B, KB, MB, GB, TB, PB) or ``"IEC"`` (B, KiB, MiB, ...).
    precision:
        Maximum number of fractional digits.  Trailing zeros are trimmed, so
        the result is as precise as the exact value allows.
    """
    if isinstance(num_bytes, bool) or not isinstance(num_bytes, int):
        raise TypeError("num_bytes must be an integer")
    if num_bytes < 0:
        raise ValueError("num_bytes must be non-negative")
    if precision < 0:
        raise ValueError("precision must be non-negative")

    system = system.upper()
    if system == "SI":
        units, base = SI_UNITS, SI_BASE
    elif system == "IEC":
        units, base = IEC_UNITS, IEC_BASE
    else:
        raise ValueError("system must be 'SI' or 'IEC'")

    if num_bytes < base:
        return f"{num_bytes} {units[0]}"

    exponent = 0
    while exponent + 1 < len(units) and num_bytes >= base ** (exponent + 1):
        exponent += 1

    denominator = base ** exponent
    whole, remainder = divmod(num_bytes, denominator)

    fraction = ""
    if precision and remainder:
        digits = []
        for _ in range(precision):
            remainder *= 10
            digit, remainder = divmod(remainder, denominator)
            digits.append(str(digit))
        fraction = "".join(digits).rstrip("0")

    if fraction:
        return f"{whole}.{fraction} {units[exponent]}"
    return f"{whole} {units[exponent]}"


def format_bytes_pair(num_bytes: int, precision: int = 3) -> tuple[str, str]:
    """Return ``(SI, IEC)`` renderings of the same byte count."""
    return (
        format_bytes(num_bytes, "SI", precision),
        format_bytes(num_bytes, "IEC", precision),
    )


# --------------------------------------------------------------------------- #
# Core unranking primitives
# --------------------------------------------------------------------------- #


def total_combinations(length: int, alphabet: Sequence[str]) -> int:
    """Return ``len(alphabet) ** length`` (exact, arbitrary precision)."""
    if not isinstance(length, int) or isinstance(length, bool):
        raise TypeError("length must be an integer")
    if length < 1:
        raise ValueError("length must be >= 1")
    base = len(alphabet)
    if base < 1:
        raise ValueError("alphabet must contain at least one symbol")
    return base ** length


def validate_alphabet(alphabet: Sequence[str]) -> tuple[str, ...]:
    """Validate an alphabet and freeze it into a tuple.

    Raises ``ValueError`` for an empty alphabet, empty symbols or duplicate
    symbols (duplicates would silently repeat words and break the bijection).
    """
    symbols = tuple(alphabet)
    if not symbols:
        raise ValueError("alphabet must contain at least one symbol")
    seen: set[str] = set()
    duplicates: list[str] = []
    for symbol in symbols:
        if not isinstance(symbol, str):
            raise TypeError("alphabet symbols must be strings")
        if not symbol:
            raise ValueError("alphabet symbols must be non-empty strings")
        if symbol in seen:
            duplicates.append(symbol)
        else:
            seen.add(symbol)
    if duplicates:
        raise ValueError(
            "alphabet contains duplicate symbols: " + repr(duplicates)
        )
    return symbols


def unrank(index: int, length: int, alphabet: Sequence[str]) -> str:
    """Return the ``index``-th word of length ``length`` over ``alphabet``.

    This is the stateless heart of the engine: one O(L) base conversion, no
    table, no state.  The mapping is a bijection between ``[0, N**L)`` and the
    set of all words, ordered lexicographically.

    >>> unrank(0, 3, "ab")
    'aaa'
    >>> unrank(7, 3, "ab")
    'bbb'
    >>> unrank(5, 3, ["x", "yz"])
    'yzxyz'
    """
    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("index must be an integer")
    base = len(alphabet)
    if not isinstance(length, int) or isinstance(length, bool):
        raise TypeError("length must be an integer")
    if length < 1:
        raise ValueError("length must be >= 1")
    if base < 1:
        raise ValueError("alphabet must contain at least one symbol")
    if index < 0:
        raise IndexError(f"index must be >= 0, got {index}")

    zero = alphabet[0]
    if index == 0:
        return zero * length

    # No N**L exponentiation here: if digits are still left after L steps the
    # index is out of range.  Valid lookups therefore stay strictly O(L), even
    # for astronomically large lengths.
    word = [zero] * length
    position = length - 1
    value = index
    while value and position >= 0:
        value, remainder = divmod(value, base)
        word[position] = alphabet[remainder]
        position -= 1
    if value:
        raise IndexError(
            f"index must satisfy 0 <= index < {base}^{length}, got {index}"
        )
    return "".join(word)


def generate_word(index: int, length: int, numerals: Sequence[str]) -> str:
    """Legacy alias of :func:`unrank` (kept for backwards compatibility)."""
    return unrank(index, length, numerals)


def convert_base(num: int, numerals: Sequence[str]) -> str:
    """Legacy helper: represent ``num`` in ``numerals`` without padding.

    :func:`unrank` is the engine primitive; this function exists only for
    backwards compatibility with older callers.
    """
    if isinstance(num, bool) or not isinstance(num, int):
        raise TypeError("num must be an integer")
    if num < 0:
        raise ValueError("num must be non-negative")
    base = len(numerals)
    if base < 1:
        raise ValueError("alphabet must contain at least one symbol")
    if num == 0:
        return numerals[0]
    digits = []
    while num:
        num, remainder = divmod(num, base)
        digits.append(numerals[remainder])
    digits.reverse()
    return "".join(digits)


def stream_range(
    length: int,
    alphabet: Sequence[str],
    begin: int = 0,
    end: int | None = None,
) -> Iterator[str]:
    """Lazily yield ``[begin, end)`` over an alphabet, holding no list in RAM.

    The returned object is a real generator: at any point in time only the
    current word, the alphabet and a few integers are alive.  Memory is O(L)
    regardless of how large ``N**L`` is.

    >>> "".join(stream_range(2, "ab", 1, 4))
    'abbabb'
    """
    symbols = validate_alphabet(alphabet)
    base = len(symbols)
    total = base ** length

    if isinstance(begin, bool) or not isinstance(begin, int):
        raise TypeError("begin must be an integer")
    if end is None:
        stop = total
    else:
        if isinstance(end, bool) or not isinstance(end, int):
            raise TypeError("end must be an integer")
        stop = end
    if begin < 0 or stop > total or begin > stop:
        raise ValueError(f"invalid range [{begin}, {stop}) for space of {total}")

    return _stream_words(length, symbols, begin, stop)


def _stream_words(
    length: int,
    symbols: tuple[str, ...],
    begin: int,
    end: int,
) -> Iterator[str]:
    base = len(symbols)
    zero = symbols[0]
    divmod_ = divmod
    for index in range(begin, end):
        if index == 0:
            yield zero * length
            continue
        word = [zero] * length
        position = length - 1
        value = index
        while value:
            value, remainder = divmod_(value, base)
            word[position] = symbols[remainder]
            position -= 1
        yield "".join(word)


# --------------------------------------------------------------------------- #
# Output planning
# --------------------------------------------------------------------------- #


class PartSpan(NamedTuple):
    """A contiguous half-open index range ``[begin, end)``."""

    begin: int
    end: int

    @property
    def count(self) -> int:
        return self.end - self.begin


@dataclass(frozen=True)
class OutputTarget:
    """Resolved output location: directory + stem + extension."""

    directory: str
    stem: str
    extension: str

    def path_for(self, index: int) -> str:
        return os.path.join(self.directory, f"{self.stem}_{index}{self.extension}")

    @property
    def pattern(self) -> str:
        return os.path.join(self.directory, f"{self.stem}_*{self.extension}")


def split_output_name(output: str) -> tuple[str, str]:
    """Split ``output`` into stem + extension, defaulting to ``.txt``.

    ``os.path.splitext`` is used so a user supplied name without an extension
    (``dataset``) no longer produces an empty name fragment.
    """
    if not output or not output.strip():
        raise ValueError("output path must not be empty")
    stem, extension = os.path.splitext(output)
    if extension == ".":
        stem, extension = output[:-1], ""
    if not extension:
        extension = DEFAULT_EXTENSION
    if not stem or os.path.basename(stem) in ("", ".", ".."):
        raise ValueError(f"invalid output file name: {output!r}")
    return stem, extension


def plan_output(output: str) -> OutputTarget:
    """Resolve a user supplied output path into a concrete target.

    If the name carries no directory component the files are placed inside
    :data:`DEFAULT_OUTPUT_DIR` (``AdvancedPermuter_Out``).
    """
    stem, extension = split_output_name(output)
    directory, name = os.path.split(stem)
    if not directory:
        directory = DEFAULT_OUTPUT_DIR
    return OutputTarget(directory=directory, stem=name, extension=extension)


def plan_parts(begin: int, end: int, count: int = 1) -> list[PartSpan]:
    """Split ``[begin, end)`` into ``count`` balanced, non-empty spans.

    The remainder of ``divmod(total_items, count)`` is distributed one extra
    item at a time over the first spans, so the largest and smallest span
    differ by at most one element.  When ``total_items < count`` empty spans
    are dropped instead of producing empty files.
    """
    if isinstance(begin, bool) or not isinstance(begin, int):
        raise TypeError("begin must be an integer")
    if isinstance(end, bool) or not isinstance(end, int):
        raise TypeError("end must be an integer")
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("count must be an integer")
    if count < 1:
        raise ValueError("part count must be >= 1")
    if end <= begin:
        raise ValueError(f"range [{begin}, {end}) is empty")

    total_items = end - begin
    quotient, remainder = divmod(total_items, count)

    spans: list[PartSpan] = []
    cursor = begin
    for part in range(count):
        size = quotient + (1 if part < remainder else 0)
        if size == 0:
            continue
        spans.append(PartSpan(cursor, cursor + size))
        cursor += size
    return spans


def estimate_bytes(length: int, alphabet: Sequence[str], count: int) -> int:
    """Estimate the on-disk size of ``count`` newline terminated words.

    Exact for uniform-width alphabets (the common case), otherwise the average
    symbol width is used.
    """
    if not isinstance(count, int) or isinstance(count, bool):
        raise TypeError("count must be an integer")
    if count < 0:
        raise ValueError("count must be non-negative")
    symbols = validate_alphabet(alphabet)
    if not isinstance(length, int) or isinstance(length, bool):
        raise TypeError("length must be an integer")
    if length < 1:
        raise ValueError("length must be >= 1")
    if count == 0:
        return 0
    symbol_chars = sum(len(symbol) for symbol in symbols)
    return (symbol_chars * length * count) // len(symbols) + count


def calculate_size(
    words: Sequence[str],
    length: int,
    count: int | None = None,
) -> tuple[str, str]:
    """Return ``(SI, IEC)`` size estimates for the whole (or partial) space."""
    symbols = validate_alphabet(words)
    total = total_combinations(length, symbols)
    items = total if count is None else count
    estimated = estimate_bytes(length, symbols, items)
    return format_bytes(estimated, "SI"), format_bytes(estimated, "IEC")


# --------------------------------------------------------------------------- #
# Streaming writer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WriteStats:
    """Outcome of a :func:`write_range` call."""

    begin: int
    count: int
    bytes_written: int
    elapsed: float
    buffer_size: int = BUFFER_SIZE
    aborted: bool = False

    @property
    def end(self) -> int:
        return self.begin + self.count

    @property
    def words_per_second(self) -> float:
        return self.count / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def bytes_per_second(self) -> float:
        return self.bytes_written / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def megabytes_per_second(self) -> float:
        return self.bytes_per_second / 1_000_000


def write_range(
    length: int,
    alphabet: Sequence[str],
    begin: int,
    end: int,
    stream: BinaryIO,
    *,
    buffer_size: int = BUFFER_SIZE,
    engine: str = "auto",
    progress: ProgressCallback | None = None,
    should_stop: StopCheck | None = None,
) -> WriteStats:
    """Write ``[begin, end)`` as newline terminated UTF-8 words to ``stream``.

    Only one ``buffer_size`` block is ever alive.  ``engine`` selects between
    the incremental odometer (``"auto"``/``"odometer"``, fastest) and a strict
    per-index ``divmod`` unrank (``"divmod"``).  Both produce byte-identical
    output; the latter is useful for verification.
    """
    if stream is None or not hasattr(stream, "write"):
        raise TypeError("stream must be a writable binary file object")
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES!r}")
    if not isinstance(buffer_size, int) or isinstance(buffer_size, bool):
        raise TypeError("buffer_size must be an integer")
    if buffer_size < 1:
        raise ValueError("buffer_size must be >= 1")

    symbols = validate_alphabet(alphabet)
    if not isinstance(begin, int) or isinstance(begin, bool):
        raise TypeError("begin must be an integer")
    if not isinstance(end, int) or isinstance(end, bool):
        raise TypeError("end must be an integer")

    total = total_combinations(length, symbols)
    if begin < 0 or end > total or begin > end:
        raise ValueError(f"invalid range [{begin}, {end}) for space of {total}")

    selected = "odometer" if engine == "auto" else engine
    single_byte_alphabet = all(
        len(symbol) == 1 and symbol.isascii() for symbol in symbols
    )
    if single_byte_alphabet:
        writer = (
            _write_odometer_bytes
            if selected == "odometer"
            else _write_divmod_bytes
        )
    else:
        writer = (
            _write_odometer_text
            if selected == "odometer"
            else _write_divmod_text
        )
    return writer(
        length,
        symbols,
        begin,
        end,
        stream,
        buffer_size,
        progress,
        should_stop,
    )


def _write_odometer_bytes(
    length: int,
    alphabet: tuple[str, ...],
    begin: int,
    end: int,
    stream: BinaryIO,
    buffer_size: int,
    progress: ProgressCallback | None,
    should_stop: StopCheck | None,
) -> WriteStats:
    """Flat 64 KiB ``bytearray`` block: peak memory stays at the buffer size.

    Used whenever every symbol is a single ASCII character (a-z, 0-9, hex,
    base62, ...), which is the common case for radix enumeration.  The odometer is
    seeded once from ``begin`` with an O(L) divmod unrank and then advances by
    one, so no per-word base conversion is needed.
    """
    started = time.perf_counter()
    codes = bytes(ord(symbol) for symbol in alphabet)
    base = len(codes)
    zero = codes[0]
    count = end - begin

    digits = [0] * length
    value = begin
    position = length - 1
    while value:
        value, remainder = divmod(value, base)
        digits[position] = remainder
        position -= 1

    word = bytearray(length)
    for position, digit in enumerate(digits):
        word[position] = codes[digit]

    block = bytearray()
    extend = block.extend
    append = block.append
    write = stream.write
    written = 0
    done = 0
    aborted = False

    for _ in range(count):
        extend(word)
        append(10)
        done += 1
        if len(block) >= buffer_size:
            write(block)
            written += len(block)
            block.clear()
            if progress is not None:
                progress(done, count, time.perf_counter() - started, written)
            if should_stop is not None and should_stop():
                aborted = True
                break

        position = length - 1
        while position >= 0:
            digit = digits[position] + 1
            if digit < base:
                digits[position] = digit
                word[position] = codes[digit]
                break
            digits[position] = 0
            word[position] = zero
            position -= 1

    if block:
        write(block)
        written += len(block)
    elapsed = time.perf_counter() - started
    if progress is not None:
        progress(done, count, elapsed, written)
    return WriteStats(
        begin=begin,
        count=done,
        bytes_written=written,
        elapsed=elapsed,
        buffer_size=buffer_size,
        aborted=aborted,
    )


def _write_divmod_bytes(
    length: int,
    alphabet: tuple[str, ...],
    begin: int,
    end: int,
    stream: BinaryIO,
    buffer_size: int,
    progress: ProgressCallback | None,
    should_stop: StopCheck | None,
) -> WriteStats:
    """Strict per-index divmod unrank writing into a flat 64 KiB block."""
    started = time.perf_counter()
    codes = bytes(ord(symbol) for symbol in alphabet)
    base = len(codes)
    zero = codes[0]
    count = end - begin
    zero_prefix = bytes([zero]) * length

    word = bytearray(length)
    block = bytearray()
    extend = block.extend
    append = block.append
    write = stream.write
    divmod_ = divmod
    written = 0
    done = 0
    aborted = False

    for index in range(begin, end):
        value = index
        position = length - 1
        while value:
            value, remainder = divmod_(value, base)
            word[position] = codes[remainder]
            position -= 1
        if position >= 0:
            word[: position + 1] = zero_prefix[: position + 1]

        extend(word)
        append(10)
        done += 1
        if len(block) >= buffer_size:
            write(block)
            written += len(block)
            block.clear()
            if progress is not None:
                progress(done, count, time.perf_counter() - started, written)
            if should_stop is not None and should_stop():
                aborted = True
                break

    if block:
        write(block)
        written += len(block)
    elapsed = time.perf_counter() - started
    if progress is not None:
        progress(done, count, elapsed, written)
    return WriteStats(
        begin=begin,
        count=done,
        bytes_written=written,
        elapsed=elapsed,
        buffer_size=buffer_size,
        aborted=aborted,
    )


def _encode_block(lines: list[str], encoding: str) -> bytes:
    return ("\n".join(lines) + "\n").encode(encoding)


def _write_odometer_text(
    length: int,
    alphabet: tuple[str, ...],
    begin: int,
    end: int,
    stream: BinaryIO,
    buffer_size: int,
    progress: ProgressCallback | None,
    should_stop: StopCheck | None,
) -> WriteStats:
    started = time.perf_counter()
    base = len(alphabet)
    zero = alphabet[0]
    count = end - begin

    digits = [0] * length
    value = begin
    position = length - 1
    while value:
        value, remainder = divmod(value, base)
        digits[position] = remainder
        position -= 1
    symbols = [alphabet[digit] for digit in digits]

    symbol_width = len(zero)
    uniform_width = True
    for symbol in alphabet[1:]:
        if len(symbol) != symbol_width:
            uniform_width = False
            break
    line_chars = symbol_width * length + 1 if uniform_width else -1

    encoding = "ascii" if all(symbol.isascii() for symbol in alphabet) else "utf-8"
    join = "".join
    write = stream.write
    buffer: list[str] = []
    append = buffer.append
    size = 0
    written = 0
    done = 0
    aborted = False

    for _ in range(count):
        append(join(symbols))
        done += 1
        if line_chars > 0:
            size += line_chars
        else:
            size += sum(map(len, symbols)) + 1
        if size >= buffer_size:
            payload = _encode_block(buffer, encoding)
            write(payload)
            written += len(payload)
            buffer.clear()
            size = 0
            if progress is not None:
                progress(done, count, time.perf_counter() - started, written)
            if should_stop is not None and should_stop():
                aborted = True
                break

        position = length - 1
        while position >= 0:
            digit = digits[position] + 1
            if digit < base:
                digits[position] = digit
                symbols[position] = alphabet[digit]
                break
            digits[position] = 0
            symbols[position] = zero
            position -= 1

    if buffer:
        payload = _encode_block(buffer, encoding)
        write(payload)
        written += len(payload)
    elapsed = time.perf_counter() - started
    if progress is not None:
        progress(done, count, elapsed, written)
    return WriteStats(
        begin=begin,
        count=done,
        bytes_written=written,
        elapsed=elapsed,
        buffer_size=buffer_size,
        aborted=aborted,
    )


def _write_divmod_text(
    length: int,
    alphabet: tuple[str, ...],
    begin: int,
    end: int,
    stream: BinaryIO,
    buffer_size: int,
    progress: ProgressCallback | None,
    should_stop: StopCheck | None,
) -> WriteStats:
    started = time.perf_counter()
    base = len(alphabet)
    zero = alphabet[0]
    count = end - begin

    symbol_width = len(zero)
    uniform_width = True
    for symbol in alphabet[1:]:
        if len(symbol) != symbol_width:
            uniform_width = False
            break
    line_chars = symbol_width * length + 1 if uniform_width else -1

    encoding = "ascii" if all(symbol.isascii() for symbol in alphabet) else "utf-8"
    join = "".join
    write = stream.write
    divmod_ = divmod
    buffer: list[str] = []
    append = buffer.append
    size = 0
    written = 0
    done = 0
    aborted = False

    for index in range(begin, end):
        value = index
        word = [zero] * length
        position = length - 1
        while value:
            value, remainder = divmod_(value, base)
            word[position] = alphabet[remainder]
            position -= 1
        append(join(word))
        done += 1
        if line_chars > 0:
            size += line_chars
        else:
            size += sum(map(len, word)) + 1
        if size >= buffer_size:
            payload = _encode_block(buffer, encoding)
            write(payload)
            written += len(payload)
            buffer.clear()
            size = 0
            if progress is not None:
                progress(done, count, time.perf_counter() - started, written)
            if should_stop is not None and should_stop():
                aborted = True
                break

    if buffer:
        payload = _encode_block(buffer, encoding)
        write(payload)
        written += len(payload)
    elapsed = time.perf_counter() - started
    if progress is not None:
        progress(done, count, elapsed, written)
    return WriteStats(
        begin=begin,
        count=done,
        bytes_written=written,
        elapsed=elapsed,
        buffer_size=buffer_size,
        aborted=aborted,
    )


# --------------------------------------------------------------------------- #
# File production (sequential + multiprocessing)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PartPlan:
    index: int
    begin: int
    end: int
    path: str


@dataclass(frozen=True)
class PartResult:
    index: int
    begin: int
    end: int
    path: str
    count: int
    bytes_written: int
    elapsed: float
    aborted: bool = False


def make_list(
    length: int,
    words: Sequence[str],
    output: str,
    multi_files: int = 1,
    begin: int = 0,
    end: int = 0,
    *,
    workers: int = 1,
    engine: str = "auto",
    on_progress: ProgressCallback | None = None,
    on_part: Callable[[PartResult], None] | None = None,
) -> list[PartResult]:
    """Generate ``[begin, end)`` into one or more UTF-8 part files.

    Files are opened with ``"wb"`` — the truncating binary equivalent of text
    ``"w"`` with ``encoding="utf-8"`` — so an existing file is truncated
    instead of appended to, newlines are always ``\\n`` and the byte count
    matches the estimate on every platform.  Part files are balanced with
    ``divmod`` and empty parts are skipped.

    ``end=0`` is the historical sentinel for "the end of the whole space".
    """
    alphabet = validate_alphabet(words)
    total = total_combinations(length, alphabet)

    if isinstance(multi_files, bool) or not isinstance(multi_files, int):
        raise TypeError("multi_files must be an integer")
    if multi_files < 1:
        raise ValueError("multi_files must be >= 1")
    if isinstance(workers, bool) or not isinstance(workers, int):
        raise TypeError("workers must be an integer")
    if workers < 1:
        raise ValueError("workers must be >= 1")
    if isinstance(begin, bool) or not isinstance(begin, int):
        raise TypeError("begin must be an integer")
    if isinstance(end, bool) or not isinstance(end, int):
        raise TypeError("end must be an integer")
    if end == 0:
        end = total
    if begin < 0 or end > total or begin > end:
        raise ValueError(f"invalid range [{begin}, {end}) for space of {total}")
    if begin == end:
        raise ValueError("range is empty; nothing to generate")

    spans = plan_parts(begin, end, multi_files)
    target = plan_output(output)
    try:
        os.makedirs(target.directory, exist_ok=True)
    except OSError as error:
        raise OSError(f"cannot create output directory {target.directory!r}: {error}") from error

    tasks = [
        PartPlan(
            index=index,
            begin=span.begin,
            end=span.end,
            path=target.path_for(index),
        )
        for index, span in enumerate(spans, start=1)
    ]

    if workers > 1 and len(tasks) > 1:
        return _run_parallel(
            length, alphabet, tasks, min(workers, len(tasks)), engine, on_part
        )
    return _run_sequential(
        length, alphabet, tasks, engine, on_progress, on_part
    )


def _run_sequential(
    length: int,
    alphabet: tuple[str, ...],
    tasks: list[PartPlan],
    engine: str,
    on_progress: ProgressCallback | None,
    on_part: Callable[[PartResult], None] | None,
) -> list[PartResult]:
    started = time.perf_counter()
    total_lines = sum(task.end - task.begin for task in tasks)
    completed_lines = 0
    completed_bytes = 0
    results: list[PartResult] = []

    for task in tasks:
        offset = completed_lines
        offset_bytes = completed_bytes

        def part_progress(
            done: int,
            _part_total: int,
            _part_elapsed: float,
            written: int,
            _offset: int = offset,
            _offset_bytes: int = offset_bytes,
        ) -> None:
            if on_progress is not None:
                on_progress(
                    _offset + done,
                    total_lines,
                    time.perf_counter() - started,
                    _offset_bytes + written,
                )

        with open(task.path, "wb", buffering=0) as handle:
            stats = write_range(
                length,
                alphabet,
                task.begin,
                task.end,
                handle,
                engine=engine,
                progress=part_progress,
            )

        result = PartResult(
            index=task.index,
            begin=task.begin,
            end=task.end,
            path=task.path,
            count=stats.count,
            bytes_written=stats.bytes_written,
            elapsed=stats.elapsed,
            aborted=stats.aborted,
        )
        completed_lines += stats.count
        completed_bytes += stats.bytes_written
        results.append(result)
        if on_part is not None:
            on_part(result)

    return results


def _part_worker(
    length: int,
    alphabet: tuple[str, ...],
    task: PartPlan,
    engine: str,
    result_queue: "multiprocessing.Queue",
    stop_event: "multiprocessing.Event",
) -> None:
    try:
        try:
            with open(task.path, "wb", buffering=0) as handle:
                stats = write_range(
                    length,
                    alphabet,
                    task.begin,
                    task.end,
                    handle,
                    engine=engine,
                    should_stop=stop_event.is_set,
                )
            result_queue.put(
                {
                    "status": "ok",
                    "pid": os.getpid(),
                    "index": task.index,
                    "begin": task.begin,
                    "count": stats.count,
                    "bytes_written": stats.bytes_written,
                    "elapsed": stats.elapsed,
                    "aborted": stats.aborted,
                }
            )
        except BaseException as error:  # noqa: BLE001 - reported to the parent
            result_queue.put(
                {
                    "status": "error",
                    "pid": os.getpid(),
                    "index": task.index,
                    "error": f"{type(error).__name__}: {error}",
                    "traceback": traceback.format_exc(),
                }
            )
    finally:
        # Flush the queue feeder thread before the process disappears.
        try:
            result_queue.close()
            result_queue.join_thread()
        except Exception:  # pragma: no cover - best effort cleanup
            pass


def _run_parallel(
    length: int,
    alphabet: tuple[str, ...],
    tasks: list[PartPlan],
    workers: int,
    engine: str,
    on_part: Callable[[PartResult], None] | None,
) -> list[PartResult]:
    context = multiprocessing.get_context("spawn")
    result_queue: "multiprocessing.Queue" = context.Queue()
    stop_event: "multiprocessing.Event" = context.Event()
    pending = deque(tasks)
    running: dict[int, "multiprocessing.Process"] = {}
    results: dict[int, PartResult] = {}
    failure: str | None = None

    try:
        while pending or running:
            while pending and len(running) < workers:
                task = pending.popleft()
                process = context.Process(
                    target=_part_worker,
                    args=(length, alphabet, task, engine, result_queue, stop_event),
                    daemon=True,
                )
                process.start()
                running[process.pid] = process

            try:
                message = result_queue.get(timeout=0.2)
            except queue.Empty:
                message = None
                for pid, process in list(running.items()):
                    if not process.is_alive():
                        try:
                            message = result_queue.get(timeout=0.5)
                        except queue.Empty:
                            message = {
                                "status": "error",
                                "pid": pid,
                                "index": None,
                                "error": "worker exited without reporting a result",
                                "traceback": "",
                            }
                        break

            if message is None:
                continue

            pid = message.get("pid")
            process = running.pop(pid, None)
            if process is not None:
                process.join(timeout=2.0)
                if process.is_alive():
                    process.terminate()

            if message["status"] != "ok":
                failure = message.get("error", "unknown worker error")
                details = message.get("traceback", "")
                if details:
                    failure = f"{failure}\n{details}"
                break

            task = next(item for item in tasks if item.index == message["index"])
            result = PartResult(
                index=task.index,
                begin=task.begin,
                end=task.end,
                path=task.path,
                count=message["count"],
                bytes_written=message["bytes_written"],
                elapsed=message["elapsed"],
                aborted=message["aborted"],
            )
            results[result.index] = result
            if on_part is not None:
                on_part(result)

        if failure is not None:
            raise RuntimeError(f"worker failed: {failure}")
    finally:
        stop_event.set()
        for process in running.values():
            process.join(timeout=2.0)
            if process.is_alive():
                process.terminate()
        result_queue.close()
        result_queue.join_thread()

    return [results[index] for index in sorted(results)]
