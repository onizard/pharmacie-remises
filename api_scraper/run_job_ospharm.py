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


def _login(page, creds):
    """Remplit le formulaire OAuth OSPHARM et attend le retour sur datastat."""
    page.locator("input[type='email'],input[name='username'],input[name='email']").first.fill(
        creds["user"], timeout=15_000)
    page.locator("input[type='password'],input[name='password']").first.fill(
        creds["pass"], timeout=5_000)
    page.locator("button[type='submit'],input[type='submit']").first.click(timeout=5_000)
    try:
        page.wait_for_url("*datastat.ospharm.org*", timeout=40_000)
    except PWTimeout:
        raise RuntimeError("Identifiants OSPHARM incorrects ou timeout login")
    try:
        page.wait_for_function("() => typeof webix !== 'undefined'", timeout=20_000)
    except Exception:
        page.wait_for_timeout(3_000)


def _reauth_if_needed(page, creds, label=""):
    """Si OSPHARM a redirigé vers la page d'auth, on se reconnecte."""
    if "accounts" not in page.url:
        return
    print(f"  [warn] Re-auth OSPHARM{' (' + label + ')' if label else ''} — url={page.url[:80]}")
    _login(page, creds)


def run_ospharm(creds: dict, progress, user_id: str = "") -> tuple[list[dict], str]:
    import tempfile, openpyxl

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

        # 1. Login
        progress("Connexion OSPHARM…")
        page.goto(OSPHARM_URL, wait_until="networkidle", timeout=30_000)

        if "accounts" in page.url or "login" in page.url:
            _login(page, creds)

        if "datastat.ospharm.org" not in page.url or "accounts" in page.url:
            raise RuntimeError("Identifiants OSPHARM incorrects")

        progress("Connecté — chargement…")
        try:
            page.wait_for_function("() => typeof webix !== 'undefined'", timeout=20_000)
        except Exception:
            page.wait_for_timeout(3_000)

        # 2. Navigation vers Toutes mes ventes (hash routing SPA — pas de page.goto)
        progress("Navigation vers Toutes mes ventes…")
        if "sellout" not in page.url:
            # Naviguer via le hash pour rester dans la SPA sans déclencher un rechargement
            page.evaluate("() => { window.location.hash = '#!/top/sellout.all'; }")
            page.wait_for_timeout(3_000)
            _reauth_if_needed(page, creds, "sellout.all")
            try:
                page.wait_for_function("() => typeof webix !== 'undefined'", timeout=15_000)
            except Exception:
                page.wait_for_timeout(2_000)

        # 3. Sélection "Année lissée" + Valider
        progress("Sélection Année lissée…")
        _reauth_if_needed(page, creds, "avant Année lissée")
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
            page.wait_for_timeout(3_000)

        # 4. Onglet Produits
        progress("Sélection onglet Produits…")
        _reauth_if_needed(page, creds, "avant Produits")
        prod_ok = page.evaluate('''() => {
            const els = document.querySelectorAll(".webix_item_tab,.webix_list_item,[class*='tab'],li,span,div,a");
            for (const el of els) {
                if (el.textContent.trim() === "Produits") {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) { el.click(); return true; }
                }
            }
            return false;
        }''')
        if not prod_ok:
            try:
                page.get_by_text("Produits", exact=True).first.click(timeout=8_000)
            except Exception:
                pass
        page.wait_for_timeout(3_000)
        _reauth_if_needed(page, creds, "après Produits")

        # 5. Export Excel
        progress("Export Excel…")
        tmp = tempfile.mktemp(suffix=".xlsx")

        # ── Debug URL avant export ────────────────────────────────────────────
        dbg = page.evaluate('''() => {
            const vids = [...document.querySelectorAll("[view_id]")];
            const tabs = [...document.querySelectorAll(".webix_item_tab")];
            const tooltipEls = [...document.querySelectorAll("[webix_tooltip]")];
            return {
                url: location.href,
                vw: window.innerWidth,
                viewIdCount: vids.length,
                viewIdSample: vids.slice(0, 8).map(e => ({
                    id: e.getAttribute("view_id"),
                    cls: e.className.slice(0, 40),
                    r: (r => ({x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)}))(e.getBoundingClientRect()),
                })),
                tabItems: tabs.map(t => t.textContent.trim()).slice(0, 8),
                tooltipEls: tooltipEls.slice(0, 5).map(e => e.getAttribute("webix_tooltip")),
                webix: typeof webix !== "undefined" ? {
                    toExcel: typeof webix.toExcel,
                    dollar: typeof webix.$$,
                    uiKeys: Object.keys(webix.ui || {}).slice(0, 10),
                } : "absent",
            };
        }''')
        print(f"  [export-dbg] {dbg}")

        try:
            with page.expect_download(timeout=60_000) as dl_info:
                exported = page.evaluate('''() => {
                    function vis(el) {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }
                    const kw = ["excel", "export", "exporter", "xls", "fomat"];

                    // ── M1 : view_id + webix.$$ (API publique Webix 6) ──────────────
                    // Chaque div racine d'une vue Webix 6 a l'attribut view_id.
                    // webix.$$(id).config.tooltip contient le tooltip configuré.
                    if (typeof webix !== "undefined" && typeof webix.$$ === "function") {
                        for (const el of document.querySelectorAll("[view_id]")) {
                            if (!vis(el)) continue;
                            const v = webix.$$(el.getAttribute("view_id"));
                            if (!v) continue;
                            const tip = (v.config?.tooltip || v.config?.label || "").toLowerCase();
                            if (kw.some(k => tip.includes(k))) {
                                el.click();
                                return "view_id:" + tip.slice(0, 40);
                            }
                        }
                        // webix.toExcel sur la datatable visible (via view_id du .webix_dtable)
                        if (webix.toExcel) {
                            for (const el of document.querySelectorAll(".webix_dtable[view_id]")) {
                                if (!vis(el)) continue;
                                const grid = webix.$$(el.getAttribute("view_id"));
                                if (grid) { webix.toExcel(grid); return "webix.toExcel:dtable"; }
                            }
                        }
                    }

                    // ── M2 : position (bande des onglets, 1er élément à droite) ────
                    const tabNames = new Set(["Laboratoires", "Familles", "Produits", "Marques"]);
                    let maxRight = 0, bandTop = 0, bandBottom = 0;
                    // Onglets Webix : .webix_item_tab ou éléments avec view_id dont le texte = nom onglet
                    for (const el of document.querySelectorAll(".webix_item_tab, [view_id]")) {
                        const txt = el.textContent.trim();
                        if (!tabNames.has(txt)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 4 || r.height < 4) continue;
                        if (r.right > maxRight) { maxRight = r.right; bandTop = r.top; bandBottom = r.bottom; }
                    }
                    if (maxRight > 0) {
                        const midY = (bandTop + bandBottom) / 2;
                        const halfH = (bandBottom - bandTop) / 2 + 8;
                        const cands = [];
                        for (const el of document.querySelectorAll("[view_id], button, .webix_el_icon")) {
                            const r = el.getBoundingClientRect();
                            if (r.left <= maxRight + 2 || r.top > midY + halfH || r.bottom < midY - halfH) continue;
                            if (r.width < 8 || r.height < 8 || r.width > 160 || r.height > 80) continue;
                            cands.push(el);
                        }
                        cands.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                        if (cands.length) {
                            (cands[0].querySelector("button,.webix_template") || cands[0]).click();
                            const r = cands[0].getBoundingClientRect();
                            return "pos:" + cands[0].tagName + "@x" + Math.round(r.left);
                        }
                        // elementFromPoint à 60 et 90px du bord droit
                        for (const xOff of [60, 90, 120]) {
                            const el = document.elementFromPoint(window.innerWidth - xOff, midY);
                            if (el && vis(el) && el.getBoundingClientRect().width < 200) {
                                el.click();
                                return "efp@" + xOff + ":" + el.tagName + "." + el.className.slice(0,20);
                            }
                        }
                    }

                    // ── M3 : attribut webix_tooltip ────────────────────────────────
                    for (const el of document.querySelectorAll("[webix_tooltip]")) {
                        if (!vis(el)) continue;
                        const tip = (el.getAttribute("webix_tooltip") || "").toLowerCase();
                        if (kw.some(k => tip.includes(k))) {
                            (el.querySelector("button") || el).click();
                            return "webix_tooltip:" + tip.slice(0, 40);
                        }
                    }

                    // ── M4 : mot-clé texte/title/aria ──────────────────────────────
                    for (const el of document.querySelectorAll("button,a,[role=button]")) {
                        if (!vis(el)) continue;
                        const hay = (el.textContent + " " + (el.title||"") + " " + (el.getAttribute("aria-label")||"")).toLowerCase();
                        if (kw.some(k => hay.includes(k))) { el.click(); return "kw:" + hay.slice(0,40); }
                    }

                    return false;
                }''')
                if not exported:
                    raise RuntimeError(f"Aucun bouton export trouvé — debug={dbg}")
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
