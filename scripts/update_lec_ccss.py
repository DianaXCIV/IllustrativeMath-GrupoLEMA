#!/usr/bin/env python3
"""
update_lec_ccss.py
==================

Utility script to update the CCSS list inside a ``lec-*.ptx`` lesson file.
It inspects the warm-up and activity files that the lesson includes and
collects their Addressing and Building standards.  The script then replaces
the lesson's CCSS paragraph with the aggregated list so that the lesson
summary accurately reflects its content.

Usage
-----

    python SCRIPTS/update_lec_ccss.py source/content/lec-explicarConteo.ptx
"""

from __future__ import annotations

import argparse
import re
from collections import OrderedDict
from pathlib import Path
from typing import Iterable, Sequence


# --------------------------------------------------------------------------- #
# Regular-expression helpers
# --------------------------------------------------------------------------- #

# The CCSS block we will replace inside the lesson.  Teacher notes are a <dl>,
# so the block is the comment plus the <li> that follows it.  Only the opening
# is matched here; the closing tag is located by tag depth, since the codes may
# sit in a <ul> whose items are interleaved with comments.
CCSS_BLOCK_OPEN = re.compile(r"<!-- Estándares CCSS asociados -->\s*<li\b[^>]*>")
LI_OPEN_RE = re.compile(r"<li\b[^>]*>")


def find_matching_li_end(text: str, start: int) -> int | None:
    """Index just past the </li> closing the <li> that begins at ``start``."""
    depth = 0
    i = start
    while i < len(text):
        if text.startswith("<!--", i):
            close = text.find("-->", i)
            i = len(text) if close == -1 else close + 3
        elif text.startswith("</li>", i):
            depth -= 1
            i += len("</li>")
            if depth == 0:
                return i
        elif m := LI_OPEN_RE.match(text, i):
            depth += 1
            i = m.end()
        else:
            i += 1
    return None


def find_ccss_block(text: str) -> tuple[int, int] | None:
    """(start, end) of the CCSS comment plus its <li>, or None if absent."""
    m = CCSS_BLOCK_OPEN.search(text)
    if not m:
        return None
    li_at = text.index("<li", m.start())
    end = find_matching_li_end(text, li_at)
    return None if end is None else (m.start(), end)

# ``<xi:include href="./filename.ptx"/>`` tags inside the lesson.
INCLUDE_PATTERN = re.compile(r'<xi:include href="\./([^"]+)"')

# Addressing / Building list items inside a warm-up or activity.
ADDRESSING_PATTERN = re.compile(
    r'<li>\s*<custom ref="ccss-addressing"\s*/>(.*?)</li>', re.S
)
# ``Building On`` and ``Building Towards`` categories.
BUILDING_ON_PATTERN = re.compile(
    r'<li>\s*<custom ref="ccss-buildingOn"\s*/>(.*?)</li>', re.S
)
BUILDING_TOWARDS_PATTERN = re.compile(
    r'<li>\s*<custom ref="ccss-buildingTowards"\s*/>(.*?)</li>', re.S
)

# ``<xref ...>K.CC.B.4</xref>`` tags from which we extract the actual codes.
XREF_PATTERN = re.compile(r'<xref[^>]*>([^<]+)</xref>')

# XML comments to strip before pattern matching.
XML_COMMENT_PATTERN = re.compile(r'<!--.*?-->', re.S)

# Some lesson includes (materials, center wrappers, etc.) do not contain CCSS
# metadata.  We skip them entirely to avoid polluting the results.
SKIP_SUFFIXES = ("-mat.ptx", "-matCentros.ptx", "-reto.ptx")
SKIP_PREFIXES = ("cool-", "PP-")
SKIP_CONTAINS = ("centros-escoger",)


# --------------------------------------------------------------------------- #
# Helper utilities
# --------------------------------------------------------------------------- #

def parse_args() -> Path:
    """Parse the command-line arguments and return the lesson path."""
    parser = argparse.ArgumentParser(
        description="Update the CCSS section in a lec-*.ptx file."
    )
    parser.add_argument("lesson", type=Path, help="Path to the lec-*.ptx file.")
    args = parser.parse_args()

    if not args.lesson.exists():
        raise FileNotFoundError(f"Lesson file not found: {args.lesson}")
    return args.lesson


def unique(seq: Iterable[str]) -> list[str]:
    """Return values in *seq* preserving their first occurrence."""
    ordered: OrderedDict[str, None] = OrderedDict()
    for value in seq:
        clean = value.strip()
        if clean and clean not in ordered:
            ordered[clean] = None
    return list(ordered.keys())


