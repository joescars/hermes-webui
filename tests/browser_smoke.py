#!/usr/bin/env python3
"""
Headless browser smoke test — the console-error page-load gate.

WHY THIS EXISTS
  `node --check`, ESLint, and the (mocked) pytest suite cannot see the class of
  bug that has actually bricked releases: JavaScript that parses fine but throws
  at *runtime* when a real browser executes the page. Examples that shipped:
    - a `const` reassigned at runtime (v0.51.168 "Failed to load conversation
      messages" — #3162)
    - a `function X(){}` colliding with a `window.X = {}` in classic scripts
      (#2715 / #2771)
  Every one of those throws on load or first interaction and produces a blank or
  broken page for *every* user. This smoke boots the real server.py and loads
  the key pages in headless Chromium, failing if ANY uncaught exception or
  console error fires.

SCOPE
  Deliberately AGENT-FREE so it runs in CI (which does not install hermes-agent):
  it verifies the page loads and its JS initializes cleanly — it does NOT drive a
  full chat (that needs the agent + mock provider and runs in the private QA
  harness's golden-path E2E). This is the "does the app even come up without
  throwing" gate, which is the highest-frequency brick class.

USAGE
  python tests/browser_smoke.py
  (Requires: playwright + chromium. Boots server.py on an ephemeral port with an
  isolated temp state dir and no agent.)

EXIT CODES
  0 — all pages loaded with zero console errors / uncaught exceptions
  1 — a console error or uncaught exception was detected (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
import os
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import zlib

PORT = int(os.getenv("SMOKE_PORT", "8796"))
BASE = f"http://127.0.0.1:{PORT}"

# Pages that must load cleanly. Hash routes are how the SPA exposes views.
PAGES = [
    "/",
    "/#settings",
    "/#sessions",
]

# Known-benign console noise (extend deliberately, each with a reason). Every
# entry here is a blind spot, so keep the list short.
BENIGN = [
    "favicon",          # favicon 404 in bare env — not app code
    "manifest.json",    # PWA manifest probe under headless http
    "serviceworker",    # SW registration noise under headless http
    "sw.js",            # service worker fetch noise
    "the server responded with a status of 404",  # static asset 404 in bare env
]


def _is_benign(text):
    t = text.lower()
    return any(p.lower() in t for p in BENIGN)


def _wait_for_health(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _check_markdown_code_rendering(page):
    """Exercise raw-code/backtick edge cases through the production renderer.

    The caller has already opened the real app, which loads ``static/ui.js``
    and its stylesheet. This helper reuses that page's ``renderMd()``; injecting
    the bundle again would redeclare its top-level lexical bindings (e.g.
    ``_recycleStash``) and throw a page error.
    """
    # ui.js is a deferred classic script; the caller's navigation waits for
    # DOMContentLoaded, then gives boot time before calling this helper. Wait
    # explicitly for the production renderer so slow script loading is a clear
    # setup failure, without reloading the page or re-adding the bundle.
    try:
        page.wait_for_function("typeof renderMd === 'function'", timeout=10000)
    except Exception:
        return ["production renderMd() is unavailable on the app page"]
    width, height = 640, 280
    raw_row = bytes([0]) + bytes([0, 127, 255, 255]) * width
    def png_chunk(kind, payload):
        import binascii
        data = kind + payload
        return struct.pack(">I", len(payload)) + data + struct.pack(">I", binascii.crc32(data) & 0xffffffff)
    png_fixture = (
        bytes.fromhex("89504e470d0a1a0a")
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(raw_row * height))
        + png_chunk(b"IEND", b"")
    )
    page.route("**/api/media?*", lambda route: route.fulfill(status=200, content_type="image/png", body=png_fixture))
    if not page.evaluate("typeof renderMd === 'function'"):
        return ["production renderMd() is unavailable on the app page"]
    inputs = [
        {
            "name": "backticks in separate raw code elements",
            "markdown": "Use <code>`</code> for inline code and <code>```</code> for fences.",
            "expectedText": "Use ` for inline code and ``` for fences.",
            "expectedCode": ["`", "```"],
            "expectedImage": None,
        },
        {
            "name": "fenced code displays literal code tags",
            "markdown": "```html\n<code>foo</code>\n```",
            "expectedText": "html<code>foo</code>",
            "expectedCode": ["<code>foo</code>"],
            "expectedImage": None,
        },
        {
            "name": "inline raw code tag displays literally in prose",
            "markdown": "Explain `<code>npm test</code>` syntax.",
            "expectedText": "Explain <code>npm test</code> syntax.",
            "expectedCode": ["<code>npm test</code>"],
            "expectedCodeParents": ["P"],
            "expectedImage": None,
        },
        {
            "name": "inline raw pre tag displays literally in prose",
            "markdown": "Use `<pre>block</pre>` for preformatted text.",
            "expectedText": "Use <pre>block</pre> for preformatted text.",
            "expectedCode": ["<pre>block</pre>"],
            "expectedCodeParents": ["P"],
            "expectedImage": None,
        },
        {
            "name": "inline raw code tag displays literally in list item",
            "markdown": "- Wrap with `<code>x</code>`",
            "expectedText": "Wrap with <code>x</code>",
            "expectedCode": ["<code>x</code>"],
            "expectedCodeParents": ["LI"],
            "expectedImage": None,
        },
        {
            "name": "inline raw code tag displays literally in table cell",
            "markdown": "| Example | Meaning |\n| --- | --- |\n| `<code>x</code>` | literal |",
            "expectedText": "\nExampleMeaning<code>x</code>literal\n",
            "expectedCode": ["<code>x</code>"],
            "expectedCodeParents": ["TD"],
            "expectedImage": None,
        },
        {
            "name": "MEDIA local image renders generated artifact card",
            "markdown": "MEDIA:/workspace/user/.hermes/cache/images/img_release_pipeline.png",
            "expectedText": "",
            "expectedCode": [],
            "expectedArtifact": {
                "alt": "img_release_pipeline.png",
                "download": "img_release_pipeline.png",
                "accessibleName": "Download",
                "icon": True,
                "srcContains": "api/media?path=%2Fworkspace%2Fuser%2F.hermes%2Fcache%2Fimages%2Fimg_release_pipeline.png",
            },
            "expectedImage": "api/media?path=%2Fworkspace%2Fuser%2F.hermes%2Fcache%2Fimages%2Fimg_release_pipeline.png",
            "expectedImageShape": {"width": 640, "height": 280, "fit": "contain"},
        },
        {
            "name": "bare cache image renders generated artifact card",
            "markdown": "![Release pipeline]\n(/workspace/user/.hermes/cache/images/img_release_pipeline.png)",
            "expectedText": "",
            "expectedCode": [],
            "expectedArtifact": {
                "alt": "Release pipeline",
                "download": "img_release_pipeline.png",
                "accessibleName": "Download",
                "icon": True,
                "srcContains": "api/media?path=%2Fworkspace%2Fuser%2F.hermes%2Fcache%2Fimages%2Fimg_release_pipeline.png",
            },
            "expectedImage": "api/media?path=%2Fworkspace%2Fuser%2F.hermes%2Fcache%2Fimages%2Fimg_release_pipeline.png",
            "expectedImageShape": {"width": 640, "height": 280, "fit": "contain"},
        },
        {
            "name": "file cache image renders generated artifact card with basename",
            "markdown": "![Release pipeline](file:///workspace/user/.hermes/cache/images/img_release_pipeline.png)",
            "expectedText": "",
            "expectedCode": [],
            "expectedArtifact": {
                "alt": "Release pipeline",
                "download": "img_release_pipeline.png",
                "accessibleName": "Download",
                "icon": True,
                "srcContains": "api/media?path=%2Fworkspace%2Fuser%2F.hermes%2Fcache%2Fimages%2Fimg_release_pipeline.png",
            },
            "expectedImage": "api/media?path=%2Fworkspace%2Fuser%2F.hermes%2Fcache%2Fimages%2Fimg_release_pipeline.png",
            "expectedImageShape": {"width": 640, "height": 280, "fit": "contain"},
        },
        {
            "name": "raw SVG and event handlers remain sanitized",
            "markdown": '<span class="msg-artifact-image"><a aria-label="Download" onclick="alert(1)"><svg onload="alert(1)"></svg></a></span>',
            "expectedText": "",
            "expectedCode": [],
            "expectedArtifact": None,
            "expectedImage": None,
            "forbiddenHtml": ["<svg", "<script", "onclick=", "aria-label=", "msg-artifact-download"],
        },
        {
            "name": "raw artifact download class stays a visible ordinary link",
            "markdown": '<a class="msg-artifact-download" href="https://example.test/report.png">visible model link</a>',
            "expectedText": "visible model link",
            "expectedCode": [],
            "expectedArtifact": None,
            "expectedImage": None,
            "expectedLink": {"text": "visible model link", "href": "https://example.test/report.png", "className": ""},
        },
        {
            "name": "raw-code backtick before image and inline code",
            "markdown": "Type <code>`</code> then see ![i](https://e.x/i.png) and `x`.",
            "expectedText": "Type ` then see  and x.",
            "expectedCode": ["`", "x"],
            "expectedImage": "https://e.x/i.png",
        },
    ]
    results = page.evaluate(
        """async (inputs) => Promise.all(inputs.map(async input => {
          if (typeof renderMd !== 'function') throw new Error('renderMd is unavailable');
          const root = document.createElement('div');
          root.innerHTML = renderMd(input.markdown);
          const images = Array.from(root.querySelectorAll('img'));
          const imageLoads = Promise.all(images.map(image => new Promise(resolve => {
            if (!(image.getAttribute('src') || '').startsWith('api/media?')) { resolve(null); return; }
            image.loading = 'eager';
            image.onload = () => resolve({width:image.naturalWidth,height:image.naturalHeight});
            image.onerror = () => resolve({width:0,height:0});
          })));
          document.body.appendChild(root);
          const dimensions = await imageLoads;
          return {
            name: input.name,
            text: root.textContent,
            "code": Array.from(root.querySelectorAll('code'), node => node.textContent),
            "codeParents": Array.from(root.querySelectorAll('code'), node => node.parentElement.tagName),
            images: Array.from(root.querySelectorAll('img'), node => node.getAttribute('src')),
            imageMetrics: Array.from(root.querySelectorAll('.msg-artifact-image img'), (node, index) => ({
              width: dimensions[images.indexOf(node)]?.width ?? 0,
              height: dimensions[images.indexOf(node)]?.height ?? 0,
              fit: node.style.objectFit || getComputedStyle(node).objectFit,
            })),
            links: Array.from(root.querySelectorAll('a'), anchor => ({
              text: anchor.textContent.trim(),
              href: anchor.getAttribute('href'),
              className: anchor.className,
            })),
            artifacts: Array.from(root.querySelectorAll('.msg-artifact-image'), wrapper => ({
              className: wrapper.className,
              alt: wrapper.querySelector('img')?.getAttribute('alt'),
              src: wrapper.querySelector('img')?.getAttribute('src'),
              download: wrapper.querySelector('a.msg-artifact-download')?.getAttribute('download'),
              accessibleName: wrapper.querySelector('a.msg-artifact-download')?.getAttribute('aria-label') || wrapper.querySelector('a.msg-artifact-download')?.textContent.trim() || '',
              icon: !!wrapper.querySelector('a.msg-artifact-download svg'),
              basename: (() => { try { return new URL(wrapper.querySelector('img')?.getAttribute('src'), document.baseURI).pathname.split('/').pop(); } catch (_) { return ''; } })(),
            })),
            "html": root.innerHTML,
            "leakedStash": /\\u0000F\\d+\\u0000|\\bF\\d+\\b/.test(root.textContent),
          }; }))""",
        inputs,
    )
    failures = []
    for case, result in zip(inputs, results, strict=True):
        if result["text"] != case["expectedText"]:
            failures.append(f"{case['name']}: text={result['text']!r}; html={result['html']!r}")
        if result["code"] != case["expectedCode"]:
            failures.append(f"{case['name']}: code={result['code']!r}")
        if "expectedCodeParents" in case and result["codeParents"] != case["expectedCodeParents"]:
            failures.append(f"{case['name']}: code parents={result['codeParents']!r}; html={result['html']!r}")
        if case.get("expectedArtifact") is not None:
            expected_artifact = case["expectedArtifact"]
            if len(result["artifacts"]) != 1:
                failures.append(f"{case['name']}: artifacts={result['artifacts']!r}; html={result['html']!r}")
            else:
                artifact = result["artifacts"][0]
                for key in ("alt", "download", "accessibleName"):
                    if artifact[key] != expected_artifact[key]:
                        failures.append(f"{case['name']}: artifact {key}={artifact[key]!r}")
                if artifact["icon"] is not expected_artifact["icon"]:
                    failures.append(f"{case['name']}: artifact icon={artifact['icon']!r}")
                if expected_artifact["srcContains"] not in artifact["src"]:
                    failures.append(f"{case['name']}: artifact src={artifact['src']!r}")
        if case.get("expectedLink"):
            expected_link = case["expectedLink"]
            matching_links = [link for link in result["links"] if link["text"] == expected_link["text"]]
            if not matching_links:
                failures.append(f"{case['name']}: visible link missing; links={result['links']!r}; html={result['html']!r}")
            else:
                link = matching_links[0]
                if link["href"] != expected_link["href"] or link["className"] != expected_link["className"]:
                    failures.append(f"{case['name']}: link={link!r}")
        for forbidden in case.get("forbiddenHtml", []):
            if forbidden.lower() in result["html"].lower():
                failures.append(f"{case['name']}: forbidden output {forbidden!r}; html={result['html']!r}")
        if case.get("expectedImageShape"):
            expected_shape = case["expectedImageShape"]
            if len(result["imageMetrics"]) != 1 or result["imageMetrics"][0] != expected_shape:
                failures.append(f"{case['name']}: image metrics={result['imageMetrics']!r}; expected={expected_shape!r}")
        expected_images = [case["expectedImage"]] if case["expectedImage"] else []
        if result["images"] != expected_images:
            failures.append(f"{case['name']}: images={result['images']!r}; html={result['html']!r}")
        if result["leakedStash"]:
            failures.append(f"{case['name']}: inline-code stash token leaked")
    return failures


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    if not os.path.exists(server_py):
        print(f"SETUP FAIL: server.py not found at {server_py}", file=sys.stderr)
        return 2

    state_dir = tempfile.mkdtemp(prefix="hermes-browser-smoke-")
    env = os.environ.copy()
    # Strip real provider keys so nothing leaks into the smoke server.
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    env.update({
        "HERMES_WEBUI_PORT": str(PORT),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        # Point agent discovery at a path that doesn't exist — the server is
        # designed to boot and serve the UI even when the agent is absent.
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
    })

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        if not _wait_for_health(timeout=30):
            print("SETUP FAIL: server did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        failures = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            for path in PAGES:
                ctx = browser.new_context(base_url=BASE)
                page = ctx.new_page()
                errors = []
                page.on("console", lambda m: errors.append(("console", m.text))
                        if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append(("pageerror", str(e))))

                page.goto(path, wait_until="domcontentloaded")
                # Give boot.js / view init time to run and throw if it's going to.
                try:
                    page.wait_for_selector("#msg, .app, body", timeout=10000)
                except Exception:
                    pass
                time.sleep(1.5)

                if path == "/":
                    try:
                        renderer_failures = _check_markdown_code_rendering(page)
                        failures.extend(f"  [markdown renderer] {failure}" for failure in renderer_failures)
                        if not renderer_failures:
                            print("OK  Markdown renderer regressions — Chromium + production renderMd()")
                    except Exception as exc:
                        failures.append(f"  [markdown renderer] browser regression check failed: {exc}")

                meaningful = [(kind, txt) for (kind, txt) in errors if not _is_benign(txt)]
                if meaningful:
                    for kind, txt in meaningful:
                        failures.append(f"  [{path}] {kind}: {txt}")
                else:
                    print(f"OK  {path} — no console errors")
                ctx.close()
            browser.close()

        if failures:
            print("\nBROWSER SMOKE FAILED — runtime JS errors detected:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nBROWSER SMOKE PASSED — all pages loaded with zero console errors")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
