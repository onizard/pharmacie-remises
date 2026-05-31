"""
run_job_ospharm.py — Exécuté par GitHub Actions (workflow scraper_ospharm.yml).

Variables d'environnement requises :
    USER_ID               Supabase user UUID
    SUPABASE_SERVICE_KEY  Clé de service Supabase (GitHub Secret)
"""

import calendar
import datetime
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
                blocking: bool = False, month_meta=None, month_stats=None,
                period_start: str = "", period_end: str = ""):
    def _do():
        try:
            state = _supa_get_state()
            job = {
                "status":      status,
                "message":     message,
                "rows":        rows or [],
                "total":       len(rows) if rows else 0,
                "error":       error,
                "month_meta":  month_meta or [],
                "month_stats": month_stats or {},
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


def _get_connectors_col() -> dict:
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}&select=connectors&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    return (rows[0].get("connectors") or {}) if rows else {}


def _get_creds() -> dict:
    # Priorité 1 : colonne connectors (atomique via upsert_connector RPC, toujours à jour)
    try:
        conns = _get_connectors_col()
        osp   = conns.get("ospharm", {})
        if osp.get("user") and osp.get("pass"):
            return {"user": osp["user"], "pass": osp["pass"]}
    except Exception:
        pass

    # Priorité 2 : state_json.connectors (fallback — peut être périmé si saveCloudState a timeout)
    state  = _supa_get_state()
    osp    = state.get("connectors", {}).get("ospharm", {})
    user   = osp.get("user", "")
    passwd = osp.get("pass", "")
    if not user or not passwd:
        raise ValueError("Identifiants OSPHARM manquants dans Supabase.")
    return {"user": user, "pass": passwd}


# ── Supabase références & defaults ────────────────────────────────────────────

