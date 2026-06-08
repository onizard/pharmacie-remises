"""
Extraction des lignes produits depuis les PDFs de factures DIGIPHARMACIE.

Formats supportés :
  1. ALLOGA FRANCE — factures au nom d'un labo (format liste per-CIP)
  2. VIATRIS / MYLAN — factures directes labo (table pdfplumber 6 colonnes)
  3. COOPERATION PHARMACEUTIQUE — factures répartiteur (EAN13 + PU brut/net)
  4. Fallback texte brut — regex CIP13 + valeurs numériques

Seules les lignes dont le labo (extrait du PDF) appartient à LABOS_CIBLES
sont retournées. Les autres labos sont ignorés.
"""

import re
from pathlib import Path

import pdfplumber

# ── Labos génériqueurs cibles ─────────────────────────────────────────────────

LABOS_CIBLES = {
    "biogaran", "teva", "mylan", "viatris", "zydus",
    "sandoz", "zentiva", "arrow", "cristers",
    "eg labo", "eg labs", "evolupharm",
}


def _is_labo_cible(labo: str) -> bool:
    n = (labo or "").lower()
    return any(kw in n for kw in LABOS_CIBLES)


def _is_generic_designation(designation: str) -> bool:
    """Vérifie si la désignation d'un produit contient un nom de labo génériqueur."""
    n = (designation or "").lower()
    return any(kw in n for kw in LABOS_CIBLES)


# ── Helpers numériques ─────────────────────────────────────────────────────────

def _to_float(s: str) -> float | None:
    if not s:
        return None
    s = str(s).strip().replace('\xa0', '').replace(' ', '').replace(' ', '').replace(',', '.')
    m = re.search(r'[\d.]+', s)
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def _to_float_signed(s: str) -> float | None:
    """Comme _to_float mais préserve le signe négatif (pour les avoirs RDP)."""
    if not s:
        return None
    s = str(s).strip().replace('\xa0', '').replace('\u202f', '').replace(' ', '')
    s = s.replace('€', '').replace('\u2212', '-').replace('\u2013', '-').replace(',', '.')
    neg = s.startswith('-')
    s   = s.lstrip('-')
    import re as _re
    m   = _re.search(r'[\d.]+', s)
    try:
        val = float(m.group()) if m else None
        return (-val if neg else val) if val is not None else None
    except (ValueError, AttributeError):
        return None



def _parse_remise_str(s: str) -> float | None:
    """Parse '5-%', '5,00%', '17.5%' → float."""
    if not s:
        return None
    m = re.search(r'(\d+)[.,\-]?(\d*)', s.replace('%', ''))
    if not m:
        return None
    integer_part = m.group(1)
    decimal_part = m.group(2) or '0'
    try:
        return float(f"{integer_part}.{decimal_part}")
    except ValueError:
        return None


RE_CIP13 = re.compile(r'\b(3[0-9]\d{11})\b')


# ── Détection du format ────────────────────────────────────────────────────────

def _detect_format(text: str) -> str:
    t400 = text[:400].lower()
    if "alloga france" in t400 or ("alloga" in t400 and "au nom et pour le compte" in text[:600].lower()):
        return "alloga"
    # CSP / Movianto : "FACTURE DE SPÉCIALITÉS" avec lignes CIP13 décimales en points
    if ("facture de sp" in text[:800].lower() and "cialité" in text[:800].lower()) \
            or "spécialités remboursées" in text[:1500].lower() \
            or "centre sp" in text[:800].lower():
        return "csp"
    # RDP (Remise de performance) / Avoir récapitulatif — pas de données par référence
    if "récapitulatif des remises" in text[:600].lower() or "remise de performance" in text[:800].lower():
        return "rdp"
    # Prestations de services / Coopération commerciale — pas de CIP
    if "facture de prestations de services" in text[:600].lower() or "convention commerciale" in text[:800].lower():
        return "presta"
    if "viatris sante" in t400 or "viatris santé" in t400 or ("mylan" in t400 and "c.i.p" in text[:1000].lower()):
        return "viatris"
    if "cooperation pharmaceutique" in t400 or "coopération pharmaceutique" in t400:
        return "cooperation"
    return "unknown"


