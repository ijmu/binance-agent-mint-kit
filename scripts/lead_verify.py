#!/usr/bin/env python3
"""Verify one free-mint lead against BSC, read-only, via mint_scan.py.

Input: a lead (name, sale contract, token contract, website).
Output: a verification report JSON with a conclusion:
  candidate    - passed the checks that could be run (board decision still
                 needs risk_score + liveness + the user's call)
  exclude      - fails a hard rule (not free, paused, owner can pause/close,
                 pool opens only after sellout, window closed, wrong token)
  inconclusive - too little could be read to decide (RPC unread, etc.)

This script NEVER mints, signs, transacts, or connects a wallet. Every
chain read goes through mint_scan.py's CLI (eth_call only) as a
subprocess; there is no inline chain code here. A failed read is
reported as "unread" with its note - never as proof that a function
does not exist.

Usage:
  python3 lead_verify.py --name MUSE --sale 0x... --token 0x...
                         [--site https://...] [--rules rules.json]
                         [--out report.json]

--rules is optional JSON with the off-chain facts a lead needs for the
Rule 10 checks that cannot be read on-chain, e.g.:
  {"pool_gate": "sellout_or_paid_after_2026-12-01T20:00:00+08:00",
   "paid_cap": 3000, "free_cap": 888, "tokens_per_share": 100000,
   "window_end": "2026-12-01T20:00:00+08:00",
   "expected_name": "Agent Muse", "expected_symbol": "MUSE"}
pool_gate values: "none" | "sellout" |
  "sellout_or_paid_after_<ISO datetime>" | "paid_after_<ISO datetime>".
Without --rules, gate/cap/name checks are "unknown" and the conclusion
can be at most "candidate".
"""

import argparse
import ast
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MINT_SCAN = str(HERE / "mint_scan.py")
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
USDT = "0x55d398326f99059fF775485246999027B3197955"
ZERO_WALLET = "0x1111111111111111111111111111111111111111"
ZERO_ADDR = "0x0000000000000000000000000000000000000000"
CALL_TIMEOUT = 90

# Free-progress getters, tried in order on the sale contract. Different
# contracts expose different names; absence of one is normal.
FREE_GETTERS = [
    "freeRemaining()", "freeClaims()", "totalFreeShares()",
    "totalShares()", "freeSold()", "soldShares()", "freeMinted()",
    "totalMinted()", "paidSold()", "freeClaimed()", "mintEnd()",
    "live()",
]
FEE_GETTERS = ["FREE_FEE()"]


