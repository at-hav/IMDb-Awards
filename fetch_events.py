#!/usr/bin/env python3
"""
Fetches award events from IMDb and writes YAML files for sync_awards.
Uses Firefox via Playwright to handle IMDb's JS-based WAF challenges.
"""
import json, pathlib, random, re, sys, time, traceback
from datetime import datetime

try:
    import yaml
except ImportError:
    print("Requirements missing: pip install pyyaml")
    sys.exit(1)

BASE_URL      = "https://www.imdb.com/event"
CURRENT_YEAR  = datetime.now().year
MIN_YEAR = 1920             # fallback only, when historyEventEditions is absent
FETCH_ERROR   = object()    # sentinel: fetch failed (retryable), not "page has no data"
VIEWPORTS     = [(1366, 768), (1440, 900), (1920, 1080), (1280, 800)]


def _fetch_year(page, event_id, year):
    url = f"{BASE_URL}/{event_id}/{year}/1/"
    for attempt in range(2):
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
            if attempt == 0 and "NS_BINDING_ABORTED" in str(e):
                time.sleep(0.5)
                continue
            print(f"    error: {e.__class__.__name__}: {str(e)[:120]}")
            # page.title()/content() hang when the WAF challenge JS is still running
            # after a wait_for_function timeout — skip diagnostics in that case.
            if "Timeout" not in type(e).__name__:
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


def _fetch_current(page, event_id):
    """Fetch the bare event URL and follow IMDb's server-side redirect to the most recent year.

    Returns (event_name, valid_years, first_year, props).
    valid_years is a set[int] or None when historyEventEditions is absent.
    Returns (None, None, None, None) on any failure.
    """
    bare_url = f"{BASE_URL}/{event_id}/"
    for attempt in range(2):
        try:
            resp = page.goto(bare_url, wait_until="domcontentloaded", timeout=30000)
            if resp and resp.status == 404:
                print(f"  event not found (404)")
                return None, None, None, None
            page.wait_for_function(
                "() => !!document.getElementById('__NEXT_DATA__')",
                timeout=45000,
            )
            m_url = re.search(r'/event/\w+/(\d{4})/', page.url)
            first_year = int(m_url.group(1)) if m_url else None
            content = page.content()
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', content, re.DOTALL)
            if not m:
                print(f"  no __NEXT_DATA__ after redirect")
                return None, None, None, None
            props = json.loads(m.group(1))["props"]["pageProps"]
            event_name = props.get("eventName", event_id)
            hist = props.get("historyEventEditions", [])
            valid_years = {h["year"] for h in hist} if hist else None
            if valid_years is None:
                print(f"  historyEventEditions absent — will use fallback range")
            else:
                print(f"  found {len(valid_years)} valid years (latest: {first_year})")
            return event_name, valid_years, first_year, props
        except Exception as e:
            if attempt == 0 and "NS_BINDING_ABORTED" in str(e):
                time.sleep(0.5)
                continue
            print(f"  error: {e.__class__.__name__}: {str(e)[:120]}")
            if "Timeout" not in type(e).__name__:
                try:
                    print(f"  page title: {page.title()!r}")
                    print(f"  snippet: {page.content()[:300]!r}")
                except Exception:
                    pass
            return None, None, None, None


