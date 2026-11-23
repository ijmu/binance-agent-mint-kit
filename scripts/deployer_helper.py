#!/usr/bin/env python3
"""Rule 11 deployer checks for the Binance Wallet free-mint board.

Read-only helper for mint_scan.py / scanner.py, also runnable standalone.

Rules (from @mulight playbook, hourly scan rule 11):
  11a  same deployer launched 3+ differently-named projects in 24h -> exclude
  11b  deployer is on the blacklist (has rugged before) -> exclude
  11c  same contract bytecode deployed 3+ times in 24h -> exclude
  11d  deployer wallet is new (first seen ~within 8h) -> caution only, not exclude
  11e  mint progress must show >= 20 distinct minter wallets to be trusted

Data sources (read-only, no wallet, no signing, no spend):
  - deployer: BscScan API `getcontractcreation` (key ONLY from env
    BSCSCAN_API_KEY; no key -> that source is skipped, never hardcoded),
    then Routescan's public Etherscan-compatible API (no key),
    then a pure-RPC fallback: binary-search the creation block with
    eth_getCode and scan that block's transactions for the creation tx.
    All fail -> "unknown", never fabricated.
  - distinct minters: eth_getLogs for Transfer events from the zero
    address (i.e. mints) on the token contract, chunked, with retries.
    RPC failure -> "unknown".
  - ledger: deployer-ledger.json (deployer -> projects seen + first_seen
    + outcome; blacklist list). Missing/corrupt ledger -> treated as
    empty, with a note; this file never writes to the ledger.

RPC style follows mint_scan.py: same public BSC endpoints, try each in
turn, tolerate 403s / flaky nodes, short timeouts, no BscScan web pages
(Cloudflare-blocked) and no Sourcify (unreachable from this host).

MEASURED LIMITATIONS (2026-11-13, this host -- see final report):
  - get_deployer: BscScan API needs BSCSCAN_API_KEY (not set here);
    Routescan creation API answers "chain not supported" for BSC;
    pure-RPC creation-block search cannot work for already-deployed
    contracts because the public RPCs keep only a shallow, flaky state
    window for eth_getCode (~512 blocks back worked, ~30k back failed
    with "missing trie node" / archive-token 403). So deployer is
    "unknown" unless a BscScan API key is configured.
  - count_distinct_minters: eth_getLogs on the free endpoints is flaky,
    not deterministically blocked -- some 5k-block chunks succeed, others
    fail with -32005 "limit exceeded" (all Binance dataseeds) or HTTP 429
    (publicnode), and the failure pattern changes run to run; llamaRPC is
    unreachable from this host and the keyless BscScan API returns an HTML
    unreachable from this host and the keyless BscScan API returns an HTML
    wall. The function is single-attempt per chunk (retrying + splitting
    was tried: a full scan then took 10+ minutes without finishing, so
    it was reverted); a failed chunk ends the scan as "unknown" with the
    partial count in evidence. Reliable minter counts need a BscScan API
    key or a getLogs-capable RPC.

Usage:
  python3 deployer_helper.py deployer <contract_address>
  python3 deployer_helper.py minters  <contract_address> [--from-block N]
  python3 deployer_helper.py check    <contract_address> [--ledger PATH]
All commands print a single JSON object on stdout.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# --- BSC public RPCs, same list/order as mint_scan.py -----------------------
RPCS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed4.binance.org",
    "https://bsc.publicnode.com",
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)  # keccak256("Transfer(address,address,uint256)")
ZERO_TOPIC = "0x" + "00" * 32

DEFAULT_LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "deployer-ledger.json")

# Rule thresholds
RULE11A_MAX_PROJECTS_24H = 3   # >= 3 distinct names in 24h -> fail
RULE11C_MAX_DEPLOYS_24H = 3    # >= 3 same-bytecode deploys in 24h -> fail
RULE11D_NEW_WALLET_HOURS = 8   # first seen within this long -> caution
RULE11E_MIN_MINTERS = 20       # >= 20 distinct minters -> pass

# eth_getLogs chunk size (public BSC nodes usually cap range at 5k-10k blocks)
LOG_CHUNK_BLOCKS = 5000
# Default minters scan-back window when --from-block is not given
# (~3.5 days at ~3s/block; bounded so a flaky public RPC can finish)
DEFAULT_SCANBACK_BLOCKS = 100000

REQUEST_TIMEOUT = 20


# --- generic JSON-RPC --------------------------------------------------------
def eth_rpc(method, params, attempts_per_rpc=1):
    """Try each RPC in turn until one returns a usable result.

    Returns (result, error_note). Mirrors mint_scan.py's failure handling:
    HTTP errors / 403s / JSON-RPC errors / empty results are all recorded
    in the note and the next endpoint is tried. The note aggregates the
    distinct errors from every endpoint (not just the last one) so a
    caller can tell a rate limit apart from a method refusal.
    """
    errs = []
    for rpc in RPCS:
        for _ in range(attempts_per_rpc):
            try:
                r = requests.post(
                    rpc,
                    json={"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params},
                    timeout=REQUEST_TIMEOUT,
                )
            except Exception as e:  # noqa: BLE001 - record and move on
                errs.append(f"{rpc}: {type(e).__name__}: {e}")
                continue
            if r.status_code == 429:
                # Public nodes rate-limit bursts; one polite retry.
                errs.append(f"{rpc}: HTTP 429")
                time.sleep(2)
                try:
                    r = requests.post(
                        rpc,
                        json={"jsonrpc": "2.0", "id": 1, "method": method,
                              "params": params},
                        timeout=REQUEST_TIMEOUT,
                    )
                except Exception as e:  # noqa: BLE001
                    errs.append(f"{rpc}: {type(e).__name__}: {e}")
                    continue
            if r.status_code != 200:
                errs.append(f"{rpc}: HTTP {r.status_code}")
                continue
            try:
                res = r.json()
            except Exception as e:  # noqa: BLE001
                errs.append(f"{rpc}: bad JSON: {e}")
                continue
            if "error" in res:
                err = res["error"]
                errs.append(f"{rpc}: {err.get('message', err)}")
                continue
            raw = res.get("result")
            if raw is None:
                errs.append(f"{rpc}: empty result")
                continue
            return raw, ""
    seen = []
    for e in errs:
        if e not in seen:
            seen.append(e)
    return None, "; ".join(seen)


def latest_block():
    raw, err = eth_rpc("eth_blockNumber", [])
    if raw is None:
        return None, err
    try:
        return int(raw, 16), ""
    except ValueError:
        return None, f"bad block number {raw!r}"


def _norm_addr(addr):
    return addr.lower() if isinstance(addr, str) else addr


# --- deployer lookup ---------------------------------------------------------
def _bscscan_creation(address):
    """BscScan getcontractcreation. Key only from env; no key -> skipped."""
    key = os.environ.get("BSCSCAN_API_KEY", "").strip()
    if not key:
        return None, "skipped: BSCSCAN_API_KEY not set"
    try:
        r = requests.get(
            "https://api.bscscan.com/api",
            params={"module": "contract", "action": "getcontractcreation",
                    "contractaddresses": address, "apikey": key},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"BscScan API request failed: {e}"
    if r.status_code != 200:
        return None, f"BscScan API HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return None, f"BscScan API bad JSON: {e}"
    result = data.get("result")
    if isinstance(result, list) and result:
        creator = result[0].get("contractCreator")
        txhash = result[0].get("txHash")
        if creator:
            return {"deployer": _norm_addr(creator), "tx_hash": txhash,
                    "source": "bscscan-api"}, ""
    return None, f"BscScan API: no creator returned (status={data.get('status')}, message={data.get('message')})"


def _routescan_creation(address):
    """Routescan public Etherscan-compatible API (no key needed)."""
    try:
        r = requests.get(
            "https://api.routescan.io/v2/network/mainnet/evm/56/etherscan/api",
            params={"module": "contract", "action": "getcontractcreation",
                    "contractaddresses": address},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"Routescan request failed: {e}"
    if r.status_code != 200:
        return None, f"Routescan HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return None, f"Routescan bad JSON: {e}"
    result = data.get("result")
    if isinstance(result, list) and result:
        creator = result[0].get("contractCreator")
        txhash = result[0].get("txHash")
        if creator:
            return {"deployer": _norm_addr(creator), "tx_hash": txhash,
                    "source": "routescan-api"}, ""
    return None, f"Routescan: no creator returned (status={data.get('status')}, message={data.get('message')})"


def _rpc_creation_block(address):
    """Binary-search the first block where the contract has code.

    Returns (block_number, error_note). Bounded: ~30 eth_getCode calls.

    Measured 2026-11-13: the four public RPCs keep only a shallow,
    node- and time-dependent state window for eth_getCode -- ~512 blocks
    back worked, ~30k blocks back failed ("missing trie node" on Binance
    dataseeds, "Archive requests require a personal token" on publicnode).
    So this fallback can only find the creation block of a contract
    deployed inside that window. We probe a ladder of lookbacks first
    and abort with a clear reason instead of binary-searching the whole
    chain (which cannot succeed).
    """
    latest, err = latest_block()
    if latest is None:
        return None, f"cannot get latest block: {err}"

    def has_code(block):
        raw, _ = eth_rpc("eth_getCode", [address, hex(block)])
        if raw is None:
            return None
        return raw not in ("0x", "0x0", "")

    # Ladder: deepest usable historical block first. First offset where
    # eth_getCode answers at all defines the provable window.
    window_lo = None
    for lookback in (100000, 10000, 1000, 100):
        probe = has_code(max(0, latest - lookback))
        if probe is None:
            continue  # no RPC could answer that far back; try shallower
        if probe:
            # Code already present at the deepest answerable block:
            # contract predates the provable window.
            return None, (f"contract already has code at latest-{lookback} "
                          f"(deepest block public RPCs will answer); "
                          f"creation block predates the shallow state window "
                          f"kept by free RPCs -- needs an archive node or a "
                          f"BscScan API key")
        window_lo = max(0, latest - lookback)
        break
    if window_lo is None:
        return None, ("historical eth_getCode unavailable on public RPCs "
                      "(Binance dataseeds: 'missing trie node'; publicnode: "
                      "archive token required); creation-block search needs "
                      "an archive node or a BscScan API key")
    lo, hi = window_lo, latest  # code empty at lo, non-empty at hi
    while hi - lo > 1:
        mid = (lo + hi) // 2
        c = has_code(mid)
        if c is None:
            return None, f"eth_getCode failed during search at block {mid}"
        if c:
            hi = mid
        else:
            lo = mid
    return hi, ""


def _rpc_creation_tx(address):
    """Pure-RPC fallback: find the creation tx in the creation block."""
    block_no, err = _rpc_creation_block(address)
    if block_no is None:
        return None, err
    block, err = eth_rpc("eth_getBlockByNumber", [hex(block_no), True])
    if block is None:
        return None, f"cannot fetch creation block {block_no}: {err}"
    for tx in block.get("transactions", []) or []:
        if tx.get("to") is None:  # contract creation tx
            receipt, rerr = eth_rpc(
                "eth_getTransactionReceipt", [tx.get("hash")])
            if receipt is None:
                continue
            if _norm_addr(receipt.get("contractAddress", "")) == _norm_addr(address):
                return {"deployer": _norm_addr(tx.get("from", "")),
                        "tx_hash": tx.get("hash"),
                        "creation_block": block_no,
                        "source": "rpc-creation-block-scan"}, ""
    return None, (f"no creation tx for {address} found in block {block_no} "
                  f"(tried {len(block.get('transactions', []) or [])} txs)")


def get_deployer(address):
    """Look up a contract's deployer.

    Returns dict: {"deployer": address|None, "status": "known"|"unknown",
    "source": ..., "tx_hash": ..., "creation_block": ..., "attempts": [...]}.
    Never fabricates a deployer: unknown stays unknown.
    """
    address = _norm_addr(address)
    attempts = []
    for fn in (_bscscan_creation, _routescan_creation, _rpc_creation_tx):
        try:
            found, err = fn(address)
        except Exception as e:  # noqa: BLE001 - a source must never crash the helper
            attempts.append(f"{fn.__name__}: exception {e}")
            continue
        if found:
            out = {"deployer": found["deployer"], "status": "known",
                   "source": found["source"], "attempts": attempts}
            if found.get("tx_hash"):
                out["tx_hash"] = found["tx_hash"]
            if found.get("creation_block") is not None:
                out["creation_block"] = found["creation_block"]
            return out
        attempts.append(f"{fn.__name__}: {err}")
    return {"deployer": None, "status": "unknown", "source": None,
            "attempts": attempts}


# --- distinct minters ----------------------------------------------------------
def count_distinct_minters(contract_address, from_block=None):
    """Count distinct wallets that minted (Transfer from zero address).

    Returns dict: {"distinct_minters": int|None, "status": "known"|"unknown",
    "from_block": int, "to_block": int, "logs_scanned": int,
    "chunks_failed": [...], "evidence": str}.
    RPC failure in any chunk -> status "unknown" (partial count kept in
    evidence, never presented as a final number).
    Deliberately single-attempt per chunk (like mint_scan.py): the free
    endpoints 429/limit-exceeded in bursts, and retrying + splitting
    made a full scan take 10+ minutes without finishing. If a chunk
    fails, the scan stops and reports unknown with the partial count.
    """
    contract = _norm_addr(contract_address)
    latest, err = latest_block()
    if latest is None:
        return {"distinct_minters": None, "status": "unknown",
                "from_block": from_block, "to_block": None, "logs_scanned": 0,
                "chunks_failed": [],
                "evidence": f"cannot get latest block: {err}"}
    if from_block is None:
        from_block = max(0, latest - DEFAULT_SCANBACK_BLOCKS)
    minters = set()
    logs_scanned = 0
    chunks_failed = []
    start = from_block
    while start <= latest:
        end = min(start + LOG_CHUNK_BLOCKS - 1, latest)
        logs, lerr = eth_rpc("eth_getLogs", [{
            "address": contract,
            "fromBlock": hex(start),
            "toBlock": hex(end),
            "topics": [TRANSFER_TOPIC, ZERO_TOPIC],
        }])
        if logs is None:
            chunks_failed.append({"from_block": start, "to_block": end,
                                  "error": lerr})
            break
        logs_scanned += len(logs)
        for log in logs:
            topics = log.get("topics", []) or []
            if len(topics) >= 3:
                # topic[2] is the `to` address (the minter), 32-byte padded
                minters.add("0x" + topics[2][-40:].lower())
        start = end + 1
    if chunks_failed:
        return {"distinct_minters": None, "status": "unknown",
                "from_block": from_block, "to_block": latest,
                "logs_scanned": logs_scanned, "chunks_failed": chunks_failed,
                "evidence": (f"eth_getLogs failed at block "
                             f"{chunks_failed[0]['from_block']}; partial scan "
                             f"saw {len(minters)} distinct minters in "
                             f"{logs_scanned} mint logs (partial, not trusted)")}
    return {"distinct_minters": len(minters), "status": "known",
            "from_block": from_block, "to_block": latest,
            "logs_scanned": logs_scanned, "chunks_failed": [],
            "evidence": (f"{len(minters)} distinct minter wallets in "
                         f"{logs_scanned} mint Transfer logs, blocks "
                         f"{from_block}-{latest}")}


# --- ledger ------------------------------------------------------------------
def load_ledger(ledger_path):
    """Load deployer-ledger.json tolerantly.

    Returns (ledger_dict, note). Missing/corrupt file -> empty ledger + note.
    Ledger shape: {"deployers": {addr: entry}, "blacklist": [addr, ...]}.
    Entry shapes tolerated:
      {"projects": [{"name": ..., "first_seen": ..., "code_hash": ...}, ...],
       "first_seen": ...}
      {"projects": ["NAME", ...], "first_seen": ...}
      [{"name": ..., "first_seen": ...}, ...]  (bare list)
    first_seen may be ISO-8601 or epoch seconds.
    """
    try:
        with open(ledger_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"deployers": {}, "blacklist": []}, f"ledger not found: {ledger_path} (treated as empty)"
    except Exception as e:  # noqa: BLE001
        return {"deployers": {}, "blacklist": []}, f"ledger unreadable ({e}); treated as empty"
    if not isinstance(data, dict):
        return {"deployers": {}, "blacklist": []}, "ledger is not a JSON object; treated as empty"
    return data, ""


def _parse_time(value):
    """ISO-8601 / epoch -> aware datetime, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _ledger_projects(entry):
    """Normalise one deployer entry -> list of project dicts.

    Each: {"name": str|None, "first_seen": datetime|None,
           "code_hash": str|None, "outcome": str|None}.
    """
    projects = []
    if isinstance(entry, list):
        raw_list = entry
        entry_first = None
    elif isinstance(entry, dict):
        raw_list = entry.get("projects", [])
        entry_first = _parse_time(entry.get("first_seen"))
        if isinstance(raw_list, dict):  # name -> {first_seen, ...}
            raw_list = [{"name": k, **(v if isinstance(v, dict) else {})}
                        for k, v in raw_list.items()]
    else:
        return projects
    if isinstance(raw_list, str):
        raw_list = [raw_list]
    for item in raw_list or []:
        if isinstance(item, str):
            projects.append({"name": item, "first_seen": entry_first,
                             "code_hash": None, "outcome": None})
        elif isinstance(item, dict):
            projects.append({
                "name": item.get("name") or item.get("token") or item.get("project"),
                "first_seen": _parse_time(item.get("first_seen")) or entry_first,
                "code_hash": item.get("code_hash") or item.get("bytecode_hash"),
                "outcome": item.get("outcome"),
            })
    if not projects and entry_first is not None and isinstance(entry, dict):
        projects.append({"name": None, "first_seen": entry_first,
                         "code_hash": None, "outcome": entry.get("outcome")})
    return projects


