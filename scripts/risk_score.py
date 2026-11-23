#!/usr/bin/env python3
"""Risk scoring for Binance Wallet free-mint board projects (风险评分).

Pure scoring function: takes verified facts (risk_facts.json format) plus
the current round's live signals, returns a 100-point score, an instruction
tier and the per-item deductions. No I/O, no HTTP, no chain reads -- the
live signals are measured elsewhere (liveness_check.py) and passed in.

Rubric source of truth: risk_rubric.md (100 base, deductions).
  >=80 可打 / 60-79 谨慎打 / 40-59 先不打 / <40 不打.
  ENDED or DEAD forces 0 分 / 不打 (pass verdict="ENDED"/"DEAD").

Scoring logic mirrors liveness_check.py score_project() exactly; this file
exists so the rubric can be unit-tested offline and reused by other tools
without running the live checks. If the rubric changes, update BOTH files
and re-run --selftest.

Usage:
  python3 risk_score.py <facts.json> [live.json]
      facts.json: {"NAME": {facts...}, ...} in risk_facts.json format.
      live.json (optional): {"NAME": {"site_up": bool,
          "site_down_hours": float, "stalled_hours": float,
          "movement": "moved|stalled|unknown|error|baseline",
          "cvalue": int|null, "cap": int|null,
          "pool_status": null|"EMPTY"|"NO_POOL"|"FUNDED"|"ERROR",
          "verdict": null|"ENDED"|"DEAD"}, ...}
      Missing live keys default to: site up, no stall, movement "moved",
      cvalue None, cap None, pool_status None.
      Prints one JSON object per project to stdout.

  python3 risk_score.py --selftest
      Replays the 2026-11-13 23:28 round for the 8 boarded projects with
      the live signals reconstructed from risk_report.json (same timestamp)
      and the FROZEN facts snapshot risk_facts_2026-11-13.json, and
      compares each computed score with the recorded one. Exit 0 iff all 8
      match exactly. The snapshot is frozen: it must NOT track the live
      risk_facts.json, so delisting a project (deleting its facts) can
      never break the self-test again (BUG-028).
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FACTS = os.path.join(HERE, "risk_facts.json")

SITE_EXTRA_HOURS = 2   # 官网连续失联满这么多小时，再扣 20
STALLED_HOURS = 6      # 链上进度停滞满这么多小时，扣 10


def score_project(name, facts, live=None):
    """Score one project. Returns dict with score/instruction/deductions.

    facts: risk_facts.json entry (sourcify, owner, deployer, lock, tax,
        pool_open, pool_thin, pool_hopeless, fake_progress, extras, ...).
    live: this round's measured signals (see module docstring).
    """
    live = live or {}
    site_up = live.get("site_up", True)
    site_down_hours = live.get("site_down_hours", 0.0) or 0.0
    stalled_hours = live.get("stalled_hours", 0.0) or 0.0
    movement = live.get("movement", "moved")
    cvalue = live.get("cvalue")
    cap = live.get("cap")
    pool_status = live.get("pool_status")
    verdict = live.get("verdict")

    d = []  # [{"label": str, "points": int}]

    def cut(label, pts):
        if pts:
            d.append({"label": label, "points": pts})

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
    # owner "none_verified" / "none" -> no deduction (verified no owner)
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

    score = max(0, 100 - sum(x["points"] for x in d))
    if score >= 80:
        instruction = "可打"
    elif score >= 60:
        instruction = "谨慎打"
    elif score >= 40:
        instruction = "先不打"
    else:
        instruction = "不打"
    problems = "、".join("%s(−%d)" % (x["label"], x["points"]) for x in d) or "无"
    if verdict in ("ENDED", "DEAD"):
        score, instruction = 0, "不打"
        problems = ("免费已领完" if verdict == "ENDED" else "疑似已死") + \
            ("、" + problems if problems != "无" else "")
    return {"name": name, "score": score, "instruction": instruction,
            "deductions": d, "problems": problems,
            "verdict": verdict or "OK"}


# --- self-test: replay the 2026-11-13 23:28 round ---------------------------
# Live signals reconstructed from risk_report.json (updated 2026-11-13 23:28)
# plus the round's recorded facts. Expected scores are the recorded ones.
# Facts come from the FROZEN snapshot risk_facts_2026-11-13.json, NOT from
# the live risk_facts.json (BUG-028: SOLACE/EGGO were delisted and their
# facts deleted, which broke the self-test). Snapshot provenance:
#   - NEBULA/OIL/VISION/WNO/HI-SCORE/BYTE: copied verbatim from the live
#     risk_facts.json; they still reproduce the 2026-11-13 scores exactly.
#   - SOLACE/EGGO: reconstructed from deep-research-2026-11-13.md §1/§9,
#     whose per-item deduction lists sum to the recorded scores
#     (SOLACE -45 static + site-down -10 = 45; EGGO -60 = 40).
# If the rubric changes, update BOTH this file and liveness_check.py, and
# re-freeze the snapshot only if the 2026-11-13 scores would change.
SELFTEST_FACTS = os.path.join(HERE, "risk_facts_2026-11-13.json")
SELFTEST_LIVE = {
    # site down since 23:28:32, ~1h at scoring time (< 2h, no extra -20);
    # chain still moving (progress 90, +1).
    "SOLACE": {"site_up": False, "site_down_hours": 1.0, "movement": "moved",
               "cvalue": 90, "cap": 4444},
    "NEBULA": {"site_up": True, "movement": "moved",
               "cvalue": 1898, "cap": 5000},
    "OIL": {"site_up": True, "movement": "moved",
            "cvalue": 351, "cap": 1125},
    "VISION": {"site_up": True, "movement": "moved",
               "cvalue": 2149, "cap": 100000},
    "WNO": {"site_up": True, "movement": "moved",
            "cvalue": 1755, "cap": 3000},
    "HI-SCORE": {"site_up": True, "movement": "moved",
                 "cvalue": 1117, "cap": 10000},
    "EGGO": {"site_up": True, "movement": "moved",
             "cvalue": 401, "cap": 1000},
    # stalled ~19h by then (>= 6h -> -10, once, not per 6h).
    "BYTE": {"site_up": True, "movement": "stalled", "stalled_hours": 19.0,
             "cvalue": 1683, "cap": 5000},
}
SELFTEST_EXPECTED = {
    "SOLACE": 45, "NEBULA": 70, "OIL": 65, "VISION": 85,
    "WNO": 80, "HI-SCORE": 65, "EGGO": 40, "BYTE": 45,
}


def run_selftest():
    try:
        with open(SELFTEST_FACTS, encoding="utf-8") as f:
            facts_all = json.load(f)
    except Exception as e:
        print("selftest: cannot load %s: %s" % (SELFTEST_FACTS, e))
        return 1
    ok = True
    for name, expected in SELFTEST_EXPECTED.items():
        facts = facts_all.get(name)
        if facts is None:
            print("FAIL %s: no facts in %s" % (name, SELFTEST_FACTS))
            ok = False
            continue
        got = score_project(name, facts, SELFTEST_LIVE[name])
        if got["score"] != expected:
            ok = False
            print("FAIL %s: got %d, expected %d | deductions: %s" % (
                name, got["score"], expected,
                "、".join("%s(−%d)" % (x["label"], x["points"])
                          for x in got["deductions"])))
        else:
            print("PASS %s: %d分 %s | %s" % (
                name, got["score"], got["instruction"], got["problems"]))
    print("selftest: %s (%d/%d)" % (
        "ALL PASS" if ok else "MISMATCH",
        sum(1 for n, e in SELFTEST_EXPECTED.items()
            if n in facts_all and
            score_project(n, facts_all[n], SELFTEST_LIVE[n])["score"] == e),
        len(SELFTEST_EXPECTED)))
    return 0 if ok else 1


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv):
    if argv and argv[0] == "--selftest":
        return run_selftest()
    if not argv:
        print("usage: risk_score.py <facts.json> [live.json] | risk_score.py --selftest",
              file=sys.stderr)
        return 2
    try:
        facts_all = load_json(argv[0])
    except Exception as e:
        print("cannot load facts %s: %s" % (argv[0], e), file=sys.stderr)
        return 2
    live_all = {}
    if len(argv) > 1:
        try:
            live_all = load_json(argv[1])
        except Exception as e:
            print("cannot load live signals %s: %s" % (argv[1], e),
                  file=sys.stderr)
            return 2
    out = []
    for name, facts in facts_all.items():
        out.append(score_project(name, facts, live_all.get(name, {})))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
