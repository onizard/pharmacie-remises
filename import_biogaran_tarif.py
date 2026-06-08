"""
Importe le CGV Biogaran 2026 dans references_pharmacie.
- Met à jour PFHT et rsf_pct pour les CIP13 existants
- Ajoute les nouveaux CIP13 avec libellé normalisé
"""

import re
import unicodedata
import pdfplumber
import psycopg2
from psycopg2.extras import execute_values

PDF_PATH = "cgv labos/cgv biogaran 2026.pdf"
LABO = "Biogaran"

DB_HOST = "aws-0-eu-west-1.pooler.supabase.com"
DB_PORT = 5432
DB_USER = "postgres.fmterazwesiwpwjpkyqi"
DB_PASS = "lDXWqP1SsuchEIRH"
DB_NAME = "postgres"

# ---------------------------------------------------------------------------
# Normalisation des libellés (même logique que import_zydus_tarif)
# ---------------------------------------------------------------------------

def remove_accents(s):
    s = s.replace('µ', 'u').replace('μ', 'u').replace('®', '')
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )

BRAND_RE = re.compile(
    r'\b(Biogaran|Biogaran France)\b',
    re.IGNORECASE
)
CONTEXT_RE = re.compile(r'\(.*?\)')

FORM_MAP = {
    r'\bcp\b': 'CPR',
    r'\bgel\b': 'GELU',
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
    r'\bcomprimes? secables?\b': 'CPR',
    r'\bcomprimes? gastro.resistants?\b': 'CPR',
    r'\bcomprimes?\b': 'CPR',
    r'\bgelule capsule\b': 'GELU',
    r'\bgelule\b': 'GELU',
    r'\bcapsule\b': 'GELU',
    r'\bcapsules\b': 'GELU',
    r'\bpoudre.*solvant\b': 'PDR',
    r'\bpoudre\b': 'PDR',
    r'\bsolution\b': 'SOL',
    r'\bsolutions?\b': 'SOL',
    r'\bsuppos\b': 'SUP',
    r'\bsuppositoire\b': 'SUP',
    r'\bpatch\b': 'PATCH',
    r'\bcreme\b': 'CREME',
    r'\bpommade\b': 'POMM',
    r'\bcollyres?\b': 'COLLY',
    r'\bspray\b': 'SPR',
    r'\bsirop\b': 'SIR',
    r'\binj\b': 'INJ',
    r'\binjection\b': 'INJ',
    r'\binhalation\b': 'INH',
    r'\bsol\b': 'SOL',
    r'\blyoph\b': 'LYOPH',
}

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
    'LOSARTAN H': 'LOSAR/HCT',
    'IRBESARTAN/HYDROCHLOROTHIAZIDE': 'IRBES/HCT',
    'OLMÉSARTAN/HYDROCHLOROTHIAZIDE': 'OLMES/HCT',
    'OLMESARTAN/HYDROCHLOROTHIAZIDE': 'OLMES/HCT',
    'TELMISARTAN/HYDROCHLOROTHIAZIDE': 'TELMI/HCT',
    'PERINDOPRIL/AMLODIPINE': 'PERIND/AMLODIP',
    'ABIRATERONE ACETATE': 'ABIRATERONE',
    'ABIRATÉRONE ACÉTATE': 'ABIRATERONE',
    'CHOLÉCALCIFÉROL': 'CHOLECALCIFEROL',
    'CHOLECALCIFEROL': 'CHOLECALCIFEROL',
    'CALCIUM/VITAMINE D3': 'CALC/VIT D3',
    'ABACAVIR/LAMIVUDINE/ZIDOVUDINE': 'ABACAVIR LAMIV ZID',
    'ABACAVIR LAMIVUDINE ZIDOVUDINE': 'ABACAVIR LAMIV ZID',
}

def normalize_dosage(s):
    s = re.sub(
        r'(\d+[.,]?\d*)\s*(mg|g|µg|mcg|mg/5ml|mg/ml|g/l|ui|iu|%|pc)\s*/\s*'
        r'(\d+[.,]?\d*)\s*(mg|g|µg|mcg|mg/5ml|mg/ml|g/l|ui|iu|%|pc)',
        lambda m: (
            m.group(1).replace(',', '.') + '/' +
            m.group(3).replace(',', '.') +
            m.group(4).upper().replace('µG', 'MCG').replace('UI', 'UI')
        ),
        s, flags=re.IGNORECASE
    )
    s = re.sub(
        r'(\d+[.,]?\d*)\s*(mg/5ml|mg/ml|g/l|mg/kg|mg|g|µg|mcg|ui|iu|%|pc)',
        lambda m: m.group(1).replace(',', '.') + m.group(2).upper().replace('µG', 'MCG'),
        s, flags=re.IGNORECASE
    )
    return s

def parse_condt(condt):
    condt = remove_accents(condt.lower().strip())
    m = re.match(r'^(\d+(?:[.,]\d+)?)\s*(.*)', condt)
    if not m:
        qty = '1'
        form_raw = condt
    else:
        qty = m.group(1).replace(',', '.')
        try:
            qf = float(qty)
            qty = str(int(qf)) if qf == int(qf) else qty
        except:
            pass
        form_raw = m.group(2).strip()

    form = None
    for pattern, abbr in FORM_MAP.items():
        if re.search(pattern, form_raw, re.IGNORECASE):
            form = abbr
            break
    if form is None:
        form = form_raw.split()[0].upper() if form_raw.split() else 'U'

    return qty, form

