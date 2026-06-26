#!/usr/bin/env python3
"""
Scrape updated Kaltura embed codes from capture.udel.edu for all entry_ids
found in .qmd source files.

Usage:
    pip install playwright
    playwright install chromium
    python update_kaltura.py --dry-run        # print replacements only
    python update_kaltura.py                  # write changes in place

capture.udel.edu sits behind UD's SAML single sign-on, so a media page
redirects to idp.nss.udel.edu unless the browser is already authenticated.
This script uses a *persistent* Chromium profile: on the first run it opens
a visible window and waits for you to log in (including Duo MFA) once. The
session cookies are saved to PROFILE_DIR, so later runs can pick up where
you left off -- and can run headless once the session is still valid.
"""

import re
import sys
import json
import time
import argparse
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

# ── 1. collect entry_ids from source tree ──────────────────────────────────

SOURCE_ROOT = Path(".")   # adjust if running from outside the repo root
SOURCE_GLOBS = ["**/*.qmd", "**/*.md"]

# Cache of scraped embed srcs ({entry_id: new_src}) so a long run can be
# resumed after an interruption without re-scraping everything.
CACHE_FILE = Path("kaltura_embeds.json")

# Persistent browser profile so the SSO session survives between runs.
PROFILE_DIR = Path.home() / ".kaltura-playwright-profile"

# Any URL on this host means we got bounced to the UD login page.
LOGIN_HOST = "idp.nss.udel.edu"

OLD_ENTRY_PATTERN = re.compile(
    r'https://cdnapisec\.kaltura\.com/p/\d+/(?:sp/\d+/)?'
    r'(?:embedIframeJs|embedPlaykitJs)'
    r'/[^\s\'"]*?entry_id=([\w]+)'
)

def collect_entry_ids():
    """Return {entry_id: [list of source paths containing it]}"""
    mapping = {}
    for glob in SOURCE_GLOBS:
        for path in SOURCE_ROOT.glob(glob):
            text = path.read_text()
            for m in OLD_ENTRY_PATTERN.finditer(text):
                eid = m.group(1)
                mapping.setdefault(eid, []).append(path)
    return mapping

# ── 2. scrape new embed src for each entry_id ──────────────────────────────

