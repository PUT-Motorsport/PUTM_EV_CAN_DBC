#!/usr/bin/env python3
"""
PUTM DBC Linter
Validates CAN DBC files for common structural errors.

Checks:
  1.  Duplicate message IDs      — across all files
  2.  Duplicate message names    — across all files
  3.  Signal overflow            — signal bits exceed the declared frame size
  4.  Overlapping signals        — two signals share a bit within one message
  5.  Duplicate signal names     — within a single message
  6.  Zero-length signals        — signal with bit_length == 0
  7.  Message size sanity        — declared DLC > 8 bytes (standard CAN limit)
  8.  Min > Max in value range   — impossible value range declared
  9.  Missing GenMsgCycleTime    — message has no cycle time attribute
  10. Cross-file ID collision     — same ID used in two different DBC files
"""

import re
import sys
import glob
import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── ANSI colours ─────────────────────────────────────────────────────────────
RED    = "\033[31m"
YELLOW = "\033[33m"
GREEN  = "\033[32m"
CYAN   = "\033[36m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Signal:
    name: str
    start_bit: int
    bit_length: int
    byte_order: str   # "1" = little-endian (Intel), "0" = big-endian (Motorola)
    value_type: str   # "+" unsigned, "-" signed
    factor: float
    offset: float
    min_val: float
    max_val: float
    line: int

@dataclass
class Message:
    msg_id: int
    name: str
    dlc: int          # declared length in bytes
    sender: str
    signals: list[Signal] = field(default_factory=list)
    has_cycle_time: bool = False
    line: int = 0
    source_file: str = ""

# ── Parser ────────────────────────────────────────────────────────────────────
BO_RE  = re.compile(
    r'^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\w+)'
)
SG_RE  = re.compile(
    r'^\s+SG_\s+(\w+)\s*:\s*'
    r'(\d+)\|(\d+)@([01])([+-])\s+'
    r'\(([^,]+),([^)]+)\)\s+'
    r'\[([^|]+)\|([^\]]+)\]'
)
BA_CYCLE_RE = re.compile(
    r'^BA_\s+"GenMsgCycleTime"\s+BO_\s+(\d+)\s+'
)

def parse_dbc(path: str) -> list[Message]:
    messages: list[Message] = []
    current: Optional[Message] = None
    cycle_ids: set[int] = set()

    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    for lineno, raw in enumerate(lines, 1):
        line = raw.rstrip()

        # Message definition
        m = BO_RE.match(line)
        if m:
            current = Message(
                msg_id=int(m.group(1)),
                name=m.group(2),
                dlc=int(m.group(3)),
                sender=m.group(4),
                line=lineno,
                source_file=path,
            )
            messages.append(current)
            continue

        # Signal definition
        s = SG_RE.match(line)
        if s and current is not None:
            try:
                min_val = float(s.group(8))
                max_val = float(s.group(9))
            except ValueError:
                min_val = max_val = 0.0
            sig = Signal(
                name=s.group(1),
                start_bit=int(s.group(2)),
                bit_length=int(s.group(3)),
                byte_order=s.group(4),
                value_type=s.group(5),
                factor=float(s.group(6)),
                offset=float(s.group(7)),
                min_val=min_val,
                max_val=max_val,
                line=lineno,
            )
            current.signals.append(sig)
            continue

        # Blank line or new section → leave current message context
        if line.strip() == "" or (line and not line[0].isspace() and not line.startswith("SG_")):
            if not BO_RE.match(line):
                current = None

        # Cycle time attribute
        c = BA_CYCLE_RE.match(line)
        if c:
            cycle_ids.add(int(c.group(1)))

    # Mark messages that have cycle time
    for msg in messages:
        if msg.msg_id in cycle_ids:
            msg.has_cycle_time = True

    return messages

# ── Bit-occupation helpers ────────────────────────────────────────────────────
def occupied_bits_intel(start: int, length: int) -> set[int]:
    """Little-endian (Intel) layout: bits are consecutive from start_bit."""
    return set(range(start, start + length))