def _extract_lab_from_header(text: str) -> str:
    """Extrait le labo depuis 'AU NOM ET POUR LE COMPTE DE → Facturé à → {labo}'."""
    m = re.search(
        r"AU NOM ET POUR LE COMPTE DE\s*\n\s*Facturé\s+à\s*\n\s*(.+)",
        text, re.IGNORECASE
    )
    if not m:
        m = re.search(r"AU NOM ET POUR LE COMPTE DE\s*\n\s*(.+)", text, re.IGNORECASE)
    if m:
        lab = m.group(1).strip()
        lab = re.sub(r'\s*N°?\s*(?:client|compte|c\.?|clt)\b.*', '', lab, flags=re.IGNORECASE).strip()
        lab = re.split(r'\s{3,}|\n', lab)[0].strip()
        return lab
    return ""


# ── 1. Format ALLOGA ───────────────────────────────────────────────────────────

# Ligne produit Alloga :
#   3400938254518 GAVISCONELL SUSP SSUCRE X12 12 7,700 35,00% 5,005 60,06 10,00%
_PCT = r'(\d{1,3},\d{2})%'
RE_ALLOGA_LINE = re.compile(
    r'^(\d{13})\s+'
    r'(.+?)\s+'
    r'(\d+)\s+'
    r'([\d,\s]+?)\s+'
    + _PCT + r'\s+'
    + r'([\d,\s]+?)\s+'
    + r'([\d,\s]+?)\s+'
    + _PCT + r'\s*$',
    re.MULTILINE,
)


def _extract_alloga(text: str, provider: str, billing_date: str) -> list[dict]:
    lab_name = _extract_lab_from_header(text)
    lines = []
    for raw_line in text.splitlines():
        m = RE_ALLOGA_LINE.match(raw_line.strip())
        if not m:
            continue
        cip13    = m.group(1)
        libelle  = m.group(2).strip()
        qte_str  = m.group(3)
        pubrut_s = m.group(4)
        remise_s = m.group(5)
        punet_s  = m.group(6)
        total_s  = m.group(7)

        pu_brut  = _to_float(pubrut_s)
        remise   = _to_float(remise_s)
        pu_net   = _to_float(punet_s)
        total_ht = _to_float(total_s)
        qte      = int(qte_str) if qte_str.isdigit() else None

        if pu_brut is None and pu_net is None:
            continue
        lines.append({
            "cip": cip13, "libelle": libelle, "labo": lab_name,
            "fournisseur": provider, "billing_date": billing_date,
            "quantite": qte, "prix_brut": round(pu_brut, 4) if pu_brut else None,
            "remise_pct": round(remise, 2) if remise is not None else None,
            "pa_net": round(pu_net, 4) if pu_net else None,
            "total_ht": round(total_ht, 2) if total_ht else None,
        })
    return lines


# ── 2. Format VIATRIS / MYLAN ─────────────────────────────────────────────────

