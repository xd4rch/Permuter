# Contributing

Thanks for improving Permuter. This project is intentionally small,
dependency-free and deterministic; please keep it that way.

## Ground rules

- **No third-party runtime dependencies.** The engine and CLI must run on a
  stock Python 3.9+ installation.
- **Deterministic output.** UTF-8, LF line endings, lexicographic radix order.
- **O(1) memory.** New features must not materialise the `N^L` space.
- **Add a test for every behaviour change.** The suite is the contract.

## Development

```bash
python -m unittest discover -s tests -v
python -m pytest tests -q          # optional, same suite
python benchmark.py --quick --no-color
python launcher.py -h
```

The benchmark's classical comparison uses a real process memory cap
(`RLIMIT_AS` on POSIX, a Job Object on Windows); it is expected to report
`MemoryError` for the list-based approach.

## Pull requests

1. Keep the diff focused.
2. Update `README.md` (English **and** Turkish) if user-facing behaviour changes.
3. Run the full suite on at least one platform and state which one.
4. Make sure `git diff --check` is clean.

## Reporting bugs

Include the exact command, the expected words, the observed words, your OS and
`python --version`. For memory issues, include the output of `python benchmark.py`.
