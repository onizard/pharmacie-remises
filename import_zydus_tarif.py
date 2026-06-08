"""
Importe les 395 présentations du Tarif Zydus France Mai 2026 dans references_pharmacie.
- Met à jour le PFHT pour les CIP13 existants
- Ajoute les nouveaux CIP13 avec libelle normalisé et rsf_pct=-30
"""

import re
import unicodedata
import pdfplumber
import psycopg2
from psycopg2.extras import execute_values

PDF_PATH = "cgv labos/Tarif Zydus France Mai 2026.pdf"
LABO = "Zydus"
DEFAULT_RSF = -30.0

DB_HOST = "aws-0-eu-west-1.pooler.supabase.com"
DB_PORT = 5432
DB_USER = "postgres.fmterazwesiwpwjpkyqi"
DB_PASS = "lDXWqP1SsuchEIRH"
DB_NAME = "postgres"

# ---------------------------------------------------------------------------
# Normalisation des libellés
# ---------------------------------------------------------------------------

def remove_accents(s):
    # Handle micro sign (µ → u) before NFD decomposition
    s = s.replace('µ', 'u').replace('μ', 'u')
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )

BRAND_RE = re.compile(
    r'\b(Zydus France|Zydus|ZF|Substipharm|Lyodis|Vivanta)\b',
    re.IGNORECASE
)
CONTEXT_RE = re.compile(r'\(.*?\)')  # remove parenthetical context like (adultes), (nourrissons)

# Form abbreviations from "Condt produit" column
FORM_MAP = {
    r'\bcp\b': 'CPR',
    r'\bgel\b': 'GELU',      # after accent removal: gél → gel
    r'\bgelule\b': 'GELU',
    r'\bgelules\b': 'GELU',
    r'\bgels\b': 'GELU',
    r'\bml\b': 'ML',
    r'\bsachet\b': 'SACH',
    r'\bsachets\b': 'SACH',
    r'\bsachets dose\b': 'SACH',
    r'\bdose\b': 'SACH',
    r'\bampoule\b': 'AMP',
    r'\bampoules\b': 'AMP',
    r'\bflacon\b': 'FL',
    r'\bflacons\b': 'FL',
    r'\blyophilisats? oraux?\b': 'LYOPH',
    r'\bcomprimes? pellicules?\b': 'CPR',
    r'\bcomprimes?\b': 'CPR',
    r'\bgelule capsule\b': 'GELU',
    r'\bgelule\b': 'GELU',
    r'\bpoudre.*solvant\b': 'PDR',
    r'\bspatule\b': 'SPA',
    r'\bspatules\b': 'SPA',
}

# DCI abbreviations / combination shorthands used in DB
DCI_ABBREV = {
    'AMLODIPINE/VALSARTAN': 'AMLODIP VALS',
    'AMOXICILLINE/ACIDE CLAVULANIQUE': 'AMOX/CLAV PDR',
    'CANDÉSARTAN/HYDROCHLOROTHIAZIDE': 'CANDESART/HCT',
    'CANDESARTAN/HYDROCHLOROTHIAZIDE': 'CANDESART/HCT',
    'EZÉTIMIBE/ATORVASTATINE': 'EZET/ATORVA',
    'EZETIMIBE/ATORVASTATINE': 'EZET/ATORVA',
    'EZÉTIMIBE/SIMVASTATINE': 'EZET/SIMVA',
    'EZETIMIBE/SIMVASTATINE': 'EZET/SIMVA',
    'VALSARTAN/HYDROCHLOROTHIAZIDE': 'VALSA/HCT',
    'LOSARTAN/HYDROCHLOROTHIAZIDE': 'LOSAR/HCT',
    'IRBESARTAN/HYDROCHLOROTHIAZIDE': 'IRBES/HCT',
    'OLMÉSARTAN/HYDROCHLOROTHIAZIDE': 'OLMES/HCT',
    'OLMESARTAN/HYDROCHLOROTHIAZIDE': 'OLMES/HCT',
    'TELMISARTAN/HYDROCHLOROTHIAZIDE': 'TELMI/HCT',
    'PERINDOPRIL/AMLODIPINE': 'PERIND/AMLODIP',
    'ABIRATERONE ACETATE': 'ABIRATERONE',
    'CHOLÉCALCIFÉROL': 'CHOLECALCIFEROL',
    'CHOLECALCIFEROL': 'CHOLECALCIFEROL',
    'CALCIUM/VITAMINE D3': 'CALC/VIT D3',
    'DESMOPRESSINE LYODIS': 'DESMOPRESSINE',
}