def _query_refs(cip_list: list) -> dict:
    """Retourne {cip13: {labo, rsf_pct, puht}} pour chaque CIP."""
    if not cip_list:
        return {}
    result = {}
    batch_size = 80  # éviter URLs trop longues
    for i in range(0, len(cip_list), batch_size):
        batch = cip_list[i:i + batch_size]
        cips_param = "(" + ",".join(batch) + ")"
        url = (f"{SUPA_URL}/rest/v1/references_pharmacie"
               f"?cip13=in.{cips_param}&select=cip13,labo,rsf_pct,puht&limit=10000")
        req = urllib.request.Request(url, headers={
            "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                for row in json.loads(r.read()):
                    result[row["cip13"]] = {
                        "labo":    row["labo"],
                        "rsf_pct": row.get("rsf_pct"),
                        "puht":    row.get("puht"),
                    }
        except Exception as e:
            print(f"  [refs-query] batch err: {e}")
    return result


def _query_rsf_defaults() -> dict:
    """Retourne {lab: {rsf_pct_str: {remise2, remise3}}}."""
    url = f"{SUPA_URL}/rest/v1/rsf_defaults?select=lab,rsf_pct,remise2,remise3&limit=10000"
    req = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.loads(r.read())
        defs: dict = {}
        for row in rows:
            if row["lab"] not in defs:
                defs[row["lab"]] = {}
            defs[row["lab"]][str(row["rsf_pct"])] = {
                "remise2": row.get("remise2") or "",
                "remise3": row.get("remise3") or "",
            }
        return defs
    except Exception as e:
        print(f"  [rsf-defaults-query] err: {e}")
        return {}


def _build_month_stats(all_rows: list, refs_by_cip: dict, rsf_defs: dict) -> dict:
    """Agrège par (year, month, labo) → month_stats dict pour le frontend."""
    by_key: dict = {}
    for row in all_rows:
        cip = row.get("cip13")
        ref = refs_by_cip.get(cip)
        if not ref:
            continue
        labo = ref["labo"] or ""
        try:
            rsf_f = float(ref.get("rsf_pct") or 0)
        except (TypeError, ValueError):
            rsf_f = 0.0
        # remise2 depuis rsf_defaults
        remise2 = 0.0
        lab_defs = rsf_defs.get(labo, {})
        d = lab_defs.get(str(int(rsf_f))) or lab_defs.get(str(rsf_f))
        if d and d.get("remise2"):
            try:
                remise2 = float(d["remise2"])
            except (TypeError, ValueError):
                pass

        qty  = float(row.get("qty") or 0)
        puht = float(row.get("puht") or 0) or float(ref.get("puht") or 0)
        ca   = qty * puht
        rsf_abs = abs(rsf_f)
        remise_ca = ca * (rsf_abs + remise2) / 100.0

        k = (row.get("year", 0), row.get("month", 0), labo)
        if k not in by_key:
            by_key[k] = {"qty": 0.0, "ca": 0.0, "remise": 0.0}
        by_key[k]["qty"]    += qty
        by_key[k]["ca"]     += ca
        by_key[k]["remise"] += remise_ca

    result: dict = {}
    for (year, month, labo), v in by_key.items():
        mk = f"{year}-{month:02d}"
        if mk not in result:
            result[mk] = []
        ca_b = round(v["ca"], 2)
        rem  = round(v["remise"], 2)
        pond = round(v["remise"] / v["ca"] * 100, 1) if v["ca"] > 0 else 0.0
        result[mk].append({
            "labo":         labo,
            "qty":          int(v["qty"]),
            "ca_brut":      ca_b,
            "pond_pct":     pond,
            "remise_totale": rem,
            "pa_net":       round(ca_b - rem, 2),
        })
    for mk in result:
        result[mk].sort(key=lambda x: x["ca_brut"], reverse=True)
    return result


# ── Row compaction ────────────────────────────────────────────────────────────

def _compact_month_rows(rows: list[dict]) -> list[dict]:
    """Convertit les lignes OSPHARM brutes en {cip13, qty, puht}.
    (pas de libelle pour économiser la place en BDD)
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
    _cat_candidates = [
        next((k for k in keys if _n(k) in ("montantcatalogue", "catotalht", "montantpfht")), None),
        next((k for k in keys if "catalogue" in _n(k) and any(x in _n(k) for x in ("montant","ca","total","ht"))), None),
        next((k for k in keys if _n(k) in ("montantht", "caht")), None),
        next((k for k in keys if "montant" in _n(k) and "ht" in _n(k)
              and "n1" not in _n(k) and "evo" not in _n(k)), None),
    ]
    cat_k = next((c for c in _cat_candidates if c), None)
    print(f"  [compact] cip={cip_k!r} qty={qty_k!r} cat={cat_k!r}")

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
            compact[cip13] = {"cip13": cip13, "qty": 0.0, "_cat_total": 0.0}
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
    """Extrait la plage de dates affichée. Inference en fallback."""
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
                return ps, pe
    except Exception:
        pass
    today = _dt.date.today()
    return today.strftime("%Y-%m-01"), today.strftime("%Y-%m-%d")


def _wait_webix(page, timeout=20_000):
    try:
        page.wait_for_function("() => typeof webix !== 'undefined'", timeout=timeout)
    except Exception:
        page.wait_for_timeout(3_000)


def _login(page, creds):
    page.locator("input[type='email'],input[name='username'],input[name='email']").first.fill(
        creds["user"], timeout=15_000)
    page.locator("input[type='password'],input[name='password']").first.fill(
        creds["pass"], timeout=5_000)
    page.locator("button[type='submit'],input[type='submit']").first.click(timeout=5_000)
    try:
        page.wait_for_url("*datastat.ospharm.org*", timeout=45_000)
    except PWTimeout:
        pass
    if "datastat.ospharm.org" not in page.url or "accounts" in page.url:
        raise RuntimeError("Identifiants OSPHARM incorrects")
    _wait_webix(page)


def _reauth_if_needed(page, creds, label=""):
    if "accounts" not in page.url:
        return
    print(f"  [warn] Re-auth OSPHARM{' (' + label + ')' if label else ''}")
    _login(page, creds)


def run_ospharm(creds: dict, progress, user_id: str = "") -> tuple:
    import tempfile, openpyxl, re as _re, os as _os

    _screenshots: list[tuple[str, bytes]] = []

    def _snap(label: str):
        try:
            data = page.screenshot(full_page=False)
            _screenshots.append((label, data))
            print(f"  [snap] {label} ({len(data):,} bytes)")
        except Exception as _se:
            print(f"  [snap] {label} ERR: {_se}")

    def _upload_screenshots():
        if not user_id or not _screenshots:
            return
        try:
            from supabase_client import upload_file_sync
            ts = datetime.datetime.now().strftime("%H%M%S")
            for label, data in _screenshots:
                safe = label.replace(" ", "_").replace("/", "-")
                upload_file_sync(user_id, "ospharm_debug", f"{ts}_{safe}.png", data, "image/png")
        except Exception as _ue:
            print(f"  [snap-upload] ERR: {_ue}")

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
            progress("Session OSPHARM restaurée…")
        else:
            progress("Connexion OSPHARM…")

        context = browser.new_context(**ctx_kwargs)
        page    = context.new_page()

        # ── Monitoring XHR/fetch pour diagnostic date-filter ──────────────────
        _xhr_log: list[str] = []
        page.on("request", lambda r: _xhr_log.append(r.url)
                if r.resource_type in ("xhr", "fetch") else None)

        page.goto(OSPHARM_URL, wait_until="networkidle", timeout=30_000)
        if "accounts" in page.url or "login" in page.url:
            progress("Authentification OSPHARM…")
            _login(page, creds)
            try:
                context.storage_state(path=sess_path)
            except Exception:
                pass
        else:
            try:
                context.storage_state(path=sess_path)
            except Exception:
                pass

        if "datastat.ospharm.org" not in page.url or "accounts" in page.url:
            raise RuntimeError("Identifiants OSPHARM incorrects")

        progress("Connecté — chargement…")
        _wait_webix(page)
        _snap("1_login")

        # ── Navigation vers section ventes ────────────────────────────────────

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
            def _wait_sellout(ms=10_000):
                try:
                    page.wait_for_function(
                        '() => ["sellout","ventes.all"].some(x => location.hash.includes(x))',
                        timeout=ms)
                    return True
                except Exception:
                    return False

            try:
                _r0 = page.evaluate('''() => {
                    if (typeof webix === "undefined") return "no-webix";
                    const sb = webix.$$("top:menu")
                             || webix.$$(document.querySelector(".webix_sidebar")?.getAttribute("view_id"));
                    if (!sb) return "no-sb";
                    try { sb.open("sellout"); } catch(e) {}
                    sb.select("sellout.all");
                    const node = sb.getItemNode ? sb.getItemNode("sellout.all") : null;
                    if (node) { node.dispatchEvent(new MouseEvent("click",{bubbles:true})); return "click-node"; }
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
                page.evaluate("""() => {
                    const base = location.href.split('#')[0];
                    location.href = base + '#!/top/sellout.all';
                }""")
                page.wait_for_timeout(2_000)
                if _ventes_tabs_visible(): return True
                if _wait_sellout(6_000) and _ventes_tabs_visible(): return True
            except Exception as _e:
                print(f"  [nav] M1 err: {_e}")

            try:
                _loc_a = page.get_by_text("Analyse des ventes", exact=True).first
                if _loc_a.is_visible(timeout=1_500):
                    _loc_a.click(force=True, timeout=5_000)
                    page.wait_for_timeout(2_000)
            except Exception:
                pass
            try:
                _loc_t = page.get_by_text("Toutes les ventes", exact=True).first
                if _loc_t.is_visible(timeout=3_000):
                    _loc_t.click(force=True, timeout=5_000)
                    page.wait_for_timeout(1_500)
                    if _ventes_tabs_visible(): return True
                    if _wait_sellout(5_000) and _ventes_tabs_visible(): return True
            except Exception:
                pass

            try:
                page.goto("https://datastat.ospharm.org/#!/top/sellout.all",
                          wait_until="domcontentloaded", timeout=25_000)
                _wait_webix(page)
                page.wait_for_timeout(3_000)
                if _ventes_tabs_visible(): return True
                if _wait_sellout(8_000) and _ventes_tabs_visible(): return True
            except Exception:
                pass

            _reauth_if_needed(page, creds, "ventes")
            _wait_webix(page)
            return _ventes_tabs_visible()

        # ── Navigation initiale ───────────────────────────────────────────────
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
                _upload_screenshots()
                browser.close()
                raise RuntimeError(f"Nav ventes échoué — url={page.url[:80]}")
            _snap("2_apres_nav")

        # ── Sélection d'un mois précis via date picker ────────────────────────

        def _click_valider():
            """Clique Valider dans un popup/overlay Webix ou bouton HTML visible."""
            page.evaluate('''() => {
                const kws = ["valider", "ok", "appliquer", "apply", "confirmer"];
                // Boutons HTML visibles
                for (const el of document.querySelectorAll("button,.webix_button")) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) continue;
                    const t = (el.textContent || "").trim().toLowerCase();
                    if (kws.some(k => t === k || t.startsWith(k))) { el.click(); return; }
                }
                // Éléments webix avec label
                if (typeof webix !== "undefined") {
                    for (const el of document.querySelectorAll("[view_id]")) {
                        const vid = el.getAttribute("view_id");
                        try {
                            const v = webix.$$(vid);
                            if (!v) continue;
                            const lbl = ((v.config && v.config.label) || "").toLowerCase();
                            if (kws.some(k => lbl.includes(k))) { v.callEvent("onItemClick",[]); return; }
                        } catch(e) {}
                    }
                }
            }''')

        def _dt_row_count() -> int:
            """Nb lignes dans le datatable principal (avant/après filtre pour vérification)."""
            try:
                return page.evaluate('''() => {
                    if (typeof webix === "undefined") return -1;
                    let mx = 0;
                    for (const el of document.querySelectorAll("[view_id]")) {
                        try {
                            const v = webix.$$(el.getAttribute("view_id"));
                            if (v && (v.name === "datatable" || v.name === "treetable") && v.count)
                                mx = Math.max(mx, v.count());
                        } catch(_) {}
                    }
                    return mx;
                }''')
            except Exception:
                return -1

        _webix_views_logged = False
        _filter_items_logged = False

        def _log_webix_views(lbl: str):
            nonlocal _webix_views_logged
            if _webix_views_logged:
                return
            try:
                views = page.evaluate('''() => {
                    if (typeof webix === "undefined") return ["no-webix"];
                    const out = [];
                    for (const el of document.querySelectorAll("[view_id]")) {
                        const vid = el.getAttribute("view_id");
                        if (!vid || vid.startsWith("$")) continue;
                        try {
                            const v = webix.$$(vid);
                            if (!v) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width < 1 || r.height < 1) continue;
                            const nm  = v.name || v.config?.view || "?";
                            const lbl = (v.config?.label || "").slice(0, 20);
                            const cnt = (v.count ? v.count() : "");
                            out.push(vid + "[" + nm + "]" + (lbl ? "(" + lbl + ")" : "") + (cnt ? "=" + cnt : ""));
                        } catch(_) {}
                    }
                    return out;
                }''')
                print(f"  [{lbl}] webix-views: {views}")
                _webix_views_logged = True
            except Exception as _lve:
                print(f"  [{lbl}] webix-views-err: {_lve}")

        def _log_filter_items(lbl: str):
            nonlocal _filter_items_logged
            if _filter_items_logged:
                return
            _filter_items_logged = True
            try:
                items = page.evaluate('''() => {
                    if (typeof webix === "undefined") return ["no-webix"];
                    const fl = webix.$$("filters");
                    if (!fl) return ["no-filters"];
                    const ser = fl.serialize ? fl.serialize()
                              : (fl.data?.serialize ? fl.data.serialize() : []);
                    return ser.slice(0, 30).map(i =>
                        (i.id || "?") + ":" + (i.value || i.label || i.$value || "").slice(0, 40));
                }''')
                print(f"  [{lbl}] filter-items: {items}")
            except Exception as _fie:
                print(f"  [{lbl}] filter-items-err: {_fie}")

        def _wait_data_reload(timeout_ms: int = 25_000):
            """Attend que les données aient fini de se charger (networkidle ou spinner)."""
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                page.wait_for_timeout(4_000)

        def _select_month_in_picker(year: int, month: int) -> tuple[str, str]:
            """Sélectionne un mois via le date picker OSPHARM.
            Retourne (start_iso, end_iso).
            """
            last_day  = calendar.monthrange(year, month)[1]
            start_fr  = f"01/{month:02d}/{year:04d}"
            end_fr    = f"{last_day:02d}/{month:02d}/{year:04d}"
            start_iso = f"{year:04d}-{month:02d}-01"
            end_iso   = f"{year:04d}-{month:02d}-{last_day:02d}"
            lbl       = f"{year}-{month:02d}"

            _log_webix_views(lbl)
            _log_filter_items(lbl)

            cnt_before = _dt_row_count()
            xhr_before = len(_xhr_log)

            # ── Approche A : interaction UI (ouvrir picker → remplir → appliquer) ─
            # C'est la seule approche qui garantit que le filtre serveur/client recharge
            # les données. Les approches Webix API (setValue) ne font que mettre à jour
            # la valeur interne des calendriers sans déclencher le rechargement.

            picker_res = page.evaluate(f'''() => {{
                // 1. Chercher par view_id contenant "date" ou "picker"
                for (const el of document.querySelectorAll("[view_id]")) {{
                    const vid = el.getAttribute("view_id") || "";
                    if (vid.startsWith("$")) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) continue;
                    if (/date|picker|period|filtre|range/i.test(vid)) {{
                        (el.querySelector("button,.webix_button,.webix_template") || el).click();
                        return "vid:" + vid;
                    }}
                }}
                // 2. Bouton dont le texte contient une date dd/mm/yyyy ou "période"
                for (const el of document.querySelectorAll("button,.webix_button,.webix_el_button,[view_id]")) {{
                    const r = el.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) continue;
                    const t = (el.innerText || el.textContent || "").trim();
                    if (/\d{{2}}[\/\-\.]\d{{2}}[\/\-\.]\d{{2,4}}/.test(t) ||
                        /^(période|filtre|date|calendrier)/i.test(t)) {{
                        el.click(); return "txt:" + t.slice(0, 30);
                    }}
                }}
                // 3. Chercher via webix un composant daterange visible
                if (typeof webix !== "undefined") {{
                    for (const el of document.querySelectorAll("[view_id]")) {{
                        const vid = el.getAttribute("view_id") || "";
                        if (vid.startsWith("$")) continue;
                        try {{
                            const v = webix.$$(vid);
                            if (!v) continue;
                            const nm = (v.name || v.config?.view || "").toLowerCase();
                            if (nm.includes("daterange") || nm.includes("datepicker")) {{
                                const r = el.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {{
                                    (el.querySelector("button,.webix_button") || el).click();
                                    return "wx:" + vid;
                                }}
                            }}
                        }} catch(_) {{}}
                    }}
                }}
                return "no-picker";
            }}''')
            print(f"  [{lbl}] picker-open: {picker_res}")
            # Attente que le popup se rende (my_datepicker + startDate + endDate)
            page.wait_for_timeout(1500)

            # Log vues webix après ouverture du picker (1 seul appel)
            if lbl == f"{start_year}-{start_month:02d}":
                try:
                    _pv = page.evaluate('''() => {
                        if (typeof webix === "undefined") return [];
                        const out = [];
                        for (const el of document.querySelectorAll("[view_id]")) {
                            const vid = el.getAttribute("view_id");
                            if (!vid || vid.startsWith("$")) continue;
                            try {
                                const v = webix.$$(vid);
                                if (!v) continue;
                                const r = el.getBoundingClientRect();
                                if (r.width < 1 || r.height < 1) continue;
                                out.push(vid + "[" + (v.name||"?") + "]");
                            } catch(_) {}
                        }
                        return out;
                    }''')
                    print(f"  [{lbl}] picker-views: {_pv}")
                except Exception:
                    pass

            if picker_res != "no-picker":
                # ── Approche A-dp : Webix datepicker startDate/endDate setValue ──
                # Le popup my_datepicker[popup] contient startDate[datepicker] et
                # endDate[datepicker]. On set les valeurs via Webix API puis on
                # tente de fermer le popup pour déclencher onHide → reload serveur.
                dp_result = page.evaluate(f'''([yr, mo, ld]) => {{
                    if (typeof webix === "undefined") return "no-webix";
                    const sd = webix.$$("startDate");
                    const ed = webix.$$("endDate");
                    const dp = webix.$$("my_datepicker");
                    if (!sd || !ed) return "no-sd-ed";

                    const start = new Date(yr, mo - 1, 1);
                    const end   = new Date(yr, mo - 1, ld);

                    // Setter les valeurs
                    try {{ sd.setValue(start); }} catch(_) {{}}
                    try {{ if (sd.callEvent) sd.callEvent("onChange", [start, null]); }} catch(_) {{}}
                    try {{ ed.setValue(end); }} catch(_) {{}}
                    try {{ if (ed.callEvent) ed.callEvent("onChange", [end, null]); }} catch(_) {{}}

                    // Chercher bouton Valider/OK/Appliquer dans le popup
                    const kws = ["valider","ok","appliquer","apply","rechercher","confirmer"];
                    if (dp && dp.$view) {{
                        for (const el of dp.$view.querySelectorAll(
                            "button,[role=button],.webix_button,[view_id]"
                        )) {{
                            const r = el.getBoundingClientRect();
                            if (r.width < 1 || r.height < 1) continue;
                            const t = (el.textContent || "").trim().toLowerCase();
                            if (kws.some(k => t === k || t.startsWith(k))) {{
                                el.click(); return "btn-click:" + t.slice(0, 20);
                            }}
                        }}
                        // Webix button views dans le popup
                        for (const el of dp.$view.querySelectorAll("[view_id]")) {{
                            const vid = el.getAttribute("view_id");
                            if (!vid) continue;
                            try {{
                                const v = webix.$$(vid);
                                if (!v || v.name !== "button") continue;
                                const lbl_ = (v.config?.label || "").toLowerCase();
                                const r = el.getBoundingClientRect();
                                if (r.width < 1 || r.height < 1) continue;
                                if (kws.some(k => lbl_.includes(k))) {{
                                    el.click(); return "wx-btn:" + lbl_.slice(0, 20);
                                }}
                            }} catch(_) {{}}
                        }}
                    }}
                    // Pas de bouton : fermer le popup pour déclencher onHide
                    try {{ if (dp && dp.hide) dp.hide(); }} catch(_) {{}}
                    return "hide-popup";
                }}''', [year, month, last_day])
                print(f"  [{lbl}] dp-set: {dp_result}")

                if dp_result not in ("no-webix", "no-sd-ed"):
                    page.wait_for_timeout(500)
                    _wait_data_reload()
                    cnt_after  = _dt_row_count()
                    xhr_delta  = len(_xhr_log) - xhr_before
                    print(f"  [{lbl}] dt: {cnt_before}→{cnt_after} | xhr-delta: {xhr_delta}")
                    if xhr_delta > 0:
                        print(f"  [{lbl}] xhr-urls: {_xhr_log[xhr_before:xhr_before+3]}")
                        return start_iso, end_iso
                    # Pas de XHR : essayer de cliquer l'input startDate pour ouvrir
                    # le calendrier interne, le naviguer, puis faire pareil pour endDate
                    print(f"  [{lbl}] dp-set: pas de XHR — tentative clic calendrier interne")
                    page.evaluate('''() => {
                        const dp = webix.$$("my_datepicker");
                        if (dp && dp.show) dp.show();
                        const sd = webix.$$("startDate");
                        if (!sd || !sd.$view) return;
                        (sd.$view.querySelector(".webix_inp_static, input") || sd.$view).click();
                    }''')
                    page.wait_for_timeout(600)

                # Remplir les inputs de date visibles
                filled = page.evaluate('''([sf, ef]) => {
                    const setVal = (el, val) => {
                        try {
                            const setter = Object.getOwnPropertyDescriptor(
                                Object.getPrototypeOf(el), "value"
                            )?.set || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
                            setter.call(el, val);
                        } catch(_) { el.value = val; }
                        ["input","change","keyup","blur"].forEach(t =>
                            el.dispatchEvent(new Event(t, {bubbles:true}))
                        );
                    };
                    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 4 && r.height > 4; };
                    const inputs = [...document.querySelectorAll(
                        "input[type=text],input:not([type]),input[type=date]"
                    )].filter(vis);
                    if (!inputs.length) return "no-inputs";
                    const starts = inputs.filter(e => /de|du|déb|from|start|début/i.test(e.placeholder+e.name+e.id));
                    const ends   = inputs.filter(e => /au|à|fin|to|end/i.test(e.placeholder+e.name+e.id));
                    if (starts.length && ends.length) {
                        setVal(starts[0], sf); setVal(ends[0], ef);
                        return "named:" + inputs.length;
                    }
                    if (inputs.length >= 2) {
                        setVal(inputs[0], sf); setVal(inputs[1], ef);
                        return "pos2:" + inputs.length;
                    }
                    setVal(inputs[0], sf); return "single";
                }''', [start_fr, end_fr])
                print(f"  [{lbl}] fill: {filled}")

                if "no-inputs" not in str(filled):
                    page.wait_for_timeout(300)
                    # Cliquer le bouton d'application (Valider / Ok / Rechercher / Appliquer)
                    apply_res = page.evaluate('''() => {
                        const kws = ["valider","ok","appliquer","apply","rechercher",
                                     "chercher","filtrer","go","confirmer"];
                        // Priorité aux popups ouverts
                        for (const scope of [
                            ".webix_popup,.webix_window,.webix_modal",
                            "button,.webix_button"
                        ]) {
                            for (const el of document.querySelectorAll(scope + " button," + scope)) {
                                const r = el.getBoundingClientRect();
                                if (r.width < 1 || r.height < 1) continue;
                                const t = (el.textContent || "").trim().toLowerCase();
                                if (kws.some(k => t === k || t.startsWith(k))) {
                                    el.click(); return "btn:" + t.slice(0, 20);
                                }
                            }
                        }
                        return "no-btn";
                    }''')
                    print(f"  [{lbl}] apply-btn: {apply_res}")
                    _wait_data_reload()

                    cnt_after = _dt_row_count()
                    xhr_delta = len(_xhr_log) - xhr_before
                    print(f"  [{lbl}] dt: {cnt_before}→{cnt_after} | xhr-delta: {xhr_delta}")
                    # Log les dernières URL XHR pour diagnostic
                    if xhr_delta > 0:
                        print(f"  [{lbl}] xhr-urls: {_xhr_log[xhr_before:xhr_before+5]}")
                    return start_iso, end_iso

                # ── Approche A2 : clic cellules calendrier (picker ouvert, pas d'inputs) ──
                # Navigue mois par mois dans le calendrier Webix puis clique J1 et Jdernier.
                _cal_ok = False
                for _cal_try in range(30):
                    _cal_nav = page.evaluate(r'''([yr, mo, lastDay]) => {
                        const FR=['janvier','février','mars','avril','mai','juin',
                                  'juillet','août','septembre','octobre','novembre','décembre'];
                        const EN=['january','february','march','april','may','june',
                                  'july','august','september','october','november','december'];
                        function vis(el){const r=el.getBoundingClientRect();return r.width>4&&r.height>4;}
                        function parseHdr(el){
                            const txt=(el.textContent||'').toLowerCase();
                            for(let i=0;i<12;i++) if(txt.includes(FR[i])||txt.includes(EN[i])){
                                const ym=txt.match(/\d{4}/);if(ym) return{m:i+1,y:+ym[0]};
                            }
                            const m2=txt.match(/(\d{1,2})[\/\-](\d{4})/);
                            if(m2) return{m:+m2[1],y:+m2[2]};
                            return null;
                        }
                        const hdrs=[...document.querySelectorAll(
                            '.webix_cal_month_name,[class*="cal_month_name"]'
                        )].filter(vis);
                        if(!hdrs.length) return{done:false,nav:null,err:'no-hdr'};
                        for(const hdr of hdrs){
                            const info=parseHdr(hdr);
                            if(!info||info.y!==yr||info.m!==mo) continue;
                            const cal=hdr.closest('[class*="webix_cal"],[class*="calendar"]')||hdr.parentElement;
                            const days=[...cal.querySelectorAll('.webix_cal_day,[class*="cal_day"]')]
                                .filter(el=>{const t=el.textContent.trim();return vis(el)&&/^\d{1,2}$/.test(t);});
                            const d1=days.find(el=>el.textContent.trim()==='1');
                            const dN=days.find(el=>el.textContent.trim()===String(lastDay));
                            if(d1) d1.click();
                            if(dN) dN.click();
                            return{done:true,d1:!!d1,dN:!!dN};
                        }
                        const firstInfo=parseHdr(hdrs[0]);
                        if(!firstInfo) return{done:false,nav:null,err:'no-parse'};
                        const diff=(yr-firstInfo.y)*12+(mo-firstInfo.m);
                        const dir=diff>0?'next':'prev';
                        const cal0=hdrs[0].closest('[class*="webix_cal"],[class*="calendar"]')||hdrs[0].parentElement;
                        const btn=cal0.querySelector(dir==='next'
                            ?'.webix_cal_next_button,[class*="cal_next"]'
                            :'.webix_cal_prev_button,[class*="cal_prev"]');
                        if(!btn||!vis(btn)) return{done:false,nav:null,err:'no-btn'};
                        btn.click();
                        return{done:false,nav:dir};
                    }''', [year, month, last_day])
                    if isinstance(_cal_nav, dict) and _cal_nav.get('done'):
                        _cal_ok = True
                        break
                    _nav_dir = _cal_nav.get('nav') if isinstance(_cal_nav, dict) else None
                    if not _nav_dir:
                        print(f"  [{lbl}] cal-nav stop: {_cal_nav}")
                        break
                    page.wait_for_timeout(250)

                if _cal_ok:
                    print(f"  [{lbl}] cal-click: ok ({_cal_try + 1} pas)")
                    page.wait_for_timeout(600)
                    _click_valider()
                    _wait_data_reload()
                    cnt_after = _dt_row_count()
                    xhr_delta = len(_xhr_log) - xhr_before
                    print(f"  [{lbl}] dt: {cnt_before}→{cnt_after} | xhr-delta: {xhr_delta}")
                    return start_iso, end_iso
                print(f"  [{lbl}] cal-click: échec — fallback API")

            # ── Approche B : Webix API (daterange.setValue + filterByAll) ─────
            # Utilisé si le picker UI n'a pas pu être ouvert.
            # Note : setValue seul ne recharge pas les données — il faut filterByAll.
            api_ok = page.evaluate(f'''() => {{
                try {{
                    if (typeof webix === "undefined") return "no-webix";
                    const start = new Date({year}, {month - 1}, 1);
                    const end   = new Date({year}, {month - 1}, {last_day});
                    const DR_NAMES = ["daterange","daterangepicker","daterangefilter"];
                    let used = null;

                    for (const el of document.querySelectorAll("[view_id]")) {{
                        const vid = el.getAttribute("view_id");
                        if (!vid || vid.startsWith("$suggest")) continue;
                        try {{
                            const v = webix.$$(vid);
                            if (!v || !v.config) continue;
                            const nm = (v.name || v.config.view || "").toLowerCase();
                            if (DR_NAMES.some(n => nm.includes(n)) && typeof v.setValue === "function") {{
                                v.setValue({{start, end}});
                                if (v.callEvent) v.callEvent("onChange", [{{start, end}}, {{}}]);
                                used = "dr:" + vid; break;
                            }}
                            if (typeof v.getValue === "function" && typeof v.setValue === "function") {{
                                try {{
                                    const cur = v.getValue();
                                    if (cur && typeof cur === "object" && ("start" in cur || "end" in cur)) {{
                                        v.setValue({{start, end}});
                                        if (v.callEvent) v.callEvent("onChange", [{{start, end}}, {{}}]);
                                        used = "generic:" + vid; break;
                                    }}
                                }} catch(_) {{}}
                            }}
                        }} catch(_) {{}}
                    }}
                    if (!used) {{
                        const sV = webix.$$("startDate"), eV = webix.$$("endDate");
                        if (sV && eV && typeof sV.setValue === "function") {{
                            sV.setValue(start); eV.setValue(end);
                            const par = sV.getParentView ? sV.getParentView() : null;
                            if (par && par !== sV && par.callEvent)
                                par.callEvent("onChange", [{{start, end}}, {{}}]);
                            if (sV.callEvent) sV.callEvent("onChange", [start, null]);
                            if (eV.callEvent) eV.callEvent("onChange", [end, null]);
                            used = "start-end";
                        }}
                    }}
                    if (!used) return "no-dr";

                    // filterByAll sur tous les datatables pour appliquer le filtre côté client
                    let fba = 0;
                    for (const el of document.querySelectorAll("[view_id]")) {{
                        try {{
                            const v = webix.$$(el.getAttribute("view_id"));
                            if (v && (v.name === "datatable" || v.name === "treetable") &&
                                typeof v.filterByAll === "function") {{
                                v.filterByAll(); fba++;
                            }}
                        }} catch(_) {{}}
                    }}
                    return used + ":fba=" + fba;
                }} catch(e) {{ return "err:" + e.message; }}
            }}''')
            print(f"  [{lbl}] webix-api: {api_ok}")

            if not str(api_ok).startswith(("no-", "err:")):
                _wait_data_reload()
                cnt_after = _dt_row_count()
                xhr_delta = len(_xhr_log) - xhr_before
                print(f"  [{lbl}] dt: {cnt_before}→{cnt_after} | xhr-delta: {xhr_delta}")
                return start_iso, end_iso

            # ── Approche C : Playwright locator fill ──────────────────────────
            try:
                vis_inputs = page.locator(
                    "input[type=text]:visible, input:not([type]):visible"
                ).all()
                if len(vis_inputs) >= 2:
                    vis_inputs[0].fill(start_fr)
                    vis_inputs[1].fill(end_fr)
                    page.wait_for_timeout(200)
                    _click_valider()
                    _wait_data_reload()
                    print(f"  [{lbl}] pw-fill: ok ({len(vis_inputs)} inputs)")
                    return start_iso, end_iso
            except Exception as _pf_err:
                print(f"  [{lbl}] pw-fill err: {_pf_err}")

            print(f"  [{lbl}] WARN: aucun picker trouvé — données potentiellement incorrectes")
            page.wait_for_timeout(300)
            return start_iso, end_iso

        # ── Export d'un mois ──────────────────────────────────────────────────

        def _export_one_month(year: int, month: int) -> tuple:
            """Sélectionne le mois, exporte Excel, retourne (raw_rows, ps, pe, file_url)."""
            lbl = f"{year}-{month:02d}"

            if not _ventes_tabs_visible():
                progress(f"Re-navigation ventes ({lbl})…")
                if not _goto_sellout():
                    raise RuntimeError(f"Nav ventes échouée pour {lbl}")
            _reauth_if_needed(page, creds, lbl)

            progress(f"Sélection {lbl}…")
            ps, pe = _select_month_in_picker(year, month)

            if not _ventes_tabs_visible():
                _goto_sellout()

            _snap(f"3_date_{lbl}")

            # Onglet Produits
            progress(f"Produits ({lbl})…")
            page.evaluate('''() => {
                for (const el of document.querySelectorAll(
                    ".webix_segment_0,.webix_segment_1,.webix_segment_N,button"
                )) {
                    if (el.textContent.trim() !== "Produits") continue;
                    const r = el.getBoundingClientRect();
                    if (r.width > 1 && r.height > 1) { el.click(); return; }
                }
            }''')
            page.wait_for_timeout(800)
            _snap(f"4_produits_{lbl}")

            if "accounts" in page.url:
                _reauth_if_needed(page, creds, f"export {lbl}")
                _wait_webix(page)
                if not _goto_sellout():
                    raise RuntimeError(f"Re-nav après reauth échouée ({lbl})")
                _select_month_in_picker(year, month)
                page.evaluate('''() => {
                    for (const el of document.querySelectorAll(
                        ".webix_segment_0,.webix_segment_1,.webix_segment_N,button"
                    )) {
                        if (el.textContent.trim() !== "Produits") continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 1 && r.height > 1) { el.click(); return; }
                    }
                }''')
                page.wait_for_timeout(3_000)

            # Attente chargement données (120s max pour un mois)
            progress(f"Chargement données ({lbl})…")
            try:
                page.wait_for_function('''() => {
                    for (const el of document.querySelectorAll("*")) {
                        if (el.children.length > 0) continue;
                        const t = el.textContent.trim();
                        if ((t.includes("Chargement") || t.includes("loading"))
                                && el.getBoundingClientRect().width > 0) return false;
                    }
                    return document.querySelectorAll(
                        ".webix_dtable .webix_row, .webix_ss_body .webix_column .webix_cell"
                    ).length > 0;
                }''', timeout=120_000)
            except Exception as _e:
                print(f"  [{lbl}] timeout chargement: {_e}")
            _snap(f"4c_data_{lbl}")

            # Export Excel
            # On ouvre expect_download AVANT de cliquer le bouton export pour capter
            # aussi bien les téléchargements directs que ceux nécessitant un popup Valider.
            progress(f"Export Excel ({lbl})…")
            _tmp_fd, _tmp_dl = tempfile.mkstemp(suffix=".xlsx")
            _os.close(_tmp_fd)
            _excel_bytes = None

            kw_export = ["excel", "export", "exporter", "xls", "télécharger"]
            _snap(f"5_avant_export_{lbl}")

            try:
                with page.expect_download(timeout=30_000) as _dl_ctx:
                    try:
                        exported = page.evaluate('''(kw) => {
                            function vis(el) { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }
                            function strTip(v) {
                                const raw = v.config?.tooltip || v.config?.label || "";
                                return (typeof raw === "string" ? raw : "").toLowerCase();
                            }
                            if (typeof webix !== "undefined") {
                                for (const el of document.querySelectorAll("[view_id]")) {
                                    if (!vis(el)) continue;
                                    const vid = el.getAttribute("view_id");
                                    const v = webix.$$(vid);
                                    if (!v || v.name !== "button") continue;
                                    const icon = (typeof v.config?.icon === "string" ? v.config.icon : "").toLowerCase();
                                    if (["excel","xls","export","exporter","download","télécharger","file-"]
                                            .some(k => icon.includes(k) || el.innerHTML.toLowerCase().includes(k))) {
                                        el.click(); return "M1b:" + icon.slice(0,30);
                                    }
                                }
                                for (const el of document.querySelectorAll("[view_id]")) {
                                    if (!vis(el)) continue;
                                    const v = webix.$$(el.getAttribute("view_id"));
                                    if (!v) continue;
                                    if (kw.some(k => strTip(v).includes(k))) { el.click(); return "M1a:" + strTip(v).slice(0,30); }
                                }
                            }
                            const tabNames = new Set(["Laboratoires","Familles","Produits","Marques"]);
                            let maxRight=0, bandTop=0, bandBottom=0;
                            for (const el of document.querySelectorAll(".webix_item_tab,.webix_segment_0,.webix_segment_1,.webix_segment_N")) {
                                if (!tabNames.has(el.textContent.trim())) continue;
                                const r = el.getBoundingClientRect();
                                if (r.width<4||r.height<4) continue;
                                if (r.right>maxRight){ maxRight=r.right; bandTop=r.top; bandBottom=r.bottom; }
                            }
                            if (maxRight > 0) {
                                const midY=(bandTop+bandBottom)/2, halfH=(bandBottom-bandTop)/2+10;
                                const cands = [...document.querySelectorAll("[view_id],button,.webix_el_icon,.webix_el_button")]
                                    .filter(el => {
                                        const r=el.getBoundingClientRect();
                                        return r.left>maxRight+2 && r.top<midY+halfH && r.bottom>midY-halfH && r.width>8 && r.height>8 && r.width<200;
                                    });
                                cands.sort((a,b)=>a.getBoundingClientRect().left-b.getBoundingClientRect().left);
                                if (cands.length) { (cands[0].querySelector("button,.webix_template")||cands[0]).click(); return "M2:pos"; }
                            }
                            for (const el of document.querySelectorAll("[webix_tooltip]")) {
                                if (!vis(el)) continue;
                                const tip=(el.getAttribute("webix_tooltip")||"").toLowerCase();
                                if (kw.some(k=>tip.includes(k))) { (el.querySelector("button")||el).click(); return "M3:"+tip.slice(0,30); }
                            }
                            for (const el of document.querySelectorAll("button,a,[role=button],.webix_el_button")) {
                                if (!vis(el)) continue;
                                const hay=(el.textContent+(el.title||"")+(el.getAttribute("aria-label")||"")).toLowerCase();
                                if (kw.some(k=>hay.includes(k))) { el.click(); return "M4:"+hay.slice(0,30); }
                            }
                            return false;
                        }''', kw_export)
                    except Exception as _ev_err:
                        if "context" in str(_ev_err).lower() or "destroyed" in str(_ev_err).lower():
                            exported = "context-destroyed-ok"
                        else:
                            raise RuntimeError(f"Export evaluate err ({lbl}): {_ev_err}")

                    if not exported:
                        raise RuntimeError(f"Bouton export introuvable ({lbl})")
                    print(f"  [{lbl}] export btn: {exported}")
                    _snap(f"5b_apres_click_{lbl}")

                    # Si un popup Valider apparaît, le cliquer
                    try:
                        _val = page.locator(
                            ".webix_window button,.webix_popup button,"
                            ".webix_modal button,.webix_win_body button"
                        ).filter(has_text="Valider").first
                        _val.wait_for(state="visible", timeout=12_000)
                        print(f"  [{lbl}] Valider → click")
                        _val.click(timeout=5_000)
                    except Exception:
                        print(f"  [{lbl}] pas de Valider — téléchargement direct attendu")

                _dl = _dl_ctx.value
                _dl.save_as(_tmp_dl)
                with open(_tmp_dl, "rb") as _f:
                    _body = _f.read()
                if len(_body) > 500:
                    _excel_bytes = _body
                    print(f"  [{lbl}] ✓ download {len(_body):,} bytes")
                else:
                    print(f"  [{lbl}] download trop petit {len(_body)} → fallback")
            except PWTimeout:
                print(f"  [{lbl}] timeout download — fallback webix.toExcel")
            except Exception as _edd:
                print(f"  [{lbl}] download err: {str(_edd)[:100]} — fallback webix.toExcel")

            if not _excel_bytes:
                try:
                    with page.expect_download(timeout=60_000) as _dl_info2:
                        _res2 = page.evaluate('''() => {
                            if (typeof webix === "undefined" || typeof webix.toExcel !== "function") return "no";
                            let best=null, bestCount=0;
                            for (const el of document.querySelectorAll("[view_id]")) {
                                const v = webix.$$(el.getAttribute("view_id"));
                                if (v && (v.name==="datatable"||v.name==="treetable")) {
                                    const c = v.count ? v.count() : 0;
                                    if (c > bestCount) { best=v; bestCount=c; }
                                }
                            }
                            if (!best) return "no-dt";
                            webix.toExcel(best, {filename:"ospharm_export"});
                            return "toExcel:" + bestCount;
                        }''')
                    print(f"  [{lbl}] webix.toExcel: {_res2}")
                    _dl2 = _dl_info2.value
                    _dl2.save_as(_tmp_dl)
                    with open(_tmp_dl, "rb") as _f:
                        _body2 = _f.read()
                    if len(_body2) > 500:
                        _excel_bytes = _body2
                except Exception as _edd2:
                    print(f"  [{lbl}] webix.toExcel err: {_edd2}")

            if not _excel_bytes:
                raise RuntimeError(f"Aucun fichier Excel reçu ({lbl})")

            # Upload Storage
            _file_url = ""
            if user_id:
                try:
                    from supabase_client import upload_file_sync, get_signed_url_sync
                    date_str = datetime.date.today().strftime("%Y-%m-%d")
                    fname = f"ospharm_{lbl}_{date_str}.xlsx"
                    path = upload_file_sync(user_id, "ospharm", fname, _excel_bytes,
                                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    _file_url = get_signed_url_sync(path)
                    print(f"  [{lbl}] storage: {fname}")
                except Exception as _ue:
                    print(f"  [{lbl}] storage ERR: {_ue}")

            # Lecture Excel
            _tmp_fd2, _tmp2 = tempfile.mkstemp(suffix=".xlsx")
            _os.close(_tmp_fd2)
            with open(_tmp2, "wb") as f:
                f.write(_excel_bytes)
            wb = openpyxl.load_workbook(_tmp2, read_only=True, data_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            headers = [str(h or "").strip() for h in next(rows_iter)]
            raw_rows = []
            for row in rows_iter:
                if any(v is not None for v in row):
                    raw_rows.append({h: _strip_html(v) for h, v in zip(headers, row)})
            wb.close()
            print(f"  [{lbl}] colonnes Excel: {headers}")

            print(f"  [{lbl}] {len(raw_rows)} lignes brutes, {ps} → {pe}")
            return raw_rows, ps, pe, _file_url

        # ── Boucle mensuelle : Jan N-1 → mois courant ────────────────────────
        today       = datetime.date.today()
        start_year  = today.year - 1
        start_month = 1
        end_year    = today.year
        end_month   = today.month

        all_compact_rows: list[dict] = []
        month_meta: list[dict]       = []
        month_errors: list[str]      = []

        # Scraping incrémental : réutiliser les mois déjà en base (sauf mois courant)
        try:
            _ex_job  = _supa_get_state().get("ospharm_job", {})
            _ex_rows = [r for r in (_ex_job.get("rows", []) or []) if r.get("month")]
            _ex_meta = {(m["year"], m["month"]): m for m in (_ex_job.get("month_meta", []) or [])}
            _have_months = {(r["year"], r["month"]) for r in _ex_rows}
            print(f"  [incr] {len(_have_months)} mois existants en base")
        except Exception as _ex_err:
            _ex_rows, _ex_meta, _have_months = [], {}, set()
            print(f"  [incr] lecture base échouée: {_ex_err}")

        total_months = (end_year - start_year) * 12 + (end_month - start_month) + 1
        year, month  = start_year, start_month
        m_idx        = 0

        while (year, month) <= (end_year, end_month):
            m_idx += 1
            lbl = f"{year}-{month:02d}"
            is_current = (year == today.year and month == today.month)

            # Réutiliser le mois s'il est déjà en base et n'est pas le mois courant
            if (year, month) in _have_months and not is_current:
                existing = [r for r in _ex_rows if r["year"] == year and r["month"] == month]
                all_compact_rows.extend(existing)
                if (year, month) in _ex_meta:
                    month_meta.append(_ex_meta[(year, month)])
                print(f"  [{lbl}] ✓ réutilisé ({len(existing)} lignes cache)")
            else:
                progress(f"Mois {m_idx}/{total_months} : {lbl}…")
                try:
                    raw_rows, ps, pe, file_url = _export_one_month(year, month)
                    compact = _compact_month_rows(raw_rows)
                    for r in compact:
                        r["year"]  = year
                        r["month"] = month
                    all_compact_rows.extend(compact)
                    month_meta.append({
                        "year": year, "month": month,
                        "period_start": ps, "period_end": pe,
                        "rows": len(compact), "file_url": file_url,
                    })
                    print(f"  [{lbl}] ✓ {len(compact)} lignes scraper")
                except Exception as _me:
                    print(f"  [{lbl}] ERREUR: {_me}")
                    month_errors.append(f"{lbl}: {_me}")

            if month == 12:
                year += 1
                month = 1
            else:
                month += 1

        _upload_screenshots()
        browser.close()

    if not all_compact_rows:
        raise RuntimeError(
            f"Aucune donnée extraite sur {total_months} mois. "
            f"Erreurs: {month_errors[:3]}"
        )

    print(f"  [total] {len(all_compact_rows)} lignes compactes sur {len(month_meta)} mois OK "
          f"({len(month_errors)} erreurs)")

    # Période globale (pour backward compat simulation)
    first_meta = month_meta[0]  if month_meta else {}
    last_meta  = month_meta[-1] if month_meta else {}

    return all_compact_rows, month_meta, first_meta.get("period_start", ""), last_meta.get("period_end", "")


# ── Sync PUHT → references_pharmacie ─────────────────────────────────────────

def _sync_puht_supabase(puht_dict: dict):
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
        threads = [threading.Thread(target=_patch, args=(c, pu), daemon=True) for c, pu in batch]
        for t in threads: t.start()
        for t in threads: t.join()

    print(f"  [puht-sync] ✅ {len(items)} PUHT ({len(errors)} erreurs)")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"🚀  Job OSPHARM mensuel démarré pour user_id={USER_ID}")
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
        all_rows, month_meta, ps_global, pe_global = run_ospharm(
            creds, progress, user_id=USER_ID
        )

        # Mise à jour PUHT Supabase
        puht_dict: dict = {r["cip13"]: r["puht"] for r in all_rows if r.get("puht")}
        if puht_dict:
            progress(f"Mise à jour PUHT ({len(puht_dict)} CIPs)…")
            _sync_puht_supabase(puht_dict)

        # Calcul des stats mensuellespar labo
        progress("Calcul stats mensuelles par laboratoire…")
        cip_list = list({r["cip13"] for r in all_rows})
        refs_by_cip = _query_refs(cip_list)
        rsf_defs    = _query_rsf_defaults()
        month_stats = _build_month_stats(all_rows, refs_by_cip, rsf_defs)
        print(f"  [stats] {len(month_stats)} mois agrégés, {len(refs_by_cip)} refs trouvées")

        _update_job(
            "done",
            f"{len(all_rows)} références — {len(month_meta)} mois",
            rows=all_rows,
            blocking=True,
            month_meta=month_meta,
            month_stats=month_stats,
            period_start=ps_global,
            period_end=pe_global,
        )
        print(f"\n✅  {len(all_rows)} lignes OSPHARM / {len(month_meta)} mois ({time.time()-t0:.1f}s)")

    except Exception as e:
        _update_job("error", error=str(e), blocking=True)
        print(f"\n❌  {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
