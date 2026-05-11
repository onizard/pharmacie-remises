"""
Scraper DIGIPHARMACIE — login camoufox + appels API dans le contexte navigateur

Toutes les requêtes API et PDF sont faites via page.request.get() pour conserver
la session camoufox intacte sans transfert de cookies.
"""

import json
import tempfile
import time
from pathlib import Path
from typing import Callable

from camoufox.sync_api import Camoufox

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


def _get_csrf(page) -> str:
    for c in page.context.cookies():
        if c["name"] == "csrftoken":
            return c["value"]
    return ""


# ── API invoices (dans le contexte navigateur) ─────────────────────────────────

def _fetch_invoices(page, progress: Callable) -> list[dict]:
    progress("Récupération des factures 2025…")
    csrf     = _get_csrf(page)
    headers  = {
        "Accept":           "application/json",
        "X-CSRFToken":      csrf,
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          f"{BASE_URL}/factures/",
    }

    invoices, page_num, total_seen = [], 1, 0

    while True:
        url  = (
            f"{BASE_URL}/api/v1/invoices/"
            f"?ordering=-billing_date&page_size={PAGE_SIZE}&page={page_num}"
        )
        resp = page.request.get(url, headers=headers, timeout=30_000)

        if resp.status != 200:
            raise RuntimeError(f"API /invoices/ : HTTP {resp.status}")

        try:
            data = resp.json()
        except Exception:
            snippet = resp.text()[:120].replace("\n", " ")
            raise RuntimeError(f"Réponse API non-JSON (session expirée ?) : {snippet}")

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

def _process_pdf(page, inv: dict) -> list[dict]:
    file_url = inv.get("file") or inv.get("file_url") or ""
    if not file_url:
        return []

    provider     = inv.get("provider_ref") or inv.get("provider_name") or ""
    billing_date = inv.get("billing_date", "")

    try:
        resp = page.request.get(file_url, timeout=60_000)
        if resp.status != 200 or len(resp.body()) < 500:
            return []
        content = resp.body()
    except Exception:
        return []

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        lines = extract_invoice_lines(tmp_path, provider, billing_date)
    finally:
        tmp_path.unlink(missing_ok=True)

    return lines


# ── Entry point ────────────────────────────────────────────────────────────────

def run_scraper(creds: dict, progress: Callable) -> list[dict]:
    username = creds["user"]
    password = creds["pass"]

    with Camoufox(headless=True, geoip=True) as browser:
        page = browser.new_page()

        # 1. Login
        progress("Connexion à DIGIPHARMACIE (Cloudflare ~8s)…")
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
            raise RuntimeError("Identifiants DIGIPHARMACIE incorrects")

        progress("Connecté — récupération des factures 2025…")

        # 2. Factures via API dans le contexte navigateur
        invoices = _fetch_invoices(page, progress)
        if not invoices:
            progress("Aucune facture générique 2025 trouvée")
            return []

        progress(f"{len(invoices)} factures trouvées — extraction PDF…")

        # 3. PDFs via contexte navigateur
        all_lines = []
        for i, inv in enumerate(invoices, 1):
            progress(f"PDF {i}/{len(invoices)} — {inv.get('provider_ref', '?')}")
            lines = _process_pdf(page, inv)
            all_lines.extend(lines)
            time.sleep(0.15)

        progress(f"Extraction terminée — {len(all_lines)} lignes produits")

    return all_lines
