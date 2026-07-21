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

# Grossistes partenaires qui envoient leurs propres ristournes/presta
_PRESTA_PROVIDER_MAP = {
    "cooperation pharmaceutique": "CERP",
    "cerp rouen": "CERP",
    "cerp rennes": "CERP",
    "alloga france": "ALLOGA",
    "alloga": "ALLOGA",
}
_PRESTA_PARTNER_LABOS = frozenset({"cerp", "alloga"})


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

def _fr_num(s: str) -> float:
    """Parse un nombre au format français : '24.430,28' ou '2.850,33' → float."""
    s = str(s).strip().replace('\xa0', '').replace(' ', '').replace(' ', '')
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def _detect_format(text: str) -> str:
    t400  = text[:400].lower()
    t800  = text[:800].lower()
    t1500 = text[:1500].lower()
    # MDL CERP — "STATISTIQUE DE LA MARGE DEGRESSIVE LISSEE"
    if "marge degressive lissee" in t800 or "marge dégressive lissée" in t800:
        return "mdl_cerp"
    # Relevé des Escomptes CERP — document mensuel agrégat (pas per-CIP)
    if "releve des escomptes" in t800 or "relevé des escomptes" in t800:
        return "escompte_cerp"
    # Relevé LCR Teva — bordereau de paiement (pas une facture)
    if "releve de lcr" in t800 or "relevé de lcr" in t800:
        return "lcr_releve"
    # CPF / CERP — facture produit sur relevé mensuel (pas une presta)
    if "facture payable sur releve" in t800 or "avoir deduit sur releve" in t800:
        return "cpf_product"
    # Teva — facture produit standard (distinct des avoirs RSF/presta). Détection
    # élargie à l'en-tête de colonnes « P.U Brut » : certaines factures Teva n'ont
    # pas « votre commande » dans les 1 500 premiers caractères et tombaient en
    # « unknown » (aucune ligne extraite).
    if "teva sant" in t400 and ("votre commande" in t1500 or "p.u brut" in text.lower()):
        return "teva_product"
    # RDP (Remise de performance) / Avoir récapitulatif — avant alloga pour éviter faux-positif
    # NB : le texte PDF est souvent en capitales SANS accent ("RECAPITULATIF DES REMISES")
    # → on accepte les deux formes, sinon l'avoir RDP n'est jamais détecté (0 ligne RDP).
    if ("récapitulatif des remises" in t800 or "recapitulatif des remises" in t800
            or "remise de performance" in t800):
        return "rdp"
    # Prestations de services / Coopération commerciale — AVANT alloga (Alloga envoie aussi des presta)
    _presta_kw = (
        "facture de prestations de services",
        "facture de prestation de services",   # variante singulier
        "convention commerciale",
        "convention de coopération",
        "convention de cooperation",
        "coopération commerciale",
        "cooperation commerciale",
        "prestation de coopération",
        "prestation de cooperation",
        "objectif de coopération",
        "objectif de cooperation",
    )
    if any(kw in t1500 for kw in _presta_kw):
        return "presta"
    if "alloga france" in t400 or ("alloga" in t400 and "au nom et pour le compte" in t800):
        return "alloga"
    # CSP / Movianto : "FACTURE DE SPÉCIALITÉS" avec lignes CIP13 décimales en points
    if ("facture de sp" in t800 and "cialité" in t800) \
            or "spécialités remboursées" in t1500 \
            or "centre sp" in t800:
        return "csp"
    if "viatris sante" in t400 or "viatris santé" in t400 or ("mylan" in t400 and "c.i.p" in t1500):
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


# ── 1b. Format TEVA SANTÉ (facture produit directe) ───────────────────────────
# Structure réelle (pdfplumber) : la désignation s'étale sur plusieurs lignes, le
# CIP13 est SEUL sur sa ligne, et les valeurs arrivent plus bas sur une ligne
# numérique « qté PU_brut remise% PU_net TVA% montant_HT », ex. :
#   AJOVY® 225 mg, solution injectable en seringue
#   3400930174593
#   préremplie, bte de 1 30049000
#   1 270,00 18,50% 220,05 2.10 % 220,05
# La ligne « Total … » a une autre forme (TVA% en tête) → non capturée.
RE_TEVA_NUM = re.compile(
    r'^(\d{1,4})\s+([\d\s.,]+?)\s+([\d.,]+)\s*%\s+([\d\s.,]+?)\s+([\d.,]+)\s*%\s+([\d\s.,]+)$'
)

