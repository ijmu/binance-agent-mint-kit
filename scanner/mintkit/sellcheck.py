"""Sell-method verification checklist generator for NFT candidates.

Implements the deterministic half of the user rule (2026-11-22): before
minting any NFT, know where and how it can be sold; if the sell path is
unknown, learn it first; safety before profit.

Auto-filled here (read-only): Sourcify match, GoPlus security, on-chain
totalSupply and this wallet's balance. Left as explicit agent_required
items (never faked): venue identification, floor observation, listing
method, extra sell fees — each with where to verify it.

CLI (from scanner/):
  python3 -m mintkit.sellcheck <nft_contract> [--venue <name>]
      [--floor <bnb>] [--site <url>] [--out <checklist.md>]

Verdict is READY only when the contract is Sourcify-matched AND the
agent has supplied a confirmed venue and an observed floor.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mintkit import chain, verify  # noqa: E402

TOTAL_SUPPLY = "0x18160ddd"  # totalSupply()
WALLET = "<WALLET_ADDRESS>"


def build_checklist(contract, venue=None, floor=None, site=None):
    sc = verify.sourcify_check(contract)
    gp = verify.goplus_token_security(contract)
    supply_raw = chain.eth_call(contract, TOTAL_SUPPLY)
    supply = chain._hex_to_int(supply_raw) if supply_raw else None
    balance = chain.erc721_balance(contract, WALLET)
    items = [
        {"id": "sourcify_match", "status": "done" if sc.get("status") == "ok" else "unread",
         "detail": sc},
        {"id": "goplus_security", "status": "done" if gp.get("status") == "ok" else "unread",
         "detail": gp},
        {"id": "chain_state", "status": "done" if supply is not None else "unread",
         "detail": {"total_supply": supply, "wallet_balance": balance}},
        {"id": "venue_identified",
         "status": "done" if venue else "agent_required",
         "detail": venue or ("Confirm the first-party sell venue: project built-in market "
                             "contract (from official site config/docs), or an Element / "
                             "OpenSea collection page for this exact contract. Record it in "
                             "scanner/nft_venues.json.")},
        {"id": "floor_observed",
         "status": "done" if floor is not None else "agent_required",
         "detail": floor if floor is not None else
                 "Read live listings at the venue and record the minimum active price (BNB)."},
        {"id": "sell_method",
         "status": "agent_required",
         "detail": ("Confirm the concrete listing method (built-in market list call / "
                    "Element API — needs a free developer key / OpenSea — needs an API key), "
                    "its gas cost and fee rate. baw sign-message covers EIP-712 listing "
                    "signatures; every external signature needs a user-confirmed preview.")},
        {"id": "extra_fees",
         "status": "agent_required",
         "detail": ("Total sell-side deductions (marketplace fee + royalty + any sell tax) "
                    "must be known before minting; if the mint itself needs an approval or "
                    "charges anything beyond gas, do not mint (user rule 2026-11-22).")},
        {"id": "post_sale_revoke", "status": "info",
         "detail": ("After a sale completes: revoke ALL approvals for that project/market "
                    "(setApprovalForAll -> false, approve -> 0), verify with "
                    "python3 -m mintkit.nfttrack (status must show no APPROVAL_OPEN), "
                    "then close the project.")},
    ]
    missing = [i["id"] for i in items if i["status"] == "agent_required"]
    ready = (sc.get("status") == "ok" and sc.get("match") not in (None, "none")
             and venue and floor is not None)
    return {"contract": contract, "site": site, "generated_at_unix": int(time.time()),
            "items": items, "missing": missing,
            "verdict": "READY" if ready else "INCOMPLETE"}


def to_markdown(cl):
    lines = [f"## 卖法核实清单 — {cl['contract']}",
             f"- 结论：**{cl['verdict']}**（未齐项：{', '.join(cl['missing']) or '无'}）"]
    for i in cl["items"]:
        lines.append(f"- [{i['status']}] {i['id']}：{json.dumps(i['detail'], ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(json.dumps({"error": "usage: sellcheck <contract> [--venue X] [--floor BNB] [--site URL] [--out file]"}))
        sys.exit(2)
    def opt(name):
        return args[args.index(name) + 1] if name in args else None
    floor_arg = opt("--floor")
    cl = build_checklist(args[0], venue=opt("--venue"),
                         floor=(float(floor_arg) if floor_arg else None),
                         site=opt("--site"))
    if opt("--out"):
        Path(opt("--out")).write_text(to_markdown(cl), encoding="utf-8")
    print(json.dumps(cl, ensure_ascii=False))
