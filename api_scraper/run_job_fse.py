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
    """Archive le XLSX dans la table fse_files (base64, PostgREST) — même pattern
    que digi_files. L'ancien POST « façon Supabase Storage » vers /storage/v1/
    échouait TOUJOURS (HTTP 400) : le stockage self-hosted est du MinIO S3 brut,
    inaccessible avec un JWT et sans clés S3 côté GitHub Actions. Ces relevés
    sont des pièces du litige labo → archivés en base, visibles dans
    l'explorateur. Best-effort : un échec n'interrompt pas le job."""
    import base64 as _b64
    safe_name = filename.replace(" ", "_")
    yyyymm    = "".join(ch for ch in safe_name if ch.isdigit())[:6]
    body = json.dumps({
        "user_id":     USER_ID,
        "storage_key": safe_name,
        "filename":    safe_name,
        "yyyymm":      yyyymm,
        "content_b64": _b64.b64encode(xlsx_bytes).decode(),
    }).encode()
    url = f"{SUPA_URL}/rest/v1/fse_files?on_conflict=user_id,storage_key"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey":        SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    })
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
        print(f"  ✓ Archivé en base : {safe_name}")
    except Exception as e:
        print(f"  [warn] Archivage fse_files échoué ({e}) — on parsera quand même localement")
    return safe_name


