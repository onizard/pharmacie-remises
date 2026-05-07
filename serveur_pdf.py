#!/usr/bin/env python3
"""
Serveur d'extraction PDF — ANALYSEUR DE REMISE
================================================
Lancer :  python serveur_pdf.py
          python serveur_pdf.py --port 5050

Dépendances (déjà installées dans venv) :
    pip install flask flask-cors pdfplumber rapidfuzz
"""

import re, sys, io, os, logging
from pathlib import Path
from collections import defaultdict, Counter

try:
    from flask import Flask, request, jsonify
    from flask_cors import CORS
except ImportError:
    sys.exit("❌  flask/flask-cors manquant.\n    Lancer : pip install flask flask-cors")

try:
    import pdfplumber
except ImportError:
    sys.exit("❌  pdfplumber manquant.\n    Lancer : pip install pdfplumber")

try:
    from rapidfuzz.distance import Levenshtein as LevenshteinDist
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

PORT = int(os.environ.get("PDF_SERVER_PORT", 5050))
app  = Flask(__name__)
CORS(app, origins="*")
logging.getLogger("werkzeug").setLevel(logging.WARNING)


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation — portage fidèle de extraire_excel.py
# ─────────────────────────────────────────────────────────────────────────────

ABREV_LABOS = sorted([
    "ARROW","ARR","ARL","BIOGARAN","BIO","BGR","VIATRIS","VIA","MYL","MYP",
    "PFIZER","PFI","SANDOZ","SDZ","SAN","ZENTIVA","ZEN","TEVA","TEV",
    "CRISTERS","CRI","ZYDUS","ZYD","CORREVIO","CPH","ABACUS","WEGOVY","STA",
    "EG","GE","ZTL","REF","SA","QVL","NOR","KS","QIL","SUB",
], key=len, reverse=True)

MOLECULES_PROTEGEES = {'H', 'A'}

DCI_CORRECTIONS = {
    r'\bCEFTRIAXIONE\b':    'CEFTRIAXONE',
    r'\bCLARITHROMYCYNE\b': 'CLARITHROMYCINE',
}

UNITES_DOSE = r'(?:MG/ML|MG|MCG|µG|UG|NG|G/ML|ML|UI/ML|MUI|MMOL|MOL|PC|%|G(?![A-Z]))'
FORMES      = r'(?:CPR|GELU|CAPS|COMP|AMP|SOL|PDR|CRE|GEL|POM|SUP|SPA|INJ|PERF|DISP|BUV|GTT|SACH|VERN|VERNIS|SPRAY|NAS|OPH|EAR|CPS|SEC|ORO|LP|LA|LI)'


def supprimer_abrev(libelle: str) -> str:
    for abrev in ABREV_LABOS:
        if abrev.upper() in MOLECULES_PROTEGEES:
            continue
        pattern = r'(?<=\s)' + re.escape(abrev) + r'(?=\s|\d|$)'
        libelle = re.sub(pattern, '', libelle, flags=re.IGNORECASE)
    for artefact in ['SEC', 'ORO', 'QUI', 'DISP', 'SP', 'TB']:
        libelle = re.sub(r'(?<![A-Z])\b' + artefact + r'\b(?![A-Z])', '', libelle, flags=re.IGNORECASE)
    libelle = re.sub(r'(?<= )X(?= |$)', '', libelle)
    for pattern, correction in DCI_CORRECTIONS.items():
        libelle = re.sub(pattern, correction, libelle, flags=re.IGNORECASE)
    if re.search(r'\bIV\b', libelle, re.IGNORECASE):
        libelle = re.sub(r'\bINJ\b\s*', '', libelle, flags=re.IGNORECASE)
    return re.sub(r' {2,}', ' ', libelle).strip()