def _extract_teva_product(text: str, provider: str, billing_date: str) -> list[dict]:
    lines_txt = text.split('\n')
    out: list[dict] = []
    pending_cips: list[tuple[str, str]] = []   # [(cip, désignation approx.)]
    last_desc = ""
    for raw in lines_txt:
        line = raw.strip()
        if not line:
            continue
        mcip = RE_CIP13.search(line)
        if mcip and len(line) <= 20:            # CIP seul sur sa ligne
            pending_cips.append((mcip.group(1), last_desc[:80]))
            continue
        mnum = RE_TEVA_NUM.match(line)
        if mnum and pending_cips:
            cip, desc = pending_cips.pop(0)
            qty      = int(mnum.group(1))
            total_ht = _fr_num(mnum.group(6))
            pu_net   = _fr_num(mnum.group(4))
            remise   = _parse_remise_str(mnum.group(3))
            if total_ht <= 0 or qty <= 0:
                continue
            out.append({
                "cip": cip, "libelle": desc or "TEVA", "labo": "TEVA",
                "quantite": qty, "pu_ht": pu_net, "total_ht": round(total_ht, 2),
                "remise_pct": remise,
                "fournisseur": provider, "billing_date": billing_date,
            })
            continue
        if not mcip:
            last_desc = line
    return out


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
    """Détecte le labo d'une facture CSP (dépositaire facturant pour plusieurs labos).

    CSP facture « d'ordre et pour compte » de labos différents : Biogaran l'indique
    EN TÊTE (« AU NOM ET POUR LE COMPTE DE BIOGARAN »), Zydus EN PIED, juste avant
    les lignes produits (« D'ORDRE ET POUR COMPTE DES LABORATOIRES ZYDUS FRANCE »).
    Lire seulement l'en-tête (800 c.) ratait donc Zydus → labo=nom de fichier →
    lignes jetées (non-cible) → achats directs Zydus invisibles. On cherche le
    donneur d'ordre PARTOUT, avec plusieurs filets.
    """
    up = text.upper()
    labos_sorted = sorted(LABOS_CIBLES, key=len, reverse=True)
    # 1. Mention explicite du donneur d'ordre : « POUR (LE) COMPTE (DES LABORATOIRES) X ».
    for m in re.finditer(
            r"POUR\s+(?:LE\s+)?COMPTE\s+(?:DES\s+LABORATOIRES?\s+|DU\s+|DE\s+|D['’]\s*)?"
            r"([A-Z][A-Z0-9\s\.\-/]{2,40})", up):
        seg = m.group(1)
        for kw in labos_sorted:
            if kw.upper() in seg:
                return kw.upper()
    # 2. En-tête (nom du labo avant l'adresse — format Biogaran classique).
    header = up[:800]
    for kw in labos_sorted:
        if re.search(r'\b' + re.escape(kw.upper()) + r'\b', header):
            return kw.upper()
    # 3. Repli : un labo cible nommé n'importe où (facture CSP = un seul labo).
    for kw in labos_sorted:
        if re.search(r'\b' + re.escape(kw.upper()) + r'\b', up):
            return kw.upper()
    return ""


def _extract_csp(text: str, provider: str, billing_date: str) -> list[dict]:
    """
    Facture de spécialités CSP / Movianto (dépositaire) au nom d'un labo générique.
    Ex : Biogaran, Teva, Arrow via Centre Spécialités Pharmaceutiques / Movianto.
    """
    lab_name = _extract_csp_lab(text) or _extract_lab_from_header(text) or provider
    # Date de rattachement : le nom de fichier prime (« …_19022026.pdf »), mais les
    # factures CSP nommées par n° de facture (« csp_W460206168.pdf », typiquement
    # Zydus) n'ont PAS de date dans le nom → billing_date vide → lignes ignorées
    # (pas de mois). Repli : 1re date JJ/MM/AAAA du contenu (facture/livraison/
    # commande sont à quelques jours → même mois, ce qui suffit à la clé mensuelle).
    if not billing_date:
        md = re.search(r'\b(\d{2})/(\d{2})/(20\d{2})\b', text)
        if md:
            billing_date = f"{md.group(3)}-{md.group(2)}-{md.group(1)}"
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

