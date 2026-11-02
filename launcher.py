#!/usr/bin/env python3
"""AdvancedPermuter launcher.

Thin orchestration layer around :mod:`scripts.algorithm`:

* validates and normalises the command line,
* renders the session plan / telemetry,
* dispatches to the streaming writer (stdout or balanced part files),
* maps failures to documented exit codes.

The engine never materialises the enumeration; see ``scripts/algorithm.py``.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Sequence

from scripts import __version__
from scripts import algorithm as engine
from scripts.assets import APP_NAME, banner_text, help_text
from scripts.betterargparse import (
    Option,
    ParameterError,
    get_input,
    parse_int,
    parse_params,
)
from scripts.telemetry import (
    BOLD,
    Console,
    ProgressReporter,
    configure_stdio,
    format_duration,
    format_integer,
    format_rate,
)

OPTIONS: tuple[Option, ...] = (
    Option("help", ("-h", "--help"), "flag"),
    Option("version", ("-V", "--version"), "flag"),
    Option("wizard", ("-w", "--wizard"), "flag"),
    Option("quick", ("-q", "--quick"), "flag"),
    Option("length", ("-l", "--length"), "value", "L"),
    Option("alphabet", ("-s", "--string"), "multi", "SYMBOLS..."),
    Option("output", ("-o", "--out-file", "-f", "--file"), "value", "PATH"),
    Option("count", ("-c", "--count"), "value", "N"),
    Option("multi", ("-m", "--multi"), "value", "N"),
    Option("workers", ("--workers",), "value", "N"),
    Option("begin", ("--begin",), "value", "INDEX"),
    Option("end", ("--end",), "value", "INDEX"),
    Option("stdout", ("--stdout",), "flag"),
    Option("yes", ("-y", "--yes"), "flag"),
    Option("proceed", ("-p", "--proceed"), "value", "0|1|y|n"),
    Option("quiet", ("--quiet",), "flag"),
    Option("no_color", ("--no-color",), "flag"),
)

JOB_KEYS = frozenset(
    {
        "length",
        "alphabet",
        "output",
        "count",
        "multi",
        "workers",
        "begin",
        "end",
        "stdout",
    }
)


@dataclass(frozen=True)
class Job:
    """Fully validated description of one generation run."""

    length: int
    alphabet: tuple[str, ...]
    begin: int
    end: int
    total: int
    multi_files: int
    workers: int
    part_count: int
    output: str | None
    stream: bool

    @property
    def count(self) -> int:
        return self.end - self.begin


# --------------------------------------------------------------------------- #
# Job construction
# --------------------------------------------------------------------------- #


def normalize_alphabet(values: Sequence[str]) -> tuple[str, ...]:
    """Turn CLI tokens into a validated tuple of alphabet symbols.

    A single token is expanded into its characters (``abc`` -> ``a b c``) while
    two or more tokens are treated as multi-character symbols.  This keeps
    ``-s abc`` ergonomic without giving up ``-s aa bb`` word alphabets.
    """
    tokens = list(values)
    if not tokens:
        raise ParameterError("Missing alphabet; pass -s/--string.")
    if len(tokens) == 1:
        symbols = tuple(tokens[0])
    else:
        symbols = tuple(tokens)
    try:
        return engine.validate_alphabet(symbols)
    except (TypeError, ValueError) as error:
        message = str(error)
        if len(tokens) == 1:
            message += (
                " (one token is split into characters; pass two or more "
                "tokens for whole-word symbols)"
            )
        raise ParameterError(message) from None


def construct_job(
    *,
    length: int,
    alphabet: Sequence[str],
    output: str | None = None,
    stream: bool = False,
    begin: int = 0,
    end: int | None = None,
    count: int | None = None,
    multi_files: int = 1,
    workers: int = 1,
) -> Job:
    """Validate range/output settings and build a :class:`Job`."""
    try:
        alphabet = engine.validate_alphabet(alphabet)
    except (TypeError, ValueError) as error:
        raise ParameterError(str(error)) from None
    try:
        total = engine.total_combinations(length, alphabet)
    except (TypeError, ValueError) as error:
        raise ParameterError(str(error)) from None

    if begin < 0 or begin > total:
        raise ParameterError(
            f"--begin must be within [0, {format_integer(total)}], got {begin}"
        )
    if begin == total:
        raise ParameterError("--begin points at the end of the space; the range would be empty")

    if end is not None and count is not None:
        raise ParameterError("Use either --end or -c/--count, not both.")
    if end is not None:
        if end > total:
            raise ParameterError(
                f"--end ({format_integer(end)}) exceeds the space size "
                f"({format_integer(total)})"
            )
    elif count is not None and count > 0:
        end = begin + count
        if end > total:
            raise ParameterError(
                f"--begin + --count exceeds the space size ({format_integer(total)})"
            )
    else:
        end = total

    if end <= begin:
        raise ParameterError(
            f"empty range [{begin}, {end}); nothing to generate"
        )

    if workers < 1:
        raise ParameterError("--workers must be >= 1")
    if multi_files < 1:
        raise ParameterError("-m/--multi must be >= 1")

    if not stream:
        try:
            engine.plan_output(output or "")
        except (TypeError, ValueError) as error:
            raise ParameterError(str(error)) from None
    elif workers > 1:
        raise ParameterError("--stdout cannot be combined with --workers")

    spans = engine.plan_parts(begin, end, multi_files)
    return Job(
        length=length,
        alphabet=alphabet,
        begin=begin,
        end=end,
        total=total,
        multi_files=multi_files,
        workers=min(workers, len(spans)),
        part_count=len(spans),
        output=output,
        stream=stream,
    )


def build_job(params: dict[str, object]) -> Job:
    """Validate quick-mode parameters and construct the job."""
    if "length" not in params:
        raise ParameterError("Missing required option: -l/--length")
    length = parse_int(params["length"], "-l/--length", minimum=1)

    if "alphabet" not in params:
        raise ParameterError("Missing required option: -s/--string")
    alphabet = normalize_alphabet(params["alphabet"])  # type: ignore[arg-type]

    stream = bool(params.get("stdout"))
    output = params.get("output")
    if stream:
        if "output" in params:
            raise ParameterError("--stdout cannot be combined with -o/-f/--out-file")
        if "multi" in params:
            raise ParameterError("--stdout cannot be combined with -m/--multi")
        if "workers" in params:
            raise ParameterError("--stdout cannot be combined with --workers")
    elif output is None:
        raise ParameterError(
            "Missing required option: -o/--out-file (or -f/--file), or use --stdout"
        )

    count = (
        parse_int(params["count"], "-c/--count", minimum=0)
        if "count" in params
        else None
    )
    end = (
        parse_int(params["end"], "--end", minimum=0)
        if "end" in params
        else None
    )
    if "count" in params and "end" in params:
        raise ParameterError("Use either --end or -c/--count, not both.")

    begin = (
        parse_int(params["begin"], "--begin", minimum=0)
        if "begin" in params
        else 0
    )
    multi_files = (
        parse_int(params["multi"], "-m/--multi", minimum=1)
        if "multi" in params
        else 1
    )
    workers = (
        parse_int(params["workers"], "--workers", minimum=1)
        if "workers" in params
        else 1
    )

    if workers > 1 and "multi" not in params:
        # One worker per part file: zero write contention by construction.
        multi_files = workers

    return construct_job(
        length=length,
        alphabet=alphabet,
        output=output if isinstance(output, str) else None,
        stream=stream,
        begin=begin,
        end=end,
        count=count,
        multi_files=multi_files,
        workers=workers,
    )


def run_wizard(ui: Console) -> Job:
    """Interactive wizard (default when the launcher is started bare)."""
    ui.section("Wizard")
    length = get_input(
        prompt="  Word length              : ",
        type="int",
        min=1,
        type_err="  [!] Word length must be an integer.",
        range_err="  [!] Word length must be at least 1.",
    )
    tokens = get_input(
        prompt="  Alphabet (abc or aa bb)  : ",
        type="str",
        split=True,
        empty_err="  [!] Enter at least one symbol.",
    )
    alphabet = normalize_alphabet(tokens)
    output = get_input(
        prompt="  Output file              : ",
        type="str",
        empty_err="  [!] Enter an output file name.",
    )
    multi_files = get_input(
        prompt="  Part files [1]           : ",
        type="int",
        min=1,
        default=1,
        type_err="  [!] Part count must be an integer.",
        range_err="  [!] Part count must be at least 1.",
    )
    count = get_input(
        prompt="  Words to generate [0=all]: ",
        type="int",
        min=0,
        default=0,
        type_err="  [!] Word count must be an integer.",
        range_err="  [!] Word count must be at least 0.",
    )
    workers = get_input(
        prompt="  Worker processes [1]     : ",
        type="int",
        min=1,
        default=1,
        type_err="  [!] Worker count must be an integer.",
        range_err="  [!] Worker count must be at least 1.",
    )
    if workers > 1 and multi_files == 1:
        # Keep the "one worker, one part file" invariant automatic.
        multi_files = workers
    return construct_job(
        length=length,
        alphabet=alphabet,
        output=output,
        count=count,
        multi_files=multi_files,
        workers=workers,
    )


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def describe_alphabet(alphabet: Sequence[str], limit: int = 38) -> str:
    if all(len(symbol) == 1 for symbol in alphabet):
        text = "".join(alphabet)
    else:
        text = " ".join(alphabet)
    text = text.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def output_description(job: Job) -> tuple[str, str]:
    target = engine.plan_output(job.output or "")
    if job.part_count == 1:
        return target.path_for(1), "single file"
    first = os.path.basename(target.path_for(1))
    last = os.path.basename(target.path_for(job.part_count))
    return target.directory, f"{job.part_count} parts · {first} … {last}"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural or singular + 's'}"


def print_session(ui: Console, job: Job) -> None:
    """Render the plan: space, range, size estimate, memory and output."""
    estimated = engine.estimate_bytes(job.length, job.alphabet, job.count)
    size_si, size_iec = engine.format_bytes_pair(estimated)

    uniform = len({len(symbol) for symbol in job.alphabet}) == 1
    single_byte = all(
        len(symbol) == 1 and symbol.isascii() for symbol in job.alphabet
    )
    exact_size = uniform or job.count == job.total
    ui.blank()
    ui.rule("session")
    ui.field(
        "Alphabet",
        describe_alphabet(job.alphabet),
        f"{len(job.alphabet)} symbols",
    )
    ui.field("Word length", str(job.length))
    ui.field(
        "Total space",
        format_integer(job.total),
        f"{len(job.alphabet)}^{job.length}",
    )
    ui.field(
        "Index range",
        f"[{format_integer(job.begin)}, {format_integer(job.end)})",
    )
    ui.field("Words to generate", format_integer(job.count))
    ui.field(
        "Estimated size",
        f"{size_si} (SI)  ·  {size_iec} (IEC)",
        "exact" if exact_size else "approximate",
    )
    ui.field(
        "Memory footprint",
        (
            "64 KiB fixed buffer  ·  O(1) space"
            if single_byte
            else "64 KiB block target  ·  O(1) space"
        ),
    )

    if job.stream:
        ui.field("Output", "stdout (pipe mode)", "zero disk writes")
        ui.field("Parallelism", "single process")
    else:
        value, hint = output_description(job)
        ui.field("Output", value, hint)
        if job.workers > 1:
            ui.field(
                "Parallelism",
                _plural(job.workers, "worker process", "worker processes"),
                _plural(job.part_count, "part file"),
            )
        else:
            ui.field(
                "Parallelism",
                "1 process",
                _plural(job.part_count, "part file"),
            )
    ui.rule()
    ui.blank()


def _stdin_is_interactive() -> bool:
    """True only for a real terminal (Windows ``NUL`` also reports isatty)."""
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        try:
            import ctypes
            import msvcrt

            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            mode = ctypes.c_uint()
            return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
        except Exception:
            return False
    return True


def confirm(params: dict[str, object], ui: Console) -> bool:
    """Resolve the confirmation policy (``-y``, ``-p`` or an interactive ask)."""
    proceed = params.get("proceed")
    decision: bool | None = None
    if proceed is not None:
        value = str(proceed).strip().lower()
        if value in ("1", "y", "yes", "true"):
            decision = True
        elif value in ("n", "no", "false"):
            decision = False
        elif value != "0":
            raise ParameterError(
                f"-p/--proceed expects 0, 1, y or n, got {proceed!r}"
            )

    if params.get("yes"):
        return True
    if decision is not None:
        return decision

    if not _stdin_is_interactive():
        raise ParameterError(
            "Confirmation required but stdin is not interactive; pass -y/--yes."
        )

    prompt_stream = Console(sys.stderr, color=ui.color)
    prompt_stream.stream.write("  " + prompt_stream.paint("Proceed?", BOLD) + " [y/N] ")
    prompt_stream.stream.flush()
    answer = sys.stdin.readline()
    if answer == "":
        raise ParameterError("Input stream closed before confirmation.")
    return answer.strip().lower() in ("y", "yes")


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


class _TextBinaryAdapter:
    """Fallback when ``sys.stdout`` has no ``.buffer`` (embedded contexts)."""

    def __init__(self, stream) -> None:
        self.stream = stream

    def write(self, data: bytes) -> int:
        self.stream.write(data.decode("utf-8"))
        return len(data)

    def flush(self) -> None:
        self.stream.flush()


def run_stdout(job: Job, ui: Console, quiet: bool) -> int:
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        stream = _TextBinaryAdapter(sys.stdout)

    progress = ProgressReporter(ui, job.count, enabled=not quiet)
    started = time.perf_counter()
    try:
        stats = engine.write_range(
            job.length,
            job.alphabet,
            job.begin,
            job.end,
            stream,
            progress=progress,
        )
        stream.flush()
    except OSError as error:
        if not _is_broken_pipe(error):
            raise
        # `head`, `Select-Object -First`, a closed pipe, ... are normal stops.
        _silence_broken_pipe()
        return 0
    finally:
        progress.close()
    elapsed = time.perf_counter() - started

    if not quiet:
        ui.success(
            f"Streamed {format_integer(stats.count)} words in "
            f"{format_duration(elapsed)}  ·  "
            f"{format_rate(stats.count / elapsed)} words/s  ·  "
            f"{format_rate(stats.bytes_written / elapsed / 1_000_000)} MB/s"
        )
        ui.field(
            "Bytes streamed",
            f"{engine.format_bytes(stats.bytes_written)} (SI)  ·  "
            f"{engine.format_bytes(stats.bytes_written, 'IEC')} (IEC)",
        )
    return 0


def run_files(job: Job, ui: Console, quiet: bool) -> int:
    progress = ProgressReporter(ui, job.count, enabled=not quiet)

    def on_part(result: engine.PartResult) -> None:
        if quiet or job.part_count == 1:
            return
        progress.close()
        ui.status(
            "ok",
            f"part {result.index}/{job.part_count}  ·  "
            f"{format_integer(result.count)} words  ·  "
            f"{format_duration(result.elapsed)}  ·  {result.path}",
        )

    if not quiet and job.workers > 1:
        ui.info(f"Dispatching {job.workers} worker processes…")

    started = time.perf_counter()
    try:
        results = engine.make_list(
            job.length,
            job.alphabet,
            job.output or "",
            job.multi_files,
            job.begin,
            job.end,
            workers=job.workers,
            on_progress=progress,
            on_part=on_part,
        )
    finally:
        progress.close()
    elapsed = time.perf_counter() - started

    if not quiet:
        lines = sum(result.count for result in results)
        written = sum(result.bytes_written for result in results)
        aborted = any(result.aborted for result in results)
        ui.success(
            f"{'Aborted after' if aborted else 'Generated'} "
            f"{format_integer(lines)} words in {format_duration(elapsed)}  ·  "
            f"{format_rate(lines / elapsed)} words/s  ·  "
            f"{format_rate(written / elapsed / 1_000_000)} MB/s"
        )
        ui.field(
            "Bytes on disk",
            f"{engine.format_bytes(written)} (SI)  ·  "
            f"{engine.format_bytes(written, 'IEC')} (IEC)",
        )
        if results:
            if job.part_count == 1:
                ui.field("Output file", results[0].path)
            else:
                ui.field(
                    "Output files",
                    os.path.dirname(results[0].path),
                    f"{len(results)} part files",
                )
        ui.blank()
    return 130 if any(result.aborted for result in results) else 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _silence_broken_pipe() -> None:
    """Point fd 1 at the null device so interpreter shutdown cannot raise."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except Exception:  # pragma: no cover - best effort
        pass
    try:
        sys.stdout.flush()
    except OSError:
        pass


