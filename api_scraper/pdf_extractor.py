"""
Extraction des données produits depuis les PDFs de factures DIGIPHARMACIE.

Formats supportés :
  1. ALLOGA FRANCE — factures au nom d'un labo (Biogaran, Reckitt, etc.)
     Colonnes : Code Article | Désignation | Qté | PU HT Brut | Taux Remise | PU HT Net | Total HT | TVA%
  2. Fallback texte brut — regex sur CIP13 + nombres voisins
"""

import re
from pathlib import Path

import pdfplumber

# ── Helpers ───────────────────────────────────────────────────────────────────

RE_CIP13 = re.compile(r'\b(3[0-9]\d{11})\b')

# Ligne produit Alloga :
#   3400938254518 DESIGNATION QTE [QTE_GRATUIT] PU_BRUT REMISE% PU_NET TOTAL_HT TVA%
# Exemple : "3400938254518 GAVISCONELL SUSP SSUCRE X12 12 7,700 35,00% 5,005 60,06 10,00%"
_NUM   = r'[\d]+(?:[,\s]\d+)*'    # nombre français (virgule décimale, espaces milliers)
_PCT   = r'(\d{1,3},\d{2})%'      # pourcentage type "35,00%"
RE_ALLOGA_LINE = re.compile(
    r'^(\d{13})\s+'              # CIP13
    r'(.+?)\s+'                  # désignation (non-greedy)
    r'(\d+)\s+'                  # quantité facturée
    r'([\d,\s]+?)\s+'            # PU HT Brut
    + _PCT + r'\s+'              # Taux Remise
    + r'([\d,\s]+?)\s+'         # PU HT Net
    + r'([\d,\s]+?)\s+'         # Total HT
    + _PCT + r'\s*$',            # Taux TVA
    re.MULTILINE,
)


def _to_float(s: str) -> float | None:
    if not s:
        return None
    s = str(s).strip().replace('\xa0', '').replace(' ', '').replace(' ', '').replace(',', '.')
    m = re.search(r'[\d.]+', s)
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def _norm(s: str) -> str:
    return (s or "").lower().strip()


# ── Détection du format ────────────────────────────────────────────────────────

def _detect_format(text: str) -> str:
    """Retourne 'alloga' ou 'unknown' selon les marqueurs dans le texte."""
    t = text[:500].lower()
    if "alloga france" in t or "alloga" in t[:200]:
        return "alloga"
    return "unknown"


def _extract_lab_name(text: str) -> str:
    """Extrait le nom du labo depuis 'AU NOM ET POUR LE COMPTE DE\\n{lab_name}'."""
    m = re.search(
        r"AU NOM ET POUR LE COMPTE DE\s*\n\s*(.+)",
        text, re.IGNORECASE
    )
    if m:
        lab = m.group(1).strip()
        # Nettoyer : parfois suivi de l'adresse sur la même ligne
        lab = re.split(r'\s{2,}|\n', lab)[0].strip()
        return lab
    return ""


# ── Extracteur ALLOGA ──────────────────────────────────────────────────────────

def _extract_alloga(text: str, provider: str, billing_date: str) -> list[dict]:
    """
    Extrait les lignes produits d'une facture Alloga.

    Format brut (raw text) car pdfplumber fusionne souvent toutes les lignes
    produits dans une seule cellule.

    Colonnes : CIP13 | Désignation | Qté | PU Brut | Remise% | PU Net | Total HT | TVA%
    """
    lab_name = _extract_lab_name(text)

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = RE_ALLOGA_LINE.match(line)
        if not m:
            continue

        cip13    = m.group(1)
        libelle  = m.group(2).strip()
        qte_str  = m.group(3)
        pubrut_s = m.group(4)
        remise_s = m.group(5)   # sans %
        punet_s  = m.group(6)
        total_s  = m.group(7)
        # tva_s  = m.group(8)   # non utilisé pour l'instant

        pu_brut  = _to_float(pubrut_s)
        remise   = _to_float(remise_s)
        pu_net   = _to_float(punet_s)
        total_ht = _to_float(total_s)
        qte      = int(qte_str) if qte_str.isdigit() else None

        if pu_brut is None and pu_net is None:
            continue

        lines.append({
            "cip":          cip13,
            "libelle":      libelle,
            "fournisseur":  provider,
            "labo":         lab_name,
            "billing_date": billing_date,
            "quantite":     qte,
            "prix_brut":    round(pu_brut, 4) if pu_brut else None,
            "remise_pct":   round(remise, 2)  if remise is not None else None,
            "pa_net":       round(pu_net, 4)  if pu_net else None,
            "total_ht":     round(total_ht, 2) if total_ht else None,
        })

    return lines


# ── Fallback texte brut ────────────────────────────────────────────────────────

def _extract_text_fallback(text: str, provider: str, billing_date: str) -> list[dict]:
    """Fallback générique : cherche CIP13 + valeurs numériques sur la même ligne."""
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
        pa_net    = (
            round(prix_brut * (1 - remise / 100), 4)
            if remise is not None else None
        )
        libelle = re.split(r'\d', line)[0].strip()[:80]

        lines.append({
            "cip":          cip,
            "libelle":      libelle,
            "fournisseur":  provider,
            "labo":         "",
            "billing_date": billing_date,
            "quantite":     None,
            "prix_brut":    round(prix_brut, 4),
            "remise_pct":   remise,
            "pa_net":       pa_net,
            "total_ht":     None,
        })

    return lines


# ── Point d'entrée public ──────────────────────────────────────────────────────

def extract_invoice_lines(pdf_path: Path, provider: str, billing_date: str) -> list[dict]:
    """
    Extrait toutes les lignes produits d'une facture PDF.
    Retourne une liste de dicts avec cip, libelle, quantite, prix_brut,
    remise_pct, pa_net, total_ht, labo, fournisseur, billing_date.
    """
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            full_text = "\n".join(
                page.extract_text() or "" for page in pdf.pages
            )
    except Exception:
        return []

    if not full_text.strip():
        return []

    fmt = _detect_format(full_text)

    if fmt == "alloga":
        lines = _extract_alloga(full_text, provider, billing_date)
    else:
        lines = _extract_text_fallback(full_text, provider, billing_date)

    # Déduplication légère : même CIP + même date
    seen, dedup = set(), []
    for line in lines:
        key = (line["cip"], line["billing_date"], line.get("libelle", "")[:20])
        if key not in seen:
            seen.add(key)
            dedup.append(line)

    return dedup