def _extract_doc_ref(text: str) -> str:
    """
    Extrait le numéro de facture/avoir depuis un PDF.

    Les documents Biogaran/Movianto étiquettent le numéro par « N°Facture : … »
    (presta) ou « N°Document : … » (RDP). Le numéro commence toujours par un
    chiffre (ex. 9006249886, 4M51003929) — on l'exige pour éviter de capturer le
    mot « Facture »/« Document » lui-même (bug historique) ou un N°Client/Contrat.
    Retourne le numéro en majuscules, ou ''.
    """
    head = text[:3000]
    # 1) Étiquettes explicites de numéro de document (ordre = priorité)
    for pat in (
        r'N[°o]\s*Facture\s*:?\s*([0-9][0-9A-Za-z]{4,15})',
        r'N[°o]\s*Document\s*:?\s*([0-9][0-9A-Za-z]{4,15})',
        r'N[°o]\s*Avoir\s*:?\s*([0-9][0-9A-Za-z]{4,15})',
        r'Facture\s*N[°o]?\s*:?\s*([0-9][0-9A-Za-z]{4,15})',
        r'Avoir\s*N[°o]?\s*:?\s*([0-9][0-9A-Za-z]{4,15})',
        r'Document\s*N[°o]?\s*:?\s*([0-9][0-9A-Za-z]{4,15})',
    ):
        m = re.search(pat, head, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    # 2) Fallback : 8-14 chiffres seuls sur une ligne
    for m2 in re.finditer(r'^\s*(\d{8,14})\s*$', head, re.MULTILINE):
        return m2.group(1)
    return ""


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
        "montant":      total,
        "total_ht":     total,
        "rdp_lines":    rdp_lines,
        "facture_num":  _extract_doc_ref(text),
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
    # Labo : d'abord chercher un labo générique dans le texte
    labo = ""
    for kw in sorted(LABOS_CIBLES, key=len, reverse=True):
        if kw.upper() in text[:1200].upper():
            labo = kw.upper(); break
    # Fallback : grossiste partenaire (ristourne CERP, Alloga presta)
    if not labo:
        prov_l = (provider or "").lower()
        for prov_kw, prov_labo in _PRESTA_PROVIDER_MAP.items():
            if prov_kw in prov_l:
                labo = prov_labo
                break
    if not labo:
        print(f"[PRESTA-DBG] labo introuvable — provider={provider!r} | début: {text[:300]!r}", flush=True)
        return []

    # Date (format DD/MM/YY ou DD/MM/YYYY)
    m_date = re.search(r'Date\s+(?:de\s+)?(?:Facture|facturation|facture)\s*:?\s*(\d{2})[/\-.](\d{2})[/\-.](\d{2,4})',
                       text, re.IGNORECASE)
    if not m_date:
        m_date = re.search(r'(\d{2})[/\-.](\d{2})[/\-.](\d{4})', text[:600])
    if m_date:
        yr = m_date.group(3)
        if len(yr) == 2:
            yr = "20" + yr
        billing_date = f"{yr}-{m_date.group(2)}-{m_date.group(1)}"

    # ── Extraction du montant TTC ────────────────────────────────────────────
    total_ht  = None
    total_ttc = None
    tva_pct   = None

    # Format Movianto/Biogaran : "Mode : VIREMENT  <HT>  <TVA%>  <TVA_MNT>  <TTC>"
    m_pay = re.search(
        r'Mode\s*:\s*VIREMENT\s+([\d\s,\.]+)\s+(\d+(?:[.,]\d+)?)\s+[\d\s,\.]+\s+([\d\s,\.]+)',
        text, re.IGNORECASE
    )
    if m_pay:
        total_ht  = _to_float(m_pay.group(1))
        tva_pct   = _to_float(m_pay.group(2))
        total_ttc = _to_float(m_pay.group(3))

    # Format Teva / Arrow : "NET À PAYER TTC  2 994,00" ou "MONTANT TTC  4 140,00"
    # Inclut les variantes avec "T.T.C." (points) utilisées par Teva Santé
    if total_ttc is None:
        for pat in [
            r'NET\s+[\u00c0A]\s+PAYER\s+(?:T\.?T\.?C\.?\s*)?([\d\s\xa0,\.]+)',
            r'NET\s+[\u00c0A]\s+R[\u00c9E]GLER\s+T\.?T\.?C\.?\s*:?\s*([\d\s\xa0,\.]+)',
            r'MONTANT\s+T\.?T\.?C\.?\s*:?\s*([\d\s\xa0,\.]+)',
            r'TOTAL\s+T\.?T\.?C\.?\s*:?\s*([\d\s\xa0,\.]+)',
            r'TOTAL\s+NET\s+T\.?T\.?C\.?\s*:?\s*([\d\s\xa0,\.]+)',
            r'\bT\.T\.C\.?\b\s*:?\s*(\d[\d\s\xa0,\.]+)',
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                total_ttc = _to_float(m.group(1))
                if total_ttc and total_ttc > 0:
                    break

    # Format : "HT  <montant>  TVA  <pct>%  TTC  <montant>"
    if total_ht is None or total_ttc is None:
        m_ht_ttc = re.search(
            r'(?:HT|H\.T\.)\s*([\d\s ,\.]+)\s+(?:TVA|T\.V\.A\.)\s+(\d+)\s*%\s*([\d\s ,\.]+)',
            text, re.IGNORECASE
        )
        if m_ht_ttc:
            total_ht  = total_ht  or _to_float(m_ht_ttc.group(1))
            tva_pct   = tva_pct   or _to_float(m_ht_ttc.group(2))
            total_ttc = total_ttc or _to_float(m_ht_ttc.group(3))

    # Fallback : dernière occurrence d'un grand nombre sur une ligne "TOTAL"
    if total_ht is None:
        m_tot = re.search(r'\bTOTAL\b[^\n]*?([\d\s ,\.]+)\s*$', text, re.MULTILINE)
        if m_tot:
            total_ht = _to_float(m_tot.group(1))

    if not total_ht and not total_ttc:
        ttc_lines = [l for l in text.split('\n') if any(kw in l.upper() for kw in ['TTC', 'T.T.C', 'PAYER', 'REGLER', 'TOTAL', 'MONTANT NET'])]
        print(f"[PRESTA-DBG] montant introuvable — labo={labo} provider={provider!r} | ttc_lines: {ttc_lines[:6]!r}", flush=True)
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
        "montant":      total_ht,
        "total_ht":     total_ht,
        "total_ttc":    total_ttc,
        "tva_pct":      tva_pct or 20.0,
        "facture_num":  _extract_doc_ref(text),
        "quantite":     0,
    }]


