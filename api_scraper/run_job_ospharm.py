"""
run_job_ospharm.py — Exécuté par GitHub Actions (workflow scraper_ospharm.yml).

Variables d'environnement requises :
    USER_ID               Supabase user UUID
    SUPABASE_SERVICE_KEY  Clé de service Supabase (GitHub Secret)
"""

import json
import os
import sys
import threading
import time
import urllib.request

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SUPA_URL     = "https://fmterazwesiwpwjpkyqi.supabase.co"
SERVICE_KEY  = os.environ["SUPABASE_SERVICE_KEY"]
USER_ID      = os.environ["USER_ID"]

OSPHARM_URL  = "https://datastat.ospharm.org/"


# ── Supabase helpers ───────────────────────────────────────────────────────────

def _supa_get_state() -> dict:
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}&select=state_json&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    return rows[0]["state_json"] if rows else {}


def _supa_patch_state(state: dict):
    url  = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}"
    body = json.dumps({"state_json": state}).encode()
    req  = urllib.request.Request(url, data=body, method="PATCH", headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    })
    with urllib.request.urlopen(req, timeout=15): pass


def _update_job(status: str, message: str = "", rows=None, error: str = "", blocking: bool = False):
    def _do():
        try:
            state = _supa_get_state()
            state["ospharm_job"] = {
                "status":  status,
                "message": message,
                "rows":    rows or [],
                "total":   len(rows) if rows else 0,
                "error":   error,
            }
            _supa_patch_state(state)
        except Exception as e:
            print(f"  [warn] Supabase update failed: {e}")
    if blocking:
        _do()
    else:
        threading.Thread(target=_do, daemon=True).start()


def _get_creds() -> dict:
    state = _supa_get_state()
    osp   = state.get("connectors", {}).get("ospharm", {})
    user  = osp.get("user", "")
    passwd = osp.get("pass", "")
    if not user or not passwd:
        raise ValueError("Identifiants OSPHARM manquants dans Supabase.")
    return {"user": user, "pass": passwd}


# ── OSPHARM scraper ────────────────────────────────────────────────────────────

def _js_click(page, text):
    return page.evaluate(f'''() => {{
        const all = document.querySelectorAll(
            ".webix_list_item,.webix_el_button button,button,[role=option],[role=button]");
        for (const el of all) {{
            if (el.textContent.trim() === {repr(text)}) {{ el.click(); return true; }}
        }}
        return false;
    }}''')