def traiter_combinaisons(libelle: str) -> str:
    lib = libelle.strip().upper()

    def norm_dose(dose):
        return re.sub(r'(\d+)MG(\d)\b(?!\d)', lambda x: f"{x.group(1)}.{x.group(2)}MG", dose)

    m = re.search(r'\bBISOPROLOL\s+H\b.*?(\d+(?:\.\d+)?MG\d?)\b', lib, re.IGNORECASE)
    if m:
        return f"BISOPROLOL/HYD {norm_dose(m.group(1))}/6.25MG{lib[m.end():]}"

    m = re.search(r'\bBISOPROLOL\s+(\d+)MG\s+(\d{1,2})(CPR|GELU|COMP)\s+(\d+)\s*(?:SEC)?\b', lib, re.IGNORECASE)
    if m:
        return f'BISOPROLOL {m.group(1)}.{m.group(2)}MG {m.group(4)}{m.group(3).upper()}{lib[m.end():]}'

    if re.search(r'\bAMOXICILLINE\s+A\b', lib, re.IGNORECASE):
        m_pdr = re.search(r'\bAMOXICILLINE\s+A\b\s+(?:\S+\s+)*PDR\s+(?:EN|NN)\s*(\d+)\s*ML', lib, re.IGNORECASE)
        if m_pdr:
            return f"AMOX/CLAV PDR {m_pdr.group(1)}ML"
        RATIO = {'125MG': '31.25MG', '250MG': '62.5MG', '500MG': '62.5MG', '875MG': '125MG', '1G': '125MG'}
        for amox, clav in RATIO.items():
            if amox in lib:
                return f"AMOX/CLAV {amox}/{clav}{lib[lib.find(amox) + len(amox):]}"
        lib = re.sub(r'\bAMOXICILLINE\s+A\b', 'AMOX/CLAV', lib, flags=re.IGNORECASE)
        if re.search(r'\b(?:8|12)\s*(?:SACH|SAC|S)\b', lib, re.IGNORECASE):
            lib = lib.replace('AMOX/CLAV', 'AMOX/CLAV 1G/125MG', 1)
        elif re.search(r'\b(?:16|24)\s*CPR\b', lib, re.IGNORECASE):
            lib = lib.replace('AMOX/CLAV', 'AMOX/CLAV 500MG/62.5MG', 1)

    return lib


def separer_molecules_collees(libelle: str) -> str:
    for suf in ['TIM', 'VALS', 'HCTZ']:
        libelle = re.sub(r'([A-Z])' + suf + r'\b', r'\1 ' + suf, libelle)
    libelle = re.sub(r'([A-Z]{4,})H(?=\s|\d)', r'\1 H', libelle)
    return libelle


def normaliser_bt(libelle: str) -> str:
    libelle = re.sub(r'\b(\d+)\s*SAC\b',  r'\1SACH', libelle, flags=re.IGNORECASE)
    libelle = re.sub(r'\b(\d+)S\b',        r'\1SACH', libelle, flags=re.IGNORECASE)
    libelle = re.sub(r'\bGLE\b', 'PDR',    libelle,   flags=re.IGNORECASE)
    libelle = re.sub(r'\b(\d+)AM\b',       r'\1AMP',  libelle, flags=re.IGNORECASE)

    FORMES_LIST = ['GELU','CAPS','COMP','SUPP','SACH','VERN','VERNIS','SPRAY','PERF','DISP',
                   'CPR','AMP','SOL','PDR','CRE','GEL','POM','SUP','SPA','INJ','BUV','GTT']
    FORMES_RE = '|'.join(FORMES_LIST)

    lib_sans_bt  = re.sub(r'\bBT\s*\d+\b', '', libelle, flags=re.IGNORECASE)
    forme_match  = re.search(r'\b(' + FORMES_RE + r')\b', lib_sans_bt, re.IGNORECASE)
    forme        = forme_match.group(1).upper() if forme_match else 'CPR'

    libelle = re.sub(r'\bBT\s*(\d+)\s*(' + FORMES_RE + r')\b', r'\1\2', libelle, flags=re.IGNORECASE)
    libelle = re.sub(r'\bBT\s*(\d+)\b', lambda m: f"{m.group(1)}{forme}", libelle, flags=re.IGNORECASE)
    libelle = re.sub(
        r'\b(' + FORMES_RE + r')\s+(\d+)(?:SEC)?\b(?!\s*(?:MG|G(?![A-Z])|ML|UI|MCG|PC|%))',
        lambda m: f"{m.group(2)}{m.group(1).upper()}", libelle, flags=re.IGNORECASE)
    libelle = re.sub(r'\b(' + FORMES_RE + r')\s+(\d+\1)\b', r'\2', libelle, flags=re.IGNORECASE)
    return re.sub(r' {2,}', ' ', libelle).strip()


