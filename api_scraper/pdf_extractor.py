"""
Extraction des données produits depuis les PDFs de factures DIGIPHARMACIE.

Stratégie :
  1. pdfplumber — lecture de toutes les tables du PDF
  2. Pour chaque ligne : chercher CIP13, libellé, prix brut, remise%, PA NET
  3. Fallback texte brut si aucune table détectée (regex)
"""

import re
from pathlib import Path

import pdfplumber

# ── Patterns ───────────────────────────────────────────────────────────────────

RE_CIP13  = re.compile(r'\b(3[46]\d{11})\b')          # CIP13 : 34xxxxx ou 36xxxxx
RE_CIP7   = re.compile(r'\b(\d{7})\b')
RE_PRICE  = re.compile(r'\b(\d{1,4}[,\.]\d{2,4})\b')  # prix : 12,34 ou 12.3456
RE_PCT    = re.compile(r'\b(\d{1,2}[,\.]\d{0,2})\s*%') # pourcentage : 17,5 %

HEADER_SYNONYMS = {
    "cip":      ["cip", "cip13", "cip7", "code cip", "code article", "référence", "ref"],
    "libelle":  ["désignation", "libellé", "produit", "description", "article"],
    "prix":     ["pu ht", "prix ht", "prix brut", "p.u.", "pu", "tarif", "p.u.h.t"],
    "remise":   ["remise", "remise %", "taux", "%" ],
    "pa_net":   ["net ht", "pa net", "montant net", "net", "total ht", "montant ht"],
    "qte":      ["qté", "quantite", "quantité", "qte", "nb"],
}


def _norm(s: str) -> str:
    return (s or "").lower().strip().replace("\n", " ")


def _match_header(cell: str, field: str) -> bool:
    n = _norm(cell)
    return any(syn in n for syn in HEADER_SYNONYMS[field])


def _to_float(s: str) -> float | None:
    if not s:
        return None
    s = str(s).strip().replace(" ", "").replace(",", ".")
    m = re.search(r"[\d.]+", s)
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


# ── Table extraction ───────────────────────────────────────────────────────────

def _extract_from_tables(pdf, provider: str, billing_date: str) -> list[dict]:
    lines = []
    for page in pdf.pages:
        for table in page.extract_tables():
            if not table or len(table) < 2:
                continue

            # Identify header row (first non-empty row)
            header_row = None
            data_start = 0
            for i, row in enumerate(table):
                if any(c and _norm(c) for c in row):
                    header_row = row
                    data_start = i + 1
                    break

            if header_row is None:
                continue

            # Map column index to field
            col = {}
            for j, cell in enumerate(header_row):
                for field in HEADER_SYNONYMS:
                    if _match_header(str(cell or ""), field) and field not in col:
                        col[field] = j
                        break

            # Need at least a price or CIP column
            if not col:
                continue

            for row in table[data_start:]:
                if not row or all(not c for c in row):
                    continue

                def get(field):
                    idx = col.get(field)
                    return str(row[idx] or "").strip() if idx is not None and idx < len(row) else ""

                # CIP — try dedicated column first, then regex scan
                cip = ""
                raw_cip = get("cip")
                m = RE_CIP13.search(raw_cip)
                if m:
                    cip = m.group(1)
                else:
                    for cell in row:
                        m = RE_CIP13.search(str(cell or ""))
                        if m:
                            cip = m.group(1)
                            break

                libelle  = get("libelle") or get("cip")  # sometimes same column
                prix_raw = get("prix")
                rem_raw  = get("remise")
                net_raw  = get("pa_net")
                qte_raw  = get("qte")

                prix_brut = _to_float(prix_raw)
                remise    = _to_float(re.sub(r'[%\s]', '', rem_raw)) if rem_raw else None
                pa_net    = _to_float(net_raw)
                qte       = _to_float(qte_raw)

                # Derive missing values
                if prix_brut and remise is not None and pa_net is None:
                    pa_net = round(prix_brut * (1 - remise / 100), 4)
                elif prix_brut and pa_net and remise is None and prix_brut > 0:
                    remise = round((1 - pa_net / prix_brut) * 100, 2)

                if not prix_brut and not pa_net:
                    continue  # skip empty/header rows

                lines.append({
                    "cip":          cip,
                    "libelle":      libelle,
                    "fournisseur":  provider,
                    "billing_date": billing_date,
                    "prix_brut":    round(prix_brut, 4) if prix_brut else None,
                    "remise_pct":   round(remise, 2)    if remise is not None else None,
                    "pa_net":       round(pa_net, 4)    if pa_net else None,
                    "quantite":     int(qte)            if qte else None,
                })

    return lines


# ── Regex fallback on raw text ─────────────────────────────────────────────────

def _extract_from_text(pdf, provider: str, billing_date: str) -> list[dict]:
    """
    Fallback : parcourt le texte ligne par ligne, cherche des CIP13
    et les valeurs numériques voisines.
    """
    lines = []
    full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    for line in full_text.splitlines():
        m_cip = RE_CIP13.search(line)
        if not m_cip:
            continue
        cip = m_cip.group(1)

        prices = [float(p.replace(",", ".")) for p in RE_PRICE.findall(line)]
        pcts   = [float(p.replace(",", ".")) for p in RE_PCT.findall(line)]

        if not prices:
            continue

        prix_brut = max(prices)                        # heuristic: largest value = brut
        remise    = pcts[0] if pcts else None
        pa_net    = (
            round(prix_brut * (1 - remise / 100), 4)
            if remise is not None else None
        )

        # Try to extract libellé (text before the first number)
        libelle = re.split(r'\d', line)[0].strip()[:80]

        lines.append({
            "cip":          cip,
            "libelle":      libelle,
            "fournisseur":  provider,
            "billing_date": billing_date,
            "prix_brut":    round(prix_brut, 4),
            "remise_pct":   remise,
            "pa_net":       pa_net,
            "quantite":     None,
        })

    return lines


# ── Public entry point ─────────────────────────────────────────────────────────

def extract_invoice_lines(pdf_path: Path, provider: str, billing_date: str) -> list[dict]:
    """
    Extrait toutes les lignes produits d'une facture PDF.
    """
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            lines = _extract_from_tables(pdf, provider, billing_date)
            if not lines:
                lines = _extract_from_text(pdf, provider, billing_date)
    except Exception:
        return []

    # Déduplication légère : même CIP + même date = garder la première occurrence
    seen  = set()
    dedup = []
    for line in lines:
        key = (line["cip"], line["billing_date"], line.get("libelle", "")[:20])
        if key not in seen:
            seen.add(key)
            dedup.append(line)

    return dedup
