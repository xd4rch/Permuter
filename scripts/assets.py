"""Static presentation assets: banner and help screen.

Kept free of logic so the launcher (and tests) can render it anywhere.
"""

from __future__ import annotations

APP_NAME = "Permuter"
TAGLINE = "Radix-Style Zero-Allocation Combinatorial Unranking Engine"

BANNER_ART = r"""
  ░█▀█░█▀▀░█▀▄░█▄█░█░█░▀█▀░█▀▀░█▀▄
  ░█▀▀░█▀▀░█▀▄░█░█░█░█░░█░░█▀▀░█▀▄
  ░▀░░░▀▀▀░▀░▀░▀░▀░▀▀▀░░▀░░▀▀▀░▀░▀
"""


def banner_text(version: str) -> str:
    """Return the ASCII banner including version and tagline."""
    return (
        BANNER_ART.rstrip("\n")
        + "\n"
        + f"  {APP_NAME} v{version}  ·  {TAGLINE}\n"
    )


def help_text(program: str = "launcher.py", version: str = "3.0.0") -> str:
    """Return the full ``--help`` screen."""
    return f"""{APP_NAME} {version} — {TAGLINE}

USAGE
  python {program} [mode] [options]

MODES
  -h, --help                 Show this help and exit.
  -V, --version              Show the version and exit.
  -w, --wizard               Interactive wizard (default when no arguments).
  -q, --quick                Direct, non-interactive mode.

TARGET SPACE
  -l, --length <int>         Word length L. Required in quick mode.
  -s, --string <symbols...>  Alphabet. One token is split into characters
                             ("-s abc" -> a, b, c); multiple tokens are whole
                             word symbols ("-s deneme test 123" -> 3 symbols).
                             Required in quick mode. Use "-s=-" for a literal -.
  -c, --count <int>          Number of words to generate (0 = the whole range).
  --begin <int>              First index of the half-open slice [begin, end).
                             Default: 0. This is the distributed/resume hook.
  --end <int>                Exclusive end index. Default: N^L (or begin+count).
                             "Use --end or -c, not both."

OUTPUT
  -o, --out-file <path>      Output file. Alias: -f, --file.
  -f, --file <path>          Same destination as -o/--out-file.
                             Without a directory the file is written inside
                             Permuter_Out/name_1.txt.
                             Without an extension ".txt" is appended.
  -m, --multi <int>          Split the range into N balanced part files.
      --workers <int>        Generate with N processes. Each process owns one
                             part file, so there is zero write contention.
      --stdout               Stream words to stdout instead of files. Telemetry
                             moves to stderr, so the pipe stays clean.
                             (--stdout cannot be combined with -o/-f/-m/--workers)

CONTROL
  -y, --yes                  Skip the confirmation prompt.
  -p, --proceed <value>      Confirmation policy: 0 = ask (default),
                             1/y = accept, n = decline.
      --quiet                Suppress banner, progress and the final summary.
      --no-color             Disable ANSI colours (also honours NO_COLOR).

EXAMPLES
  Full 8-character space over a-z, split into 10 files, no questions:
    python {program} -q -l 8 -s abcdefghijklmnopqrstuvwxyz -o dataset -m 10 -y

  Resume / distributed shard 7 of 10 (compute the slice on any machine):
    python {program} -q -l 8 -s abc -o shard7 --begin 1679616000 --end 1889568000 -y

  Four cores, four part files, zero write contention:
    python {program} -q -l 6 -s abcdefghijklmnopqrstuvwxyz -o words --workers 4 -y

  Stream to another tool without touching the disk:
    python {program} -q -l 6 -s abc --stdout -y | head -n 100

  Library use:
    python -c "from scripts.algorithm import unrank; print(unrank(10**18, 20, '0123456789abcdefghijklmnopqrstuvwxyz'))"

EXIT CODES
  0   success (or a clean user cancellation / closed pipe)
  1   runtime error (I/O, worker failure, ...)
  2   invalid arguments
  130 interrupted with Ctrl+C
"""


# --------------------------------------------------------------------------- #
# Backwards-compatible helpers (the old launcher printed directly)
# --------------------------------------------------------------------------- #


def banner(version: str = "3.0.0") -> None:
    """Print the banner (legacy helper)."""
    print(banner_text(version))


def help(program: str = "launcher.py", version: str = "3.0.0") -> None:  # noqa: A001
    """Print the help screen (legacy helper)."""
    print(help_text(program, version))