def normaliser_concentration(libelle: str) -> str:
    def conv(m):
        v = float(m.group(1).replace(',', '.')) * 10
        return f"{int(v) if v == int(v) else v}MG/ML"
    return re.sub(r'(\d+(?:[.,]\d+)?)\s*(?:PC|%)\s+\d+(?:[.,]\d+)?\s*ML', conv, libelle, flags=re.IGNORECASE)


def inserer_espaces(libelle: str) -> str:
    libelle = re.sub(r'\b(COLLY|COL|INJ|GTT)(\d)', r'\1 \2', libelle, flags=re.IGNORECASE)
    libelle = re.sub(r'\b(\d+)MG(\d{2})\b', lambda m: f'{m.group(1)}.{m.group(2)}MG', libelle)
    libelle = re.sub(r'\b(\d+)MG(\d)\b(?!\d)', lambda m: f'{m.group(1)}.{m.group(2)}MG', libelle)
    libelle = re.sub(r'\b(0\.\d+)MG\s+(\d+)ML\b', r'\1MG/ML \2ML', libelle, flags=re.IGNORECASE)
    libelle = re.sub(
        r'(\d+(?:[.,]\d+)?' + UNITES_DOSE + r')(\d+(?=' + FORMES + r'))',
        r'\1 \2', libelle, flags=re.IGNORECASE)
    return libelle


def parser_libelle(libelle: str) -> dict:
    lib = libelle.strip().upper()
    pda = bool(re.search(r'\bPDA\b', lib))
    lib = re.sub(r'\bPDA\b', '', lib).strip()
    lib = supprimer_abrev(lib)
    lib = traiter_combinaisons(lib)
    lib = separer_molecules_collees(lib)
    lib = normaliser_concentration(lib)
    lib = normaliser_bt(lib)
    lib = inserer_espaces(lib)
    lib = re.sub(r' {2,}', ' ', lib).strip()

    _U = r'(?:MG/ML|MG|MCG|ML|UI|G(?![A-Z]))'
    pattern_dose = (
        r'(\d+(?:[.,]\d+)?' + _U + r'(?:/' + r'\d+(?:[.,]\d+)?' + _U + r')+'
        r'|\d+(?:[.,]\d+)?(?:/\d+(?:[.,]\d+)?)*\s*(?:MG/ML|UI/ML|MUI|MG|MCG|ML|MMOL|MOL|PC|UI|%|G(?![A-Z]))'
        r'|\d+/\d+(?:[.,]\d+)?)')
    doses = re.findall(pattern_dose, lib, re.IGNORECASE)
    dosage = ' '.join(d.strip() for d in doses) if doses else ''

    qte_match = re.search(r'(\d+)\s*(' + FORMES + r'(?:\s+' + FORMES + r')*)', lib, re.IGNORECASE)
    quantite = qte_match.group(1) if qte_match else ''
    forme    = qte_match.group(2).strip().upper() if qte_match else ''

    dci = lib
    for d in doses:
        dci = dci.replace(d, '')
    if qte_match and qte_match.group(0) in dci:
        dci = dci[:dci.find(qte_match.group(0))]
    dci = re.sub(r'\d+', '', dci)
    dci = re.sub(r'[.,]', '', dci)
    dci = re.sub(r'(?<![A-Z])/|/(?![A-Z])', '', dci)
    dci = re.sub(r'\s+', ' ', dci).strip()

    suffixe = ''
    if qte_match:
        reste = lib[qte_match.end():].strip()
        for mot in ['SEC', 'DISP', 'SP', 'ORO', 'QUI', 'X', 'TB']:
            reste = re.sub(r'\b' + mot + r'\b', '', reste, flags=re.IGNORECASE)
        suffixe = re.sub(r'\s+', ' ', reste).strip()

    return {'dci': dci, 'pda': pda, 'dosage': dosage.upper(),
            'quantite': quantite, 'forme': forme.upper(), 'suffixe': suffixe.upper()}


