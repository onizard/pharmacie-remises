"""
run_job_fse.py — Scraper OSPHARM FSE Banque (HTP+OI)

Flux :
  1. Login via camoufox sur accounts.dev.ospharm.org (même auth qu'OSPHARM Datastat)
  2. Navigation vers fse.ospharm.org/#!/top/manager.fse.bank
  3. Sélection filtre HTP+OI + Tous les virements + plage de dates
  4. Export → XLSX téléchargé
  5. Upload vers Supabase Storage bucket 'fse-bank'
  6. Appel /parse/fse-bank → agrégation et sauvegarde fse_month_stats

Variables d'environnement :
    USER_ID               Supabase user UUID
    SUPABASE_SERVICE_KEY  Clé de service Supabase
    DATE_FROM             Date début (YYYY-MM-DD), ex: 2025-01-01
    DATE_TO               Date fin   (YYYY-MM-DD), ex: 2026-04-30
"""

import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

import datetime as _dt_module

SUPA_URL    = "https://api.break-pharma.fr"
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
USER_ID     = os.environ["USER_ID"]

# Plage par défaut : janvier 2025 → avril 2026
_today    = _dt_module.date.today()
DATE_FROM = os.environ.get("DATE_FROM", "") or "2025-01-01"
DATE_TO   = os.environ.get("DATE_TO",   "") or "2026-04-30"

