"""Fixed-script read path for JSON / text files (mintkit).

Provenance: local helper for the Binance free-mint pipeline; replaces
ad-hoc `python3 -c` reads, which the scanner task bodies forbid
(BUG-030 covered only log *writes*; BUG-031 covers *reads*).

Usage (from scanner/):
    python3 -m mintkit.read <path> [--key a.b.c]
    python3 -m mintkit.read <path> --lines 5        # print last N lines (text files)
Prints the file content (JSON re-serialised, or raw text) to stdout.
Exit 2 on missing file / invalid JSON / bad key path.
Only stdlib; read-only, never writes.
"""

import argparse
import json
import sys


def read_json(path, key=None):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if key:
        for part in key.split("."):
            if isinstance(data, dict) and part in data:
                data = data[part]
            elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
                data = data[int(part)]
            else:
                raise KeyError(key)
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mintkit.read")
    ap.add_argument("path")
    ap.add_argument("--key", default=None, help="dotted key path into JSON, e.g. a.b.0")
    ap.add_argument("--lines", type=int, default=None, help="print last N lines (text mode)")
    args = ap.parse_args(argv)
    try:
        if args.lines is not None:
            with open(args.path, encoding="utf-8") as f:
                lines = f.readlines()
            sys.stdout.write("".join(lines[-args.lines:]))
            return 0
        data = read_json(args.path, args.key)
        if isinstance(data, str):
            print(data)
        else:
            print(json.dumps(data, ensure_ascii=False))
        return 0
    except FileNotFoundError:
        print(f"read error: file not found: {args.path}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, KeyError) as e:
        print(f"read error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