def cle_normalisation(parsed: dict) -> str:
    tag = '_PDA' if parsed['pda'] else ''
    return f"{parsed['dci']}|{parsed['dosage']}|{parsed['quantite']}|{parsed['forme']}|{parsed['suffixe']}{tag}"


def construire_libelle_normalise(parsed: dict) -> str:
    parts = [parsed['dci']]
    if parsed['pda']:       parts.append('PDA')
    if parsed['dosage']:    parts.append(parsed['dosage'])
    if parsed['quantite'] and parsed['forme']:
        parts.append(f"{parsed['quantite']}{parsed['forme']}")
    if parsed['suffixe']:   parts.append(parsed['suffixe'])
    return ' '.join(p for p in parts if p)


def corriger_dci_typos(all_rows: list) -> dict:
    if not HAS_RAPIDFUZZ:
        return {}
    dci_count = Counter(r['_parsed']['dci'] for r in all_rows)
    dcis = [d for d in dci_count if d]
    dci_ctx: dict = defaultdict(set)
    for r in all_rows:
        p = r['_parsed']
        if p['dci']:
            dci_ctx[p['dci']].add((p['dosage'], p['forme']))

    parent = {d: d for d in dcis}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb: return
        if dci_count[ra] >= dci_count[rb]: parent[rb] = ra
        else: parent[ra] = rb

    for i, d1 in enumerate(dcis):
        for d2 in dcis[i + 1:]:
            w1, w2 = d1.split(), d2.split()
            if len(w1) != len(w2) or w1[1:] != w2[1:]: continue
            if LevenshteinDist.distance(w1[0], w2[0]) != 1: continue
            c1, c2 = dci_ctx[d1], dci_ctx[d2]
            if {x[0] for x in c1} & {x[0] for x in c2} or {x[1] for x in c1} & {x[1] for x in c2}:
                union(d1, d2)

    corrections = {}
    groups: dict = defaultdict(list)
    for d in dcis:
        groups[find(d)].append(d)
    for root, members in groups.items():
        if len(members) > 1:
            canonical = max(members, key=lambda d: dci_count[d])
            for m in members:
                if m != canonical:
                    corrections[m] = canonical
    return corrections


# ─────────────────────────────────────────────────────────────────────────────
# Détection automatique des colonnes dans un tableau PDF
# ─────────────────────────────────────────────────────────────────────────────

# Patterns pour identifier la colonne RSF « standard / facture »
RSF_PREFER  = [r'\bTAUX\b', r'STANDARD', r'FACTUR', r'RSF', r'REM.*FACT', r'TAUX.*REM', r'REMISE.*\b1\b', r'REMISE']
RSF_EXCLUDE = [r'\bCIP', r'\bACL\b', r'\bEAN\b', r'\bCODE\b', r'RFA', r'PALIER', r'VOLUME', r'OBJECTIF', r'BONUS', r'CONDIT', r'ANNUEL', r'FIN\s*AN', r'PRIX', r'NET\s*REM']


def _score_rsf(header_text: str) -> int:
    h = header_text.upper()
    for pat in RSF_EXCLUDE:
        if re.search(pat, h):
            return -99
    for i, pat in enumerate(RSF_PREFER):
        if re.search(pat, h):
            return 10 - i
    return 0


def _is_pct(v: str) -> bool:
    try:
        f = abs(float(v.replace(',', '.').replace('%', '').strip()))
        return 0 < f < 100
    except Exception:
        return False


def _is_price(v: str) -> bool:
    if not any(c in v for c in (',', '.', '€')):
        return False
    try:
        f = float(v.replace(',', '.').replace('€', '').strip())
        return 0.1 < f < 5000
    except Exception:
        return False