def fetch_event(page, event_id, events_dir, retry_years=frozenset()):
    out_path = pathlib.Path(events_dir) / f"{event_id}.yml"
    yaml_data = {}
    event_name = None

    if out_path.exists():
        content = out_path.read_text()
        first_line = content.split("\n", 1)[0]
        if first_line.startswith("# "):
            event_name = first_line[2:].strip()
        try:
            yaml_data = yaml.safe_load(content) or {}
        except yaml.YAMLError:
            print(f"  warning: corrupt YAML — will re-fetch")
            yaml_data = {}

    # Fetch bare URL — IMDb redirects to most recent ceremony year.
    # This gives us the actual latest year + historyEventEditions in one request,
    # for both new events and incremental updates.
    event_name_current, valid_years, first_year, first_props = _fetch_current(page, event_id)
    if first_props is None:
        return set(retry_years) or {CURRENT_YEAR}  # ensure event stays in the summary retry map for visibility

    if event_name is None:
        event_name = event_name_current

    error_years = set()
    new_years = 0

    # Store the current year's data if we don't already have it
    if first_year is not None and str(first_year) not in yaml_data:
        awards = _parse_awards(first_props)
        if awards:
            yaml_data[str(first_year)] = awards
            new_years += 1
            print(f"  {first_year}: {len(awards)} awards, {sum(len(v) for v in awards.values())} categories")

    # Determine remaining years to fetch
    existing_years = {int(k) for k in yaml_data if k.isdigit()}
    if valid_years is not None:
        to_fetch = (valid_years - existing_years) | set(retry_years)
    else:
        to_fetch = (set(range(CURRENT_YEAR - 1, MIN_YEAR - 1, -1)) - existing_years) | set(retry_years)
    if first_year is not None:
        to_fetch.discard(first_year)

    years_to_fetch = list(to_fetch)
    random.shuffle(years_to_fetch)
    consecutive_errors = 0
    for i, year in enumerate(years_to_fetch):
        if i > 0:
            time.sleep(random.uniform(0.5, 1.5))
        result = _fetch_year(page, event_id, year)
        if result is FETCH_ERROR:
            error_years.add(year)
            print(f"  {year}: fetch error")
            consecutive_errors += 1
            if consecutive_errors >= 3:
                remaining = years_to_fetch[i + 1:]
                error_years.update(remaining)
                if remaining:
                    print(f"  aborting after 3 consecutive errors — {len(remaining)} years queued for retry")
                break
            continue
        consecutive_errors = 0
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

    tmp_path = out_path.with_suffix(".tmp")
    with tmp_path.open("w") as f:
        if event_name:
            f.write(f"# {event_name}\n")
        yaml.dump(yaml_data, f, default_flow_style=False, allow_unicode=True, sort_keys=True)
    tmp_path.replace(out_path)
    print(f"  written: {out_path}")
    return error_years


def _build_summary(events_dir, retry_map, duration, failed=False):
    """Machine-readable record of a fetch run: per-event data stats plus run metadata."""
    events = {}
    for yml_path in sorted(pathlib.Path(events_dir).glob("ev*.yml")):
        eid = yml_path.stem
        try:
            text = yml_path.read_text(encoding="utf-8")
        except Exception:
            continue
        first_line = text.split("\n", 1)[0]
        name = first_line[2:].strip() if first_line.startswith("# ") else eid
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            data = {}
        years = sorted(int(y) for y in data if str(y).isdigit())
        awards = cats = 0
        for yr_val in data.values():
            if not isinstance(yr_val, dict):
                continue
            awards += len(yr_val)
            for cat_val in yr_val.values():
                if isinstance(cat_val, dict):
                    cats += len(cat_val)
        events[eid] = {"name": name, "years": years, "awards": awards, "categories": cats}
    return {
        "updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "failed": failed,
        "duration": duration,
        "retry": {k: sorted(v) for k, v in sorted(retry_map.items())},
        "events": events,
    }


def _write_summary(events_dir, summary):
    with (pathlib.Path(events_dir).parent / "summary.yml").open("w") as f:
        yaml.dump(summary, f, default_flow_style=None, allow_unicode=True, sort_keys=False)


