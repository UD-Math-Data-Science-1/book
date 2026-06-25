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
import time
import argparse
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

# ── 1. collect entry_ids from source tree ──────────────────────────────────

SOURCE_ROOT = Path(".")   # adjust if running from outside the repo root
SOURCE_GLOBS = ["**/*.qmd", "**/*.md"]

# Persistent browser profile so the SSO session survives between runs.
PROFILE_DIR = Path.home() / ".kaltura-playwright-profile"

# Any URL on this host means we got bounced to the UD login page.
LOGIN_HOST = "idp.nss.udel.edu"

OLD_ENTRY_PATTERN = re.compile(
    r'https://cdnapisec\.kaltura\.com/p/\d+/(?:embedIframeJs|embedPlaykitJs)'
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
    page.goto(url, wait_until="networkidle", timeout=60_000)

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
    page.goto(url, wait_until="networkidle", timeout=60_000)
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
    page.goto(url, wait_until="networkidle", timeout=30_000)

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
    ], timeout=8_000):
        print(f"  [!] Could not find Share button for {entry_id}", file=sys.stderr)
        dump_debug(page, entry_id, "no-share")
        return None

    # Click the Embed sub-tab within the Share panel.
    if not _click_first(page, [
        "#embedTextArea-pane-tab",
        "a.embedTextArea-pane-tab",
        "a:has-text('Embed')",
    ], timeout=5_000):
        print(f"  [!] Could not find Embed tab for {entry_id}", file=sys.stderr)
        dump_debug(page, entry_id, "no-embed")
        return None

    # The embed snippet lives in textarea#embedTextArea; wait for it to be
    # populated with the playkit code.
    embed_code = None
    try:
        ta = page.locator("#embedTextArea")
        ta.wait_for(state="attached", timeout=5_000)
        for _ in range(20):  # poll up to ~5s for the value to render
            embed_code = ta.input_value()
            if embed_code and "embedPlaykitJs" in embed_code:
                break
            time.sleep(0.25)
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
        r"(src=['\"])https://cdnapisec\.kaltura\.com/p/\d+/"
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

    entry_map = {}  # entry_id -> (new_src, paths)

    with sync_playwright() as p:
        # Persistent context keeps the SSO cookies between runs.
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=args.headless,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Make sure we're authenticated before grinding through every id.
        first_id = next(iter(id_to_paths))
        ensure_logged_in(page, first_id)

        for i, (entry_id, paths) in enumerate(id_to_paths.items(), 1):
            print(f"[{i}/{len(id_to_paths)}] {entry_id} ...", end=" ", flush=True)
            new_src = get_embed_src(page, entry_id)
            if new_src:
                print(f"OK")
                entry_map[entry_id] = (new_src, paths)
            else:
                print(f"FAILED")
            time.sleep(0.5)  # be polite

        ctx.close()

    print(f"\n{len(entry_map)}/{len(id_to_paths)} entries resolved.")
    print("\nApplying replacements" + (" (dry run)" if args.dry_run else "") + "...")
    update_files(entry_map, dry_run=args.dry_run)
    print("Done.")

if __name__ == "__main__":
    main()