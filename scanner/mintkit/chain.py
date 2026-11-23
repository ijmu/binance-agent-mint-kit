"""BSC chain reads with RPC rotation. Standard library only.

Every public function returns None when all endpoints fail ("unread");
nothing here raises for network/RPC errors, and nothing signs or sends.

出处 / Sources: BNB Chain 公共 JSON-RPC 节点（BNB Chain 生态公共节点，
文档 https://docs.bnbchain.org/ ）——publicnode（PublicNode 公共节点）、
bsc-dataseed（BNB Chain 官方 dataseed 节点）。
"""
import json
import urllib.request

RPCS = [
    "https://bsc.publicnode.com",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed.binance.org",  # known to 403 from here; last-resort fallback
]

_UA = {"User-Agent": "mintkit/0.1 (+local read-only)", "Content-Type": "application/json"}

BALANCE_OF = "0x70a08231"  # balanceOf(address) — same selector for ERC-20 and ERC-721
OWNER_OF = "0x6352211e"    # ownerOf(uint256)


def _rpc(method, params, timeout=15):
    """One JSON-RPC call, rotating endpoints. Returns the result or None."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params}).encode()
    for url in RPCS:
        try:
            req = urllib.request.Request(url, data=payload, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            if isinstance(data, dict) and data.get("result") is not None:
                return data["result"]
        except Exception:
            continue
    return None


def _pad_addr(address):
    return address.lower().replace("0x", "").rjust(64, "0")


def _hex_to_int(value):
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return None


def eth_call(to, data):
    """eth_call at 'latest'. Returns the raw hex result string or None."""
    return _rpc("eth_call", [{"to": to, "data": data}, "latest"])


def get_receipt(txhash):
    """eth_getTransactionReceipt. Returns the receipt dict or None."""
    return _rpc("eth_getTransactionReceipt", [txhash])


def erc20_balance(token, wallet):
    """ERC-20 balanceOf(wallet) as int (raw units), or None if unread."""
    res = eth_call(token, BALANCE_OF + _pad_addr(wallet))
    return _hex_to_int(res) if res else None


def erc721_balance(nft, wallet):
    """ERC-721 balanceOf(wallet) as int, or None if unread."""
    res = eth_call(nft, BALANCE_OF + _pad_addr(wallet))
    return _hex_to_int(res) if res else None


def owner_of(nft, token_id):
    """ERC-721 ownerOf(token_id) as a 0x address, or None if unread."""
    data = OWNER_OF + format(int(token_id), "064x")
    res = eth_call(nft, data)
    if not res or len(res) < 42:
        return None
    return "0x" + res[-40:]


def gas_cost_bnb(receipt):
    """gasUsed * gas price, in BNB (float), from a receipt dict. None if n/a."""
    if not receipt:
        return None
    used = _hex_to_int(receipt.get("gasUsed"))
    price = _hex_to_int(receipt.get("effectiveGasPrice") or receipt.get("gasPrice"))
    if used is None or price is None:
        return None
    return used * price / 1e18


if __name__ == "__main__":
    wallet = "<WALLET_ADDRESS>"
    agc = "0x63dea5acdbe1f27c968907d08610a5f0a0284ba6"
    sudo_tx = "<TX_HASH>"
    bal = erc20_balance(agc, wallet)
    rcpt = get_receipt(sudo_tx)
    print(json.dumps({
        "agc_balance_raw": bal,
        "agc_balance_tokens": (bal / 1e18 if bal is not None else None),
        "sudosaints_receipt_status": (rcpt.get("status") if rcpt else None),
        "sudosaints_gas_bnb": gas_cost_bnb(rcpt),
    }, ensure_ascii=False))
