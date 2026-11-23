#!/usr/bin/env python3
"""Hourly BSC eth_call scan for the Binance Wallet free-mint board.
Rebuilt 2026-11-09 ~18:30 CST (previous /tmp copy lost on VM recycle).
Contract map sourced from the operator scan logs of 2026-11-09 (scan-2026-11-09-*.md)
(0644 NUSA sale; 0944 SUOHA/YAMA; 1044 BYTE; 1144 KZMB/MALTA/OPEC/$BI; 1244 WOJAGENT/META BLOOM/ORDIagent/BHTB; 1630 EGGO).
Usage: python3 mint_scan.py
"""
import json, urllib.request
from Crypto.Hash import keccak

RPCS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed4.binance.org",
    "https://bsc.publicnode.com",
]

def sel(sig: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()[:8]

def eth_rpc(method: str, params):
    """Generic JSON-RPC call, trying each RPC until one returns a usable result."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    last_err = ""
    for rpc in RPCS:
        req = urllib.request.Request(rpc, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.load(r)
        except Exception as e:
            last_err = f"{rpc}: {e}"
            continue
        if "error" in res:
            last_err = f"{rpc}: {res['error'].get('message', res['error'])}"
            continue
        raw = res.get("result")
        if raw is None or raw == "0x":
            last_err = f"{rpc}: empty result"
            continue
        return raw, ""
    return None, last_err


def eth_call(rpc: str, to: str, data: str):
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }).encode()
    req = urllib.request.Request(rpc, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def read_raw(to: str, sig: str):
    """Try each RPC until one returns a usable result. Returns raw hex or None + note."""
    data = sel(sig)
    last_err = ""
    for rpc in RPCS:
        try:
            res = eth_call(rpc, to, data)
        except Exception as e:
            last_err = f"{rpc}: {e}"
            continue
        if "error" in res:
            last_err = f"{rpc}: {res['error'].get('message', res['error'])}"
            continue
        raw = res.get("result")
        if raw is None or raw == "0x":
            last_err = f"{rpc}: empty result"
            continue
        return raw, ""
    return None, last_err


def decode_string(raw: str):
    """ABI-decode a string return value. Returns the string or None."""
    try:
        b = bytes.fromhex(raw[2:])
        if len(b) < 64:
            return None
        ln = int.from_bytes(b[32:64], "big")
        if ln < 0 or 64 + ln > len(b):
            return None
        return b[64:64 + ln].decode("utf-8", "replace")
    except Exception:
        return None


def decode_address(raw: str):
    """Last 40 hex chars of a 32-byte word, as a checksummed-ish address string."""
    try:
        return "0x" + raw[-40:]
    except Exception:
        return None


def cmd_call(to: str, sig: str):
    """One generic eth_call through this stable script. Prints raw + decoded."""
    raw, err = read_raw(to, sig)
    if raw is None:
        print(f"{sig} on {to}: FAIL ({err})")
        return
    print(f"{sig} on {to}: raw {raw}")
    s = decode_string(raw)
    if s is not None:
        print(f"   string: {s!r}")
    try:
        print(f"   uint: {int(raw, 16)}")
    except ValueError:
        pass
    if sig.rstrip("()") in ("token", "saleToken", "owner", "treasury", "vault"):
        print(f"   address: {decode_address(raw)}")


def cmd_token(addr: str):
    """Verify a token contract: token() pointer (when present), name, symbol,
    decimals, totalSupply. Used for new-project token verification (rule 5c)."""
    print(f"== token check {addr}")
    for sig in ("token()", "name()", "symbol()", "decimals()", "totalSupply()"):
        cmd_call(addr, sig)


# --- New scanner checks (2026-11-12, for the new Agentic-priority scanner) ---
PANCAKE_V2_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"


def encode_calldata(sig: str, args):
    """Selector + ABI-encoded args. Args that look like 0x addresses are
    encoded as a 32-byte word; plain integers as uint256."""
    data = sel(sig)
    for a in args:
        if isinstance(a, str) and a.startswith("0x") and len(a) == 42:
            data += "0" * 24 + a[2:].lower()
        else:
            data += format(int(a), "064x")
    return data


def read_with_args(to: str, sig: str, args):
    """eth_call with address/uint args. Returns raw hex or None + note."""
    data = encode_calldata(sig, args)
    for rpc in RPCS:
        try:
            res = eth_call(rpc, to, data)
        except Exception as e:
            last_err = f"{rpc}: {e}"
            continue
        if "error" in res:
            last_err = f"{rpc}: {res['error'].get('message', res['error'])}"
            continue
        raw = res.get("result")
        if raw is None or raw == "0x":
            last_err = f"{rpc}: empty result"
            continue
        return raw, ""
    return None, last_err


def cmd_call_args(to: str, sig: str, args):
    """call with args, e.g. balanceOf(address), allowance(address,address),
    getPair(address,address). Prints raw + decoded uint/address."""
    raw, err = read_with_args(to, sig, args)
    if raw is None:
        print(f"{sig}{tuple(args)} on {to}: FAIL ({err})")
        return
    print(f"{sig}{tuple(args)} on {to}: raw {raw}")
    try:
        print(f"   uint: {int(raw, 16)}")
    except ValueError:
        pass
    if sig.rstrip("()") in ("owner", "token", "saleToken", "treasury", "vault"):
        print(f"   address: {decode_address(raw)}")


def cmd_pair(token_a: str, token_b: str):
    """PancakeSwap V2 pair for (tokenA, tokenB) + its reserves.
    Used for the sell-path check: no pair = no executable sell quote."""
    raw, err = read_with_args(PANCAKE_V2_FACTORY, "getPair(address,address)",
                              [token_a, token_b])
    if raw is None:
        print(f"getPair({token_a},{token_b}): FAIL ({err})")
        return
    pair = decode_address(raw)
    print(f"pair: {pair}")
    if pair == "0x0000000000000000000000000000000000000000":
        print("   no pair (zero address) — no pool, no executable sell quote")
        return
    raw2, err2 = read_raw(pair, "getReserves()")
    if raw2 is None:
        print(f"getReserves on {pair}: FAIL ({err2})")
        return
    try:
        b = bytes.fromhex(raw2[2:])
        r0 = int.from_bytes(b[0:32], "big")
        r1 = int.from_bytes(b[32:64], "big")
        print(f"   reserves: {r0} / {r1}")
    except Exception as e:
        print(f"   reserves decode FAIL ({e}): raw {raw2}")


def cmd_admin(contract: str):
    """Admin-power surface: owner() + paused(). A project whose owner can
    pause/close is excluded under Rule 10(b); paused()==True means closed."""
    print(f"== admin check {contract}")
    cmd_call(contract, "owner()")
    raw, err = read_raw(contract, "paused()")
    if raw is None:
        print(f"   paused(): no such function or FAIL ({err})")
    else:
        try:
            print(f"   paused(): {bool(int(raw, 16))}")
        except ValueError:
            print(f"   paused(): raw {raw}")


def cmd_storage(contract: str, slot: str):
    """eth_getStorageAt. EIP-1967 implementation slot reveals a proxy's
    implementation address (upgrade power)."""
    raw, err = eth_rpc("eth_getStorageAt", [contract, slot, "latest"])
    if raw is None:
        print(f"storage {slot} on {contract}: FAIL ({err})")
        return
    addr = decode_address(raw)
    print(f"storage {slot} on {contract}: {raw} (address: {addr})")
def read(to: str, sig: str):
    """Try each RPC until one returns a usable uint256/bool. Returns int or None + note."""
    data = sel(sig)
    last_err = ""
    for rpc in RPCS:
        try:
            res = eth_call(rpc, to, data)
        except Exception as e:
            last_err = f"{rpc}: {e}"
            continue
        if "error" in res:
            last_err = f"{rpc}: {res['error'].get('message', res['error'])}"
            continue
        raw = res.get("result")
        if raw is None or raw == "0x":
            last_err = f"{rpc}: empty result"
            continue
        try:
            return int(raw, 16), ""
        except ValueError:
            last_err = f"{rpc}: bad hex {raw}"
            continue
    return None, last_err

CALLS = [
    # Current board projects read by default (AGC added 2026-11-20, deposit-type).
    # GEM (mining starts 2026-11-11 20:30) and EGGO (snapshot API) have no
    # mint_scan counters and are read separately.
    ("NEBULA", "0xf08ac9c91dcfc5b74eae6c87e744cfefe0d32a68",
     ["totalSupply()"]),
    ("OIL", "0x2FC1BE6675ce2dF69abCc02F5dc8c812736e9575",
     ["freeRemaining()", "freeClaims()"]),
    ("VISION", "0x5f76cab7b054e088563d5098e17bd0dc430e61ae",
     ["totalShares()", "totalFreeShares()"]),
    ("WNO", "0x8c969a4cd49e4774908e667702c3d7c3ece164d0",
     ["totalShares()"]),
    ("HI-SCORE", "0xa74081fd9bc9623a2d9321223c35b41016a7fd65",
     ["freeCount()"]),
    ("BYTE", "0x98199FDe995eA29E3BC960B338A282a7E3FeF746",
     ["freeCardsRemaining()", "cardsSold()"]),
    ("AGC", "0x6288DFe86f22B53eEB6886aE2D78941de7483743",
     ["totalShares()"]),
]

def main():
    import sys
    args = sys.argv[1:]
    if args and args[0] == "call" and len(args) >= 3:
        if len(args) == 3:
            cmd_call(args[1], args[2])
        else:
            cmd_call_args(args[1], args[2], args[3:])
        return
    if args and args[0] == "token" and len(args) == 2:
        cmd_token(args[1])
        return
    if args and args[0] == "pair" and len(args) == 3:
        cmd_pair(args[1], args[2])
        return
    if args and args[0] == "admin" and len(args) == 2:
        cmd_admin(args[1])
        return
    if args and args[0] == "storage" and len(args) == 3:
        cmd_storage(args[1], args[2])
        return
    if args and args[0] == "impl" and len(args) == 2:
        cmd_storage(args[1], EIP1967_IMPL_SLOT)
        return
    if args:
        print("usage: mint_scan.py [call <contract> <sig> [args...] | token <contract> | pair <tokenA> <tokenB> | admin <contract> | storage <contract> <slot> | impl <contract>]")
        return
    for name, addr, sigs in CALLS:
        print(f"== {name} ({addr})")
        for sig in sigs:
            v, err = read(addr, sig)
            if v is None:
                print(f"   {sig}: FAIL ({err})")
            else:
                print(f"   {sig}: {v}")

if __name__ == "__main__":
    main()
