#!/usr/bin/env python3
"""Assemble the deployable site from the built page.

    python build_site.py --firebase firebase.json

Takes `dist/octagon-draft.html` and produces `site/` — a complete static site
that installs as an app and syncs the draft live:

    site/index.html              the league, with the Firebase config injected
    site/manifest.webmanifest    makes it installable rather than a bookmark
    site/sw.js                   offline shell; network-first so scores stay fresh
    site/icon-*.png              home-screen icons

Point GitHub Pages at that folder and your friends need nothing but the link.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FB_TAG = re.compile(
    r'(<script id="firebase-config" type="application/json">)(.*?)(</script>)', re.S)
STATE_TAG = re.compile(
    r'(<script id="league-state" type="application/json">)(.*?)(</script>)', re.S)

MANIFEST_LINK = (
    '<link rel="manifest" href="./manifest.webmanifest">\n'
    '<link rel="apple-touch-icon" href="./apple-touch-icon.png">\n')

SW_REGISTER = """
<script>
/* Offline shell. Network-first for the page itself so a republished league is
   never masked by a stale cache — the cache is a fallback for a dead train
   tunnel, not a performance trick. */
if ("serviceWorker" in navigator) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("./sw.js").catch(function () {});
  });
}
</script>
"""

SW_JS = """/* Octagon Draft service worker. Bumped on every deploy so old shells retire. */
const VERSION = "%(version)s";
const SHELL = "octagon-" + VERSION;
const ASSETS = ["./", "./index.html", "./manifest.webmanifest",
                "./icon-192.png", "./icon-512.png", "./apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  /* Never cache Firebase traffic: it is the live draft. */
  if (url.hostname.endsWith("firebaseio.com") || url.hostname.endsWith("googleapis.com")) return;

  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(SHELL).then((c) => c.put("./index.html", copy));
        return res;
      }).catch(() => caches.match("./index.html"))
    );
    return;
  }
  e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
    if (res.ok && url.origin === location.origin) {
      const copy = res.clone();
      caches.open(SHELL).then((c) => c.put(req, copy));
    }
    return res;
  }).catch(() => hit)));
});
"""


def league_name(html: str, fallback: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html)
    return (m.group(1).strip() if m else "") or fallback


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--page", default=str(ROOT / "dist" / "octagon-draft.html"))
    p.add_argument("--out", default=str(ROOT / "site"))
    p.add_argument("--firebase", default=str(ROOT / "firebase.json"),
                   help="Firebase web config JSON (from the Firebase console)")
    p.add_argument("--name", default="", help="overrides the league name for the icon label")
    args = p.parse_args(argv)

    page = Path(args.page)
    if not page.exists():
        print(f"missing built page: {page}\nRun build_app.py first.", file=sys.stderr)
        return 1
    html = page.read_text(encoding="utf-8")
    name = args.name or league_name(html, "Octagon Draft")

    # --- Firebase --------------------------------------------------------
    fb_path = Path(args.firebase)
    if fb_path.exists():
        cfg = json.loads(fb_path.read_text(encoding="utf-8"))
        missing = [k for k in ("apiKey", "databaseURL", "projectId") if not cfg.get(k)]
        if missing:
            print(f"firebase config is missing {', '.join(missing)} — "
                  "copy the whole config object from the Firebase console",
                  file=sys.stderr)
            return 1
        html = FB_TAG.sub(
            lambda m: m.group(1) + json.dumps(cfg).replace("</", "<\\/") + m.group(3),
            html, count=1)
        print(f"  live sync ON  ({cfg['projectId']})")
    else:
        print(f"  live sync OFF — no {fb_path.name}. The draft will not sync between\n"
              "                  people until you add it. See README > Live sync.")

    # A sync id that is not the human-friendly join code, so the database path
    # is not guessable from anything printed in the app.
    m = STATE_TAG.search(html)
    state = json.loads(m.group(2).replace("<\\/", "</"))
    if not state.get("league", {}).get("syncId"):
        import secrets
        state.setdefault("league", {})["syncId"] = secrets.token_hex(8)
        html = STATE_TAG.sub(
            lambda mm: mm.group(1) + "\n"
            + json.dumps(state, separators=(",", ":")).replace("</", "<\\/")
            + "\n" + mm.group(3), html, count=1)
    sync_id = state["league"]["syncId"]

    html = html.replace("</style>", "</style>\n" + MANIFEST_LINK, 1)
    html = html + SW_REGISTER

    # --- write the site --------------------------------------------------
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(html, encoding="utf-8")

    manifest = {
        "name": name, "short_name": name[:12], "start_url": "./",
        "scope": "./", "display": "standalone", "orientation": "portrait",
        "background_color": "#0C0B0E", "theme_color": "#0C0B0E",
        "icons": [
            {"src": "./icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "./icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "./icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    (out / "manifest.webmanifest").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    version = str(int(page.stat().st_mtime))
    (out / "sw.js").write_text(SW_JS % {"version": version}, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")   # GitHub Pages: serve files as-is

    for icon in (ROOT / "assets").glob("*.png"):
        shutil.copy(icon, out / icon.name)

    size = sum(f.stat().st_size for f in out.iterdir() if f.is_file())
    print(f"wrote    {out}  ({size/1024:.0f} KB, {len(list(out.iterdir()))} files)")
    print(f"         league '{name}' · sync id {sync_id}")
    print("\nDeploy with:  .\\schedule\\deploy.ps1      (or ./schedule/deploy.sh)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