def gather_codes(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Extract CCSS codes from warm-up/activity text using *pattern*."""
    codes: list[str] = []
    for block in pattern.findall(text):
        codes.extend(XREF_PATTERN.findall(block))
    return codes


def code_to_ref(code: str) -> str:
    """
    Convert a CCSS code (e.g. ``K.CC.B.4``) to the ``ccss-...`` reference id
    used within the project.
    """
    cleaned = code.replace("\u2013", "-").replace("–", "-").replace(" ", "")
    return "ccss-" + cleaned.replace(".", "-").replace("/", "-")


def should_skip(filename: str) -> bool:
    """Return True if *filename* should not be considered for CCSS data."""
    if filename.endswith(SKIP_SUFFIXES):
        return True
    if filename.startswith(SKIP_PREFIXES):
        return True
    return any(token in filename for token in SKIP_CONTAINS)


def collect_included_files(lesson_text: str) -> list[str]:
    """Return the list of included warm-up/activity filenames to inspect."""
    includes = []
    for fname in INCLUDE_PATTERN.findall(lesson_text):
        if should_skip(fname):
            continue
        includes.append(fname)
    return includes


def collect_codes_from_includes(
    base_dir: Path, filenames: Sequence[str]
) -> tuple[list[str], list[str], list[str]]:
    """
    Read each include in *filenames* (relative to *base_dir*) and collect the
    Addressing, Building On, and Building Towards codes present in their CCSS blocks.
    """
    addressing: list[str] = []
    building_on: list[str] = []
    building_towards: list[str] = []

    for fname in filenames:
        include_path = base_dir / fname
        if not include_path.exists():
            print(f"Warning: included file not found (skipping) -> {include_path}")
            continue

        text = XML_COMMENT_PATTERN.sub('', include_path.read_text(encoding="utf-8"))
        addressing.extend(gather_codes(text, ADDRESSING_PATTERN))
        building_on.extend(gather_codes(text, BUILDING_ON_PATTERN))
        building_towards.extend(gather_codes(text, BUILDING_TOWARDS_PATTERN))

    return unique(addressing), unique(building_on), unique(building_towards)


def build_ccss_block(
    addressing: list[str], building_on: list[str], building_towards: list[str]
) -> str:
    """
    Construct the new CCSS paragraph block using the aggregated Addressing and
    Building lists.
    """
    lines = [
        "<!-- Estándares CCSS asociados -->",
        "  <li>",
        '    <title><custom ref="ccss-leccion-titulo"/></title> ',
        "    <ul>",
    ]

    if addressing:
        joined = ", ".join(
            f'<xref ref="{code_to_ref(code)}" text="custom">{code}</xref>'
            for code in addressing
        )
        lines.extend(
            [
                "      <li>",
                '        <custom ref="ccss-addressing"/>',
                f"        {joined}",
                "      </li>",
            ]
        )

    if building_on:
        joined = ", ".join(
            f'<xref ref="{code_to_ref(code)}" text="custom">{code}</xref>'
            for code in building_on
        )
        lines.extend(
            [
                "      <li>",
                '        <custom ref="ccss-buildingOn"/>',
                f"        {joined}",
                "      </li>",
            ]
        )

    if building_towards:
        joined = ", ".join(
            f'<xref ref="{code_to_ref(code)}" text="custom">{code}</xref>'
            for code in building_towards
        )
        lines.extend(
            [
                "      <li>",
                '        <custom ref="ccss-buildingTowards"/>',
                f"        {joined}",
                "      </li>",
            ]
        )

    lines.extend(["    </ul>", "  </li>"])
    return "\n".join(lines)


def update_lesson_ccss(lesson_path: Path) -> None:
    """
    Aggregate CCSS codes from the lesson's includes and replace the lesson's
    CCSS paragraph with the updated content.
    """
    lesson_text = lesson_path.read_text(encoding="utf-8")
    base_dir = lesson_path.parent

    include_files = collect_included_files(lesson_text)
    addressing, building_on, building_towards = collect_codes_from_includes(
        base_dir, include_files
    )

    if not (addressing or building_on or building_towards):
        print("No CCSS codes found in included files; nothing to update.")
        return

    new_block = build_ccss_block(addressing, building_on, building_towards)

    span = find_ccss_block(lesson_text)
    if span is not None:
        start, end = span
        updated_text = lesson_text[:start] + new_block + lesson_text[end:]
    else:
        # The lesson has no CCSS block yet.  Anchor on the <dl> that follows the
        # introduction, so the new item lands inside the teacher-notes list.
        intro_tag = '<introduction component="profesor">'
        intro_at = lesson_text.find(intro_tag)
        if intro_at == -1:
            raise ValueError(
                "Could not find existing CCSS block or introduction tag to insert into."
            )
        dl_match = re.compile(r"<dl>\n").search(lesson_text, intro_at)
        if dl_match:
            at = dl_match.end()
            updated_text = lesson_text[:at] + new_block + "\n" + lesson_text[at:]
        else:
            # No teacher-notes list yet: create the wrapper around the block.
            at = intro_at + len(intro_tag)
            updated_text = (
                lesson_text[:at]
                + f"\n<p>\n<dl>\n{new_block}\n</dl>\n</p>"
                + lesson_text[at:]
            )

    lesson_path.write_text(updated_text, encoding="utf-8")

    # Provide a concise summary for the caller.
    print(f"Updated CCSS block in {lesson_path}")
    if addressing:
        print("  Addressing:", ", ".join(addressing))
    if building_on:
        print("  Building On:", ", ".join(building_on))
    if building_towards:
        print("  Building Towards:", ", ".join(building_towards))


def main() -> None:
    lesson_path = parse_args()
    update_lesson_ccss(lesson_path)


if __name__ == "__main__":
    main()
