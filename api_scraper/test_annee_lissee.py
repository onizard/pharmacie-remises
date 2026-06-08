"""
Test: navigation sellout.all → Année lissée → onglet Produits → export Excel
"""
import json, sys, io, urllib.request, asyncio, os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
asyncio.set_event_loop(asyncio.new_event_loop())

SUPA_URL    = "https://api.break-pharma.fr"
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def get_creds():
    url = f"{SUPA_URL}/rest/v1/user_state?select=user_id,state_json&limit=20"
    req = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    for row in rows:
        osp = row["state_json"].get("connectors", {}).get("ospharm", {})
        if osp.get("user") and osp.get("pass"):
            return {"user": osp["user"], "pass": osp["pass"]}, row["user_id"]
    raise ValueError("No OSPHARM creds")


creds, user_id = get_creds()
print(f"user={creds['user'][:4]}*** user_id={user_id[:8]}...")

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import tempfile as _tf, os as _os

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()

    dl_bytes = []
    def on_dl(dl):
        try:
            tmp = _tf.mktemp(suffix=".xlsx")
            dl.save_as(tmp)
            with open(tmp, "rb") as f: body = f.read()
            if len(body) > 500:
                dl_bytes.append(body)
                print(f"  [DL] {dl.suggested_filename} ({len(body):,} bytes)")
        except Exception as e:
            print(f"  [DL err] {e}")
    page.on("download", on_dl)

    # ── Login ──────────────────────────────────────────────────────────────────
    print("\n=== LOGIN ===")
    page.goto("https://datastat.ospharm.org/", wait_until="networkidle", timeout=30_000)
    if "accounts" in page.url or "login" in page.url:
        page.locator("input[type='email'],input[name='username'],input[name='email']").first.fill(creds["user"])
        page.locator("input[type='password'],input[name='password']").first.fill(creds["pass"])
        page.locator("button[type='submit'],input[type='submit']").first.click()
        try: page.wait_for_url("*datastat.ospharm.org*", timeout=45_000)
        except PWTimeout: pass
    page.wait_for_function("() => typeof webix !== 'undefined'", timeout=20_000)
    try: page.wait_for_load_state("networkidle", timeout=20_000)
    except: pass
    page.wait_for_timeout(3_000)
    print(f"  URL: {page.url[:80]}")

    # ── Navigation sellout.all ──────────────────────────────────────────────────
    print("\n=== NAVIGATION sellout.all ===")
    r = page.evaluate('''() => {
        if (typeof webix === "undefined") return "no-webix";
        const sideEl = document.querySelector(".webix_sidebar");
        if (!sideEl) return "no-sidebar";
        const sb = webix.$$(sideEl.getAttribute("view_id"));
        if (!sb) return "no-sb";
        sb.select("sellout.all");
        return "selected";
    }''')
    print(f"  nav: {r}")
    page.wait_for_timeout(5_000)
    print(f"  URL: {page.url[:80]}")

    # ── Date picker → Année lissée ──────────────────────────────────────────────
    print("\n=== DATE PICKER (click button_date_picker) ===")
    r_date = page.evaluate('''() => {
        // Clic natif sur le bouton de date
        const el = document.querySelector("[view_id='button_date_picker']");
        if (!el) return "no-el";
        const btn = el.querySelector("button") || el;
        btn.click();
        return "clicked:" + btn.tagName;
    }''')
    print(f"  click date btn: {r_date}")
    page.wait_for_timeout(1_500)

    # Check what appeared
    popup_state = page.evaluate('''() => {
        const results = [];
        for (const el of document.querySelectorAll("*")) {
            if (el.children.length > 0) continue;
            const txt = el.textContent.trim();
            if (txt.length < 3 || txt.length > 50) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 1 && r.height > 1) results.push(txt);
        }
        return [...new Set(results)].filter(t =>
            ["liss", "ann", "mois", "semaine", "valider", "annuler", "periode", "période",
             "personnalis", "dernier", "filtre"].some(k => t.toLowerCase().includes(k))
        ).slice(0, 20);
    }''')
    print(f"  popup texts: {popup_state}")

    # Try clicking "Année lissée" directly
    found_lissee = page.evaluate('''() => {
        for (const el of document.querySelectorAll("*")) {
            if (el.children.length > 0) continue;
            const txt = el.textContent.trim().toLowerCase();
            if (!txt.includes("liss")) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) continue;
            el.click();
            return {txt: el.textContent.trim(), cls: el.className.slice(0,50), x: Math.round(r.x), y: Math.round(r.y)};
        }
        return null;
    }''')
    print(f"  found & clicked 'lissée': {found_lissee}")
    page.wait_for_timeout(1_500)

    # Check for Valider button
    after_lissee = page.evaluate('''() => {
        const results = [];
        for (const el of document.querySelectorAll("button, .webix_list_item, [role=option]")) {
            const txt = el.textContent.trim();
            if (!txt) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 1 && r.height > 1) results.push({txt: txt.slice(0,30), cls: el.className.slice(0,30)});
        }
        return results.slice(0, 20);
    }''')
    print(f"  after lissée click, buttons: {json.dumps(after_lissee, ensure_ascii=False)}")

    # Click Valider if found
    valider = page.evaluate('''() => {
        for (const el of document.querySelectorAll("button, .webix_list_item")) {
            if (el.textContent.trim().toLowerCase() === "valider") {
                el.click();
                return "clicked-valider";
            }
        }
        return "no-valider";
    }''')
    print(f"  valider: {valider}")
    page.wait_for_timeout(3_000)
    print(f"  URL after: {page.url[:80]}")

    # Check current period
    period_now = page.evaluate('''() => {
        const btn = document.getElementById("button_date_picker");
        if (btn) return btn.textContent.trim().slice(0,60);
        const el = document.querySelector("[view_id='button_date_picker']");
        if (el) return el.textContent.trim().slice(0,60);
        // Try regex in body text
        const re = /(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\s*[àa\-]\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})/i;
        const m = document.body.innerText.match(re);
        return m ? m[0] : "not found";
    }''')
    print(f"  current period: {period_now}")

    # ── Onglet Produits ──────────────────────────────────────────────────────────
    print("\n=== ONGLET PRODUITS ===")
    segment_info = page.evaluate('''() => {
        const results = [];
        for (const el of document.querySelectorAll(".webix_segment_0, .webix_segment_1, .webix_segment_N, button")) {
            const txt = el.textContent.trim();
            if (!["Laboratoires","Familles","Produits","Marques"].includes(txt)) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) continue;
            results.push({txt, cls: el.className.slice(0,40), selected: el.className.includes("selected")});
        }
        return results;
    }''')
    print(f"  segments: {json.dumps(segment_info, ensure_ascii=False)}")

    produits_click = page.evaluate('''() => {
        for (const el of document.querySelectorAll(".webix_segment_0, .webix_segment_1, .webix_segment_N, button")) {
            if (el.textContent.trim() !== "Produits") continue;
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) continue;
            el.click();
            return {cls: el.className.slice(0,50), x: Math.round(r.x), y: Math.round(r.y)};
        }
        return null;
    }''')
    print(f"  Produits clicked: {produits_click}")
    page.wait_for_timeout(3_000)

    # Check active segment
    active_seg = page.evaluate('''() => {
        for (const el of document.querySelectorAll(".webix_segment_0, .webix_segment_1, .webix_segment_N, button")) {
            const txt = el.textContent.trim();
            if (!["Laboratoires","Familles","Produits","Marques"].includes(txt)) continue;
            if (el.className.includes("selected")) return txt;
        }
        return "none";
    }''')
    print(f"  active segment: {active_seg}")

    # Check visible datatable
    dtable_info = page.evaluate('''() => {
        if (typeof webix === "undefined") return "no-webix";
        const results = [];
        for (const el of document.querySelectorAll(".webix_dtable[view_id]")) {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) continue;
            results.push(el.getAttribute("view_id"));
        }
        return results;
    }''')
    print(f"  visible datatables: {dtable_info}")

    # ── Export ───────────────────────────────────────────────────────────────────
    print("\n=== EXPORT ===")
    export_r = page.evaluate('''() => {
        if (typeof webix === "undefined" || typeof webix.toExcel !== "function") return "no-toExcel";
        // Try datatable_sellout first
        for (const vid of ["datatable_sellout", "datatable_sellout_product", "datatable_product"]) {
            const grid = webix.$$(vid);
            if (grid) { webix.toExcel(grid); return "toExcel:" + vid; }
        }
        // Try first visible datatable
        for (const el of document.querySelectorAll(".webix_dtable[view_id]")) {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) continue;
            const grid = webix.$$(el.getAttribute("view_id"));
            if (grid) { webix.toExcel(grid); return "toExcel:first:" + el.getAttribute("view_id"); }
        }
        return "no-dtable";
    }''')
    print(f"  export: {export_r}")

    for i in range(16):
        if dl_bytes: break
        page.wait_for_timeout(2_500)
        print(f"  waiting {(i+1)*2.5:.0f}s...")

    if dl_bytes:
        import openpyxl, io as _io
        wb = openpyxl.load_workbook(_io.BytesIO(dl_bytes[0]), read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        headers = [str(h or "").strip() for h in next(rows_iter)]
        rows = []
        for row in rows_iter:
            if any(v is not None for v in row):
                rows.append(dict(zip(headers, row)))
        wb.close()
        print(f"\n✅ Excel: {len(rows)} rows")
        print(f"   Colonnes: {headers}")
        if rows: print(f"   1ère ligne: {dict(list(rows[0].items())[:4])}")
    else:
        print("\n❌ Aucun fichier Excel reçu")

    browser.close()
