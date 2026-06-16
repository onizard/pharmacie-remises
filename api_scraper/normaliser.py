"""
Pipeline de normalisation des libellés de références pharmaceutiques.
Extrait de extraire_excel.py — utilisé par main.py (Render) lors de l'import CGV.
"""

import re

# ── Abréviations de labos à supprimer ────────────────────────────────────────
ABREV_LABOS = sorted([
    "ARROW", "ARRW", "ARR", "ARL",
    "BIOGARAN", "BIO", "BGR",
    "VIATRIS", "VIA", "MYL", "MYP",
    "PFIZER", "PFI",
    "SANDOZ", "SDZ", "SAN",
    "ZENTIVA", "ZENT", "ZEN",
    "TEVA", "TEV",
    "CRISTERS", "CRI",
    "ZYDUS", "ZYD",
    "CORREVIO", "CPH",
    "ABACUS",
    "STA",
    "EG", "GE", "ZTL", "REF", "SA", "QVL", "NOR", "KS", "QIL", "SUB",
], key=len, reverse=True)

MOLECULES_PROTEGEES = {'H', 'A'}

DCI_CORRECTIONS = {
    r'\bCEFTRIAXIONE\b':    'CEFTRIAXONE',
    r'\bCLARITHROMYCYNE\b': 'CLARITHROMYCINE',
}

UNITES_DOSE = r'(?:MG/ML|MG|MCG|µG|UG|NG|G/ML|ML|UI/ML|MUI|MMOL|MOL|PC|%|G(?![A-Z]))'
FORMES = r'(?:SERING|STYLO|CPR|GELU|CAPS|COMP|AMP|SOL|PDR|CRE|GEL|POM|SUP|SPA|INJ|PERF|DISP|BUV|GTT|SACH|VERN|VERNIS|SPRAY|NAS|OPH|EAR|CPS|SEC|ORO|LP|LA|LI)'

