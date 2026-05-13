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
SERVICE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")
USER_ID      = os.environ.get("USER_ID", "")

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


def run_ospharm(creds: dict, progress, user_id: str = "") -> tuple[list[dict], str]:
    import tempfile, openpyxl

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

        # 1. Login
        progress("Connexion OSPHARM…")
        page.goto(OSPHARM_URL, wait_until="networkidle", timeout=30_000)

        # SSO silencieux : déjà sur le dashboard
        if not ("datastat.ospharm.org" in page.url and "login" not in page.url and "accounts" not in page.url):
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

        progress("Connecté — chargement…")
        try:
            page.wait_for_function("() => typeof webix !== 'undefined'", timeout=20_000)
        except Exception:
            page.wait_for_timeout(2000)

        # 2. Analyse des ventes → Toutes mes ventes
        progress("Navigation vers Toutes mes ventes…")
        for label in ["Analyse des ventes", "Toutes les ventes", "Toutes mes ventes"]:
            try:
                page.get_by_text(label, exact=True).first.click(timeout=8_000)
                page.wait_for_timeout(700)
            except Exception:
                pass

        if "sellout" not in page.url:
            page.goto("https://datastat.ospharm.org/#!/top/sellout.all",
                      wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_function("() => typeof webix !== 'undefined'", timeout=15_000)
            except Exception:
                page.wait_for_timeout(1500)

        # 3. Sélection "Année lissée" + Valider
        progress("Sélection Année lissée…")
        try:
            page.locator("button.webix_el_htmlbutton").first.click(timeout=10_000)
            page.wait_for_timeout(500)
        except Exception:
            pass

        ok = _js_click(page, "Année lissée")
        if not ok:
            try:
                page.get_by_text("Année lissée", exact=True).first.click(force=True, timeout=5_000)
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
            const els = document.querySelectorAll(".webix_item_tab,.webix_list_item,[class*='tab'],li,span,div,a");
            for (const el of els) {
                if (el.textContent.trim() === "Produits" && el.offsetParent !== null) { el.click(); return true; }
            }
            return false;
        }''')
        if not prod_ok:
            try:
                page.get_by_text("Produits", exact=True).first.click(timeout=8_000)
            except Exception:
                pass
        try:
            page.wait_for_function(
                "() => { const g = Object.values(webix?.ui?.views||{}).find(v=>v.name==='datatable'&&v.isVisible()); return g && g.count() > 0; }",
                timeout=20_000,
            )
        except Exception:
            page.wait_for_timeout(2500)

        # 5. Export Excel
        progress("Export Excel…")
        tmp = tempfile.mktemp(suffix=".xlsx")
        try:
            with page.expect_download(timeout=60_000) as dl_info:
                exported = page.evaluate('''() => {
                    // Visibilité réelle via getBoundingClientRect (offsetParent échoue
                    // sur les éléments Webix en position:fixed)
                    function vis(el) {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }

                    const kw = ["excel", "export", "exporter", "xls", "fomat"];
                    const tabNames = new Set(["Laboratoires", "Familles", "Produits", "Marques"]);

                    // ── Méthode 1 : position visuelle (bande des onglets) ───────────
                    // Dans Webix 6 les onglets ont la classe .webix_item_tab.
                    // Le bouton export est le 1er élément visible à droite du dernier onglet.
                    {
                        let maxRight = 0, bandTop = 0, bandBottom = 0;

                        // Onglets Webix
                        for (const tab of document.querySelectorAll(
                                ".webix_item_tab, .webix_list_item, [role=tab]")) {
                            if (!tabNames.has(tab.textContent.trim())) continue;
                            const r = tab.getBoundingClientRect();
                            if (r.width < 4 || r.height < 4) continue;
                            if (r.right > maxRight) {
                                maxRight = r.right; bandTop = r.top; bandBottom = r.bottom;
                            }
                        }

                        // Fallback : chercher par texte exact avec TreeWalker
                        if (maxRight === 0) {
                            const walker = document.createTreeWalker(
                                document.body, NodeFilter.SHOW_ELEMENT);
                            while (walker.nextNode()) {
                                const el = walker.currentNode;
                                if (!tabNames.has(el.textContent.trim())) continue;
                                const r = el.getBoundingClientRect();
                                if (r.width < 4 || r.width > 300 || r.height < 4) continue;
                                if (r.right > maxRight) {
                                    maxRight = r.right; bandTop = r.top; bandBottom = r.bottom;
                                }
                            }
                        }

                        if (maxRight > 0) {
                            const midY   = (bandTop + bandBottom) / 2;
                            const halfH  = (bandBottom - bandTop) / 2 + 6;
                            const cands  = [];

                            // Éléments Webix connus + éléments standard à droite des onglets
                            const sel = ".webix_el_icon, .webix_el_button, .webix_view, button, a";
                            for (const el of document.querySelectorAll(sel)) {
                                const r = el.getBoundingClientRect();
                                if (r.left <= maxRight + 2)          continue;
                                if (r.top  >  midY + halfH)          continue;
                                if (r.bottom < midY - halfH)         continue;
                                if (r.width  < 8 || r.height < 8)   continue;
                                if (r.width  > 160 || r.height > 80) continue;
                                cands.push(el);
                            }
                            cands.sort((a, b) =>
                                a.getBoundingClientRect().left - b.getBoundingClientRect().left);

                            if (cands.length) {
                                // 1er depuis la gauche = export (2e = corbeille d'après screenshot)
                                const target = cands[0];
                                const r = target.getBoundingClientRect();
                                // Cliquer la target ou son enfant button/template
                                (target.querySelector("button, .webix_template") || target).click();
                                return "pos:" + target.tagName + "/" + target.className.slice(0,30)
                                       + "@x" + Math.round(r.left);
                            }

                            // Dernier recours : elementFromPoint légèrement avant le bord droit
                            const el = document.elementFromPoint(
                                window.innerWidth - 60, midY);
                            if (el) {
                                let cur = el;
                                while (cur && cur !== document.body) {
                                    if (vis(cur) && (cur.onclick ||
                                        /webix_el|webix_icon|webix_template|button/i.test(cur.className))) {
                                        cur.click();
                                        return "efp:" + cur.className.slice(0,30);
                                    }
                                    cur = cur.parentElement;
                                }
                            }
                        }
                    }

                    // ── Méthode 2 : API interne Webix 6 ────────────────────────────
                    if (typeof webix !== "undefined") {
                        const coll = webix.ui?._collection || webix.ui?.views || {};
                        for (const v of Object.values(coll)) {
                            const tip = (v.config?.tooltip || v.config?.label || "").toLowerCase();
                            if (!kw.some(k => tip.includes(k))) continue;
                            const node = v.getNode?.();
                            const r = node?.getBoundingClientRect();
                            if (r && r.width > 0) { node.click(); return "webix-api:" + tip.slice(0,40); }
                        }
                        if (webix.toExcel) {
                            const grid = Object.values(coll).find(
                                v => v.name === "datatable" && v.isVisible?.());
                            if (grid) { webix.toExcel(grid); return "webix.toExcel"; }
                        }
                    }

                    // ── Méthode 3 : attribut webix_tooltip ─────────────────────────
                    for (const el of document.querySelectorAll("[webix_tooltip]")) {
                        if (!vis(el)) continue;
                        const tip = (el.getAttribute("webix_tooltip") || "").toLowerCase();
                        if (!kw.some(k => tip.includes(k))) continue;
                        (el.querySelector("button") || el).click();
                        return "webix_tooltip:" + tip.slice(0,40);
                    }

                    // ── Méthode 4 : mot-clé texte/title/aria ───────────────────────
                    for (const el of document.querySelectorAll(
                            "button, a, [role=button], .webix_el_button button")) {
                        if (!vis(el)) continue;
                        const hay = (el.textContent + " " + (el.title||"")
                            + " " + (el.getAttribute("aria-label")||"")
                            + " " + (el.getAttribute("webix_tooltip")||"")).toLowerCase();
                        if (kw.some(k => hay.includes(k))) { el.click(); return "kw:" + hay.slice(0,40); }
                    }

                    return false;
                }''')
                if not exported:
                    raise RuntimeError("Aucun bouton export trouvé — vérifiez l'interface OSPHARM")
            dl = dl_info.value
            dl.save_as(tmp)
        except RuntimeError:
            browser.close()
            raise
        except Exception as e:
            browser.close()
            raise RuntimeError(f"Export Excel échoué : {e}")

        browser.close()

    # Lecture Excel
    progress("Lecture du fichier Excel…")
    wb = openpyxl.load_workbook(tmp, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = [str(h or "").strip() for h in next(rows_iter)]
    rows = []
    for row in rows_iter:
        if any(v is not None for v in row):
            rows.append({h: v for h, v in zip(headers, row)})
    wb.close()

    # Upload vers Supabase Storage
    file_url = ""
    if user_id:
        try:
            progress("Sauvegarde du fichier en ligne…")
            from supabase_client import upload_file_sync, get_signed_url_sync
            import datetime
            date_str = datetime.date.today().strftime("%Y-%m-%d")
            filename  = f"ospharm_{date_str}.xlsx"
            with open(tmp, "rb") as f:
                file_bytes = f.read()
            path     = upload_file_sync(user_id, "ospharm", filename, file_bytes,
                                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            file_url = get_signed_url_sync(path)
        except Exception as e:
            print(f"  [warn] Storage upload failed: {e}")

    return rows, file_url


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
