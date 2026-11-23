#!/usr/bin/env python3
"""Board project liveness re-verification + risk scoring (存活复核 + 风险评分).

Built 2026-11-13 at the user's request, after GEM's website went down and
no scan round had noticed: the hourly scan only refreshed progress numbers
and never checked whether a project was still alive. Risk scoring added the
same day: the board the executor reads must tell it plainly whether to mint, so
it stops waiting blindly (e.g. GEM mining with the website down).

Read-only. For each board project it checks TWO live signals, then scores:
  1. Website: HTTP status of the project homepage (urllib, 20s timeout).
  2. Chain: the project's progress counter via mint_scan.py (or the EGGO
     snapshot API), compared with last round's value from the state file.

Time-based, not round-based: streaks are measured in HOURS (site_down_since
/ last_change_at timestamps), because a slow project can legitimately show
no progress for hours, and manual test runs minutes apart must not count
as separate rounds. (First version counted rounds and wrongly flagged
every slow project as "stalled" -10 and could never declare GEM dead,
because token_alive reset the streak every round. Fixed 2026-11-13.)

Verdicts (also see the hourly-scan cron, steps 12-13):
  OK    - site up, chain moving/alive. Nothing to do.
  WARN  - site down but chain still moving: keep the project, flag once.
          Or site down + chain stalled/unverifiable but under the DEAD
          threshold: keep, count the time.
  DEAD  - site continuously down >= 3h AND chain shows no movement
          (unverifiable, or stalled >= 6h): suspected dead, remove from
          board, report once with evidence.
  ENDED - progress reached its cap / free allocation exhausted: mint over,
          remove (same as scan rule 6).
  POOL_EMPTY - pool-opened project (window ended) whose PancakeSwap pair has
          no reserves: keep on board, mark "池子未注资、暂不能卖", report once.

RPC/API failures (BSC public RPC 403s are frequent) are reported as
chain=error and NEVER count as stalled: no removal on unreadable chain data.

Risk score: 100 base, deductions per the rubric in risk_rubric.md.
Static project facts live in risk_facts.json (update it when a project is
researched/added; the scan worker updates facts when findings change).
  >=80 可打 / 60-79 谨慎打 / 40-59 先不打 / <40 不打.
  ENDED or DEAD forces 0 分 / 不打.

Usage: python3 liveness_check.py
  Prints one line per project + a RISK section + a REPORT section
  (new events only). Writes risk_report.json (machine-readable, for the
  scan worker to copy scores into the board).
  State lives in liveness_state.json next to this script. Do not hand-edit it.
  When the board gains/loses a project, update PROJECTS below and
  risk_facts.json in the same edit.
  Exit code is always 0; verdicts are in the output, not the exit code.
"""

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "liveness_state.json")
FACTS_FILE = os.path.join(HERE, "risk_facts.json")
RISK_REPORT_FILE = os.path.join(HERE, "risk_report.json")
MINT_SCAN = os.path.join(HERE, "mint_scan.py")
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
SITE_TIMEOUT = 20
CHAIN_TIMEOUT = 60
# Hours of continuous site outage before DEAD (with no chain movement).
DEAD_SITE_HOURS = 3
# Hours of no chain movement before it counts as "stalled" (slow projects
# legitimately show no movement for hours).
STALLED_HOURS = 6
# Extra site deduction once the outage reaches this many hours.
SITE_EXTRA_HOURS = 2
CST = timezone(timedelta(hours=8))

