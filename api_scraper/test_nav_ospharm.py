"""
Test script: diagnose OSPHARM navigation to "Ventes" section.
Fetches credentials from Supabase, then runs detailed navigation test.
"""
import json
import os
import sys
import time
import urllib.request
import asyncio
import io

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SUPA_URL     = "https://fmterazwesiwpwjpkyqi.supabase.co"
SERVICE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")
OSPHARM_URL  = "https://datastat.ospharm.org/"


def get_first_ospharm_creds():
    """Query Supabase for the first user_state row with ospharm credentials."""
    url = f"{SUPA_URL}/rest/v1/user_state?select=user_id,state_json&limit=20"
    req = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    for row in rows:
        state = row.get("state_json", {})
        osp = state.get("connectors", {}).get("ospharm", {})
        if osp.get("user") and osp.get("pass"):
            print(f"  Found creds for user_id={row['user_id'][:8]}...")
            return {"user": osp["user"], "pass": osp["pass"]}, row["user_id"]
    raise ValueError("No OSPHARM credentials found in Supabase")


def run_nav_test(creds):
    asyncio.set_event_loop(asyncio.new_event_loop())
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Capture console messages
        console_msgs = []
        page.on("console", lambda msg: console_msgs.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: console_msgs.append(f"[JSERR] {err}"))

        print("\n=== LOGIN ===")
        page.goto(OSPHARM_URL, wait_until="networkidle", timeout=30_000)
        print(f"  After goto: {page.url[:80]}")

        if "accounts" in page.url or "login" in page.url:
            page.locator("input[type='email'],input[name='username'],input[name='email']").first.fill(creds["user"])
            page.locator("input[type='password'],input[name='password']").first.fill(creds["pass"])
            page.locator("button[type='submit'],input[type='submit']").first.click()
            try:
                page.wait_for_url("*datastat.ospharm.org*", timeout=45_000)
            except PWTimeout:
                pass

        print(f"  After login: {page.url[:80]}")
        if "datastat.ospharm.org" not in page.url:
            print("  ❌ Login failed")
            browser.close()
            return

        # Wait for Webix + networkidle
        try:
            page.wait_for_function("() => typeof webix !== 'undefined'", timeout=20_000)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        page.wait_for_timeout(3_000)
        print(f"  After networkidle: {page.url[:80]}")

        print("\n=== SIDEBAR STRUCTURE ===")
        sidebar_info = page.evaluate('''() => {
            if (typeof webix === "undefined") return {error: "no-webix"};
            const sideEl = document.querySelector(".webix_sidebar");
            if (!sideEl) return {error: "no-sidebar-el"};
            const sid = sideEl.getAttribute("view_id");
            const sb = webix.$$(sid);
            if (!sb) return {error: "no-sb-widget", sid};

            const flat = [];
            function flatten(arr, depth) {
                for (const it of arr) {
                    flat.push({id: it.id, value: it.value, depth, hasData: !!(it.data && it.data.length)});
                    if (it.data) flatten(it.data, depth+1);
                }
            }
            flatten(sb.data.serialize ? sb.data.serialize() : [], 0);
            return {sid, items: flat};
        }''')
        print(f"  {json.dumps(sidebar_info, indent=2, ensure_ascii=False)[:2000]}")

        print("\n=== FULL SIDEBAR STRUCTURE (all items) ===")
        full_sidebar = page.evaluate('''() => {
            if (typeof webix === "undefined") return {error: "no-webix"};
            const sideEl = document.querySelector(".webix_sidebar");
            if (!sideEl) return {error: "no-sidebar"};
            const sb = webix.$$(sideEl.getAttribute("view_id"));
            if (!sb) return {error: "no-sb"};
            const flat = [];
            function flatten(arr, depth) {
                for (const it of arr) {
                    flat.push({id: it.id, value: it.value, depth});
                    if (it.data) flatten(it.data, depth+1);
                }
            }
            flatten(sb.data.serialize ? sb.data.serialize() : [], 0);
            return flat;
        }''')
        print(json.dumps(full_sidebar, indent=2, ensure_ascii=False))

        print("\n=== ELEMENTS CONTAINING 'Ventes' ===")
        ventes_els = page.evaluate('''() => {
            const results = [];
            for (const el of document.querySelectorAll("*")) {
                if (el.children.length > 0) continue;
                const txt = el.textContent.trim();
                if (txt !== "Ventes") continue;
                const r = el.getBoundingClientRect();
                results.push({
                    tag: el.tagName,
                    cls: el.className.slice(0, 80),
                    parentCls: (el.parentElement||{}).className || "",
                    visible: r.width > 1 && r.height > 1,
                    x: Math.round(r.x + r.width/2),
                    y: Math.round(r.y + r.height/2),
                });
            }
            return results;
        }''')
        print(json.dumps(ventes_els, indent=2, ensure_ascii=False))

        print("\n=== WEBIX JET APP ===")
        jet_info = page.evaluate('''() => {
            const info = {};
            info.webix_app = typeof webix !== "undefined" && typeof webix.app !== "undefined"
                ? String(typeof webix.app) : "absent";
            info.webix_app_show = typeof webix !== "undefined" && webix.app && typeof webix.app.show === "function"
                ? "yes" : "no";
            info.window_keys_with_show = Object.keys(window).filter(k => {
                try { return window[k] && typeof window[k] === "object" && typeof window[k].show === "function"
                    && typeof window[k].getService === "function"; } catch(e) { return false; }
            }).slice(0, 5);
            return info;
        }''')
        print(f"  {json.dumps(jet_info, indent=2)}")

        print("\n=== SELECT sellout.all + EXPORT TEST ===")
        import tempfile as _tf
        _tmp_dl_fd, _tmp_dl = _tf.mkstemp(suffix=".xlsx")
        import os as _os; _os.close(_tmp_dl_fd)
        context.set_default_timeout(5_000)
        _dl_bytes = []

        def _on_dl(dl):
            try:
                dl.save_as(_tmp_dl)
                with open(_tmp_dl, "rb") as f: body = f.read()
                if len(body) > 500:
                    _dl_bytes.append(body)
                    print(f"  [DL] download event: {dl.suggested_filename} ({len(body):,} bytes)")
            except Exception as e:
                print(f"  [DL] err: {e}")
        page.on("download", _on_dl)

        # Navigate to sellout.all
        nav_result = page.evaluate('''() => {
            if (typeof webix === "undefined") return "no-webix";
            const sideEl = document.querySelector(".webix_sidebar");
            if (!sideEl) return "no-sidebar";
            const sb = webix.$$(sideEl.getAttribute("view_id"));
            if (!sb) return "no-sb";
            sb.select("sellout.all");
            return "selected";
        }''')
        print(f"  nav: {nav_result}")
        page.wait_for_timeout(5_000)
        print(f"  URL: {page.url[:80]}")

        # Show page buttons with webix tooltip
        view_info = page.evaluate('''() => {
            const results = [];
            if (typeof webix !== "undefined") {
                for (const el of document.querySelectorAll("[view_id]")) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) continue;
                    const v = webix.$$(el.getAttribute("view_id"));
                    if (!v) continue;
                    const raw = v.config?.tooltip || v.config?.label || v.config?.value || "";
                    const tip = (typeof raw === "string" ? raw : String(raw || "")).toLowerCase();
                    if (!tip) continue;
                    results.push({vid: el.getAttribute("view_id"), tip, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)});
                }
            }
            return results.slice(0, 30);
        }''')
        print(f"  views with tooltips: {json.dumps(view_info, ensure_ascii=False)[:3000]}")

        # Try webix.toExcel on visible datatable
        export_result = page.evaluate('''() => {
            if (typeof webix === "undefined" || typeof webix.toExcel !== "function") return "no-webix-toExcel";
            for (const el of document.querySelectorAll(".webix_dtable[view_id]")) {
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) continue;
                const grid = webix.$$(el.getAttribute("view_id"));
                if (grid) { webix.toExcel(grid); return "toExcel:" + el.getAttribute("view_id"); }
            }
            return "no-dtable";
        }''')
        print(f"  export result: {export_result}")

        # Wait for download
        for i in range(12):
            if _dl_bytes: break
            page.wait_for_timeout(2_500)
            print(f"  waiting... {(i+1)*2.5:.0f}s")

        if _dl_bytes:
            print(f"\n  ✅ Excel downloaded: {len(_dl_bytes[0]):,} bytes")
        else:
            print(f"\n  ❌ No Excel file received")

        print("\n=== TABS VISIBLE? ===")
        tabs = page.evaluate('''() => {
            const res = [];
            for (const el of document.querySelectorAll(".webix_item_tab")) {
                const r = el.getBoundingClientRect();
                if (r.width > 1 && r.height > 1) res.push(el.textContent.trim());
            }
            return res;
        }''')
        print(f"  tabs: {tabs}")

        print("\n=== CONSOLE MESSAGES ===")
        for msg in console_msgs[-30:]:
            print(f"  {msg[:120]}")

        browser.close()
        print("\n=== DONE ===")


if __name__ == "__main__":
    print("Fetching credentials from Supabase...")
    try:
        creds, user_id = get_first_ospharm_creds()
        print(f"Got creds: user={creds['user'][:4]}***")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    run_nav_test(creds)