def _extract_viatris(pdf, provider: str, billing_date: str) -> list[dict]:
    """
    Facture Viatris/Mylan — table pdfplumber 6 colonnes (cellules multi-lignes).

    Ligne data : [CIP13, Designation, "Qty\\nRemise%\\nLot", "PU_brut\\nPU_net\\nDate", "Montant_brut\\nMontant_net\\nDate", TVA%]
    Quand pas de remise : [CIP13, Designation, "Qty\\nLot", "PU_brut\\nDate", "Montant_brut\\nDate", TVA%]
    """
    lab_name = ""
    lines    = []

    for page in pdf.pages:
        text = page.extract_text() or ""
        if not lab_name:
            # Viatris met son nom en tête sans "AU NOM ET POUR LE COMPTE DE"
            m = re.search(r'(VIATRIS\s+SANT[EÉ]|MYLAN\s+SAS|MYLAN)\b', text, re.IGNORECASE)
            if m:
                lab_name = m.group(1)

        for tbl in page.extract_tables():
            if not tbl or len(tbl) < 2:
                continue
            # Vérifier que c'est le tableau produits (colonnes C.I.P, Designation, Quantite…)
            header_str = " ".join(str(c or "") for c in tbl[0]).lower()
            if "c.i.p" not in header_str and "designation" not in header_str:
                continue

            for row in tbl[1:]:
                if not row or len(row) < 6:
                    continue
                cip_raw = str(row[0] or "").strip()
                # Ignorer les lignes d'en-tête secondaires ou vides
                if not RE_CIP13.match(cip_raw.split('\n')[0]):
                    continue

                cip13   = cip_raw.split('\n')[0].strip()
                libelle = str(row[1] or "").split('\n')[0].strip()

                # col[2] = "qty\\nremise%\\nlot"  ou  "qty\\nlot"
                col2 = str(row[2] or "").split('\n')
                qty_str    = col2[0].strip() if col2 else ""
                remise_str = col2[1].strip() if len(col2) >= 3 else ""

                # col[3] = "pu_brut EUR\\npu_net EUR\\ndate"  ou  "pu_brut EUR\\ndate"
                col3   = str(row[3] or "").split('\n')
                pubrut = _to_float(col3[0])
                punet  = _to_float(col3[1]) if len(col3) >= 3 else None

                # col[4] = "montant_brut EUR\\nmontant_net EUR\\ndate"  ou  "montant_brut EUR\\ndate"
                col4       = str(row[4] or "").split('\n')
                total_brut = _to_float(col4[0])
                total_net  = _to_float(col4[1]) if len(col4) >= 3 else None

                if pubrut is None:
                    continue

                # Calculer la remise si non fournie explicitement
                remise = _parse_remise_str(remise_str) if remise_str and '%' in remise_str else None
                if remise is None and pubrut and punet and pubrut > 0:
                    remise = round((1 - punet / pubrut) * 100, 2)

                qte = None
                m_qty = re.search(r'(\d+)', qty_str)
                if m_qty:
                    qte = int(m_qty.group(1))

                lines.append({
                    "cip": cip13, "libelle": libelle, "labo": lab_name or "VIATRIS SANTE",
                    "fournisseur": provider, "billing_date": billing_date,
                    "quantite": qte,
                    "prix_brut": round(pubrut, 4) if pubrut else None,
                    "remise_pct": round(remise, 2) if remise is not None else None,
                    "pa_net": round(punet, 4) if punet else None,
                    "total_ht": round(total_net or total_brut, 2) if (total_net or total_brut) else None,
                })
    return lines


# ── 3. Format COOPÉRATION PHARMACEUTIQUE ──────────────────────────────────────

# Ligne produit Coopération :
#   1179070 3614810007097 POUXIT PEIGNE A/POUX 6 6 10,05 5,53 33,18 5
# Colonnes : code_int(7) | ean13(13) | designation | qty_liv | qty_fac |
#            [prix_fab] | pu_brut | pu_net | montant | tva_code
RE_COOP_LINE = re.compile(
    r'^\d{6,7}\s+'                  # code article interne
    r'(\d{13})\s+'                   # EAN13 / CIP13
    r'(.+?)\s+'                      # désignation (non-greedy)
    r'(\d+)\s+'                      # qté livrée
    r'(\d+)\s+'                      # qté facturée
    r'(?:\d[\d,]*\s+)?'              # prix fabricant (optionnel)
    r'(\d[\d,]*,\d{2})\s+'          # PU HT brut
    r'(\d[\d,]*,\d{2})\s+'          # PU HT net
    r'(\d[\d,]*,\d{2})\s+'          # montant HT
    r'\d+\s*$',                      # code TVA
    re.MULTILINE,
)


def _extract_cooperation(text: str, provider: str, billing_date: str) -> list[dict]:
    """
    Factures répartiteur Coopération Pharmaceutique.
    Le labo n'est pas dans l'en-tête — on le détecte depuis la désignation.
    """
    lines = []
    for raw_line in text.splitlines():
        m = RE_COOP_LINE.match(raw_line.strip())
        if not m:
            continue
        ean13   = m.group(1)
        libelle = m.group(2).strip()
        # qte_liv = m.group(3)
        qte_fac = m.group(4)
        pubrut  = _to_float(m.group(5))
        punet   = _to_float(m.group(6))
        montant = _to_float(m.group(7))

        if pubrut is None or punet is None:
            continue

        # Calculer la remise
        remise = round((1 - punet / pubrut) * 100, 2) if pubrut > 0 else None

        # Détecter le labo depuis la désignation
        labo_detected = ""
        libelle_lc = libelle.lower()
        for kw in LABOS_CIBLES:
            if kw in libelle_lc:
                labo_detected = kw.upper()
                break

        lines.append({
            "cip": ean13, "libelle": libelle, "labo": labo_detected,
            "fournisseur": provider, "billing_date": billing_date,
            "quantite": int(qte_fac) if qte_fac.isdigit() else None,
            "prix_brut": round(pubrut, 4),
            "remise_pct": round(remise, 2) if remise is not None else None,
            "pa_net": round(punet, 4),
            "total_ht": round(montant, 2) if montant else None,
        })
    return lines