def now_cst():
    return datetime.now(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%d %H:%M:%S")


def run_mintscan(args):
    """Run one mint_scan.py command. Returns stdout text ('' on failure)."""
    try:
        r = subprocess.run(
            [sys.executable, MINT_SCAN] + args,
            capture_output=True, text=True, timeout=CALL_TIMEOUT)
        return r.stdout or ""
    except subprocess.TimeoutExpired:
        return "TIMEOUT after %ds" % CALL_TIMEOUT
    except Exception as e:  # noqa: BLE001
        return "LOCAL ERROR (%s: %s)" % (type(e).__name__, e)


def _find(pattern, text):
    m = re.search(pattern, text, re.M)
    return m.group(1) if m else None


def parse_call(text):
    """Parse one mint_scan `call` output block into a result dict."""
    if "TIMEOUT" in text or "LOCAL ERROR" in text:
        return {"status": "unread", "note": text.strip()[:200]}
    fail = _find(r"FAIL \((.*)\)\s*$", text)
    if fail is not None:
        low = fail.lower()
        if "empty result" in low or "revert" in low:
            return {"status": "absent",
                    "note": "函数不存在或调用 revert: %s" % fail[:160]}
        return {"status": "unread", "note": "RPC 未读到: %s" % fail[:160]}
    raw = _find(r"raw (0x[0-9a-fA-F]+)", text)
    if raw is None:
        return {"status": "unread", "note": "无 raw 输出: %s" % text[:160]}
    out = {"status": "ok", "raw": raw}
    s = _find(r"^\s+string: (.*)$", text)
    if s is not None:
        try:
            out["string"] = ast.literal_eval(s.strip())
        except Exception:
            out["string"] = s.strip()
    addr = _find(r"^\s+address: (0x[0-9a-fA-F]+)", text)
    if addr is not None:
        out["address"] = addr
    # uint is only meaningful for a single 32-byte word return.
    if len(raw) == 66:
        try:
            out["uint"] = int(raw, 16)
        except ValueError:
            pass
    else:
        words = [int(raw[2 + i * 64:2 + (i + 1) * 64], 16)
                 for i in range((len(raw) - 2) // 64)]
        out["words"] = words
    return out


def call(contract, sig, args=None):
    cmd = ["call", contract, sig] + (args or [])
    return parse_call(run_mintscan(cmd))


def token_check(addr):
    out = run_mintscan(["token", addr])
    res = {}
    # Split per-signature blocks: "<sig> on <addr>: ..."
    blocks = re.split(r"(?m)^(?=\S+\(\) on 0x[0-9a-fA-F]+: )", out)
    for block in blocks:
        m = re.match(r"(\S+\(\)) on ", block)
        if not m:
            continue
        res[m.group(1)] = parse_call(block)
    return res


def admin_check(addr):
    out = run_mintscan(["admin", addr])
    owner_block = ""
    m = re.search(r"(?ms)(owner\(\) on .*?)(?=\n   paused\(\):|\Z)", out)
    if m:
        owner_block = m.group(1)
    owner = parse_call(owner_block) if owner_block else {
        "status": "unread", "note": "无 owner 输出"}
    paused_raw = _find(r"^\s+paused\(\): (True|False)\s*$", out)
    paused_note = _find(r"^\s+paused\(\): (no such function.*)$", out)
    if paused_raw is not None:
        paused = {"status": "ok", "value": paused_raw == "True"}
    elif paused_note is not None:
        # cmd_admin prints "no such function or FAIL (...)" for ANY read
        # failure. Only an empty-result/revert means the function is
        # really absent; an HTTP/RPC error means unread, not absent.
        low = paused_note.lower()
        if "empty result" in low or "revert" in low:
            paused = {"status": "absent", "note": paused_note}
        else:
            paused = {"status": "unread",
                      "note": "RPC 未读到: %s" % paused_note[:160]}
    else:
        paused = {"status": "unread",
                  "note": "无 paused 输出: %s" % out[:160]}
    return {"owner": owner, "paused": paused}


def impl_check(addr):
    out = run_mintscan(["impl", addr])
    addr_hit = _find(r"\(address: (0x[0-9a-fA-F]+)\)", out)
    if addr_hit is None:
        return {"status": "unread", "note": "无 impl 输出: %s" % out[:160]}
    if addr_hit.lower() == ZERO_ADDR:
        return {"status": "ok", "implementation": None,
                "is_proxy": False, "note": "EIP-1967 实现槽为空，非代理合约"}
    return {"status": "ok", "implementation": addr_hit,
            "is_proxy": True,
            "note": "代理合约，可升级（实现地址 %s）" % addr_hit}


def pair_check(token_a, token_b):
    out = run_mintscan(["pair", token_a, token_b])
    pair = _find(r"^pair: (0x[0-9a-fA-F]+)\s*$", out)
    if pair is None:
        return {"status": "unread", "note": "无 pair 输出: %s" % out[:200]}
    if pair.lower() == ZERO_ADDR:
        return {"status": "ok", "pair": None, "reserves": [0, 0],
                "note": "无池子（零地址）"}
    m = re.search(r"^\s+reserves: (\d+) / (\d+)\s*$", out, re.M)
    if m is None:
        return {"status": "ok", "pair": pair, "reserves": None,
                "note": "有池子地址，储备未读到"}
    r0, r1 = int(m.group(1)), int(m.group(2))
    return {"status": "ok", "pair": pair, "reserves": [r0, r1],
            "note": "池子储备 %d / %d" % (r0, r1)}


def site_check(url):
    if not url:
        return {"status": "unknown", "note": "未提供官网"}
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 free-mint-lead-verify/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read(4096)
            return {"status": "ok", "http_status": r.status,
                    "note": "官网可达 (HTTP %d)" % r.status}
    except urllib.error.HTTPError as e:
        return {"status": "fail", "http_status": e.code,
                "note": "官网返回 HTTP %d" % e.code}
    except Exception as e:  # noqa: BLE001
        return {"status": "fail", "http_status": None,
                "note": "官网不可达(%s: %s)" % (type(e).__name__, e)}


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Verify one free-mint lead against BSC (read-only).")
    p.add_argument("--name", required=True, help="项目名，如 MUSE")
    p.add_argument("--sale", required=True, help="销售/铸造合约地址")
    p.add_argument("--token", default=None, help="代币合约地址（可空，自动从销售合约读 token()）")
    p.add_argument("--site", default=None, help="官网 URL")
    p.add_argument("--rules", default=None, help="链下事实 JSON 文件（见模块 docstring）")
    p.add_argument("--out", default=None, help="报告写到此文件")
    args = p.parse_args(argv)

    rules = {}
    if args.rules:
        with open(args.rules, encoding="utf-8") as f:
            rules = json.load(f)

    report = {
        "checked_at": now_cst(),
        "lead": {"name": args.name, "sale": args.sale,
                 "token": args.token, "site": args.site},
        "blockers": [], "warnings": [], "rpc_notes": [],
        "conclusion": "inconclusive", "reasons": [],
    }

    # ---- 1. token contract (discover via sale.token() if not given) ----
    token_addr = args.token
    if not token_addr:
        t = call(args.sale, "token()")
        token_addr = t.get("address") if t.get("status") == "ok" else None
        report["token_discovered_from_sale"] = t
        if not token_addr:
            report["blockers"].append("代币合约地址无法确认（未提供且销售合约读不到 token()）")
    token_info = {}
    if token_addr:
        token_info = token_check(token_addr)
        report["token"] = {"address": token_addr, "checks": token_info}
        name = token_info.get("name()", {}).get("string")
        symbol = token_info.get("symbol()", {}).get("string")
        decimals = token_info.get("decimals()", {}).get("uint")
        supply_raw = token_info.get("totalSupply()", {}).get("uint")
        report["token"].update({"name": name, "symbol": symbol,
                               "decimals": decimals,
                               "total_supply_raw": supply_raw})
        if name is None and symbol is None and supply_raw is None:
            report["rpc_notes"].append("代币合约完全未读到（name/symbol/totalSupply 均无）")
        if rules.get("expected_name") and name and name != rules["expected_name"]:
            report["blockers"].append(
                "代币名不符：链上 %r，线索称 %r" % (name, rules["expected_name"]))
        if rules.get("expected_symbol") and symbol and \
                symbol != rules["expected_symbol"]:
            report["blockers"].append(
                "代币符号不符：链上 %r，线索称 %r" % (symbol, rules["expected_symbol"]))
        if decimals is not None and supply_raw is not None:
            human = supply_raw / (10 ** decimals)
            report["token"]["total_supply_human"] = human
            tps = rules.get("tokens_per_share")
            if tps:
                report["value_note"] = (
                    "每免费席 %s %s，占总量 %.10g%%（总量 %s）"
                    % (tps, symbol or "", tps / human * 100, human))
    else:
        report["token"] = {"address": None, "checks": {}}

    # ---- 2. admin powers (sale + token) ----
    sale_admin = admin_check(args.sale)
    report["sale_admin"] = sale_admin
    token_admin = admin_check(token_addr) if token_addr else {}
    report["token_admin"] = token_admin
    for label, adm in (("销售合约", sale_admin), ("代币合约", token_admin)):
        if not adm:
            continue
        ow = adm.get("owner", {})
        if ow.get("status") == "ok":
            addr = ow.get("address")
            if addr and addr.lower() != ZERO_ADDR:
                report["warnings"].append(
                    "%s 有 owner（%s），须确认是否有暂停/关闭/提款权" % (label, addr))
            elif addr:
                report["reasons"].append("%s owner 已放弃（零地址）" % label)
        elif ow.get("status") == "absent":
            report["reasons"].append("%s 读不到 owner（无 owner 函数）" % label)
        else:
            report["rpc_notes"].append("%s owner 未读到：%s" % (label, ow.get("note", "")))
        ps = adm.get("paused", {})
        if ps.get("status") == "ok" and ps.get("value") is True:
            report["blockers"].append("%s 已暂停（paused=true）" % label)
        elif ps.get("status") == "ok":
            report["reasons"].append("%s 未暂停" % label)
        elif ps.get("status") == "absent":
            report["reasons"].append("%s 无 paused 函数" % label)
        else:
            report["rpc_notes"].append("%s paused 未读到：%s" % (label, ps.get("note", "")))
    # Rule 10(b): owner + paused function present = owner can pause/close.
    sale_ow = sale_admin.get("owner", {})
    sale_ps = sale_admin.get("paused", {})
    if sale_ow.get("status") == "ok" and sale_ow.get("address") and \
            sale_ow["address"].lower() != ZERO_ADDR and \
            sale_ps.get("status") == "ok":
        report["blockers"].append(
            "销售合约 owner（%s）+ paused 函数并存：owner 可暂停/关闭（规则 10b）"
            % sale_ow["address"])

    # ---- 3. proxy check ----
    report["sale_proxy"] = impl_check(args.sale)
    if token_addr:
        report["token_proxy"] = impl_check(token_addr)
    for label, px in (("销售合约", report["sale_proxy"]),
                      ("代币合约", report.get("token_proxy", {}))):
        if px.get("is_proxy"):
            report["warnings"].append("%s 是代理合约，可升级" % label)

    # ---- 4. free-progress getters on the sale contract ----
    free = {}
    for sig in FREE_GETTERS:
        free[sig] = call(args.sale, sig)
    report["free"] = free

    def free_uint(sig):
        r = free.get(sig, {})
        return r.get("uint") if r.get("status") == "ok" else None

    free_remaining = free_uint("freeRemaining()")
    free_claimed = free_uint("freeClaimed()")
    free_sold = free_uint("freeSold()")
    total_free = free_uint("totalFreeShares()")
    total_shares = free_uint("totalShares()")
    paid_sold = free_uint("paidSold()")
    mint_end = free_uint("mintEnd()")
    live = free_uint("live()")

    # Reconcile free progress from whichever getters exist.
    free_cap = rules.get("free_cap")
    if free_cap is not None:
        sold = None
        if free_claimed is not None:
            sold = free_claimed
        elif free_sold is not None:
            sold = free_sold
        elif free_remaining is not None:
            sold = free_cap - free_remaining
        elif total_shares is not None and total_free is not None:
            sold = total_shares - total_free
        if sold is not None:
            report["free_progress"] = {
                "sold": sold, "cap": free_cap,
                "remaining": free_cap - sold}
            report["reasons"].append(
                "免费已领 %d/%d，剩余 %d" % (sold, free_cap, free_cap - sold))
    if paid_sold is not None:
        report["paid_progress"] = {"sold": paid_sold,
                                   "cap": rules.get("paid_cap")}
        report["reasons"].append("付费已售 %s/%s" % (
            paid_sold, rules.get("paid_cap") or "?"))
    if mint_end is not None:
        end = datetime.fromtimestamp(mint_end, tz=timezone.utc)
        report["window_end_onchain"] = end.strftime("%Y-%m-%d %H:%M:%S UTC")
        if end.timestamp() < datetime.now(timezone.utc).timestamp():
            report["blockers"].append(
                "免费窗口已结束（mintEnd %s）" % report["window_end_onchain"])
    if live is not None and live == 0:
        report["blockers"].append("live()=false，销售窗口已关")
    if rules.get("window_end"):
        wend = parse_iso(rules["window_end"])
        if wend and datetime.now(wend.tzinfo or timezone.utc) > wend:
            report["warnings"].append(
                "线索称窗口截止 %s，已过该时间" % rules["window_end"])

    # ---- 5. fees: must be 0 (free means gas only) ----
    fees = {}
    for sig in FEE_GETTERS:
        fees[sig] = call(args.sale, sig)
    fee_for = call(args.sale, "feeFor(address,uint256)",
                   [ZERO_WALLET, "1"])
    fees["feeFor(address,uint256) zero-wallet x1"] = fee_for
    report["fees"] = fees
    free_fee = fees.get("FREE_FEE()", {}).get("uint")
    if free_fee is not None and free_fee > 0:
        report["blockers"].append(
            "FREE_FEE()=%d wei/份：免费领要收费，不是只付 gas" % free_fee)
    elif free_fee == 0:
        report["reasons"].append("FREE_FEE()=0：免费领不收费")
    ff = fee_for.get("uint")
    if ff is not None and ff > 0:
        report["blockers"].append(
            "feeFor(0 付费钱包, 1 份)=%d wei：免费领要收费" % ff)
    elif ff == 0:
        report["reasons"].append("feeFor(0 付费钱包, 1 份)=0")

    # ---- 6. 0-paid-wallet quote probe (raw hex only, never decode-then-trust) ----
    quote = call(args.sale, "quote(address,uint256)", [ZERO_WALLET, "1"])
    report["quote_zero_wallet"] = quote
    if quote.get("status") == "ok":
        words = quote.get("words") or ([quote["uint"]] if "uint" in quote else [])
        report["quote_words"] = words
        if words and all(w == 0 for w in words):
            report["warnings"].append("quote(0 付费钱包) 全零：可能须先付费才给免费额度")
        elif words and words[-1] == 0:
            report["reasons"].append(
                "quote(0 付费钱包)=%s：费用为 0" % (words,))
        elif words:
            report["warnings"].append(
                "quote(0 付费钱包)=%s：末项非零，需人工确认含义" % (words,))

    # ---- 7. pool (sell path) ----
    pool = {}
    if token_addr:
        pool["wbnb"] = pair_check(token_addr, WBNB)
        pool["usdt"] = pair_check(token_addr, USDT)
    report["pool"] = pool
    pool_funded = False
    for key in ("wbnb", "usdt"):
        pc = pool.get(key, {})
        res = pc.get("reserves")
        if res and (res[0] > 0 or res[1] > 0):
            pool_funded = True
            report["reasons"].append(
                "池子 %s 已注资（储备 %d / %d）" % (key, res[0], res[1]))
    if not pool_funded:
        report["reasons"].append("池子未注资（WBNB/USDT 储备均为 0 或无池子），暂不能卖")

    # ---- 8. website ----
    report["site"] = site_check(args.site)
    if args.site and report["site"]["status"] == "fail":
        report["warnings"].append("官网不可达：%s" % report["site"]["note"])
    if not args.site:
        report["blockers"].append("无独立官网（规则 10f）")

    # ---- 9. pool gate (Rule 10c-like) ----
    gate = rules.get("pool_gate", "none")
    gate_met = None
    if gate == "none":
        gate_met = None
    elif pool_funded:
        gate_met = True
        report["reasons"].append("池子已注资，开池门槛已满足")
    elif gate == "sellout":
        sold = (report.get("free_progress") or {}).get("sold")
        gate_met = (sold is not None and free_cap is not None
                    and sold >= free_cap
                    and (rules.get("paid_cap") is None
                         or (paid_sold or 0) >= rules["paid_cap"]))
    elif gate.startswith("sellout_or_paid_after_"):
        when = parse_iso(gate[len("sellout_or_paid_after_"):])
        sold = (report.get("free_progress") or {}).get("sold")
        sellout = (sold is not None and free_cap is not None
                   and sold >= free_cap
                   and (rules.get("paid_cap") is None
                        or (paid_sold or 0) >= rules["paid_cap"]))
        paid_after = (when is not None and paid_sold is not None
                      and paid_sold >= 1
                      and datetime.now(when.tzinfo or timezone.utc) >= when)
        gate_met = bool(sellout or paid_after)
    elif gate.startswith("paid_after_"):
        when = parse_iso(gate[len("paid_after_"):])
        gate_met = (when is not None and paid_sold is not None
                    and paid_sold >= 1
                    and datetime.now(when.tzinfo or timezone.utc) >= when)
    report["pool_gate"] = {"rule": gate, "met": gate_met}
    if gate_met is False:
        report["blockers"].append(
            "开池门槛未满足（%s）：池子为空，付费已售 %s，免费已领 %s"
            % (gate, paid_sold, (report.get("free_progress") or {}).get("sold")))

    # ---- 10. conclusion ----
    if report["blockers"]:
        report["conclusion"] = "exclude"
    elif not token_addr or (
            report["token"].get("name") is None
            and report["token"].get("symbol") is None
            and report["token"].get("total_supply_raw") is None):
        report["conclusion"] = "inconclusive"
        report["reasons"].append("代币身份未读到，无法定论")
    elif all(free.get(s, {}).get("status") != "ok" for s in FREE_GETTERS) \
            and not pool_funded:
        report["conclusion"] = "inconclusive"
        report["reasons"].append("免费进度与池子均未读到，无法定论")
    else:
        report["conclusion"] = "candidate"
    if not args.rules:
        report["reasons"].append("未给 --rules：开池门槛/上限/名称检查记 unknown，结论最高 candidate")

    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
