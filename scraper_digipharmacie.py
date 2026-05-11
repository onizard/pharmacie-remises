"""
Scraper DIGIPHARMACIE — Factures PDF des laboratoires génériques 2025

Flux :
  1. Login via camoufox (contourne Cloudflare Turnstile en ~8s)
  2. Extraction des cookies de session
  3. Appel paginé de l'API Django REST /invoices/ via curl_cffi
  4. Filtrage côté client : billing_date 2025 + fournisseur générique
  5. Téléchargement des PDFs (GCS signed URLs)

Prérequis dans .env :
    BP_EMAIL=votre_email_break_pharma
    BP_PASSWORD=votre_mot_de_passe_break_pharma

Les identifiants DIGIPHARMACIE sont lus depuis votre compte break-pharma.fr
(bouton CONNECTEUR → DIGIPHARMACIE).

Usage :
    python scraper_digipharmacie.py
"""

import json
import re
import time
from pathlib import Path
from get_connectors import get_connectors

try:
    from camoufox.sync_api import Camoufox
except ImportError:
    raise SystemExit(
        "❌  camoufox non installé.\n"
        "    pip install camoufox && python -m camoufox fetch"
    )

try:
    from curl_cffi.requests import Session as CurlSession
except ImportError:
    raise SystemExit("❌  curl_cffi non installé.\n    pip install curl_cffi")

# ── Configuration ──────────────────────────────────────────────────────────────

BASE_URL    = "https://app.digipharmacie.fr"
OUTPUT_DIR  = Path("pdf_factures_generiques")
PAGE_SIZE   = 100
YEAR_FILTER = "2025"

# Mots-clés (minuscules) pour identifier les fournisseurs génériques.
# Si le nom du fournisseur contient l'un de ces termes, la facture est retenue.
LABOS_GENERIQUES = [
    "cerp",
    "mylan",
    "viatris",
    "biogaran",
    "arrow",
    "sandoz",
    "teva",
    "zentiva",
    "cooperation pharmaceutique",
    "cooperation pharma",
    "alloga",
    "cristers",
    "eg labo",
    " eg ",
    "ranbaxy",
    "ratiopharm",
    "actavis",
    "hexal",
    "aurobindo",
    "intas",
    "sun pharma",
    "pharmaki",
    "strides",
    "qualimed",
    "almus",
    "ibigen",
    "substipharm",
    "evolupharm",
    "medipha",
    "phlorogine",
]


def is_generic_provider(provider_name: str) -> bool:
    name = (provider_name or "").lower()
    return any(kw in name for kw in LABOS_GENERIQUES)


# ── Login via camoufox ─────────────────────────────────────────────────────────

def login_and_get_cookies(username: str, password: str) -> dict:
    """
    Ouvre app.digipharmacie.fr/login avec camoufox, remplit le formulaire,
    attend que Cloudflare Turnstile se résolve automatiquement (~8s),
    puis retourne les cookies de session sous forme de dict.
    """
    print("🦊  Démarrage de camoufox (contournement Cloudflare Turnstile)…")
    cookies = {}

    with Camoufox(headless=True, geoip=True) as browser:
        page = browser.new_page()

        print(f"  → Navigation vers {BASE_URL}/login/")
        page.goto(f"{BASE_URL}/login/", timeout=60000)

        # Attendre que le champ email soit disponible (Turnstile peut retarder)
        try:
            page.wait_for_selector("input[type='email']", timeout=40000)
        except Exception:
            page.screenshot(path="digi_login_debug.png")
            raise RuntimeError(
                "Impossible de trouver le formulaire de connexion.\n"
                "Capture sauvegardée : digi_login_debug.png"
            )

        print("  → Remplissage du formulaire…")
        page.locator("input[type='email']").first.fill(username)
        page.locator("input[type='password']").first.fill(password)

        # Le bouton est type='button' (pas type='submit') → Enter sur le champ password
        page.locator("input[type='password']").first.press("Enter")

        # Attendre la redirection post-login
        try:
            page.wait_for_url("**/dashboard**", timeout=20000)
        except Exception:
            try:
                page.wait_for_function(
                    "() => !window.location.pathname.includes('/login')",
                    timeout=15000
                )
            except Exception:
                pass

        current_url = page.url
        if "/login" in current_url:
            page.screenshot(path="digi_login_debug.png")
            raise RuntimeError(
                f"Échec du login (toujours sur {current_url}).\n"
                "Capture sauvegardée : digi_login_debug.png"
            )

        print(f"  ✓  Connecté ! (URL : {current_url})")

        for c in page.context.cookies():
            cookies[c["name"]] = c["value"]

        page.close()

    required = {"sessionid", "csrftoken"}
    missing  = required - set(cookies)
    if missing:
        raise RuntimeError(f"Cookies manquants après login : {missing}")

    print(f"  ✓  Cookies extraits : {sorted(cookies.keys())}")
    return cookies


# ── Session curl_cffi ──────────────────────────────────────────────────────────

def make_session(cookies: dict) -> CurlSession:
    sess = CurlSession(impersonate="chrome")
    sess.cookies.update(cookies)
    sess.headers.update({
        "Referer":          f"{BASE_URL}/factures/",
        "X-CSRFToken":      cookies.get("csrftoken", ""),
        "Accept":           "application/json",
        "X-Requested-With": "XMLHttpRequest",
    })
    return sess


# ── Récupération des factures ──────────────────────────────────────────────────

