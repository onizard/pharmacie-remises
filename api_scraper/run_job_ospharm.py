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
                period_start: str = "", period_end: str = "", file_url: str = ""):
    def _do():
        try:
            state = _supa_get_state()
            job = {
                "status":   status,
                "message":  message,
                "rows":     rows or [],
                "total":    len(rows) if rows else 0,
                "error":    error,
                "file_url": file_url,
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


# ── Row compaction ────────────────────────────────────────────────────────────

def _compact_osp_rows(rows: list[dict]) -> list[dict]:
    """Convertit les lignes OSPHARM brutes (24 cols) en {cip13, qty, libelle}.
    Réduit ~5 Mo → ~400 Ko pour le stockage dans Supabase.
    """
    import re as _re
    if not rows:
        return []

    def _n(k):
        s = (k or "").lower()
        for a, b in [("é","e"),("è","e"),("ê","e"),("à","a"),("ù","u"),("î","i"),("ô","o")]:
            s = s.replace(a, b)
        return _re.sub(r"[^a-z0-9]", "", s)

    keys = list(rows[0].keys())
    cip_k = next((k for k in keys if _n(k) == "codeean"), None) or \
            next((k for k in keys if any(p in _n(k) for p in ("cip", "ean", "acl"))), None)
    qty_k = next((k for k in keys if _n(k) == "quantite"), None) or \
            next((k for k in keys if any(p in _n(k) for p in ("qte", "qty"))
                  and "n1" not in _n(k) and "evo" not in _n(k)), None)
    lib_k = next((k for k in keys if _n(k) == "libelleproduit"), None) or \
            next((k for k in keys if "produit" in _n(k)), None) or \
            next((k for k in keys if "libelle" in _n(k)), None)

    if not cip_k or not qty_k:
        return rows

    result = []
    for r in rows:
        raw = _re.sub(r"\D", "", str(r.get(cip_k) or ""))
        cip13 = raw if len(raw) == 13 else ("340000" + raw if len(raw) == 7 else None)
        try:
            qty = float(str(r.get(qty_k) or 0).replace(",", "."))
        except (ValueError, TypeError):
            qty = 0.0
        if not cip13 or qty <= 0:
            continue
        result.append({
            "cip13":   cip13,
            "qty":     qty,
            "libelle": str(r.get(lib_k) or "").strip() if lib_k else "",
        })
    return result


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

    # ── Screenshots diagnostic ─────────────────────────────────────────────────
    _screenshots: list[tuple[str, bytes]] = []  # [(label, png_bytes)]

    def _snap(label: str):
        """Capture un screenshot et le conserve pour upload en fin de run."""
        try:
            data = page.screenshot(full_page=False)
            _screenshots.append((label, data))
            print(f"  [snap] {label} ({len(data):,} bytes) url={page.url[:70]}")
        except Exception as _se:
            print(f"  [snap] {label} ERREUR: {_se}")

    def _upload_screenshots():
        if not user_id or not _screenshots:
            return
        try:
            from supabase_client import upload_file_sync
            import datetime
            ts = datetime.datetime.now().strftime("%H%M%S")
            for label, data in _screenshots:
                safe = label.replace(" ", "_").replace("/", "-")
                path = upload_file_sync(user_id, "ospharm_debug",
                                        f"{ts}_{safe}.png", data, "image/png")
                print(f"  [snap-upload] {path}")
        except Exception as _ue:
            print(f"  [snap-upload] ERREUR: {_ue}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True,
                                      viewport={"width": 1440, "height": 900})
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
        _snap("1_apres_login")

        # ── helper : naviguer vers la section ventes ───────────────────────────
        # La condition d'arrivée : les tabs "Laboratoires/Familles/Produits/Marques"
        # sont visibles. C'est le seul signe fiable qu'on est bien sur la page ventes.
        def _ventes_tabs_visible():
            # Condition 1 : URL contient une route ventes (méthode la plus fiable)
            try:
                if any(x in page.url for x in ["sellout", "ventes.all", "mysellout"]):
                    return True
            except Exception:
                pass
            # Condition 2 : segments Laboratoires/Familles/Produits/Marques visibles
            # (boutons webix_segment_X sur la page sellout.all)
            try:
                return page.evaluate('''() => {
                    const kw = ["laboratoire", "famille", "produit", "marque"];
                    for (const el of document.querySelectorAll(
                        ".webix_item_tab, .webix_segment_0, .webix_segment_1, .webix_segment_N, button"
                    )) {
                        const t = el.textContent.trim().toLowerCase();
                        if (kw.some(k => t.startsWith(k))) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 1 && r.height > 1) return true;
                        }
                    }
                    return false;
                }''') or False
            except Exception:
                return False

        def _goto_sellout():
            """Navigue vers 'Toutes les ventes' (sellout.all)."""
            def _wait_sellout(ms=10_000):
                try:
                    page.wait_for_function(
                        '() => ["sellout","ventes.all"].some(x => location.hash.includes(x))',
                        timeout=ms,
                    )
                    return True
                except Exception:
                    return False

            # ── M0 : sidebar select + clic DOM réel sur le nœud ─────────────────
            try:
                _r0 = page.evaluate('''() => {
                    if (typeof webix === "undefined") return "no-webix";
                    const sb = webix.$$("top:menu")
                             || webix.$$(document.querySelector(".webix_sidebar")
                                         ?.getAttribute("view_id"));
                    if (!sb) return "no-sb";
                    try { sb.open("sellout"); } catch(e) {}
                    sb.select("sellout.all");
                    const node = sb.getItemNode ? sb.getItemNode("sellout.all") : null;
                    if (node) {
                        node.dispatchEvent(new MouseEvent("click",
                            {bubbles:true, cancelable:true}));
                        return "click-node";
                    }
                    return "select-only";
                }''')
                print(f"  [nav] M0: {_r0}")
                if _r0 not in ("no-webix", "no-sb"):
                    page.wait_for_timeout(5_000)
                    if _ventes_tabs_visible(): return True
                    if _wait_sellout(5_000) and _ventes_tabs_visible(): return True
            except Exception as _e:
                print(f"  [nav] M0 err: {_e}")

            # ── M1 : location.href (router hash SPA)
            # NOTE : location.hash setter ajoute '#' automatiquement → double hash.
            # On utilise location.href avec le hash complet pour éviter ce piège.
            try:
                _r1 = page.evaluate("""() => {
                    const base = location.href.split('#')[0];
                    location.href = base + '#!/top/sellout.all';
                    return location.href;
                }""")
                print(f"  [nav] M1 href: {str(_r1)[:80]}")
                page.wait_for_timeout(5_000)
                if _ventes_tabs_visible(): return True
                if _wait_sellout(8_000) and _ventes_tabs_visible(): return True
            except Exception as _e:
                print(f"  [nav] M1 err: {_e}")

            # ── M2 : expand "Analyse des ventes" puis clic "Toutes les ventes" ──
            try:
                _loc_a = page.get_by_text("Analyse des ventes", exact=True).first
                if _loc_a.is_visible(timeout=1_500):
                    _loc_a.click(force=True, timeout=5_000)
                    print(f"  [nav] M2 expanded 'Analyse des ventes'")
                    page.wait_for_timeout(2_000)
            except Exception as _e:
                print(f"  [nav] M2a err: {_e}")
            try:
                _loc_t = page.get_by_text("Toutes les ventes", exact=True).first
                if _loc_t.is_visible(timeout=3_000):
                    _loc_t.click(force=True, timeout=5_000)
                    print(f"  [nav] M2 clicked 'Toutes les ventes'")
                    page.wait_for_timeout(3_000)
                    if _ventes_tabs_visible(): return True
                    if _wait_sellout(6_000) and _ventes_tabs_visible(): return True
            except Exception as _e:
                print(f"  [nav] M2b err: {_e}")

            # ── M3 : page.goto (rechargement complet — dernier recours) ──────────
            try:
                page.goto("https://datastat.ospharm.org/#!/top/sellout.all",
                          wait_until="domcontentloaded", timeout=25_000)
                _wait_webix(page)
                page.wait_for_timeout(6_000)
                if _ventes_tabs_visible(): return True
                if _wait_sellout(10_000) and _ventes_tabs_visible(): return True
            except Exception as _e:
                print(f"  [nav] M3 goto err: {_e}")

            _reauth_if_needed(page, creds, "ventes")
            _wait_webix(page)
            return _ventes_tabs_visible()

        # 2. Navigation vers la section ventes
        progress("Navigation vers Toutes mes ventes…")
        if not _ventes_tabs_visible():
            # Attendre que le SPA ait fini d'initialiser son router (dashboard + API calls)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            page.wait_for_timeout(5_000)  # 5s pour que Webix Jet enregistre les listeners
            _snap("2_avant_nav")

            if not _goto_sellout():
                _snap("2_nav_echec")
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
                _upload_screenshots()
                raise RuntimeError(
                    f"Nav ventes échoué — url={page.url[:80]} — visible={_nav_visible}"
                )

            print(f"  [nav] url après ventes: {page.url[:80]}")
            _snap("2_apres_nav_ok")

        print(f"  [step3] url={page.url[:80]}")
        # 3. Sélection "Année lissée" — ouvrir le date picker puis cliquer l'option
        progress("Sélection Année lissée…")
        _reauth_if_needed(page, creds, "avant Année lissée")
        try:
            # Cliquer le bouton date picker pour ouvrir le popup
            _date_clicked = page.evaluate('''() => {
                const el = document.querySelector("[view_id='button_date_picker']");
                if (!el) return "no-el";
                const btn = el.querySelector("button") || el;
                btn.click();
                return "clicked:" + btn.tagName;
            }''')
            print(f"  [step3] date picker: {_date_clicked}")
            page.wait_for_timeout(1_500)

            # Cliquer "Année lissée" dans le popup
            _lissee_clicked = page.evaluate('''() => {
                for (const el of document.querySelectorAll("*")) {
                    if (el.children.length > 0) continue;
                    if (!el.textContent.trim().toLowerCase().includes("liss")) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) continue;
                    el.click();
                    return el.textContent.trim();
                }
                return null;
            }''')
            print(f"  [step3] lissée: {_lissee_clicked}")
            page.wait_for_timeout(800)

            # Cliquer "Valider"
            _val = page.evaluate('''() => {
                for (const el of document.querySelectorAll("button")) {
                    if (el.textContent.trim().toLowerCase() === "valider") {
                        el.click(); return "clicked";
                    }
                }
                return "not-found";
            }''')
            print(f"  [step3] valider: {_val}")
            page.wait_for_timeout(3_000)
        except Exception as _e3:
            print(f"  [step3] err: {_e3}")

        # Si on est revenu sur le dashboard, retourner sur ventes
        if not _ventes_tabs_visible():
            print(f"  [warn] Année lissée a redirigé → {page.url[:60]} — retour ventes…")
            _goto_sellout()

        _url_post3 = page.url
        print(f"  [step3-done] url={_url_post3[:80]}")
        period_start, period_end = _extract_period(page)

        # 4. Onglet Produits
        print(f"  [step4] url={page.url[:80]}")
        progress("Sélection onglet Produits…")
        try:
            _prod_clicked = page.evaluate('''() => {
                for (const el of document.querySelectorAll(
                    ".webix_segment_0, .webix_segment_1, .webix_segment_N, button"
                )) {
                    if (el.textContent.trim() !== "Produits") continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) continue;
                    el.click();
                    return el.className.slice(0, 50);
                }
                return null;
            }''')
            print(f"  [step4] Produits: {_prod_clicked}")
            page.wait_for_timeout(3_000)
        except Exception as _e4:
            print(f"  [step4] err: {_e4}")
        _snap("4_apres_produits")
        print(f"  [step4-done] url={page.url[:80]}")

        # 4b. Reauth check — la session peut expirer pendant les étapes précédentes
        if "accounts" in page.url:
            print(f"  [warn] Session expirée avant export — reconnexion…")
            _reauth_if_needed(page, creds, "avant export")
            _wait_webix(page)
            progress("Re-navigation après reconnexion…")
            if not _goto_sellout():
                raise RuntimeError("Re-navigation ventes après reauth échouée")
            # Re-select Année lissée
            try:
                page.evaluate('''() => {
                    const el = document.querySelector("[view_id='button_date_picker']");
                    if (!el) return;
                    (el.querySelector("button") || el).click();
                }''')
                page.wait_for_timeout(1_500)
                page.evaluate('''() => {
                    for (const el of document.querySelectorAll("*")) {
                        if (el.children.length > 0) continue;
                        if (!el.textContent.trim().toLowerCase().includes("liss")) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 2 || r.height < 2) continue;
                        el.click(); return;
                    }
                }''')
                page.wait_for_timeout(800)
                page.evaluate('''() => {
                    for (const el of document.querySelectorAll("button")) {
                        if (el.textContent.trim().toLowerCase() === "valider") {
                            el.click(); return;
                        }
                    }
                }''')
                page.wait_for_timeout(3_000)
                period_start, period_end = _extract_period(page)
            except Exception as _e_ra3:
                print(f"  [reauth] step3 err: {_e_ra3}")
            # Re-select Produits
            try:
                page.evaluate('''() => {
                    for (const el of document.querySelectorAll(
                        ".webix_segment_0, .webix_segment_1, .webix_segment_N, button"
                    )) {
                        if (el.textContent.trim() !== "Produits") continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 2 || r.height < 2) continue;
                        el.click(); return;
                    }
                }''')
                page.wait_for_timeout(3_000)
            except Exception as _e_ra4:
                print(f"  [reauth] step4 err: {_e_ra4}")
            print(f"  [reauth] setup terminé — url={page.url[:80]}")

        # 4c. Attente chargement données — la datatable affiche "Chargement en cours..."
        # après sélection de l'onglet Produits ; l'export ne doit se faire qu'une fois
        # les lignes effectivement présentes dans le DOM (sinon Excel = 1 ligne vide).
        progress("Chargement des données produits…")
        try:
            page.wait_for_function('''() => {
                // "Chargement en cours..." ou tout spinner doit avoir disparu
                for (const el of document.querySelectorAll("*")) {
                    if (el.children.length > 0) continue;
                    const t = el.textContent.trim();
                    if ((t.includes("Chargement") || t.includes("loading") || t.includes("Loading"))
                            && el.getBoundingClientRect().width > 0) {
                        return false;
                    }
                }
                // Et au moins une ligne de données visible dans la datatable
                const rows = document.querySelectorAll(
                    ".webix_dtable .webix_row, .webix_ss_body .webix_column .webix_cell"
                );
                return rows.length > 0;
            }''', timeout=360_000)
            print("  [step4c] données chargées")
        except Exception as _e4c:
            print(f"  [step4c] timeout attente données ({_e4c}) — export quand même")
        _snap("4c_donnees_chargees")

        # 5. Export Excel
        # Deux mécanismes de capture complémentaires :
        # - page.on("download") → blob côté client (webix.toExcel, lien <a download>, etc.)
        # - context.on("response") → réponse HTTP serveur avec Content-Disposition: attachment
        progress("Export Excel…")
        import tempfile as _tf
        _tmp_fd, tmp = _tf.mkstemp(suffix=".xlsx")
        import os as _os; _os.close(_tmp_fd)
        _tmp_dl_fd, _tmp_dl = _tf.mkstemp(suffix=".xlsx")
        _os.close(_tmp_dl_fd)
        _excel_bytes = []

        def _on_download(dl):
            if _excel_bytes:
                return
            try:
                dl.save_as(_tmp_dl)
                with open(_tmp_dl, "rb") as _f:
                    body = _f.read()
                if len(body) > 500:
                    _excel_bytes.append(body)
                    print(f"  [export] download event: '{dl.suggested_filename}' ({len(body):,} bytes)")
                else:
                    print(f"  [export] download event trop petit ({len(body)} bytes) — ignoré")
            except Exception as _de:
                print(f"  [export] download event erreur: {_de}")

        page.on("download", _on_download)

        def _on_response(resp):
            if _excel_bytes:
                return
            try:
                ct = resp.headers.get("content-type", "").lower()
                cd = resp.headers.get("content-disposition", "").lower()
                is_excel = any(x in ct for x in
                    ["excel", "spreadsheet", "openxmlformats", "xls"])
                has_attach = "attachment" in cd and ("xls" in cd or "xlsx" in cd or "excel" in cd)
                if is_excel or has_attach:
                    body = resp.body()
                    if len(body) > 500:
                        _excel_bytes.append(body)
                        print(f"  [export] HTTP response: {len(body):,} bytes — ct={ct[:60]}")
            except Exception as _ie:
                print(f"  [export-intercept] {_ie}")

        context.on("response", _on_response)

        # ── Debug étendu ───────────────────────────────────────────────────────
        try:
            dbg = page.evaluate('''() => {
                function vis(el) { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; }
                const tabs = [...document.querySelectorAll(".webix_item_tab")];
                const tips = [...document.querySelectorAll("[webix_tooltip]")].filter(vis);
                const viewIds = [...document.querySelectorAll("[view_id]")].filter(vis).slice(0, 20).map(el => {
                    const vid = el.getAttribute("view_id");
                    let cfg = {};
                    try {
                        if (typeof webix !== "undefined") {
                            const v = webix.$$(vid);
                            if (v) cfg = { tooltip: v.config?.tooltip, label: v.config?.label, type: v.name };
                        }
                    } catch(e) {}
                    return { vid, tag: el.tagName, cfg };
                });
                return {
                    url: location.href,
                    tabItems: tabs.map(t => t.textContent.trim()).slice(0, 10),
                    tooltipEls: tips.slice(0, 10).map(e => e.getAttribute("webix_tooltip")),
                    viewIds,
                    webix: typeof webix !== "undefined" ? {
                        toExcel: typeof webix.toExcel,
                        dollar:  typeof webix.$$,
                    } : "absent",
                };
            }''')
            print(f"  [export-dbg] tabs={dbg.get('tabItems')} webix={dbg.get('webix')}")
            print(f"  [export-dbg] tooltips={dbg.get('tooltipEls')}")
            print(f"  [export-dbg] viewIds={dbg.get('viewIds')}")
        except Exception as _dbg_err:
            dbg = {}
            print(f"  [export-dbg] skipped ({_dbg_err})")

        # ── Clic bouton export (M0→M5) ─────────────────────────────────────────
        kw_export = ["excel", "export", "exporter", "xls", "format", "fomat", "télécharger"]
        try:
            exported = page.evaluate('''(kw) => {
                function vis(el) {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }
                function strTip(v) {
                    // Certaines configs Webix ont tooltip/label en tant que fonction ou objet
                    const raw = v.config?.tooltip || v.config?.label || "";
                    return (typeof raw === "string" ? raw : "").toLowerCase();
                }

                // M0 (webix.toExcel) supprimé — n'exporte que les lignes en mémoire (pagination serveur)
                // → utiliser uniquement le bouton natif OSPHARM qui génère un export complet

                // M1a : view_id avec tooltip/label contenant un mot-clé export
                if (typeof webix !== "undefined" && typeof webix.$$ === "function") {
                    for (const el of document.querySelectorAll("[view_id]")) {
                        if (!vis(el)) continue;
                        const v = webix.$$(el.getAttribute("view_id"));
                        if (!v) continue;
                        const tip = strTip(v);
                        if (kw.some(k => tip.includes(k))) { el.click(); return "M1a:view_id:" + tip.slice(0,40); }
                    }
                }
                // M2 : boutons à droite de la bande d'onglets ventes
                const tabNames = new Set(["Laboratoires", "Familles", "Produits", "Marques"]);
                let maxRight = 0, bandTop = 0, bandBottom = 0;
                for (const el of document.querySelectorAll(".webix_item_tab")) {
                    const txt = el.textContent.trim();
                    if (!tabNames.has(txt)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 4 || r.height < 4) continue;
                    if (r.right > maxRight) { maxRight = r.right; bandTop = r.top; bandBottom = r.bottom; }
                }
                if (maxRight > 0) {
                    const midY = (bandTop + bandBottom) / 2;
                    const halfH = (bandBottom - bandTop) / 2 + 10;
                    const cands = [];
                    for (const el of document.querySelectorAll("[view_id], button, .webix_el_icon, .webix_el_button")) {
                        const r = el.getBoundingClientRect();
                        if (r.left <= maxRight + 2 || r.top > midY + halfH || r.bottom < midY - halfH) continue;
                        if (r.width < 8 || r.height < 8 || r.width > 200 || r.height > 100) continue;
                        cands.push(el);
                    }
                    cands.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                    if (cands.length) {
                        const target = cands[0].querySelector("button,.webix_template") || cands[0];
                        target.click();
                        return "M2:pos:" + cands[0].tagName + "@x" + Math.round(cands[0].getBoundingClientRect().left);
                    }
                    // fallback elementFromPoint
                    for (const xOff of [50, 80, 110, 140]) {
                        const el = document.elementFromPoint(window.innerWidth - xOff, midY);
                        if (el && vis(el) && el.getBoundingClientRect().width < 200) {
                            el.click(); return "M2:efp@" + xOff + ":" + el.tagName;
                        }
                    }
                }
                // M3 : webix_tooltip
                for (const el of document.querySelectorAll("[webix_tooltip]")) {
                    if (!vis(el)) continue;
                    const tip = (el.getAttribute("webix_tooltip") || "").toLowerCase();
                    if (kw.some(k => tip.includes(k))) {
                        (el.querySelector("button") || el).click();
                        return "M3:tooltip:" + tip.slice(0,40);
                    }
                }
                // M4 : texte/title/aria
                for (const el of document.querySelectorAll("button,a,[role=button],.webix_el_button")) {
                    if (!vis(el)) continue;
                    const hay = (el.textContent + " " + (el.title||"") + " " + (el.getAttribute("aria-label")||"")).toLowerCase();
                    if (kw.some(k => hay.includes(k))) { el.click(); return "M4:kw:" + hay.slice(0,40); }
                }
                // M5 : icône de téléchargement par forme/position (dernier recours)
                const allVis = [...document.querySelectorAll("button,.webix_el_icon,[role=button]")].filter(vis);
                for (const el of allVis) {
                    const r = el.getBoundingClientRect();
                    if (r.right > window.innerWidth * 0.6 && r.top < window.innerHeight * 0.3) {
                        const inner = el.innerHTML.toLowerCase();
                        if (inner.includes("download") || inner.includes("arrow") || inner.includes("↓")) {
                            el.click(); return "M5:icon:" + el.tagName + "@" + Math.round(r.left);
                        }
                    }
                }
                return false;
            }''', kw_export)
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

        # ── Poll Valider (popup OSPHARM, jusqu'à 25s) ──────────────────────────
        print(f"  [export] bouton cliqué ({exported}), poll Valider…")
        _val_clicked = False
        for _attempt in range(10):
            page.wait_for_timeout(2_500)
            if _excel_bytes:
                print(f"  [export] fichier reçu avant/pendant Valider — ok")
                break
            try:
                _loc = page.locator(
                    ".webix_window button, .webix_popup button, .webix_modal button,"
                    " .webix_win_body button, button"
                ).filter(has_text="Valider").first
                if _loc.is_visible(timeout=400):
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
            print(f"  [export] Valider non trouvé après 25s")

        # ── Attente réception fichier Excel jusqu'à 3 min (gros fichiers > 20 MB) ──
        progress("Attente du fichier Excel…")
        for _w in range(72):
            if _excel_bytes:
                break
            page.wait_for_timeout(2_500)
            if (_w + 1) % 4 == 0:
                print(f"  [export] attente... {(_w+1)*2.5:.0f}s")

        if not _excel_bytes:
            browser.close()
            raise RuntimeError(f"Export Excel : aucun fichier reçu en 3 min. Debug: {dbg}")

        print(f"  [export] fichier capturé ({len(_excel_bytes[0]):,} bytes) — fermeture navigateur")
        with open(tmp, "wb") as f:
            f.write(_excel_bytes[0])

        _upload_screenshots()
        browser.close()

    # Lecture Excel
    import re as _re
    def _strip_html(v):
        if isinstance(v, str) and "<" in v:
            return _re.sub(r"<[^>]+>", "", v).strip()
        return v

    progress("Lecture du fichier Excel…")
    wb = openpyxl.load_workbook(tmp, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = [str(h or "").strip() for h in next(rows_iter)]
    rows = []
    for row in rows_iter:
        if any(v is not None for v in row):
            rows.append({h: _strip_html(v) for h, v in zip(headers, row)})
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
        rows, file_url, ps, pe = run_ospharm(creds, progress, user_id=USER_ID)
        stored_rows = _compact_osp_rows(rows)
        print(f"  [compact] {len(rows)} → {len(stored_rows)} lignes ({len(stored_rows)/max(len(rows),1)*100:.0f}%)")
        _update_job("done", f"{len(stored_rows)} lignes extraites", stored_rows, blocking=True,
                    period_start=ps, period_end=pe, file_url=file_url)
        print(f"\n✅  {len(stored_rows)} lignes OSPHARM sauvegardées dans Supabase. ({time.time()-t0:.1f}s total)")
    except Exception as e:
        _update_job("error", error=str(e), blocking=True)
        print(f"\n❌  {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