def safe_goto(page, url: str, timeout: int = 30_000, retries: int = 2) -> None:
    """Navigate with retries. Waits for domcontentloaded (networkidle is
    unreliable here -- the player keeps background requests alive)."""
    last: Exception = RuntimeError("safe_goto: no attempt made")
    for _ in range(retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last

def find_login_link(page):
    """
    Return the MediaSpace 'Login' link element if the current page is being
    viewed as a guest, else None. capture.udel.edu serves media pages to
    anonymous users, so the URL alone can't tell us whether we're signed in --
    the presence of a Login link is the reliable signal.

    Note: the link lives inside a collapsed user menu (role=menuitem,
    tabindex=-1), so it is present in the DOM but NOT visible. We must not
    require visibility here -- we navigate via its href regardless.
    """
    for sel in (
        "a.login-btn[href*='/user/login']",
        "a[href*='/user/login']",
        "a[href*='login']",
    ):
        el = page.query_selector(sel)
        if el is not None:
            return el
    return None


def ensure_logged_in(page, sample_entry_id: str) -> None:
    """
    Navigate to a media page. If we're viewing it as a guest, kick off the UD
    sign-in flow and block on terminal input while the user authenticates
    (incl. Duo) in the visible window. The browser runs in its own process and
    stays fully interactive while Python waits on input().
    """
    url = f"https://capture.udel.edu/media/x/{sample_entry_id}"
    safe_goto(page, url, timeout=60_000)

    if LOGIN_HOST not in page.url and find_login_link(page) is None:
        return  # already authenticated

    # We're a guest -- navigate straight to the login link so the user
    # doesn't have to hunt for it.
    link = find_login_link(page)
    if link is not None:
        href = link.get_attribute("href")
        if href:
            try:
                page.goto(urljoin(page.url, href),
                          wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass  # fall back to manual navigation

    print(
        "\n"
        "────────────────────────────────────────────────────────────\n"
        " Viewing capture.udel.edu as a guest.\n"
        " Complete the UD sign-in (and Duo MFA) in the browser window,\n"
        " wait until you're signed in, THEN return here.\n"
        "────────────────────────────────────────────────────────────",
        file=sys.stderr,
    )
    input("Press Enter once you are logged in... ")

    # Re-load the target page and confirm we're no longer a guest.
    safe_goto(page, url, timeout=60_000)
    if LOGIN_HOST in page.url or find_login_link(page) is not None:
        print("  [!] Still appears to be a guest -- login may not have completed.",
              file=sys.stderr)
    else:
        print("  [+] Logged in. Continuing.\n", file=sys.stderr)


DEBUG_DIR = Path("kaltura_debug")


def _try_click(page, sel, timeout: int) -> bool:
    """Attempt to click one selector several ways: normal, then scroll+force,
    then a raw JS .click() on the underlying element. Returns True on success."""
    loc = page.locator(sel).first
    # 1. normal actionable click
    try:
        loc.click(timeout=timeout)
        return True
    except Exception:
        pass
    # 2. scroll into view and force past any overlay
    try:
        loc.scroll_into_view_if_needed(timeout=2_000)
        loc.click(timeout=2_000, force=True)
        return True
    except Exception:
        pass
    # 3. dispatch a DOM click directly
    try:
        loc.dispatch_event("click", timeout=2_000)
        return True
    except Exception:
        return False


def _click_first(page, selectors, timeout: int) -> bool:
    """Try each selector in turn; click the first that resolves. Returns True
    on success. The per-selector timeout is kept short so we fail over quickly
    to the next candidate."""
    per = max(1_500, timeout // len(selectors))
    for sel in selectors:
        if _try_click(page, sel, per):
            return True
    return False


def dump_debug(page, entry_id: str, tag: str) -> None:
    """Save a screenshot + HTML so we can see what the page actually looked
    like when a selector failed."""
    DEBUG_DIR.mkdir(exist_ok=True)
    stem = DEBUG_DIR / f"{entry_id}_{tag}"
    try:
        page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
        stem.with_suffix(".html").write_text(page.content())
        print(f"      [debug] saved {stem}.png / .html", file=sys.stderr)
    except Exception as e:
        print(f"      [debug] could not save debug artifacts: {e}", file=sys.stderr)
    # Quick inventory of likely-relevant elements.
    for sel in ("#tab-share-tab", "a[role='tab']", "[aria-controls='share-tab']"):
        try:
            n = page.locator(sel).count()
            print(f"      [debug] {sel!r} -> {n} match(es)", file=sys.stderr)
        except Exception:
            pass


def get_embed_src(page, entry_id: str) -> str | None:
    """
    Navigate to the MediaSpace media page, open Share → Embed,
    and return the iframe src string.
    """
    url = f"https://capture.udel.edu/media/x/{entry_id}"
    # Use domcontentloaded, not networkidle: MediaSpace keeps background
    # player/analytics requests alive, so the page may never go fully idle.
    safe_goto(page, url, timeout=30_000, retries=2)

    if LOGIN_HOST in page.url:
        print(f"  [!] Session expired (redirected to login) for {entry_id}",
              file=sys.stderr)
        return None

    # Click the Share tab. The label text sits in a nested <span> with a
    # leading space, so match the stable element id rather than the text.
    if not _click_first(page, [
        "#tab-share-tab",
        "a.tab-share-tab",
        "[aria-controls='share-tab']",
    ], timeout=5_000):
        print(f"  [!] Could not find Share button for {entry_id}", file=sys.stderr)
        dump_debug(page, entry_id, "no-share")
        return None

    # Click the Embed sub-tab within the Share panel.
    if not _click_first(page, [
        "#embedTextArea-pane-tab",
        "a.embedTextArea-pane-tab",
        "a:has-text('Embed')",
    ], timeout=4_000):
        print(f"  [!] Could not find Embed tab for {entry_id}", file=sys.stderr)
        dump_debug(page, entry_id, "no-embed")
        return None

    # The embed snippet lives in textarea#embedTextArea. Wait, event-driven,
    # until its value actually contains the playkit code -- this returns the
    # instant the field is populated instead of polling on a fixed interval.
    embed_code = None
    try:
        page.wait_for_function(
            """() => {
                const el = document.querySelector('#embedTextArea');
                return el && el.value && el.value.includes('embedPlaykitJs')
                    ? el.value : null;
            }""",
            timeout=5_000,
        )
        embed_code = page.locator("#embedTextArea").input_value()
    except Exception:
        pass

    # Fallback: scan any textarea on the page.
    if not (embed_code and "embedPlaykitJs" in embed_code):
        for el in page.query_selector_all("textarea"):
            val = el.input_value()
            if val and "embedPlaykitJs" in val:
                embed_code = val
                break

    if not (embed_code and "embedPlaykitJs" in embed_code):
        print(f"  [!] No embed code found for {entry_id}", file=sys.stderr)
        dump_debug(page, entry_id, "no-code")
        return None

    # Extract the src URL from the iframe HTML (the snippet uses single quotes).
    m = re.search(r"src='([^']+)'", embed_code) or re.search(r'src="([^"]+)"', embed_code)
    if not m:
        print(f"  [!] Could not parse src from embed code for {entry_id}", file=sys.stderr)
        print(f"      embed_code was: {embed_code[:200]}", file=sys.stderr)
        return None

    new_src = m.group(1)

    # Sanity check: the scraped src must reference the entry_id we asked for,
    # otherwise we'd write the wrong video's URL into the file.
    if f"entry_id={entry_id}" not in new_src:
        print(f"  [!] Scraped src does not contain entry_id={entry_id}; skipping",
              file=sys.stderr)
        print(f"      got: {new_src[:160]}", file=sys.stderr)
        return None

    return new_src

# ── 3. replace in source files ─────────────────────────────────────────────

# Matches the full iframe src attribute value for a given entry_id
def make_old_src_pattern(entry_id: str) -> re.Pattern:
    return re.compile(
        r"(src=['\"])https://cdnapisec\.kaltura\.com/p/\d+/(?:sp/\d+/)?"
        r"(?:embedIframeJs|embedPlaykitJs)/[^\s'\"]*?"
        rf"entry_id={re.escape(entry_id)}[^\s'\"]*(['\"])"
    )

def update_files(entry_map: dict[str, tuple[str, list[Path]]], dry_run: bool):
    for entry_id, (new_src, paths) in entry_map.items():
        pattern = make_old_src_pattern(entry_id)
        for path in paths:
            text = path.read_text()
            new_text = pattern.sub(lambda m: f"{m.group(1)}{new_src}{m.group(2)}", text)
            if new_text != text:
                print(f"  Updated {entry_id} in {path}")
                if not dry_run:
                    path.write_text(new_text)

# ── 4. main ────────────────────────────────────────────────────────────────

def load_cache() -> dict[str, str]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_cache(cache: dict[str, str]) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--headless", action="store_true",
        help="Run without a visible window. Only works once a valid SSO "
             "session is already saved in the profile; the first login must "
             "be done in headed mode.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only process the first N entry_ids (handy for a test run).",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Ignore the resume cache and re-scrape every entry_id.",
    )
    args = parser.parse_args()

    print("Scanning source files for entry_ids...")
    id_to_paths = collect_entry_ids()
    print(f"Found {len(id_to_paths)} unique entry_ids across source files.\n")

    if not id_to_paths:
        print("No entry_ids found; nothing to do.")
        return

    if args.limit is not None:
        id_to_paths = dict(list(id_to_paths.items())[:args.limit])
        print(f"Limiting to first {len(id_to_paths)} entry_ids.\n")

    cache = {} if args.refresh else load_cache()
    if cache:
        print(f"Loaded {len(cache)} cached embed srcs from {CACHE_FILE}.")

    # Only scrape the ids we don't already have.
    todo = {eid: paths for eid, paths in id_to_paths.items() if eid not in cache}
    print(f"{len(todo)} to scrape, {len(id_to_paths) - len(todo)} from cache.\n")

    failures = []

    with sync_playwright() as p:
        # Persistent context keeps the SSO cookies between runs.
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=args.headless,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if todo:
            # Make sure we're authenticated before grinding through every id.
            ensure_logged_in(page, next(iter(todo)))

        for i, (entry_id, _paths) in enumerate(todo.items(), 1):
            print(f"[{i}/{len(todo)}] {entry_id} ...", end=" ", flush=True)
            try:
                new_src = get_embed_src(page, entry_id)
            except Exception as e:
                # One bad page must never abort the whole run.
                new_src = None
                print(f"ERROR ({type(e).__name__})", end=" ")
            if new_src:
                print("OK")
                cache[entry_id] = new_src
                save_cache(cache)  # persist after every success -> resumable
            else:
                print("FAILED")
                failures.append(entry_id)

        ctx.close()

    # Build the replacement map from the cache (covers this run + prior runs).
    entry_map = {
        eid: (cache[eid], paths)
        for eid, paths in id_to_paths.items() if eid in cache
    }

    if failures:
        print(f"\n{len(failures)} entries FAILED this run: {', '.join(failures)}")
        print("Re-run to retry just these (cached successes are skipped).")

    print(f"\n{len(entry_map)}/{len(id_to_paths)} entries resolved.")
    print("\nApplying replacements" + (" (dry run)" if args.dry_run else "") + "...")
    update_files(entry_map, dry_run=args.dry_run)
    print("Done.")

if __name__ == "__main__":
    main()