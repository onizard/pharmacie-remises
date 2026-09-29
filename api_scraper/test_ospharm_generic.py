"""Tests du traitement des ventes génériques OSPHARM (ospharm_generic.py).

Les lignes d'entrée sont celles RÉELLEMENT relevées sur le compte le 23/09/2026
dans dt_sellout_resume_productname, valeurs comprises — y compris les flottants
bruités d'OSPHARM (« 2.9900000000000002 »).

Lancer : python3 api_scraper/test_ospharm_generic.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ospharm_generic import (norm_labo, extract_cip, normalize_rows,
                             build_month_stats, compact_rows, resolve_cips,
                             parse_payload, merge_job)

# Trois lignes authentiques du relevé.
REELLES = [
    {"product_name": "ACEBUTOLOL 200 mg", "package": "30.0",
     "manufacturer_name": "BIOGARAN", "sellin_price_ht": "2.9900000000000002",
     "quantity": "3", "product_discount": "40.0",
     "product_discount_price_ht": 1.79, "sum_sellin_ht": "5.3819999999999997",
     "groupe_gener": "ACEBUTOLOL 200 mg"},
    {"product_name": "ACEBUTOLOL 200 mg", "package": "90.0",
     "manufacturer_name": "BIOGARAN", "sellin_price_ht": "8.5099999999999998",
     "quantity": "1", "product_discount": "30.0",
     "product_discount_price_ht": 5.96, "sum_sellin_ht": "5.9569999999999999",
     "groupe_gener": "ACEBUTOLOL 200 mg"},
    {"product_name": "ACIDE FOLIQUE 5 mg", "package": "20.0",
     "manufacturer_name": "ARROW GENERIQUES", "sellin_price_ht": "0.90000000000000002",
     "quantity": "2", "product_discount": "20.0",
     "product_discount_price_ht": 0.72, "sum_sellin_ht": "1.4399999999999999",
     "groupe_gener": "ACIDE FOLIQUE 5 mg"},
]

_ok = _ko = 0


def check(label, got, want, tol=None):
    global _ok, _ko
    good = (abs(got - want) <= tol) if tol is not None else (got == want)
    print(("  ✔ " if good else "  ✘ ") + label + f" → {got!r}" +
          ("" if good else f"   ATTENDU {want!r}"))
    if good:
        _ok += 1
    else:
        _ko += 1


print("── noms de laboratoires ──")
check("ARROW GENERIQUES", norm_labo("ARROW GENERIQUES"), "ARROW")
check("TEVA SANTE",       norm_labo("TEVA SANTE"),       "TEVA")
check("VIATRIS SANTE",    norm_labo("VIATRIS SANTE"),    "VIATRIS")
check("MYLAN → VIATRIS",  norm_labo("MYLAN"),            "VIATRIS")
check("BIOGARAN",         norm_labo("BIOGARAN"),         "BIOGARAN")

print("\n── extraction du code produit ──")
check("absent des lignes réelles", extract_cip(REELLES[0]), "")
check("champ cip13",  extract_cip({"cip13": "3400930270592"}), "3400930270592")
check("champ code_ean", extract_cip({"code_ean": "3400 936 290 815"}), "3400936290815")
check("cip7 complété", extract_cip({"cip": "1234567"})[:4], "3400")

print("\n── normalisation ──")
rows = normalize_rows(REELLES, 2025, 1)
check("3 lignes", len(rows), 3)
check("labo normalisé", rows[2]["labo"], "ARROW")
check("PFHT unitaire", rows[0]["puht"], 2.99, tol=0.001)
check("quantité", rows[0]["qty"], 3)
check("net remisé", rows[0]["net"], 5.38, tol=0.01)
check("clé présentation", rows[0]["key"], "ACEBUTOLOL 200 mg|30.0|BIOGARAN")
check("quantité nulle écartée", len(normalize_rows(
    [{"product_name": "X", "quantity": "0", "manufacturer_name": "BIOGARAN"}], 2025, 1)), 0)

print("\n── net reconstruit quand il manque ──")
sansnet = normalize_rows([{"product_name": "TEST", "package": "10",
                           "manufacturer_name": "BIOGARAN",
                           "sellin_price_ht": "10", "quantity": "5",
                           "product_discount": "25"}], 2025, 3)
check("10 € × 5 − 25 %", sansnet[0]["net"], 37.5, tol=0.01)

print("\n── agrégats mensuels ──")
st = build_month_stats(rows)
check("un mois", list(st.keys()), ["2025-01"])
bio = [x for x in st["2025-01"] if x["labo"] == "BIOGARAN"][0]
arr = [x for x in st["2025-01"] if x["labo"] == "ARROW"][0]
# BIOGARAN : 2,99×3 + 8,51×1 = 17,48 brut ; net 5,382 + 5,957 = 11,339
check("BIOGARAN ca_brut", bio["ca_brut"], 17.48, tol=0.01)
check("BIOGARAN pa_net",  bio["pa_net"],  11.34, tol=0.01)
check("BIOGARAN remise",  bio["remise_totale"], 6.14, tol=0.01)
check("BIOGARAN qté",     bio["qty"], 4)
check("BIOGARAN pond %",  bio["pond_pct"], 35.13, tol=0.05)
check("ARROW ca_brut",    arr["ca_brut"], 1.80, tol=0.01)
check("remise 2 à zéro",  bio["r2_ca"], 0.0)
check("tri par ca_brut décroissant", st["2025-01"][0]["labo"], "BIOGARAN")
check("brut = net + remise",
      round(bio["pa_net"] + bio["remise_totale"], 2), bio["ca_brut"], tol=0.01)

print("\n── lignes compactes ──")
cr = compact_rows(rows)
check("3 lignes", len(cr), 3)
check("champs", sorted(cr[0].keys()), ["cip13", "key", "month", "puht", "qty", "year"])

print("\n── rapprochement au catalogue ──")
catalogue = [
    {"cip13": "3400930000011", "labo": "Biogaran", "libelle": "ACEBUTOLOL 200MG 30 CP"},
    {"cip13": "3400930000028", "labo": "Biogaran", "libelle": "ACEBUTOLOL 200MG 90 CP"},
    {"cip13": "3400930000035", "labo": "Arrow",    "libelle": "ACIDE FOLIQUE 5MG 20 CP"},
]
r2 = normalize_rows(REELLES, 2025, 1)
rep = resolve_cips(r2, catalogue)
# Le conditionnement distingue les deux boîtes d'ACEBUTOLOL (30 et 90).
check("3 lignes résolues", rep["resolus"], 3)
check("aucune ambiguïté", rep["ambigus"], 0)
check("ACEBUTOLOL 30 → bon CIP", r2[0]["cip13"], "3400930000011")
check("ACEBUTOLOL 90 → bon CIP", r2[1]["cip13"], "3400930000028")
check("ACIDE FOLIQUE résolu",    r2[2]["cip13"], "3400930000035")

# Ambiguïté réelle : deux références du même labo, même libellé, même
# conditionnement. On refuse de trancher au hasard — une mauvaise affectation
# fausserait les remises, donc de l'argent.
amb = normalize_rows([{"product_name": "IBUPROFENE 400 mg", "package": "30.0",
                       "manufacturer_name": "BIOGARAN", "sellin_price_ht": "1",
                       "quantity": "1", "sum_sellin_ht": "1"}], 2025, 1)
rep2 = resolve_cips(amb, [
    {"cip13": "3400930000101", "labo": "Biogaran", "libelle": "IBUPROFENE 400MG 30 CP"},
    {"cip13": "3400930000118", "labo": "Biogaran", "libelle": "IBUPROFENE 400MG 30 CP SEC"},
])
check("ambiguïté détectée", rep2["ambigus"], 1)
check("laissée non résolue", amb[0]["cip13"], "")

# Mauvais laboratoire : aucun rapprochement, même si le libellé colle.
mal = normalize_rows([{"product_name": "ACIDE FOLIQUE 5 mg", "package": "20.0",
                       "manufacturer_name": "ZYDUS", "sellin_price_ht": "1",
                       "quantity": "1", "sum_sellin_ht": "1"}], 2025, 1)
resolve_cips(mal, catalogue)
check("pas de rapprochement inter-labos", mal[0]["cip13"], "")

print("\n── doublons de libellé départagés par le PFHT ──")
# Cas RÉEL du catalogue : deux références Biogaran au libellé strictement
# identique, mais PUHT 5 € / RSF −2,5 % contre PUHT 3 € / RSF −20 %. Elles ne
# sont pas interchangeables ; OSPHARM annonce un PFHT de 2,99 €.
JUMEAUX = [
    {"cip13": "3400930067390", "labo": "Biogaran", "libelle": "ACEBUTOLOL 200MG 30 CPR", "puht": 5},
    {"cip13": "3400935307446", "labo": "Biogaran", "libelle": "ACEBUTOLOL 200MG 30 CPR", "puht": 3},
]
jr = normalize_rows([{"product_name": "ACEBUTOLOL 200 mg", "package": "30.0",
                      "manufacturer_name": "BIOGARAN", "sellin_price_ht": "2.99",
                      "quantity": "3", "sum_sellin_ht": "5.38"}], 2025, 1)
jrep = resolve_cips(jr, JUMEAUX)
check("départagé par le prix", jr[0]["cip13"], "3400935307446")
check("compté comme tel", jrep.get("par_prix"), 1)

# Prix trop proches l'un de l'autre : le signal ne tranche plus, on refuse.
PROCHES = [
    {"cip13": "3400930000201", "labo": "Biogaran", "libelle": "TEST 10MG 30 CPR", "puht": 3.00},
    {"cip13": "3400930000218", "labo": "Biogaran", "libelle": "TEST 10MG 30 CPR", "puht": 3.02},
]
pr = normalize_rows([{"product_name": "TEST 10 mg", "package": "30.0",
                      "manufacturer_name": "BIOGARAN", "sellin_price_ht": "3.01",
                      "quantity": "1", "sum_sellin_ht": "3.01"}], 2025, 1)
prep = resolve_cips(pr, PROCHES)
check("signal trop faible → refus", pr[0]["cip13"], "")
check("compté en ambigu", prep["ambigus"], 1)

# PFHT très éloigné des deux candidats : aucun n'est crédible.
LOIN = [
    {"cip13": "3400930000301", "labo": "Biogaran", "libelle": "AUTRE 5MG 30 CPR", "puht": 50},
    {"cip13": "3400930000318", "labo": "Biogaran", "libelle": "AUTRE 5MG 30 CPR", "puht": 80},
]
lr = normalize_rows([{"product_name": "AUTRE 5 mg", "package": "30.0",
                      "manufacturer_name": "BIOGARAN", "sellin_price_ht": "2.00",
                      "quantity": "1", "sum_sellin_ht": "2.00"}], 2025, 1)
resolve_cips(lr, LOIN)
check("aucun candidat crédible → refus", lr[0]["cip13"], "")

print("\n── validation du corps reçu ──")
rws, meta = parse_payload({"months": [
    {"year": 2025, "month": 1, "period_start": "2025-01-01",
     "period_end": "2025-01-31", "rows": REELLES},
    {"year": 2025, "month": 2, "period_start": "2025-02-01",
     "period_end": "2025-02-28", "rows": REELLES},
]})
check("6 lignes sur 2 mois", len(rws), 6)
check("2 mois de métadonnées", len(meta), 2)
check("mois triés", [(m["year"], m["month"]) for m in meta], [(2025, 1), (2025, 2)])
for bad, why in [({}, "corps vide"),
                 ({"months": []}, "months vide"),
                 ({"months": [{"year": 2025, "month": 1, "rows": []}]}, "aucune ligne")]:
    try:
        parse_payload(bad)
        check(f"refus attendu ({why})", "accepté", "refusé")
    except ValueError:
        check(f"refus attendu ({why})", "refusé", "refusé")

print("\n── fusion dans ospharm_job, à la maille du mois ──")
# Un import partiel ne doit JAMAIS effacer les mois qu'il ne couvre pas : c'est
# précisément ce qui faisait repartir l'ancien scraper de zéro à chaque tour.
ancien = {
    "rows": [{"cip13": "1", "key": "a", "qty": 1, "puht": 1, "year": 2025, "month": 1},
             {"cip13": "2", "key": "b", "qty": 2, "puht": 2, "year": 2025, "month": 5}],
    "month_meta": [{"year": 2025, "month": 1, "period_start": "2025-01-01",
                    "period_end": "2025-01-31", "rows": 1},
                   {"year": 2025, "month": 5, "period_start": "2025-05-01",
                    "period_end": "2025-05-31", "rows": 1}],
    "month_stats": {"2025-01": [{"labo": "ANCIEN"}], "2025-05": [{"labo": "GARDE"}]},
    "status": "error", "error": "vieille panne",
}
nr = normalize_rows(REELLES, 2025, 1)          # on re-capture JANVIER seulement
nm = [{"year": 2025, "month": 1, "period_start": "2025-01-01",
       "period_end": "2025-01-31", "rows": len(nr)}]
fus = merge_job(ancien, nr, nm, build_month_stats(nr))
check("janvier remplacé", len([r for r in fus["rows"] if r["month"] == 1]), 3)
check("mai conservé",     len([r for r in fus["rows"] if r["month"] == 5]), 1)
check("stats mai intactes", fus["month_stats"]["2025-05"], [{"labo": "GARDE"}])
check("stats janvier refaites", fus["month_stats"]["2025-01"][0]["labo"], "BIOGARAN")
check("2 mois de métadonnées", len(fus["month_meta"]), 2)
check("statut remis à done", fus["status"], "done")
check("erreur effacée", fus["error"], "")
check("total recalculé", fus["total"], 4)
check("période globale", (fus["period_start"], fus["period_end"]),
      ("2025-01-01", "2025-05-31"))

vide = merge_job({}, nr, nm, build_month_stats(nr))
check("fusion sur base vide", vide["total"], 3)

print(f"\n{'='*52}\n  {_ok} réussis, {_ko} échoués\n{'='*52}")
sys.exit(1 if _ko else 0)
