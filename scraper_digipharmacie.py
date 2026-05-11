"""
Scraper DIGIPHARMACIE — Téléchargement automatique des PDFs de factures
URL : https://app.digipharmacie.fr/login

Prérequis dans .env :
    BP_EMAIL=votre_email_break_pharma
    BP_PASSWORD=votre_mot_de_passe_break_pharma

Les identifiants DIGIPHARMACIE sont lus depuis votre compte break-pharma.fr
(bouton CONNECTEUR → DIGIPHARMACIE).

Usage :
    python scraper_digipharmacie.py
"""

import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin
from get_connectors import get_connectors
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Configuration ─────────────────────────────────────────────────────────────

BASE_URL   = "https://app.digipharmacie.fr"
LOGIN_URL  = "https://app.digipharmacie.fr/login"
OUTPUT_DIR = Path("pdfs_factures")

# ── Helpers ───────────────────────────────────────────────────────────────────

def download_pdf(context, url: str, dest: Path) -> bool:
    try:
        cookies    = context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        req = urllib.request.Request(url, headers={
            "Cookie":     cookie_str,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Referer":    BASE_URL,
            "Accept":     "application/pdf,*/*",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            if len(data) > 500 and (data[:4] == b"%PDF" or "pdf" in resp.headers.get("Content-Type", "").lower()):
                dest.write_bytes(data)
                return True
            print(f"  ⚠️  Réponse non-PDF ({len(data)} octets)")
    except Exception as e:
        print(f"  ❌  {e}")
    return False

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("🔑  Récupération des identifiants depuis break-pharma.fr…")
    try:
        creds = get_connectors()
    except Exception as e:
        print(f"❌  {e}")
        return

    USERNAME = creds["digipharmacie"].get("user", "")
    PASSWORD = creds["digipharmacie"].get("pass", "")

    if not USERNAME or not PASSWORD:
        print("❌  Identifiants DIGIPHARMACIE vides.")
        print("    Remplis-les dans break-pharma.fr → bouton CONNECTEUR → DIGIPHARMACIE.")
        return

    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"📁  Dossier de sortie : {OUTPUT_DIR.resolve()}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

        # ── 1. Connexion ──────────────────────────────────────────────────────
        print("🔐  Connexion à DIGIPHARMACIE…")
        page.goto(LOGIN_URL, wait_until="networkidle")

        try:
            page.locator("input[type='email'], input[name='email'], input[name='username'], #email, #username").first.fill(USERNAME)
            page.locator("input[type='password'], input[name='password'], #password").first.fill(PASSWORD)
            page.locator("button[type='submit'], input[type='submit'], button:has-text('Connexion'), button:has-text('Se connecter'), button:has-text('Login')").first.click()
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            print("⚠️  Timeout lors de la connexion.")

        if "login" in page.url.lower():
            print(f"❌  Échec de la connexion (URL : {page.url})")
            browser.close()
            return

        print(f"✅  Connecté ! (URL : {page.url})\n")

        # ── 2. Téléchargement des factures PDF ────────────────────────────────
        # ⚠️  Adapter la navigation selon la structure réelle du site
        print("🔍  Recherche des factures PDF…\n")

        downloaded = 0
        seen       = set()

        pdf_links = page.query_selector_all(
            "a[href*='.pdf'], a[href*='facture'], a[href*='invoice'], "
            "a[href*='download'], button:has-text('Télécharger'), "
            "a:has-text('PDF'), a:has-text('Facture')"
        )

        if pdf_links:
            print(f"📦  {len(pdf_links)} facture(s) détectée(s).\n")
            for i, link in enumerate(pdf_links, 1):
                try:
                    href = link.get_attribute("href") or ""
                    if not href:
                        with context.expect_download(timeout=20000) as dl_info:
                            link.click()
                        download = dl_info.value
                        filename = download.suggested_filename or f"facture_{i}.pdf"
                        dest = OUTPUT_DIR / filename
                        if str(dest) not in seen:
                            download.save_as(dest)
                            seen.add(str(dest))
                            print(f"[{i}] ✅  {filename}")
                            downloaded += 1
                        continue

                    url = urljoin(BASE_URL, href) if not href.startswith("http") else href
                    if url in seen:
                        continue
                    seen.add(url)

                    filename = url.split("/")[-1].split("?")[0] or f"facture_{i}.pdf"
                    if not filename.lower().endswith(".pdf"):
                        filename += ".pdf"
                    dest = OUTPUT_DIR / filename

                    if dest.exists():
                        print(f"[{i}] ⏭️  {filename} déjà téléchargé")
                        downloaded += 1
                        continue

                    print(f"[{i}] {filename}")
                    if download_pdf(context, url, dest):
                        print(f"  ✅  Sauvegardé")
                        downloaded += 1
                    time.sleep(0.3)

                except Exception as e:
                    print(f"[{i}] ❌  {e}")
        else:
            print("⚠️  Aucune facture PDF détectée automatiquement.")
            page.screenshot(path="digipharmacie_debug.png")
            print("    → Capture sauvegardée : digipharmacie_debug.png")
            print("    → Partage cette image pour adapter les sélecteurs.")

        browser.close()

    print(f"\n🎉  Terminé : {downloaded} PDF(s) dans « {OUTPUT_DIR.resolve()} »")


if __name__ == "__main__":
    main()
