"""Minted registry — what this wallet has already minted (dedupe source).

Data file: scanner/minted_registry.json, {"entries": [...]}; each entry:
{contract, kind: "token"|"nft", token_id (nft) or amount (token), tx,
 time_cst, source_task}. Created by the code on first use if missing.

CLI (from scanner/):
  python3 -m mintkit.registry check <contract>
  python3 -m mintkit.registry add '<entry-json>'
"""
import json
import sys
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "minted_registry.json"


def _load():
    if not REGISTRY_PATH.exists():
        return {"entries": []}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data
    except Exception:
        pass
    return {"entries": []}


def _save(data):
    REGISTRY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")


def is_registered(contract):
    """Return the registry entry for a contract (case-insensitive) or None."""
    target = (contract or "").lower()
    for entry in _load()["entries"]:
        if (entry.get("contract") or "").lower() == target:
            return entry
    return None


def register(entry):
    """Append an entry. Dedupe on (contract, tx): if the same contract was
    already recorded with the same tx, nothing is written. Returns True when
    a new entry was written, False when it was a duplicate."""
    data = _load()
    contract = (entry.get("contract") or "").lower()
    tx = (entry.get("tx") or "").lower()
    for old in data["entries"]:
        if (old.get("contract") or "").lower() == contract and \
                (old.get("tx") or "").lower() == tx:
            return False
    data["entries"].append(entry)
    _save(data)
    return True


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "check":
        entry = is_registered(args[1])
        print(json.dumps({"registered": entry is not None, "entry": entry},
                         ensure_ascii=False))
    elif len(args) == 2 and args[0] == "add":
        try:
            new_entry = json.loads(args[1])
        except Exception as e:
            print(json.dumps({"error": f"bad entry json: {e}"}))
            sys.exit(2)
        print(json.dumps({"added": register(new_entry)}, ensure_ascii=False))
    else:
        print(json.dumps({"error": "usage: registry check <contract> | add '<json>'"}))
        sys.exit(2)