def fetch_all_invoices_2025(sess: CurlSession) -> list[dict]:
    """
    Pagine /api/v1/invoices/ et retourne les factures dont billing_date
    commence par YEAR_FILTER et dont le fournisseur est générique.
    """
    invoices   = []
    page_num   = 1
    total_seen = 0

    print(f"\n📋  Récupération des factures (filtre : {YEAR_FILTER}, génériques)…")

    while True:
        url  = (
            f"{BASE_URL}/api/v1/invoices/"
            f"?ordering=-billing_date&page_size={PAGE_SIZE}&page={page_num}"
        )
        resp = sess.get(url, timeout=30)

        if resp.status_code != 200:
            print(f"  ❌  Erreur API page {page_num} : HTTP {resp.status_code}")
            print(f"       {resp.text[:300]}")
            break

        data    = resp.json()
        results = data.get("results", data if isinstance(data, list) else [])

        if not results:
            break

        total_seen += len(results)
        stop_early  = False

        for inv in results:
            billing_date = str(inv.get("billing_date", ""))
            provider     = (
                inv.get("provider_ref") or inv.get("provider_name") or ""
            )

            # Arrêt anticipé : résultats triés par date desc, on ne trouvera plus rien
            if billing_date and billing_date[:4] < YEAR_FILTER:
                print(f"  → Date {billing_date} < {YEAR_FILTER}, arrêt de la pagination.")
                stop_early = True
                break

            if not billing_date.startswith(YEAR_FILTER):
                continue

            if is_generic_provider(provider):
                invoices.append(inv)

        count_str = data.get("count", "?")
        print(
            f"  page {page_num} — {total_seen}/{count_str} lues, "
            f"{len(invoices)} génériques {YEAR_FILTER} retenues"
        )

        if stop_early or not data.get("next"):
            break

        page_num += 1
        time.sleep(0.3)

    return invoices


# ── Téléchargement des PDFs ────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    return re.sub(r'[^\w\-_\. ]', '_', str(name)).strip()[:80]


def download_pdfs(invoices: list[dict], sess: CurlSession) -> int:
    OUTPUT_DIR.mkdir(exist_ok=True)
    total   = len(invoices)
    success = 0
    skip    = 0

    print(f"\n⬇️   Téléchargement de {total} factures PDF…")

    for i, inv in enumerate(invoices, 1):
        file_url = inv.get("file") or inv.get("file_url") or ""

        if not file_url:
            print(f"  [{i}/{total}] ⚠  Pas d'URL PDF pour facture {inv.get('id', '?')}")
            skip += 1
            continue

        billing_date = inv.get("billing_date", "inconnu")
        provider     = safe_filename(
            inv.get("provider_ref") or inv.get("provider_name") or "inconnu"
        )
        inv_id       = inv.get("id", i)
        number       = inv.get("number") or inv.get("invoice_number") or ""
        number_part  = f"_{safe_filename(number)}" if number else ""
        filename     = f"{billing_date}_{provider}{number_part}_{inv_id}.pdf"
        out_path     = OUTPUT_DIR / filename

        if out_path.exists():
            print(f"  [{i}/{total}] ⏭  Déjà présent : {filename}")
            skip += 1
            continue

        try:
            # GCS signed URLs sont accessibles directement (sans cookie d'auth Django)
            dl_resp = sess.get(file_url, timeout=60)
            if dl_resp.status_code == 200:
                content = dl_resp.content
                if len(content) > 500:
                    out_path.write_bytes(content)
                    size_kb = len(content) // 1024
                    print(f"  [{i}/{total}] ✓  {filename} ({size_kb} ko)")
                    success += 1
                else:
                    print(f"  [{i}/{total}] ⚠  Contenu trop court ({len(content)} octets) : {filename}")
                    skip += 1
            else:
                print(f"  [{i}/{total}] ❌  HTTP {dl_resp.status_code} : {filename}")
                skip += 1
        except Exception as e:
            print(f"  [{i}/{total}] ❌  {e}")
            skip += 1

        time.sleep(0.2)

    print(f"\n📊  {success} téléchargés · {skip} ignorés/erreurs · {total} total")
    return success


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("🔑  Récupération des identifiants depuis break-pharma.fr…")
    try:
        creds = get_connectors()
    except Exception as e:
        print(f"❌  {e}")
        return

    username = creds["digipharmacie"].get("user", "")
    password = creds["digipharmacie"].get("pass", "")

    if not username or not password:
        print("❌  Identifiants DIGIPHARMACIE vides.")
        print("    Remplis-les dans break-pharma.fr → bouton CONNECTEUR → DIGIPHARMACIE.")
        return

    print(f"📁  Dossier de sortie : {OUTPUT_DIR.resolve()}\n")

    # 1. Login via camoufox
    try:
        cookies = login_and_get_cookies(username, password)
    except RuntimeError as e:
        print(f"❌  {e}")
        return

    # 2. Session curl_cffi avec les cookies
    sess = make_session(cookies)

    # 3. Factures génériques 2025
    invoices = fetch_all_invoices_2025(sess)

    if not invoices:
        print(
            f"\n⚠️  Aucune facture générique {YEAR_FILTER} trouvée.\n"
            "   Vérifie la liste LABOS_GENERIQUES ou les identifiants DIGIPHARMACIE."
        )
        return

    print(f"\n✅  {len(invoices)} factures génériques {YEAR_FILTER} identifiées.")

    # Aperçu des fournisseurs
    providers: dict[str, int] = {}
    for inv in invoices:
        p = inv.get("provider_ref") or inv.get("provider_name") or "?"
        providers[p] = providers.get(p, 0) + 1

    print("\nFournisseurs :")
    for prov, cnt in sorted(providers.items(), key=lambda x: -x[1]):
        print(f"  {cnt:4d}  {prov}")

    # 4. Téléchargement
    download_pdfs(invoices, sess)
    print(f"\n🎉  Terminé. PDFs dans : {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