# Corrections de libellés par CIP13 (dosages tronqués dans les PDFs source)
LIBELLES_CORRIGES_CIP13 = {
    '3400930173534': 'TAMSULOSINE LP 0,4MG 30GELU',
    '3400937056984': 'TAMSULOSINE LP 0,4MG 30GELU',
    '3400938841817': 'TAMSULOSINE LP 0,4MG 30GELU',
    '3400941771033': 'TAMSULOSINE LP 0,4MG 30CPR',
    '3400937119832': 'TAMSULOSINE LP 0,4MG 30GELU',
    '3400937186438': 'TAMSULOSINE LP 0,4MG 30GELU',
    '3400937185608': 'TAMSULOSINE LP 0,4MG 30GELU',
    '3400937076807': 'TAMSULOSINE LP 0,4MG 30GELU',
    '3400930111963': 'TAMSULOSINE LP 0,4MG 30GELU',
    '3400930012314': 'PRAMIPEXOLE LP 0,52MG 30CPR',
    '3400934832468': 'ALPRAZOLAM PDA 0,5MG 30CPR',
    '3400934837432': 'ALPRAZOLAM PDA 0,25MG 30CPR',
    '3400930208977': 'REPAGLINIDE PDA 0,5MG 90CPR',
}


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
    lib = libelle.upper().strip()

    def norm_dose(dose):
        return re.sub(r'(\d+)MG(\d)\b(?!\d)', lambda x: f"{x.group(1)}.{x.group(2)}MG", dose)

    m = re.search(r'\bBISOPROLOL\s+H\b.*?(\d+(?:\.\d+)?MG\d?)\b', lib, re.IGNORECASE)
    if m:
        dose = norm_dose(m.group(1))
        reste = lib[m.end():]
        return f"BISOPROLOL/HYD {dose}/6.25MG{reste}"

    m = re.search(r'\bBISOPROLOL\s+(\d+)MG\s+(\d{1,2})(CPR|GELU|COMP)\s+(\d+)\s*(?:SEC)?\b', lib, re.IGNORECASE)
    if m:
        dose = f'{m.group(1)}.{m.group(2)}MG'
        qty  = m.group(4)
        form = m.group(3).upper()
        reste = lib[m.end():]
        return f'BISOPROLOL {dose} {qty}{form}{reste}'

    if re.search(r'\bAMOXICILLINE\s+A\b', lib, re.IGNORECASE):
        m_pdr = re.search(r'\bAMOXICILLINE\s+A\b\s+(?:\S+\s+)*PDR\s+(?:EN|NN)\s*(\d+)\s*ML', lib, re.IGNORECASE)
        if m_pdr:
            return f"AMOX/CLAV PDR {m_pdr.group(1)}ML"
        RATIO = {'125MG': '31.25MG', '250MG': '62.5MG', '500MG': '62.5MG', '875MG': '125MG', '1G': '125MG'}
        for amox, clav in RATIO.items():
            if amox in lib:
                idx = lib.find(amox) + len(amox)
                reste = lib[idx:]
                return f"AMOX/CLAV {amox}/{clav}{reste}"
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
    libelle = re.sub(r'\b(\d+)\s*SAC\b', r'\1SACH', libelle, flags=re.IGNORECASE)
    libelle = re.sub(r'\b(\d+)S\b', r'\1SACH', libelle, flags=re.IGNORECASE)
    libelle = re.sub(r'\bGLE\b', 'PDR', libelle, flags=re.IGNORECASE)
    libelle = re.sub(r'\b(\d+)AM\b', r'\1AMP', libelle, flags=re.IGNORECASE)
    FORMES_LIST = ['SERING', 'STYLO', 'GELU', 'CAPS', 'COMP', 'SUPP', 'SACH', 'VERN', 'VERNIS',
                   'SPRAY', 'PERF', 'DISP', 'CPR', 'AMP', 'SOL', 'PDR',
                   'CRE', 'GEL', 'POM', 'SUP', 'SPA', 'INJ', 'BUV', 'GTT']
    FORMES_RE = '|'.join(FORMES_LIST)
    lib_sans_bt = re.sub(r'\bBT\s*\d+\b', '', libelle, flags=re.IGNORECASE)
    forme_match = re.search(r'\b(' + FORMES_RE + r')\b', lib_sans_bt, re.IGNORECASE)
    forme = forme_match.group(1).upper() if forme_match else 'CPR'
    libelle = re.sub(r'\bBT\s*(\d+)\s*(' + FORMES_RE + r')\b', r'\1\2', libelle, flags=re.IGNORECASE)
    libelle = re.sub(r'\bBT\s*(\d+)\b', lambda m: f"{m.group(1)}{forme}", libelle, flags=re.IGNORECASE)
    libelle = re.sub(
        r'\b(' + FORMES_RE + r')\s+(\d+)(?:SEC)?\b(?!\s*(?:MG|G(?![A-Z])|ML|UI|MCG|PC|%))',
        lambda m: f"{m.group(2)}{m.group(1).upper()}",
        libelle, flags=re.IGNORECASE,
    )
    libelle = re.sub(r'\b(' + FORMES_RE + r')\s+(\d+\1)\b', r'\2', libelle, flags=re.IGNORECASE)
    return re.sub(r' {2,}', ' ', libelle).strip()


def normaliser_concentration(libelle: str) -> str:
    def convertir(m):
        pct = float(m.group(1).replace(',', '.'))
        mg_ml = pct * 10
        val = int(mg_ml) if mg_ml == int(mg_ml) else mg_ml
        return f"{val}MG/ML"
    return re.sub(
        r'(\d+(?:[.,]\d+)?)\s*(?:PC|%)\s+\d+(?:[.,]\d+)?\s*ML',
        convertir, libelle, flags=re.IGNORECASE,
    )


def inserer_espaces(libelle: str) -> str:
    libelle = re.sub(r'\b(COLLY|COL|INJ|GTT)(\d)', r'\1 \2', libelle, flags=re.IGNORECASE)
    libelle = re.sub(r'\b(\d+)MG(\d{2})\b', lambda m: f'{m.group(1)}.{m.group(2)}MG', libelle)
    libelle = re.sub(r'\b(\d+)MG(\d)\b(?!\d)', lambda m: f'{m.group(1)}.{m.group(2)}MG', libelle)
    libelle = re.sub(r'\b(0\.\d+)MG\s+(\d+)ML\b', r'\1MG/ML \2ML', libelle, flags=re.IGNORECASE)
    libelle = re.sub(
        r'(\d+(?:[.,]\d+)?' + UNITES_DOSE + r')(\d+(?=' + FORMES + r'))',
        r'\1 \2', libelle, flags=re.IGNORECASE,
    )
    return libelle