def detect_columns(table: list) -> dict:
    """
    Retourne {cip, lib, rsf, puht, punet, data_start} (indices de colonnes, -1 si absent).
    Stratégie : en-têtes d'abord, puis sampling du contenu.
    """
    if not table:
        return {}

    # Trouver la ligne d'en-tête : première ligne sans code CIP 13 chiffres
    hdr_idx = 0
    for i, row in enumerate(table[:5]):
        cells = [str(c or '').strip() for c in row if c]
        if not any(re.fullmatch(r'\d{13}', c.replace(' ', '')) for c in cells):
            hdr_idx = i
            break

    header    = [str(c or '').strip().upper() for c in table[hdr_idx]]
    data_rows = [r for r in table[hdr_idx + 1:] if any(c for c in (r or []))]

    roles = {'cip': -1, 'lib': -1, 'rsf': -1, 'puht': -1, 'punet': -1, 'data_start': hdr_idx + 1}

    # ── Détection par en-têtes ────────────────────────────────────────────────
    # Priorité à la colonne CIP13 (ex: "CIP/ACL 13") avant CIP7
    for i, h in enumerate(header):
        if re.search(r'13', h) and re.search(r'\bCIP|ACL|EAN\b', h):
            roles['cip'] = i; break
    if roles['cip'] == -1:
        for i, h in enumerate(header):
            if re.search(r'\bCIP|ACL|EAN\b', h):
                roles['cip'] = i; break
        if re.search(r'PU.?HT|P[FA]HT|PRIX.?HT|CATALOGUE|TARIF\s+BRUT', h) and roles['puht'] == -1:
            roles['puht'] = i
        if re.search(r'LIB|DESIG|ARTICLE|NOM|PRODUIT|DÉNOMINATION', h) and roles['lib'] == -1 and i != roles['cip']:
            roles['lib'] = i

    # RSF : choisir la colonne avec le meilleur score (éviter RFA etc.)
    rsf_scores = [(i, _score_rsf(h)) for i, h in enumerate(header) if i not in (roles['cip'], roles['puht'], roles['lib'])]
    rsf_scores = [(i, s) for i, s in rsf_scores if s > -50]
    if rsf_scores:
        best = max(rsf_scores, key=lambda x: x[1])
        if best[1] >= 0:
            roles['rsf'] = best[0]

    # PU NET
    for i, h in enumerate(header):
        if re.search(r'\bNET\b|\bREMISÉ\b|\bNET\s+REMIS', h) and i not in (roles['cip'], roles['lib'], roles['rsf'], roles['puht']):
            roles['punet'] = i
            break

    # ── Fallback par contenu (si colonnes manquantes) ─────────────────────────
    if not data_rows:
        return roles

    sample    = data_rows[:min(15, len(data_rows))]
    col_count = max(len(r or []) for r in sample)

    for col in range(col_count):
        vals = [str((r[col] if len(r) > col else None) or '').strip() for r in sample]
        non_empty = [v for v in vals if v]
        if not non_empty:
            continue

        if roles['cip'] == -1:
            cip_hits = sum(1 for v in non_empty if re.fullmatch(r'\d{13}', v.replace(' ', '')))
            if cip_hits >= len(non_empty) * 0.7:
                roles['cip'] = col; continue

        if roles['rsf'] == -1 and col not in (roles['cip'], roles['puht'], roles['punet'], roles['lib']):
            pct_hits = sum(1 for v in non_empty if _is_pct(v))
            if pct_hits >= len(non_empty) * 0.7:
                roles['rsf'] = col; continue

        if roles['lib'] == -1 and col not in (roles['cip'], roles['rsf']):
            avg_len = sum(len(v) for v in non_empty) / len(non_empty)
            if avg_len > 12:
                roles['lib'] = col; continue

        if roles['puht'] == -1 and col not in (roles['cip'], roles['rsf'], roles['lib']):
            price_hits = sum(1 for v in non_empty if _is_price(v))
            if price_hits >= len(non_empty) * 0.7:
                roles['puht'] = col

    return roles