def build_libelle(specialite, condt):
    name = BRAND_RE.sub('', specialite)
    name = CONTEXT_RE.sub('', name)
    name = re.sub(r'®', '', name)
    name = re.sub(r'\s+', ' ', name).strip()

    modifier = ''
    lp_match = re.search(r'\b(L\.P\.|LP|LM)\b', name, re.IGNORECASE)
    if lp_match:
        modifier = ' LP' if 'P' in lp_match.group(0).upper() else ' LM'
        name = name[:lp_match.start()] + name[lp_match.end():]
        name = re.sub(r'[\s.]+', ' ', name).strip()

    dose_start = re.search(r'\d', name)
    if dose_start:
        dci_raw = name[:dose_start.start()].strip()
        dose_raw = name[dose_start.start():].strip()
    else:
        dci_raw = name
        dose_raw = ''

    dci_upper = remove_accents(dci_raw).upper().strip().rstrip('/')
    dci_final = DCI_ABBREV.get(dci_upper, dci_upper)

    dose_norm = normalize_dosage(dose_raw) if dose_raw else ''

    qty, form = parse_condt(condt)

    parts = [dci_final + modifier]
    if dose_norm:
        parts.append(dose_norm)
    parts.append(qty + form)

    return ' '.join(parts)

# ---------------------------------------------------------------------------
# Parse PFHT et RSF depuis le PDF
# ---------------------------------------------------------------------------

def parse_pfht(s):
    if not s: return None
    s = re.sub(r'[€\s]', '', s).replace(',', '.')
    try: return float(s)
    except: return None

def parse_rsf(s):
    if not s: return None
    s = s.strip().rstrip('%').replace(',', '.')
    try: return -abs(float(s))
    except: return None

# ---------------------------------------------------------------------------
# Extraction PDF
# ---------------------------------------------------------------------------

def extract_pdf(pdf_path):
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    row = [c for c in row if c is not None]
                    # Trouver le CIP13
                    cip13 = None
                    for cell in row:
                        cell = (cell or '').strip()
                        if len(cell) == 13 and cell.isdigit():
                            cip13 = cell
                            break
                    if not cip13:
                        continue
                    # Trouver le PFHT (contient €)
                    pfht = None
                    for cell in reversed(row):
                        if '€' in (cell or ''):
                            pfht = parse_pfht(cell)
                            break
                    if pfht is None:
                        continue
                    # Trouver le RSF (contient %, pas €)
                    rsf_pct = None
                    for cell in reversed(row):
                        if '%' in (cell or '') and '€' not in (cell or ''):
                            rsf_pct = parse_rsf(cell)
                            break
                    # Specialité = texte le plus long (hors CIP, prix, RSF, chiffres purs)
                    specialite = ''
                    for cell in row:
                        cell = (cell or '').strip()
                        if (cell == cip13 or '€' in cell or '%' in cell or
                                cell.isdigit() or len(cell) <= 2):
                            continue
                        if len(cell) > len(specialite):
                            specialite = cell
                    # Conditionnement = 2e texte le plus long
                    condt = ''
                    for cell in row:
                        cell = (cell or '').strip()
                        if (cell == cip13 or '€' in cell or '%' in cell or
                                cell.isdigit() or len(cell) <= 2 or cell == specialite):
                            continue
                        if len(cell) > len(condt):
                            condt = cell
                    if not specialite or not condt:
                        continue
                    libelle = build_libelle(specialite, condt)
                    rows.append((cip13, libelle, pfht, rsf_pct))

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
    for r in rows[:8]:
        print(f"  {r[0]}  {r[2]:.2f}€  rsf={r[3]}  {r[1]}")

    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS, dbname=DB_NAME
    )
    cur = conn.cursor()

    cur.execute("SELECT cip13, libelle, rsf_pct FROM references_pharmacie WHERE labo=%s", (LABO,))
    db_existing = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    print(f"\nDB existant: {len(db_existing)} refs {LABO}")

    updates = []  # (pfht, rsf_pct, cip13)
    inserts = []  # (cip13, labo, libelle, puht, rsf_pct)

    for cip13, libelle, pfht, rsf_pct in rows:
        if cip13 in db_existing:
            updates.append((pfht, rsf_pct, cip13))
        else:
            inserts.append((cip13, LABO, libelle, pfht, rsf_pct))

    print(f"À mettre à jour : {len(updates)}")
    print(f"À insérer (nouveaux) : {len(inserts)}")
    if inserts:
        print("Nouveaux refs (sample) :")
        for r in inserts[:10]:
            print(f"  {r[0]}  {r[3]:.2f}€  rsf={r[4]}  {r[2]}")

    if updates:
        cur.executemany(
            "UPDATE references_pharmacie SET puht=%s, rsf_pct=%s WHERE cip13=%s AND labo='Biogaran'",
            updates
        )
        print(f"  → {cur.rowcount} refs mises à jour (PFHT + RSF)")

    if inserts:
        execute_values(cur, """
            INSERT INTO references_pharmacie (cip13, labo, libelle, puht, rsf_pct)
            VALUES %s
            ON CONFLICT (cip13) DO UPDATE
              SET puht    = EXCLUDED.puht,
                  rsf_pct = EXCLUDED.rsf_pct,
                  libelle = EXCLUDED.libelle,
                  labo    = EXCLUDED.labo
        """, inserts)
        print(f"  → {len(inserts)} nouvelles refs insérées")

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM references_pharmacie WHERE labo=%s", (LABO,))
    total = cur.fetchone()[0]
    print(f"\nTotal {LABO} dans la base: {total}")

    cur.close()
    conn.close()
    print("Terminé.")

if __name__ == "__main__":
    main()
