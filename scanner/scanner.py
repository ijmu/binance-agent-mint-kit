#!/usr/bin/env python3
"""Agentic-priority free-mint scanner (MVP).

Local, low-model-consumption scanner for Binance Agentic Wallet / Agent ID
token projects. Python + SQLite + cron. Reuses the existing free-mint board
for handoff. (Project updates are published to PROJECTS.md in the public
repo ijmu/binance-agent-mint-kit.)

Scope (per the 2026-11-12 spec, which replaces the old hourly-scan rules):
- ONLY projects with priority for Agentic Wallet / Agent ID users
  (exclusive eligibility, priority participation, or exclusive allocation
  stage), evidenced by an official announcement, official docs, or on-chain
  eligibility rules. Merely saying "supports Agentic Wallet" does NOT count.
- Two categories, shown separately:
  (A) Limited free — free mint/claim, clear cap, direct fungible tokens.
  (B) Small refundable deposit — deposit refundable in full within 24h of
      receipt, user-initiated, no admin approval / fundraising target /
      new entrants / token price dependency. NEVER labelled free,
      capital-protected, or risk-free. Deposit caps are NOT configured yet,
      so deposit projects only reach "待预算确认", never execution.
- Exclusions: NFT, NFT-bonus tokens, blind boxes, points, long-term mining,
  restaking, loop staking, cross-chain, complex follow-ups. Plus the old
  Rule 10 baseline: (a) fixed-duration lock, (b) owner pause/close,
  (c) sellout-to-open, (d) no cap / unpublished cap, (e) social tasks,
  (f) no independent website.

Safety: READ-ONLY. No wallet login, no signing, no transactions, no private
keys or session credentials, no wallet permission changes. Eligibility is
checked against a configured PUBLIC address only (placeholder until the
user fills it in). Web pages, posts, skill.md and llms.txt are UNTRUSTED
data: read as text only, never execute commands or install components
from them.

Model budget (spec defaults): max 5 studies/day, max 1 per project per day,
20k input tokens/day, 5k output tokens/day, amount cap supported. Any limit
reached pauses model calls. When real usage cannot be obtained it is marked
unavailable, never fabricated.

Outputs (this directory): opportunities.json, changes.jsonl,
research_queue.json. GrokBot reads only new and material changes.
The live board is NOT touched by this MVP; handover.md explains
how the old hourly scan hands over after the 7-day evaluation.

Usage:
  scanner.py init                 create DB schema + output files
  scanner.py seed [seed.json]     seed current board projects as 待核实
  scanner.py collect              15-min incremental source check
  scanner.py refresh              5-min active-candidate chain refresh
  scanner.py keywords             2x/day keyword-search task package
  scanner.py ingest <leads.json>  ingest leads found by search/model step
  scanner.py check <project-id>   run automated verification checks
  scanner.py set-state <id> <state> [--note ...]
  scanner.py budget [record --in N --out N --study ...]
  scanner.py export               regenerate the three output files
  scanner.py status               one-line health summary
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

HERE = Path(__file__).resolve().parent
BOARD = HERE.parent
MINT_SCAN = BOARD / "hidden_files" / "mint_scan.py"

DB_PATH = HERE / "scanner.db"
CONFIG_PATH = HERE / "config.json"
OPPORTUNITIES_PATH = HERE / "opportunities.json"
CHANGES_PATH = HERE / "changes.jsonl"
RESEARCH_QUEUE_PATH = HERE / "research_queue.json"
SOURCE_STATE_PATH = HERE / "source_state.json"

# States (spec: 看板与交接)
STATES = ["待核实", "免费型合格候选", "押金型合格候选", "待预算确认",
          "待开池观察", "已参与待退款", "已参与待退出", "已失效", "已排除"]
# Note: "押金型合格候选／待预算确认" is one state family; with caps
# unconfigured every deposit candidate lands in "待预算确认".
# "已参与待退款／待退出" are manual states set after the user participates.

# Verification checklist. Each check yields pass | fail | unknown.
CHECKS = [
    "nft_exclusion",        # not an NFT / NFT-bonus / blind box / points
    "agentic_priority",     # priority evidence: official announcement/docs/on-chain rule
    "rule10a_lock",         # no fixed-duration lock (lock-to-open is OK)
    "rule10b_admin",        # no owner pause/close/declareFailed power
    "rule10c_sellout_open", # pool does not open only after full sellout
    "rule10d_cap",          # free cap exists and is verifiable
    "rule10e_social",       # no X/social task required to mint
    "rule10f_website",      # independent official website exists
    "rule11a_deployer_spam",    # same deployer, 3+ differently-named projects in 24h
    "rule11b_deployer_blacklist", # deployer rugged before (no pool/payback in 48h, collapsed)
    "rule11c_same_bytecode",    # same bytecode deployed 3+ times in 24h
    "rule11d_fresh_deployer",   # deployer wallet created within ~8h (caution only, not a gate)
    "rule11e_fake_progress",    # claimed progress needs >=20 distinct minting wallets
    "auth_no_approve",      # claim/deposit needs no approve/permit/Permit2/setApprovalForAll
    "source_verified",      # Sourcify exact_match (unknown if API unreachable)
    "proxy_impl",           # no upgradeable proxy, or implementation known + benign
    "sell_path",            # a pool exists for the token (pair address found)
    "sell_quote",           # executable sell quote for the obtainable amount
    "gas_sponsorship",      # current sponsorship covers user/chain/token/operation
    "deposit_refund_24h",   # deposit only: full refund within 24h, user-initiated
]

# Hard gates: a fail on any of these blocks "qualified" states.
HARD_GATES = ["nft_exclusion", "agentic_priority", "rule10a_lock",
              "rule10b_admin", "rule10c_sellout_open", "rule10d_cap",
              "rule10e_social", "rule10f_website", "auth_no_approve",
              "rule11a_deployer_spam", "rule11b_deployer_blacklist",
              "rule11c_same_bytecode", "rule11e_fake_progress",
              "deposit_refund_24h"]

# Deposit refund sub-checks (spec section 押金型额外核验, 8 items).
REFUND_SUBCHECKS = [
    "amount_asset", "clock_window", "auto_or_manual", "short_window_risk",
    "rules_on_events", "penalties_fees", "contract_enforceable",
    "fund_source_coverage",
]

DEFAULT_CONFIG = {
    "user_address": "0xYOUR_ADDRESS_HERE",
    "chain": "BSC",
    "chain_id": 56,
    "model_budget": {
        "max_studies_per_day": 5,
        "max_studies_per_project_per_day": 1,
        "max_input_tokens_per_day": 20000,
        "max_output_tokens_per_day": 5000,
        "amount_cap_usd": None,
        "paused": False,
        "usage_unavailable": False,
    },
    "deposit_caps": {
        "per_project": None,
        "total_in_deposit": None,
        "daily_new": None,
        "status": "not_configured",
    },
    "cadences": {
        "incremental_minutes": 15,
        "active_candidate_minutes": 5,
        "keyword_search_per_day": 2,
    },
    "channels": [
        {"priority": 1, "name": "official_sites_docs",
         "note": "verified projects' official sites, official X, announcements, docs"},
        {"priority": 2, "name": "agentidhub",
         "url": "https://agentidhub.eth.limo/"},
        {"priority": 3, "name": "agentidhub_feedback",
         "url": "https://agentidhub.eth.limo/feedback.html"},
        {"priority": 4, "name": "thecolony",
         "url": "https://thecolony.ai/"},
        {"priority": 5, "name": "telegram_discord",
         "note": "announcement sources already accessible (configure in config.json)"},
        {"priority": 6, "name": "verified_devs",
         "note": "verified developers' new projects, partnerships, new rounds"},
    ],
    "keyword_groups": [
        ["Agent ID", "free mint"],
        ["Agent ID", "free claim"],
        ["Agentic Wallet", "token launch"],
        ["Agent ID", "refundable"],
        ["Agent ID", "deposit"],
        ["Agentic Wallet", "refund"],
        ["Agent ID", "限量免费"],
        ["Agent ID", "押金", "退款"],
    ],
    "sources": [
        {"id": "mints_index", "kind": "json",
         "url": "https://mints-bnb.pages.dev/projects.json"},
        {"id": "agentidhub", "kind": "html",
         "url": "https://agentidhub.eth.limo/"},
        {"id": "agentidhub_feedback", "kind": "html",
         "url": "https://agentidhub.eth.limo/feedback.html"},
        {"id": "thecolony", "kind": "html",
         "url": "https://thecolony.ai/"},
        {"id": "eggo_snapshot", "kind": "json",
         "url": "https://eggo-awr.pages.dev/api/eggo/snapshot"},
        {"id": "boltbook_daily", "kind": "html",
         "url": "https://boltbook.fun/alphapedia/11-12"},
    ],
    "excluded_known": [
        {"name": "NUSA", "reason": "rule10a", "evidence": "token locked until 800/800 sellout"},
        {"name": "BHTB", "reason": "rule10a", "evidence": "token locked 365 days"},
        {"name": "SUOHA", "reason": "rule10b", "evidence": "owner can pause"},
        {"name": "WOJAGENT", "reason": "rule10b", "evidence": "owner can pause/declareFailed"},
        {"name": "NEURA", "reason": "rule10b", "evidence": "owner can close claims"},
        {"name": "Pixel GateHound/HOUND", "reason": "rule10c", "evidence": "opens only after 8,000 paid shares sell out"},
        {"name": "ToyoCoin/TOYO", "reason": "rule10c", "evidence": "opens only after 2,500 shares sell out"},
        {"name": "LUCKY777", "reason": "rule10c", "evidence": "sellout-to-open / slow open"},
        {"name": "JINMA", "reason": "rule10c", "evidence": "sellout-to-open / slow open"},
        {"name": "COSO v2", "reason": "rule10c", "evidence": "sellout-to-open / slow open"},
        {"name": "11•11", "reason": "rule10c", "evidence": "sellout-to-open / slow open"},
        {"name": "MiniCoin (COI)", "reason": "rule10d", "evidence": "no cap / unlimited mint"},
        {"name": "AGENT MALTA", "reason": "rule10d", "evidence": "no cap"},
        {"name": "BASKET", "reason": "rule10d", "evidence": "no cap"},
        {"name": "UFO", "reason": "rule10e", "evidence": "needs X post for permit before claim"},
        {"name": "Ryuwire", "reason": "rule10d", "evidence": "cap unpublished / unreadable"},
        {"name": "Dogwoodvale", "reason": "rule10d", "evidence": "cap unpublished / unreadable"},
        {"name": "Loquatmist", "reason": "rule10d", "evidence": "cap unpublished / unreadable"},
        {"name": "Jettydrift", "reason": "rule10d", "evidence": "cap unpublished / unreadable"},
        {"name": "Servalbrook", "reason": "rule10d", "evidence": "cap unpublished / unreadable"},
        {"name": "Njordora", "reason": "rule10d", "evidence": "cap unpublished / unreadable"},
        {"name": "Relicflint", "reason": "rule10d", "evidence": "cap unpublished / unreadable"},
        {"name": "BabyDogeFrenzy", "reason": "rule10d", "evidence": "cap unpublished / unreadable"},
        {"name": "Godceo", "reason": "rule10d", "evidence": "cap unpublished / unreadable"},
        {"name": "YAMA", "reason": "rule10d", "evidence": "free cap unreadable"},
        {"name": "OPEC", "reason": "rule10d", "evidence": "cap unpublished"},
        {"name": "$BI", "reason": "rule10d", "evidence": "progress unreadable / cap disputed"},
        {"name": "LOWB", "reason": "rule10f", "evidence": "no independent website"},
        {"name": "BabyNeiro", "reason": "rule10f", "evidence": "no independent website"},
        {"name": "FMA", "reason": "rule10f", "evidence": "no independent website"},
        {"name": "AGENT", "reason": "no_pool_value", "evidence": "both PancakeSwap pairs empty, value ~$0"},
        {"name": "META BLOOM", "reason": "sold_out", "evidence": "sold out 2026-11-10"},
        {"name": "PepeWarCry", "reason": "rule10", "evidence": "excluded under Rule 10"},
        {"name": "SPY", "reason": "rule10", "evidence": "excluded under Rule 10"},
        {"name": "Hash21", "reason": "rule10", "evidence": "excluded under Rule 10"},
        {"name": "NOVRA", "reason": "rule10", "evidence": "excluded under Rule 10"},
    ],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, kind TEXT, url TEXT,
  etag TEXT, content_hash TEXT,
  last_ok_at TEXT, last_status TEXT, last_error TEXT,
  consecutive_failures INTEGER DEFAULT 0, last_checked_at TEXT
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT, type TEXT, state TEXT,
  chain TEXT, chain_id INTEGER, homepage TEXT,
  sale_contract TEXT, token_contract TEXT,
  agentic_evidence TEXT DEFAULT 'unknown',
  agentic_evidence_url TEXT,
  free_cap INTEGER, free_claimed INTEGER, free_remaining INTEGER,
  deadline TEXT, deposit_json TEXT, refund_json TEXT,
  auth_required TEXT DEFAULT 'unknown', auth_detail TEXT,
  sell_quote TEXT DEFAULT 'unknown', sell_quote_at TEXT,
  gas_sponsorship TEXT DEFAULT 'unknown', gas_sponsorship_evidence_at TEXT,
  heat_evidence TEXT, risks TEXT, sources TEXT,
  counter_json TEXT, round TEXT DEFAULT 'default',
  discovered_at TEXT, discovered_via TEXT, last_verified_at TEXT,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT, check_name TEXT, result TEXT,
  evidence TEXT, checked_at TEXT,
  UNIQUE(project_id, check_name)
);
CREATE TABLE IF NOT EXISTS excluded (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, chain TEXT, contract TEXT, reason_code TEXT,
  evidence TEXT, first_seen_at TEXT,
  UNIQUE(name)
);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, project_id TEXT, field TEXT,
  old_value TEXT, new_value TEXT, material INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS research_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT, task_type TEXT, priority INTEGER DEFAULT 3,
  detail TEXT, status TEXT DEFAULT 'open',
  created_at TEXT, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS budget (
  date TEXT PRIMARY KEY, studies_used INTEGER DEFAULT 0,
  tokens_in_used INTEGER DEFAULT 0, tokens_out_used INTEGER DEFAULT 0,
  amount_usd REAL DEFAULT 0, paused INTEGER DEFAULT 0,
  usage_unavailable INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS studies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT, date TEXT,
  tokens_in INTEGER, tokens_out INTEGER, amount_usd REAL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chain TEXT, contract TEXT, name TEXT, url TEXT,
  source_id TEXT, first_seen_at TEXT, status TEXT DEFAULT 'new',
  UNIQUE(chain, contract)
);
"""


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_cst():
    # Budget day is counted in the user's timezone (Asia/Shanghai).
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        # Fill any missing keys from defaults (forward-compatible).
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    return json.loads(json.dumps(DEFAULT_CONFIG))


def connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def sha256_hex(data: bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# P6 (2026-11-22): content-normalized change detection, PILOT SOURCES ONLY.
# Dynamic pages (thecolony / eggo) report a "change" almost every fetch
# because counters, timestamps and ad slots churn; the raw hash then
# triggers useless refresh work. For these two sources the change hash is
# computed over normalized text: script/style blocks, HTML comments, ISO
# timestamps/dates/times, unix epochs and long digit runs (counters) are
# stripped and whitespace collapsed before hashing. All other sources keep
# the raw-byte hash exactly as before.
NORMALIZED_HASH_SOURCES = {"thecolony", "eggo_snapshot", "tg_sanyueaibot",
                           "tg_agentidcall", "mintradar_datt",
                           "twiscan_binancewallet", "square_hashtag_agent",
                           "agentidhub", "agentidhub_feedback", "mint_redceh",
                           "gang_fun", "agent_radar", "four_meme", "flap_agentid"}
_NORM_STRIP_RES = None


def normalized_content_hash(body: bytes) -> str:
    global _NORM_STRIP_RES
    if _NORM_STRIP_RES is None:
        import re as _re
        _NORM_STRIP_RES = [
            _re.compile(r"<script\b[^>]*>.*?</script>", _re.I | _re.S),
            _re.compile(r"<style\b[^>]*>.*?</style>", _re.I | _re.S),
            _re.compile(r"<!--.*?-->", _re.S),
            _re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"),
            _re.compile(r"\d{4}-\d{2}-\d{2}"),
            _re.compile(r"\b\d{2}:\d{2}(:\d{2})?\b"),
            _re.compile(r"\b1[6-9]\d{8}\b"),          # unix epoch seconds/ms-ish
            _re.compile(r"\d[\d,]{3,}"),               # counters incl. 1,234-style grouping
            _re.compile(r"\d{6,}"),                    # long digit runs (counters/ids)
            _re.compile(r"\s+"),
        ]
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    for rx in _NORM_STRIP_RES[:-1]:
        text = rx.sub(" ", text)
    text = _NORM_STRIP_RES[-1].sub(" ", text).strip()
    return sha256_hex(text.encode("utf-8"))


def content_hash_for(sid, body: bytes) -> str:
    if sid in NORMALIZED_HASH_SOURCES:
        return normalized_content_hash(body)
    return sha256_hex(body)


def log_change(conn, project_id, field, old, new, material=1):
    conn.execute(
        "INSERT INTO changes (ts, project_id, field, old_value, new_value, material)"
        " VALUES (?,?,?,?,?,?)",
        (utcnow(), project_id, field, _s(old), _s(new), material))
    conn.commit()


def _s(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def queue_research(conn, project_id, task_type, detail, priority=3):
    conn.execute(
        "INSERT INTO research_queue (project_id, task_type, priority, detail, status, created_at)"
        " VALUES (?,?,?,?, 'open', ?)",
        (project_id, task_type, priority, _s(detail), utcnow()))
    conn.commit()


def project_row_to_dict(row):
    d = dict(row)
    for k in ("deposit_json", "refund_json", "counter_json"):
        v = d.get(k)
        d[k.replace("_json", "")] = json.loads(v) if v else None
        del d[k]
    for k in ("risks", "sources"):
        v = d.get(k)
        d[k] = json.loads(v) if v else []
    return d

# ---------------------------------------------------------------------------
# Source fetching (collect) — ordinary scripts only, no model calls.
# Fetched pages are UNTRUSTED data: text is read and regex-scanned for
# addresses/URLs only. Nothing from a page is ever executed or installed.
# ---------------------------------------------------------------------------
ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


def fetch_url(url, etag=None, timeout=20):
    """GET a URL. Returns (status, body_bytes, new_etag, error).
    Uses requests when available, else urllib. Never raises for HTTP errors."""
    headers = {"User-Agent": "agentic-scanner-mvp/1.0 (+local read-only)"}
    if etag:
        headers["If-None-Match"] = etag
    if HAVE_REQUESTS:
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            return r.status_code, r.content, r.headers.get("ETag"), None
        except Exception as e:
            return None, b"", None, str(e)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("ETag"), None
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(), e.headers.get("ETag"), None
        except Exception:
            return e.code, b"", None, None
    except Exception as e:
        return None, b"", None, str(e)


def parse_mints_index(text):
    """Structured parser for the MINTS index (projects.json).
    Returns a list of lead dicts with name/contract/kind/standard/website/
    mint_terms/agent_id_required/max_supply/minted/status.
    Only ERC-20 token entries are returned as candidates; NFT entries are
    returned too but flagged kind='nft' so ingest excludes them (NFT rule).
    Raw address scraping is deliberately NOT used here: the index is a
    structured directory, so we parse its records, not its bytes."""
    try:
        data = json.loads(text)
    except Exception:
        return []
    entries = []
    for key in ("featured", "projects"):
        for p in data.get(key) or []:
            if not isinstance(p, dict):
                continue
            contract = p.get("contract")
            if not contract or not ADDR_RE.fullmatch(contract):
                continue
            entries.append({
                "contract": contract,
                "name": p.get("name"),
                "kind": (p.get("kind") or "").lower(),
                "standard": (p.get("standard") or ""),
                "status": p.get("status"),
                "website": p.get("website"),
                "mint_terms": p.get("mint_terms") or "",
                "agent_id_required": p.get("agent_id_required"),
                "max_supply": p.get("max_supply"),
                "minted": p.get("minted"),
                "sold_out": bool(p.get("sold_out")),
                "url": p.get("website"),
                "snippet": (p.get("mint_terms") or "")[:400],
                "source_id": "mints_index",
            })
    return entries


def leads_for_source(text, sid):
    """Lead extraction, per source. Conservative by design:
    - mints_index: structured JSON parse only.
    - eggo_snapshot: progress API only, no leads (it lists holders).
    - HTML / other sources: change detection only; discovery from these
      comes via the keyword search + model step, not raw address scraping.
    Raw address scraping floods the queue with holders, art files and
    unrelated contracts — that noise is worse than missing a lead here."""
    if sid == "mints_index":
        return parse_mints_index(text)
    return []


def cmd_collect(conn, cfg):
    """15-min incremental check of configured sources. Content hash + ETag
    dedup: unchanged sources are skipped, changed ones yield leads.
    Source failures are recorded and reported — never 'no projects'."""
    print(f"== collect {utcnow()} ==")
    results = {"checked": 0, "changed": 0, "failed": [], "new_leads": 0,
               "skipped_cooldown": []}
    # Per-source fetch cadence (config tier/every_minutes): a source whose
    # cooldown has not elapsed is skipped this round. Fetch epochs live in
    # source_state.json; a missing record means "fetch now".
    fetch_state = {}
    if SOURCE_STATE_PATH.exists():
        try:
            fetch_state = json.loads(SOURCE_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            fetch_state = {}
    now_epoch = time.time()
    for src in cfg["sources"]:
        sid, url = src["id"], src["url"]
        every_min = src.get("every_minutes")
        if every_min:
            last_fetch = fetch_state.get(sid)
            if last_fetch and now_epoch - float(last_fetch) < float(every_min) * 60:
                results["skipped_cooldown"].append(sid)
                print(f"  [skip] {sid} (skipped_cooldown, every {every_min}m)")
                continue
        fetch_state[sid] = now_epoch
        row = conn.execute("SELECT etag, content_hash, consecutive_failures"
                           " FROM sources WHERE id=?", (sid,)).fetchone()
        old_etag = row["etag"] if row else None
        old_hash = row["content_hash"] if row else None
        status, body, new_etag, err = fetch_url(url, etag=old_etag)
        results["checked"] += 1
        now = utcnow()
        if err is not None or status is None:
            fails = (row["consecutive_failures"] if row else 0) + 1
            conn.execute(
                "INSERT INTO sources (id, kind, url, etag, content_hash, last_status,"
                " last_error, consecutive_failures, last_checked_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET last_status='fetch_error',"
                " last_error=excluded.last_error, consecutive_failures=excluded.consecutive_failures,"
                " last_checked_at=excluded.last_checked_at",
                (sid, src["kind"], url, old_etag, old_hash, "fetch_error", err, fails, now))
            conn.commit()
            results["failed"].append({"source": sid, "error": err, "failures": fails})
            print(f"  [FAIL] {sid} ({url}): {err} (consecutive failures: {fails})")
            continue
        if status == 304 or (old_hash and content_hash_for(sid, body) == old_hash and status == 200):
            conn.execute(
                "UPDATE sources SET last_checked_at=?, last_status=? WHERE id=?",
                (now, f"unchanged {status}", sid))
            conn.commit()
            print(f"  [same] {sid}")
            continue
        if status != 200:
            fails = (row["consecutive_failures"] if row else 0) + 1
            conn.execute(
                "INSERT INTO sources (id, kind, url, etag, content_hash, last_status,"
                " last_error, consecutive_failures, last_checked_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET last_status=excluded.last_status,"
                " last_error=excluded.last_error, consecutive_failures=excluded.consecutive_failures,"
                " last_checked_at=excluded.last_checked_at",
                (sid, src["kind"], url, old_etag, old_hash, f"HTTP {status}", f"HTTP {status}", fails, now))
            conn.commit()
            results["failed"].append({"source": sid, "error": f"HTTP {status}", "failures": fails})
            print(f"  [FAIL] {sid} ({url}): HTTP {status} (consecutive failures: {fails})")
            continue
        new_hash = content_hash_for(sid, body)
        conn.execute(
            "INSERT INTO sources (id, kind, url, etag, content_hash, last_ok_at,"
            " last_status, last_error, consecutive_failures, last_checked_at)"
            " VALUES (?,?,?,?,?,?, 'ok', NULL, 0, ?)"
            " ON CONFLICT(id) DO UPDATE SET etag=excluded.etag,"
            " content_hash=excluded.content_hash, last_ok_at=excluded.last_ok_at,"
            " last_status='ok', last_error=NULL, consecutive_failures=0,"
            " last_checked_at=excluded.last_checked_at",
            (sid, src["kind"], url, new_etag, new_hash, now, now))
        conn.commit()
        results["changed"] += 1
        print(f"  [changed] {sid} ({len(body)} bytes, hash {new_hash[:12]}...)")
        try:
            text = body.decode("utf-8", "replace")
        except Exception:
            text = ""
        leads = leads_for_source(text, sid)
        seen = set()
        for lead in leads:
            key = (cfg["chain"], lead["contract"].lower())
            if key in seen:
                continue
            seen.add(key)
            cur = conn.execute(
                "INSERT OR IGNORE INTO leads (chain, contract, name, url, source_id, first_seen_at, status)"
                " VALUES (?,?,?,?,?,?, 'new')",
                (cfg["chain"], lead["contract"], lead.get("name"), lead.get("url"), sid, now))
            if cur.rowcount:
                results["new_leads"] += 1
                queue_research(conn, None, "verify_candidate",
                               {"contract": lead["contract"], "url": lead.get("url"),
                                "name": lead.get("name"), "kind": lead.get("kind"),
                                "standard": lead.get("standard"),
                                "website": lead.get("website"),
                                "mint_terms": lead.get("mint_terms"),
                                "agent_id_required": lead.get("agent_id_required"),
                                "max_supply": lead.get("max_supply"),
                                "minted": lead.get("minted"),
                                "sold_out": lead.get("sold_out"),
                                "source": sid, "snippet": lead.get("snippet")}, priority=3)
                print(f"    new lead: {lead.get('name') or lead['contract']} ({lead['contract']}) via {sid}")
        conn.commit()
    try:
        SOURCE_STATE_PATH.write_text(json.dumps(fetch_state), encoding="utf-8")
    except Exception:
        pass
    if results["failed"]:
        print(f"  NOTE: {len(results['failed'])} source(s) failed — recorded as failures, NOT as 'no projects'.")
    print(f"collect done: checked={results['checked']} changed={results['changed']} "
          f"failed={len(results['failed'])} new_leads={results['new_leads']} "
          f"skipped_cooldown={len(results['skipped_cooldown'])}")
    return results


# ---------------------------------------------------------------------------
# Chain reads — always through the fixed mint_scan.py (never inline python
# for chain checks, per the old scan's rule 9). Subprocess only.
# ---------------------------------------------------------------------------
def run_mint_scan(*args, timeout=120):
    r = subprocess.run([sys.executable, str(MINT_SCAN), *args],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def sourcify_check(address, chain_id=56, timeout=20):
    """Sourcify v2 exact_match? Returns (result, evidence).
    API unreachable -> unknown (never fail on an unreachable API)."""
    url = f"https://api.sourcify.dev/server/v2/contract/{chain_id}/{address}"
    status, body, _, err = fetch_url(url, timeout=timeout)
    if err is not None or status is None:
        return "unknown", f"Sourcify API unreachable: {err}"
    if status == 404:
        return "fail", "Sourcify: no match (404)"
    if status != 200:
        return "unknown", f"Sourcify: HTTP {status}"
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return "unknown", "Sourcify: unparsable response"
    std = data.get("stdJson") or {}
    match = std.get("match") or data.get("match")
    if match == "exact":
        return "pass", "Sourcify exact_match"
    if match == "partial":
        return "unknown", "Sourcify partial match only"
    return "fail", f"Sourcify: match={match}"


def check_token(address):
    """mint_scan.py token: returns dict with name/symbol/decimals/totalSupply
    (values or None) + raw output."""
    out = run_mint_scan("token", address)
    vals = {}
    for key in ("name()", "symbol()", "decimals()", "totalSupply()"):
        seg = out.split(key)
        if len(seg) > 1:
            m = re.search(r"string: '([^']*)'", seg[1][:400])
            if m:
                vals[key] = m.group(1)
                continue
            m = re.search(r"uint: (\d+)", seg[1][:400])
            if m:
                vals[key] = int(m.group(1))
    return vals, out


def check_admin(address):
    """mint_scan.py admin: returns (owner_or_None, paused_or_None, raw)."""
    out = run_mint_scan("admin", address)
    m = re.search(r"address: (0x[a-fA-F0-9]{40})", out)
    owner = m.group(1) if m else None
    m2 = re.search(r"paused\(\): (True|False)", out)
    paused = (m2.group(1) == "True") if m2 else None
    return owner, paused, out


def check_pair(token_address, quote_address="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"):
    """mint_scan.py pair: returns (pair_or_None, reserves_or_None, raw)."""
    out = run_mint_scan("pair", token_address, quote_address)
    m = re.search(r"pair: (0x[a-fA-F0-9]{40})", out)
    pair = m.group(1) if m else None
    m2 = re.search(r"reserves: (\d+) / (\d+)", out)
    reserves = (int(m2.group(1)), int(m2.group(2))) if m2 else None
    return pair, reserves, out


def check_impl(address):
    """mint_scan.py impl: returns implementation address or None (zero = not a proxy)."""
    out = run_mint_scan("impl", address)
    m = re.search(r"\(address: (0x[a-fA-F0-9]{40})\)", out)
    if not m:
        return None, out
    addr = m.group(1)
    if addr == "0x0000000000000000000000000000000000000000":
        return None, out
    return addr, out


# ---------------------------------------------------------------------------
# Verification checklist + state machine.
# ---------------------------------------------------------------------------
def set_check(conn, project_id, name, result, evidence):
    assert result in ("pass", "fail", "unknown")
    conn.execute(
        "INSERT INTO checks (project_id, check_name, result, evidence, checked_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(project_id, check_name) DO UPDATE SET result=excluded.result,"
        " evidence=excluded.evidence, checked_at=excluded.checked_at",
        (project_id, name, result, _s(evidence), utcnow()))
    conn.commit()


def get_checks(conn, project_id):
    rows = conn.execute(
        "SELECT check_name, result, evidence, checked_at FROM checks WHERE project_id=?",
        (project_id,)).fetchall()
    return {r["check_name"]: {"result": r["result"], "evidence": r["evidence"],
                             "checked_at": r["checked_at"]} for r in rows}


def evaluate_refund_rule(refund):
    """Evaluate a deposit project's refund rule against the 24h condition.
    refund is a dict (see config deposit/refund schema). Returns
    (qualified: bool, reasons: list[str]).
    The 24h condition is satisfied ONLY when ALL of these hold:
      - clock starts at deposit receipt (not at unlock, not at a team promise)
      - the user can initiate a full refund and the latest request time is
        within 24h of deposit receipt
      - the refund is contract-enforceable (not a team promise / manual payout)
      - no sell-to-recover, no new-funds-paid refund, no manual admin payout,
        principal coverage confirmed.
    These patterns do NOT satisfy the condition (spec, verbatim):
      'unlock starts after 24h', 'team promises 24h refund',
      'apply within 24h but arrival time unknown'."""
    reasons = []
    if not isinstance(refund, dict):
        return False, ["no refund rule recorded"]
    # Clock start must be deposit receipt.
    clock = (refund.get("clock_start") or "").strip().lower()
    if clock != "deposit_receipt":
        reasons.append(f"clock_start={clock or 'missing'} (must be deposit_receipt)")
    # Latest request time must be within 24h.
    latest = refund.get("latest_request_hours")
    if latest is None:
        reasons.append("latest_request_hours missing")
    elif latest > 24:
        reasons.append(f"latest_request_hours={latest}h (> 24h)")
    # Full refund, same currency, no deductions.
    if refund.get("full_refund") is not True:
        reasons.append("full_refund is not true")
    if refund.get("same_currency") is not True:
        reasons.append("same_currency is not true")
    if refund.get("principal_deductions"):
        reasons.append(f"principal_deductions present: {refund['principal_deductions']}")
    # Contract-enforceable, not a promise / manual payout.
    if refund.get("contract_enforceable") is not True:
        reasons.append("contract_enforceable is not true")
    if refund.get("manual_admin_payout"):
        reasons.append("refund requires manual admin payout")
    # Disqualifying patterns.
    if refund.get("sell_to_recover"):
        reasons.append("deposit is recovered by selling tokens (not a refund)")
    if refund.get("new_funds_refund"):
        reasons.append("refund is paid from new entrants' funds")
    if refund.get("coverage_unconfirmed"):
        reasons.append("principal coverage unconfirmed")
    if refund.get("arrival_unknown"):
        reasons.append("refund arrival time unknown")
    if refund.get("unlock_after_24h"):
        reasons.append("unlock starts after 24h (not a 24h refund)")
    return (len(reasons) == 0), reasons


def gas_is_stale(checks, project, max_age_hours=24):
    """Gas sponsorship evidence is stale if the check was recorded more
    than max_age_hours ago. Historical gas-free announcements do not
    replace current verification; stale evidence = unknown."""
    g = checks.get("gas_sponsorship") or {}
    if g.get("result") != "pass":
        return False
    checked_at = g.get("checked_at")
    if not checked_at:
        return True
    try:
        t = datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        try:
            t = datetime.fromisoformat(checked_at.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return True
    age = (datetime.utcnow() - t).total_seconds() / 3600.0
    return age > max_age_hours


def compute_state(project, checks, cfg):
    """Pure state function. Returns (state, reasons list).
    Qualified states require every HARD_GATE to be 'pass'. Anything else
    stays 待核实 (or 已失效/已排除 when set explicitly). No composite score
    is used — a security gap is never hidden behind one number."""
    reasons = []
    ptype = project.get("type") or "free"
    # Hard gates
    for gate in HARD_GATES:
        if gate == "deposit_refund_24h" and ptype != "deposit":
            continue
        res = checks.get(gate, {}).get("result", "unknown")
        if res == "fail":
            return "已排除", [f"hard gate {gate} failed"]
        if res != "pass":
            reasons.append(f"{gate}: {res}")
    if reasons:
        return "待核实", reasons
    # All hard gates pass.
    if ptype == "deposit":
        caps = cfg.get("deposit_caps", {})
        if caps.get("status") != "configured":
            return "待预算确认", ["deposit caps not configured"]
        return "押金型合格候选", []
    # Free type: pool open + executable quote + current gas sponsorship.
    sell = checks.get("sell_path", {}).get("result", "unknown")
    quote = checks.get("sell_quote", {}).get("result", "unknown")
    gas = checks.get("gas_sponsorship", {}).get("result", "unknown")
    # Stale gas evidence is treated as unknown (historical announcements
    # do not replace current verification).
    if gas == "pass" and gas_is_stale(checks, project):
        gas = "unknown"
        reasons.append("gas_sponsorship evidence is stale (>24h)")
    if sell == "pass" and quote == "pass":
        if gas == "pass":
            return "免费型合格候选", []
        if gas == "fail":
            return "待开池观察", ["gas sponsorship failed/expired"]
        return "待开池观察", ["gas sponsorship unverified"]
    return "待开池观察", ["pool not open yet or no executable quote"]


def apply_state(conn, project_id, cfg):
    """Recompute a project's state from its checks. Logs material changes."""
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        return None
    project = project_row_to_dict(row)
    checks = get_checks(conn, project_id)
    new_state, reasons = compute_state(project, checks, cfg)
    old_state = project["state"]
    if new_state != old_state:
        # Never auto-promote out of a manual terminal state.
        if old_state in ("已参与待退款", "已参与待退出", "已失效", "已排除"):
            return old_state
        conn.execute("UPDATE projects SET state=?, last_verified_at=? WHERE id=?",
                     (new_state, utcnow(), project_id))
        log_change(conn, project_id, "state", old_state, new_state, material=1)
        conn.commit()
        print(f"  state {project_id}: {old_state} -> {new_state} ({'; '.join(reasons)})")
        return new_state
    return old_state


def cmd_check(conn, cfg, project_id):
    """Run the automated (scriptable) verification checks for one project:
    token, admin, proxy implementation, sell path (pair), Sourcify.
    Model-dependent checks (Agentic priority evidence, refund mechanism,
    heat, gas sponsorship currency) are queued as research tasks, not
    guessed here."""
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        print(f"unknown project: {project_id}")
        return None
    project = project_row_to_dict(row)
    print(f"== check {project_id} ({project['name']}) ==")
    token_addr = project.get("token_contract")
    sale_addr = project.get("sale_contract")
    if token_addr:
        vals, _ = check_token(token_addr)
        ok = bool(vals.get("name()")) and bool(vals.get("symbol()"))
        set_check(conn, project_id, "source_verified", "unknown",
                  "token read only; Sourcify check below is authoritative")
        print(f"  token: {vals}")
        res, ev = sourcify_check(token_addr, cfg.get("chain_id", 56))
        set_check(conn, project_id, "source_verified", res, ev)
        print(f"  sourcify: {res} ({ev})")
    if sale_addr:
        owner, paused, _ = check_admin(sale_addr)
        if paused is True:
            set_check(conn, project_id, "rule10b_admin", "fail",
                      f"sale contract is paused (owner {owner})")
        elif owner:
            set_check(conn, project_id, "rule10b_admin", "unknown",
                      f"owner {owner} exists; pause power not yet read from source")
        else:
            set_check(conn, project_id, "rule10b_admin", "unknown",
                      "no owner() function found on sale contract")
        print(f"  admin: owner={owner} paused={paused}")
        impl, _ = check_impl(sale_addr)
        if impl:
            set_check(conn, project_id, "proxy_impl", "unknown",
                      f"proxy implementation {impl} — upgrade power needs source review")
        else:
            set_check(conn, project_id, "proxy_impl", "pass",
                      "EIP-1967 implementation slot is zero — not a proxy")
        print(f"  proxy impl: {impl or 'none'}")
    if token_addr:
        pair, reserves, _ = check_pair(token_addr)
        if pair and pair != "0x0000000000000000000000000000000000000000":
            set_check(conn, project_id, "sell_path", "pass",
                      f"PancakeSwap V2 pair {pair}, reserves {reserves}")
            set_check(conn, project_id, "sell_quote", "unknown",
                      "pair exists; executable quote for the obtainable amount not yet computed")
        else:
            set_check(conn, project_id, "sell_path", "fail",
                      "no PancakeSwap V2 pair — no pool, no executable sell quote")
            set_check(conn, project_id, "sell_quote", "unknown",
                      "no pool — expected profit unknown, never fabricated")
        print(f"  pair: {pair} reserves={reserves}")
    # Model-dependent checks are queued, never guessed.
    for chk in ("agentic_priority", "auth_no_approve", "gas_sponsorship",
                "rule10a_lock", "rule10c_sellout_open", "rule10d_cap",
                "rule10e_social", "rule10f_website"):
        cur = get_checks(conn, project_id).get(chk, {}).get("result")
        if cur is None:
            queue_research(conn, project_id, "verify_check",
                           {"check": chk, "note": "needs official docs/source review"}, priority=2)
    if (project.get("type") == "deposit"):
        queue_research(conn, project_id, "verify_refund",
                       {"note": "record the 8 refund sub-checks from contract + docs"}, priority=1)
        # If a refund rule is already recorded, evaluate it against the 24h
        # condition now (no model call — the rule is data, the test is code).
        refund = project.get("refund")
        if refund:
            ok, why = evaluate_refund_rule(refund)
            set_check(conn, project_id, "deposit_refund_24h",
                      "pass" if ok else "fail",
                      "24h refund satisfied" if ok else ("; ".join(why)))
            print(f"  refund 24h: {'pass' if ok else 'FAIL'} ({'; '.join(why) if why else 'ok'})")
    new_state = apply_state(conn, project_id, cfg)
    print(f"  state now: {new_state}")
    return new_state

# ---------------------------------------------------------------------------
# Refresh — 5-min active-candidate chain refresh via mint_scan.py.
# Never calls the model for ordinary count changes.
# ---------------------------------------------------------------------------
def parse_counter_output(out, sig, section=None):
    """Extract the numeric value for one sig from mint_scan.py output.
    When section is given (a project name), only lines under the
    '== NAME (...)' header for that project are considered — several
    projects share sig names (e.g. totalShares), so an unscoped search
    would return the wrong project's value."""
    lines = out.splitlines()
    if section:
        in_section = False
        for line in lines:
            s = line.strip()
            if s.startswith("=="):
                in_section = section.lower() in s.lower()
                continue
            if in_section and s.startswith(sig + ":"):
                m = re.search(r":\s*(\d+)\s*$", s)
                if m:
                    return int(m.group(1))
        return None
    for line in lines:
        s = line.strip()
        if s.startswith(sig + ":"):
            m = re.search(r":\s*(\d+)\s*$", s)
            if m:
                return int(m.group(1))
    return None


def cmd_refresh(conn, cfg):
    """Refresh chain counters for active candidates. Material changes
    (progress moved, deadline passed) go to changes.jsonl."""
    print(f"== refresh {utcnow()} ==")
    active_states = ("待核实", "免费型合格候选", "押金型合格候选", "待预算确认", "待开池观察")
    rows = conn.execute(
        "SELECT * FROM projects WHERE state IN (%s)" % ",".join("?" * len(active_states)),
        active_states).fetchall()
    # One mint_scan.py no-arg run covers the known counters for the current
    # board; per-project counters are read with `call` when configured.
    out = run_mint_scan(timeout=180)
    updated = 0
    for row in rows:
        project = project_row_to_dict(row)
        counter = project.get("counter") or {}
        sig = counter.get("sig")
        if not sig:
            continue
        val = parse_counter_output(out, sig, section=project["id"])
        if val is None:
            # Fall back to a targeted call on the configured contract.
            addr = counter.get("contract")
            if not addr:
                continue
            out2 = run_mint_scan("call", addr, sig)
            val = parse_counter_output(out2, sig)
        if val is None:
            print(f"  [warn] {project['id']}: counter {sig} unreadable this round")
            continue
        divisor = int(counter.get("divisor") or 1)
        if counter.get("mode") == "remaining":
            # Counter reports what is LEFT; claimed = cap - remaining.
            cap = project.get("free_cap")
            claimed = (cap - val) if cap else None
        else:
            claimed = val // divisor if divisor else val
        cap = project.get("free_cap")
        remaining = (cap - claimed) if (cap and claimed is not None) else None
        old_claimed = project.get("free_claimed")
        if old_claimed != claimed:
            conn.execute(
                "UPDATE projects SET free_claimed=?, free_remaining=?, last_verified_at=? WHERE id=?",
                (claimed, remaining, utcnow(), project["id"]))
            log_change(conn, project["id"], "free_claimed", old_claimed, claimed,
                       material=1 if (cap and old_claimed is not None and claimed >= cap) else 0)
            conn.commit()
            updated += 1
            print(f"  {project['id']}: {sig} {old_claimed} -> {claimed}"
                  + (f" (cap {cap}, remaining {remaining})" if cap else ""))
    print(f"refresh done: {len(rows)} active, {updated} progress updates")
    return updated


# ---------------------------------------------------------------------------
# Keywords — 2x/day keyword-search task package (the model/browser step).
# The script does not do web search itself; it packages the work.
# ---------------------------------------------------------------------------
def cmd_keywords(conn, cfg):
    """Write the keyword-search task package for the model step.
    Only the relevant keyword groups + channels are included — never the
    whole board or full history."""
    pkg = {
        "generated_at": utcnow(),
        "cadence": "2x/day",
        "channels": [c for c in cfg["channels"]],
        "keyword_groups": cfg["keyword_groups"],
        "instructions": (
            "Search each keyword group across the channels in priority order. "
            "For each candidate found, return a leads JSON entry with: name, "
            "homepage, sale_contract, token_contract (if known), type "
            "(free|deposit), agentic_evidence (quote the official announcement, "
            "docs, or on-chain rule — 'supports Agentic Wallet' alone does NOT "
            "count), and source URLs. Feed the file back with: "
            "scanner.py ingest <leads.json>. Do NOT expand to generic new-coin "
            "lists, ordinary paid listings, or generic airdrops."),
        "scope_reminder": (
            "NFT, NFT-bonus tokens, blind boxes, points, long-term mining, "
            "restaking, loop staking, cross-chain, and complex follow-ups are "
            "out of scope. Rule 10 (a)-(f) still applies as a baseline."),
    }
    path = HERE / "keyword_search_package.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pkg, f, ensure_ascii=False, indent=2)
    print(f"keyword search package written: {path}")
    print(f"  groups: {len(cfg['keyword_groups'])}, channels: {len(cfg['channels'])}")
    return pkg


# ---------------------------------------------------------------------------
# Ingest — leads found by the search/model step. Scope + Rule 10 baseline
# + dedup happen here, in code, before anything reaches the board.
# ---------------------------------------------------------------------------
NFT_WORDS = ["nft", "erc-721", "erc721", "blind box", "blindbox", "points",
             "point system", "long-term mining", "restak", "loop staking",
             "cross-chain", "cross chain"]


def cmd_ingest(conn, cfg, leads_path):
    """Ingest a leads JSON file: [{name, homepage, sale_contract,
    token_contract, type, agentic_evidence, agentic_evidence_url,
    sources, free_cap, deadline, deposit, ...}].
    Returns a summary dict. Excluded leads are recorded, never re-checked."""
    with open(leads_path, encoding="utf-8") as f:
        leads = json.load(f)
    if isinstance(leads, dict):
        leads = leads.get("leads", [])
    summary = {"received": len(leads), "added": 0, "excluded": 0, "duplicates": 0}
    for lead in leads:
        name = (lead.get("name") or "").strip()
        contract = (lead.get("sale_contract") or lead.get("token_contract") or "").strip()
        chain = lead.get("chain", cfg["chain"])
        # Dedup: same chain + same contract = same project.
        if contract:
            dup = conn.execute(
                "SELECT id FROM projects WHERE lower(sale_contract)=? OR lower(token_contract)=?",
                (contract.lower(), contract.lower())).fetchone()
            if dup:
                summary["duplicates"] += 1
                print(f"  [dup] {name or contract} already tracked as {dup['id']}")
                continue
            dup2 = conn.execute(
                "SELECT id FROM excluded WHERE lower(contract)=?", (contract.lower(),)).fetchone()
            if dup2:
                summary["duplicates"] += 1
                print(f"  [dup-excluded] {name or contract} already excluded")
                continue
        # NFT / out-of-scope exclusion (keyword + explicit flag + structured kind/standard).
        blob = json.dumps(lead, ensure_ascii=False).lower()
        nft_hit = (lead.get("is_nft")
                   or (lead.get("kind") or "").lower() in ("nft", "erc-721")
                   or (lead.get("standard") or "").upper() in ("ERC-721", "ERC721")
                   or any(w in blob for w in NFT_WORDS))
        if nft_hit:
            conn.execute(
                "INSERT OR IGNORE INTO excluded (name, chain, contract, reason_code, evidence, first_seen_at)"
                " VALUES (?,?,?,?,?,?)",
                (name or contract, chain, contract, "nft",
                 "NFT / NFT-bonus / blind box / points / mining / staking / cross-chain", utcnow()))
            conn.commit()
            summary["excluded"] += 1
            print(f"  [excluded:nft] {name or contract}")
            continue
        # Known-excluded list (never re-check). Match by name OR contract
        # (a directory may call the same token "Agent (AGENT) token").
        known = {e["name"].lower(): e for e in cfg.get("excluded_known", [])}
        known_by_contract = {e["contract"].lower(): e for e in cfg.get("excluded_known", [])
                             if e.get("contract")}
        if name.lower() in known or (contract and contract.lower() in known_by_contract):
            e = known.get(name.lower()) or known_by_contract[contract.lower()]
            conn.execute(
                "INSERT OR IGNORE INTO excluded (name, chain, contract, reason_code, evidence, first_seen_at)"
                " VALUES (?,?,?,?,?,?)",
                (name, chain, contract, e["reason"], e["evidence"], utcnow()))
            conn.commit()
            summary["excluded"] += 1
            print(f"  [excluded:known] {name} ({e['reason']})")
            continue
        # Agentic priority evidence is REQUIRED. "Supports Agentic Wallet"
        # alone does not count.
        ev = (lead.get("agentic_evidence") or "").strip()
        ev_ok = bool(ev) and ev.lower() not in (
            "supports agentic wallet", "supports binance agentic wallet",
            "agentic wallet supported")
        pid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"lead-{int(time.time())}"
        # A second round of the same project (same name, different contract)
        # is a separate project — avoid an id collision by suffixing.
        if conn.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone():
            pid = f"{pid}-{contract[-6:].lower()}" if contract else f"{pid}-{int(time.time())}"
        ptype = lead.get("type", "free")
        if ptype not in ("free", "deposit"):
            ptype = "free"
        conn.execute(
            "INSERT INTO projects (id, name, type, state, chain, chain_id, homepage,"
            " sale_contract, token_contract, agentic_evidence, agentic_evidence_url,"
            " free_cap, deadline, deposit_json, sources, discovered_at, discovered_via,"
            " last_verified_at, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, name, ptype, "待核实", chain, cfg.get("chain_id", 56),
             lead.get("homepage"), lead.get("sale_contract"), lead.get("token_contract"),
             ev if ev_ok else "unknown", lead.get("agentic_evidence_url"),
             lead.get("free_cap"), lead.get("deadline"),
             json.dumps(lead.get("deposit")) if lead.get("deposit") else None,
             json.dumps(lead.get("sources") or []),
             utcnow(), lead.get("discovered_via", "ingest"),
             utcnow(),
             "Agentic priority evidence missing — needs official announcement/docs/on-chain rule"
             if not ev_ok else ""))
        conn.commit()
        summary["added"] += 1
        log_change(conn, pid, "state", None, "待核实", material=1)
        queue_research(conn, pid, "verify_candidate",
                       {"note": "new candidate: run full checklist (check command) + model review"},
                       priority=2)
        print(f"  [added] {name} ({ptype}) -> 待核实"
              + (" [agentic evidence missing]" if not ev_ok else ""))
    print(f"ingest done: received={summary['received']} added={summary['added']} "
          f"excluded={summary['excluded']} duplicates={summary['duplicates']}")
    return summary


def cmd_process_leads(conn, cfg):
    """Process open verify_candidate tasks that have no project yet (leads
    from collect, e.g. the structured MINTS index). Automated scope checks
    only — no model call:
      NFT / known-excluded / sold-out -> excluded (recorded, never re-checked)
      duplicate contract -> skipped
      token + agent_id_required -> added as 待核实 (MINTS is directory
        evidence, NOT official-announcement evidence; the project stays
        待核实 until the model verifies the official source)
      token + agent_id_required False -> left open for model review
        (may still have priority participation / exclusive allocation,
        but that is a model judgment, not an automated one)."""
    tasks = conn.execute(
        "SELECT * FROM research_queue WHERE task_type='verify_candidate'"
        " AND project_id IS NULL AND status='open' ORDER BY id").fetchall()
    summary = {"processed": 0, "added": 0, "excluded": 0, "duplicates": 0, "needs_model": 0}
    known = {e["name"].lower(): e for e in cfg.get("excluded_known", [])}
    known_by_contract = {e["contract"].lower(): e for e in cfg.get("excluded_known", [])
                         if e.get("contract")}
    for t in tasks:
        d = json.loads(t["detail"] or "{}")
        name = (d.get("name") or "").strip()
        contract = (d.get("contract") or "").strip()
        chain = cfg["chain"]
        summary["processed"] += 1
        # Duplicate contract?
        if contract:
            dup = conn.execute(
                "SELECT id FROM projects WHERE lower(sale_contract)=? OR lower(token_contract)=?",
                (contract.lower(), contract.lower())).fetchone()
            if dup:
                summary["duplicates"] += 1
                print(f"  [dup] {name or contract} already tracked as {dup['id']}")
                conn.execute("UPDATE research_queue SET status='done', completed_at=? WHERE id=?",
                             (utcnow(), t["id"]))
                continue
            dup2 = conn.execute(
                "SELECT id FROM excluded WHERE lower(contract)=?", (contract.lower(),)).fetchone()
            if dup2:
                summary["duplicates"] += 1
                print(f"  [dup-excluded] {name or contract} already excluded")
                conn.execute("UPDATE research_queue SET status='done', completed_at=? WHERE id=?",
                             (utcnow(), t["id"]))
                continue
        # NFT / out-of-scope (structured kind/standard).
        if ((d.get("kind") or "").lower() in ("nft", "erc-721")
                or (d.get("standard") or "").upper() in ("ERC-721", "ERC721")):
            conn.execute(
                "INSERT OR IGNORE INTO excluded (name, chain, contract, reason_code, evidence, first_seen_at)"
                " VALUES (?,?,?,?,?,?)",
                (name or contract, chain, contract, "nft",
                 "MINTS index: kind=nft / standard=ERC-721", utcnow()))
            summary["excluded"] += 1
            print(f"  [excluded:nft] {name or contract}")
            conn.execute("UPDATE research_queue SET status='done', completed_at=? WHERE id=?",
                         (utcnow(), t["id"]))
            continue
        if name.lower() in known or (contract and contract.lower() in known_by_contract):
            e = known.get(name.lower()) or known_by_contract[contract.lower()]
            conn.execute(
                "INSERT OR IGNORE INTO excluded (name, chain, contract, reason_code, evidence, first_seen_at)"
                " VALUES (?,?,?,?,?,?)",
                (name, chain, contract, e["reason"], e["evidence"], utcnow()))
            summary["excluded"] += 1
            print(f"  [excluded:known] {name} ({e['reason']})")
            conn.execute("UPDATE research_queue SET status='done', completed_at=? WHERE id=?",
                         (utcnow(), t["id"]))
            continue
        if d.get("sold_out"):
            conn.execute(
                "INSERT OR IGNORE INTO excluded (name, chain, contract, reason_code, evidence, first_seen_at)"
                " VALUES (?,?,?,?,?,?)",
                (name or contract, chain, contract, "sold_out", "MINTS index: sold_out=true", utcnow()))
            summary["excluded"] += 1
            print(f"  [excluded:sold_out] {name or contract}")
            conn.execute("UPDATE research_queue SET status='done', completed_at=? WHERE id=?",
                         (utcnow(), t["id"]))
            continue
        if d.get("agent_id_required") is False:
            summary["needs_model"] += 1
            print(f"  [needs_model] {name or contract}: agent_id_required=False — "
                  "priority participation / exclusive allocation is a model judgment")
            continue
        # Token + Agent ID gated (or unknown) -> add as 待核实.
        pid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"lead-{int(time.time())}"
        # Avoid id collision.
        if conn.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone():
            pid = f"{pid}-{contract[-6:].lower()}"
        mint_terms = d.get("mint_terms") or ""
        conn.execute(
            "INSERT INTO projects (id, name, type, state, chain, chain_id, homepage,"
            " sale_contract, token_contract, agentic_evidence, agentic_evidence_url,"
            " free_cap, free_claimed, sources, discovered_at, discovered_via,"
            " last_verified_at, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, name, "free", "待核实", chain, cfg.get("chain_id", 56),
             d.get("website"), contract, contract,
             f"MINTS index (directory): agent_id_required={d.get('agent_id_required')}, "
             f"mint_terms={mint_terms[:200]}",
             d.get("website"),
             d.get("max_supply"), d.get("minted"),
             json.dumps([d.get("source", "mints_index"), d.get("website") or ""]),
             utcnow(), "mints_index", utcnow(),
             "Directory evidence only — official announcement / docs / on-chain "
             "Agentic-priority rule still needs model verification."))
        log_change(conn, pid, "state", None, "待核实", material=1)
        for chk in ("agentic_priority", "auth_no_approve", "gas_sponsorship",
                    "sell_quote", "rule10b_admin"):
            queue_research(conn, pid, "verify_check",
                           {"check": chk, "note": "new MINTS lead, not yet run"}, priority=2)
        conn.execute("UPDATE research_queue SET status='done', completed_at=? WHERE id=?",
                     (utcnow(), t["id"]))
        summary["added"] += 1
        print(f"  [added] {name} -> 待核实 (MINTS directory evidence only)")
    conn.commit()
    print(f"process_leads done: processed={summary['processed']} added={summary['added']} "
          f"excluded={summary['excluded']} duplicates={summary['duplicates']} "
          f"needs_model={summary['needs_model']}")
    return summary


# ---------------------------------------------------------------------------
# Budget — model quota tracker. Any limit reached pauses model calls.
# When real usage cannot be obtained it is marked unavailable, never fabricated.
# ---------------------------------------------------------------------------
def budget_row(conn, date=None):
    date = date or today_cst()
    row = conn.execute("SELECT * FROM budget WHERE date=?", (date,)).fetchone()
    if not row:
        conn.execute("INSERT INTO budget (date) VALUES (?)", (date,))
        conn.commit()
        row = conn.execute("SELECT * FROM budget WHERE date=?", (date,)).fetchone()
    return dict(row)


def cmd_budget(conn, cfg, record=None):
    """Show budget, or record one study's usage.
    record = {'tokens_in': int|None, 'tokens_out': int|None, 'amount_usd': float|None,
              'project_id': str, 'unavailable': bool}"""
    mb = cfg["model_budget"]
    row = budget_row(conn)
    if record is not None:
        if record.get("unavailable"):
            conn.execute("UPDATE budget SET usage_unavailable=1 WHERE date=?", (row["date"],))
            print("  usage unavailable — recorded as unavailable, not fabricated")
        else:
            conn.execute(
                "UPDATE budget SET studies_used=studies_used+1,"
                " tokens_in_used=tokens_in_used+?, tokens_out_used=tokens_out_used+?,"
                " amount_usd=amount_usd+? WHERE date=?",
                (record.get("tokens_in") or 0, record.get("tokens_out") or 0,
                 record.get("amount_usd") or 0, row["date"]))
            if record.get("project_id"):
                # One row per study (the per-project cap counts studies, not
                # the verification tasks a study happens to close).
                conn.execute(
                    "INSERT INTO studies (project_id, date, tokens_in, tokens_out,"
                    " amount_usd, created_at) VALUES (?,?,?,?,?,?)",
                    (record["project_id"], row["date"], record.get("tokens_in") or 0,
                     record.get("tokens_out") or 0, record.get("amount_usd") or 0,
                     utcnow()))
        conn.commit()
        row = budget_row(conn)
    paused = bool(row["paused"]) or bool(mb.get("paused"))
    over = (row["studies_used"] >= mb["max_studies_per_day"]
            or row["tokens_in_used"] >= mb["max_input_tokens_per_day"]
            or row["tokens_out_used"] >= mb["max_output_tokens_per_day"])
    if over and not row["paused"]:
        conn.execute("UPDATE budget SET paused=1 WHERE date=?", (row["date"],))
        conn.commit()
        paused = True
        print("  BUDGET LIMIT REACHED — model calls paused for today")
    print(f"budget {row['date']}: studies {row['studies_used']}/{mb['max_studies_per_day']}, "
          f"tokens_in {row['tokens_in_used']}/{mb['max_input_tokens_per_day']}, "
          f"tokens_out {row['tokens_out_used']}/{mb['max_output_tokens_per_day']}, "
          f"amount_usd {row['amount_usd']}, paused={paused}, "
          f"usage_unavailable={bool(row['usage_unavailable'])}")
    return {"date": row["date"], "paused": paused, "over": over, **{k: row[k] for k in row if k not in ("date", "paused")}}


def can_study(conn, cfg, project_id):
    """Gate for dispatching a model study: daily cap, per-project cap, pause."""
    mb = cfg["model_budget"]
    b = cmd_budget(conn, cfg)
    if b["paused"] or b["over"]:
        return False, "daily budget exhausted or paused"
    n = conn.execute(
        "SELECT COUNT(*) c FROM studies WHERE project_id=? AND date=?",
        (project_id, today_cst())).fetchone()["c"]
    if n >= mb["max_studies_per_project_per_day"]:
        return False, f"project already studied {n}x today (max {mb['max_studies_per_project_per_day']})"
    return True, "ok"


# ---------------------------------------------------------------------------
# Export — regenerate the three GrokBot-facing output files from the DB.
# ---------------------------------------------------------------------------
RESEARCH_TASK_TTL_DAYS = 7


def expire_research_tasks(conn):
    """P9 (2026-11-22): open research tasks older than the TTL are marked
    'expired' so they stop being exported every round. Only the task row
    changes status — the underlying lead/change data is never deleted."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=RESEARCH_TASK_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "UPDATE research_queue SET status='expired', completed_at=?"
        " WHERE status='open' AND created_at < ?",
        (utcnow(), cutoff))
    conn.commit()
    return cur.rowcount


def cmd_export(conn, cfg):
    expired = expire_research_tasks(conn)
    if expired:
        print(f"export: {expired} open research task(s) past "
              f"{RESEARCH_TASK_TTL_DAYS}d TTL marked expired")
    rows = conn.execute(
        "SELECT * FROM projects WHERE state != '已排除' ORDER BY discovered_at DESC").fetchall()
    opps = []
    for row in rows:
        p = project_row_to_dict(row)
        p["checks"] = get_checks(conn, p["id"])
        # Expected net profit: only with a reliable basis, else unknown.
        if p.get("sell_quote") == "unknown" or p.get("sell_quote") is None:
            p["expected_net_profit"] = "unknown"
        opps.append(p)
    with open(OPPORTUNITIES_PATH, "w", encoding="utf-8") as f:
        json.dump({"generated_at": utcnow(), "count": len(opps),
                   "states": sorted({o["state"] for o in opps}),
                   "opportunities": opps}, f, ensure_ascii=False, indent=2)
    ch = conn.execute(
        "SELECT ts, project_id, field, old_value, new_value, material FROM changes"
        " ORDER BY id").fetchall()
    with open(CHANGES_PATH, "w", encoding="utf-8") as f:
        for r in ch:
            f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
    rq = conn.execute(
        "SELECT id, project_id, task_type, priority, detail, status, created_at, completed_at"
        " FROM research_queue WHERE status='open' ORDER BY priority, created_at").fetchall()
    with open(RESEARCH_QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump({"generated_at": utcnow(), "open_tasks": len(rq),
                   "tasks": [dict(r) for r in rq]}, f, ensure_ascii=False, indent=2)
    print(f"export done: {len(opps)} opportunities, {len(ch)} changes, {len(rq)} open research tasks")
    return {"opportunities": len(opps), "changes": len(ch), "tasks": len(rq)}


def cmd_seed(conn, cfg, seed_path=None):
    """Seed the current board projects as 待核实 with their known facts.
    NOTHING is upgraded to 'qualified' — the new evidence checks
    (Agentic priority, sell quote, gas sponsorship, auth, admin) have not
    been run under the new spec. Each seeded project gets research tasks
    for those checks."""
    path = Path(seed_path) if seed_path else HERE / "seed_projects.json"
    with open(path, encoding="utf-8") as f:
        seed = json.load(f)
    added = 0
    for p in seed:
        cur = conn.execute("SELECT id FROM projects WHERE id=?", (p["id"],)).fetchone()
        if cur:
            continue
        conn.execute(
            "INSERT INTO projects (id, name, type, state, chain, chain_id, homepage,"
            " sale_contract, token_contract, agentic_evidence, agentic_evidence_url,"
            " free_cap, free_claimed, free_remaining, deadline, deposit_json, refund_json,"
            " auth_required, auth_detail, sell_quote, sell_quote_at, gas_sponsorship,"
            " gas_sponsorship_evidence_at, heat_evidence, risks, sources, counter_json,"
            " round, discovered_at, discovered_via, last_verified_at, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], p["name"], p.get("type", "free"), "待核实",
             p.get("chain", cfg["chain"]), cfg.get("chain_id", 56),
             p.get("homepage"), p.get("sale_contract"), p.get("token_contract"),
             "unknown", None,
             p.get("free_cap"), p.get("free_claimed"), p.get("free_remaining"),
             p.get("deadline"),
             json.dumps(p.get("deposit")) if p.get("deposit") else None,
             json.dumps(p.get("refund")) if p.get("refund") else None,
             p.get("auth_required", "unknown"), p.get("auth_detail"),
             p.get("sell_quote", "unknown"), p.get("sell_quote_at"),
             p.get("gas_sponsorship", "unknown"), p.get("gas_sponsorship_evidence_at"),
             p.get("heat_evidence"), json.dumps(p.get("risks") or []),
             json.dumps(p.get("sources") or []),
             json.dumps(p.get("counter")) if p.get("counter") else None,
             p.get("round", "default"),
             p.get("discovered_at", utcnow()), p.get("discovered_via", "board-seed"),
             utcnow(), p.get("notes", "")))
        added += 1
        log_change(conn, p["id"], "state", None, "待核实", material=1)
        for chk in ("agentic_priority", "auth_no_approve", "gas_sponsorship",
                    "sell_quote", "rule10b_admin"):
            queue_research(conn, p["id"], "verify_check",
                           {"check": chk, "note": "new-spec check, not yet run"}, priority=2)
    conn.commit()
    # Known exclusions into the excluded table (never re-checked).
    for e in cfg.get("excluded_known", []):
        conn.execute(
            "INSERT OR IGNORE INTO excluded (name, chain, contract, reason_code, evidence, first_seen_at)"
            " VALUES (?,?,?,?,?,?)",
            (e["name"], cfg["chain"], e.get("contract"), e["reason"], e["evidence"], utcnow()))
    conn.commit()
    print(f"seed done: {added} projects added as 待核实, "
          f"{len(cfg.get('excluded_known', []))} known exclusions recorded")
    return added


def cmd_status(conn, cfg):
    n_proj = conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
    by_state = conn.execute(
        "SELECT state, COUNT(*) c FROM projects GROUP BY state").fetchall()
    n_open = conn.execute(
        "SELECT COUNT(*) c FROM research_queue WHERE status='open'").fetchone()["c"]
    n_src_fail = conn.execute(
        "SELECT COUNT(*) c FROM sources WHERE consecutive_failures > 0").fetchone()["c"]
    n_excl = conn.execute("SELECT COUNT(*) c FROM excluded").fetchone()["c"]
    print(f"status: {n_proj} projects "
          f"({', '.join(f'{r['state']}={r['c']}' for r in by_state)}), "
          f"{n_open} open research tasks, {n_excl} excluded, "
          f"{n_src_fail} failing sources")
    b = cmd_budget(conn, cfg)
    return {"projects": n_proj, "open_tasks": n_open, "excluded": n_excl,
            "failing_sources": n_src_fail, "budget_paused": b["paused"]}


def cmd_init(conn, cfg):
    connect()  # creates schema
    for path, default in ((OPPORTUNITIES_PATH, {"generated_at": utcnow(), "count": 0, "opportunities": []}),
                          (RESEARCH_QUEUE_PATH, {"generated_at": utcnow(), "open_tasks": 0, "tasks": []})):
        if not path.exists():
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=2)
    if not CHANGES_PATH.exists():
        CHANGES_PATH.touch()
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"wrote example config: {CONFIG_PATH} (edit user_address + deposit caps)")
    print(f"init done: db={DB_PATH}")
    print("  next: scanner.py seed   (seed the 9 current board projects as 待核实)")


def main():
    cfg = load_config()
    args = sys.argv[1:]
    if not args:
        print(__doc__.split("Usage:")[1].strip())
        return 2
    cmd = args[0]
    conn = connect()
    try:
        if cmd == "init":
            cmd_init(conn, cfg)
        elif cmd == "seed":
            cmd_seed(conn, cfg, args[1] if len(args) > 1 else None)
            cmd_export(conn, cfg)
        elif cmd == "collect":
            cmd_collect(conn, cfg)
            cmd_export(conn, cfg)
        elif cmd == "refresh":
            cmd_refresh(conn, cfg)
            cmd_export(conn, cfg)
        elif cmd == "keywords":
            cmd_keywords(conn, cfg)
        elif cmd == "ingest" and len(args) == 2:
            cmd_ingest(conn, cfg, args[1])
            cmd_export(conn, cfg)
        elif cmd == "process-leads":
            cmd_process_leads(conn, cfg)
            cmd_export(conn, cfg)
        elif cmd == "check" and len(args) == 2:
            cmd_check(conn, cfg, args[1])
            cmd_export(conn, cfg)
        elif cmd == "set-state" and len(args) >= 3:
            pid, state = args[1], args[2]
            if state not in STATES:
                print(f"unknown state: {state} (valid: {', '.join(STATES)})")
                return 2
            row = conn.execute("SELECT state FROM projects WHERE id=?", (pid,)).fetchone()
            if not row:
                print(f"unknown project: {pid}")
                return 2
            conn.execute("UPDATE projects SET state=?, last_verified_at=? WHERE id=?",
                         (state, utcnow(), pid))
            log_change(conn, pid, "state", row["state"], state, material=1)
            conn.commit()
            print(f"state {pid}: {row['state']} -> {state} (manual)")
        elif cmd == "budget":
            rec = None
            if len(args) > 1 and args[1] == "record":
                rec = {"unavailable": "--unavailable" in args}
                for i, a in enumerate(args):
                    if a == "--in" and i + 1 < len(args):
                        rec["tokens_in"] = int(args[i + 1])
                    if a == "--out" and i + 1 < len(args):
                        rec["tokens_out"] = int(args[i + 1])
                    if a == "--usd" and i + 1 < len(args):
                        rec["amount_usd"] = float(args[i + 1])
                    if a == "--project" and i + 1 < len(args):
                        rec["project_id"] = args[i + 1]
            cmd_budget(conn, cfg, rec)
        elif cmd == "export":
            cmd_export(conn, cfg)
        elif cmd == "status":
            cmd_status(conn, cfg)
        else:
            print(f"unknown command: {cmd}")
            print(__doc__.split("Usage:")[1].strip())
            return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