# ─────────────────────────────────────────────────────────────────────────────
# Nettoyage prix / taux
# ─────────────────────────────────────────────────────────────────────────────

def nettoyer_prix(s: str) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(',', '.').replace('€', '').replace(' ', '').strip())
    except ValueError:
        return None


def nettoyer_taux(s: str) -> float | None:
    """Retourne le taux en valeur NÉGATIVE (convention Supabase : RSF = réduction)."""
    if not s:
        return None
    try:
        v = float(s.replace(',', '.').replace('%', '').replace(' ', '').strip())
        return -abs(v) if v != 0 else 0.0
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Extraction principale
# ─────────────────────────────────────────────────────────────────────────────

def extraire_refs_pdf(pdf_fp, labo: str) -> dict:
    warnings_list = []
    raw_rows      = []

    with pdfplumber.open(pdf_fp) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
            for tbl_idx, table in enumerate(tables):
                if not table or len(table) < 2:
                    continue

                cols = detect_columns(table)

                if cols.get('cip', -1) == -1:
                    warnings_list.append(f"P.{page_num} tableau {tbl_idx+1} : colonne CIP non détectée, ignoré.")
                    continue
                if cols.get('lib', -1) == -1:
                    warnings_list.append(f"P.{page_num} tableau {tbl_idx+1} : colonne libellé non détectée, ignoré.")
                    continue

                for row in (table[cols['data_start']:] if cols.get('data_start') else table[1:]):
                    if not row:
                        continue

                    def cell(idx):
                        return str(row[idx] or '').strip() if idx != -1 and idx < len(row) else ''

                    cip13    = cell(cols['cip']).replace(' ', '')
                    libelle  = cell(cols['lib']).upper()
                    rsf_raw  = cell(cols['rsf'])
                    puht_raw = cell(cols['puht'])
                    punet_raw= cell(cols['punet'])

                    if not re.fullmatch(r'\d{13}', cip13):
                        continue
                    if not libelle or libelle in ('LIBELLÉ', 'DÉSIGNATION', 'ARTICLE', ''):
                        continue

                    rsf_val   = nettoyer_taux(rsf_raw)
                    puht_val  = nettoyer_prix(puht_raw)
                    punet_val = nettoyer_prix(punet_raw)
                    # Prix net direct (certains labos ne donnent pas de taux mais un prix net)
                    punet_pdf = punet_val if (rsf_val is None and punet_val is not None) else None

                    raw_rows.append({
                        'cip13':       cip13,
                        'libelle_brut': libelle,
                        'rsf_pct':     rsf_val,
                        'puht':        puht_val,
                        '_punet_pdf':  punet_pdf,
                    })

    if not raw_rows:
        return {
            'labo': labo, 'refs': [],
            'warnings': warnings_list + ['Aucune référence trouvée dans les tableaux du PDF.'],
        }

    # ── Normalisation des libellés ────────────────────────────────────────────
    for row in raw_rows:
        row['_parsed'] = parser_libelle(row['libelle_brut'])
        row['_cle']    = cle_normalisation(row['_parsed'])

    corrections = corriger_dci_typos(raw_rows)
    if corrections:
        for row in raw_rows:
            if row['_parsed']['dci'] in corrections:
                row['_parsed']['dci'] = corrections[row['_parsed']['dci']]
            row['_cle'] = cle_normalisation(row['_parsed'])
        warnings_list.append(f"{len(corrections)} correction(s) orthographique(s) DCI appliquée(s).")

    # Libellé modèle par groupe de clé canonique
    groupes = defaultdict(list)
    for row in raw_rows:
        groupes[row['_cle']].append(row['_parsed'])
    modeles = {cle: min([construire_libelle_normalise(p) for p in lst], key=lambda x: (len(x), x))
               for cle, lst in groupes.items()}

    # Propagation PU HT par libellé normalisé
    cle_to_puht: dict = {}
    for row in raw_rows:
        if row['puht'] is not None:
            cle_to_puht.setdefault(row['_cle'], row['puht'])
    propages = 0
    for row in raw_rows:
        if row['puht'] is None and row['_cle'] in cle_to_puht:
            row['puht'] = cle_to_puht[row['_cle']]; propages += 1
    if propages:
        warnings_list.append(f"{propages} PU HT propagé(s) par libellé normalisé.")

    # Calcul PU NET
    for row in raw_rows:
        puht, rsf = row.get('puht'), row.get('rsf_pct')
        if puht is not None and rsf is not None:
            row['punet'] = round(puht * (1 + rsf / 100), 4)
        elif row.get('_punet_pdf') is not None:
            row['punet'] = row['_punet_pdf']
        else:
            row['punet'] = None

    # Déduplique par CIP13 — garde la première occurrence
    seen: dict = {}
    for row in raw_rows:
        seen.setdefault(row['cip13'], row)

    refs = []
    for row in seen.values():
        refs.append({
            'cip13':        row['cip13'],
            'labo':         labo,
            'libelle':      modeles.get(row['_cle'], construire_libelle_normalise(row['_parsed'])),
            'libelle_brut': row['libelle_brut'],
            'rsf_pct':      row['rsf_pct'],
            'puht':         row['puht'],
            'punet':        row.get('punet'),
        })

    nb_bruts = len(set(r['libelle_brut'] for r in refs))
    nb_norm  = len(set(r['libelle']      for r in refs))
    warnings_list.insert(0, f"{len(refs)} références · {nb_bruts} libellés bruts → {nb_norm} normalisés.")

    return {'labo': labo, 'refs': refs, 'warnings': warnings_list}


