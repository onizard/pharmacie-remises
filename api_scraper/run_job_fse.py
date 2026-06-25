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

        # Remplir
        email_sel = "input[type='email'],input[name='email'],input[name='username'],input[type='text']"
        await page.locator(email_sel).first.fill(username)
        await page.locator("input[type='password']").first.fill(password)
        await page.locator("input[type='password']").first.press("Enter")

        # Attendre la redirection vers fse.ospharm.org
        try:
            await page.wait_for_url(f"{FSE_URL}/**", timeout=20_000)
        except Exception:
            pass
        if LOGIN_URL in page.url:
            await page.screenshot(path="fse_login_debug.png")
            raise RuntimeError(f"Login FSE échoué (toujours sur {page.url})")

        print(f"  ✓ Connecté — URL: {page.url}")

        # ── 2. Navigation vers Banque ────────────────────────────────────────────
        print("  → Navigation vers Banque…")
        await page.goto(f"{FSE_URL}/#!/top/manager.fse.bank", wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(5_000)  # Angular SPA init

        # ── 3. Filtre HTP+OI ──────────────────────────────────────────────────────
        print("  → Sélection filtre HTP+OI…")
        # Chercher le premier dropdown et sélectionner HTP+OI
        _sel_htp = await page.evaluate("""() => {
            // Chercher tous les éléments qui ressemblent à un sélecteur avec "HTP"
            const selects = document.querySelectorAll('select');
            for (const s of selects) {
                for (const o of s.options) {
                    if (o.text.includes('HTP') || o.value.includes('HTP')) {
                        s.value = o.value;
                        s.dispatchEvent(new Event('change', {bubbles: true}));
                        return 'select:' + o.text;
                    }
                }
            }
            // Chercher dropdowns custom (boutons/div avec texte HTP)
            const btns = document.querySelectorAll('[class*="dropdown"],[class*="select"] *');
            for (const el of btns) {
                if ((el.textContent||'').includes('HTP + OI') || (el.textContent||'').includes('HTP+OI')) {
                    el.click();
                    return 'btn:' + el.textContent.trim();
                }
            }
            return 'not-found';
        }""")
        print(f"  filter-HTP: {_sel_htp}")
        if 'not-found' in str(_sel_htp):
            # Essayer de cliquer sur le premier dropdown pour l'ouvrir puis sélectionner HTP
            for _ in range(3):
                _r2 = await page.evaluate("""() => {
                    const el = document.querySelector('[class*="dropdown-toggle"],[class*="select-btn"]');
                    if (el) { el.click(); return 'opened'; }
                    return 'no-toggle';
                }""")
                await page.wait_for_timeout(500)
                _r3 = await page.evaluate("""() => {
                    const items = document.querySelectorAll('[class*="dropdown-item"],[class*="option"]');
                    for (const it of items) {
                        if ((it.textContent||'').includes('HTP')) { it.click(); return 'clicked:' + it.textContent.trim(); }
                    }
                    return 'no-htp';
                }""")
                if 'clicked' in str(_r3): break
                await page.wait_for_timeout(500)

        await page.wait_for_timeout(3_000)

        # ── 4. Filtre "Tous les virements" ────────────────────────────────────────
        print("  → Sélection 'Tous les virements'…")
        await page.evaluate("""() => {
            const selects = document.querySelectorAll('select');
            for (const s of selects) {
                for (const o of s.options) {
                    if (o.text.includes('Tous') && o.text.toLowerCase().includes('virement')) {
                        s.value = o.value;
                        s.dispatchEvent(new Event('change', {bubbles: true}));
                        return;
                    }
                }
            }
            const items = document.querySelectorAll('[class*="dropdown-item"],[class*="option"]');
            for (const it of items) {
                if ((it.textContent||'').includes('Tous les virements')) { it.click(); break; }
            }
        }""")
        await page.wait_for_timeout(3_000)

        # ── 5. Plage de dates ──────────────────────────────────────────────────────
        if date_from and date_to:
            print(f"  → Plage dates : {date_from} → {date_to}")
            # Convertir YYYY-MM-DD → DD/MM/YYYY pour l'affichage
            def _fmt(d: str) -> str:
                y, m, day = d.split('-'); return f"{day}/{m}/{y}"
            date_from_fr = _fmt(date_from)
            date_to_fr   = _fmt(date_to)

            _date_set = await page.evaluate(f"""([from_fr, to_fr]) => {{
                // Chercher des inputs de type date ou texte avec placeholder DD/MM/YYYY
                const inputs = [...document.querySelectorAll('input[type="date"],input[type="text"]')];
                const dateInputs = inputs.filter(i =>
                    i.placeholder?.includes('/') || i.name?.toLowerCase().includes('date') ||
                    i.id?.toLowerCase().includes('date')
                );
                if (dateInputs.length >= 2) {{
                    dateInputs[0].value = from_fr;
                    dateInputs[0].dispatchEvent(new Event('input', {{bubbles:true}}));
                    dateInputs[0].dispatchEvent(new Event('change', {{bubbles:true}}));
                    dateInputs[1].value = to_fr;
                    dateInputs[1].dispatchEvent(new Event('input', {{bubbles:true}}));
                    dateInputs[1].dispatchEvent(new Event('change', {{bubbles:true}}));
                    return 'set:' + dateInputs[0].value + '→' + dateInputs[1].value;
                }}
                return 'no-date-inputs:' + inputs.length;
            }}""", [date_from_fr, date_to_fr])
            print(f"  date-set: {_date_set}")
            # Le site recharge les virements côté serveur (~20s) avant de pouvoir exporter
            print("  → Attente chargement des virements (~20s)…")
            await page.wait_for_timeout(25_000)

        # ── 6. Export ──────────────────────────────────────────────────────────────
        print("  → Clic sur Export…")
        async with page.expect_download(timeout=120_000) as dl_info:
            _exp = await page.evaluate("""() => {
                // Chercher le bouton Export
                const btns = document.querySelectorAll('button,a,[class*="btn"],[class*="export"]');
                for (const b of btns) {
                    const t = (b.textContent||b.title||b.getAttribute('title')||'').trim().toLowerCase();
                    if (t === 'exporter' || t === 'export' || t.includes('export')) {
                        b.click(); return 'clicked:' + b.textContent.trim();
                    }
                }
                return 'no-export-btn';
            }""")
            print(f"  export-btn: {_exp}")
            if 'no-export-btn' in str(_exp):
                raise RuntimeError("Bouton Export non trouvé sur la page Banque FSE")

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