def _is_broken_pipe(error: OSError) -> bool:
    import errno

    if error.errno in (errno.EPIPE, errno.EINVAL):
        return True
    # Windows: ERROR_BROKEN_PIPE (109) / ERROR_NO_DATA (232)
    return getattr(error, "winerror", None) in (109, 232)


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    arguments = list(sys.argv[1:] if argv is None else argv)

    streaming_hint = "--stdout" in arguments
    color: bool | None = False if "--no-color" in arguments else None
    ui = Console(sys.stderr if streaming_hint else sys.stdout, color=color)
    err = Console(sys.stderr, color=color)

    try:
        params = parse_params(OPTIONS, arguments)
    except ParameterError as error:
        err.error(error.message)
        err.hint("Run with -h or --help for usage.")
        return 2

    if params.get("help"):
        print(help_text(os.path.basename(sys.argv[0]) or "launcher.py", __version__))
        return 0
    if params.get("version"):
        print(f"{APP_NAME} {__version__}")
        return 0

    if params.get("no_color") and ui.color:
        ui = Console(ui.stream, color=False)
        err = Console(sys.stderr, color=False)

    quiet = bool(params.get("quiet"))
    wizard = bool(params.get("wizard"))
    quick = bool(params.get("quick"))

    if wizard and quick:
        err.error("-w/--wizard and -q/--quick are mutually exclusive.")
        return 2

    if not wizard and not quick:
        wizard = not (JOB_KEYS & params.keys())

    if wizard:
        forbidden = JOB_KEYS & params.keys()
        if forbidden:
            names = ", ".join(sorted("--" + key.replace("_", "-") for key in forbidden))
            err.error(f"-w/--wizard cannot be combined with {names}.")
            return 2

    if not quiet:
        if not streaming_hint:
            ui.clear_screen()
        ui.banner(banner_text(__version__))

    try:
        job = run_wizard(ui) if wizard else build_job(params)
    except ParameterError as error:
        err.error(error.message)
        err.hint("Run with -h or --help for usage.")
        return 2

    if not quiet:
        print_session(ui, job)

    try:
        approved = confirm(params, ui)
    except ParameterError as error:
        err.error(error.message)
        err.hint("Run with -h or --help for usage.")
        return 2

    if not approved:
        if not quiet:
            ui.info("Cancelled.")
        return 0

    if not quiet:
        ui.blank()

    try:
        if job.stream:
            return run_stdout(job, ui, quiet)
        return run_files(job, ui, quiet)
    except BrokenPipeError:
        _silence_broken_pipe()
        return 0
    except OSError as error:
        if _is_broken_pipe(error):
            _silence_broken_pipe()
            return 0
        err.error(str(error))
        return 1
    except KeyboardInterrupt:
        if not quiet:
            ui.warn(
                "Interrupted — partial part files were kept; resume with "
                "--begin/--end."
            )
        return 130
    except (RuntimeError, ValueError) as error:
        err.error(str(error))
        return 1


def _entry() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        print("\n  [!] Interrupted.", file=sys.stderr)
        return 130
    except OSError as error:
        if _is_broken_pipe(error):
            _silence_broken_pipe()
            return 0
        raise


if __name__ == "__main__":
    sys.exit(_entry())
