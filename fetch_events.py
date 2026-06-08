#!/usr/bin/env python3
"""
Fetches award events year-by-year and writes YAML in Kometa format.
Uses a real browser to handle JS-based access challenges.
"""
import json, pathlib, re, sys
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
    from ruamel.yaml import YAML
except ImportError:
    print("Requirements missing: pip install playwright ruamel.yaml")
    sys.exit(1)

BASE_URL      = "https://www.imdb.com/event"
CURRENT_YEAR  = datetime.now().year
YEAR_LOOKBACK = 20
MAX_MISSES    = 3


def _fetch_year(page, event_id, year):
    url = f"{BASE_URL}/{event_id}/{year}/1/"
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        # IMDb serves a JS challenge first (202); wait up to 20s for it to resolve
        # and the real page to load.
        page.wait_for_function(
            "() => !!document.getElementById('__NEXT_DATA__')",
            timeout=20000,
        )
        content = page.content()
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
            content,
            re.DOTALL,
        )
        if not m:
            print(f"    no __NEXT_DATA__ ({url})")
            print(f"    title: {page.title()!r}  len: {len(content)}")
            print(f"    snippet: {content[:400]!r}")
            return None
        return json.loads(m.group(1))["props"]["pageProps"]
    except Exception as e:
        print(f"    error: {e}")
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


def fetch_event(page, event_id, events_dir):
    yaml_data = {}
    event_name = None
    misses = 0

    for year in range(CURRENT_YEAR, CURRENT_YEAR - YEAR_LOOKBACK - 1, -1):
        props = _fetch_year(page, event_id, year)
        if props is None:
            misses += 1
            print(f"  {year}: no data (miss {misses}/{MAX_MISSES})")
            if misses >= MAX_MISSES:
                print(f"  stopping after {MAX_MISSES} consecutive misses")
                break
            continue
        misses = 0
        if event_name is None:
            event_name = props.get("eventName", event_id)
        awards = _parse_awards(props)
        if awards:
            yaml_data[str(year)] = awards
            print(f"  {year}: {len(awards)} awards, {sum(len(v) for v in awards.values())} categories")

    if not yaml_data:
        print(f"  no data fetched for {event_id}")
        return False

    out_path = pathlib.Path(events_dir) / f"{event_id}.yml"
    with out_path.open("w") as f:
        if event_name:
            f.write(f"# {event_name}\n")
        ry = YAML()
        ry.default_flow_style = False
        ry.dump(yaml_data, f)
    print(f"  written: {out_path}")
    return True


if __name__ == "__main__":
    import os

    base_dir   = pathlib.Path(__file__).parent
    events_dir = base_dir / "events"
    events_dir.mkdir(exist_ok=True)

    ids_file = base_dir / "event_ids.yml"
    if not ids_file.exists():
        print("event_ids.yml not found — nothing to do")
        sys.exit(0)

    with ids_file.open() as f:
        config = YAML().load(f)
    our_ids = [str(e).split()[0] for e in (config.get("event_ids") or [])]

    # Optionally merge Kometa's event list (set via KOMETA_EVENT_IDS env var in GHA)
    kometa_ids_path = os.environ.get("KOMETA_EVENT_IDS")
    if kometa_ids_path:
        with open(kometa_ids_path) as f:
            kometa_ids = [str(e).split()[0] for e in (YAML().load(f).get("event_ids") or [])]
        print(f"Loaded {len(kometa_ids)} events from Kometa, {len(our_ids)} custom events")
    else:
        kometa_ids = []

    seen = set()
    event_ids = []
    for eid in kometa_ids + our_ids:
        if eid not in seen:
            seen.add(eid)
            event_ids.append(eid)

    if not event_ids:
        print("No event IDs configured")
        sys.exit(0)

    print(f"Fetching {len(event_ids)} event(s)\n")
    with sync_playwright() as pw:
        browser = pw.firefox.launch()
        page = browser.new_page()
        for eid in event_ids:
            print(f"[{eid}]")
            fetch_event(page, eid, events_dir)
        browser.close()

    print("\nDone.")
