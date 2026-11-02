#!/usr/bin/env python3
"""AdvancedPermuter benchmark suite.

Sections
--------
1. Random access  : resolve index 10^18 in O(L), no enumeration.
2. Memory proof   : trace the peak heap while streaming 1,000,000 words.
3. Throughput     : words/s and MB/s over several alphabets/lengths.
4. Memory wall    : a classical list build under a hard process memory limit
                    dies with MemoryError, while the engine finishes the same
                    space with its 64 KiB buffer.
5. Projections    : how much RAM a list-based approach would need.

Run ``python benchmark.py`` (about 10-20 seconds).  No third-party packages.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import tracemalloc

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import __version__  # noqa: E402
from scripts import algorithm as engine  # noqa: E402
from scripts.telemetry import (  # noqa: E402
    BOLD,
    DIM,
    Console,
    configure_stdio,
    format_duration,
    format_integer,
    format_rate,
)

ALPHA_10 = "0123456789"
ALPHA_26 = "abcdefghijklmnopqrstuvwxyz"
ALPHA_36 = ALPHA_10 + ALPHA_26
ALPHA_62 = ALPHA_26 + ALPHA_26.upper() + ALPHA_10

LARGE_ALPHABET = ALPHA_36
LARGE_LENGTH = 20
LARGE_INDEX = 10**18


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def human_seconds(seconds: float) -> str:
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f} µs"
    if seconds < 1:
        return f"{seconds * 1000:.2f} ms"
    return f"{seconds:.3f} s"


def table(console: Console, headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
    header = "  " + "  ".join(
        console.paint(text.ljust(width), DIM, BOLD)
        for text, width in zip(headers, widths)
    )
    console.write(header.rstrip())
    console.write("  " + console.paint("─" * (sum(widths) + 2 * (len(widths) - 1)), DIM))
    for row in rows:
        line = "  " + "  ".join(
            str(text).ljust(width) for text, width in zip(row, widths)
        )
        console.write(line.rstrip())


def measure_stream(length, alphabet, count, directory, tag) -> dict:
    path = os.path.join(directory, f"bench_{tag}.txt")
    started = time.perf_counter()
    results = engine.make_list(length, alphabet, path, begin=0, end=count)
    elapsed = time.perf_counter() - started
    written = sum(result.bytes_written for result in results)
    try:
        os.remove(path)
    except OSError:
        pass
    return {
        "length": length,
        "alphabet": alphabet,
        "count": count,
        "elapsed": elapsed,
        "bytes": written,
        "words_per_second": count / elapsed if elapsed else 0.0,
        "mb_per_second": written / elapsed / 1_000_000 if elapsed else 0.0,
    }


# --------------------------------------------------------------------------- #
# 1. Random access
# --------------------------------------------------------------------------- #


def run_random_access(ui: Console) -> dict:
    total = len(LARGE_ALPHABET) ** LARGE_LENGTH
    word = engine.unrank(LARGE_INDEX, LARGE_LENGTH, LARGE_ALPHABET)

    iterations = 50_000
    for _ in range(2_000):  # warm up
        engine.unrank(LARGE_INDEX, LARGE_LENGTH, LARGE_ALPHABET)
    started = time.perf_counter()
    for _ in range(iterations):
        engine.unrank(LARGE_INDEX, LARGE_LENGTH, LARGE_ALPHABET)
    elapsed = time.perf_counter() - started
    per_call = elapsed / iterations

    ui.section("1 · Random access without enumeration")
    ui.field("Space", f"{len(LARGE_ALPHABET)}^{LARGE_LENGTH} = {format_integer(total)} words")
    ui.field("Index", format_integer(LARGE_INDEX))
    ui.field("Word", word, f"{LARGE_LENGTH} symbols")
    ui.field(
        "Unrank time",
        f"{per_call * 1_000_000:.3f} µs  ·  {per_call * 1000:.5f} ms",
        f"{format_rate(1 / per_call)} calls/s",
    )
    ui.field("Work done", f"O(L) = {LARGE_LENGTH} divmod steps", "no list, no tree")

    # A random sample proves the access pattern is genuinely stateless.
    import random

    rng = random.Random(0xC0FFEE)
    sample = [rng.randrange(total) for _ in range(10)]
    started = time.perf_counter()
    for index in sample:
        engine.unrank(index, LARGE_LENGTH, LARGE_ALPHABET)
    random_elapsed = (time.perf_counter() - started) / len(sample)
    ui.field(
        "Random indices",
        f"{random_elapsed * 1_000_000:.3f} µs/op",
        "uniformly distributed over 10^31 indices",
    )
    return {"word": word, "per_call": per_call, "total": total}


# --------------------------------------------------------------------------- #
# 2. Constant memory
# --------------------------------------------------------------------------- #


def run_memory_proof(ui: Console, count: int = 1_000_000) -> dict:
    with tempfile.TemporaryDirectory(prefix="ap_mem_") as directory:
        # Warm up file I/O and lazy imports so the trace shows the steady-state
        # streaming heap rather than one-off interpreter initialisation.
        warm_path = os.path.join(directory, "warmup.txt")
        engine.make_list(3, "ab", warm_path)

        path = os.path.join(directory, "memory_proof.txt")
        tracemalloc.start()
        started = time.perf_counter()
        results = engine.make_list(8, ALPHA_26, path, begin=0, end=count)
        elapsed = time.perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        written = sum(result.bytes_written for result in results)
        file_size = os.path.getsize(results[0].path)

    ui.section("2 · Constant memory while streaming")
    ui.field("Words generated", format_integer(count))
    ui.field("On-disk output", f"{engine.format_bytes(file_size)} (SI)")
    ui.field("Engine buffer", f"{engine.BUFFER_SIZE:,} bytes (64 KiB)", "fixed")
    ui.field(
        "Peak traced heap",
        f"{peak / 1024:.1f} KiB",
        f"{peak / engine.BUFFER_SIZE:.2f}× the buffer",
    )
    ui.field("Elapsed", human_seconds(elapsed), f"{format_rate(count / elapsed)} words/s")
    ui.field("Interpretation", "memory is independent of N^L")
    return {"count": count, "peak": peak, "bytes": written, "elapsed": elapsed}


# --------------------------------------------------------------------------- #
# 3. Throughput
# --------------------------------------------------------------------------- #


def run_throughput(ui: Console, words: int) -> list[dict]:
    configurations = [
        ("0-9 · L=6", 6, ALPHA_10),
        ("a-z · L=5", 5, ALPHA_26),
        ("a-z · L=8", 8, ALPHA_26),
        ("0-9a-z · L=8", 8, ALPHA_36),
        ("0-9a-zA-Z · L=6", 6, ALPHA_62),
    ]
    rows = []
    results = []
    with tempfile.TemporaryDirectory(prefix="ap_bench_") as directory:
        for label, length, alphabet in configurations:
            count = min(words, len(alphabet) ** length)
            result = measure_stream(length, alphabet, count, directory, label.replace(" ", "_"))
            results.append(result)
            rows.append(
                [
                    label,
                    format_integer(result["count"]),
                    human_seconds(result["elapsed"]),
                    format_rate(result["words_per_second"]),
                    f"{result['mb_per_second']:.1f}",
                ]
            )

    ui.section("3 · Streaming throughput (real UTF-8 files, 64 KiB blocks)")
    table(
        ui,
        ["Configuration", "Words", "Time", "Words/s", "MB/s"],
        rows,
        [18, 12, 10, 10, 7],
    )
    ui.write(
        "  "
        + ui.paint(
            f"Disk writes are real; the OS page cache means these numbers are "
            f"upper bounds for an NVMe-class device.",
            DIM,
        )
    )
    return results


# --------------------------------------------------------------------------- #
# 4. Classical list vs 64 KiB engine (real MemoryError)
# --------------------------------------------------------------------------- #


def _apply_memory_limit(limit_bytes: int) -> bool:
    """Cap the current process' committed memory.  Returns success."""
    if os.name == "nt":  # pragma: no cover - Windows only
        try:
            import ctypes
            from ctypes import wintypes

            class BasicLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimitInformation),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return False
            info = ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = 0x00000100  # PROCESS_MEMORY
            info.ProcessMemoryLimit = limit_bytes
            if not kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                return False
            if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
                return False
            globals()["_JOB_HANDLE"] = job  # keep the job alive
            return True
        except Exception:
            return False

    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        return True
    except Exception:
        return False


