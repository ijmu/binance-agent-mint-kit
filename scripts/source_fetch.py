#!/usr/bin/env python3
"""Fetch the 4 machine-readable free-mint sources (local HTTP, read-only).

Replaces the broken HTML scrapes the hourly scan used to do:
  - Boltbook Alphapedia daily page (404) -> public JSON API
  - Binance Square news page (202 / WAF block) -> Binance announcements
    CMS API (the news feed behind Square news)
  - Mint RedCeh page (JS-rendered) -> /api/board JSON
  - MintRadar page (JS-rendered) -> per-contract /data/kontrak/<addr>.json
Agent Radar has no verified machine interface and MINTS stopped updating
(2026-11-11); both are intentionally NOT fetched here.

Stdlib only (urllib). No login, no keys, no wallet, no signing.
Each source returns: status ok|fail|not_found, http_status, items,
a small sample, and an error note when it fails. A failed source is
reported as a failure, never as proof that no projects exist.

Usage:
  python3 source_fetch.py [--source boltbook|binance_cms|mint_redceh|mintradar]
                          [--contract ADDR] [--out FILE]
  No --source: fetch all four. --contract is required for mintradar
  (default: MUSE sale contract). --out writes the unified JSON to a file
  as well as printing it.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
TIMEOUT = 20
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) free-mint-local-fetch/1.0"}

BOLTBOOK_URL = "https://boltbook.fun/api/v1/alpha?sort=new&kind=token"
BINANCE_CMS_URL = ("https://www.binance.com/bapi/apex/v1/public/apex/cms/"
                   "article/list/query?type=1&pageNo=1&pageSize=10&catalogId=49")
REDCEH_URL = "https://mint-redceh.vercel.app/api/board"
MINT_RADAR_URL = "https://datt.my/data/kontrak/{addr}.json"

# Default probe contract for MintRadar: MUSE sale (PremiereHall).
DEFAULT_CONTRACT = "0x779fa133848d23052B7345F63B3b2E5309cd13ed"


def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M")


def fetch_json(url):
    """GET url, return (http_status, parsed_json_or_None, error_note)."""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, None, "HTTP %d%s" % (
            e.code, (" " + body[:120]) if body else "")
    except urllib.error.URLError as e:
        return None, None, "连接失败(%s)" % (e.reason or "unknown")
    except Exception as e:  # noqa: BLE001
        return None, None, "请求失败(%s: %s)" % (type(e).__name__, e)
    try:
        return status, json.loads(body), ""
    except Exception as e:  # noqa: BLE001
        return status, None, "返回不是 JSON(%s)，前 120 字符: %s" % (
            e, body[:120])


def fetch_boltbook():
    url = BOLTBOOK_URL
    status, data, err = fetch_json(url)
    out = {"source": "boltbook", "url": url, "http_status": status,
           "status": "fail", "items": 0, "sample": [], "error": err,
           "fetched_at": now_cst()}
    if data is None:
        return out
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        out["error"] = "返回结构无 projects 列表"
        return out
    out["status"] = "ok"
    out["items"] = len(projects)
    out["sample"] = [
        {k: p.get(k) for k in ("slug", "name", "contract", "updated_at")
         if p.get(k) is not None}
        for p in projects[:3]]
    updated = [p.get("updated_at") for p in projects if p.get("updated_at")]
    out["newest_updated_at"] = max(updated) if updated else None
    return out


def fetch_binance_cms():
    url = BINANCE_CMS_URL
    status, data, err = fetch_json(url)
    out = {"source": "binance_cms", "url": url, "http_status": status,
           "status": "fail", "items": 0, "sample": [], "error": err,
           "fetched_at": now_cst(),
           "note": "币安公告 CMS 接口（Latest Binance News，catalogId=49），"
                   "是 Square news 频道的底层新闻源，不是 Square 帖子本身"}
    if data is None:
        return out
    if not isinstance(data, dict) or data.get("code") != "000000":
        out["error"] = "返回 code 不是 000000: %s" % (
            str(data)[:200] if data is not None else "无数据")
        return out
    try:
        catalogs = data["data"]["catalogs"]
        articles = catalogs[0]["articles"]
    except (KeyError, IndexError, TypeError):
        out["error"] = "返回结构无 catalogs/articles"
        return out
    out["status"] = "ok"
    out["items"] = len(articles)
    out["sample"] = [
        {"title": a.get("title"), "releaseDate": a.get("releaseDate")}
        for a in articles[:3]]
    out["catalog_name"] = catalogs[0].get("catalogName")
    out["catalog_total"] = catalogs[0].get("total")
    return out


def _as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("listings", "data", "board", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return None


def fetch_redceh():
    url = REDCEH_URL
    status, data, err = fetch_json(url)
    out = {"source": "mint_redceh", "url": url, "http_status": status,
           "status": "fail", "items": 0, "sample": [], "error": err,
           "fetched_at": now_cst(),
           "note": "多数是 NFT 条目，代币条目较少；标签 real/berpotensi/indikasi "
                   "对应 Clear/Potential/Fee-Farm"}
    if data is None:
        return out
    listings = _as_list(data)
    if listings is None:
        out["error"] = "返回结构无 listings 列表"
        return out
    out["status"] = "ok"
    out["items"] = len(listings)
    out["sample"] = [
        {k: e.get(k) for k in ("name", "slug", "contract", "checkedAt",
                               "bucket", "status")
         if isinstance(e, dict) and e.get(k) is not None}
        for e in listings[:3]]
    checked = [e.get("checkedAt") for e in listings
               if isinstance(e, dict) and e.get("checkedAt")]
    out["newest_checked_at"] = max(checked) if checked else None
    return out


def fetch_mintradar(contract):
    # skill.md: address must be lower-case in the URL.
    url = MINT_RADAR_URL.format(addr=contract.lower())
    status, data, err = fetch_json(url)
    out = {"source": "mintradar", "url": url, "http_status": status,
           "status": "fail", "items": 0, "sample": [], "error": err,
           "fetched_at": now_cst(), "contract": contract}
    if status == 404:
        out["status"] = "not_found"
        out["error"] = "HTTP 404：端点活着，该合约未被 MintRadar 收录"
        return out
    if data is None:
        return out
    out["status"] = "ok"
    out["items"] = 1
    if isinstance(data, dict):
        out["sample"] = [{k: data.get(k) for k in (
            "name", "contract", "address", "isAgentId", "agentId",
            "flags", "checkedAt", "mints", "status")
            if data.get(k) is not None}]
    else:
        out["sample"] = [str(data)[:300]]
    return out


FETCHERS = {
    "boltbook": lambda contract: fetch_boltbook(),
    "binance_cms": lambda contract: fetch_binance_cms(),
    "mint_redceh": lambda contract: fetch_redceh(),
    "mintradar": lambda contract: fetch_mintradar(contract),
}


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Fetch the 4 machine-readable free-mint sources.")
    p.add_argument("--source", choices=sorted(FETCHERS),
                   help="fetch one source only (default: all four)")
    p.add_argument("--contract", default=DEFAULT_CONTRACT,
                   help="contract address for mintradar (default: MUSE sale)")
    p.add_argument("--out", default=None,
                   help="also write the unified JSON to this file")
    args = p.parse_args(argv)

    names = [args.source] if args.source else sorted(FETCHERS)
    sources = {}
    for name in names:
        try:
            sources[name] = FETCHERS[name](args.contract)
        except Exception as e:  # noqa: BLE001 - one source must not kill the rest
            sources[name] = {"source": name, "status": "fail",
                             "http_status": None, "items": 0, "sample": [],
                             "error": "本地异常(%s: %s)" % (type(e).__name__, e),
                             "fetched_at": now_cst()}
    result = {"fetched_at": now_cst(), "sources": sources}
    text = json.dumps(result, ensure_ascii=False, indent=1)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    failed = [n for n, s in sources.items() if s["status"] == "fail"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