# ─────────────────────────────────────────────────────────────────────────────
# Extraction fichier d'achats/ventes PDF (CIP13 + quantités)
# ─────────────────────────────────────────────────────────────────────────────

def detect_columns_achats(table: list) -> dict:
    """
    Détecte les colonnes CIP13, quantité et libellé dans un tableau d'achat.
    Retourne {cip, qty, lib, data_start}.
    """
    if not table:
        return {}

    # Ligne d'en-tête : première ligne sans CIP 13 chiffres
    hdr_idx = 0
    for i, row in enumerate(table[:6]):
        cells = [str(c or '').strip() for c in (row or []) if c]
        if not any(re.fullmatch(r'\d{13}', c.replace(' ', '')) for c in cells):
            hdr_idx = i
            break

    header    = [str(c or '').strip().upper() for c in (table[hdr_idx] or [])]
    data_rows = [r for r in table[hdr_idx + 1:] if any(c for c in (r or []))]
    roles     = {'cip': -1, 'qty': -1, 'lib': -1, 'data_start': hdr_idx + 1}

    # Détection par en-tête
    for i, h in enumerate(header):
        if re.search(r'\bCIP\b|ACL|EAN\b|CODE', h) and roles['cip'] == -1:
            roles['cip'] = i
        if re.search(r'QT[EÉ]|QUANT|NOMBRE|^NB$|COLIS|UNIT', h) and roles['qty'] == -1 and i != roles['cip']:
            roles['qty'] = i
        if re.search(r'LIB|D[EÉ]SIG|ARTICLE|NOM|PRODUIT', h) and roles['lib'] == -1 and i not in (roles['cip'], roles['qty']):
            roles['lib'] = i

    # Fallback par contenu
    if not data_rows:
        return roles

    sample    = data_rows[:min(20, len(data_rows))]
    col_count = max((len(r or []) for r in sample), default=0)

    for col in range(col_count):
        vals = [str((r[col] if r and len(r) > col else None) or '').strip() for r in sample]
        non_empty = [v for v in vals if v]
        if not non_empty:
            continue

        if roles['cip'] == -1:
            hits = sum(1 for v in non_empty if re.fullmatch(r'\d{13}', v.replace(' ', '')))
            if hits >= len(non_empty) * 0.6:
                roles['cip'] = col
                continue

        if roles['qty'] == -1 and col != roles['cip'] and col != roles['lib']:
            hits = sum(1 for v in non_empty if re.fullmatch(r'\d{1,6}', v.replace(' ', '')))
            if hits >= len(non_empty) * 0.6:
                roles['qty'] = col
                continue

        if roles['lib'] == -1 and col not in (roles['cip'], roles['qty']):
            avg_len = sum(len(v) for v in non_empty) / len(non_empty)
            if avg_len > 10:
                roles['lib'] = col

    return roles