# ── 8. MDL CERP — Statistique Marge Dégressive Lissée ────────────────────────
#
# Format : "STATISTIQUE DE LA MARGE DEGRESSIVE LISSEE DES SPECIALITES MEDICALES
#            REMBOURSEES DU MOIS DE FEVRIER 2026"
# Section utile : "Génériques.Fde." → montants HT par labo au prix fabricant
#                 "CERP Rouen" → S.M.R. Génériques (total génériques grossiste)

_MDL_KNOWN_LABOS = {
    "arrow", "biogaran", "eg labo", "eg labs", "evolupharm",
    "mylan", "sandoz", "teva", "viatris", "zentiva", "zydus",
    "cristers", "arrow generiques",
}


def _extract_mdl_cerp(text: str, provider: str, billing_date: str) -> list[dict]:
    """Extrait les montants HT par labo (Génériques.Fde.) + totaux CERP depuis le MDL."""

    # ── Période ────────────────────────────────────────────────────────────────
    m_period = re.search(r'DU\s+MOIS\s+DE\s+([A-ZÉÈÊÀÙÛÔÎÂÄËÏÜ]+)\s+(\d{4})', text, re.IGNORECASE)
    if m_period:
        mon = _MONTHS_FR.get(m_period.group(1).upper()
                               .replace('É','E').replace('È','E').replace('Û','U'), "")
        period_month = f"{m_period.group(2)}-{mon}" if mon else billing_date[:7]
    else:
        period_month = billing_date[:7]

    # ── S.M.R. Génériques (CERP Rouen) ─────────────────────────────────────────
    smr_gen_mois   = 0.0
    smr_gen_cumul  = 0.0
    smr_total_mois = 0.0
    m_smr = re.search(
        r'S\.M\.R\.?\s+G[eé]n[eé]riques\s+(' + _RE_FR_NUM[1:-1] + r')',
        text, re.IGNORECASE,
    )
    if m_smr:
        smr_gen_mois = _fr_num(m_smr.group(1))
    m_smr_c = re.search(
        r'S\.M\.R\.?\s+G[eé]n[eé]riques.*?(' + _RE_FR_NUM[1:-1] + r')\s+\d+,\d+\s+(' + _RE_FR_NUM[1:-1] + r')',
        text, re.IGNORECASE,
    )
    if m_smr_c:
        smr_gen_cumul = _fr_num(m_smr_c.group(2))
    m_tot = re.search(r'Total\s+Spec\.?\s*Med\.?\s*Remb\.?\s+(' + _RE_FR_NUM[1:-1] + r')', text, re.IGNORECASE)
    if m_tot:
        smr_total_mois = _fr_num(m_tot.group(1))

    # ── Génériques.Fde. — extraction per-labo ──────────────────────────────────
    labo_rows = []

    # Isole la section "Génériques.Fde." jusqu'à la fin du texte ou saut de section
    m_sec = re.search(
        r'G[eé]n[eé]riques\.?\s*Fde\.?\s*\n(.*?)(?:\n{2,}|\Z)',
        text, re.DOTALL | re.IGNORECASE,
    )
    section_text = m_sec.group(1) if m_sec else ""

    # Fallback : si la section n'est pas clairement délimitée, chercher les labos connus
    # directement dans tout le texte après "Génériques.Fde."
    if not section_text:
        m_pos = re.search(r'G[eé]n[eé]riques\.?\s*Fde\.?', text, re.IGNORECASE)
        if m_pos:
            section_text = text[m_pos.end():]

    if section_text:
        for line in section_text.split('\n'):
            line = line.strip()
            if not line:
                continue
            # Extraire tous les nombres de la ligne
            nums_raw = re.findall(r'[\d][\d\s]*,\d{2}', line)
            if not nums_raw:
                continue
            # Nom du labo = tout ce qui précède le premier nombre
            name_part = re.sub(r'[\d\s,]+$', '', line.split(nums_raw[0])[0]).strip()
            if not name_part:
                continue
            # Vérifier que le nom contient un labo connu (matching souple)
            name_low = name_part.lower()
            matched = next((k for k in _MDL_KNOWN_LABOS if k in name_low), None)
            if not matched:
                # Accepter aussi les noms courts inconnus (>= 3 chars) dans la section
                if len(name_part) >= 3 and not any(c.isdigit() for c in name_part):
                    matched = name_part
            if not matched:
                continue
            ca_mois   = _fr_num(nums_raw[0])
            ca_cumul  = _fr_num(nums_raw[-1]) if len(nums_raw) >= 2 else ca_mois
            if ca_mois > 0:
                labo_rows.append({
                    "type":          "mdl",
                    "labo":          name_part,
                    "fournisseur":   provider or "CERP",
                    "billing_date":  billing_date,
                    "period_month":  period_month,
                    "ca_fab_mois":   ca_mois,
                    "ca_fab_cumul":  ca_cumul,
                })

    # Ligne de synthèse CERP
    labo_rows.append({
        "type":            "mdl",
        "labo":            "_cerp",
        "fournisseur":     provider or "CERP",
        "billing_date":    billing_date,
        "period_month":    period_month,
        "smr_gen_mois":    smr_gen_mois,
        "smr_gen_cumul":   smr_gen_cumul,
        "smr_total_mois":  smr_total_mois,
        "ca_fab_mois":     0.0,
        "ca_fab_cumul":    0.0,
    })

    return labo_rows


