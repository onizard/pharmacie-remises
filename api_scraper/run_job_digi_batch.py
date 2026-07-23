"""
run_job_digi_batch.py — Traitement EN FILE des avoirs Digi déposés par l'utilisateur.

Tourne sur GitHub Actions (runner toujours actif) → le traitement continue même si
l'utilisateur ferme son navigateur. Déclenché par le backend Render (dispatch), avec :

    USER_ID               (input du workflow) — l'utilisateur concerné
    SUPABASE_SERVICE_KEY  (GitHub Secret)     — clé service_role (bypass RLS)

Principe :
  1. lit les lignes digi_files en statut 'pending' pour cet utilisateur ;
  2. parse chaque PDF un par un (mêmes parsers que /parse/digi-pdf) ;
  3. fusionne digi_month_stats / escompte_stats / mdl_stats dans user_state ;
  4. passe chaque ligne en 'done' (avec les mois couverts) ou 'error' ;
  5. écrit la progression dans user_state.digi_batch_job à chaque fichier
     → la barre de progression du site la lit par polling.
"""
import os
import re
import sys
import json
import time
import base64
import tempfile
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from supabase_client import SUPA_URL, _supa_key, _get_state_sync, _patch_state_sync
from pdf_extractor import extract_invoice_lines
from run_job import (_compute_digi_month_stats, _merge_digi_stats,
                     _compute_escompte_stats, _merge_escompte_stats,
                     _compute_mdl_stats, _merge_mdl_stats, _norm_labo)

USER_ID = os.environ.get("USER_ID", "").strip()


# ── Accès digi_files (clé service_role → bypass RLS) ──────────────────────────
def _digi_pending(user_id: str) -> list:
    key = _supa_key()
    def _fetch(sel):
        url = (f"{SUPA_URL}/rest/v1/digi_files?user_id=eq.{user_id}&status=eq.pending"
               f"&select={sel}&order=id.asc")
        req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    try:
        return _fetch("id,filename,content_b64,source_url")
    except urllib.error.HTTPError:
        # Colonne source_url absente (migration non lancée) → repli sans elle.
        return _fetch("id,filename,content_b64")


def _download(url: str) -> bytes:
    """Télécharge un PDF depuis son URL (avoirs/factures Digi = URLs publiques/S3)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read()


def _digi_update(row_id, patch: dict):
    key = _supa_key()
    url = f"{SUPA_URL}/rest/v1/digi_files?id=eq.{row_id}"
    body = json.dumps(patch).encode()
    req = urllib.request.Request(url, data=body, method="PATCH", headers={
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    })
    with urllib.request.urlopen(req, timeout=30):
        pass


def _digi_done_no_kinds(user_id: str) -> list:
    """Avoirs déjà traités mais sans catégorie (kinds vide) → à ré-indexer
    (corriger months = période + kinds)."""
    key = _supa_key()
    url = (f"{SUPA_URL}/rest/v1/digi_files?user_id=eq.{user_id}&status=eq.done"
           f"&kinds=eq.%7B%7D&select=id,filename,content_b64&order=id.asc")
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _derive_months_kinds(lines: list):
    """Mois de rattachement (période pour rdp/presta) + catégories du PDF."""
    def _mois(l):
        if l.get("type") in ("rdp", "presta") and l.get("period_month"):
            return str(l["period_month"])[:7]
        return str(l.get("billing_date", ""))[:7]
    months = sorted({_mois(l) for l in lines if _mois(l)})
    kinds = []
    if any(l.get("type") == "rdp" for l in lines):    kinds.append("rdp")
    if any(l.get("type") == "presta" for l in lines): kinds.append("presta")
    if any(l.get("type") == "escompte" for l in lines): kinds.append("escompte")
    if any(l.get("type") == "mdl" for l in lines):      kinds.append("mdl")
    if any(l.get("type") not in ("rdp", "presta", "escompte", "mdl") for l in lines):
        kinds.append("product")
    # Tag du/des labo(s) présents (« labo:ZYDUS ») → permet au front de ranger la
    # facture sous le bon labo. Indispensable pour les factures CSP, qui facturent
    # « d'ordre et pour compte » de labos différents (Biogaran, Zydus…) : le nom de
    # fichier « csp_… » ne dit pas duquel il s'agit, seul le contenu le révèle.
    for lab in sorted({_norm_labo(l.get("labo") or l.get("fournisseur") or "") for l in lines}):
        if lab:
            kinds.append("labo:" + lab)
    return months, (kinds or ["product"])


def _persist(user_id: str, acc: dict, job: dict):
    """Relit l'état FRAIS (préserve ce que le frontend aurait écrit entre-temps),
    puis n'écrase que les clés du runner : stats Digi + statut du job."""
    fresh = _get_state_sync(user_id) or {}
    if acc.get("digi") is not None:     fresh["digi_month_stats"] = acc["digi"]
    if acc.get("esc")  is not None:     fresh["escompte_stats"]   = acc["esc"]
    if acc.get("mdl")  is not None:     fresh["mdl_stats"]        = acc["mdl"]
    job["updated_at"] = int(time.time() * 1000)
    fresh["digi_batch_job"] = job
    _patch_state_sync(user_id, fresh)