def extraire_achats_pdf(pdf_fp) -> dict:
    """
    Extrait les lignes CIP13 + quantité depuis un fichier d'achats/ventes PDF.
    Retourne {rows: [{cip13, qty, libelle}], warnings: [...], total: int}.
    """
    warnings_list = []
    rows_by_cip: dict = {}   # cip13 → {cip13, qty, libelle}

    with pdfplumber.open(pdf_fp) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()

            for tbl_idx, table in enumerate(tables):
                if not table or len(table) < 2:
                    continue

                cols = detect_columns_achats(table)

                if cols.get('cip', -1) == -1:
                    warnings_list.append(f"P.{page_num} tab.{tbl_idx+1} : colonne CIP non détectée, ignoré.")
                    continue
                if cols.get('qty', -1) == -1:
                    warnings_list.append(f"P.{page_num} tab.{tbl_idx+1} : colonne quantité non détectée, ignoré.")
                    continue

                for row in (table[cols['data_start']:] if cols.get('data_start') else table[1:]):
                    if not row:
                        continue

                    def cell(idx):
                        return str(row[idx] or '').strip() if idx != -1 and idx < len(row) else ''

                    cip13 = cell(cols['cip']).replace(' ', '')
                    if not re.fullmatch(r'\d{13}', cip13):
                        continue

                    qty_raw = cell(cols['qty']).replace(' ', '')
                    try:
                        qty = int(float(qty_raw.replace(',', '.')))
                    except ValueError:
                        continue
                    if qty <= 0:
                        continue

                    libelle = cell(cols['lib']).upper() if cols.get('lib', -1) != -1 else ''

                    if cip13 in rows_by_cip:
                        rows_by_cip[cip13]['qty'] += qty
                    else:
                        rows_by_cip[cip13] = {'cip13': cip13, 'qty': qty, 'libelle': libelle}

    if not rows_by_cip:
        return {
            'rows': [],
            'warnings': warnings_list + ['Aucune ligne CIP/quantité trouvée dans le PDF.'],
            'total': 0,
        }

    rows = sorted(rows_by_cip.values(), key=lambda r: r['cip13'])
    warnings_list.insert(0, f"{len(rows)} références · {sum(r['qty'] for r in rows)} unités.")
    return {'rows': rows, 'warnings': warnings_list, 'total': len(rows)}


# ─────────────────────────────────────────────────────────────────────────────
# Routes Flask
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/ping')
def ping():
    return jsonify({'ok': True, 'version': '1.0'})


@app.route('/extract-achats', methods=['POST'])
def extract_achats():
    if 'file' not in request.files:
        return jsonify({'error': 'Champ "file" manquant'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Le fichier doit être un PDF (.pdf)'}), 400
    print(f"  ⚙️  Achats PDF : {f.filename}")
    try:
        result = extraire_achats_pdf(io.BytesIO(f.read()))
        print(f"  ✓   {result['total']} références extraites")
        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/extract-pdf', methods=['POST'])
def extract_pdf():
    if 'file' not in request.files:
        return jsonify({'error': 'Champ "file" manquant dans le formulaire'}), 400

    f    = request.files['file']
    labo = request.form.get('labo', 'Inconnu').strip()

    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Le fichier doit être un PDF (.pdf)'}), 400

    print(f"  ⚙️  Extraction PDF : {f.filename}  (labo={labo})")
    try:
        result = extraire_refs_pdf(io.BytesIO(f.read()), labo)
        print(f"  ✓   {len(result['refs'])} références extraites")
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"""
╔══════════════════════════════════════════════╗
║  ANALYSEUR DE REMISE — Serveur PDF           ║
║  http://localhost:{port:<27}║
║  Arrêter : Ctrl+C                            ║
╚══════════════════════════════════════════════╝
""")
    app.run(host='0.0.0.0', port=port, debug=False)