# --- Rule 11 evaluation --------------------------------------------------------
def evaluate_rule11(address, ledger_path=DEFAULT_LEDGER):
    """Evaluate rules 11a-11e for a contract address against the ledger.

    Returns dict with per-rule {"status": "pass"|"fail"|"unknown"|"caution",
    "evidence": str} plus "deployer", "overall" and "ledger_note".
    Statuses: fail = exclude; caution (11d only) = flag, do not exclude;
    unknown = could not verify, do not treat as pass.
    """
    now = datetime.now(timezone.utc)
    address = _norm_addr(address)
    ledger, ledger_note = load_ledger(ledger_path)
    deployers = ledger.get("deployers", {}) or {}
    blacklist = {_norm_addr(b) for b in (ledger.get("blacklist", []) or [])
                 if isinstance(b, str)}

    dep = get_deployer(address)
    deployer = dep.get("deployer")
    entry = None
    if deployer:
        entry = deployers.get(deployer) or deployers.get(deployer.lower())
    projects = _ledger_projects(entry) if entry is not None else []

    def recent(hours):
        return [p for p in projects
                if p["first_seen"] is not None
                and (now - p["first_seen"]).total_seconds() <= hours * 3600]

    results = {"address": address, "deployer": deployer,
               "deployer_status": dep["status"],
               "deployer_source": dep.get("source"),
               "deployer_attempts": dep.get("attempts", []),
               "ledger_note": ledger_note, "rules": {}}

    # 11a: 3+ differently-named projects from same deployer in 24h
    if deployer is None:
        results["rules"]["11a"] = {
            "status": "unknown",
            "evidence": "deployer unknown, cannot check 24h project count"}
    elif entry is None:
        results["rules"]["11a"] = {
            "status": "pass",
            "evidence": f"deployer {deployer} not in ledger (0 known projects)"}
    else:
        names_24h = {p["name"] for p in recent(24) if p["name"]}
        n = len(names_24h)
        results["rules"]["11a"] = {
            "status": "fail" if n >= RULE11A_MAX_PROJECTS_24H else "pass",
            "evidence": (f"deployer {deployer} launched {n} distinct-named "
                         f"project(s) in last 24h: {sorted(names_24h) or 'none in ledger'}"
                         + (f" (>= {RULE11A_MAX_PROJECTS_24H} -> exclude)"
                            if n >= RULE11A_MAX_PROJECTS_24H else ""))}

    # 11b: deployer blacklist
    if deployer is None:
        results["rules"]["11b"] = {
            "status": "unknown",
            "evidence": "deployer unknown, cannot check blacklist"}
    elif deployer in blacklist:
        results["rules"]["11b"] = {
            "status": "fail",
            "evidence": f"deployer {deployer} is on the blacklist"}
    else:
        results["rules"]["11b"] = {
            "status": "pass",
            "evidence": f"deployer {deployer} not on blacklist "
                        f"({len(blacklist)} entries)"}

    # 11c: same contract bytecode deployed 3+ times in 24h
    if deployer is None:
        results["rules"]["11c"] = {
            "status": "unknown",
            "evidence": "deployer unknown, cannot check repeat deployments"}
    elif entry is None:
        results["rules"]["11c"] = {
            "status": "unknown",
            "evidence": (f"deployer {deployer} not in ledger; no bytecode "
                         "hashes recorded to compare repeat deployments")}
    else:
        hashes_24h = [p["code_hash"] for p in recent(24) if p["code_hash"]]
        if not hashes_24h:
            results["rules"]["11c"] = {
                "status": "unknown",
                "evidence": ("ledger has no code_hash/bytecode_hash for this "
                             "deployer's projects; repeat-deployment check "
                             "needs hashes recorded at scan time")}
        else:
            from collections import Counter
            most_common, count = Counter(hashes_24h).most_common(1)[0]
            results["rules"]["11c"] = {
                "status": "fail" if count >= RULE11C_MAX_DEPLOYS_24H else "pass",
                "evidence": (f"most-repeated bytecode hash in last 24h "
                             f"deployed {count}x ({most_common[:18]}...)"
                             + (f" (>= {RULE11C_MAX_DEPLOYS_24H} -> exclude)"
                                if count >= RULE11C_MAX_DEPLOYS_24H else ""))}

    # 11d: new-wallet deployer (~8h) -> caution only
    if deployer is None:
        results["rules"]["11d"] = {
            "status": "unknown",
            "evidence": "deployer unknown, cannot check wallet age"}
    elif entry is None:
        results["rules"]["11d"] = {
            "status": "caution",
            "evidence": (f"deployer {deployer} not in ledger: first-seen "
                         "unknown, treat as new/unseen wallet (caution, not exclusion)")}
    else:
        firsts = [p["first_seen"] for p in projects if p["first_seen"]]
        if not firsts:
            results["rules"]["11d"] = {
                "status": "unknown",
                "evidence": f"ledger entry for {deployer} has no first_seen timestamp"}
        else:
            first = min(firsts)
            age_h = (now - first).total_seconds() / 3600
            results["rules"]["11d"] = {
                "status": "caution" if age_h <= RULE11D_NEW_WALLET_HOURS else "pass",
                "evidence": (f"deployer first seen {first.isoformat()} "
                             f"({age_h:.1f}h ago)"
                             + (" (<= 8h -> caution, not exclusion)"
                                if age_h <= RULE11D_NEW_WALLET_HOURS else ""))}

    # 11e: >= 20 distinct minter wallets
    m = count_distinct_minters(address)
    if m["status"] != "known":
        results["rules"]["11e"] = {"status": "unknown", "evidence": m["evidence"]}
    else:
        n = m["distinct_minters"]
        results["rules"]["11e"] = {
            "status": "pass" if n >= RULE11E_MIN_MINTERS else "fail",
            "evidence": m["evidence"]
                         + (f" (>= {RULE11E_MIN_MINTERS} -> trusted)"
                            if n >= RULE11E_MIN_MINTERS
                            else f" (< {RULE11E_MIN_MINTERS} -> do not trust progress yet)")}
    results["minters"] = m

    statuses = [r["status"] for r in results["rules"].values()]
    if "fail" in statuses:
        overall = "fail"
    elif "unknown" in statuses:
        overall = "unknown"
    elif "caution" in statuses:
        overall = "caution"
    else:
        overall = "pass"
    results["overall"] = overall
    return results


# --- CLI -----------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Rule 11 deployer checks (read-only) for the free-mint board.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dep = sub.add_parser("deployer", help="look up a contract's deployer")
    p_dep.add_argument("address")

    p_min = sub.add_parser("minters", help="count distinct minter wallets")
    p_min.add_argument("address")
    p_min.add_argument("--from-block", type=int, default=None,
                       help="first block to scan (default: latest - 100000)")

    p_chk = sub.add_parser("check", help="evaluate rules 11a-11e")
    p_chk.add_argument("address")
    p_chk.add_argument("--ledger", default=DEFAULT_LEDGER,
                       help="path to deployer-ledger.json")

    args = parser.parse_args(argv)
    if args.cmd == "deployer":
        out = get_deployer(args.address)
    elif args.cmd == "minters":
        out = count_distinct_minters(args.address, args.from_block)
    else:
        out = evaluate_rule11(args.address, args.ledger)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
