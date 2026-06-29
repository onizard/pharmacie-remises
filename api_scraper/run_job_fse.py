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


def _parse_and_save_fse(xlsx_bytes: bytes) -> dict:
    """Parse le XLSX FSE Banque LOCALEMENT (sans Render, sans Storage) et
    sauvegarde fse_month_stats dans user_state via la clé de service.

    Port autonome de _parse_fse_bank_sync (main.py) : le runner GitHub Actions
    n'installe que camoufox+openpyxl (pas fastapi), donc on ne peut pas importer
    main.py ; et l'API Render rejette la clé de service (verify_token → 401).
    """
    import io, re as _re
    import openpyxl

    _LABO_KEYS = [
        ("BIOGARAN", "BIOGARAN"), ("TEVA", "TEVA"), ("VIATRIS", "VIATRIS"),
        ("MYLAN", "VIATRIS"), ("SANDOZ", "SANDOZ"), ("ZENTIVA", "ZENTIVA"),
        ("ARROW", "ARROW"), ("CRISTERS", "CRISTERS"), ("ZYDUS", "ZYDUS"),
        ("EG LABO", "EG LABO"), ("EG LABS", "EG LABO"), ("EVOLUPHARM", "EVOLUPHARM"),
        ("RANBAXY", "RANBAXY"), ("AUROBINDO", "AUROBINDO"), ("INTAS", "INTAS"),
        ("ALMUS", "ALMUS"), ("QUALIMED", "QUALIMED"),
        ("MOVIANTO", "MOVIANTO"), ("ALLOGA", "ALLOGA"), ("CEGEDIM", "CEGEDIM"),
        ("CERP", "CERP"), ("COOPERATION PHARMACEUTIQUE", "CERP"), ("CPF", "CERP"),
    ]

    def _identify_labo(libelle):
        lib = libelle.upper().strip()
        m = _re.match(r'VIR\s+(.+?)\s+-\s', lib)
        if not m:
            for key, canon in _LABO_KEYS:
                if key in lib:
                    return canon
            return None
        name_part = m.group(1)
        for key, canon in _LABO_KEYS:
            if name_part.startswith(key) or key in name_part:
                return canon
        return None

    def _extract_all_refs(libelle):
        return [s for s in _re.findall(r'\b(\d{8,14})\b', libelle) if len(s) <= 14]

    def _classify_transfer(libelle, ref):
        text = (libelle + ' ' + ref).upper()
        r2_kw = ['RDP', ' R2 ', '-R2-', 'R2-', '-R2', 'REMISE FIN', 'AVOIR', 'REDUCTI', 'RED FIN']
        r3_kw = ['PRESTA', 'COOP', ' R3 ', '-R3-', 'R3-', '-R3', 'PREST', 'COOPERAT', 'PRESTAT']
        if any(kw in text for kw in r2_kw):
            return 'r2'
        if any(kw in text for kw in r3_kw):
            return 'r3'
        return 'r3'

    def _parse_date(val):
        if isinstance(val, (_dt_module.date, _dt_module.datetime)):
            return val.strftime("%Y-%m-%d")
        s = str(val or "").strip()
        m = _re.match(r'(\d{2})/(\d{2})/(\d{4})', s)
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None

    def _parse_amount(val):
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val or "").replace('\xa0', '').replace(' ', '').replace('€', '').replace(',', '.').strip()
        try:
            return float(s) if s else None
        except ValueError:
            return None

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(max_row=5000, values_only=True))

    # ── Détection des colonnes ─────────────────────────────────────────────────
    _DATE_KW = ('date',)
    _LIB_KW  = ('libell', 'libellé', 'description', 'opération', 'operation', 'désignation')
    _AMT_KW  = ('montant', 'crédit', 'credit', 'débit', 'debit', 'valeur', 'amount')
    hdr_idx  = -1
    col_date, col_lib, col_amt = 0, 1, 2

    for ri, row in enumerate(all_rows[:15]):
        cells = [str(c or '').lower().strip() for c in row]
        has_date = any(any(k in c for k in _DATE_KW) for c in cells)
        has_lib  = any(any(k in c for k in _LIB_KW)  for c in cells)
        if has_date and has_lib:
            hdr_idx = ri
            for ci, c in enumerate(cells):
                if any(k in c for k in _DATE_KW) and 'mise' not in c and 'update' not in c:
                    col_date = ci
                if any(k in c for k in _LIB_KW):
                    col_lib = ci
                if any(k in c for k in _AMT_KW):
                    col_amt = ci
            break

    if hdr_idx < 0:
        for ri, row in enumerate(all_rows[:30]):
            for ci, c in enumerate(row):
                s = str(c or '').upper().strip()
                if s.startswith('VIR ') and len(s) > 6:
                    hdr_idx  = max(0, ri - 1)
                    col_lib  = ci
                    col_date = max(0, ci - 1)
                    col_amt  = ci + 1
                    break
            if hdr_idx >= 0:
                break

    data_start = hdr_idx + 2

    # ── Lookup ref/montant → labo depuis digi_month_stats ──────────────────────
    state_pre   = _supa_get_state()
    _digi_stats = state_pre.get("digi_month_stats") or {}
    ref_to_labo, ref_to_type = {}, {}
    amount_to_candidates: dict = {}
    for _mk_d, _arr in _digi_stats.items():
        for _row in (_arr or []):
            _labo_d = _row.get("labo", "")
            for _ref in (_row.get("facture_refs") or []):
                _ref_s = str(_ref).strip()
                if _ref_s and len(_ref_s) >= 6:
                    ref_to_labo[_ref_s] = _labo_d
                    ref_to_type[_ref_s] = 'r2' if (_row.get("rdp_total") or 0) > 0 else 'r3'
            for _field, _typ in [("presta_total_ttc", "r3"), ("rdp_total", "r2")]:
                _amt = _row.get(_field) or 0
                if _amt > 0:
                    amount_to_candidates.setdefault(round(_amt * 100), []).append((_labo_d, _mk_d, _typ))
    print(f"  → Lookup factures : {len(ref_to_labo)} refs · {len(amount_to_candidates)} montants (digi)")

    # ── Parcours des lignes ────────────────────────────────────────────────────
    acc: dict = {}
    row_num = n_ref_matched = n_amt_matched = 0
    skipped_lib: list = []

    for row in all_rows[data_start:]:
        if not any(c for c in row):
            continue
        n = len(row)
        date_str = _parse_date(row[col_date] if col_date < n else None)
        libelle  = str(row[col_lib] if col_lib < n else '') or ''
        amount   = _parse_amount(row[col_amt] if col_amt < n else None)
        if (amount is None or amount <= 0) and col_amt + 1 < n:
            amount = _parse_amount(row[col_amt + 1])
        if not date_str or not libelle:
            continue
        if amount is None or amount <= 0:
            continue
        if not libelle.upper().strip().startswith('VIR '):
            continue

        mk    = date_str[:7]
        labo  = _identify_labo(libelle)
        refs  = _extract_all_refs(libelle)
        vtype = _classify_transfer(libelle, refs[0] if refs else "")

        if not labo and refs:
            for _r in refs:
                if _r in ref_to_labo:
                    labo  = ref_to_labo[_r]
                    vtype = ref_to_type.get(_r, vtype)
                    n_ref_matched += 1
                    break

        if not labo:
            _cands = amount_to_candidates.get(round(amount * 100))
            if _cands:
                _match = next((c for c in _cands if c[1] == mk), _cands[0])
                labo, vtype = _match[0], _match[2]
                n_amt_matched += 1

        if not labo:
            if len(skipped_lib) < 20:
                skipped_lib.append(libelle[:80])
            continue

        for key, canon in _LABO_KEYS:
            if key in labo.upper():
                labo = canon
                break

        acc.setdefault(mk, {}).setdefault(
            labo, {"montant_ttc": 0.0, "r2_ttc": 0.0, "r3_ttc": 0.0, "count": 0, "refs": []})
        acc[mk][labo]["montant_ttc"] += amount
        acc[mk][labo]["r2_ttc"]      += amount if vtype == 'r2' else 0.0
        acc[mk][labo]["r3_ttc"]      += amount if vtype == 'r3' else 0.0
        acc[mk][labo]["count"]       += 1
        for _r in refs[:3]:
            if _r not in acc[mk][labo]["refs"]:
                acc[mk][labo]["refs"].append(_r)
        row_num += 1

    print(f"  → {row_num} virements parsés · {n_ref_matched} via ref · {n_amt_matched} via montant")
    if skipped_lib:
        print(f"  → Libellés non reconnus : {skipped_lib[:5]}")

    if not acc:
        sample = [[str(c)[:25] for c in r if c]
                  for r in all_rows[max(0, hdr_idx):hdr_idx + 5] if any(c for c in r)]
        raise RuntimeError(
            f"Aucun virement reconnu. Colonnes date={col_date} libellé={col_lib} "
            f"montant={col_amt} header={hdr_idx}. Échantillon={sample[:3]}")

    fse_stats = {
        mk: {labo: {
            "montant_ttc": round(d["montant_ttc"], 2),
            "r2_ttc":      round(d["r2_ttc"], 2),
            "r3_ttc":      round(d["r3_ttc"], 2),
            "count":       d["count"],
            "refs":        d["refs"][:10],
        } for labo, d in labos.items()}
        for mk, labos in sorted(acc.items())
    }

    # ── Fusion avec l'état existant + sauvegarde (clé de service) ──────────────
    state    = _supa_get_state()
    existing = state.get("fse_month_stats") or {}
    merged: dict = {}
    for mk, lab_data in existing.items():
        merged[mk] = ({r["labo"]: dict(r) for r in lab_data}
                      if isinstance(lab_data, list) else dict(lab_data))
    for mk, new_labos in fse_stats.items():
        if mk not in merged:
            merged[mk] = new_labos
        else:
            for labo, nd in new_labos.items():
                if labo in merged[mk]:
                    ex = merged[mk][labo]
                    ex["montant_ttc"] = round(ex.get("montant_ttc", 0) + nd["montant_ttc"], 2)
                    ex["r2_ttc"]      = round(ex.get("r2_ttc", 0) + nd["r2_ttc"], 2)
                    ex["r3_ttc"]      = round(ex.get("r3_ttc", 0) + nd["r3_ttc"], 2)
                    ex["count"]      += nd["count"]
                    ex["refs"]        = (ex.get("refs", []) + nd["refs"])[:20]
                else:
                    merged[mk][labo] = dict(nd)
    state["fse_month_stats"] = merged
    _supa_patch_state(state)

    total_ttc = sum(d["montant_ttc"] for labos in fse_stats.values() for d in labos.values())
    return {"months": sorted(fse_stats), "rows_parsed": row_num, "total_ttc": round(total_ttc, 2)}


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

        # ── 2. Module Banque avec dates via params de route ───────────────────────
        # datedebut/datefin (YYYY-MM-DD) → plage du daterangepicker (lue par config).
        # L'origine est mise à HTP+OI ensuite via le richselect DOM (le param htp
        # ne filtrait pas → run #10 exportait du tiers-payant : MGEN, CETIP…).
        print(f"  → Module Banque ({date_from} → {date_to})…")
        # Chargement FRAIS de la route Banque avec les params (cache-buster ?bp=1
        # pour forcer un vrai reload → config() lit getParam('datedebut'/'datefin')
        # au boot ; le hashchange seul ne ré-exécutait pas config() → dates par
        # défaut au run #11). Le ?bp=1 n'est pas ?code= donc me() ré-authentifie
        # via cookie sans re-login.
        _url = (f"{FSE_URL}/?bp=1#!/top/manager.fse.bank"
                f"?datedebut={date_from}&datefin={date_to}")
        await page.goto(_url, wait_until="domcontentloaded", timeout=90_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
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
        await page.wait_for_timeout(3_000)
        _before = await page.evaluate("""() => ({
            origin: (document.querySelector('[view_id="fse_bank_origin"]')||{}).innerText,
            date:   (document.querySelector('[view_id="fse_bank_date"]')||{}).innerText
        })""")
        print(f"  ✓ Module monté — {_before}")

        # ── 2b. Origine = HTP + OI via le richselect DOM ──────────────────────────
        _open = await page.evaluate("""() => {
            const el = document.querySelector('[view_id="fse_bank_origin"]');
            if (!el) return 'no-origin';
            (el.querySelector('.webix_inp_static') || el.querySelector('input') || el).click();
            return 'opened';
        }""")
        await page.wait_for_timeout(1_000)
        _pick = await page.evaluate("""() => {
            const items = document.querySelectorAll('.webix_list_item');
            const opts = [...items].map(i => (i.textContent||'').trim());
            for (const it of items) {
                const t = (it.textContent||'').trim();
                if (t === 'HTP + OI' || t === 'HTP+OI') { it.click(); return 'picked:HTPOI'; }
            }
            for (const it of items) {
                if ((it.textContent||'').trim() === 'Hors tiers-payant') {
                    it.click(); return 'picked:HTP';
                }
            }
            return 'not-found:' + opts.slice(0, 10).join('|');
        }""")
        print(f"  origin richselect: {_open}/{_pick}")
        if str(_pick).startswith('not-found'):
            # fermer un éventuel popup et continuer (origine par défaut = TP : on
            # tentera quand même l'export, mais on signale le problème)
            await page.keyboard.press("Escape")

        # Laisser le datatable recharger les virements côté serveur avant l'export.
        print("  → Rechargement des virements…")
        await page.wait_for_timeout(15_000)
        _after = await page.evaluate("""() => ({
            origin: (document.querySelector('[view_id="fse_bank_origin"]')||{}).innerText,
            rows: document.querySelectorAll('[view_id="fse_bank_datatable"] .webix_cell').length
        })""")
        print(f"  → après filtre: {_after}")

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

    # Archivage best-effort vers Storage (non bloquant — le tunnel MinIO peut
    # renvoyer 502 ; le parsing se fait de toute façon localement).
    try:
        _upload_xlsx_to_storage(xlsx_bytes, filename)
    except Exception as e:
        print(f"  [warn] Upload Storage ignoré ({e})")

    # Parser le XLSX LOCALEMENT et sauvegarder fse_month_stats (clé de service).
    _update_job("running", "Parsing du XLSX…")
    try:
        result_data = _parse_and_save_fse(xlsx_bytes)
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
