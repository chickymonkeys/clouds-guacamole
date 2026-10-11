"""Never-synced patterns: the global filters.txt, per-folder overrides, and matching them.

filters.txt stays the source of the global list, in rclone filter syntax, so it can still be
edited by hand. The widget only manages its exclude rules (`- pattern` lines): comments, include
rules and anything else in the file are kept as they are. A folder may instead have its own
list of patterns, which replaces the global file for that folder; an empty list means global.
"""

import hashlib
import re
from collections.abc import Iterable

MAX_PATTERNS = 500
MAX_PATTERN_LENGTH = 300

OVERRIDE_HEADER = """\
# Written by Guacamole: this folder's own never-synced patterns, used instead of filters.txt.
# Change them in the widget (the folder's 󰈲 action) or with `guac exclude`.
"""


def glob_to_regex(pattern: str) -> str:
    """rclone's glob syntax as a regular expression over a slash-separated relative path.

    A leading / anchors to the folder's top, otherwise the pattern matches the end of the path
    after any /. `*` and `?` stop at /, `**` does not, `[...]` and `{a,b}` work as in rclone.
    Raises ValueError for patterns rclone would refuse.
    """
    out = []
    if pattern.startswith("/"):
        out.append("^")
        pattern = pattern[1:]
    else:
        out.append("(^|/)")
    i, n, braces = 0, len(pattern), 0
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 >= n:
                raise ValueError("a pattern can't end with \\")
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if c == "*":
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = pattern.find(
                "]",
                i + 2
                if pattern.startswith("[!", i) or pattern.startswith("[^", i)
                else i + 1,
            )
            if end < 0:
                raise ValueError("unclosed [ in pattern")
            body = pattern[i + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
            continue
        elif c == "{":
            if pattern.startswith("{{", i):
                raise ValueError("regular expressions ({{...}}) are not supported here")
            braces += 1
            out.append("(")
        elif c == "}" and braces:
            braces -= 1
            out.append(")")
        elif c == "," and braces:
            out.append("|")
        else:
            out.append(re.escape(c))
        i += 1
    if braces:
        raise ValueError("unclosed { in pattern")
    out.append("$")
    return "".join(out)


def clean_pattern(value: object) -> str:
    text = str(value if value is not None else "").strip()
    if text.startswith("- "):
        text = text[2:].strip()
    if not text:
        raise ValueError("A pattern can't be empty")
    if "\n" in text or "\r" in text:
        raise ValueError("One pattern per line")
    if text.startswith("+ "):
        raise ValueError(
            "Only never-sync patterns can be set here; edit filters.txt for include rules"
        )
    if len(text) > MAX_PATTERN_LENGTH:
        raise ValueError("That pattern is too long")
    try:
        re.compile(glob_to_regex(text))
    except (ValueError, re.error) as e:
        raise ValueError(f"'{text}' is not a valid pattern: {e}") from None
    return text


def clean_patterns(values: Iterable[object]) -> list[str]:
    out: list[str] = []
    for v in values:
        p = clean_pattern(v)
        if p not in out:
            out.append(p)
    if len(out) > MAX_PATTERNS:
        raise ValueError(f"At most {MAX_PATTERNS} patterns")
    return out


def _rule(line: str) -> tuple[str, str] | None:
    """(sign, pattern) of a filter rule line, None for comments, blanks and anything else."""
    s = line.strip()
    if len(s) >= 2 and s[0] in "+-" and s[1] == " ":
        return s[0], s[2:].strip()
    return None


def excludes_of(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        r = _rule(line)
        if r and r[0] == "-" and r[1] and r[1] not in out:
            out.append(r[1])
    return out


def other_rules(text: str) -> int:
    """Rules in the file the widget doesn't manage (includes, resets, flags)."""
    n = 0
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        r = _rule(s)
        if r is None or r[0] == "+":
            n += 1
    return n


def with_excludes(text: str, patterns: list[str]) -> str:
    """text with its exclude rules made exactly `patterns`: removed ones are dropped in place,
    the rest of the file is untouched, and new ones are appended."""
    keep = set(patterns)
    present: set[str] = set()
    lines = []
    for line in text.splitlines():
        r = _rule(line)
        if r and r[0] == "-":
            if r[1] not in keep or r[1] in present:
                continue
            present.add(r[1])
        lines.append(line)
    lines += [f"- {p}" for p in patterns if p not in present]
    return "\n".join(lines) + "\n"


def override_text(patterns: list[str]) -> str:
    return OVERRIDE_HEADER + "".join(f"- {p}\n" for p in patterns)


def digest(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class Matcher:
    """Whether a path in a folder is never synced, by the folder's filter rules.

    Rules apply first match first, as in rclone; a path inside a never-synced directory is
    never synced either. Only used to skip file events, so anything it can't read (regex
    rules, flags) counts as synced: a missed skip costs a no-op sync, a wrong one a late sync.
    """

    def __init__(self, text: str) -> None:
        self.rules: list[tuple[bool, re.Pattern[str]]] = []
        for line in text.splitlines():
            s = line.strip()
            if s == "!":
                self.rules = []
                continue
            r = _rule(s)
            if r is None or not r[1]:
                continue
            try:
                self.rules.append((r[0] == "-", re.compile(glob_to_regex(r[1]))))
            except (ValueError, re.error):
                # An unreadable rule may include what a later one excludes: stop here
                break

    def _excluded(self, path: str) -> bool:
        for exclude, rx in self.rules:
            if rx.search(path):
                return exclude
        return False

    def ignored(self, rel: str, is_dir: bool = False) -> bool:
        if not self.rules or not rel or rel == ".":
            return False
        parts = rel.split("/")
        for i in range(1, len(parts)):
            if self._excluded("/".join(parts[:i]) + "/"):
                return True
        return self._excluded(rel + "/" if is_dir else rel)
