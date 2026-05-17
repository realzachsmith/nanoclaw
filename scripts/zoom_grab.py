#!/usr/bin/env python3
"""Use Playwright to authenticate to a password-protected Zoom share, then download the recording."""
import json, os, re, sys, time
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright

URL = os.environ["URL"]
PW = os.environ["PW"]
WORK = Path("work"); WORK.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"

net = []  # captured responses

def looks_videoish(u):
    return any(p in u for p in (".mp4", ".m3u8", "/wcvod/", "GetVideoSource", "play/info", "rec/play"))

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
    page = ctx.new_page()

    def on_response(resp):
        try:
            u = resp.url
            if looks_videoish(u) or ".cloudfront.net" in u or "vod" in u or "ssrweb" in u:
                net.append({
                  "url": u,
                  "status": resp.status,
                  "method": resp.request.method,
                  "resource_type": resp.request.resource_type,
                  "headers": dict(resp.headers),
                })
                print(f"NET {resp.status} {resp.request.resource_type} {u}", flush=True)
        except Exception as e:
            print("on_response err:", e, flush=True)
    page.on("response", on_response)

    print("GOTO", URL, flush=True)
    page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
    time.sleep(2)

    # Try to fill password
    selectors = ['input#passwd', 'input[name="passwd"]', 'input[type="password"]']
    filled = False
    for s in selectors:
        try:
            page.wait_for_selector(s, timeout=8000, state="visible")
            page.fill(s, PW)
            filled = True
            print("filled selector:", s, flush=True)
            break
        except Exception:
            pass
    if filled:
        for s in ['#passwd_btn', 'button#passwordPasscodeSubmit', 'button[type="submit"]', 'input[type="submit"]', 'text=Submit']:
            try:
                if page.locator(s).count():
                    page.click(s, timeout=5000)
                    print("clicked submit:", s, flush=True); break
            except Exception: pass
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception: pass
    else:
        print("no password input found - may already be authenticated", flush=True)

    # Wait for the video element
    try:
        page.wait_for_selector('video', timeout=60_000, state='attached')
        print("video element attached", flush=True)
    except Exception as e:
        print("no video element after wait:", e, flush=True)

    # Let the page settle and player initialize
    try: page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception: pass
    time.sleep(5)

    page.screenshot(path="work/screenshot.png", full_page=False)
    html = page.content()
    Path("work/play.html").write_text(html)

    video_src = page.evaluate("() => document.querySelector('video')?.src || ''")
    print("video.src =", video_src, flush=True)

    # Try to find a real mp4 from network captures
    candidates = []
    for r in net:
        u = r["url"]; ct = (r.get("headers",{}) or {}).get("content-type","")
        score = 0
        if ".mp4" in u: score += 5
        if ".m3u8" in u: score += 4
        if "/wcvod/" in u: score += 3
        if "ssrweb" in u: score += 2
        if "video" in ct: score += 4
        if r.get("resource_type") == "media": score += 5
        if r["status"] in (200, 206): score += 2
        if r["method"] == "GET": score += 1
        candidates.append((score, r))
    candidates.sort(key=lambda x: -x[0])
    Path("work/network.json").write_text(json.dumps(net, indent=2))

    chosen = None
    if video_src and not video_src.startswith("blob:"):
        chosen = video_src
    elif candidates:
        chosen = candidates[0][1]["url"]
    else:
        # Try to extract from HTML
        for pat in [r'(https?://[^"\'\\\s]+\.mp4[^"\'\\\s]*)', r'(https?://[^"\'\\\s]+\.m3u8[^"\'\\\s]*)']:
            m = re.search(pat, html)
            if m: chosen = m.group(1); break

    if not chosen:
        print("FAIL: no candidate URL", flush=True)
        ctx.close(); browser.close()
        sys.exit(2)
    print("CHOSEN:", chosen, flush=True)

    cookies = ctx.cookies()
    Path("work/cookies.json").write_text(json.dumps(cookies, indent=2))
    referer = page.url
    ctx.close(); browser.close()

sess = requests.Session()
for c in cookies:
    sess.cookies.set(c["name"], c["value"], domain=c.get("domain","").lstrip("."), path=c.get("path","/"))
sess.headers.update({
    "User-Agent": UA,
    "Accept": "*/*",
    "Referer": referer,
    "Origin": "https://us06web.zoom.us",
    "Accept-Language": "en-US,en;q=0.9",
    "Range": "bytes=0-",
})
Path("work/headers.txt").write_text(
    "\r\n".join([f"{k}: {v}" for k,v in sess.headers.items() if k != "Range"]) + "\r\n"
)

out = "work/recording.m3u8" if chosen.endswith(".m3u8") else "work/recording.mp4"
print("Downloading", chosen, "->", out, flush=True)
with sess.get(chosen, stream=True, timeout=300, allow_redirects=True) as r:
    print("HTTP", r.status_code, dict(r.headers).get("content-type"), dict(r.headers).get("content-length"), flush=True)
    r.raise_for_status()
    with open(out, "wb") as f:
        for c in r.iter_content(1<<20):
            f.write(c)
size = os.path.getsize(out)
print("Saved", out, size, "bytes", flush=True)
if size < 100_000:
    print("WARN: suspiciously small file", flush=True)