def normalize_dosage(s):
    """Normalise les doses : '200 mg' → '200MG', '0,25 mg' → '0.25MG', '1 g' → '1G'"""
    # Combos like "5 mg/80 mg" → "5/80MG"
    s = re.sub(
        r'(\d+[.,]?\d*)\s*(mg|g|µg|mcg|mg/5ml|mg/ml|g/l|ui|iu|%|pc)\s*/\s*'
        r'(\d+[.,]?\d*)\s*(mg|g|µg|mcg|mg/5ml|mg/ml|g/l|ui|iu|%|pc)',
        lambda m: (
            m.group(1).replace(',','.') + '/' +
            m.group(3).replace(',','.') +
            m.group(4).upper().replace('µG','MCG').replace('UI','UI')
        ),
        s, flags=re.IGNORECASE
    )
    # Simple doses "200 mg" → "200MG"
    s = re.sub(
        r'(\d+[.,]?\d*)\s*(mg/5ml|mg/ml|g/l|mg/kg|mg|g|µg|mcg|ui|iu|%|pc)',
        lambda m: m.group(1).replace(',','.') + m.group(2).upper().replace('µG','MCG').replace('UI','UI').replace('/','/'),
        s, flags=re.IGNORECASE
    )
    return s

def parse_condt(condt):
    """Extrait qty et form depuis 'Condt produit'. Retourne ('30', 'CPR') par ex."""
    condt = remove_accents(condt.lower().strip())
    # Cherche qty au début
    m = re.match(r'^(\d+(?:[.,]\d+)?)\s*(.*)', condt)
    if not m:
        # qty=1 maybe
        qty = '1'
        form_raw = condt
    else:
        qty = m.group(1).replace(',', '.')
        # clean float qty: "60.0" → "60"
        try:
            qf = float(qty)
            qty = str(int(qf)) if qf == int(qf) else qty
        except:
            pass
        form_raw = m.group(2).strip()

    # Normalize form
    form = None
    for pattern, abbr in FORM_MAP.items():
        if re.search(pattern, form_raw, re.IGNORECASE):
            form = abbr
            break

    if form is None:
        # fallback: uppercase first word
        form = form_raw.split()[0].upper() if form_raw.split() else 'U'

    return qty, form

def build_libelle(specialite, condt):
    """Construit le libellé normalisé depuis la spécialité + conditionnement."""
    # 1. Remove brand identifiers
    name = BRAND_RE.sub('', specialite)
    name = CONTEXT_RE.sub('', name)
    name = re.sub(r'\s+', ' ', name).strip()

    # 2. Extract modifier LP/LM
    modifier = ''
    lp_match = re.search(r'\b(L\.P\.|LP|LM)\b', name, re.IGNORECASE)
    if lp_match:
        modifier = ' LP' if 'P' in lp_match.group(0).upper() else ' LM'
        name = name[:lp_match.start()] + name[lp_match.end():]
        name = re.sub(r'[\s.]+', ' ', name).strip()

    # 3. Split DCI vs dosage: dosage starts at first digit-with-unit pattern
    dose_start = re.search(r'\d', name)
    if dose_start:
        dci_raw = name[:dose_start.start()].strip()
        dose_raw = name[dose_start.start():].strip()
    else:
        dci_raw = name
        dose_raw = ''

    # 4. Remove accents and uppercase DCI
    dci_upper = remove_accents(dci_raw).upper().strip().rstrip('/')

    # 5. Apply DCI abbreviations
    dci_final = DCI_ABBREV.get(dci_upper, dci_upper)

    # 6. Normalize dosage
    dose_norm = normalize_dosage(dose_raw) if dose_raw else ''

    # 7. Parse condt
    qty, form = parse_condt(condt)

    # 8. Assemble
    parts = [dci_final + modifier]
    if dose_norm:
        parts.append(dose_norm)
    parts.append(qty + form)

    return ' '.join(parts)