# ── Parse d'un PDF (même logique que /parse/digi-pdf) ─────────────────────────
def _parse_one(pdf_bytes: bytes, filename: str):
    provider = filename.rsplit(".", 1)[0][:80]
    billing_date = ""
    m = re.search(r'_(\d{2})(\d{2})(\d{4})(?:\.[Pp][Dd][Ff])?$', filename)
    if m:
        billing_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)
    try:
        lines = extract_invoice_lines(tmp_path, provider, billing_date)
    finally:
        tmp_path.unlink(missing_ok=True)
    return lines


def _reindex(user_id: str) -> int:
    """Ré-indexe les avoirs déjà stockés mais sans catégorie : re-parse chaque PDF
    et corrige months (= période de référence) + kinds. Ne touche PAS aux stats
    (déjà calculées au 1er dépôt). Passe unique : une fois kinds renseigné, exclu."""
    rows = _digi_done_no_kinds(user_id)
    if not rows:
        return 0
    print(f"→ ré-indexation de {len(rows)} avoir(s) existant(s)…", flush=True)
    n = 0
    for row in rows:
        try:
            pdf   = base64.b64decode(row.get("content_b64") or "")
            lines = _parse_one(pdf, row.get("filename") or "")
            if lines:
                months, kinds = _derive_months_kinds(lines)
                patch = {"kinds": kinds, "months": months}
            else:
                patch = {"kinds": ["product"]}   # illisible → marque pour ne pas reboucler
            _digi_update(row["id"], patch)
            n += 1
        except Exception as e:
            _digi_update(row["id"], {"kinds": ["product"]})
            print(f"  [warn] réindex {str(row.get('filename',''))[:40]} : {e}", flush=True)
    print(f"→ ré-indexation terminée : {n}", flush=True)
    return n


def _digi_errored(user_id: str) -> list:
    """Fichiers 'error' potentiellement récupérables — on EXCLUT les échecs permanents
    (contenu perdu / URL expirée, marqués « PDF indisponible »). Léger : sans contenu."""
    key = _supa_key()
    url = (f"{SUPA_URL}/rest/v1/digi_files?user_id=eq.{user_id}&status=eq.error"
           f"&error=not.ilike.*indisponible*&select=id,filename,source_url&order=id.asc")
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError:
        return []