# ── 5. Format CSP / Movianto (factures de spécialités au nom d'un labo) ──────
#
# Ligne produit :
#   3400930285473  ARIPIPRAZOLE 10MG 28CP BGN  1600297  2  14.56  20.00  11.648  23.30  2.10
#   CIP13          Désignation                 Lot      Qty PU_HT  RSF%   PU_net  Mnt    TVA
#
# Décimaux avec point (pas de signe %). Lot = token alphanum avant la quantité.

RE_CSP_LINE = re.compile(
    r'^(3\d{12})\s+'       # CIP13
    r'(.+?)\s+'            # Désignation (non-greedy — s'arrête au lot)
    r'(\S+)\s+'            # Numéro de lot (alphanum, pas d'espace)
    r'(\d+)\s+'            # Quantité facturée
    r'(\d+\.\d+)\s+'      # Prix unitaire HT
    r'(\d+\.\d+)\s+'      # Remise (%)
    r'(\d+\.\d+)\s+'      # Prix unitaire après remise
    r'(\d+\.\d+)\s+'      # Montant HT
    r'(\d+\.\d+)',         # Taux TVA
    re.MULTILINE,
)


def _extract_csp_lab(text: str) -> str:
    """Détecte le labo depuis l'en-tête CSP (le nom du labo précède l'adresse)."""
    header = text[:800].upper()
    for kw in sorted(LABOS_CIBLES, key=len, reverse=True):
        if re.search(r'\b' + re.escape(kw.upper()) + r'\b', header):
            return kw.upper()
    return ""


def _extract_csp(text: str, provider: str, billing_date: str) -> list[dict]:
    """
    Facture de spécialités CSP / Movianto (dépositaire) au nom d'un labo générique.
    Ex : Biogaran, Teva, Arrow via Centre Spécialités Pharmaceutiques / Movianto.
    """
    lab_name = _extract_csp_lab(text) or _extract_lab_from_header(text) or provider
    lines = []
    for m in RE_CSP_LINE.finditer(text):
        cip13   = m.group(1)
        libelle = m.group(2).strip()
        # m.group(3) = numéro de lot (ignoré)
        qty_str = m.group(4)
        pubrut  = _to_float(m.group(5))
        remise  = _to_float(m.group(6))
        punet   = _to_float(m.group(7))
        montant = _to_float(m.group(8))
        # m.group(9) = TVA % (ignoré)

        if pubrut is None:
            continue

        lines.append({
            "cip":          cip13,
            "libelle":      libelle,
            "labo":         lab_name,
            "fournisseur":  provider,
            "billing_date": billing_date,
            "quantite":     int(qty_str) if qty_str.isdigit() else None,
            "prix_brut":    round(pubrut, 4),
            "remise_pct":   round(remise, 2) if remise is not None else None,
            "pa_net":       round(punet, 4) if punet else None,
            "total_ht":     round(montant, 2) if montant else None,
        })
    return lines


# ── 4. Fallback texte brut ─────────────────────────────────────────────────────

def _extract_text_fallback(text: str, provider: str, billing_date: str) -> list[dict]:
    RE_PRICE = re.compile(r'\b(\d{1,4}[,\.]\d{2,4})\b')
    RE_PCT   = re.compile(r'\b(\d{1,2}[,\.]\d{0,2})\s*%')
    lines = []
    for line in text.splitlines():
        m_cip = RE_CIP13.search(line)
        if not m_cip:
            continue
        cip = m_cip.group(1)
        prices = [float(p.replace(",", ".")) for p in RE_PRICE.findall(line)]
        pcts   = [float(p.replace(",", ".")) for p in RE_PCT.findall(line)]
        if not prices:
            continue
        prix_brut = max(prices)
        remise    = pcts[0] if pcts else None
        pa_net    = round(prix_brut * (1 - remise / 100), 4) if remise is not None else None
        libelle   = re.split(r'\d', line)[0].strip()[:80]
        lines.append({
            "cip": cip, "libelle": libelle, "labo": "",
            "fournisseur": provider, "billing_date": billing_date,
            "quantite": None, "prix_brut": round(prix_brut, 4),
            "remise_pct": remise, "pa_net": pa_net, "total_ht": None,
        })
    return lines