# ---------------------------------------------------------------------------
# Parse PFHT
# ---------------------------------------------------------------------------

def parse_pfht(s):
    if not s:
        return None
    s = re.sub(r'\s+', '', s)
    s = s.replace(',', '.')
    try:
        return float(s)
    except:
        return None

# ---------------------------------------------------------------------------
# Extraction PDF
# ---------------------------------------------------------------------------

def extract_pdf(pdf_path):
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not row[0] or len(row[0]) != 13 or not row[0].isdigit():
                        continue
                    cip13 = row[0]
                    specialite = (row[1] or '').strip()
                    condt = (row[2] or '').strip()
                    pfht = parse_pfht(row[4] or '')
                    if pfht is None:
                        continue
                    libelle = build_libelle(specialite, condt)
                    rows.append((cip13, libelle, pfht))
    # Deduplicate (last seen wins for same CIP)
    seen = {}
    for r in rows:
        seen[r[0]] = r
    return list(seen.values())

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Extraction du PDF…")
    rows = extract_pdf(PDF_PATH)
    print(f"  → {len(rows)} CIP13 extraits")

    # Show sample
    for r in rows[:8]:
        print(f"  {r[0]}  {r[2]:.2f}€  {r[1]}")

    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS, dbname=DB_NAME
    )
    cur = conn.cursor()

    # Fetch existing Zydus CIPs
    cur.execute("SELECT cip13, libelle, rsf_pct FROM references_pharmacie WHERE labo=%s", (LABO,))
    db_existing = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    print(f"\nDB existant: {len(db_existing)} refs Zydus")

    updates = []  # (pfht, cip13)
    inserts = []  # (cip13, labo, libelle, puht, rsf_pct)

    for cip13, libelle, pfht in rows:
        if cip13 in db_existing:
            updates.append((pfht, cip13))
        else:
            inserts.append((cip13, LABO, libelle, pfht, DEFAULT_RSF))

    print(f"À mettre à jour : {len(updates)}")
    print(f"À insérer (nouveaux) : {len(inserts)}")
    if inserts:
        print("Nouveaux refs (sample) :")
        for r in inserts[:10]:
            print(f"  {r[0]}  {r[3]:.2f}€  {r[2]}")

    # Update PFHT for existing
    if updates:
        cur.executemany(
            "UPDATE references_pharmacie SET puht=%s WHERE cip13=%s AND labo='Zydus'",
            updates
        )
        print(f"  → {cur.rowcount} PFHT mis à jour")

    # Insert new
    if inserts:
        execute_values(cur, """
            INSERT INTO references_pharmacie (cip13, labo, libelle, puht, rsf_pct)
            VALUES %s
            ON CONFLICT (cip13) DO UPDATE
              SET puht    = EXCLUDED.puht,
                  libelle = EXCLUDED.libelle,
                  labo    = EXCLUDED.labo
        """, inserts)
        print(f"  → {len(inserts)} nouvelles refs insérées")

    conn.commit()

    # Recap
    cur.execute("SELECT COUNT(*) FROM references_pharmacie WHERE labo=%s", (LABO,))
    total = cur.fetchone()[0]
    print(f"\nTotal Zydus dans la base: {total}")

    cur.close()
    conn.close()
    print("Terminé.")

if __name__ == "__main__":
    main()