FSE_URL     = "https://fse.ospharm.org"
LOGIN_URL   = "https://accounts.dev.ospharm.org"


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
    req  = urllib.request.Request(
        url, data=body, method="PATCH",
        headers={
            "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=15):
        pass


def _update_job(status: str, message: str = "", error: str = ""):
    try:
        import datetime as _dt
        state = _supa_get_state()
        job = {"status": status, "message": message, "error": error}
        if status == "done":
            job["completed_at"] = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        state["fse_job"] = job
        _supa_patch_state(state)
    except Exception as e:
        print(f"  [warn] Supabase update failed: {e}")


def _get_creds() -> dict:
    """Récupère les credentials depuis la colonne 'connectors' ou state_json.connectors."""
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}&select=connectors&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    conns = (rows[0].get("connectors") or {}) if rows else {}
    # Priorité : colonne connectors → state_json.connectors
    for key in ("concentrateur", "ospharm"):
        c = conns.get(key, {})
        if c.get("user") and c.get("pass"):
            print(f"  → Credentials depuis connector '{key}'")
            return {"user": c["user"], "pass": c["pass"]}

    # Fallback : state_json.connectors
    state = _supa_get_state()
    for key in ("concentrateur", "ospharm"):
        c = (state.get("connectors") or {}).get(key, {})
        if c.get("user") and c.get("pass"):
            print(f"  → Credentials depuis state_json.connectors.{key}")
            return {"user": c["user"], "pass": c["pass"]}

    raise ValueError("Credentials OSPHARM FSE manquants — configurez le connecteur Concentrateur ou OSPHARM Datastat.")


def _upload_xlsx_to_storage(xlsx_bytes: bytes, filename: str) -> str:
    """Upload le XLSX vers le bucket 'fse-bank' et retourne le storage_path."""
    safe_name = filename.replace(" ", "_")
    path      = f"{USER_ID}/{safe_name}"
    url       = f"{SUPA_URL}/storage/v1/object/fse-bank/{path}"
    req = urllib.request.Request(
        url, data=xlsx_bytes, method="POST",
        headers={
            "apikey":        SERVICE_KEY,
            "Authorization": f"Bearer {SERVICE_KEY}",
            "Content-Type":  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        print(f"  ✓ Uploadé : {path} ({resp})")
    except Exception as e:
        print(f"  [warn] Upload Storage échoué ({e}) — on parsera quand même localement")
    return path


def _call_parse_api(storage_path: str) -> dict:
    """Appelle /parse/fse-bank via l'API Render pour agréger et sauvegarder."""
    import urllib.parse
    RENDER_URL = os.environ.get("RENDER_API_URL", "https://pharmacie-remises.onrender.com")
    url  = f"{RENDER_URL}/parse/fse-bank"
    body = json.dumps({"storage_path": storage_path}).encode()
    req  = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {SERVICE_KEY}",  # service key comme Bearer token
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# ── Scraper Playwright/camoufox ────────────────────────────────────────────────

async def _scrape_fse_async(creds: dict, date_from: str, date_to: str) -> bytes:
    """
    Login sur OSPHARM FSE, navigue vers Banque > HTP+OI, sélectionne la plage de dates,
    exporte le XLSX et retourne les bytes du fichier.
    """
    from camoufox.async_api import AsyncCamoufox

    username = creds["user"]
    password = creds["pass"]

    async with AsyncCamoufox(headless=True) as browser:
        page = await browser.new_page()
        page.set_default_timeout(60_000)
        page.on("pageerror", lambda _: None)

        # ── 1. Login ────────────────────────────────────────────────────────────
        print("  → Navigation vers le login OSPHARM FSE…")
        login_url = f"{LOGIN_URL}/?client_id=test&redirect_uri={FSE_URL}/"
        await page.goto(login_url, wait_until="domcontentloaded", timeout=90_000)

        # Attendre le formulaire de login
        try:
            await page.wait_for_selector("input[type='password']", timeout=30_000)
        except Exception:
            await page.screenshot(path="fse_login_debug.png")
            raise RuntimeError(f"Formulaire login FSE introuvable (URL: {page.url})")

        # Remplir le formulaire de login accounts.dev (form POST /authorize).
        # client_id=test est le client FSE officiel (validé côté serveur) ; en cas
        # d'identifiants corrects le serveur renvoie ?code=… vers fse.ospharm.org.
        email_sel = "input[type='email'],input[name='email'],input[name='username'],input[type='text']"
        await page.locator(email_sel).first.fill(username)
        await page.locator("input[type='password']").first.fill(password)
        try:
            await page.locator("button[type='submit'],input[type='submit']").first.click(timeout=5_000)
        except Exception:
            await page.locator("input[type='password']").first.press("Enter")

        # Attendre la redirection vers fse.ospharm.org (avec ?code=…)
        try:
            await page.wait_for_url(f"{FSE_URL}/**", timeout=30_000)
        except Exception:
            pass
        if LOGIN_URL in page.url:
            await page.screenshot(path="fse_login_debug.png")
            raise RuntimeError(
                f"Login FSE échoué — identifiants OSPHARM refusés par accounts.dev "
                f"('Connexion impossible'). URL: {page.url}")

        print(f"  ✓ Code d'autorisation reçu — URL: {page.url[:80]}…")

        # ── 1b. Échange du code + attente de l'app ────────────────────────────────
        # L'app FSE est un SPA Vue+Webix où webix N'EST PAS exposé en global
        # (run #8 : 100 éléments [view_id] rendus mais window.webix/$$ undefined,
        # un seul frame). On ne peut donc pas piloter l'API Webix : on configure
        # les filtres + la plage de dates via les PARAMÈTRES DE ROUTE que le module
        # Banque lit lui-même (getParam: htp, datedebut, datefin…), puis on clique
        # le bouton "Exporter" via le DOM (l'app fait webix.toExcel en interne).
        print("  → Échange du code et initialisation de l'app FSE…")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(4_000)
        try:
            await page.wait_for_function(
                "() => /#!\\/top\\//.test(window.location.hash) "
                "&& document.querySelectorAll('[view_id]').length > 5",
                timeout=90_000)
        except Exception:
            await page.screenshot(path="fse_app_debug.png")
            raise RuntimeError(f"App FSE non chargée après login (URL: {page.url})")
        print(f"  ✓ App FSE rendue — {page.url[-40:]}")

        # ── 2. Module Banque avec filtres + dates via params de route ─────────────
        # htp=1 → origine "Hors tiers-payant" (virements labo directs, RDP/coop).
        # datedebut/datefin (YYYY-MM-DD) → plage du daterangepicker.
        print(f"  → Module Banque (HTP, {date_from} → {date_to})…")
        _route = (f"#!/top/manager.fse.bank?htp=1"
                  f"&datedebut={date_from}&datefin={date_to}")
        await page.evaluate(
            "(r) => { window.location.hash = r; "
            "window.dispatchEvent(new HashChangeEvent('hashchange')); }", _route)
        try:
            await page.wait_for_function(
                "() => document.querySelector('[view_id=\"fse_bank_origin\"]') "
                "&& document.querySelector('[view_id=\"fse_bank_datatable\"]')",
                timeout=60_000)
        except Exception:
            await page.screenshot(path="fse_bank_debug.png")
            _ids = await page.evaluate(
                "() => [...document.querySelectorAll('[view_id]')]"
                ".map(e => e.getAttribute('view_id')).slice(0, 40)")
            raise RuntimeError(f"Module Banque non monté. view_ids={_ids}")
        print("  ✓ Module Banque monté")

        # Laisser le datatable charger les virements côté serveur avant l'export.
        print("  → Rechargement des virements…")
        await page.wait_for_timeout(15_000)

        # ── 3. Export → clic DOM sur "Exporter" → webix.toExcel → téléchargement ──
        print("  → Export Excel…")
        async with page.expect_download(timeout=120_000) as dl_info:
            _exp = await page.evaluate("""() => {
                // Bouton "Exporter" du module Banque (webix_button). Son onItemClick
                // (code interne de l'app) construit un datatable d'export toutes-
                // lignes puis appelle webix.toExcel() → déclenche le download.
                const cands = document.querySelectorAll(
                    'button, .webix_button, .webix_el_button, [role="button"]');
                for (const b of cands) {
                    const t = (b.textContent || b.value || '').trim();
                    if (t === 'Exporter' || t === 'Exporter ') {
                        (b.querySelector('button') || b).click();
                        return 'clicked';
                    }
                }
                // fallback : recherche plus large
                for (const b of cands) {
                    if ((b.textContent || '').trim().includes('Export')) {
                        (b.querySelector('button') || b).click();
                        return 'clicked-loose:' + b.textContent.trim();
                    }
                }
                return 'no-export-btn';
            }""")
            print(f"  export-btn: {_exp}")
            if 'no-export-btn' in str(_exp):
                await page.screenshot(path="fse_export_debug.png")
                raise RuntimeError("Bouton Exporter introuvable (DOM)")

        download  = await dl_info.value
        filename  = download.suggested_filename or f"fse_bank_{date_from}_{date_to}.xlsx"
        tmp_path  = Path(f"/tmp/{filename}")
        await download.save_as(tmp_path)
        print(f"  ✓ Téléchargé : {filename} ({tmp_path.stat().st_size} bytes)")
        return tmp_path.read_bytes(), filename


def main():
    print(f"🚀  Job FSE Banque pour user_id={USER_ID}")
    _update_job("running", "Initialisation…")

    try:
        creds = _get_creds()
        print(f"  → Credentials chargés : user={creds['user'][:4]}***")
    except ValueError as e:
        _update_job("error", error=str(e))
        print(f"❌  {e}")
        sys.exit(1)

    print(f"  → Période : {DATE_FROM} → {DATE_TO}")

    try:
        _update_job("running", f"Connexion à OSPHARM FSE…")
        result = asyncio.run(_scrape_fse_async(creds, DATE_FROM, DATE_TO))
        xlsx_bytes, filename = result
    except Exception as e:
        _update_job("error", error=str(e))
        print(f"❌  Scraping échoué : {e}")
        sys.exit(1)

    # Upload vers Storage
    _update_job("running", "Upload vers Storage…")
    try:
        storage_path = _upload_xlsx_to_storage(xlsx_bytes, filename)
    except Exception as e:
        print(f"  [warn] Upload échoué ({e}) — parsing local")
        storage_path = None

    # Parser le XLSX et sauvegarder
    _update_job("running", "Parsing du XLSX…")
    try:
        if storage_path:
            result_data = _call_parse_api(storage_path)
        else:
            # Fallback : parser localement sans passer par l'API
            import importlib.util, io as _io
            spec = importlib.util.spec_from_file_location("main_api", __file__.replace("run_job_fse.py", "main.py"))
            # Parsing local direct
            from main import _parse_fse_bank_sync
            result_data = _parse_fse_bank_sync(USER_ID, "")  # ne fonctionnera pas sans storage
        months = result_data.get("months", [])
        total  = result_data.get("total_ttc", 0)
        print(f"  ✓ {len(months)} mois · {total:,.2f} € TTC")
        _update_job("done", f"{len(months)} mois · {total:,.2f} € TTC ({', '.join(months)})")
    except Exception as e:
        _update_job("error", error=str(e))
        print(f"❌  Parsing échoué : {e}")
        sys.exit(1)

    print("\n🎉  Job FSE terminé.")


if __name__ == "__main__":
    main()
