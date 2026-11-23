"""One-shot round runners so a round is one command, not six hand-built ones.

  python3 -m mintkit.round incremental     scanner.py collect/refresh/
                                           process-leads/export/status/budget
  python3 -m mintkit.round publish_checks  hidden_files mint_scan.py,
                                           liveness_check.py, risk_score.py

Each step runs to completion regardless of earlier failures; the summary is
one JSON line: {ok, mode, steps: {name: exit_code}, tail: {name: last line}}
plus, for publish_checks, highlights (liveness REPORT lines, risk scores).
Run from the scanner/ directory.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

SCANNER_DIR = Path(__file__).resolve().parent.parent
HIDDEN_DIR = SCANNER_DIR.parent / "hidden_files"
STEP_TIMEOUT = 900


def _run(cwd, argv):
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                           timeout=STEP_TIMEOUT)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode, out
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except Exception as e:
        return -2, str(e)


def _last_line(out):
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _run_steps(cwd, steps):
    codes, tails, outputs = {}, {}, {}
    for name, argv in steps:
        code, out = _run(cwd, argv)
        codes[name] = code
        tails[name] = _last_line(out)
        outputs[name] = out
    return codes, tails, outputs


def incremental():
    names = ["collect", "refresh", "process-leads", "export", "status", "budget"]
    steps = [(n, [sys.executable, "scanner.py", n]) for n in names]
    codes, tails, _ = _run_steps(SCANNER_DIR, steps)
    return {"ok": all(c == 0 for c in codes.values()), "mode": "incremental",
            "steps": codes, "tail": tails}


def publish_checks():
    # risk_score.py requires the facts file; the newest live-*.json (written
    # by the publish flow's make_live step) is passed when one exists.
    risk_argv = [sys.executable, "risk_score.py", "risk_facts.json"]
    live_files = sorted(HIDDEN_DIR.glob("live-*.json"),
                        key=lambda p: p.stat().st_mtime)
    if live_files:
        risk_argv.append(live_files[-1].name)
    steps = [("mint_scan", [sys.executable, "mint_scan.py"]),
             ("liveness_check", [sys.executable, "liveness_check.py"]),
             ("risk_score", risk_argv)]
    codes, tails, outputs = _run_steps(HIDDEN_DIR, steps)
    highlights = {
        "liveness_report": [ln.strip() for ln in
                            outputs.get("liveness_check", "").splitlines()
                            if "REPORT" in ln],
        "risk_scores": [ln.strip() for ln in
                        outputs.get("risk_score", "").splitlines()
                        if re.search(r'"score"\s*:', ln)][:30],
    }
    return {"ok": all(c == 0 for c in codes.values()), "mode": "publish_checks",
            "steps": codes, "tail": tails, "highlights": highlights}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "incremental":
        print(json.dumps(incremental(), ensure_ascii=False))
    elif mode == "publish_checks":
        print(json.dumps(publish_checks(), ensure_ascii=False))
    else:
        print(json.dumps({"error": "usage: python3 -m mintkit.round incremental|publish_checks"}))
        sys.exit(2)
