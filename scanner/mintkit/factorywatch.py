"""On-chain factory event watch — SHADOW MODE ONLY (added 2026-11-22, P2).

Polls eth_getLogs for the known launch-factory contracts listed in
scanner/config.json -> "factory_watch.factories" and appends one line per
event to hidden_files/factory-shadow-log.md. Shadow means: results are
NEVER fed into verification, scoring or the board; the log exists so a
human/agent can judge the signal-to-noise ratio before any wiring.

Factories come only from local data (config excluded_known evidence /
deployer-ledger). As of 2026-11-22 the only factory with a full address
in scanner data is the StarsAgent launch factory; the Agent ID registry
contract has no full address in local data and is intentionally absent
(see config factory_watch.skipped).

Read-only: eth_blockNumber + eth_getLogs through mintkit.chain's RPC
rotation. Nothing signs or sends; unread RPC is reported, never guessed.

CLI (from scanner/):
  python3 -m mintkit.factorywatch [--max-blocks 5000]

State: scanner/factory_watch_state.json {factory_lower: last_block}.
First run for a factory anchors at the current head (no history dump).
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mintkit import chain, logs  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "factory_watch_state.json"
SHADOW_LOG = BASE.parent / "hidden_files" / "factory-shadow-log.md"


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _block_number():
    res = chain._rpc("eth_blockNumber", [])
    return chain._hex_to_int(res) if res else None


def _get_logs(address, from_block, to_block):
    """eth_getLogs for one emitter address. Returns a list (possibly
    empty), or None when every RPC endpoint failed. Public endpoints
    rate-limit eth_getLogs hard (-32005), so retry once after a pause;
    on persistent failure the caller keeps its state and retries the
    same range next poll — events are never skipped silently."""
    for attempt in range(2):
        res = chain._rpc("eth_getLogs", [{
            "address": address,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }])
        if isinstance(res, list):
            return res
        if attempt == 0:
            time.sleep(3)
    return None


def _addr_from_word(word):
    """Extract an address from a 32-byte topic/data word, or None."""
    if not word or len(word) != 66:
        return None
    if word[2:26] == "0" * 24:
        return "0x" + word[-40:]
    return None


def _candidates(entry):
    """Address-looking words in topics[1:] and the first data words —
    heuristic only; shadow log consumers must verify before any use."""
    out = []
    for topic in (entry.get("topics") or [])[1:]:
        addr = _addr_from_word(topic)
        if addr and addr not in out:
            out.append(addr)
    data = entry.get("data") or "0x"
    body = data[2:]
    for i in range(0, min(len(body), 64 * 4), 64):
        addr = _addr_from_word("0x" + body[i:i + 64])
        if addr and addr not in out:
            out.append(addr)
    return out


def poll_factory(factory, state, max_blocks):
    """Poll one factory. Returns a summary dict; mutates `state`."""
    name = factory.get("name") or "factory"
    address = factory["address"]
    key = address.lower()
    head = _block_number()
    if head is None:
        return {"factory": name, "status": "unread", "events": 0}
    last = state.get(key)
    if last is None:
        state[key] = head
        logs.append_log(str(SHADOW_LOG),
                        f"- {_utcnow()} | INIT | {name} {address} | "
                        f"anchored at block {head} (no history backfill)",
                        dedupe_key=f"init:{key}:{head}")
        return {"factory": name, "status": "anchored", "head": head, "events": 0}
    from_block = int(last) + 1
    if from_block > head:
        return {"factory": name, "status": "up_to_date", "head": head, "events": 0}
    to_block = min(head, from_block + max_blocks - 1)
    entries = _get_logs(address, from_block, to_block)
    if entries is None:
        return {"factory": name, "status": "unread",
                "from": from_block, "to": to_block, "events": 0}
    for entry in entries:
        block = chain._hex_to_int(entry.get("blockNumber"))
        tx = entry.get("transactionHash") or "?"
        idx = entry.get("logIndex") or "0x0"
        topics = entry.get("topics") or []
        cands = _candidates(entry)
        logs.append_log(
            str(SHADOW_LOG),
            f"- {_utcnow()} | EVENT | {name} {address} | block {block} | "
            f"tx {tx} | topic0 {(topics[0] if topics else '?')} | "
            f"candidate_addrs {','.join(cands) if cands else 'none'}",
            dedupe_key=f"evt:{tx}:{idx}")
    state[key] = to_block
    return {"factory": name, "status": "ok", "from": from_block,
            "to": to_block, "head": head, "events": len(entries)}


def poll(max_blocks=5000):
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    watch = cfg.get("factory_watch") or {}
    factories = watch.get("factories") or []
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    results = [poll_factory(f, state, max_blocks) for f in factories]
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")
    return {"generated_at": _utcnow(), "mode": watch.get("mode", "shadow"),
            "skipped": [s.get("name") for s in watch.get("skipped") or []],
            "results": results}


if __name__ == "__main__":
    args = sys.argv[1:]
    max_blocks = 5000
    if "--max-blocks" in args:
        max_blocks = int(args[args.index("--max-blocks") + 1])
    print(json.dumps(poll(max_blocks), ensure_ascii=False))