def _parse_and_save_fse(xlsx_bytes_list: list) -> dict:
    """Parse une LISTE de XLSX FSE Banque (une par fenêtre de dates) LOCALEMENT
    et sauvegarde fse_month_stats dans user_state via la clé de service.

    Les fenêtres se chevauchent (≤120 j) : on dédoublonne les virements par
    (date, libellé, montant). L'agrégat remplace fse_month_stats (run complet).

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
        # Numéros purement numériques (8-14 chiffres) + tokens alphanumériques de
        # facture (ex. "24BO24121001288") — les n° de facture labo ne sont pas
        # toujours purement numériques.
        refs = [s for s in _re.findall(r'\b(\d{8,14})\b', libelle) if len(s) <= 14]
        for tok in _re.findall(r'\b([A-Z0-9]{8,20})\b', libelle.upper()):
            if any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok):
                refs.append(tok)
        return refs

    def _classify_transfer(libelle, ref, amount=0.0):
        # Règle principale (fiable pour Biogaran) : les virements COOP sont des sommes
        # RONDES (sans centimes) ; dès qu'il y a des décimales, c'est de la RDP
        # (CA brut × taux → quasi toujours des centimes).
        cents = int(round(abs(float(amount or 0)) * 100)) % 100
        if cents != 0:
            return 'r2'
        # Somme ronde → coop par défaut ; un mot-clé RDP explicite peut la basculer.
        text = (libelle + ' ' + ref).upper()
        if any(kw in text for kw in ('RDP', ' R2 ', '-R2-', 'R2-', '-R2')):
            return 'r2'
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

    # ── Lookup ref/montant → labo depuis digi_month_stats (global) ─────────────
    state_pre   = _supa_get_state()
    _digi_stats = state_pre.get("digi_month_stats") or {}
    ref_to_labo, ref_to_type = {}, {}
    norm_ref_index: list = []   # [(norm_ref, labo, type)] — n° facture normalisé
    amount_to_candidates: dict = {}
    for _mk_d, _arr in _digi_stats.items():
        for _row in (_arr or []):
            _labo_d = _row.get("labo", "")
            for _ref in (_row.get("facture_refs") or []):
                _ref_s = str(_ref).strip()
                if _ref_s and len(_ref_s) >= 6:
                    _typ_r = 'r2' if (_row.get("rdp_total") or 0) > 0 else 'r3'
                    ref_to_labo[_ref_s] = _labo_d
                    ref_to_type[_ref_s] = _typ_r
                    _nr = _re.sub(r'[^A-Z0-9]', '', _ref_s.upper())
                    if len(_nr) >= 7:
                        norm_ref_index.append((_nr, _labo_d, _typ_r))
            for _field, _typ in [("presta_total_ttc", "r3"), ("rdp_total", "r2")]:
                _amt = _row.get(_field) or 0
                if _amt > 0:
                    amount_to_candidates.setdefault(round(_amt * 100), []).append((_labo_d, _mk_d, _typ))
    # Les n° les plus longs d'abord → on apparie le match le plus spécifique.
    norm_ref_index.sort(key=lambda x: -len(x[0]))
    print(f"  → Lookup factures : {len(ref_to_labo)} refs ({len(norm_ref_index)} normalisés) · {len(amount_to_candidates)} montants (digi)")

    _DATE_KW = ('date',)
    _LIB_KW  = ('libell', 'libellé', 'description', 'opération', 'operation', 'désignation')
    _AMT_KW  = ('montant', 'crédit', 'credit', 'débit', 'debit', 'valeur', 'amount')

    # ── Agrégation sur l'ensemble des fenêtres (dédoublonnage global) ──────────
    acc: dict = {}
    seen: set = set()
    row_num = n_ref_matched = n_amt_matched = n_dup = 0
    skipped_lib: list = []
    _last_sample: list = []

    for _xlsx in xlsx_bytes_list:
        wb = openpyxl.load_workbook(io.BytesIO(_xlsx), read_only=True, data_only=True)
        ws = wb.active
        all_rows = list(ws.iter_rows(max_row=5000, values_only=True))

        # Détection des colonnes (par fichier).
        hdr_idx = -1
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
                        hdr_idx, col_lib = max(0, ri - 1), ci
                        col_date, col_amt = max(0, ci - 1), ci + 1
                        break
                if hdr_idx >= 0:
                    break
        _last_sample = [[str(c)[:25] for c in r if c]
                        for r in all_rows[max(0, hdr_idx):hdr_idx + 5] if any(c for c in r)]

        for row in all_rows[hdr_idx + 2:]:
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
            # Virements uniquement — TOLÉRANT sur le préfixe : « VIR  », mais
            # aussi « VIREMENT SEPA … », « VIRT », « VIR. » (cas réel : le virement
            # Biogaran du 29/10/2025 de 1 396,29 € était absent des stats car son
            # libellé ne commençait pas par « VIR<espace> »).
            if not libelle.upper().strip().startswith('VIR'):
                # Filet de sécurité PAR SOMME/N° DE DOCUMENT : une ligne au libellé
                # inhabituel (banque qui met le nom en premier, « SEPA … ») est
                # quand même gardée si (a) son libellé contient un n° de facture/
                # avoir Digi connu, ou (b) son montant correspond AU CENTIME à un
                # avoir connu — uniquement pour les montants À DÉCIMALES (1 396,29 :
                # collision quasi impossible ; les sommes rondes, plus banales,
                # exigent le n° de document pour éviter les faux positifs).
                _nl = _re.sub(r'[^A-Z0-9]', '', libelle.upper())
                _known_ref = bool(norm_ref_index) and any(_nr in _nl for _nr, _, _ in norm_ref_index)
                _cents_ok  = round(amount * 100) % 100 != 0 and round(amount * 100) in amount_to_candidates
                if not _known_ref and not _cents_ok:
                    continue

            # Dédoublonnage inter-fenêtres.
            _key = (date_str, libelle.strip()[:80], round(amount * 100))
            if _key in seen:
                n_dup += 1
                continue
            seen.add(_key)

            mk    = date_str[:7]
            labo  = _identify_labo(libelle)
            refs  = _extract_all_refs(libelle)
            vtype = _classify_transfer(libelle, refs[0] if refs else "", amount)

            if not labo and refs:
                for _r in refs:
                    if _r in ref_to_labo:
                        labo  = ref_to_labo[_r]
                        vtype = ref_to_type.get(_r, vtype)
                        n_ref_matched += 1
                        break

            # Fallback 1b : n° de facture Digi présent en sous-chaîne du libellé
            # normalisé (le n° est souvent noyé dans le libellé du virement, ex.
            # ".../INV/24BO24121001288..."), ciblage par numéro de facture.
            if not labo and norm_ref_index:
                _norm_lib = _re.sub(r'[^A-Z0-9]', '', libelle.upper())
                for _nref, _nlabo, _ntyp in norm_ref_index:
                    if _nref in _norm_lib:
                        labo, vtype = _nlabo, _ntyp
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

            # Garde-fou final (règle métier) : un virement à somme RONDE sans
            # mot-clé RDP explicite est de la coop (r3), même si un lookup par
            # n° de facture a dit r2 — facture_refs mélange les refs RDP et
            # presta d'un même mois, donc son type par ligne est peu fiable
            # (ex. réel : virements coop CSP 6540/2760/4140/3720 € classés r2).
            # Décimales ⇒ RDP reste inchangé.
            if vtype == 'r2' and round(amount * 100) % 100 == 0 \
                    and 'RDP' not in libelle.upper():
                vtype = 'r3'

            for key, canon in _LABO_KEYS:
                if key in labo.upper():
                    labo = canon
                    break

            acc.setdefault(mk, {}).setdefault(
                labo, {"montant_ttc": 0.0, "r2_ttc": 0.0, "r3_ttc": 0.0,
                       "count": 0, "refs": [], "transfers": []})
            acc[mk][labo]["montant_ttc"] += amount
            acc[mk][labo]["r2_ttc"]      += amount if vtype == 'r2' else 0.0
            acc[mk][labo]["r3_ttc"]      += amount if vtype == 'r3' else 0.0
            acc[mk][labo]["count"]       += 1
            for _r in refs[:3]:
                if _r not in acc[mk][labo]["refs"]:
                    acc[mk][labo]["refs"].append(_r)
            # Détail virement par virement (pour le rapprochement RDP côté front).
            # Borné pour ne pas gonfler user_state ; libellé tronqué.
            if len(acc[mk][labo]["transfers"]) < 200:
                acc[mk][labo]["transfers"].append({
                    "date":    date_str,
                    "lib":     libelle.strip()[:90],
                    "montant": round(amount, 2),
                    "vtype":   vtype,
                    "refs":    refs[:3],
                })
            row_num += 1

    print(f"  → {row_num} virements parsés · {n_ref_matched} via ref · "
          f"{n_amt_matched} via montant · {n_dup} doublons écartés")
    if skipped_lib:
        print(f"  → Libellés non reconnus : {skipped_lib[:5]}")

    if not acc:
        raise RuntimeError(
            f"Aucun virement reconnu sur {len(xlsx_bytes_list)} fenêtre(s). "
            f"Échantillon dernière fenêtre={_last_sample[:3]}")

    fse_stats = {
        mk: {labo: {
            "montant_ttc": round(d["montant_ttc"], 2),
            "r2_ttc":      round(d["r2_ttc"], 2),
            "r3_ttc":      round(d["r3_ttc"], 2),
            "count":       d["count"],
            "refs":        d["refs"][:10],
            "transfers":   d.get("transfers", []),
        } for labo, d in labos.items()}
        for mk, labos in sorted(acc.items())
    }

    # ── Sauvegarde (clé de service) ────────────────────────────────────────────
    # Le run couvre toute la période demandée avec dédoublonnage → on REMPLACE
    # les mois présents dans ce run (pas de fusion additive qui doublonnerait
    # entre exécutions), tout en préservant d'éventuels mois hors période.
    state    = _supa_get_state()
    existing = state.get("fse_month_stats") or {}
    merged: dict = {}
    for mk, lab_data in existing.items():
        merged[mk] = ({r["labo"]: dict(r) for r in lab_data}
                      if isinstance(lab_data, list) else dict(lab_data))
    for mk, new_labos in fse_stats.items():
        merged[mk] = new_labos  # remplacement complet du mois
    state["fse_month_stats"] = merged
    _supa_patch_state(state)

    total_ttc = sum(d["montant_ttc"] for labos in fse_stats.values() for d in labos.values())
    return {"months": sorted(fse_stats), "rows_parsed": row_num, "total_ttc": round(total_ttc, 2)}


def _month_list(date_from: str, date_to: str) -> list:
    """Liste des mois 'YYYYMM' de date_from à date_to inclus. Le module Banque
    accepte un param de route `date=YYYYMM` qui fait un setValue explicite du
    daterangepicker sur ce mois (start=1er, end=dernier jour) — seul moyen fiable
    de fixer la plage (datedebut/datefin ne s'appliquent pas, plafond ~120 j)."""
    d_from = _dt_module.date.fromisoformat(date_from)
    d_to   = _dt_module.date.fromisoformat(date_to)
    months: list = []
    y, m = d_from.year, d_from.month
    while (y, m) <= (d_to.year, d_to.month):
        months.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


# ── Scraper Playwright/camoufox ────────────────────────────────────────────────

async def _scrape_fse_async(creds: dict, date_from: str, date_to: str) -> list:
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

        # ── 2. Export mois par mois (param de route date=YYYYMM) ──────────────────
        # Le daterangepicker FSE plafonne la plage à ~120 j et ignore datedebut/
        # datefin (run #13 : toutes les fenêtres retombaient sur les 120 j par
        # défaut). En revanche le module Banque lit `date=YYYYMM` et fait un
        # setValue EXPLICITE du picker sur ce mois → on exporte mois par mois.
        months = _month_list(date_from, date_to)
        print(f"  → {len(months)} mois à exporter : {months}")

        async def _export_month(idx: int, yyyymm: str) -> bytes:
            # Chargement FRAIS de la route Banque (cache-buster unique ; ?cb=… n'est
            # pas ?code= donc me() ré-authentifie via cookie sans re-login).
            # rappnonrapp=1 → type "rapprochés + non rapprochés".
            url = (f"{FSE_URL}/?cb={idx}#!/top/manager.fse.bank"
                   f"?date={yyyymm}&rappnonrapp=1")
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
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
                await page.screenshot(path=f"fse_bank_debug_{idx}.png")
                raise RuntimeError(f"Module Banque non monté (mois {yyyymm})")
            await page.wait_for_timeout(3_000)

            # Origine = HTP + OI via le richselect DOM.
            await page.evaluate("""() => {
                const el = document.querySelector('[view_id="fse_bank_origin"]');
                if (el) (el.querySelector('.webix_inp_static') || el.querySelector('input') || el).click();
            }""")
            await page.wait_for_timeout(1_000)
            _pick = await page.evaluate("""() => {
                const items = document.querySelectorAll('.webix_list_item');
                for (const it of items) {
                    const t = (it.textContent||'').trim();
                    if (t === 'HTP + OI' || t === 'HTP+OI') { it.click(); return 'HTPOI'; }
                }
                for (const it of items) {
                    if ((it.textContent||'').trim() === 'Hors tiers-payant') { it.click(); return 'HTP'; }
                }
                return 'not-found';
            }""")
            if str(_pick) == 'not-found':
                await page.keyboard.press("Escape")
            await page.wait_for_timeout(12_000)
            _diag = await page.evaluate("""() => ({
                date: (document.querySelector('[view_id="fse_bank_date"]')||{}).innerText,
                rows: document.querySelectorAll('[view_id="fse_bank_datatable"] .webix_cell').length
            })""")
            print(f"  [{yyyymm}] origin={_pick} {_diag}")

            async with page.expect_download(timeout=120_000) as dl_info:
                _exp = await page.evaluate("""() => {
                    const cands = document.querySelectorAll(
                        'button, .webix_button, .webix_el_button, [role="button"]');
                    for (const b of cands) {
                        const t = (b.textContent || b.value || '').trim();
                        if (t === 'Exporter' || t === 'Exporter ') {
                            (b.querySelector('button') || b).click(); return 'clicked';
                        }
                    }
                    for (const b of cands) {
                        if ((b.textContent || '').trim().includes('Export')) {
                            (b.querySelector('button') || b).click(); return 'clicked-loose';
                        }
                    }
                    return 'no-export-btn';
                }""")
                if 'no-export-btn' in str(_exp):
                    await page.screenshot(path=f"fse_export_debug_{idx}.png")
                    raise RuntimeError(f"Bouton Exporter introuvable (mois {yyyymm})")
            download = await dl_info.value
            tmp_path = Path(f"/tmp/fse_bank_{idx}.xlsx")
            await download.save_as(tmp_path)
            data = tmp_path.read_bytes()
            print(f"  [{yyyymm}] ✓ {len(data)} bytes")
            return data

        all_bytes: list = []
        for _i, _mm in enumerate(months):
            all_bytes.append(await _export_month(_i, _mm))
        return all_bytes


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
        xlsx_list = asyncio.run(_scrape_fse_async(creds, DATE_FROM, DATE_TO))
    except Exception as e:
        _update_job("error", error=str(e))
        print(f"❌  Scraping échoué : {e}")
        sys.exit(1)

    # Archivage best-effort en base (table fse_files — non bloquant ; le parsing
    # se fait de toute façon localement). Un fichier par mois exporté, nommé par
    # son mois (l'export est mois par mois, dans l'ordre de _month_list).
    _mois = _month_list(DATE_FROM, DATE_TO)
    for _i, _xb in enumerate(xlsx_list):
        try:
            _mm = _mois[_i] if _i < len(_mois) else str(_i)
            _upload_xlsx_to_storage(_xb, f"BanqueOspharmFSE_{_mm}.xlsx")
        except Exception as e:
            print(f"  [warn] Archivage ignoré ({e})")

    # Parser les XLSX LOCALEMENT et sauvegarder fse_month_stats (clé de service).
    _update_job("running", "Parsing des XLSX…")
    try:
        result_data = _parse_and_save_fse(xlsx_list)
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
