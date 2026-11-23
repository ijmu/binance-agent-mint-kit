"""Contract verification helpers: Sourcify (source match) + GoPlus (security).

Both are read-only public APIs. Anything unreachable is reported as
{"status": "unread"} — never guessed, never fabricated.

出处 / Sources:
- Sourcify: 以太坊官方生态源码验证服务 https://sourcify.dev
  （文档 https://docs.sourcify.dev ，v2 API: sourcify.dev/server/v2）。
- GoPlus Security: token security API，GoPlus Labs 官方公开接口
  https://api.gopluslabs.io （文档 https://docs.gopluslabs.io ）。

CLI: python3 -m mintkit.verify <address>   (run from the scanner/ directory)
"""
import json
import sys
import urllib.request

_UA = {"User-Agent": "mintkit/0.1 (+local read-only)"}


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def sourcify_check(address, chain_id=56):
    """Sourcify v2 lookup. NOTE: api.sourcify.dev does not resolve on this
    host — the working path is sourcify.dev/server/v2."""
    url = f"https://sourcify.dev/server/v2/contract/{chain_id}/{address}"
    try:
        data = _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "ok", "match": "none", "creation_match": None,
                    "runtime_match": None}
        return {"status": "unread", "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"status": "unread", "error": str(e)}
    if not isinstance(data, dict):
        return {"status": "unread", "error": "unexpected response"}
    return {
        "status": "ok",
        "match": data.get("match") or "none",
        "creation_match": data.get("creationMatch"),
        "runtime_match": data.get("runtimeMatch"),
        "verified_at": data.get("verifiedAt"),
    }


_GOPLUS_FIELDS = (
    "is_honeypot", "buy_tax", "sell_tax", "is_open_source", "is_proxy",
    "hidden_owner", "can_take_back_ownership", "holder_count", "is_mintable",
    "is_blacklisted", "cannot_buy", "cannot_sell_all", "is_in_dex",
    "owner_address", "creator_address",
)


def goplus_token_security(address, chain_id=56):
    """GoPlus token security (public, no key). Extracts the key fields."""
    url = (f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
           f"?contract_addresses={address}")
    try:
        data = _get_json(url)
    except Exception as e:
        return {"status": "unread", "error": str(e)}
    result = (data or {}).get("result") or {}
    entry = result.get(address.lower()) or result.get(address)
    if not entry:
        return {"status": "unread", "error": "no result for address"}
    out = {"status": "ok"}
    for f in _GOPLUS_FIELDS:
        if f in entry:
            out[f] = entry[f]
    return out


def check(address):
    return {"address": address,
            "sourcify": sourcify_check(address),
            "goplus": goplus_token_security(address)}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage: python3 -m mintkit.verify <address>"}))
        sys.exit(2)
    print(json.dumps(check(sys.argv[1]), ensure_ascii=False))
