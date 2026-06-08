#!/usr/bin/env python3
"""
Fetches award events whose base URL redirects (no year-enumeration page).
Requests year-specific pages directly and writes YAML in Kometa format.
"""
import json, pathlib, re, sys, time
from datetime import datetime

try:
    import cloudscraper
    from ruamel.yaml import YAML
except ImportError:
    print("Requirements missing: pip install cloudscraper ruamel.yaml")
    sys.exit(1)

BASE_URL     = "https://www.imdb.com/event"
HEADERS      = {"Accept-Language": "en-US,en;q=0.9"}
CURRENT_YEAR = datetime.now().year
YEAR_LOOKBACK = 20
MAX_MISSES    = 3


def _fetch_year(session, event_id, year):
    url = f"{BASE_URL}/{event_id}/{year}/1/"
    resp = session.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
    if resp.status_code != 200:
        print(f"    HTTP {resp.status_code} ({url})")
        return None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', resp.text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError):
        return None


def _parse_awards(props):
    result = {}
    for award in props.get("edition", {}).get("awards", []):
        award_key = award.get("text", "").lower()
        award_data = {}
        for cat_edge in award.get("nominationCategories", {}).get("edges", []):
            node = cat_edge["node"]
            cat = node.get("category")
            cat_key = cat["text"].lower() if cat else award_key
            winners, nominees = [], []
            for nom_edge in node.get("nominations", {}).get("edges", []):
                nom = nom_edge["node"]
                ids = []
                entities = nom.get("awardedEntities", {})
                for t in entities.get("awardTitles", []):
                    if "id" in t.get("title", {}):
                        ids.append(t["title"]["id"])
                for n in entities.get("awardNames", []):
                    if "id" in n.get("name", {}):
                        ids.append(n["name"]["id"])
                (winners if nom.get("isWinner") else nominees).extend(ids)
            entry = {}
            if winners:
                entry["winner"] = sorted(set(winners))
            if nominees:
                entry["nominee"] = sorted(set(nominees))
            if entry:
                award_data[cat_key] = entry
        if award_data:
            result[award_key] = award_data
    return result


def fetch_event(event_id, events_dir):
    session = cloudscraper.create_scraper()
    yaml_data = {}
    event_name = None
    misses = 0

    for year in range(CURRENT_YEAR, CURRENT_YEAR - YEAR_LOOKBACK - 1, -1):
        props = _fetch_year(session, event_id, year)
        if props is None:
            misses += 1
            print(f"  {year}: no data (miss {misses}/{MAX_MISSES})")
            if misses >= MAX_MISSES:
                print(f"  Stopping after {MAX_MISSES} consecutive misses")
                break
            continue
        misses = 0
        if event_name is None:
            event_name = props.get("eventName", event_id)
        awards = _parse_awards(props)
        if awards:
            yaml_data[str(year)] = awards
            print(f"  {year}: {len(awards)} awards, {sum(len(v) for v in awards.values())} categories")
        time.sleep(1)

    if not yaml_data:
        print(f"  No data scraped for {event_id}")
        return False

    out_path = pathlib.Path(events_dir) / f"{event_id}.yml"
    with out_path.open("w") as f:
        if event_name:
            f.write(f"# {event_name}\n")
        ry = YAML()
        ry.default_flow_style = False
        ry.dump(yaml_data, f)
    print(f"  Written: {out_path}")
    return True


if __name__ == "__main__":
    base_dir   = pathlib.Path(__file__).parent
    events_dir = base_dir / "events"
    events_dir.mkdir(exist_ok=True)

    ids_file = base_dir / "redirect_event_ids.yml"
    if not ids_file.exists():
        print("redirect_event_ids.yml not found — nothing to do")
        sys.exit(0)

    with ids_file.open() as f:
        config = YAML().load(f)
    event_ids = [str(e).split()[0] for e in (config.get("event_ids") or [])]

    if not event_ids:
        print("No redirect event IDs configured")
        sys.exit(0)

    print(f"Fetching {len(event_ids)} redirect event(s)\n")
    for eid in event_ids:
        print(f"[{eid}]")
        fetch_event(eid, events_dir)

    print("\nDone.")