def classic_child(alphabet: str, length: int, limit_mib: int) -> int:
    """Internal: materialise the whole space under a hard memory cap."""
    limit_bytes = limit_mib * 1024 * 1024
    limited = _apply_memory_limit(limit_bytes)
    if not limited:
        print(json.dumps({"status": "limit_unavailable"}))
        return 2

    from itertools import product

    started = time.perf_counter()
    counter = 0
    status = "completed"
    error = ""
    words = []
    try:
        for item in product(alphabet, repeat=length):
            words.append("".join(item))
            counter += 1
    except MemoryError:
        status = "memory_error"
        error = "MemoryError"
    elapsed = time.perf_counter() - started

    print(
        json.dumps(
            {
                "status": status,
                "count": counter,
                "elapsed": elapsed,
                "limit_mib": limit_mib,
                "error": error,
            }
        )
    )
    return 0 if status == "memory_error" else 1


def run_classical_comparison(ui: Console, limit_mib: int = 96) -> dict | None:
    length = 5
    alphabet = ALPHA_26
    total = len(alphabet) ** length
    ui.section("4 · Classical list vs. zero-allocation engine")

    command = [
        sys.executable,
        os.path.abspath(__file__),
        "--classic-child",
        "--alphabet",
        alphabet,
        "--length",
        str(length),
        "--limit-mib",
        str(limit_mib),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        ui.warn(f"Classical demo could not run: {error}")
        return None

    payload = None
    for line in reversed(completed.stdout.strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue

    if not payload or payload.get("status") == "limit_unavailable":
        ui.warn(
            "Could not enforce a process memory limit on this platform; "
            "showing the projection table only."
        )
        return None

    if payload["status"] == "completed":
        ui.warn("The list fit inside the cap; increase --classic-limit-mib.")
        return None

    classic = payload

    with tempfile.TemporaryDirectory(prefix="ap_classic_") as directory:
        path = os.path.join(directory, "engine_full_space.txt")
        started = time.perf_counter()
        results = engine.make_list(length, alphabet, path, begin=0, end=total)
        elapsed = time.perf_counter() - started
        written = sum(result.bytes_written for result in results)
        file_size = os.path.getsize(results[0].path)

    table(
        ui,
        ["Approach", "Outcome", "Words held", "Memory"],
        [
            [
                "Python list",
                f"MemoryError after {classic['elapsed']:.2f} s",
                format_integer(classic["count"]),
                f"{limit_mib} MiB cap",
            ],
            [
                "AdvancedPermuter",
                f"completed in {elapsed:.2f} s",
                format_integer(total),
                "64 KiB buffer",
            ],
        ],
        [17, 30, 17, 16],
    )
    ui.field(
        "Engine throughput",
        f"{format_rate(total / elapsed)} words/s  ·  "
        f"{format_rate(file_size / elapsed / 1_000_000)} MB/s",
        f"{engine.format_bytes(file_size)} written",
    )
    ui.field(
        "Classical progress",
        f"{classic['count'] / total * 100:.2f}% of the space",
        "the list never fits",
    )
    return {
        "classic": classic,
        "engine": {"elapsed": elapsed, "bytes": written, "total": total},
    }


# --------------------------------------------------------------------------- #
# 5. Projection table
# --------------------------------------------------------------------------- #


def run_projection(ui: Console) -> list[tuple[int, int]]:
    sample_size = 50_000
    sample = [engine.unrank(index, 5, ALPHA_26) for index in range(sample_size)]
    per_entry = (
        sys.getsizeof(sample) + sum(sys.getsizeof(word) for word in sample)
    ) / sample_size
    del sample

    spaces = [26**6, 26**8, 26**10]
    rows = []
    for space in spaces:
        estimated = int(space * per_entry)
        rows.append(
            [
                format_integer(space),
                engine.format_bytes(estimated),
                f"{engine.BUFFER_SIZE // 1024} KiB",
            ]
        )

    ui.section("5 · The memory wall")
    ui.field("Measured list overhead", f"{per_entry:.1f} bytes / word", "CPython 3.14")
    table(
        ui,
        ["Space (26^L)", "List-based RAM", "AdvancedPermuter"],
        rows,
        [20, 22, 22],
    )
    ui.write(
        "  "
        + ui.paint(
            "The engine stays at 65,536 bytes for every row; only the output "
            "file grows.",
            DIM,
        )
    )
    return [(space, int(space * per_entry)) for space in spaces]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AdvancedPermuter benchmark (zero-allocation unranking engine)."
    )
    parser.add_argument("--words", type=int, default=1_000_000, help="words per throughput row")
    parser.add_argument(
        "--classic-limit-mib",
        type=int,
        default=96,
        help="memory cap for the classical list comparison",
    )
    parser.add_argument("--quick", action="store_true", help="short run (~5 s)")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colours")
    parser.add_argument("--classic-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--alphabet", default=ALPHA_26, help=argparse.SUPPRESS)
    parser.add_argument("--length", type=int, default=5, help=argparse.SUPPRESS)
    parser.add_argument("--limit-mib", type=int, default=96, help=argparse.SUPPRESS)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.classic_child:
        return classic_child(args.alphabet, args.length, args.limit_mib)

    configure_stdio()

    if args.quick:
        args.words = min(args.words, 200_000)
        args.classic_limit_mib = min(args.classic_limit_mib, 64)

    ui = Console(sys.stdout, color=False if args.no_color else None)

    ui.blank()
    ui.header(
        f"AdvancedPermuter v{__version__} · benchmark",
        "zero-allocation combinatorial unranking",
        width=74,
    )
    ui.field("Python", sys.version.split()[0])
    ui.field("Platform", sys.platform)
    ui.field("CPU cores", str(os.cpu_count() or 1))
    ui.field("Buffer", f"{engine.BUFFER_SIZE:,} bytes (64 KiB)")
    ui.field("Dependencies", "none (standard library only)")

    total_started = time.perf_counter()
    run_random_access(ui)
    run_memory_proof(ui, count=args.words if args.quick else 1_000_000)
    run_throughput(ui, args.words)
    if args.quick:
        ui.section("4-5 · classical comparison skipped in --quick mode")
    else:
        run_classical_comparison(ui, args.classic_limit_mib)
        run_projection(ui)

    elapsed = time.perf_counter() - total_started
    ui.blank()
    ui.rule()
    ui.success(f"Benchmark completed in {format_duration(elapsed)}")
    ui.blank()
    return 0


if __name__ == "__main__":
    sys.exit(main())
