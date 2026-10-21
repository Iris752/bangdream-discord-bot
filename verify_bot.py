"""Read-only verification of 13 BanG Dream news sources. Never posts to Discord or writes state.json."""
import argparse
import csv
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import bot

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("verify")
# Strict source-wide title verification; no page-wide body matches.
def reason(title, summary, source, date_status, link):
    name = source["name"]
    if not bot.relevant(title, summary, source):
        return "除外: タイトルにバンドリ関連語なし"
    if date_status == "old":
        return "除外: 公開7日超過"
    if date_status == "future":
        return "除外: 公開日が未来"
    if date_status == "unknown":
        return "保留: 公開日不明（安全優先）"
    return "通知候補: 公開7日以内・関連性あり"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-source", type=int, default=5, help="inspect at most N URLs per source")
    ap.add_argument("--max-seconds", type=int, default=240)
    args = ap.parse_args()
    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    enabled = [s for s in sources if s.get("enabled")]
    LOG.info("READ ONLY verification: %d enabled sources, no Discord posts, no state changes", len(enabled))
    rows = []
    started = datetime.now(timezone.utc)
    for source in enabled:
        name = source["name"]
        if (datetime.now(timezone.utc)-started).total_seconds() > args.max_seconds:
            LOG.warning("Overall time budget reached; remaining sources not checked")
            break
        try:
            links = bot.discover(source)
            LOG.info("SOURCE %s: %d URLs discovered", name, len(links))
        except Exception as exc:
            LOG.error("SOURCE %s: discovery failed: %s", name, exc)
            rows.append({"source":name,"url":"","title":"","published":"","status":"取得失敗","reason":str(exc)})
            continue
        for link in links[:max(0,args.per_source)]:
            if (datetime.now(timezone.utc)-started).total_seconds() > args.max_seconds:
                break
            try:
                # URL date is used as a conservative lower bound when present.
                url_date = bot.date_from_url(link)
                if url_date and bot.publication_status(url_date) in ("old","future"):
                    title, summary, pub = "(URLの日付で判定)", "", url_date
                else:
                    pub = bot.publication_date(link)
                    title, summary = bot.extract(link)
                status = bot.publication_status(pub)
                decision = reason(title,summary,source,status,link)
                row = {"source":name,"url":link,"title":title[:200],
                       "published":pub.isoformat() if pub else "",
                       "status":status,"reason":decision}
                rows.append(row)
                LOG.info("CHECK [%s] %s | %s | %s", name, decision, title[:90], link)
            except Exception as exc:
                rows.append({"source":name,"url":link,"title":"","published":"",
                             "status":"記事取得失敗","reason":f"{type(exc).__name__}: {exc}"})
                LOG.warning("CHECK [%s] article failed %s: %s", name, link, exc)
    out = ROOT / "verification_results.csv"
    with out.open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=["source","url","title","published","status","reason"])
        writer.writeheader()
        writer.writerows(rows)
    LOG.info("SUMMARY: %s",dict(Counter(r["reason"] for r in rows)))
    LOG.info("Wrote %s (%d rows); Discord NOT contacted; state.json NOT modified",out,len(rows))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s")
    main()
