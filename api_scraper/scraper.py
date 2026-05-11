"""
Scraper DIGIPHARMACIE — login camoufox + API curl_cffi + extraction PDF

Appelé par main.py dans un thread séparé.
"""

import tempfile
import time
from pathlib import Path
from typing import Callable

from camoufox.sync_api import Camoufox
from curl_cffi.requests import Session as CurlSession

from pdf_extractor import extract_invoice_lines

# ── Configuration ──────────────────────────────────────────────────────────────

BASE_URL  = "https://app.digipharmacie.fr"
PAGE_SIZE = 100
YEAR      = "2025"

LABOS_GENERIQUES = [
    "cerp", "mylan", "viatris", "biogaran", "arrow", "sandoz", "teva",
    "zentiva", "cooperation pharmaceutique", "cooperation pharma", "alloga",
    "cristers", "eg labo", " eg ", "ranbaxy", "ratiopharm", "actavis",
    "hexal", "aurobindo", "intas", "sun pharma", "pharmaki", "strides",
    "qualimed", "almus", "ibigen", "substipharm", "evolupharm", "medipha",
    "phlorogine",
]


def _is_generic(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in LABOS_GENERIQUES)


# ── Login ──────────────────────────────────────────────────────────────────────

def _login(username: str, password: str, progress: Callable) -> dict:
    progress("Connexion à DIGIPHARMACIE (Cloudflare ~8s)…")
    cookies = {}

    with Camoufox(headless=True, geoip=True) as browser:
        page = browser.new_page()
        page.goto(f"{BASE_URL}/login/", timeout=60_000)

        try:
            page.wait_for_selector("input[type='email']", timeout=40_000)
        except Exception:
            raise RuntimeError("Formulaire de login DIGIPHARMACIE introuvable")

        page.locator("input[type='email']").first.fill(username)
        page.locator("input[type='password']").first.fill(password)
        page.locator("input[type='password']").first.press("Enter")

        try:
            page.wait_for_url("**/dashboard**", timeout=20_000)
        except Exception:
            try:
                page.wait_for_function(
                    "() => !window.location.pathname.includes('/login')",
                    timeout=15_000,
                )
            except Exception:
                pass

        if "/login" in page.url:
            raise RuntimeError("Échec du login DIGIPHARMACIE — identifiants incorrects ?")

        for c in page.context.cookies():
            cookies[c["name"]] = c["value"]
        page.close()

    if not {"sessionid", "csrftoken"} <= set(cookies):
        raise RuntimeError("Cookies de session manquants après login")

    return cookies


# ── API invoices ───────────────────────────────────────────────────────────────

def _make_session(cookies: dict) -> CurlSession:
    sess = CurlSession(impersonate="chrome")
    sess.cookies.update(cookies)
    sess.headers.update({
        "Referer":          f"{BASE_URL}/factures/",
        "X-CSRFToken":      cookies.get("csrftoken", ""),
        "Accept":           "application/json",
        "X-Requested-With": "XMLHttpRequest",
    })
    return sess


def _fetch_invoices(sess: CurlSession, progress: Callable) -> list[dict]:
    progress("Récupération des factures 2025…")
    invoices, page_num, total_seen = [], 1, 0

    while True:
        url  = (
            f"{BASE_URL}/api/v1/invoices/"
            f"?ordering=-billing_date&page_size={PAGE_SIZE}&page={page_num}"
        )
        resp = sess.get(url, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"API /invoices/ : HTTP {resp.status_code}")

        data    = resp.json()
        results = data.get("results", data if isinstance(data, list) else [])
        if not results:
            break

        total_seen += len(results)
        stop_early  = False

        for inv in results:
            date     = str(inv.get("billing_date", ""))
            provider = inv.get("provider_ref") or inv.get("provider_name") or ""

            if date and date[:4] < YEAR:
                stop_early = True
                break

            if date.startswith(YEAR) and _is_generic(provider):
                invoices.append(inv)

        progress(f"Page {page_num} — {total_seen} lues · {len(invoices)} génériques {YEAR}")

        if stop_early or not data.get("next"):
            break
        page_num += 1
        time.sleep(0.3)

    return invoices


# ── PDF download + extraction ──────────────────────────────────────────────────

def _process_pdf(inv: dict, sess: CurlSession) -> list[dict]:
    """
    Télécharge le PDF de la facture, extrait les lignes produits.
    Retourne une liste de dicts prêts pour le frontend.
    """
    file_url = inv.get("file") or inv.get("file_url") or ""
    if not file_url:
        return []

    provider     = inv.get("provider_ref") or inv.get("provider_name") or ""
    billing_date = inv.get("billing_date", "")

    try:
        resp = sess.get(file_url, timeout=60)
        if resp.status_code != 200 or len(resp.content) < 500:
            return []
    except Exception:
        return []

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(resp.content)
        tmp_path = Path(tmp.name)

    try:
        lines = extract_invoice_lines(tmp_path, provider, billing_date)
    finally:
        tmp_path.unlink(missing_ok=True)

    return lines


# ── Entry point ────────────────────────────────────────────────────────────────

def run_scraper(creds: dict, progress: Callable) -> list[dict]:
    """
    Point d'entrée appelé par main.py.
    Retourne la liste de toutes les lignes extraites des PDFs.
    """
    username = creds["user"]
    password = creds["pass"]

    # 1. Login
    cookies = _login(username, password, progress)
    sess    = _make_session(cookies)

    # 2. Récupérer les factures génériques 2025
    invoices = _fetch_invoices(sess, progress)
    if not invoices:
        return []

    progress(f"{len(invoices)} factures trouvées — extraction PDF…")

    # 3. Extraire les données PDF
    all_lines = []
    for i, inv in enumerate(invoices, 1):
        progress(f"PDF {i}/{len(invoices)} — {inv.get('provider_ref','?')}")
        lines = _process_pdf(inv, sess)
        all_lines.extend(lines)
        time.sleep(0.15)

    progress(f"Extraction terminée — {len(all_lines)} lignes produits")
    return all_lines
