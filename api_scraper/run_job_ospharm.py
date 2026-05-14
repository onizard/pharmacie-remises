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


def _update_job(status: str, message: str = "", rows=None, error: str = "", blocking: bool = False,
                period_start: str = "", period_end: str = ""):
    def _do():
        try:
            state = _supa_get_state()
            job = {
                "status":  status,
                "message": message,
                "rows":    rows or [],
                "total":   len(rows) if rows else 0,
                "error":   error,
            }
            if period_start:
                job["period_start"] = period_start
                job["period_end"]   = period_end
            state["ospharm_job"] = job
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

def _extract_period(page) -> tuple[str, str]:
    """Extrait la plage de dates affichée par OSPHARM. Inference en fallback."""
    import re as _re, datetime as _dt
    try:
        raw = page.evaluate(r'''() => {
            const re = /(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\s*[àa\-\–]\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})/i;
            const m = document.body.innerText.match(re);
            return m ? [m[1], m[2]] : null;
        }''')
        if raw:
            def _parse(s):
                parts = _re.split(r'[\/\-]', s)
                if len(parts) != 3: return None
                d, mo, y = int(parts[0]), int(parts[1]), int(parts[2])
                if y < 100: y += 2000
                return f"{y:04d}-{mo:02d}-{d:02d}"
            ps, pe = _parse(raw[0]), _parse(raw[1])
            if ps and pe:
                print(f"  [period] scraped: {ps} → {pe}")
                return ps, pe
    except Exception as exc:
        print(f"  [period] scraping error: {exc}")
    # Inference: Année lissée = 01/05/(Y-1) à 30/04/Y où Y = dernier avril écoulé
    today = _dt.date.today()
    apr30 = _dt.date(today.year, 4, 30)
    y = today.year if today >= apr30 else today.year - 1
    pe = f"{y:04d}-04-30"
    ps = f"{y-1:04d}-05-01"
    print(f"  [period] inferred: {ps} → {pe}")
    return ps, pe


def _wait_webix(page, timeout=20_000):
    """Attend que webix soit défini, absorbe toutes les erreurs (navigation, timeout)."""
    try:
        page.wait_for_function("() => typeof webix !== 'undefined'", timeout=timeout)
    except Exception:
        page.wait_for_timeout(3_000)


def _js_click(page, text):
    try:
        return page.evaluate(f'''() => {{
            const t = {repr(text)}.toLowerCase();
            const all = document.querySelectorAll(
                ".webix_list_item,.webix_el_button button,button,input[type=button],input[type=submit],[role=option],[role=button]");
            for (const el of all) {{
                const hay = (el.textContent + " " + (el.value||"") + " " + (el.getAttribute("aria-label")||"")).trim().toLowerCase();
                if (hay === t || hay.includes(t)) {{ el.click(); return true; }}
            }}
            return false;
        }}''')
    except Exception:
        return False


def _login(page, creds):
    """Remplit le formulaire OAuth OSPHARM et attend le retour sur datastat."""
    page.locator("input[type='email'],input[name='username'],input[name='email']").first.fill(
        creds["user"], timeout=15_000)
    page.locator("input[type='password'],input[name='password']").first.fill(
        creds["pass"], timeout=5_000)
    page.locator("button[type='submit'],input[type='submit']").first.click(timeout=5_000)
    try:
        page.wait_for_url("*datastat.ospharm.org*", timeout=45_000)
    except PWTimeout:
        pass  # check URL below
    if "datastat.ospharm.org" not in page.url or "accounts" in page.url:
        raise RuntimeError("Identifiants OSPHARM incorrects")
    _wait_webix(page)


def _reauth_if_needed(page, creds, label=""):
    """Si OSPHARM a redirigé vers la page d'auth, on se reconnecte."""
    if "accounts" not in page.url:
        return
    print(f"  [warn] Re-auth OSPHARM{' (' + label + ')' if label else ''} — url={page.url[:80]}")
    _login(page, creds)


