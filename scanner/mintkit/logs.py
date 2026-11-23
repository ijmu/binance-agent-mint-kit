"""Shared one-line log appends (agent-hunt-log / auto-exec-log /
parallel-eval-log). Append mode + trailing newline; optional dedupe key:
if the key already appears anywhere in the file, nothing is written.

CLI: python3 -m mintkit.logs <logfile> <line> [--key <k>]   (from scanner/)
"""
import json
import sys
from pathlib import Path


def append_log(path, line, dedupe_key=None):
    """Append `line` to `path`. Returns True when written, False when the
    dedupe key was already present."""
    p = Path(path)
    if dedupe_key and p.exists():
        if dedupe_key in p.read_text(encoding="utf-8", errors="replace"):
            return False
    with open(p, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
    return True


if __name__ == "__main__":
    args = sys.argv[1:]
    key = None
    if "--key" in args:
        i = args.index("--key")
        key = args[i + 1] if i + 1 < len(args) else None
        args = args[:i]
    if len(args) != 2:
        print(json.dumps({"error": "usage: python3 -m mintkit.logs <logfile> <line> [--key <k>]"}))
        sys.exit(2)
    print(json.dumps({"appended": append_log(args[0], args[1], key)},
                     ensure_ascii=False))