def parser_libelle(libelle: str) -> dict:
    lib = libelle.strip().upper()
    pda = bool(re.search(r'\bPDA\b', lib))
    lib = re.sub(r'\bPDA\b', '', lib).strip()
    lib = re.sub(r'\bFLACON\b', '', lib).strip()
    lib = supprimer_abrev(lib)
    lib = traiter_combinaisons(lib)
    lib = separer_molecules_collees(lib)
    lib = normaliser_concentration(lib)
    lib = normaliser_bt(lib)
    lib = inserer_espaces(lib)
    lib = re.sub(r' {2,}', ' ', lib).strip()

    _U = r'(?:MG/ML|MG|MCG|ML|UI|G(?![A-Z]))'
    pattern_dose = (r'(\d+(?:[.,]\d+)?' + _U + r'(?:/' + r'\d+(?:[.,]\d+)?' + _U + r')+'
                    r'|\d+(?:[.,]\d+)?(?:/\d+(?:[.,]\d+)?)*\s*(?:MG/ML|UI/ML|MUI|MG|MCG|ML|MMOL|MOL|PC|UI|%|G(?![A-Z]))'
                    r'|\d+/\d+(?:[.,]\d+)?)')
    doses = re.findall(pattern_dose, lib, re.IGNORECASE)
    dosage = ' '.join(d.strip() for d in doses) if doses else ''

    pattern_qte = r'(\d+)\s*(' + FORMES + r'(?:\s+' + FORMES + r')*)'
    qte_match = re.search(pattern_qte, lib, re.IGNORECASE)
    quantite = ''
    forme    = ''
    if qte_match:
        quantite = qte_match.group(1)
        forme    = qte_match.group(2).strip().upper()

    dci = lib
    for d in doses:
        dci = dci.replace(d, '')
    if qte_match:
        dci = dci[:dci.find(qte_match.group(0))] if qte_match.group(0) in dci else dci
    dci = re.sub(r'\d+', '', dci)
    dci = re.sub(r'[.,]', '', dci)
    dci = re.sub(r'(?<![A-Z])/|/(?![A-Z])', '', dci)
    dci = re.sub(r'\s+', ' ', dci).strip()

    suffixe = ''
    if qte_match:
        reste = lib[qte_match.end():].strip()
        reste = re.sub(r'\s+', ' ', reste).strip()
        for mot in ['SEC', 'DISP', 'SP', 'ORO', 'QUI', 'X', 'TB']:
            reste = re.sub(r'\b' + mot + r'\b', '', reste, flags=re.IGNORECASE)
        suffixe = re.sub(r'\s+', ' ', reste).strip()

    return {
        'dci':      dci,
        'pda':      pda,
        'dosage':   dosage.upper(),
        'quantite': quantite,
        'forme':    forme.upper(),
        'suffixe':  suffixe.upper(),
    }


def construire_libelle_normalise(parsed: dict) -> str:
    parts = [parsed['dci']]
    if parsed['pda']:
        parts.append('PDA')
    if parsed['dosage']:
        parts.append(parsed['dosage'])
    if parsed['quantite'] and parsed['forme']:
        parts.append(f"{parsed['quantite']}{parsed['forme']}")
    if parsed['suffixe']:
        parts.append(parsed['suffixe'])
    return ' '.join(p for p in parts if p)


def normaliser_libelle(lib_raw: str, cip13: str = '') -> str:
    """
    Point d'entrée principal.
    Applique le pipeline complet : nettoyage → parsing → reconstruction normalisée.
    Si cip13 est fourni et dans LIBELLES_CORRIGES_CIP13, retourne la valeur corrigée.
    """
    if cip13 and cip13 in LIBELLES_CORRIGES_CIP13:
        return LIBELLES_CORRIGES_CIP13[cip13]
    if not lib_raw or not lib_raw.strip():
        return lib_raw
    parsed = parser_libelle(lib_raw.strip().upper())
    result = construire_libelle_normalise(parsed)
    # Fallback : si le parsing a produit un libellé vide, garder l'original nettoyé
    if not result.strip():
        return supprimer_abrev(lib_raw.strip().upper())
    return result