# name, homepage, chain check, cap, pool_check_after (CST, "" = none)
# chain check kinds:
#   ("supply_div", token, divisor)  -> totalSupply / divisor (whole tokens)
#   ("call", contract, sig)         -> mint_scan.py call, parse uint
#   ("eggo",)                       -> EGGO snapshot API, global.eggShares
#   ("token_alive", token)          -> token contract responds; mining
#                                      progress itself is NOT measurable,
#                                      so chain movement is always "unknown"
#   ("site_only",)                  -> no chain signal
PROJECTS = [
    {"name": "ECHO", "site": "https://echoonbsc.github.io/echo",
     "chain": ("call", "0x05b02a9fd0b4f58bf013b4137d84593e7a0e6546", "freeSold()"),
     "cap": 600, "pool_after": ""},
    {"name": "HARBOR", "site": "https://harbor-hrbr.github.io/harbor/",
     "chain": ("call", "0xa5844f0539e289c9a477672818ee263ef862b517", "freeSold()"),
     "cap": 1500, "pool_after": "2026-11-17 16:00"},
    {"name": "NEBULA", "site": "https://nebula-neb.pages.dev",
     "chain": ("call", "0xf08ac9c91dcfc5b74eae6c87e744cfefe0d32a68", "totalFreeShares()"),
     "cap": 5000, "pool_after": ""},
    {"name": "OIL", "site": "https://oil-woad.vercel.app",
     "chain": ("call", "0x2FC1BE6675ce2dF69abCc02F5dc8c812736e9575", "freeClaims()"),
     "cap": 1125, "pool_after": ""},
    {"name": "VISION", "site": "https://vision-vsn.pages.dev",
     "chain": ("call", "0x5f76cab7b054e088563d5098e17bd0dc430e61ae", "totalShares()"),
     "cap": 100000, "pool_after": ""},
    {"name": "WNO", "site": "https://war-need-over.github.io/war-need-over/",
     "chain": ("call", "0x8c969a4cd49e4774908e667702c3d7c3ece164d0", "totalFreeShares()"),
     "cap": 3000, "pool_after": ""},
    {"name": "HI-SCORE", "site": "https://insert1up.fun/",
     "chain": ("call", "0xa74081fd9bc9623a2d9321223c35b41016a7fd65", "freeCount()"),
     "cap": 10000, "pool_after": ""},
    {"name": "BYTE", "site": "https://byte-fair-mint.pages.dev",
     "chain": ("call", "0x98199FDe995eA29E3BC960B338A282a7E3FeF746", "cardsSold()"),
     "cap": 5000, "pool_after": ""},
    {"name": "AGC", "site": "https://agcc.fun",
     "chain": ("call", "0x6288DFe86f22B53eEB6886aE2D78941de7483743", "totalShares()"),
     "cap": 1650000, "pool_after": "2026-11-06 00:00"},
]

# token address per project, for the pool check (pair token+WBNB)
POOL_TOKEN = {"HARBOR": "0xa5844f0539e289c9a477672818ee263ef862b517",
              "AGC": "0x63dea5aCdbE1F27c968907D08610A5f0A0284bA6"}

MOVE_DISPLAY = {"moved": "在动", "stalled": "停滞", "unknown": "无法验证",
                "baseline": "基线", "error": "未读到"}