def occupied_bits_motorola(start: int, length: int) -> set[int]:
    """
    Big-endian (Motorola) layout used in DBC:
    start_bit is the MSB position in the Vector bit-numbering scheme.
    We follow the standard byte-flipped DBC convention.
    """
    bits = set()
    bit = start
    for _ in range(length):
        bits.add(bit)
        if bit % 8 == 0:
            bit += 15
        else:
            bit -= 1
    return bits

def signal_bits(sig: Signal) -> set[int]:
    if sig.byte_order == "1":
        return occupied_bits_intel(sig.start_bit, sig.bit_length)
    else:
        return occupied_bits_motorola(sig.start_bit, sig.bit_length)

# ── Issue collector ───────────────────────────────────────────────────────────
@dataclass
class Issue:
    severity: str   # "error" | "warning"
    file: str
    line: int
    msg_name: str
    description: str

    def __str__(self):
        colour = RED if self.severity == "error" else YELLOW
        sev = f"{colour}{BOLD}{self.severity.upper()}{RESET}"
        loc = f"{CYAN}{self.file}:{self.line}{RESET}"
        return f"{sev}  {loc}  [{self.msg_name}]  {self.description}"

# ── Checks ────────────────────────────────────────────────────────────────────
def check_all(file_messages: dict[str, list[Message]]) -> list[Issue]:
    issues: list[Issue] = []
    all_messages: list[Message] = [m for msgs in file_messages.values() for m in msgs]

    # ── 1 & 10: Duplicate message IDs (global, including cross-file) ──────────
    id_seen: dict[int, Message] = {}
    for msg in all_messages:
        if msg.msg_id in id_seen:
            prev = id_seen[msg.msg_id]
            cross = f" (cross-file: {Path(prev.source_file).name} vs {Path(msg.source_file).name})" \
                    if prev.source_file != msg.source_file else ""
            issues.append(Issue(
                "error", msg.source_file, msg.line, msg.name,
                f"Duplicate message ID {msg.msg_id} — first seen as '{prev.name}' "
                f"at {Path(prev.source_file).name}:{prev.line}{cross}"
            ))
        else:
            id_seen[msg.msg_id] = msg

    # ── 2: Duplicate message names (global) ───────────────────────────────────
    name_seen: dict[str, Message] = {}
    for msg in all_messages:
        if msg.name in name_seen:
            prev = name_seen[msg.name]
            issues.append(Issue(
                "error", msg.source_file, msg.line, msg.name,
                f"Duplicate message name '{msg.name}' — first seen at "
                f"{Path(prev.source_file).name}:{prev.line}"
            ))
        else:
            name_seen[msg.name] = msg

    # Per-message checks ───────────────────────────────────────────────────────
    for msg in all_messages:
        frame_bits = msg.dlc * 8

        # ── 7: DLC sanity ─────────────────────────────────────────────────────
        if msg.dlc > 8:
            issues.append(Issue(
                "error", msg.source_file, msg.line, msg.name,
                f"DLC={msg.dlc} exceeds 8-byte standard CAN limit"
            ))

        # ── 9: Missing cycle time ─────────────────────────────────────────────
        if not msg.has_cycle_time:
            issues.append(Issue(
                "warning", msg.source_file, msg.line, msg.name,
                "No GenMsgCycleTime attribute defined for this message"
            ))

        # ── 5: Duplicate signal names within message ──────────────────────────
        sig_names_seen: dict[str, Signal] = {}
        for sig in msg.signals:
            if sig.name in sig_names_seen:
                issues.append(Issue(
                    "error", msg.source_file, sig.line, msg.name,
                    f"Duplicate signal name '{sig.name}' within message"
                ))
            else:
                sig_names_seen[sig.name] = sig

        # ── Per-signal checks ─────────────────────────────────────────────────
        occupied: dict[int, str] = {}   # bit → signal_name (for overlap check)

        for sig in msg.signals:

            # ── 6: Zero-length signal ─────────────────────────────────────────
            if sig.bit_length == 0:
                issues.append(Issue(
                    "error", msg.source_file, sig.line, msg.name,
                    f"Signal '{sig.name}' has bit_length=0"
                ))
                continue

            bits = signal_bits(sig)

            # ── 3: Signal overflow (exceeds frame) ────────────────────────────
            out_of_range = [b for b in bits if b >= frame_bits]
            if out_of_range:
                issues.append(Issue(
                    "error", msg.source_file, sig.line, msg.name,
                    f"Signal '{sig.name}' occupies bits {sorted(out_of_range)} "
                    f"which exceed the {frame_bits}-bit frame (DLC={msg.dlc})"
                ))

            # ── 4: Overlapping signals ────────────────────────────────────────
            for b in bits:
                if b in occupied:
                    other = occupied[b]
                    if other != sig.name:   # avoid duplicate reports
                        issues.append(Issue(
                            "error", msg.source_file, sig.line, msg.name,
                            f"Signal '{sig.name}' overlaps '{other}' at bit {b}"
                        ))
                        break
            for b in bits:
                occupied.setdefault(b, sig.name)

            # ── 8: Min > Max ──────────────────────────────────────────────────
            if sig.min_val > sig.max_val:
                issues.append(Issue(
                    "error", msg.source_file, sig.line, msg.name,
                    f"Signal '{sig.name}' has min={sig.min_val} > max={sig.max_val}"
                ))

    return issues

# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="PUTM DBC Linter")
    parser.add_argument(
        "paths", nargs="+",
        help="DBC file(s) or glob pattern(s), e.g. '**/*.dbc'"
    )
    parser.add_argument(
        "--no-warnings", action="store_true",
        help="Suppress warnings, only show errors"
    )
    parser.add_argument(
        "--github-annotations", action="store_true",
        help="Emit GitHub Actions annotation commands (::error / ::warning)"
    )
    args = parser.parse_args()

    # Resolve globs
    files: list[str] = []
    for pat in args.paths:
        matched = glob.glob(pat, recursive=True)
        if matched:
            files.extend(matched)
        elif Path(pat).exists():
            files.append(pat)
        else:
            print(f"{YELLOW}WARNING{RESET}: pattern '{pat}' matched no files", file=sys.stderr)

    if not files:
        print(f"{RED}ERROR{RESET}: no DBC files found", file=sys.stderr)
        return 2

    files = sorted(set(files))
    print(f"\n{BOLD}PUTM DBC Linter{RESET} — checking {len(files)} file(s)\n")

    file_messages: dict[str, list[Message]] = {}
    for f in files:
        try:
            file_messages[f] = parse_dbc(f)
            n = len(file_messages[f])
            print(f"  {CYAN}{f}{RESET}  ({n} message{'s' if n != 1 else ''})")
        except Exception as e:
            print(f"  {RED}PARSE ERROR{RESET} {f}: {e}")
            return 2

    issues = check_all(file_messages)

    if args.no_warnings:
        issues = [i for i in issues if i.severity == "error"]

    errors   = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    print()
    if not issues:
        print(f"{GREEN}{BOLD}✓ No issues found{RESET}\n")
        return 0

    for issue in sorted(issues, key=lambda i: (i.file, i.line)):
        print(issue)
        if args.github_annotations:
            level = "error" if issue.severity == "error" else "warning"
            rel   = Path(issue.file).as_posix()
            msg   = issue.description.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::{level} file={rel},line={issue.line}::{msg}")

    print()
    print(
        f"Found {RED}{BOLD}{len(errors)} error(s){RESET} "
        f"and {YELLOW}{BOLD}{len(warnings)} warning(s){RESET}"
    )
    print()
    return 1 if errors else 0

if __name__ == "__main__":
    sys.exit(main())
