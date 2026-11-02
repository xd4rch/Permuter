"""Comprehensive test suite for AdvancedPermuter.

Run with either of:

    python -m unittest discover -s tests -v
    python -m pytest tests -q
"""

from __future__ import annotations

import contextlib
import io
import itertools
import os
import subprocess
import sys
import tempfile
import tracemalloc
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import algorithm as engine  # noqa: E402
from scripts.betterargparse import Option, ParameterError, parse_params  # noqa: E402

PYTHON = sys.executable
ENV = dict(os.environ, NO_COLOR="1", PYTHONIOENCODING="utf-8")


def run_cli(args, *, cwd=None, timeout=180):
    return subprocess.run(
        [PYTHON, str(ROOT / "launcher.py"), *args],
        cwd=str(cwd or ROOT),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=ENV,
        timeout=timeout,
    )


def read_lines(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read().splitlines()


class FormatBytesTests(unittest.TestCase):
    def test_bytes_below_base(self):
        self.assertEqual(engine.format_bytes(0), "0 B")
        self.assertEqual(engine.format_bytes(1), "1 B")
        self.assertEqual(engine.format_bytes(999), "999 B")
        self.assertEqual(engine.format_bytes(1023, "IEC"), "1023 B")

    def test_si_scaling_is_exact(self):
        self.assertEqual(engine.format_bytes(1000), "1 KB")
        self.assertEqual(engine.format_bytes(1536), "1.536 KB")
        self.assertEqual(engine.format_bytes(1_000_000), "1 MB")
        self.assertEqual(engine.format_bytes(1_500_000), "1.5 MB")
        self.assertEqual(engine.format_bytes(10**18), "1000 PB")

    def test_iec_scaling_is_exact(self):
        self.assertEqual(engine.format_bytes(1024, "IEC"), "1 KiB")
        self.assertEqual(engine.format_bytes(1536, "IEC"), "1.5 KiB")
        self.assertEqual(engine.format_bytes(1_048_576, "IEC"), "1 MiB")
        self.assertEqual(engine.format_bytes(10**18, "IEC"), "888.178 PiB")

    def test_pair_returns_both_systems(self):
        self.assertEqual(
            engine.format_bytes_pair(1536), ("1.536 KB", "1.5 KiB")
        )

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            engine.format_bytes(-1)
        with self.assertRaises(TypeError):
            engine.format_bytes(1.5)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            engine.format_bytes(10, "banana")
        with self.assertRaises(ValueError):
            engine.format_bytes(10, "SI", precision=-1)

    def test_engine_has_no_matplotlib_dependency(self):
        self.assertNotIn("matplotlib", Path(engine.__file__).read_text(encoding="utf-8"))
        probe = subprocess.run(
            [
                PYTHON,
                "-c",
                "import scripts.algorithm, sys; "
                "assert 'matplotlib' not in sys.modules, 'matplotlib imported'; "
                "print('ok')",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=ENV,
            timeout=60,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("ok", probe.stdout)


class UnrankTests(unittest.TestCase):
    def test_first_last_and_boundaries(self):
        self.assertEqual(engine.unrank(0, 3, "abc"), "aaa")
        self.assertEqual(engine.unrank(9, 3, "abc"), "baa")
        self.assertEqual(engine.unrank(26, 3, "abc"), "ccc")
        self.assertEqual(engine.unrank(0, 1, "xy"), "x")
        self.assertEqual(engine.unrank(1, 1, "xy"), "y")

    def test_full_space_matches_itertools(self):
        alphabet = "abc"
        expected = ["".join(chars) for chars in itertools.product(alphabet, repeat=4)]
        produced = [engine.unrank(index, 4, alphabet) for index in range(len(expected))]
        self.assertEqual(produced, expected)

    def test_multi_character_symbols(self):
        alphabet = ["aa", "b"]
        expected = ["".join(chars) for chars in itertools.product(alphabet, repeat=3)]
        produced = [engine.unrank(index, 3, alphabet) for index in range(8)]
        self.assertEqual(produced, expected)
        self.assertEqual(engine.unrank(3, 3, alphabet), "aabb")

    def test_multi_word_alphabet(self):
        alphabet = ["deneme", "test", "123"]
        expected = ["".join(parts) for parts in itertools.product(alphabet, repeat=2)]
        self.assertEqual(
            [engine.unrank(index, 2, alphabet) for index in range(9)], expected
        )
        self.assertIn("denemetest", expected)
        self.assertIn("test123", expected)

    def test_zero_symbol_padding_is_fixed_length(self):
        self.assertEqual(engine.unrank(5, 6, ["xy", "z"]), "xyxyxyzxyz")
        self.assertEqual(len(engine.unrank(5, 6, ["xy", "z"])), 10)

    def test_unicode_alphabet(self):
        alphabet = ["ç", "ğ", "ş"]
        expected = ["".join(chars) for chars in itertools.product(alphabet, repeat=2)]
        produced = [engine.unrank(index, 2, alphabet) for index in range(9)]
        self.assertEqual(produced, expected)

    def test_generate_word_is_alias(self):
        for index in (0, 1, 7, 63):
            self.assertEqual(
                engine.generate_word(index, 3, "abcd"),
                engine.unrank(index, 3, "abcd"),
            )

    def test_out_of_range_index(self):
        with self.assertRaises(IndexError):
            engine.unrank(-1, 3, "ab")
        with self.assertRaises(IndexError):
            engine.unrank(8, 3, "ab")
        with self.assertRaises(IndexError):
            engine.unrank(10**18, 3, "ab")

    def test_invalid_arguments(self):
        with self.assertRaises(ValueError):
            engine.unrank(0, 0, "ab")
        with self.assertRaises(ValueError):
            engine.unrank(0, 2, "")
        with self.assertRaises(TypeError):
            engine.unrank("0", 2, "ab")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            engine.unrank(0.0, 2, "ab")  # type: ignore[arg-type]


class StreamRangeTests(unittest.TestCase):
    def test_returns_generator(self):
        stream = engine.stream_range(3, "ab")
        self.assertIsInstance(stream, types.GeneratorType)
        self.assertEqual(next(stream), "aaa")

    def test_full_range(self):
        self.assertEqual(
            list(engine.stream_range(3, "abc")),
            [engine.unrank(index, 3, "abc") for index in range(27)],
        )

    def test_slice(self):
        full = list(engine.stream_range(4, "abc"))
        self.assertEqual(list(engine.stream_range(4, "abc", 5, 17)), full[5:17])
        self.assertEqual(list(engine.stream_range(4, "abc", 0, 0)), [])
        self.assertEqual(list(engine.stream_range(4, "abc", 81, 81)), [])

    def test_arbitrary_splits_cover_the_space_once(self):
        total = 3**5
        shards = [(0, 17), (17, 50), (50, 120), (120, 200), (200, 243)]
        combined = []
        for begin, end in shards:
            combined.extend(engine.stream_range(5, "abc", begin, end))
        self.assertEqual(len(combined), total)
        self.assertEqual(len(set(combined)), total)
        self.assertEqual(sorted(combined), list(engine.stream_range(5, "abc")))

    def test_lazy_over_astronomical_range(self):
        # 2^60 > 10^18: building a list would be impossible, islice is instant.
        first = list(itertools.islice(engine.stream_range(60, "ab", 0, 10**18), 3))
        self.assertEqual(first, ["a" * 60, "a" * 59 + "b", "a" * 58 + "ba"])

    def test_invalid_range_is_eager(self):
        with self.assertRaises(ValueError):
            engine.stream_range(2, "ab", -1, 4)
        with self.assertRaises(ValueError):
            engine.stream_range(2, "ab", 0, 5)
        with self.assertRaises(ValueError):
            engine.stream_range(2, "ab", 3, 2)
        with self.assertRaises(ValueError):
            engine.stream_range(2, "")
        with self.assertRaises(ValueError):
            engine.stream_range(2, ["a", "a"])

    def test_multi_character_symbols(self):
        self.assertEqual(
            list(engine.stream_range(2, ["aa", "b"], 1, 4)),
            ["aab", "baa", "bb"],
        )


class PlanPartsTests(unittest.TestCase):
    def test_balanced_distribution(self):
        spans = engine.plan_parts(0, 100, 7)
        self.assertEqual(len(spans), 7)
        self.assertEqual(sum(span.count for span in spans), 100)
        sizes = [span.count for span in spans]
        self.assertLessEqual(max(sizes) - min(sizes), 1)
        self.assertEqual(spans[0].begin, 0)
        self.assertEqual(spans[-1].end, 100)
        for left, right in zip(spans, spans[1:]):
            self.assertEqual(left.end, right.begin)

    def test_more_parts_than_items(self):
        spans = engine.plan_parts(0, 3, 10)
        self.assertEqual(len(spans), 3)
        self.assertTrue(all(span.count == 1 for span in spans))

    def test_offset_range(self):
        spans = engine.plan_parts(50, 55, 2)
        self.assertEqual(spans[0].begin, 50)
        self.assertEqual(spans[-1].end, 55)
        self.assertEqual(sum(span.count for span in spans), 5)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            engine.plan_parts(0, 0, 1)
        with self.assertRaises(ValueError):
            engine.plan_parts(0, 10, 0)
        with self.assertRaises(ValueError):
            engine.plan_parts(10, 5, 2)


class OutputTests(unittest.TestCase):
    def test_extension_defaults_to_txt(self):
        self.assertEqual(engine.split_output_name("dataset"), ("dataset", ".txt"))

    def test_existing_extension_is_kept(self):
        self.assertEqual(engine.split_output_name("dataset.csv"), ("dataset", ".csv"))

    def test_trailing_dot(self):
        self.assertEqual(engine.split_output_name("dataset."), ("dataset", ".txt"))

    def test_default_directory(self):
        target = engine.plan_output("dataset")
        self.assertEqual(target.directory, engine.DEFAULT_OUTPUT_DIR)
        self.assertTrue(target.path_for(3).endswith(os.path.join("dataset_3.txt")))

    def test_explicit_directory(self):
        target = engine.plan_output(os.path.join("out", "dataset"))
        self.assertEqual(target.directory, "out")
        self.assertEqual(target.stem, "dataset")

    def test_invalid_names(self):
        with self.assertRaises(ValueError):
            engine.split_output_name("")
        with self.assertRaises(ValueError):
            engine.split_output_name("   ")


class WriteRangeTests(unittest.TestCase):
    def _write(self, **kwargs):
        buffer = io.BytesIO()
        stats = engine.write_range(
            kwargs.pop("length", 4),
            kwargs.pop("alphabet", "abcd"),
            kwargs.pop("begin", 0),
            kwargs.pop("end", 256),
            buffer,
            **kwargs,
        )
        return buffer.getvalue(), stats

    def test_byte_exact_output(self):
        payload, stats = self._write()
        self.assertEqual(stats.count, 256)
        self.assertEqual(stats.bytes_written, len(payload))
        self.assertEqual(payload, b"".join(
            (engine.unrank(index, 4, "abcd") + "\n").encode() for index in range(256)
        ))
        self.assertTrue(payload.endswith(b"dddd\n"))
        self.assertEqual(payload.count(b"\n"), 256)

    def test_engines_are_identical(self):
        odometer, _ = self._write(engine="odometer")
        divmod_output, _ = self._write(engine="divmod")
        self.assertEqual(odometer, divmod_output)

    def test_progress_callback(self):
        calls = []

        def progress(done, total, elapsed, written):
            calls.append((done, total, written))

        payload, stats = self._write(buffer_size=64, progress=progress)
        self.assertTrue(calls)
        self.assertEqual(calls[-1][0], 256)
        self.assertEqual(calls[-1][1], 256)
        self.assertEqual(calls[-1][2], len(payload))

    def test_stop_check(self):
        payload, stats = self._write(buffer_size=16, should_stop=lambda: True)
        self.assertTrue(stats.aborted)
        self.assertLess(stats.count, 256)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(payload.count(b"\n"), stats.count)

    def test_unicode_and_multichar(self):
        buffer = io.BytesIO()
        stats = engine.write_range(2, ["ç", "ğ"], 0, 4, buffer)
        self.assertEqual(buffer.getvalue().decode("utf-8").splitlines(), ["çç", "çğ", "ğç", "ğğ"])
        self.assertEqual(stats.bytes_written, len(buffer.getvalue()))

    def test_invalid_ranges(self):
        with self.assertRaises(ValueError):
            engine.write_range(2, "ab", 0, 5, io.BytesIO())
        with self.assertRaises(ValueError):
            engine.write_range(2, "ab", 3, 2, io.BytesIO())
        with self.assertRaises(ValueError):
            engine.write_range(2, "ab", 0, 4, io.BytesIO(), engine="nope")
        with self.assertRaises(TypeError):
            engine.write_range(2, "ab", 0, 4, None)  # type: ignore[arg-type]


class MakeListTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_file_without_extension(self):
        output = self.tmp / "dataset"
        results = engine.make_list(3, "abc", str(output))
        self.assertEqual(len(results), 1)
        path = Path(results[0].path)
        self.assertEqual(path.suffix, ".txt")
        self.assertEqual(path, self.tmp / "dataset_1.txt")
        self.assertEqual(read_lines(path), [engine.unrank(i, 3, "abc") for i in range(27)])
        self.assertEqual(path.stat().st_size, 27 * 4)

    def test_write_mode_truncates_instead_of_appending(self):
        output = self.tmp / "data"
        engine.make_list(3, "abc", str(output))
        engine.make_list(3, "abc", str(output))
        self.assertEqual(len(read_lines(output.with_suffix(".txt").parent / "data_1.txt")), 27)

    def test_balanced_parts_have_no_empty_files(self):
        output = self.tmp / "words"
        results = engine.make_list(4, "abcd", str(output), multi_files=7)
        self.assertEqual(len(results), 7)
        paths = [Path(result.path) for result in results]
        self.assertTrue(all(path.exists() for path in paths))
        counts = [len(read_lines(path)) for path in paths]
        self.assertEqual(sum(counts), 256)
        self.assertLessEqual(max(counts) - min(counts), 1)
        combined = []
        for path in paths:
            combined.extend(read_lines(path))
        self.assertEqual(len(combined), 256)
        self.assertEqual(len(set(combined)), 256)
        self.assertEqual(combined, [engine.unrank(i, 4, "abcd") for i in range(256)])

    def test_more_parts_than_items(self):
        output = self.tmp / "tiny"
        results = engine.make_list(3, "ab", str(output), multi_files=20)
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(len(read_lines(result.path)) for result in results), 8)

    def test_slice(self):
        output = self.tmp / "slice"
        results = engine.make_list(4, "abc", str(output), begin=5, end=17)
        self.assertEqual(read_lines(results[0].path), [
            engine.unrank(index, 4, "abc") for index in range(5, 17)
        ])

    def test_divmod_engine_matches_odometer(self):
        first = self.tmp / "odo"
        second = self.tmp / "div"
        engine.make_list(4, "abc", str(first))
        engine.make_list(4, "abc", str(second), engine="divmod")
        self.assertEqual(
            Path(first.parent, "odo_1.txt").read_bytes(),
            Path(second.parent, "div_1.txt").read_bytes(),
        )

    def test_workers_match_sequential(self):
        sequential = self.tmp / "seq"
        parallel = self.tmp / "par"
        engine.make_list(4, "abc", str(sequential), multi_files=4)
        engine.make_list(4, "abc", str(parallel), multi_files=4, workers=2)
        seq_lines = []
        par_lines = []
        for part in range(1, 5):
            seq_lines.extend(read_lines(self.tmp / f"seq_{part}.txt"))
            par_lines.extend(read_lines(self.tmp / f"par_{part}.txt"))
        self.assertEqual(seq_lines, par_lines)
        self.assertEqual(len(par_lines), 81)

    def test_on_part_callback(self):
        seen = []
        engine.make_list(4, "abc", str(self.tmp / "cb"), multi_files=3, on_part=seen.append)
        self.assertEqual([result.index for result in seen], [1, 2, 3])

    def test_unicode_output_is_utf8(self):
        output = self.tmp / "unicode"
        results = engine.make_list(2, ["ç", "ğ"], str(output))
        payload = Path(results[0].path).read_bytes()
        self.assertEqual(payload.decode("utf-8").splitlines(), ["çç", "çğ", "ğç", "ğğ"])

    def test_empty_range_rejected(self):
        with self.assertRaises(ValueError):
            engine.make_list(3, "abc", str(self.tmp / "x"), begin=4, end=4)

    def test_memory_stays_bounded(self):
        output = self.tmp / "memory"
        tracemalloc.start()
        try:
            engine.make_list(4, "abcdefghijklmnopqrstuvwxyz", str(output), begin=0, end=200_000)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(engine.BUFFER_SIZE, 64 * 1024)
        self.assertLess(peak, 4 * 1024 * 1024, f"peak memory was {peak} bytes")


class EstimateTests(unittest.TestCase):
    def test_exact_for_uniform_alphabet(self):
        self.assertEqual(engine.estimate_bytes(4, "abcd", 256), 256 * 5)
        self.assertEqual(engine.estimate_bytes(3, ["aa", "bb"], 8), 8 * 7)

    def test_estimate_zero(self):
        self.assertEqual(engine.estimate_bytes(3, "ab", 0), 0)

    def test_calculate_size(self):
        size_si, size_iec = engine.calculate_size("abcd", 4)
        self.assertEqual(size_si, engine.format_bytes(256 * 5))
        self.assertEqual(size_iec, engine.format_bytes(256 * 5, "IEC"))


class ParserTests(unittest.TestCase):
    OPTIONS = (
        Option("length", ("-l", "--length"), "value"),
        Option("output", ("-o", "--out-file", "-f", "--file"), "value"),
        Option("alphabet", ("-s", "--string"), "multi"),
        Option("yes", ("-y", "--yes"), "flag"),
    )

    def test_aliases_share_a_destination(self):
        self.assertEqual(parse_params(self.OPTIONS, ["-o", "a.txt"])["output"], "a.txt")
        self.assertEqual(parse_params(self.OPTIONS, ["-f", "b.txt"])["output"], "b.txt")
        self.assertEqual(
            parse_params(self.OPTIONS, ["--file", "c.txt"])["output"], "c.txt"
        )

    def test_inline_values(self):
        parsed = parse_params(self.OPTIONS, ["-l=8", "--out-file=x.txt"])
        self.assertEqual(parsed["length"], "8")
        self.assertEqual(parsed["output"], "x.txt")

    def test_greedy_multi_stops_at_next_option(self):
        parsed = parse_params(self.OPTIONS, ["-s", "aa", "bb", "-y"])
        self.assertEqual(parsed["alphabet"], ["aa", "bb"])
        self.assertTrue(parsed["yes"])

    def test_errors(self):
        cases = [
            ["--nope"],
            ["-l"],
            ["-l", "-y"],
            ["-l", "1", "-l", "2"],
            ["-o", "-f"],
            ["stray"],
            ["--yes=1"],
            ["-s"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaises(ParameterError):
                parse_params(self.OPTIONS, argv)

    def test_duplicate_destination_is_rejected(self):
        with self.assertRaises(ParameterError):
            parse_params(self.OPTIONS, ["-o", "a", "-f", "b"])


class LegacyCompatibilityTests(unittest.TestCase):
    def test_convert_base(self):
        self.assertEqual(engine.convert_base(0, "abc"), "a")
        self.assertEqual(engine.convert_base(5, "abc"), "bc")
        self.assertEqual(engine.convert_base(26, "abc"), "ccc")
        with self.assertRaises(ValueError):
            engine.convert_base(-1, "abc")

    def test_assets_banner_and_help(self):
        from scripts import assets

        self.assertIn("AdvancedPermuter", assets.banner_text("3.0.0"))
        self.assertIn("USAGE", assets.help_text("launcher.py"))
        with contextlib.redirect_stdout(io.StringIO()):
            assets.banner()
            assets.help()


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_help_and_version(self):
        help_result = run_cli(["-h"])
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("Zero-Allocation", help_result.stdout)
        self.assertIn("--begin", help_result.stdout)
        self.assertIn("--stdout", help_result.stdout)
        self.assertIn("--workers", help_result.stdout)

        version = run_cli(["--version"])
        self.assertEqual(version.returncode, 0)
        self.assertIn("3.0.0", version.stdout)

    def test_quick_mode_writes_file(self):
        output = self.tmp / "quick"
        result = run_cli(["-q", "-l", "3", "-s", "abc", "-o", str(output), "-y"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_lines(self.tmp / "quick_1.txt"), [
            engine.unrank(index, 3, "abc") for index in range(27)
        ])

    def test_file_alias_and_extension(self):
        result = run_cli(["-q", "-l", "2", "-s", "ab", "-f", str(self.tmp / "alias"), "-y"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "alias_1.txt").exists())

    def test_stdout_is_pure_data_and_diskless(self):
        result = run_cli(
            ["-q", "-l", "2", "-s", "ab", "--stdout", "-y"], cwd=self.tmp
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "aa\nab\nba\nbb\n")
        self.assertIn("Streamed 4 words", result.stderr)
        self.assertFalse((self.tmp / engine.DEFAULT_OUTPUT_DIR).exists())

    def test_stdout_slice(self):
        result = run_cli(
            ["-q", "-l", "3", "-s", "abc", "--stdout", "--begin", "2", "--end", "5", "-y"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "aac\naba\nabb\n")

    def test_stdout_has_no_ansi_when_piped(self):
        result = run_cli(["-q", "-l", "1", "-s", "ab", "--stdout", "-y"])
        self.assertNotIn("\x1b[", result.stdout)

    def test_start_end_slice(self):
        output = self.tmp / "slice"
        result = run_cli(
            ["-q", "-l", "4", "-s", "abcd", "-o", str(output),
             "--begin", "100", "--end", "120", "-y"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_lines(self.tmp / "slice_1.txt"), [
            engine.unrank(index, 4, "abcd") for index in range(100, 120)
        ])

    def test_multi_files_complete(self):
        output = self.tmp / "multi"
        result = run_cli(
            ["-q", "-l", "3", "-s", "abc", "-o", str(output), "-m", "5", "-y"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        files = sorted(self.tmp.glob("multi_*.txt"))
        self.assertEqual(len(files), 5)
        lines = []
        for path in files:
            self.assertGreater(path.stat().st_size, 0)
            lines.extend(read_lines(path))
        self.assertEqual(len(lines), 27)
        self.assertEqual(len(set(lines)), 27)

    def test_worker_processes(self):
        output = self.tmp / "workers"
        result = run_cli(
            ["-q", "-l", "3", "-s", "abc", "-o", str(output), "-m", "3", "--workers", "2", "-y"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        files = sorted(self.tmp.glob("workers_*.txt"))
        self.assertEqual(len(files), 3)
        lines = [line for path in files for line in read_lines(path)]
        self.assertEqual(sorted(lines), list(engine.stream_range(3, "abc")))

    def test_multi_word_alphabet_tokens(self):
        output = self.tmp / "words"
        result = run_cli(
            ["-q", "-l", "2", "-s", "deneme", "test", "123", "-o", str(output), "-y"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = [
            "".join(parts)
            for parts in itertools.product(["deneme", "test", "123"], repeat=2)
        ]
        self.assertEqual(read_lines(self.tmp / "words_1.txt"), expected)
        self.assertIn("deneme test 123", result.stdout)

    def test_confirmation_flags(self):
        output = self.tmp / "confirm"
        refused = run_cli(["-q", "-l", "2", "-s", "ab", "-o", str(output), "-p", "n"])
        self.assertEqual(refused.returncode, 0)
        self.assertFalse((self.tmp / "confirm_1.txt").exists())

        accepted = run_cli(["-q", "-l", "2", "-s", "ab", "-o", str(output), "-p", "y"])
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertTrue((self.tmp / "confirm_1.txt").exists())

    def test_non_interactive_without_yes_is_refused(self):
        result = run_cli(["-q", "-l", "2", "-s", "ab", "-o", str(self.tmp / "nope")])
        self.assertEqual(result.returncode, 2)
        self.assertIn("Confirmation required", result.stderr)
        self.assertFalse((self.tmp / "nope_1.txt").exists())

    def test_invalid_arguments(self):
        cases = [
            (["--nope"], "Unknown option"),
            (["-q", "-l", "2", "-s", "ab", "-y"], "Missing required option"),
            (["-q", "-s", "ab", "-o", "x", "-y"], "Missing required option"),
            (["-q", "-l", "2", "-s", "ab", "-o", "x", "-y", "--begin", "9"], "--begin"),
            (["-q", "-l", "2", "-s", "ab", "-o", "x", "-y", "--begin", "3", "--end", "2"], "empty range"),
            (["-q", "-l", "2", "-s", "ab", "-o", "x", "-y", "--end", "5"], "exceeds"),
            (["-q", "-l", "2", "-s", "ab", "-o", "x", "-y", "--end", "2", "-c", "1"], "not both"),
            (["-q", "-l", "2", "-s", "aa", "-o", "x", "-y"], "duplicate"),
            (["-q", "-l", "2", "-s", "ab", "--stdout", "--workers", "2", "-y"], "cannot be combined"),
            (["-q", "-l", "2", "-s", "ab", "-o", "x", "-o", "y", "-y"], "more than once"),
            (["-q", "-l", "2", "-s", "ab", "-o", "x", "-y", "-p", "maybe"], "--proceed"),
        ]
        for argv, needle in cases:
            with self.subTest(argv=argv):
                result = run_cli(argv)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(needle, result.stderr)

    def test_unicode_words_roundtrip(self):
        output = self.tmp / "turkish"
        result = run_cli(["-q", "-l", "2", "-s", "çğş", "-o", str(output), "-y"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            read_lines(self.tmp / "turkish_1.txt"),
            ["çç", "çğ", "çş", "ğç", "ğğ", "ğş", "şç", "şğ", "şş"],
        )

    def test_wizard_rejects_options(self):
        result = run_cli(["-w", "-l", "3"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be combined", result.stderr)

    def test_wizard_eof_is_clean(self):
        result = run_cli(["-w"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("Input stream closed", result.stderr)

    def test_broken_pipe_is_clean(self):
        process = subprocess.Popen(
            [
                PYTHON,
                str(ROOT / "launcher.py"),
                "-q", "-l", "6", "-s", "abcdefghijklmnopqrstuvwxyz",
                "--stdout", "-y",
            ],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=ENV,
        )
        try:
            first = process.stdout.readline()
            self.assertTrue(first)
            process.stdout.close()
            returncode = process.wait(timeout=60)
            stderr = process.stderr.read().decode("utf-8", "replace")
        finally:
            if process.poll() is None:
                process.kill()
            process.stderr.close()
        self.assertEqual(returncode, 0, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("Exception ignored", stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