# ── 9. ESCOMPTE CERP — Relevé mensuel agrégé (pas per-CIP) ────────────────────
#
# Format CERP Rouen : "RELEVE DES ESCOMPTES DU MOIS DE SEPTEMBRE 2025"
# Tableau 1 : VENTILATION DES ACHATS PAR CATEGORIE (CA HT + %)
# Tableau 2 : REMISES COMMERCIALES H.T. (remise par catégorie)
# Tableau 3 : VENTILATION PAR TAUX DE TVA (totaux remises + escomptes financiers)

_MONTHS_FR = {
    "JANVIER": "01", "FEVRIER": "02", "FÉVRIER": "02", "MARS": "03",
    "AVRIL": "04", "MAI": "05", "JUIN": "06", "JUILLET": "07",
    "AOUT": "08", "AOÛT": "08", "SEPTEMBRE": "09", "OCTOBRE": "10",
    "NOVEMBRE": "11", "DECEMBRE": "12", "DÉCEMBRE": "12",
}

_RE_FR_NUM = r'([\d]+(?:\.[\d]{3})*,\d{2})'   # 24.430,28 ou 2.850,33 ou 158,29


def _extract_escompte_cerp(text: str, provider: str, billing_date: str) -> list[dict]:
    """Retourne une seule ligne de synthèse de type 'escompte' depuis un Relevé CERP."""

    # ── Période : "DU MOIS DE SEPTEMBRE 2025" ──────────────────────────────────
    m_period = re.search(r'DU\s+MOIS\s+DE\s+([A-ZÉÈÊÀÙÛÔÎÂÄËÏÜ]+)\s+(\d{4})', text, re.IGNORECASE)
    if m_period:
        mon = _MONTHS_FR.get(m_period.group(1).upper().replace('É', 'E').replace('Û', 'U'), "")
        period_month = f"{m_period.group(2)}-{mon}" if mon else billing_date[:7]
    else:
        period_month = billing_date[:7]

    # ── Section 1 : achats génériques (prix fabriquant HT) ─────────────────────
    # Ligne : "SPEC GENERIQUES (pf.ht)   24.430,28   18,50"
    ca_spec_gen_ht = 0.0
    m_ca = re.search(
        r'SPEC\s+G[EÉ]N[EÉ]RIQUES\s*\(pf\.?\s*ht\.?\)\s*' + _RE_FR_NUM,
        text, re.IGNORECASE,
    )
    if m_ca:
        ca_spec_gen_ht = _fr_num(m_ca.group(1))

    # ── Section 2 : remises commerciales HT ────────────────────────────────────
    # On isole la section entre "REMISES COMMERCIALES H.T." et "VENTILATION PAR TAUX"
    remise_spec_gen_ht = 0.0
    m_sec = re.search(
        r'REMISES\s+COMMERCIALES\s+H\.?T\.(.+?)(?:VENTILATION\s+PAR\s+TAUX|Aux\s+montants)',
        text, re.IGNORECASE | re.DOTALL,
    )
    if m_sec:
        sec = m_sec.group(1)
        # "SPEC GENERIQUES   2.850,33" (sans "(pf.ht)" dans cette section)
        m_r2 = re.search(r'SPEC\s+G[EÉ]N[EÉ]RIQUES\s+' + _RE_FR_NUM, sec, re.IGNORECASE)
        if m_r2:
            remise_spec_gen_ht = _fr_num(m_r2.group(1))

    # ── Section 3 : ventilation TVA → totaux remises et escomptes ──────────────
    remise_total_ht  = 0.0
    remise_total_ttc = 0.0
    escompte_ttc     = 0.0
    total_ttc        = 0.0

    m_tva = re.search(r'VENTILATION\s+PAR\s+TAUX\s+DE\s+TVA(.+?)$', text, re.IGNORECASE | re.DOTALL)
    if m_tva:
        tva_sec = m_tva.group(1)

        # Les deux blocs TOTAL (un pour REMISES, un pour ESCOMPTES) dans la section TVA
        totals = re.findall(r'\bTOTAL\b\s+' + _RE_FR_NUM + r'\s+' + _RE_FR_NUM + r'\s+' + _RE_FR_NUM, tva_sec, re.IGNORECASE)
        if len(totals) >= 1:
            remise_total_ht  = _fr_num(totals[0][0])
            # totals[0][1] = TVA montant, totals[0][2] = TTC
            remise_total_ttc = _fr_num(totals[0][2])
        if len(totals) >= 2:
            escompte_ttc = _fr_num(totals[1][2])

        # TOTAL TTC final
        m_ttc = re.search(r'TOTAL\s+TTC\s+' + _RE_FR_NUM, tva_sec, re.IGNORECASE)
        if m_ttc:
            total_ttc = _fr_num(m_ttc.group(1))

    return [{
        "type":               "escompte",
        "fournisseur":        provider or "CERP",
        "billing_date":       billing_date,
        "period_month":       period_month,
        "ca_spec_gen_ht":     ca_spec_gen_ht,
        "remise_spec_gen_ht": remise_spec_gen_ht,
        "remise_total_ht":    remise_total_ht,
        "remise_total_ttc":   remise_total_ttc,
        "escompte_ttc":       escompte_ttc,
        "total_ttc":          total_ttc,
    }]