def run_ospharm(creds: dict, progress, user_id: str = "") -> tuple[list[dict], str, str, str]:
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
        _wait_webix(page)

        # ── helper : naviguer vers la section ventes ───────────────────────────
        def _on_ventes_page():
            return "organization.dashboard" not in page.url and "login" not in page.url

        def _goto_sellout():
            """Navigue vers la section ventes. True si on n'est plus sur le dashboard."""
            _before = page.url

            # M1 : clic exact "Ventes" dans la sidebar (item visible dans le nav-debug)
            try:
                _loc = page.get_by_text("Ventes", exact=True).first
                if _loc.is_visible(timeout=3_000):
                    _loc.click(timeout=5_000)
                    print("  [nav] cliqué 'Ventes' exact")
            except Exception:
                pass

            # M2 : clic JS large sur items nav contenant "ventes"/"sellout"
            if not _on_ventes_page():
                try:
                    _c = page.evaluate('''() => {
                        const targets = ["ventes", "sellout", "tout"];
                        const sels = ".webix_sidebar_item,.webix_list_item,.webix_tree_item,li,a,[role=menuitem],[role=treeitem]";
                        for (const el of document.querySelectorAll(sels)) {
                            const txt = el.textContent.trim().toLowerCase();
                            const r = el.getBoundingClientRect();
                            if (r.width < 2 || r.height < 2) continue;
                            for (const t of targets) {
                                if (txt === t || (txt.includes(t) && txt.length < 40)) {
                                    el.click(); return txt.slice(0, 40);
                                }
                            }
                        }
                        return false;
                    }''')
                    if _c:
                        print(f"  [nav] JS click: {_c!r}")
                except Exception:
                    pass

            # M3 : webix.$$().show()
            if not _on_ventes_page():
                try:
                    _r = page.evaluate('''() => {
                        if (typeof webix === "undefined") return "no-webix";
                        for (const id of ["top", "app", "main", "layout", "router"]) {
                            try {
                                const v = webix.$$(id);
                                if (v && typeof v.show === "function") {
                                    v.show("sellout.all"); return "show:" + id;
                                }
                            } catch(e) {}
                        }
                        return false;
                    }''')
                    print(f"  [nav] webix.show: {_r}")
                except Exception:
                    pass

            # M4 : hash change (change l'URL même si Webix ne l'honore pas)
            if not _on_ventes_page():
                try:
                    page.evaluate("() => { window.location.hash = '#!/top/sellout.all'; }")
                except Exception:
                    pass

            # Attendre navigation hors du dashboard
            try:
                page.wait_for_function(
                    "() => !location.href.includes('organization.dashboard')",
                    timeout=15_000,
                )
            except Exception:
                page.wait_for_timeout(4_000)

            _reauth_if_needed(page, creds, "ventes")
            _wait_webix(page)
            return _on_ventes_page()

        # 2. Navigation vers la section ventes
        progress("Navigation vers Toutes mes ventes…")
        if "organization.dashboard" in page.url or "login" in page.url:
            # Stabiliser la page (Webix SPA route encore après login)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            page.wait_for_timeout(1_500)

            if not _goto_sellout():
                # Capture les textes visibles et l'URL dans le message d'erreur
                _nav_visible = []
                try:
                    _nav_visible = page.evaluate('''() => {
                        const items = [];
                        for (const el of document.querySelectorAll("*")) {
                            if (el.children.length > 0) continue;
                            const txt = el.textContent.trim();
                            if (txt.length < 3 || txt.length > 50) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width < 2 || r.height < 2) continue;
                            items.push(txt);
                        }
                        return [...new Set(items)].slice(0, 40);
                    }''')
                except Exception as _e:
                    _nav_visible = [f"err:{_e}"]
                raise RuntimeError(
                    f"Nav ventes échoué — url={page.url[:80]} — visible={_nav_visible}"
                )

            print(f"  [nav] url après ventes: {page.url[:80]}")

        print(f"  [step3] url={page.url[:80]}")
        # 3. Sélection "Année lissée" — tentative légère seulement
        # On NE clique pas le bouton global (webix_el_htmlbutton) qui ouvre le filtre
        # date du dashboard et provoque une redirection. On cherche simplement si
        # "Année lissée" est déjà visible sur la page ventes et on le clique.
        progress("Sélection Année lissée…")
        _reauth_if_needed(page, creds, "avant Année lissée")
        try:
            _loc_lissee = page.get_by_text("Année lissée", exact=False).first
            if _loc_lissee.is_visible(timeout=3_000):
                _loc_lissee.click(timeout=5_000)
                page.wait_for_timeout(1_000)
                # Cherche Valider dans un popup SEULEMENT (pas navigation globale)
                try:
                    _val_loc = page.locator(
                        ".webix_window button,.webix_popup button,.webix_modal button"
                    ).filter(has_text="Valider").first
                    if _val_loc.is_visible(timeout=2_000):
                        _val_loc.click(timeout=3_000)
                        _wait_webix(page)
                except Exception:
                    pass
        except Exception:
            pass

        # Si on est revenu sur le dashboard, retourner sur ventes
        if not _on_ventes_page():
            print(f"  [warn] Année lissée a redirigé → {page.url[:60]} — retour ventes…")
            _goto_sellout()

        _url_post3 = page.url
        print(f"  [step3-done] url={_url_post3[:80]}")
        # Capture la plage de dates affichée (quelle que soit la sélection)
        period_start, period_end = _extract_period(page)

        # 4. Onglet Produits — UNIQUEMENT les tabs webix, pas les liens de navigation
        print(f"  [step4] url={page.url[:80]}")
        progress("Sélection onglet Produits…")
        _reauth_if_needed(page, creds, "avant Produits")
        try:
            prod_ok = page.evaluate('''() => {
                // Cherche uniquement dans les tabs Webix (pas les liens <a> de nav)
                for (const el of document.querySelectorAll(".webix_item_tab")) {
                    if (el.textContent.trim() === "Produits") {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) { el.click(); return true; }
                    }
                }
                return false;
            }''')
        except Exception:
            prod_ok = False
        if not prod_ok:
            try:
                # Cibler explicitement les tabs webix, pas get_by_text générique
                page.locator(".webix_item_tab").filter(has_text="Produits").first.click(timeout=8_000)
            except Exception:
                pass
        page.wait_for_timeout(3_000)
        _reauth_if_needed(page, creds, "après Produits")
        _url_post4 = page.url
        print(f"  [step4-done] url={_url_post4[:80]}")
        if "organization.dashboard" in _url_post4:
            raise RuntimeError(f"Step4: retour dashboard — url={_url_post4[:80]}")

        # 5. Export Excel — interception réseau (fonctionne quel que soit le mécanisme
        #    de téléchargement : réponse HTTP serveur, Blob, nouvel onglet, etc.)
        progress("Export Excel…")
        tmp = tempfile.mktemp(suffix=".xlsx")
        _excel_bytes = []

        def _on_response(resp):
            if _excel_bytes:
                return
            try:
                ct = resp.headers.get("content-type", "").lower()
                cd = resp.headers.get("content-disposition", "").lower()
                is_excel = any(x in ct for x in
                    ["excel", "spreadsheet", "openxmlformats", "xls", "octet-stream"])
                has_attach = "attachment" in cd
                if is_excel or has_attach:
                    body = resp.body()
                    if len(body) > 500:
                        _excel_bytes.append(body)
                        print(f"  [export] intercepté {len(body)} bytes — ct={ct[:50]}")
            except Exception as _ie:
                print(f"  [export-intercept] {_ie}")

        context.on("response", _on_response)

        # ── Debug ──────────────────────────────────────────────────────────────
        try:
            dbg = page.evaluate('''() => {
                const tabs = [...document.querySelectorAll(".webix_item_tab")];
                const tips = [...document.querySelectorAll("[webix_tooltip]")];
                return {
                    url: location.href,
                    tabItems: tabs.map(t => t.textContent.trim()).slice(0, 8),
                    tooltipEls: tips.slice(0, 5).map(e => e.getAttribute("webix_tooltip")),
                    webix: typeof webix !== "undefined" ? {
                        toExcel: typeof webix.toExcel, dollar: typeof webix.$$,
                    } : "absent",
                };
            }''')
            print(f"  [export-dbg] {dbg}")
        except Exception as _dbg_err:
            dbg = {}
            print(f"  [export-dbg] skipped ({_dbg_err})")

        # ── Clic bouton export (M1→M4) ─────────────────────────────────────────
        try:
            exported = page.evaluate('''() => {
                function vis(el) {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }
                const kw = ["excel", "export", "exporter", "xls", "fomat"];

                // M1 : view_id + webix.$$
                if (typeof webix !== "undefined" && typeof webix.$$ === "function") {
                    for (const el of document.querySelectorAll("[view_id]")) {
                        if (!vis(el)) continue;
                        const v = webix.$$(el.getAttribute("view_id"));
                        if (!v) continue;
                        const tip = (v.config?.tooltip || v.config?.label || "").toLowerCase();
                        if (kw.some(k => tip.includes(k))) { el.click(); return "view_id:" + tip.slice(0,40); }
                    }
                    if (webix.toExcel) {
                        for (const el of document.querySelectorAll(".webix_dtable[view_id]")) {
                            if (!vis(el)) continue;
                            const grid = webix.$$(el.getAttribute("view_id"));
                            if (grid) { webix.toExcel(grid); return "webix.toExcel:dtable"; }
                        }
                    }
                }
                // M2 : position bande onglets
                const tabNames = new Set(["Laboratoires", "Familles", "Produits", "Marques"]);
                let maxRight = 0, bandTop = 0, bandBottom = 0;
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
                        return "pos:" + cands[0].tagName + "@x" + Math.round(cands[0].getBoundingClientRect().left);
                    }
                    for (const xOff of [60, 90, 120]) {
                        const el = document.elementFromPoint(window.innerWidth - xOff, midY);
                        if (el && vis(el) && el.getBoundingClientRect().width < 200) {
                            el.click(); return "efp@" + xOff + ":" + el.tagName;
                        }
                    }
                }
                // M3 : webix_tooltip
                for (const el of document.querySelectorAll("[webix_tooltip]")) {
                    if (!vis(el)) continue;
                    const tip = (el.getAttribute("webix_tooltip") || "").toLowerCase();
                    if (kw.some(k => tip.includes(k))) { (el.querySelector("button") || el).click(); return "tooltip:" + tip.slice(0,40); }
                }
                // M4 : texte/title/aria
                for (const el of document.querySelectorAll("button,a,[role=button]")) {
                    if (!vis(el)) continue;
                    const hay = (el.textContent + " " + (el.title||"") + " " + (el.getAttribute("aria-label")||"")).toLowerCase();
                    if (kw.some(k => hay.includes(k))) { el.click(); return "kw:" + hay.slice(0,40); }
                }
                return false;
            }''')
        except Exception as _eval_err:
            if "context" in str(_eval_err).lower() or "destroyed" in str(_eval_err).lower():
                exported = "context-destroyed-ok"
                print(f"  [export] context destroyed au clic — download en route")
            else:
                browser.close()
                raise RuntimeError(f"Export Excel : evaluate échoué : {_eval_err}")

        if not exported:
            browser.close()
            raise RuntimeError(f"Aucun bouton export trouvé — debug={dbg}")

        # ── Poll Valider (popup OSPHARM, jusqu'à 20s) ──────────────────────────
        print(f"  [export] bouton cliqué ({exported}), poll Valider…")
        _val_clicked = False
        for _attempt in range(8):
            page.wait_for_timeout(2_500)
            if _excel_bytes:
                print(f"  [export] fichier déjà reçu avant Valider — ok")
                break
            try:
                _loc = page.locator(
                    ".webix_window button, .webix_popup button, .webix_modal button,"
                    " button, input[type=button], [role=button]"
                ).filter(has_text="Valider").first
                if _loc.is_visible(timeout=500):
                    _loc.click(timeout=3_000)
                    _val_clicked = True
                    print(f"  [export] Valider cliqué (locator, attempt {_attempt+1})")
                    break
            except Exception:
                pass
            try:
                if _js_click(page, "Valider"):
                    _val_clicked = True
                    print(f"  [export] Valider cliqué (js, attempt {_attempt+1})")
                    break
            except Exception as _je:
                if "context" in str(_je).lower() or "destroyed" in str(_je).lower():
                    print(f"  [export] context destroyed pendant poll Valider — ok")
                    break
        if not _val_clicked and not _excel_bytes:
            print(f"  [export] Valider non trouvé après 20s")

        # ── Attente réponse Excel jusqu'à 45s ──────────────────────────────────
        progress("Attente du fichier Excel…")
        for _w in range(18):
            if _excel_bytes:
                break
            page.wait_for_timeout(2_500)
            print(f"  [export] attente... {(_w+1)*2.5:.0f}s")

        if not _excel_bytes:
            browser.close()
            raise RuntimeError(f"Export Excel : aucun fichier reçu en 45s. Debug: {dbg}")

        with open(tmp, "wb") as f:
            f.write(_excel_bytes[0])

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

    return rows, file_url, period_start, period_end


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
        rows, _fu, ps, pe = run_ospharm(creds, progress)
        _update_job("done", f"{len(rows)} lignes extraites", rows, blocking=True,
                    period_start=ps, period_end=pe)
        print(f"\n✅  {len(rows)} lignes OSPHARM sauvegardées dans Supabase. ({time.time()-t0:.1f}s total)")
    except Exception as e:
        _update_job("error", error=str(e), blocking=True)
        print(f"\n❌  {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
