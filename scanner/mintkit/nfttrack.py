"""NFT holdings tracker: held check + marketplace approval snapshot + floor.

Deterministic, read-only. For every NFT in minted_registry.json:
  - held: ownerOf(token_id) == wallet (or balanceOf > 0 when no token_id)
  - approvals: isApprovedForAll(wallet, operator) for the marketplace
    operators configured in scanner/nft_venues.json — this is the data
    source for the post-sale revoke checklist (user rule 2026-11-22:
    after a sale, revoke ALL approvals, then close the project).
  - floor: from nft_venues.json — "call" (single uint256-returning call,
    parsed as BNB), "manual" (last agent-verified floor, may be stale),
    or "none" (venue unconfirmed / no market found).

Writes scanner/nft_track_state.json and prints a summary JSON.
CLI (from scanner/):
  python3 -m mintkit.nfttrack [--md <path-to-write-markdown-table>]

Nothing here signs or sends. Unread chain data is reported as null,
never guessed.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mintkit import chain, registry  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
VENUES_PATH = BASE / "nft_venues.json"
STATE_PATH = BASE / "nft_track_state.json"

IS_APPROVED_FOR_ALL = "0xe985e9c5"  # isApprovedForAll(address,address)


def _load_venues():
    try:
        data = json.loads(VENUES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"wallet": None, "operators": {}, "venues": {}}


def _approval(nft, wallet, operator):
    if not operator:
        return None
    data = IS_APPROVED_FOR_ALL + chain._pad_addr(wallet) + chain._pad_addr(operator)
    res = chain.eth_call(nft, data)
    val = chain._hex_to_int(res) if res else None
    return (val == 1) if val is not None else None


def _floor(venue_cfg):
    if not venue_cfg:
        return {"source": "none", "bnb": None, "note": "no venue config"}
    src = venue_cfg.get("floor_source", "none")
    if src == "manual":
        return {"source": "manual", "bnb": venue_cfg.get("floor_bnb_manual"),
                "observed_at": venue_cfg.get("floor_observed_at"),
                "note": "agent-verified; may be stale, re-verify before acting"}
    if src == "call":
        call = venue_cfg.get("floor_call") or {}
        res = chain.eth_call(call.get("to", ""), call.get("data", ""))
        raw = chain._hex_to_int(res) if res else None
        return {"source": "call",
                "bnb": (raw / 1e18 if raw is not None else None),
                "note": "live on-chain floor call" if raw is not None else "floor call unread"}
    return {"source": "none", "bnb": None,
            "note": venue_cfg.get("floor_evidence", "venue unconfirmed / no market found")}


def track():
    cfg = _load_venues()
    wallet = cfg.get("wallet") or "<WALLET_ADDRESS>"
    operators = cfg.get("operators") or {}
    venues = cfg.get("venues") or {}
    items = []
    for entry in registry._load()["entries"]:
        if (entry.get("kind") or "").lower() != "nft":
            continue
        contract = entry.get("contract")
        token_id = entry.get("token_id", entry.get("tokenId"))
        owner = chain.owner_of(contract, token_id) if token_id is not None else None
        if owner is not None:
            held = owner.lower() == wallet.lower()
        else:
            bal = chain.erc721_balance(contract, wallet)
            held = (bal > 0) if bal is not None else None
        approvals = {name: _approval(contract, wallet, addr)
                     for name, addr in operators.items()}
        open_approvals = [n for n, v in approvals.items() if v is True]
        vcfg = venues.get((contract or "").lower())
        items.append({
            "project": entry.get("project") or (vcfg or {}).get("project"),
            "contract": contract,
            "token_id": token_id,
            "held": held,
            "venue": (vcfg or {}).get("venue_name"),
            "venue_type": (vcfg or {}).get("venue_type", "unknown"),
            "floor": _floor(vcfg),
            "approvals": approvals,
            "status": ("NOT_HELD" if held is False else
                       "APPROVAL_OPEN" if open_approvals else "HELD"),
            "pending_revoke": open_approvals,
        })
    state = {"generated_at_unix": int(time.time()), "wallet": wallet, "items": items}
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")
    return state


def to_markdown(state):
    lines = ["| 项目 | tokenId | 持有 | 卖出场馆 | 地板(BNB) | 地板来源 | 未撤授权 | 状态 |",
             "|---|---|---|---|---|---|---|---|"]
    for it in state["items"]:
        fl = it["floor"]
        lines.append("| {p} | {t} | {h} | {v} | {f} | {s} | {r} | {st} |".format(
            p=it["project"] or "?", t=it["token_id"],
            h=("是" if it["held"] else "否" if it["held"] is False else "未读到"),
            v=it["venue"] or "未确认", f=fl["bnb"] if fl["bnb"] is not None else "—",
            s=fl["source"], r=(", ".join(it["pending_revoke"]) or "无"), st=it["status"]))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    args = sys.argv[1:]
    state = track()
    if "--md" in args:
        out = Path(args[args.index("--md") + 1])
        out.write_text(to_markdown(state), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False))
