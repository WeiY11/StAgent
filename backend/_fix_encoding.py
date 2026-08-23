"""One-shot recovery: fix CJK/em-dash mojibake in chat/__init__.py.

The PowerShell Get-Content/Set-Content round-trip corrupted non-ASCII chars on
9 lines. We rebuild those exact lines from HEAD (correct UTF-8) by matching a
stable ASCII anchor, then rewrite the file as clean UTF-8 (no BOM, LF).
"""

import subprocess

PATH = "StAgent/api/v1/chat/__init__.py"

# Read HEAD (correct CJK) as raw bytes -> utf-8.
head = subprocess.run(
    ["git", "show", "HEAD:backend/StAgent/api/v1/chat/__init__.py"],
    capture_output=True,
    check=True,
).stdout.decode("utf-8")
head_lines = head.splitlines()

# Current (corrupted) working tree, BOM stripped.
cur = open(PATH, encoding="utf-8-sig").read().splitlines()

# ASCII anchors uniquely identifying each corrupted line. The correct line is
# looked up from HEAD by the same anchor.
anchors = [
    "endpoints never",
    "Request/Response Models",
    "not just the LLM",
    "free chats stay",
    "to cut time-to-first-token",
    "_continue_keywords = {",
    "full chat history stays intact",
    "The same micro",
    "the frontend xterm dock renders",
]


def head_line_for(anchor: str) -> str:
    matches = [ln for ln in head_lines if anchor in ln]
    if len(matches) != 1:
        raise SystemExit(f"anchor {anchor!r} matched {len(matches)} HEAD lines")
    return matches[0]


fixed = 0
for i, line in enumerate(cur):
    if not any(ord(c) > 127 for c in line):
        continue
    # find which anchor applies
    applicable = [a for a in anchors if a in line]
    if len(applicable) != 1:
        raise SystemExit(f"line {i+1} matched anchors {applicable}: {line!r}")
    correct = head_line_for(applicable[0])
    if correct != line:
        cur[i] = correct
        fixed += 1
        print(f"fixed L{i+1}: {correct!r}")

# Sanity: no non-ASCII corruption artefacts remain that aren't in HEAD.
open(PATH, "w", encoding="utf-8", newline="\n").write("\n".join(cur) + "\n")
print("total fixed:", fixed)