def run_ospharm(creds: dict, progress) -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page    = context.new_page()

        # 1. Login — aller sur le site, il redirige vers le formulaire OAuth
        progress("Connexion OSPHARM…")
        page.goto(OSPHARM_URL, wait_until="networkidle", timeout=30_000)
        try:
            page.locator("input[type='email'],input[name='username'],input[name='email']").first.fill(creds["user"], timeout=15_000)
            page.locator("input[type='password'],input[name='password']").first.fill(creds["pass"], timeout=5_000)
            page.locator("button[type='submit'],input[type='submit']").first.click(timeout=5_000)
            try:
                page.wait_for_url("*datastat.ospharm.org*", timeout=30_000)
            except PWTimeout:
                pass
        except PWTimeout as e:
            raise RuntimeError(f"Timeout login OSPHARM: {e}")

        if "datastat.ospharm.org" not in page.url or "accounts" in page.url:
            raise RuntimeError("Identifiants OSPHARM incorrects")

        progress("Connecté — navigation vers Toutes les ventes…")
        # Attendre que Webix soit chargé plutôt qu'un networkidle global
        try:
            page.wait_for_function("() => typeof webix !== 'undefined'", timeout=20_000)
        except Exception:
            page.wait_for_timeout(2000)

        # 2. Navigation
        for label in ["Analyse des ventes", "Toutes les ventes"]:
            try:
                page.get_by_text(label, exact=True).first.click(timeout=8_000)
                page.wait_for_timeout(800)
            except Exception:
                pass

        if "sellout" not in page.url:
            page.goto("https://datastat.ospharm.org/#!/top/sellout.all",
                      wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_function("() => typeof webix !== 'undefined'", timeout=15_000)
            except Exception:
                page.wait_for_timeout(1500)

        # 3. Sélection période "Année précédente"
        progress("Sélection période 2025…")
        try:
            page.locator("button.webix_el_htmlbutton").first.click(timeout=10_000)
            page.wait_for_timeout(600)
        except Exception:
            pass

        ok = _js_click(page, "Année précédente")
        if not ok:
            try:
                page.get_by_text("Année précédente", exact=True).first.click(force=True, timeout=5_000)
                ok = True
            except Exception:
                pass

        if ok:
            val_ok = _js_click(page, "Valider")
            if not val_ok:
                try:
                    page.get_by_text("Valider", exact=True).first.click(force=True, timeout=5_000)
                except Exception:
                    pass
            # Attendre que le datatable recharge ses données
            try:
                page.wait_for_function(
                    "() => { const g = Object.values(webix?.ui?.views||{}).find(v=>v.name==='datatable'&&v.isVisible()); return g && g.count() > 0; }",
                    timeout=20_000,
                )
            except Exception:
                page.wait_for_timeout(2500)

        # 4. Onglet Produits
        progress("Sélection onglet Produits…")
        prod_ok = page.evaluate('''() => {
            const c = document.querySelectorAll(".webix_item_tab,.webix_list_item,[class*='tab'],li,span,div,a");
            for (const el of c) { if (el.textContent.trim() === "Produits" && el.offsetParent !== null) { el.click(); return true; } }
            return false;
        }''')
        if not prod_ok:
            try:
                page.get_by_text("Produits", exact=True).first.click(timeout=8_000)
            except Exception:
                pass
        # Attendre que la grille Produits ait des données
        try:
            page.wait_for_function(
                "() => { const g = Object.values(webix?.ui?.views||{}).find(v=>v.name==='datatable'&&v.isVisible()); return g && g.count() > 0; }",
                timeout=20_000,
            )
        except Exception:
            page.wait_for_timeout(2500)

        # 5. Extraction Webix
        progress("Extraction des données Webix…")
        result = page.evaluate('''() => {
            if (typeof webix === "undefined") return { error: "webix non défini" };
            const grids = Object.values(webix.ui.views || {}).filter(v => v.name === "datatable" && v.isVisible());
            if (!grids.length) return { error: "aucun datatable visible" };
            const grid = grids[0];
            const columns = (grid.config.columns || []).map(c => {
                let label = c.id;
                if (typeof c.header === "string") label = c.header;
                else if (Array.isArray(c.header)) label = c.header.map(h => typeof h === "string" ? h : (h && h.text ? h.text : "")).filter(Boolean).join(" ");
                return { id: String(c.id), label: label || String(c.id) };
            });
            const rows = [];
            grid.eachRow(id => { const item = grid.getItem(id); if (item) rows.push(item); });
            return { columns, rows, total: rows.length };
        }''')

        browser.close()

    if not isinstance(result, dict) or "error" in result:
        raise RuntimeError(f"Webix extraction: {result}")

    columns = result["columns"]
    raw_rows = result["rows"]

    # Normaliser les rows : utiliser les labels comme clés
    col_map = {c["id"]: c["label"] for c in columns}
    normalized = []
    for r in raw_rows:
        normalized.append({col_map.get(k, k): v for k, v in r.items() if not k.startswith("$")})

    return normalized


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"🚀  Job OSPHARM démarré pour user_id={USER_ID}")
    _update_job("running", "Initialisation…")

    try:
        creds = _get_creds()
    except ValueError as e:
        _update_job("error", error=str(e))
        sys.exit(1)

    t0 = time.time()

    def progress(msg):
        elapsed = time.time() - t0
        print(f"  [{elapsed:5.1f}s] {msg}")
        _update_job("running", msg)

    try:
        rows = run_ospharm(creds, progress)
        _update_job("done", f"{len(rows)} lignes extraites", rows, blocking=True)
        print(f"\n✅  {len(rows)} lignes OSPHARM sauvegardées dans Supabase. ({time.time()-t0:.1f}s total)")
    except Exception as e:
        _update_job("error", error=str(e), blocking=True)
        print(f"\n❌  {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