# ── 6. RDP — Récapitulatif Des remises (R2 mensuelle / R3 trimestrielle) ──────
#
# Format Biogaran : AVOIR "RÉCAPITULATIF DES REMISES"
# Texte pdfplumber (ex) :
#   RECAPITULATIF DES REMISES \nBIOGARAN \n...
#   Du 01/04/2026 au 30/04/2026
#   Date document : 22/05/2026
#   RDP 10.00% *** SUR [desc]\nACHATS GROSSISTES** [desc] 1 10,00 -870,43 € 0,00\n
#   C.A brut de référence grossistes partenaires :8 703,88 €
#   TOTAL -1 866,24 €

def _extract_rdp(text: str, provider: str, billing_date: str) -> list[dict]:
    # Labo : ligne après "RECAPITULATIF DES REMISES"
    labo = ""
    m = re.search(r'RECAPITULATIF\s+DES\s+REMISES\s*\n([\w ]+)', text, re.IGNORECASE)
    if m:
        labo = m.group(1).strip().split()[0]  # premier mot = nom du labo
    if not labo:
        for kw in sorted(LABOS_CIBLES, key=len, reverse=True):
            if kw.upper() in text[:1200].upper():
                labo = kw.upper(); break

    # Période de référence → clé du mois d'achat
    m_per = re.search(r'Du\s+(\d{2})/(\d{2})/(\d{4})\s+au\s+\d{2}/\d{2}/\d{4}', text, re.IGNORECASE)
    period_month = billing_date[:7]  # fallback = mois du document
    if m_per:
        period_month = f"{m_per.group(3)}-{m_per.group(2)}"

    # Date document → billing_date normalisé ISO
    m_date = re.search(r'Date\s+document\s*:\s*(\d{2})/(\d{2})/(\d{4})', text, re.IGNORECASE)
    if m_date:
        billing_date = f"{m_date.group(3)}-{m_date.group(2)}-{m_date.group(1)}"

    # Total avoir
    m_tot = re.search(r'\bTOTAL\b\s+([-−][\d\s,\.]+)\s*€', text)
    total = _to_float_signed(m_tot.group(1)) if m_tot else None
    if total is None:
        return []

    # Lignes détail RDP :
    # La ligne "ACHATS GROSSISTES** ... 1 10,00 -870,43 € 0,00" contient taux et montant
    rdp_lines = []
    for m_achats in re.finditer(
        r'(ACHATS\s+(?:GROSSISTE\w*|DIRECT\w*))[^\n]*?'
        r'\b1\b\s+([\d,\.]+)\s+([-−][\d\s,]+)\s*€',
        text, re.IGNORECASE
    ):
        canal   = 'GROSSISTE' if 'GROS' in m_achats.group(1).upper() else 'DIRECT'
        taux    = _to_float(m_achats.group(2))
        montant = _to_float_signed(m_achats.group(3))
        # CA brut de référence sur la ligne suivante
        tail    = text[m_achats.start():][:400]
        m_ca    = re.search(r'C\.A\s+brut\s+de\s+r[eé]f[eé]rence[^:]+:\s*([\d\s,\.]+)\s*€', tail, re.IGNORECASE)
        ca_brut = _to_float(m_ca.group(1)) if m_ca else None
        rdp_lines.append({"canal": canal, "taux": taux, "montant": montant, "ca_brut": ca_brut})

    return [{
        "type":         "rdp",
        "labo":         labo,
        "fournisseur":  provider,
        "billing_date": billing_date,
        "period_month": period_month,
        "montant":      total,      # négatif = avoir (argent reçu par la pharmacie)
        "total_ht":     total,
        "rdp_lines":    rdp_lines,
        "quantite":     0,
    }]


# ── 7. PRESTA — Facture de prestations de services / Coopération (R3) ─────────
#
# Format Movianto pour Biogaran : "FACTURE DE PRESTATIONS DE SERVICES CONVENTION COMMERCIALE"
# Texte pdfplumber dégradé (colonnes mélangées) — on extrait uniquement les données fiables :
#   BIOGARAN (dans les 1 000 premiers caractères)
#   Date Facture : 18/09/25
#   PAIEMENT ... 5450.00 20 1090.00 6540.00  (BRUT HT, TVA%, MONTANT TVA, NET TTC)
#   ou TOTAL 5 5450,00