def _retry_errored(user_id: str, acc: dict) -> int:
    """Re-parse les fichiers 'error' récupérables avec le parseur COURANT.
    SÛR : un fichier 'error' n'a jamais contribué aux stats (le statut est posé AVANT
    tout merge), donc le retraiter n'ajoute qu'UNE fois → aucun double comptage.
    Répare automatiquement les factures que d'anciennes versions du parseur rataient
    (ex. CSP Zydus : donneur d'ordre en pied de page → labo non reconnu → 0 ligne).
    Échec de téléchargement → sentinel « PDF indisponible » (exclu des tentatives
    suivantes, anti-boucle sur les contenus perdus)."""
    rows = _digi_errored(user_id)
    if not rows:
        return 0
    print(f"→ nouvelle tentative sur {len(rows)} fichier(s) en erreur…", flush=True)
    key, fixed = _supa_key(), 0
    for r in rows:
        try:
            curl = f"{SUPA_URL}/rest/v1/digi_files?id=eq.{r['id']}&select=content_b64"
            creq = urllib.request.Request(curl, headers={"apikey": key,
                                          "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(creq, timeout=60) as cr:
                crows = json.loads(cr.read())
            pdf_bytes = base64.b64decode((crows[0] if crows else {}).get("content_b64") or "")
            if not pdf_bytes and r.get("source_url"):
                try:
                    pdf_bytes = _download(r["source_url"])
                    if pdf_bytes:
                        _digi_update(r["id"], {"content_b64": base64.b64encode(pdf_bytes).decode()})
                except Exception:
                    pdf_bytes = b""
            if not pdf_bytes:
                _digi_update(r["id"], {"error": "PDF indisponible"})   # permanent → exclu ensuite
                continue
            lines = _parse_one(pdf_bytes, r.get("filename") or "")
            if not lines:
                continue   # toujours 0 ligne → reste 'error' (parapharmacie, etc.)
            esc  = [l for l in lines if l.get("type") == "escompte"]
            mdl  = [l for l in lines if l.get("type") == "mdl"]
            digi = [l for l in lines if l.get("type") not in ("escompte", "mdl")]
            if digi: acc["digi"] = _merge_digi_stats(acc["digi"], _compute_digi_month_stats(digi))
            if esc:  acc["esc"]  = _merge_escompte_stats(acc["esc"], _compute_escompte_stats(esc))
            if mdl:  acc["mdl"]  = _merge_mdl_stats(acc["mdl"], _compute_mdl_stats(mdl))
            months, kinds = _derive_months_kinds(lines)
            _digi_update(r["id"], {"status": "done", "months": months, "kinds": kinds, "error": None})
            fixed += 1
            print(f"  ✓ réparé : {str(r.get('filename',''))[:50]}", flush=True)
        except Exception as e:
            print(f"  [warn] retry {str(r.get('filename',''))[:40]} : {e}", flush=True)
    print(f"→ tentatives terminées : {fixed} réparé(s)", flush=True)
    return fixed


def _backfill_labo_kinds(user_id: str) -> int:
    """Ajoute le tag « labo:<NORM> » aux fichiers 'done' qui ne l'ont pas encore,
    pour que le front range chaque facture (notamment CSP) sous le bon labo.
    MÉTADONNÉES SEULEMENT — ne recalcule JAMAIS les stats (déjà faites au 1er dépôt,
    additives) : aucun risque de double comptage. Idempotent : un fichier déjà taggé
    est ignoré ; s'il ne révèle aucun labo cible on pose un sentinel « labo:? » pour
    ne pas le re-parser indéfiniment. Liste légère d'abord (sans contenu) → contenu
    récupéré seulement pour le sous-ensemble à compléter."""
    key = _supa_key()
    url = (f"{SUPA_URL}/rest/v1/digi_files?user_id=eq.{user_id}&status=eq.done"
           f"&select=id,filename,kinds&order=id.asc")
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            rows = json.loads(r.read())
    except urllib.error.HTTPError:
        return 0            # colonne kinds absente → rien à faire
    todo = [r for r in rows
            if not any(str(k).startswith("labo:") for k in (r.get("kinds") or []))]
    if not todo:
        return 0
    print(f"→ tag labo : {len(todo)} fichier(s) à compléter…", flush=True)
    n = 0
    for r in todo:
        existing = [k for k in (r.get("kinds") or []) if not str(k).startswith("labo:")]
        try:
            curl = (f"{SUPA_URL}/rest/v1/digi_files?id=eq.{r['id']}"
                    f"&select=content_b64,filename")
            creq = urllib.request.Request(curl, headers={"apikey": key,
                                          "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(creq, timeout=60) as cr:
                crows = json.loads(cr.read())
            pdf   = base64.b64decode((crows[0] if crows else {}).get("content_b64") or "")
            lines = _parse_one(pdf, r.get("filename") or "")
            labos = sorted({_norm_labo(l.get("labo") or l.get("fournisseur") or "")
                            for l in lines})
            tags  = ["labo:" + l for l in labos if l]
            _digi_update(r["id"], {"kinds": existing + (tags or ["labo:?"])})
            n += 1
        except Exception as e:
            _digi_update(r["id"], {"kinds": existing + ["labo:?"]})   # sentinel anti-boucle
            print(f"  [warn] tag labo {str(r.get('filename',''))[:40]} : {e}", flush=True)
    print(f"→ tag labo terminé : {n}", flush=True)
    return n


def _rebuild_digi_all(user_id: str) -> int:
    """Reconstruit digi_month_stats DE ZÉRO (produits + avoirs) depuis TOUS les
    fichiers 'done' stockés. Corrige :
      - la divergence snake/camel (le front lit `digiMonthStats`, le batch écrivait
        `digi_month_stats` → certains achats n'apparaissaient jamais) ;
      - les stats PRODUITS manquantes ou mal attribuées (ex. achats directs Zydus via
        CSP : fichier 'done', taggé labo:ZYDUS, mais absent des stats affichées).
    SÛR : recompute une seule fois par fichier → aucun double comptage. À n'exécuter
    que si TOUS les 'done' ont leur contenu (sinon on perdrait les non-stockés) — ce
    qui est le cas ici. Écrit snake ET camel pour un affichage garanti côté front.
    Les avoirs sont ensuite raffinés (dédoublonnés) par _rebuild_avoirs."""
    key = _supa_key()
    url = (f"{SUPA_URL}/rest/v1/digi_files?user_id=eq.{user_id}&status=eq.done"
           f"&select=id,filename,content_b64&order=id.asc")
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        rows = json.loads(r.read())
    if not rows:
        return 0
    # GARDE : n'exécute le rebuild QUE si tous les 'done' ont leur contenu, sinon on
    # perdrait les stats des non-stockés (le contenu vide est "" côté insert).
    missing = sum(1 for r in rows if not (r.get("content_b64") or "").strip())
    if missing:
        print(f"→ rebuild complet ABANDONNÉ : {missing} 'done' sans contenu (risque de perte)", flush=True)
        return 0
    print(f"→ reconstruction COMPLÈTE digi (produits+avoirs) depuis {len(rows)} fichier(s)…", flush=True)
    acc, n = {}, 0
    for row in rows:
        try:
            pdf = base64.b64decode(row.get("content_b64") or "")
            if not pdf:
                continue
            lines = _parse_one(pdf, row.get("filename") or "")
            digi  = [l for l in lines if l.get("type") not in ("escompte", "mdl")]
            if digi:
                acc = _merge_digi_stats(acc, _compute_digi_month_stats(digi))
                n += 1
        except Exception as e:
            print(f"  [warn] rebuild-all {str(row.get('filename',''))[:40]} : {e}", flush=True)
    fresh = _get_state_sync(user_id) or {}
    fresh["digi_month_stats"] = acc          # snake (source de vérité backend)
    fresh["digiMonthStats"]   = acc          # camel (ce que le front affiche) → synchronisé
    _patch_state_sync(user_id, fresh)
    print(f"→ reconstruction complète : {len(acc)} mois depuis {n} fichier(s)", flush=True)
    return n


def _sync_digi_camel(user_id: str) -> None:
    """Aligne le camel `digiMonthStats` sur le snake `digi_month_stats` (source de
    vérité backend) APRÈS tous les rebuilds — garantit que le front affiche les
    valeurs recalculées quel que soit son chemin de chargement (snake vs camel)."""
    fresh = _get_state_sync(user_id) or {}
    snake = fresh.get("digi_month_stats")
    if snake is not None and fresh.get("digiMonthStats") != snake:
        fresh["digiMonthStats"] = snake
        _patch_state_sync(user_id, fresh)
        print("→ camel digiMonthStats aligné sur snake digi_month_stats", flush=True)


def _rebuild_avoirs(user_id: str) -> int:
    """Reconstruit les stats d'AVOIRS (rdp_total, rdp_by_taux, presta_total[_ttc],
    facture_refs) depuis les PDF stockés — source DÉDOUBLONNÉE. Corrige les stats
    gonflées par des imports répétés du même avoir (fusion additive). Les champs
    produits (qty, total_ht) ne sont pas touchés : tous les PDF produits ne sont
    pas forcément stockés, alors que les avoirs rdp/presta le sont."""
    key = _supa_key()
    url = (f"{SUPA_URL}/rest/v1/digi_files?user_id=eq.{user_id}&status=eq.done"
           f"&kinds=ov.%7Brdp,presta%7D&select=id,filename,content_b64&order=id.asc")
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            rows = json.loads(r.read())
    except urllib.error.HTTPError:
        return 0            # colonne kinds absente → rien à reconstruire
    if not rows:
        return 0
    print(f"→ reconstruction des stats d'avoirs depuis {len(rows)} PDF stocké(s)…", flush=True)
    # Dédoublonnage PAR CONTENU PARSÉ : le même avoir existe souvent sous PLUSIEURS
    # noms (dépôt manuel « avoir_…_9006187702_… » + extension « BIOGARAN_22102025 »,
    # presta CSP sous 3 noms) que ni le dédoublonnage par nom ni la signature
    # nom+date (_doc_sig) ne peuvent rapprocher. Additionner ces copies doublait /
    # triplait rdp_total, rdp_by_taux et presta (cas réel : sept. 2025 rdp 2 792,58
    # au lieu de 1 396,29, presta 19 620 au lieu de 6 540 → toutes les
    # réconciliations semblaient sous-payées). Deux fichiers dont les lignes
    # d'avoir parsées sont identiques = même document → une seule prise en compte.
    parsed = []
    for row in rows:
        try:
            pdf   = base64.b64decode(row.get("content_b64") or "")
            lines = _parse_one(pdf, row.get("filename") or "")
            av    = [l for l in lines if l.get("type") in ("rdp", "presta")]
            if av:
                parsed.append((row, av))
        except Exception as e:
            print(f"  [warn] rebuild {str(row.get('filename',''))[:40]} : {e}", flush=True)

    # Signature de contenu : labo NORMALISÉ (les copies d'un même avoir parsent des
    # labos bruts différents selon le nom de fichier : « Biogaran » / « CSP » /
    # « Centre-Specialites-Pharmaceutiques »), et DEUX niveaux pour le n° de
    # facture : la version longue/scannée d'un avoir parse parfois un facture_num
    # VIDE (cas réel : presta CSP sept. 2025 en 3 exemplaires dont un sans n° →
    # presta restait à 13 080 = 2 × 6 540). On garde d'abord les fichiers AVEC n°
    # (dédoublonnés sur la signature complète), puis un fichier SANS n° est écarté
    # si sa signature SANS n° (type, période, labo, montant) correspond à un
    # document déjà retenu. Deux documents distincts gardent des n° distincts →
    # jamais fusionnés à tort.
    def _sig(av, with_ref):
        return tuple(sorted(
            (str(l.get("type")), str(l.get("period_month") or l.get("billing_date", ""))[:7],
             _norm_labo(l.get("labo") or ""),
             str(l.get("facture_num") or "") if with_ref else "",
             round(abs(float(l.get("montant") or l.get("total_ht") or 0)), 2))
            for l in av))
    def _has_ref(av):
        return any(l.get("facture_num") for l in av)

    avoir_lines, seen_full, seen_noref, dup_files = [], set(), set(), 0
    for row, av in sorted(parsed, key=lambda t: not _has_ref(t[1])):   # avec n° d'abord
        fs, ns = _sig(av, True), _sig(av, False)
        if fs in seen_full or (not _has_ref(av) and ns in seen_noref):
            dup_files += 1
            continue
        seen_full.add(fs); seen_noref.add(ns)
        avoir_lines += av
    if dup_files:
        print(f"→ {dup_files} copie(s) du même avoir ignorée(s) (dédoublonnage par contenu)", flush=True)
    fresh = _compute_digi_month_stats(avoir_lines)

    state = _get_state_sync(user_id) or {}
    cur   = state.get("digi_month_stats") or {}
    # 1) Remise à zéro des champs d'avoirs partout (les refs sont celles des avoirs).
    for rows_ in cur.values():
        for r in rows_:
            r["rdp_total"] = 0; r["rdp_by_taux"] = []
            r["presta_total"] = 0; r["presta_total_ttc"] = 0
            r["facture_refs"] = []
    # 2) Réinjection des valeurs recalculées depuis les PDF.
    for mk, new_rows in fresh.items():
        cur.setdefault(mk, [])
        lm = {r["labo"]: r for r in cur[mk]}
        for nr in new_rows:
            if nr["labo"] in lm:
                r = lm[nr["labo"]]
                for k in ("rdp_total", "rdp_by_taux", "presta_total", "presta_total_ttc", "facture_refs"):
                    r[k] = nr[k]
            else:
                lm[nr["labo"]] = dict(nr)
        cur[mk] = sorted(lm.values(), key=lambda r: r["labo"])
    state["digi_month_stats"] = cur
    _patch_state_sync(user_id, state)
    print(f"→ stats d'avoirs reconstruites ({len(fresh)} mois de période)", flush=True)
    return len(rows)


def _doc_sig(fn: str):
    """Signature « même document » : n° de document + date extraits du nom Digi
    (« …_9006335480_17122025.pdf »). None si le nom n'a pas cette forme."""
    m = re.search(r'_([A-Za-z0-9-]{8,})_(\d{8})\.(pdf|xlsx?)$', fn or '', re.I)
    return f"{m.group(1)}|{m.group(2)}".upper() if m else None


def _rebuild_grossiste(user_id: str) -> int:
    """Reconstruit les stats grossiste (récap par palier + détail par CIP) depuis
    les justificatifs XLSX STOCKÉS (kinds=grossiste-xlsx). Maille MOIS : les mois
    couverts par les fichiers remplacent l'existant ; les mois sans fichier stocké
    (imports d'avant la conservation) sont préservés."""
    from grossiste_parse import _parse_grossiste_bytes, _parse_grossiste_detail_bytes
    key = _supa_key()
    url = (f"{SUPA_URL}/rest/v1/digi_files?user_id=eq.{user_id}&status=eq.done"
           f"&kinds=cs.%7Bgrossiste-xlsx%7D&select=id,filename,content_b64&order=id.asc")
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            rows = json.loads(r.read())
    except urllib.error.HTTPError:
        return 0
    if not rows:
        return 0
    print(f"→ reconstruction grossiste depuis {len(rows)} justificatif(s) XLSX…", flush=True)
    recap, cip = {}, {}
    for row in rows:
        try:
            xlsx = base64.b64decode(row.get("content_b64") or "")
            recap.update(_parse_grossiste_bytes(xlsx))          # mois → remplacé
            cip.update(_parse_grossiste_detail_bytes(xlsx))
        except Exception as e:
            print(f"  [warn] rebuild grossiste {str(row.get('filename',''))[:40]} : {e}", flush=True)
    if not recap and not cip:
        return 0
    state = _get_state_sync(user_id) or {}
    cur_r = state.get("grossiste_month_stats") or {}
    cur_r.update(recap)
    state["grossiste_month_stats"] = cur_r
    cur_c = state.get("grossiste_cip_stats") or {}
    cur_c.update(cip)
    state["grossiste_cip_stats"] = cur_c
    _patch_state_sync(user_id, state)
    print(f"→ grossiste reconstruit : {len(recap)} mois récap, {len(cip)} mois détail CIP", flush=True)
    return len(rows)


def _dedupe(user_id: str) -> int:
    """Supprime les fichiers en double : même nom, OU même document sous deux noms
    (un avoir apparaît parfois aussi en « facture_… » — on garde l'« avoir_ »,
    sinon le plus ancien id)."""
    key = _supa_key()
    url = f"{SUPA_URL}/rest/v1/digi_files?user_id=eq.{user_id}&select=id,filename&order=id.asc"
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rows = json.loads(r.read())
    seen, dele = set(), []
    for row in rows:
        fn = (row.get("filename") or "").strip()
        if not fn:
            continue
        if fn.lower() in seen:
            dele.append(row["id"])          # doublon de nom → à supprimer
        else:
            seen.add(fn.lower())
    # Même document sous deux noms différents (avoir_/facture_).
    deleted = set(dele)
    by_sig: dict = {}
    for row in rows:
        if row["id"] in deleted:
            continue
        sig = _doc_sig(row.get("filename") or "")
        if not sig:
            continue
        prev = by_sig.get(sig)
        if prev is None:
            by_sig[sig] = row
            continue
        keep_prev = (prev.get("filename") or "").lower().startswith("avoir") \
                    or not (row.get("filename") or "").lower().startswith("avoir")
        loser = row if keep_prev else prev
        if not keep_prev:
            by_sig[sig] = row
        dele.append(loser["id"])
    if not dele:
        return 0
    for i in range(0, len(dele), 100):
        ids = ",".join(str(x) for x in dele[i:i + 100])
        durl = f"{SUPA_URL}/rest/v1/digi_files?id=in.({ids})"
        dreq = urllib.request.Request(durl, method="DELETE", headers={
            "apikey": key, "Authorization": f"Bearer {key}", "Prefer": "return=minimal"})
        with urllib.request.urlopen(dreq, timeout=60):
            pass
    print(f"→ dé-doublonnage : {len(dele)} doublon(s) supprimé(s)", flush=True)
    return len(dele)


def main():
    if not USER_ID:
        print("!! USER_ID manquant — abandon", flush=True)
        sys.exit(1)

    # Mode « reconstruction seule » : saute le traitement des fichiers en attente
    # (utile quand la file est énorme et lente) et reconstruit directement les stats
    # depuis les fichiers déjà 'done' → les achats déjà traités remontent aussitôt.
    rebuild_only = os.environ.get("REBUILD_ONLY", "").lower() in ("1", "true", "yes")

    state = _get_state_sync(USER_ID) or {}
    rows  = [] if rebuild_only else _digi_pending(USER_ID)
    total = len(rows)
    if rebuild_only:
        print(f"→ MODE RECONSTRUCTION SEULE (pending ignoré) pour {USER_ID[:8]}", flush=True)
    else:
        print(f"→ {total} avoir(s) en attente pour {USER_ID[:8]}", flush=True)

    # Accumulateurs des stats Digi (partent de l'existant, la base du runner).
    acc = {"digi": state.get("digi_month_stats") or {},
           "esc":  state.get("escompte_stats")   or {},
           "mdl":  state.get("mdl_stats")         or {}}

    ok = err = 0
    if total > 0:
      _persist(USER_ID, acc, {"status": "running", "done": 0, "total": total,
                              "message": f"Traitement de {total} fichier(s)…"})
      for i, row in enumerate(rows):
        fname = row.get("filename") or f"fichier {i+1}"
        try:
            # PDF déjà stocké (dépôt manuel) ? sinon on le télécharge depuis source_url
            # (import via l'extension navigateur) et on le persiste pour l'aperçu.
            pdf_bytes = base64.b64decode(row["content_b64"]) if row.get("content_b64") else b""
            if not pdf_bytes and row.get("source_url"):
                pdf_bytes = _download(row["source_url"])
                if pdf_bytes:
                    _digi_update(row["id"], {"content_b64": base64.b64encode(pdf_bytes).decode()})
            if not pdf_bytes:
                raise ValueError("PDF indisponible (ni contenu stocké ni URL téléchargeable)")
            lines = _parse_one(pdf_bytes, fname)
            if not lines:
                raise ValueError("aucune donnée extractible (format non reconnu)")

            esc  = [l for l in lines if l.get("type") == "escompte"]
            mdl  = [l for l in lines if l.get("type") == "mdl"]
            digi = [l for l in lines if l.get("type") not in ("escompte", "mdl")]

            if digi:
                acc["digi"] = _merge_digi_stats(acc["digi"], _compute_digi_month_stats(digi))
            if esc:
                acc["esc"]  = _merge_escompte_stats(acc["esc"], _compute_escompte_stats(esc))
            if mdl:
                acc["mdl"]  = _merge_mdl_stats(acc["mdl"], _compute_mdl_stats(mdl))

            months, kinds = _derive_months_kinds(lines)
            _digi_update(row["id"], {"status": "done", "months": months, "kinds": kinds})
            ok += 1
            msg = f"{i+1}/{total} · {fname} ✓"
        except Exception as e:
            _digi_update(row["id"], {"status": "error", "error": str(e)[:300]})
            err += 1
            msg = f"{i+1}/{total} · {fname} ✗ ({e})"
            print(f"  [warn] {msg}", flush=True)

        _persist(USER_ID, acc, {"status": "running", "done": i + 1, "total": total, "message": msg})
        print(f"  {msg}", flush=True)

    # Nouvelle tentative sur les fichiers 'error' avec le parseur courant (répare
    # ce que d'anciennes versions rataient, ex. CSP Zydus). Sauté en reconstruction seule.
    nfix  = 0 if rebuild_only else _retry_errored(USER_ID, acc)
    # Ré-indexation des avoirs existants mal datés / sans catégorie, puis dé-doublonnage.
    reidx = _reindex(USER_ID)
    nlabo = _backfill_labo_kinds(USER_ID)
    ndup  = _dedupe(USER_ID)

    parts = []
    if total: parts.append(f"{ok} importé(s)")
    if err:   parts.append(f"{err} en erreur")
    if nfix:  parts.append(f"{nfix} réparé(s)")
    if reidx: parts.append(f"{reidx} ré-indexé(s)")
    if nlabo: parts.append(f"{nlabo} labo(s) taggé(s)")
    if ndup:  parts.append(f"{ndup} doublon(s) supprimé(s)")
    _persist(USER_ID, acc, {"status": "done", "done": total, "total": total,
                            "message": "Terminé : " + (", ".join(parts) if parts else "rien à faire")})

    # Reconstruction COMPLÈTE des stats produits+avoirs depuis TOUS les PDF 'done'
    # stockés (corrige la divergence snake/camel + les achats directs manquants,
    # ex. Zydus via CSP). AVANT _rebuild_avoirs, qui raffine ensuite les avoirs.
    try:
        _rebuild_digi_all(USER_ID)
    except Exception as e:
        print(f"  [warn] rebuild digi complet : {e}", flush=True)
    # Reconstruction des stats d'avoirs depuis les PDF stockés (dédoublonnés) —
    # APRÈS le _persist final : elle relit l'état frais et remplace uniquement
    # les champs d'avoirs (corrige les cumuls gonflés par les doublons).
    try:
        _rebuild_avoirs(USER_ID)
    except Exception as e:
        print(f"  [warn] rebuild avoirs : {e}", flush=True)
    # Idem pour le grossiste : récap par palier + détail par CIP depuis les
    # justificatifs XLSX stockés (le bouton « ré-analyser » couvre donc tout).
    try:
        _rebuild_grossiste(USER_ID)
    except Exception as e:
        print(f"  [warn] rebuild grossiste : {e}", flush=True)
    # Aligner le camel sur le snake APRÈS tous les rebuilds → affichage front garanti.
    try:
        _sync_digi_camel(USER_ID)
    except Exception as e:
        print(f"  [warn] sync camel : {e}", flush=True)
    print(f"✅ Terminé — {ok} ok, {err} err, {reidx} ré-indexé(s)", flush=True)


if __name__ == "__main__":
    main()
