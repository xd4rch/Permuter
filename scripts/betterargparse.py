"""Strict, dependency-free command line parsing for Permuter.

The original helper was a positional token juggler with three design bugs:
option aliases could not share a destination, a forgotten ``raise`` made an
invalid option silently pass through and multi-value options were hard-wired
to "everything after this flag".  This module replaces it with a small but
predictable parser:

* every alias maps to one canonical destination (``-f``/``--file`` and
  ``-o``/``--out-file`` all end up as ``"output"``),
* flags, single-value options and greedy multi-value options are explicit,
* ``--option=value`` works for every option,
* unknown, duplicated and valueless options raise :class:`ParameterError`
  with a human readable message, and the launcher maps that to exit code 2.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Iterable, Sequence


class ParameterError(ValueError):
    """Raised for invalid command line input (mapped to exit code 2)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Option:
    """Description of a single command line option.

    ``kind`` is one of:

    ``"flag"``
        Boolean switch, takes no value.
    ``"value"``
        Takes exactly one value (``-l 8`` or ``-l=8``).
    ``"multi"``
        Takes one or more values up to the next known option
        (``-s a b c`` for a multi-character alphabet).
    """

    name: str
    flags: tuple[str, ...]
    kind: str = "value"
    metavar: str = "VALUE"

    def __post_init__(self) -> None:
        if self.kind not in ("flag", "value", "multi"):
            raise ValueError(f"invalid option kind: {self.kind!r}")
        if not self.flags:
            raise ValueError("an option needs at least one flag")


def build_lookup(options: Iterable[Option]) -> dict[str, Option]:
    lookup: dict[str, Option] = {}
    for option in options:
        for flag in option.flags:
            if flag in lookup:
                raise ValueError(f"duplicate option flag: {flag}")
            lookup[flag] = option
    return lookup


def parse_params(options: Sequence[Option], argv: Sequence[str]) -> dict[str, object]:
    """Parse ``argv`` (without the program name) according to ``options``.

    Returns a dictionary keyed by :attr:`Option.name`.  Repeated options,
    unknown flags, missing values and stray positional arguments all raise
    :class:`ParameterError`.
    """
    lookup = build_lookup(options)
    result: dict[str, object] = {}
    index = 0
    argv = list(argv)

    while index < len(argv):
        token = argv[index]

        if token == "--":
            if index + 1 < len(argv):
                raise ParameterError(f"Unexpected argument: {argv[index + 1]}")
            break

        if not token.startswith("-") or token == "-":
            raise ParameterError(f"Unexpected argument: {token}")

        # Support --option=value and -o=value for every kind that takes a value.
        inline: str | None = None
        flag = token
        if "=" in token:
            candidate, inline = token.split("=", 1)
            flag = candidate
            if candidate not in lookup:
                raise ParameterError(f"Unknown option: {candidate}")

        option = lookup.get(flag)
        if option is None:
            raise ParameterError(f"Unknown option: {flag}")

        if option.name in result:
            raise ParameterError(f"Option {flag} was given more than once")

        if option.kind == "flag":
            if inline is not None:
                raise ParameterError(f"Option {flag} does not take a value")
            result[option.name] = True
            index += 1
            continue

        if option.kind == "value":
            if inline is not None:
                value = inline
                index += 1
            else:
                if index + 1 >= len(argv):
                    raise ParameterError(f"Missing value for {flag} ({option.metavar})")
                value = argv[index + 1]
                if value.startswith("-") and value != "-":
                    raise ParameterError(f"Missing value for {flag} ({option.metavar})")
                index += 2
            if value == "":
                raise ParameterError(f"Empty value for {flag}")
            result[option.name] = value
            continue

        # Greedy multi-value option.
        values: list[str] = []
        if inline is not None:
            if inline == "":
                raise ParameterError(f"Empty value for {flag}")
            values.append(inline)
            index += 1
        else:
            cursor = index + 1
            while cursor < len(argv):
                candidate = argv[cursor]
                if candidate.startswith("-") and candidate != "-":
                    break
                values.append(candidate)
                cursor += 1
            if not values:
                raise ParameterError(f"Missing value for {flag} ({option.metavar})")
            index = cursor
        result[option.name] = values

    return result


def parse_int(
    raw: object,
    option: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Strictly parse ``raw`` as a base-10 integer within optional bounds."""
    try:
        value = int(str(raw), 10)
    except (TypeError, ValueError):
        raise ParameterError(f"{option} expects an integer, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ParameterError(f"{option} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ParameterError(f"{option} must be <= {maximum}, got {value}")
    return value


def handle_exceptions(function, *args, **kwargs):
    """Legacy adapter: turn :class:`ParameterError` into a message string.

    Kept for backwards compatibility with the previous launcher API.  New code
    should let the exception propagate and decide on an exit code.
    """
    try:
        return function(*args, **kwargs)
    except ParameterError as error:
        return error.message


def get_input(
    *,
    prompt: str,
    type: str = "str",
    min: int | None = None,
    max: int | None = None,
    split: bool = False,
    default=None,
    empty_err: str | None = None,
    type_err: str | None = None,
    range_err: str | None = None,
):
    """Prompt until a valid value is entered.

    ``type="int"`` validates and converts, ``split=True`` returns a list of
    whitespace separated tokens.  ``EOFError`` (closed stdin) is converted to
    :class:`ParameterError` so batch runners never hang.
    """
    if type not in ("str", "int"):
        raise ValueError(f"unsupported input type: {type!r}")

    while True:
        try:
            raw = input(prompt)
        except EOFError:
            raise ParameterError("Input stream closed before a value was provided.") from None

        raw = raw.strip()
        if not raw:
            if default is not None:
                return default
            print(empty_err or "Please enter a value.", file=sys.stderr)
            continue

        if type == "int":
            try:
                value = int(raw, 10)
            except ValueError:
                print(type_err or "Expected an integer.", file=sys.stderr)
                continue
            if min is not None and value < min:
                print(range_err or f"Value must be >= {min}.", file=sys.stderr)
                continue
            if max is not None and value > max:
                print(range_err or f"Value must be <= {max}.", file=sys.stderr)
                continue
            return value

        if split:
            return raw.split()
        return raw