# ── Point d'entrée public ──────────────────────────────────────────────────────

def _ocr_pdf(pdf_path: Path) -> str:
    """OCR de secours pour les PDF scannés (sans couche texte) : rend chaque page
    en image via PyMuPDF puis lit le texte avec Tesseract (français). Traite les
    pages une par une pour limiter la mémoire."""
    try:
        import io as _io
        import fitz               # PyMuPDF (déjà en requirements)
        import pytesseract
        from PIL import Image
    except Exception as e:
        print(f"[OCR] dépendances manquantes : {e}", flush=True)
        return ""
    out = []
    try:
        doc = fitz.open(str(pdf_path))
        for page in doc:
            pix = page.get_pixmap(dpi=200)          # 200 DPI : bon compromis OCR/mémoire
            img = Image.open(_io.BytesIO(pix.tobytes("png")))
            out.append(pytesseract.image_to_string(img, lang="fra"))
            img.close()
            pix = None
        doc.close()
    except Exception as e:
        print(f"[OCR] échec : {e}", flush=True)
    txt = "\n".join(out)
    print(f"[OCR] {len(txt)} caractères extraits", flush=True)
    return txt


def extract_invoice_lines(pdf_path: Path, provider: str, billing_date: str) -> list[dict]:
    """
    Extrait les lignes d'une facture PDF. Pour les formats produits (alloga, csp,
    viatris, cooperation), retourne les lignes CIP. Pour les formats avoirs (rdp,
    presta), retourne une ligne de synthèse avec type="rdp" ou type="presta".
    """
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            # PDF scanné (aucune couche texte) → OCR de secours.
            if not full_text.strip():
                full_text = _ocr_pdf(pdf_path)
            fmt = _detect_format(full_text)

            # lcr_releve : bordereau de paiement (pas une facture). cpf_product :
            # facture CPF payable sur relevé — déjà comptée via le justificatif
            # répartiteur (la parser doublerait les achats). teva_product, lui,
            # EST un achat direct labo → parsé (longtemps jeté ici à tort : les
            # achats directs Teva restaient invisibles du vérificateur).
            if fmt in ("lcr_releve", "cpf_product"):
                return []
            elif fmt == "teva_product":
                lines = _extract_teva_product(full_text, provider, billing_date)
            elif fmt == "mdl_cerp":
                return _extract_mdl_cerp(full_text, provider, billing_date)
            elif fmt == "escompte_cerp":
                return _extract_escompte_cerp(full_text, provider, billing_date)
            elif fmt == "alloga":
                lines = _extract_alloga(full_text, provider, billing_date)
                # presta Alloga déjà détecté avant ce point par _detect_format
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
                # Format inconnu : tenter presta (couvre Teva Sante et autres non reconnus)
                lines = _extract_presta(full_text, provider, billing_date)
                if not lines:
                    lines = _extract_text_fallback(full_text, provider, billing_date)
    except Exception:
        return []

    # Filtrer sur les labos génériqueurs cibles (s'applique à rdp/presta aussi)
    def _is_keepable(line: dict) -> bool:
        labo = (line.get("labo") or "").lower()
        if _is_labo_cible(labo): return True
        if line.get("type") in ("presta", "rdp") and any(p in labo for p in _PRESTA_PARTNER_LABOS): return True
        return False
    lines = [l for l in lines if _is_keepable(l)]

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