def check_site(url):
    """Return (up: bool, detail: str)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=SITE_TIMEOUT) as r:
            return True, "HTTP %s" % r.status
    except urllib.error.HTTPError as e:
        return False, "HTTP %s" % e.code
    except urllib.error.URLError as e:
        return False, "连接失败(%s)" % (e.reason or "unknown")
    except Exception as e:
        return False, "连接失败(%s)" % e


def run_mint(args):
    try:
        p = subprocess.run([sys.executable, MINT_SCAN] + args,
                           capture_output=True, text=True, timeout=CHAIN_TIMEOUT)
        return p.stdout + p.stderr
    except Exception as e:
        return "ERROR: %s" % e


def parse_uint_after(out, label):
    """First 'uint: N' line in the block that starts with label."""
    lines = out.splitlines()
    in_block = False
    for ln in lines:
        if ln.startswith(label):
            in_block = True
            continue
        if in_block:
            if ln.strip().startswith("uint:"):
                try:
                    return int(ln.split("uint:")[1].strip())
                except ValueError:
                    return None
            if ln and not ln.startswith(" ") and not ln.startswith("raw"):
                return None
    return None


def check_chain(spec):
    """Return (status, value, detail).
    status: value/unknown/error. value: progress int or None."""
    kind = spec[0]
    if kind == "site_only":
        return "unknown", None, "无链上进度指标"
    if kind == "token_alive":
        # Token responding proves the token exists, NOT that mining works.
        # Mining progress is not measurable on-chain, so status is always
        # "unknown": a down website + this status still counts toward DEAD.
        out = run_mint(["token", spec[1]])
        if "string: '" in out:
            return "unknown", None, "代币合约可读，挖矿进度无法验证"
        if "403" in out or "Remote end closed" in out or "ERROR" in out:
            return "error", None, "RPC 读失败"
        return "unknown", None, "代币合约无响应"
    if kind == "supply_div":
        out = run_mint(["token", spec[1]])
        raw = parse_uint_after(out, "totalSupply()")
        if raw is None:
            return "error", None, "totalSupply 读失败(RPC)"
        return "value", raw // (10 ** 18) // spec[2], "totalSupply 折算"
    if kind == "call":
        out = run_mint(["call", spec[1], spec[2]])
        v = parse_uint_after(out, spec[2])
        if v is None:
            return "error", None, "%s 读失败(RPC)" % spec[2]
        return "value", v, spec[2]
    if kind == "eggo":
        try:
            req = urllib.request.Request(
                "https://eggo-awr.pages.dev/api/eggo/snapshot",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=SITE_TIMEOUT) as r:
                d = json.loads(r.read().decode())
            if not d.get("ok"):
                return "error", None, "快照 API 返回 ok=false"
            return "value", int(d["global"]["eggShares"]), "snapshot eggShares"
        except Exception as e:
            return "error", None, "快照 API 读失败(%s)" % e
    return "unknown", None, "未知检查类型"


def check_pool(token):
    """PancakeSwap V2 pair reserves for token/WBNB.
    Returns (status, detail): NO_POOL / EMPTY / FUNDED / ERROR."""
    out = run_mint(["pair", token, WBNB])
    if "no pair" in out:
        return "NO_POOL", "无 PancakeSwap V2 池子"
    if "FAIL" in out or "ERROR" in out:
        return "ERROR", "池子读取失败(RPC)"
    uints = []
    for ln in out.splitlines():
        if ln.strip().startswith("uint:"):
            try:
                uints.append(int(ln.split("uint:")[1].strip()))
            except ValueError:
                pass
    if len(uints) < 2:
        return "ERROR", "储备读数不完整"
    if uints[0] == 0 and uints[1] == 0:
        return "EMPTY", "池子储备 0/0，未注资"
    return "FUNDED", "池子储备 %s / %s" % (uints[0], uints[1])


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def score_project(name, facts, site_up, site_down_hours, stalled_hours,
                  movement, cvalue, cap, pool_status):
    """100 base, deductions per risk_rubric.md. Returns (score, instruction,
    deductions: [(label, points)]). ENDED/DEAD are forced to 0/不打 by caller."""
    d = []  # (label, points deducted)

    def cut(label, pts):
        if pts:
            d.append((label, pts))

    # --- live signals ---
    if not site_up:
        cut("官网失联", 30 if facts.get("mint_needs_site") else 10)
        if site_down_hours >= SITE_EXTRA_HOURS:
            cut("官网连续失联", 20)
    if stalled_hours >= STALLED_HOURS:
        cut("链上进度停滞", 10)
    elif movement == "error":
        cut("链上未读到", 5)
    # --- static facts ---
    if not facts.get("sourcify", False):
        cut("源码未验证", 10)
    owner = facts.get("owner", "unconfirmed")
    if owner == "unconfirmed":
        cut("owner权限未确认", 10)
    elif owner == "claimed_none":
        cut("官网称无owner但未验证", 5)
    elif owner == "bounded":
        cut("owner权限有界但可调参", 5)
    elif owner == "upgradeable":
        cut("合约可升级、控制权未说明", 10)
    deployer = facts.get("deployer", "unknown")
    if deployer == "unknown":
        cut("部署者未核实", 5)
    elif deployer == "fresh":
        cut("部署者新钱包", 10)
    if facts.get("fake_progress"):
        cut("进度独立钱包<20", 10)
    lock = facts.get("lock", "unknown")
    if lock == "until_pool":
        cut("锁仓到开池", 5)
    elif lock == "unknown":
        cut("锁仓情况未确认", 5)
    tax = facts.get("tax", "unknown")
    if tax == "unknown":
        cut("买卖税未知", 5)
    elif isinstance(tax, (int, float)) and tax >= 2:
        cut("买卖税%s%%" % tax, 5)
    # --- pool / sellability ---
    if pool_status in ("EMPTY", "NO_POOL"):
        cut("池子未注资", 30)
    elif pool_status == "FUNDED":
        pass
    elif facts.get("pool_open"):
        pass  # pool exists already, no waiting deduction
    else:
        cut("未开池", 5)
    if facts.get("pool_thin"):
        cut("池子极薄、暂卖不掉", 5)
    if facts.get("pool_hopeless"):
        cut("开池条件无望", 10)
    for extra in facts.get("extras", []):
        cut(extra, 5)
    # --- scarcity ---
    if (isinstance(cvalue, int) and isinstance(cap, int) and cap > 0
            and (cap - cvalue) / cap < 0.10):
        cut("免费剩余不足10%", 5)

    score = max(0, 100 - sum(pts for _, pts in d))
    if score >= 80:
        instruction = "可打"
    elif score >= 60:
        instruction = "谨慎打"
    elif score >= 40:
        instruction = "先不打"
    else:
        instruction = "不打"
    return score, instruction, d


def main():
    now = datetime.now(CST)
    state = load_json(STATE_FILE, {"projects": {}})
    facts_all = load_json(FACTS_FILE, {})
    st = state.setdefault("projects", {})
    report = []
    risk_entries = []
    counts = {"OK": 0, "WARN": 0, "DEAD": 0, "ENDED": 0}

    for p in PROJECTS:
        name = p["name"]
        facts = facts_all.get(name, {})
        prev = st.get(name, {})
        site_up, site_detail = check_site(p["site"])
        cstatus, cvalue, cdetail = check_chain(p["chain"])

        # --- time-based tracking ---
        if site_up:
            site_down_since = None
            site_down_hours = 0.0
        else:
            site_down_since = parse_ts(prev.get("site_down_since") or "") or now
            site_down_hours = (now - site_down_since).total_seconds() / 3600.0

        # Chain movement vs last round (only for numeric progress).
        if cstatus == "value":
            last = prev.get("last_progress")
            if last is None:
                movement = "baseline"  # first run: no baseline yet
                last_change_at = now
            elif cvalue > last:
                movement = "moved"
                last_change_at = now
            else:
                movement = "stalled"
                last_change_at = parse_ts(prev.get("last_change_at") or "") or now
        elif cstatus == "error":
            movement = "error"
            last_change_at = parse_ts(prev.get("last_change_at") or "") or now
        else:
            movement = "unknown"
            last_change_at = parse_ts(prev.get("last_change_at") or "") or now
        stalled_hours = (now - last_change_at).total_seconds() / 3600.0

        ended = (cstatus == "value" and p["cap"] is not None
                 and cvalue >= p["cap"])
        is_dead = (not site_up and site_down_hours >= DEAD_SITE_HOURS
                   and (movement == "unknown" or stalled_hours >= STALLED_HOURS))

        # Pool check for window-ended projects.
        pool_status = None
        pool_note = ""
        if p["pool_after"]:
            try:
                after = datetime.strptime(p["pool_after"], "%Y-%m-%d %H:%M").replace(tzinfo=CST)
            except ValueError:
                after = None
            if after and now >= after and name in POOL_TOKEN:
                pstatus, pdetail = check_pool(POOL_TOKEN[name])
                pool_status = pstatus
                if pstatus in ("EMPTY", "NO_POOL"):
                    pool_note = " | 池子: %s" % pdetail
                    if not prev.get("pool_warned"):
                        report.append("%s 池子未注资、暂不能卖 (%s)。" % (name, pdetail))
                        prev["pool_warned"] = True
                elif pstatus == "FUNDED":
                    pool_note = " | 池子: %s" % pdetail
                    prev["pool_warned"] = False
                else:
                    pool_note = " | 池子: 未读到"

        if ended:
            verdict = "ENDED"
        elif is_dead:
            verdict = "DEAD"
        elif not site_up:
            verdict = "WARN"
        else:
            verdict = "OK"
        counts[verdict] += 1

        # --- risk score ---
        score, instruction, deductions = score_project(
            name, facts, site_up, site_down_hours, stalled_hours,
            movement, cvalue if cstatus == "value" else None, p["cap"], pool_status)
        if verdict in ("ENDED", "DEAD"):
            score, instruction = 0, "不打"
        problems = "、".join("%s(−%d)" % (label, pts) for label, pts in deductions) or "无"
        if verdict in ("ENDED", "DEAD"):
            problems = ("免费已领完" if verdict == "ENDED" else "疑似已死") + \
                ("、" + problems if problems != "无" else "")
        risk_entries.append({
            "name": name, "score": score, "instruction": instruction,
            "problems": problems, "verdict": verdict,
            "progress": cvalue if cstatus == "value" else None,
        })

        # Report only NEW events (a WARN/DEAD/ENDED the worker hasn't acted on).
        prev_verdict = prev.get("last_verdict")
        if verdict in ("DEAD", "ENDED") and prev_verdict != verdict:
            if verdict == "DEAD":
                report.append("%s 疑似已死：官网已连续失联 %.1f 小时(%s)，链上%s；请从看板撤下。"
                              % (name, site_down_hours, site_detail, cdetail))
            else:
                report.append("%s 免费已领完/进度到顶(进度 %s)，请从看板撤下。" % (name, cvalue))
        elif verdict == "WARN" and prev_verdict != "WARN":
            if movement == "moved":
                report.append("%s 官网失联(%s)，但链上仍在打，保留观察。"
                              % (name, site_detail))
            elif movement == "error":
                report.append("%s 官网失联(%s)，链上未读到(RPC)，暂保留、继续计数。"
                              % (name, site_detail))
            elif movement == "stalled":
                report.append("%s 官网失联(%s)，链上进度已停滞 %.1f 小时，暂保留、继续计数。"
                              % (name, site_detail, stalled_hours))
            else:
                report.append("%s 官网失联(%s)，链上进度无法验证(已连续失联 %.1f 小时)，暂保留、继续计数。"
                              % (name, site_detail, site_down_hours))

        print("%s | 网站=%s(%s) | 链上=%s%s | 判定=%s | 风险 %d分·%s | 问题: %s%s" % (
            name, "正常" if site_up else "打不开", site_detail,
            MOVE_DISPLAY.get(movement, movement),
            (" %s" % cvalue) if cvalue is not None else "",
            verdict, score, instruction, problems, pool_note))

        st[name] = {
            "last_progress": cvalue if cstatus == "value" else prev.get("last_progress"),
            "last_change_at": last_change_at.isoformat(),
            "site_down_since": site_down_since.isoformat() if site_down_since else None,
            "last_verdict": verdict,
            "pool_status": pool_status,
            "pool_warned": prev.get("pool_warned", False),
            "checked_at": now.strftime("%Y-%m-%d %H:%M"),
        }

    state["updated"] = now.strftime("%Y-%m-%d %H:%M")
    save_json(STATE_FILE, state)
    save_json(RISK_REPORT_FILE, {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "rubric": "100满分扣分制；≥80可打、60-79谨慎打、40-59先不打、<40不打；ENDED/DEAD为0分不打",
        "projects": risk_entries,
    })
    print("SUMMARY: OK %d, WARN %d, DEAD %d, ENDED %d" % (
        counts["OK"], counts["WARN"], counts["DEAD"], counts["ENDED"]))
    print("RISK:")
    for e in risk_entries:
        print(" - %s %d分 %s | %s" % (e["name"], e["score"], e["instruction"], e["problems"]))
    if report:
        print("REPORT:")
        for r in report:
            print(" - " + r)
    else:
        print("REPORT: 无新事件")


if __name__ == "__main__":
    main()