def _extract_presta(text: str, provider: str, billing_date: str) -> list[dict]:
    # Labo
    labo = ""
    for kw in sorted(LABOS_CIBLES, key=len, reverse=True):
        if kw.upper() in text[:1200].upper():
            labo = kw.upper(); break
    if not labo:
        return []

    # Date (format DD/MM/YY ou DD/MM/YYYY)
    m_date = re.search(r'Date\s+Facture\s*:\s*(\d{2})/(\d{2})/(\d{2,4})', text, re.IGNORECASE)
    if m_date:
        yr = m_date.group(3)
        if len(yr) == 2:
            yr = "20" + yr
        billing_date = f"{yr}-{m_date.group(2)}-{m_date.group(1)}"

    # Ligne PAIEMENT : "Mode : VIREMENT  <HT>  <TVA%>  <TVA_MONTANT>  <TTC>"
    # Ex pdfplumber : "Mode : VIREMENT 5450.00 20 1090.00 6540.00"
    total_ht  = None
    total_ttc = None
    tva_pct   = None

    m_pay = re.search(
        r'Mode\s*:\s*VIREMENT\s+([\d\s,\.]+)\s+(\d+(?:[.,]\d+)?)\s+[\d\s,\.]+\s+([\d\s,\.]+)',
        text, re.IGNORECASE
    )
    if m_pay:
        total_ht  = _to_float(m_pay.group(1))
        tva_pct   = _to_float(m_pay.group(2))   # ex: 20
        total_ttc = _to_float(m_pay.group(3))   # ex: 6540.00

    # Fallback : ligne TOTAL (ex: "TOTAL 5 5450,00" → le dernier nombre)
    if total_ht is None:
        m_tot = re.search(r'\bTOTAL\b[^\n]*?([\d\s,\.]+)\s*$', text, re.MULTILINE)
        if m_tot:
            total_ht = _to_float(m_tot.group(1))

    if not total_ht:
        return []

    # Si TTC non extrait, calcul depuis TVA
    if total_ttc is None:
        pct = tva_pct if tva_pct else 20.0  # TVA 20% par défaut pour les prestations
        total_ttc = round(total_ht * (1 + pct / 100), 2)

    period_month = billing_date[:7]

    return [{
        "type":         "presta",
        "labo":         labo,
        "fournisseur":  provider,
        "billing_date": billing_date,
        "period_month": period_month,
        "montant":      total_ht,   # HT — positif = facture de coop (argent reçu)
        "total_ht":     total_ht,
        "total_ttc":    total_ttc,  # TTC = montant du virement bancaire (TVA 20%)
        "tva_pct":      tva_pct or 20.0,
        "quantite":     0,
    }]


# ── Point d'entrée public ──────────────────────────────────────────────────────

def extract_invoice_lines(pdf_path: Path, provider: str, billing_date: str) -> list[dict]:
    """
    Extrait les lignes d'une facture PDF. Pour les formats produits (alloga, csp,
    viatris, cooperation), retourne les lignes CIP. Pour les formats avoirs (rdp,
    presta), retourne une ligne de synthèse avec type="rdp" ou type="presta".
    """
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            fmt = _detect_format(full_text)

            if fmt == "alloga":
                lines = _extract_alloga(full_text, provider, billing_date)
            elif fmt == "csp":
                lines = _extract_csp(full_text, provider, billing_date)
            elif fmt == "viatris":
                lines = _extract_viatris(pdf, provider, billing_date)
            elif fmt == "cooperation":
                lines = _extract_cooperation(full_text, provider, billing_date)
            elif fmt == "rdp":
                lines = _extract_rdp(full_text, provider, billing_date)
            elif fmt == "presta":
                lines = _extract_presta(full_text, provider, billing_date)
            else:
                lines = _extract_text_fallback(full_text, provider, billing_date)
    except Exception:
        return []

    # Filtrer sur les labos génériqueurs cibles (s'applique à rdp/presta aussi)
    lines = [l for l in lines if _is_labo_cible(l.get("labo", ""))]

    # Déduplication : clé différente selon le type
    seen, dedup = set(), []
    for line in lines:
        if line.get("type") in ("rdp", "presta"):
            key = (line["type"], line["billing_date"], line.get("labo", ""), line.get("montant", 0))
        else:
            key = (line.get("cip"), line["billing_date"], line.get("libelle", "")[:20])
        if key not in seen:
            seen.add(key)
            dedup.append(line)

    return dedup