def _write_readme(events_dir, summary):
    events = summary["events"]
    lines = [
        "# IMDb-Awards\n\n",
        "IMDb award event data. Auto-updated nightly by GitHub Actions.\n\n",
        f"## Events ({len(events)})\n\n",
        "| Event ID | Name | Years | Awards | Categories |\n",
        "|---|---|---:|---:|---:|\n",
    ]
    for eid in sorted(events):
        e = events[eid]
        lines.append(
            f"| [{eid}](events/{eid}.yml) | {e['name']} | {len(e['years'])} | {e['awards']} | {e['categories']} |\n"
        )

    if summary["retry"]:
        lines.append("\n## Pending Retries\n\n| Event ID | Name |\n|---|---|\n")
        for eid in sorted(summary["retry"]):
            name = events[eid]["name"] if eid in events else eid
            lines.append(f"| {eid} | {name} |\n")

    when = datetime.strptime(summary["updated"], "%Y-%m-%dT%H:%M:%SZ").strftime("%B %d, %Y %H:%M UTC")
    footer = f"**Failed** {when}" if summary["failed"] else f"Last updated {when}"
    lines.append(f"\n---\n_{footer}, duration {summary['duration']}_\n")
    (pathlib.Path(events_dir).parent / "README.md").write_text("".join(lines), encoding="utf-8")


if __name__ == "__main__":
    # Force line-buffered output so GHA streams logs in real time
    sys.stdout.reconfigure(line_buffering=True)

    base_dir   = pathlib.Path(__file__).parent
    events_dir = base_dir / "events"
    events_dir.mkdir(exist_ok=True)

    # Pending retries live in summary.yml; carry them over from the previous run.
    summary_path = base_dir / "summary.yml"
    try:
        prev_summary = (yaml.safe_load(summary_path.read_text()) if summary_path.exists() else None) or {}
    except yaml.YAMLError:
        prev_summary = {}
    retry_map = dict(prev_summary.get("retry") or {})

    if "--rebuild-summary" in sys.argv[1:]:
        summary = _build_summary(events_dir, retry_map, "00:00:00")
        _write_summary(events_dir, summary)
        _write_readme(events_dir, summary)
        print("summary.yml and README.md rebuilt from events on disk")
        sys.exit(0)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Requirements missing: pip install playwright")
        sys.exit(1)

    ids_file = base_dir / "event_ids.yml"
    if not ids_file.exists():
        print("event_ids.yml not found — nothing to do")
        sys.exit(0)

    with ids_file.open() as f:
        config = yaml.safe_load(f)
    event_ids = list(dict.fromkeys(
        str(e).split()[0] for e in (config.get("event_ids") or [])
    ))

    if not event_ids:
        print("No event IDs configured")
        sys.exit(0)

    with sync_playwright() as pw:
        browser = pw.firefox.launch()

        w, h = random.choice(VIEWPORTS)
        ctx = browser.new_context(viewport={"width": w, "height": h})

        run_start = time.time()

        # Health check: pre-warm IMDb session and confirm WAF challenge resolves.
        # If the homepage doesn't load, this runner IP is blocked — fail fast.
        print("Health check: loading imdb.com...")
        warmup_page = None
        try:
            warmup_page = ctx.new_page()
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
            elapsed = int(time.time() - run_start)
            duration = f"{elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"
            summary = _build_summary(events_dir, retry_map, duration, failed=True)
            _write_summary(events_dir, summary)
            _write_readme(events_dir, summary)
            ctx.close()
            browser.close()
            sys.exit(1)

        print(f"\nFetching {len(event_ids)} event(s)\n")
        random.shuffle(event_ids)
        for i, eid in enumerate(event_ids):
            print(f"[{eid}]")
            page = ctx.new_page()
            try:
                errors = fetch_event(page, eid, events_dir, set(retry_map.get(eid, [])))
            except Exception as e:
                print(f"  unexpected error: {e.__class__.__name__}: {e}")
                traceback.print_exc()
                errors = set(retry_map.get(eid, [])) or {CURRENT_YEAR}
            finally:
                page.close()
            if errors:
                retry_map[eid] = sorted(errors)
            else:
                retry_map.pop(eid, None)
            if i < len(event_ids) - 1:
                delay = random.uniform(2, 5)
                print(f"  pausing {delay:.1f}s")
                time.sleep(delay)

        elapsed = int(time.time() - run_start)
        duration = f"{elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"
        summary = _build_summary(events_dir, retry_map, duration)
        _write_summary(events_dir, summary)
        _write_readme(events_dir, summary)

        ctx.close()
        browser.close()

    print("\nDone.")
