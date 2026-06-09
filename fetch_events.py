#!/usr/bin/env python3
"""
Fetches award events from IMDb and writes YAML files for sync_awards.
Uses Firefox via Playwright to handle IMDb's JS-based WAF challenges.
"""
import json, pathlib, re, sys
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
    import yaml
except ImportError:
    print("Requirements missing: pip install playwright pyyaml")
    sys.exit(1)

BASE_URL      = "https://www.imdb.com/event"
CURRENT_YEAR  = datetime.now().year
MIN_YEAR = 1920             # fallback only, when historyEventEditions is absent
FETCH_ERROR   = object()    # sentinel: fetch failed (retryable), not "page has no data"


def _fetch_year(page, event_id, year):
    url = f"{BASE_URL}/{event_id}/{year}/1/"
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if resp and resp.status == 404:
            print(f"    404 ({url})")
            return None
        # IMDb serves a JS challenge first (202); wait up to 45s for it to resolve.
        page.wait_for_function(
            "() => !!document.getElementById('__NEXT_DATA__')",
            timeout=45000,
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
        print(f"    error: {e.__class__.__name__}: {str(e)[:120]}")
        try:
            print(f"    page title: {page.title()!r}")
            print(f"    snippet: {page.content()[:300]!r}")
        except Exception:
            pass
        return FETCH_ERROR


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


def _fetch_event_history(page, event_id):
    """Fetch the most recent year to get the canonical year list from historyEventEditions.

    Tries CURRENT_YEAR, CURRENT_YEAR-1, CURRENT_YEAR-2 (3 attempts). Returns
    (event_name, valid_years, error_years, first_year, first_props).
    valid_years is a set[int] or None when historyEventEditions is absent.
    On complete failure returns (None, None, error_years, None, None).
    """
    error_years = set()
    for attempt in range(3):
        year = CURRENT_YEAR - attempt
        result = _fetch_year(page, event_id, year)
        if result is FETCH_ERROR:
            error_years.add(year)
            print(f"  history attempt {attempt + 1}/3 failed (year {year})")
            continue
        if result is None:
            print(f"  {year}: no data, trying previous year")
            continue
        event_name = result.get("eventName", event_id)
        hist = result.get("historyEventEditions", [])
        valid_years = {h["year"] for h in hist} if hist else None
        if valid_years is None:
            print(f"  historyEventEditions absent — will use fallback range")
        else:
            print(f"  found {len(valid_years)} valid years via historyEventEditions")
        return event_name, valid_years, error_years, year, result
    print(f"  all 3 history attempts failed")
    return None, None, error_years, None, None


def fetch_event(page, event_id, events_dir, retry_years=frozenset()):
    out_path = pathlib.Path(events_dir) / f"{event_id}.yml"
    yaml_data = {}
    event_name = None

    if out_path.exists():
        content = out_path.read_text()
        first_line = content.split("\n", 1)[0]
        if first_line.startswith("# "):
            event_name = first_line[2:].strip()
        yaml_data = yaml.safe_load(content) or {}

    error_years = set()
    new_years = 0

    if yaml_data:
        # Incremental: YAML covers history; retry.yml catches errors.
        # Only check the current year + explicit retry years.
        to_fetch = {CURRENT_YEAR} | set(retry_years)
    else:
        # New event: use historyEventEditions to discover the full year list.
        event_name_hist, valid_years, error_years, first_year, first_props = \
            _fetch_event_history(page, event_id)
        if event_name is None:
            event_name = event_name_hist
        if first_props is None:
            return error_years
        awards = _parse_awards(first_props)
        if awards:
            yaml_data[str(first_year)] = awards
            new_years += 1
            print(f"  {first_year}: {len(awards)} awards, {sum(len(v) for v in awards.values())} categories")
        if valid_years is not None:
            to_fetch = (valid_years | set(retry_years)) - {first_year}
        else:
            to_fetch = (set(range(CURRENT_YEAR - 1, MIN_YEAR - 1, -1)) | set(retry_years)) - {first_year}

    for year in sorted(to_fetch, reverse=True):
        result = _fetch_year(page, event_id, year)
        if result is FETCH_ERROR:
            error_years.add(year)
            print(f"  {year}: fetch error")
            continue
        if result is None:
            print(f"  {year}: no data")
            continue
        if event_name is None:
            event_name = result.get("eventName", event_id)
        awards = _parse_awards(result)
        if awards:
            yaml_data[str(year)] = awards
            new_years += 1
            print(f"  {year}: {len(awards)} awards, {sum(len(v) for v in awards.values())} categories")

    if not yaml_data and not error_years:
        print(f"  no data fetched for {event_id}")
        return error_years
    if not new_years and not error_years:
        print(f"  {event_id}: up to date")
        return error_years

    with out_path.open("w") as f:
        if event_name:
            f.write(f"# {event_name}\n")
        yaml.dump(yaml_data, f, default_flow_style=False, allow_unicode=True, sort_keys=True)
    print(f"  written: {out_path}")
    return error_years


if __name__ == "__main__":
    import os

    # Force line-buffered output so GHA streams logs in real time
    sys.stdout.reconfigure(line_buffering=True)

    base_dir   = pathlib.Path(__file__).parent
    events_dir = base_dir / "events"
    events_dir.mkdir(exist_ok=True)

    ids_file = base_dir / "event_ids.yml"
    if not ids_file.exists():
        print("event_ids.yml not found — nothing to do")
        sys.exit(0)

    with ids_file.open() as f:
        config = yaml.safe_load(f)
    our_ids = [str(e).split()[0] for e in (config.get("event_ids") or [])]

    # Optionally merge Kometa's event list (set via KOMETA_EVENT_IDS env var in GHA)
    kometa_ids_path = os.environ.get("KOMETA_EVENT_IDS")
    if kometa_ids_path:
        with open(kometa_ids_path) as f:
            kometa_ids = [str(e).split()[0] for e in (yaml.safe_load(f).get("event_ids") or [])]
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

    with sync_playwright() as pw:
        browser = pw.firefox.launch()

        # Health check: pre-warm IMDb session and confirm WAF challenge resolves.
        # If the homepage doesn't load, this runner IP is blocked — fail fast.
        print("Health check: loading imdb.com...")
        warmup_page = None
        try:
            warmup_page = browser.new_page()
            warmup_page.goto("https://www.imdb.com/", wait_until="domcontentloaded", timeout=30000)
            warmup_page.wait_for_function("() => document.title.length > 5", timeout=30000)
            print(f"  OK — {warmup_page.title()!r}")
            warmup_page.close()
        except Exception as e:
            print(f"  FAILED — {e.__class__.__name__}: {str(e)[:120]}")
            if warmup_page:
                try:
                    print(f"  title: {warmup_page.title()!r}")
                    print(f"  snippet: {warmup_page.content()[:300]!r}")
                except Exception:
                    pass
            print("Aborting: WAF may be blocking this runner IP.")
            browser.close()
            sys.exit(1)

        print(f"\nFetching {len(event_ids)} event(s)\n")
        retry_path = events_dir / "retry.yml"
        retry_map  = (yaml.safe_load(retry_path.read_text()) if retry_path.exists() else None) or {}

        for eid in event_ids:
            print(f"[{eid}]")
            with browser.new_context() as ctx:
                errors = fetch_event(ctx.new_page(), eid, events_dir, set(retry_map.get(eid, [])))
            if errors:
                retry_map[eid] = sorted(errors)
            else:
                retry_map.pop(eid, None)

        if retry_map:
            retry_path.write_text(yaml.dump(retry_map, default_flow_style=False, sort_keys=True))
        else:
            retry_path.unlink(missing_ok=True)

        browser.close()

    print("\nDone.")
