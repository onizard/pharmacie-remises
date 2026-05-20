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
SESSION_DIR  = "/tmp"
SESSION_TTL  = 10 * 3600  # 10h


def _session_path(uid: str) -> str:
    safe = (uid or "anon").replace("-", "")[:20]
    return f"{SESSION_DIR}/ospharm_sess_{safe}.json"


def _session_fresh(path: str) -> bool:
    import os as _os, time as _time
    try:
        return (_time.time() - _os.path.getmtime(path)) < SESSION_TTL
    except OSError:
        return False


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


def _update_job(status: str, message: str = "", rows=None, error: str = "",
                blocking: bool = False, period_start: str = "", period_end: str = "",
                file_url: str = ""):
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
    """Convertit les lignes OSPHARM brutes en {cip13, qty, libelle, puht}.
    puht = montant_catalogue_total / qty (prix unitaire brut).
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
    # Colonne montant catalogue (total brut = qty × PUHT) — priorité catalogue > HT générique
    _cat_candidates = [
        next((k for k in keys if _n(k) in ("montantcatalogue", "catotalht", "montantpfht", "totalcatalogue", "catht")), None),
        next((k for k in keys if "catalogue" in _n(k) and any(x in _n(k) for x in ("montant", "ca", "total", "ht"))), None),
        next((k for k in keys if "pfht" in _n(k) and "montant" in _n(k)), None),
        next((k for k in keys if _n(k) in ("montantht", "caht")), None),
        next((k for k in keys if "montant" in _n(k) and "ht" in _n(k)
              and "n1" not in _n(k) and "evo" not in _n(k)), None),
    ]
    cat_k = next((c for c in _cat_candidates if c), None)
    print(f"  [compact] colonnes: cip={cip_k!r} qty={qty_k!r} lib={lib_k!r} cat={cat_k!r}")

    if not cip_k or not qty_k:
        return rows

    compact: dict = {}
    for r in rows:
        raw = _re.sub(r"\D", "", str(r.get(cip_k) or ""))
        cip13 = raw if len(raw) == 13 else ("340000" + raw if len(raw) == 7 else None)
        try:
            qty = float(str(r.get(qty_k) or 0).replace(",", "."))
        except (ValueError, TypeError):
            qty = 0.0
        if not cip13 or qty <= 0:
            continue
        try:
            cat_val = float(str(r.get(cat_k) or 0).replace(",", ".")) if cat_k else 0.0
        except (ValueError, TypeError):
            cat_val = 0.0
        if cip13 not in compact:
            compact[cip13] = {
                "cip13":      cip13,
                "qty":        0.0,
                "libelle":    str(r.get(lib_k) or "").strip() if lib_k else "",
                "_cat_total": 0.0,
            }
        compact[cip13]["qty"]        += qty
        compact[cip13]["_cat_total"] += cat_val

    result = []
    for entry in compact.values():
        cat_total = entry.pop("_cat_total", 0.0)
        if cat_total > 0 and entry["qty"] > 0:
            entry["puht"] = round(cat_total / entry["qty"], 4)
        result.append(entry)
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


def run_ospharm(creds: dict, progress, user_id: str = "") -> tuple:
    import tempfile, openpyxl, re as _re, os as _os

    # ── Screenshots diagnostic ─────────────────────────────────────────────────
    _screenshots: list[tuple[str, bytes]] = []

    def _snap(label: str):
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

    def _strip_html(v):
        if isinstance(v, str) and "<" in v:
            return _re.sub(r"<[^>]+>", "", v).strip()
        return v

    sess_path = _session_path(user_id)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx_kwargs = dict(accept_downloads=True, viewport={"width": 1440, "height": 900})
        if _session_fresh(sess_path):
            ctx_kwargs["storage_state"] = sess_path
            progress("Session OSPHARM restaurée — connexion rapide…")
            print(f"  [session] chargement {sess_path}")
        else:
            progress("Connexion OSPHARM…")

        context = browser.new_context(**ctx_kwargs)
        page    = context.new_page()

        # 1. Login (sauté si session valide)
        page.goto(OSPHARM_URL, wait_until="networkidle", timeout=30_000)

        if "accounts" in page.url or "login" in page.url:
            progress("Authentification OSPHARM…")
            _login(page, creds)
            # Sauvegarder la session pour les prochains runs
            try:
                context.storage_state(path=sess_path)
                print(f"  [session] sauvegardée → {sess_path}")
            except Exception as _se:
                print(f"  [session] save err: {_se}")
        else:
            print("  [session] session restaurée ✓ — login ignoré")
            # Rafraîchir le TTL
            try:
                context.storage_state(path=sess_path)
            except Exception:
                pass

        if "datastat.ospharm.org" not in page.url or "accounts" in page.url:
            raise RuntimeError("Identifiants OSPHARM incorrects")

        progress("Connecté — chargement…")
        _wait_webix(page)
        _snap("1_apres_login")

        # ── helper : naviguer vers la section ventes ───────────────────────────
        def _ventes_tabs_visible():
            try:
                if any(x in page.url for x in ["sellout", "ventes.all", "mysellout"]):
                    return True
            except Exception:
                pass
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
                    page.wait_for_timeout(2_000)
                    if _ventes_tabs_visible(): return True
                    if _wait_sellout(4_000) and _ventes_tabs_visible(): return True
            except Exception as _e:
                print(f"  [nav] M0 err: {_e}")

            try:
                _r1 = page.evaluate("""() => {
                    const base = location.href.split('#')[0];
                    location.href = base + '#!/top/sellout.all';
                    return location.href;
                }""")
                print(f"  [nav] M1 href: {str(_r1)[:80]}")
                page.wait_for_timeout(2_000)
                if _ventes_tabs_visible(): return True
                if _wait_sellout(6_000) and _ventes_tabs_visible(): return True
            except Exception as _e:
                print(f"  [nav] M1 err: {_e}")

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
                    page.wait_for_timeout(1_500)
                    if _ventes_tabs_visible(): return True
                    if _wait_sellout(5_000) and _ventes_tabs_visible(): return True
            except Exception as _e:
                print(f"  [nav] M2b err: {_e}")

            try:
                page.goto("https://datastat.ospharm.org/#!/top/sellout.all",
                          wait_until="domcontentloaded", timeout=25_000)
                _wait_webix(page)
                page.wait_for_timeout(3_000)
                if _ventes_tabs_visible(): return True
                if _wait_sellout(8_000) and _ventes_tabs_visible(): return True
            except Exception as _e:
                print(f"  [nav] M3 goto err: {_e}")

            _reauth_if_needed(page, creds, "ventes")
            _wait_webix(page)
            return _ventes_tabs_visible()

        # 2. Navigation initiale vers la section ventes
        progress("Navigation vers Toutes mes ventes…")
        if not _ventes_tabs_visible():
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            page.wait_for_timeout(2_000)
            _snap("2_avant_nav")

            if not _goto_sellout():
                _snap("2_nav_echec")
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

        # ── Fonction interne : sélectionne une période et exporte l'Excel ────────
        def _run_export_pass(period_kw: str, year_tag: int, label: str) -> tuple:
            """Sélectionne la période `period_kw`, onglet Produits, exporte Excel.
            Retourne (raw_rows_with_year, period_start, period_end, file_url).
            """
            # a. S'assurer qu'on est sur la page ventes
            if not _ventes_tabs_visible():
                progress(f"Re-navigation ventes ({label})…")
                if not _goto_sellout():
                    _snap(f"nav_echec_{year_tag}")
                    _upload_screenshots()
                    browser.close()
                    raise RuntimeError(f"Nav ventes échouée pour {label}")
            _reauth_if_needed(page, creds, f"avant {label}")

            # b. Sélection de la période dans le date picker
            progress(f"Sélection {label}…")
            kw_lower = period_kw.lower()
            try:
                _dc = page.evaluate('''() => {
                    const el = document.querySelector("[view_id='button_date_picker']");
                    if (!el) return "no-el";
                    const btn = el.querySelector("button") || el;
                    btn.click();
                    return "clicked:" + btn.tagName;
                }''')
                print(f"  [{label}] date picker: {_dc}")
                page.wait_for_timeout(800)

                _pc = page.evaluate(f'''() => {{
                    const kw = {repr(kw_lower)};
                    for (const el of document.querySelectorAll("*")) {{
                        if (el.children.length > 0) continue;
                        if (!el.textContent.trim().toLowerCase().includes(kw)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 2 || r.height < 2) continue;
                        el.click();
                        return el.textContent.trim();
                    }}
                    return null;
                }}''')
                print(f"  [{label}] option sélectionnée: {_pc}")
                page.wait_for_timeout(400)

                _val = page.evaluate('''() => {
                    for (const el of document.querySelectorAll("button")) {
                        if (el.textContent.trim().toLowerCase() === "valider") {
                            el.click(); return "clicked";
                        }
                    }
                    return "not-found";
                }''')
                print(f"  [{label}] valider: {_val}")
                page.wait_for_timeout(1_500)
            except Exception as _e3:
                print(f"  [{label}] step3 err: {_e3}")

            if not _ventes_tabs_visible():
                print(f"  [warn] {label} a redirigé → retour ventes…")
                _goto_sellout()

            ps, pe = _extract_period(page)
            print(f"  [{label}] période: {ps} → {pe}")
            _snap(f"3_periode_{year_tag}")

            # c. Onglet Produits
            progress(f"Onglet Produits ({label})…")
            try:
                _pc2 = page.evaluate('''() => {
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
                print(f"  [{label}] Produits: {_pc2}")
                page.wait_for_timeout(1_500)
            except Exception as _e4:
                print(f"  [{label}] step4 err: {_e4}")
            _snap(f"4_produits_{year_tag}")

            # d. Reauth check — session peut expirer entre les passes
            if "accounts" in page.url:
                print(f"  [warn] Session expirée avant export ({label}) — reconnexion…")
                _reauth_if_needed(page, creds, f"avant export {label}")
                _wait_webix(page)
                progress(f"Re-navigation après reconnexion ({label})…")
                if not _goto_sellout():
                    _upload_screenshots()
                    browser.close()
                    raise RuntimeError(f"Re-navigation après reauth échouée ({label})")
                try:
                    page.evaluate('''() => {
                        const el = document.querySelector("[view_id='button_date_picker']");
                        if (!el) return;
                        (el.querySelector("button") || el).click();
                    }''')
                    page.wait_for_timeout(1_500)
                    page.evaluate(f'''() => {{
                        const kw = {repr(kw_lower)};
                        for (const el of document.querySelectorAll("*")) {{
                            if (el.children.length > 0) continue;
                            if (!el.textContent.trim().toLowerCase().includes(kw)) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width < 2 || r.height < 2) continue;
                            el.click(); return;
                        }}
                    }}''')
                    page.wait_for_timeout(800)
                    page.evaluate('''() => {
                        for (const el of document.querySelectorAll("button")) {
                            if (el.textContent.trim().toLowerCase() === "valider") {
                                el.click(); return;
                            }
                        }
                    }''')
                    page.wait_for_timeout(3_000)
                    ps, pe = _extract_period(page)
                except Exception as _e_ra3:
                    print(f"  [reauth] {label} step3 err: {_e_ra3}")
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
                    print(f"  [reauth] {label} step4 err: {_e_ra4}")
                print(f"  [reauth] setup terminé — url={page.url[:80]}")

            # e. Attente chargement données
            progress(f"Chargement données ({label})…")
            try:
                page.wait_for_function('''() => {
                    for (const el of document.querySelectorAll("*")) {
                        if (el.children.length > 0) continue;
                        const t = el.textContent.trim();
                        if ((t.includes("Chargement") || t.includes("loading") || t.includes("Loading"))
                                && el.getBoundingClientRect().width > 0) {
                            return false;
                        }
                    }
                    const rows = document.querySelectorAll(
                        ".webix_dtable .webix_row, .webix_ss_body .webix_column .webix_cell"
                    );
                    return rows.length > 0;
                }''', timeout=360_000)
                print(f"  [{label}] données chargées")
            except Exception as _e4c:
                print(f"  [{label}] timeout chargement ({_e4c}) — export quand même")
            _snap(f"4c_donnees_{year_tag}")

            # f. Export Excel — expect_download + fallback webix.toExcel direct
            progress(f"Export Excel ({label})…")
            _tmp_dl_fd, _tmp_dl = tempfile.mkstemp(suffix=".xlsx")
            _os.close(_tmp_dl_fd)

            # Debug étendu
            try:
                dbg = page.evaluate('''() => {
                    function vis(el) { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; }
                    const tabs = [...document.querySelectorAll(".webix_item_tab,.webix_segment_0,.webix_segment_1,.webix_segment_N")];
                    const tips = [...document.querySelectorAll("[webix_tooltip]")].filter(vis);
                    const datatables = [...document.querySelectorAll("[view_id]")].filter(el => {
                        try {
                            const v = webix.$$(el.getAttribute("view_id"));
                            return v && (v.name === "datatable" || v.name === "treetable");
                        } catch(e) { return false; }
                    }).map(el => {
                        const vid = el.getAttribute("view_id");
                        const v = webix.$$(vid);
                        return { vid, count: v.count ? v.count() : "?" };
                    });
                    return {
                        url: location.href,
                        tabItems: tabs.map(t => t.textContent.trim()).slice(0, 15),
                        tooltipEls: tips.slice(0, 10).map(e => e.getAttribute("webix_tooltip")),
                        datatables,
                        webix: typeof webix !== "undefined" ? {
                            toExcel: typeof webix.toExcel,
                            dollar:  typeof webix.$$,
                        } : "absent",
                    };
                }''')
                print(f"  [export-dbg-{year_tag}] tabs={dbg.get('tabItems')} webix={dbg.get('webix')}")
                print(f"  [export-dbg-{year_tag}] datatables={dbg.get('datatables')}")
            except Exception as _dbg_err:
                dbg = {}
                print(f"  [export-dbg-{year_tag}] skipped ({_dbg_err})")

            kw_export = ["excel", "export", "exporter", "xls", "format", "fomat", "télécharger"]
            try:
                exported = page.evaluate('''(kw) => {
                    function vis(el) {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }
                    function strTip(v) {
                        const raw = v.config?.tooltip || v.config?.label || "";
                        return (typeof raw === "string" ? raw : "").toLowerCase();
                    }

                    // M1b : bouton Webix dont l'icon (config) contient un mot-clé export
                    if (typeof webix !== "undefined" && typeof webix.$$ === "function") {
                        for (const el of document.querySelectorAll("[view_id]")) {
                            if (!vis(el)) continue;
                            const vid = el.getAttribute("view_id");
                            const v = webix.$$(vid);
                            if (!v || v.name !== "button") continue;
                            const icon = (typeof v.config?.icon === "string" ? v.config.icon : "").toLowerCase();
                            const cls  = (el.className || "").toLowerCase();
                            const html = el.innerHTML.toLowerCase();
                            const isExport = ["excel", "xls", "export", "exporter", "download", "télécharger", "file-"]
                                .some(k => icon.includes(k) || html.includes(k));
                            if (isExport) { el.click(); return "M1b:icon:" + (icon || cls).slice(0, 50); }
                        }
                    }

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
                    for (const el of document.querySelectorAll(".webix_item_tab,.webix_segment_0,.webix_segment_1,.webix_segment_N")) {
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
                    print(f"  [export-{year_tag}] context destroyed au clic — download en route")
                else:
                    _snap(f"export_evaluate_error_{year_tag}")
                    _upload_screenshots()
                    browser.close()
                    raise RuntimeError(f"Export Excel ({label}) : evaluate échoué : {_eval_err}")

            if not exported:
                _snap(f"export_bouton_introuvable_{year_tag}")
                _upload_screenshots()
                browser.close()
                raise RuntimeError(f"Aucun bouton export trouvé ({label}) — debug={dbg}")

            print(f"  [export-{year_tag}] bouton cliqué ({exported}), attente dialog Valider…")

            # Attendre que le bouton Valider soit visible dans la popup (max 20s)
            _val_loc = None
            for _att in range(10):
                page.wait_for_timeout(2_000)
                try:
                    _loc = page.locator(
                        ".webix_window button, .webix_popup button, .webix_modal button,"
                        " .webix_win_body button"
                    ).filter(has_text="Valider").first
                    if _loc.is_visible(timeout=300):
                        _val_loc = _loc
                        print(f"  [export-{year_tag}] Valider visible après {(_att+1)*2}s")
                        break
                except Exception:
                    pass

            _excel_bytes = None

            # Méthode 1 : expect_download sur le clic Valider
            if _val_loc:
                try:
                    progress(f"Attente fichier Excel ({label})…")
                    with page.expect_download(timeout=300_000) as _dl_info:
                        _val_loc.click(timeout=5_000)
                    _dl = _dl_info.value
                    _dl.save_as(_tmp_dl)
                    with open(_tmp_dl, "rb") as _f:
                        _body = _f.read()
                    if len(_body) > 500:
                        _excel_bytes = _body
                        print(f"  [export-{year_tag}] Valider→download: '{_dl.suggested_filename}' ({len(_body):,} bytes)")
                    else:
                        print(f"  [export-{year_tag}] Valider→download trop petit: {len(_body)} bytes")
                except Exception as _edd:
                    print(f"  [export-{year_tag}] expect_download(Valider) err: {_edd}")
            else:
                print(f"  [export-{year_tag}] Valider non trouvé après 20s — fallback direct")

            # Méthode 2 : webix.toExcel() direct sur la datatable (bypass dialog)
            if not _excel_bytes:
                print(f"  [export-{year_tag}] fallback: webix.toExcel() direct…")
                try:
                    with page.expect_download(timeout=60_000) as _dl_info2:
                        _res2 = page.evaluate('''() => {
                            if (typeof webix === "undefined" || typeof webix.toExcel !== "function")
                                return "no-webix-toExcel";
                            let best = null, bestCount = 0;
                            for (const el of document.querySelectorAll("[view_id]")) {
                                const vid = el.getAttribute("view_id");
                                try {
                                    const v = webix.$$(vid);
                                    if (v && (v.name === "datatable" || v.name === "treetable")) {
                                        const c = v.count ? v.count() : 0;
                                        if (c > bestCount) { best = v; bestCount = c; }
                                    }
                                } catch(e) {}
                            }
                            if (!best) return "no-datatable";
                            webix.toExcel(best, {filename: "ospharm_export"});
                            return "toExcel:" + bestCount;
                        }''')
                    print(f"  [export-{year_tag}] webix.toExcel: {_res2}")
                    _dl2 = _dl_info2.value
                    _dl2.save_as(_tmp_dl)
                    with open(_tmp_dl, "rb") as _f:
                        _body2 = _f.read()
                    if len(_body2) > 500:
                        _excel_bytes = _body2
                        print(f"  [export-{year_tag}] webix.toExcel capturé: {len(_body2):,} bytes")
                    else:
                        print(f"  [export-{year_tag}] webix.toExcel trop petit: {len(_body2)} bytes")
                except Exception as _edd2:
                    print(f"  [export-{year_tag}] webix.toExcel err: {_edd2}")

            if not _excel_bytes:
                _snap(f"export_timeout_{year_tag}")
                _upload_screenshots()
                browser.close()
                raise RuntimeError(f"Export Excel ({label}) : aucun fichier reçu. Debug: {dbg}")

            print(f"  [export-{year_tag}] capturé ({len(_excel_bytes):,} bytes) ✓")

            # Upload fichier brut vers Supabase Storage
            _file_url = ""
            if user_id:
                try:
                    from supabase_client import upload_file_sync, get_signed_url_sync
                    import datetime
                    date_str = datetime.date.today().strftime("%Y-%m-%d")
                    filename = f"ospharm_{year_tag}_{date_str}.xlsx"
                    path = upload_file_sync(user_id, "ospharm", filename, _excel_bytes,
                                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    _file_url = get_signed_url_sync(path)
                    print(f"  [storage-{year_tag}] {filename}")
                except Exception as _ue:
                    print(f"  [storage-{year_tag}] ERREUR: {_ue}")

            # Lecture Excel
            _tmp_fd2, _tmp2 = tempfile.mkstemp(suffix=".xlsx")
            _os.close(_tmp_fd2)
            with open(_tmp2, "wb") as f:
                f.write(_excel_bytes)

            progress(f"Lecture Excel ({label})…")
            wb = openpyxl.load_workbook(_tmp2, read_only=True, data_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            headers = [str(h or "").strip() for h in next(rows_iter)]
            raw_rows = []
            for row in rows_iter:
                if any(v is not None for v in row):
                    raw_rows.append({h: _strip_html(v) for h, v in zip(headers, row)})
            wb.close()

            print(f"  [{label}] {len(raw_rows)} lignes, période {ps}→{pe}")
            return raw_rows, ps, pe, _file_url

        # 3. Export : Année lissée
        progress("Export Année lissée…")
        all_rows, ps, pe, file_url = _run_export_pass("lissée", 0, "Année lissée")

        _upload_screenshots()
        browser.close()

    return all_rows, file_url, ps, pe


# ── Sync PUHT → references_pharmacie ─────────────────────────────────────────

def _sync_puht_supabase(puht_dict: dict):
    """Met à jour references_pharmacie.puht pour chaque CIP depuis OSPHARM.
    Requêtes PATCH parallèles par lots de 50.
    puht_dict: {cip13: puht_float}
    """
    items = list(puht_dict.items())
    if not items:
        return
    errors = []
    done_count = [0]
    lock = threading.Lock()

    def _patch(cip13, puht):
        url = f"{SUPA_URL}/rest/v1/references_pharmacie?cip13=eq.{cip13}"
        body = json.dumps({"puht": puht}).encode()
        req = urllib.request.Request(url, data=body, method="PATCH", headers={
            "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal",
        })
        try:
            with urllib.request.urlopen(req, timeout=15): pass
        except Exception as e:
            with lock:
                errors.append(f"{cip13}: {e}")
        with lock:
            done_count[0] += 1
            if done_count[0] % 500 == 0:
                print(f"  [puht-sync] {done_count[0]}/{len(items)}…")

    BATCH = 50
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        threads = [threading.Thread(target=_patch, args=(c, p), daemon=True) for c, p in batch]
        for t in threads: t.start()
        for t in threads: t.join()

    print(f"  [puht-sync] ✅ {len(items)} PUHT mis à jour ({len(errors)} erreurs)")
    if errors[:3]:
        print(f"  [puht-sync] exemples erreurs: {errors[:3]}")


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
        print(f"  [compact] {len(rows)} → {len(stored_rows)} lignes")
        _update_job(
            "done",
            f"{len(stored_rows)} lignes extraites",
            stored_rows,
            blocking=True,
            period_start=ps,
            period_end=pe,
            file_url=file_url,
        )
        print(f"\n✅  {len(stored_rows)} lignes OSPHARM sauvegardées. ({time.time()-t0:.1f}s total)")

        # Mise à jour PUHT Supabase depuis OSPHARM
        puht_dict: dict = {r["cip13"]: r["puht"] for r in stored_rows if r.get("puht")}
        if puht_dict:
            progress(f"Mise à jour PUHT Supabase ({len(puht_dict)} CIPs)…")
            _sync_puht_supabase(puht_dict)
    except Exception as e:
        _update_job("error", error=str(e), blocking=True)
        print(f"\n❌  {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
