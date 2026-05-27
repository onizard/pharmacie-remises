"""
Scraper DIGIPHARMACIE — login camoufox async + interception réseau

Stratégie :
1. Login camoufox (contourne Cloudflare)
2. Naviguer sur /factures/ et intercepter les réponses API que la SPA fait
3. Pour la pagination : fetch() JS depuis le contexte /factures/
"""

import asyncio
import base64
import json
import tempfile
import time
from pathlib import Path
from typing import Callable

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


# ── Récupération des factures ──────────────────────────────────────────────────

async def _fetch_invoices(page, progress: Callable) -> list[dict]:
    progress("Navigation vers les factures…")

    first_data = {}
    first_api_url = [None]

    def on_response(response):
        if first_data:
            return
        url = response.url
        if ("invoices" in url or "facture" in url.lower()) and response.status == 200:
            try:
                body = response.json()
                if isinstance(body, dict) and ("results" in body or "count" in body):
                    first_data.update(body)
                    first_api_url[0] = url
            except Exception:
                pass

    page.on("response", on_response)
    await page.goto(f"{BASE_URL}/factures/", wait_until="networkidle", timeout=60_000)
    await page.wait_for_timeout(4000)
    page.off("response", on_response)

    if first_data and first_api_url[0]:
        progress(f"API détectée : {first_api_url[0]}")
        return await _paginate_from_browser(page, first_data, first_api_url[0], progress)

    progress("Tentative API directe depuis /factures/…")
    csrf = _get_csrf(page)
    if not csrf:
        await page.wait_for_timeout(800)
        csrf = _get_csrf(page)
    api_url = (
        f"{BASE_URL}/api/v1/invoices/"
        f"?ordering=-billing_date&page_size={PAGE_SIZE}&page=1"
    )
    return await _paginate_fetch(page, api_url, csrf, progress)


async def _paginate_from_browser(page, first_data: dict, first_url: str, progress: Callable) -> list[dict]:
    invoices    = []
    total_seen  = 0
    page_num    = 1
    csrf        = _get_csrf(page)

    data = first_data
    while True:
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

        next_url = data.get("next")
        if stop_early or not next_url:
            break

        page_num += 1
        data = await _js_fetch_json(page, next_url, csrf)
        await page.wait_for_timeout(300)

    return invoices


async def _paginate_fetch(page, start_url: str, csrf: str, progress: Callable) -> list[dict]:
    invoices, page_num, total_seen = [], 1, 0
    url = start_url

    while True:
        data    = await _js_fetch_json(page, url, csrf)
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

        next_url = data.get("next")
        if stop_early or not next_url:
            break

        page_num += 1
        url = next_url
        await page.wait_for_timeout(300)

    return invoices


async def _js_fetch_json(page, url: str, csrf: str) -> dict:
    result = await page.evaluate("""
        async ([url, csrf]) => {
            const resp = await fetch(url, {
                credentials: 'include',
                headers: {
                    'Accept':           'application/json',
                    'X-CSRFToken':      csrf,
                    'X-Requested-With': 'XMLHttpRequest',
                }
            });
            const text = await resp.text();
            return { status: resp.status, text };
        }
    """, [url, csrf])

    if result["status"] != 200:
        raise RuntimeError(f"API HTTP {result['status']}")
    try:
        return json.loads(result["text"])
    except Exception:
        snippet = result["text"][:200].replace("\n", " ")
        raise RuntimeError(f"Réponse non-JSON : {snippet}")


# ── PDF download + extraction ──────────────────────────────────────────────────

async def _process_pdf(page, inv: dict) -> list[dict]:
    file_url = inv.get("file") or inv.get("file_url") or ""
    if not file_url:
        return []

    provider     = inv.get("provider_ref") or inv.get("provider_name") or ""
    billing_date = inv.get("billing_date", "")

    b64 = await page.evaluate("""
        async ([url]) => {
            try {
                const resp = await fetch(url, { credentials: 'include' });
                if (!resp.ok) return null;
                const buf   = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (const b of bytes) bin += String.fromCharCode(b);
                return btoa(bin);
            } catch(e) { return null; }
        }
    """, [file_url])

    if not b64:
        return []
    content = base64.b64decode(b64)
    if len(content) < 500:
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

async def _run_scraper_async(creds: dict, progress: Callable) -> list[dict]:
    from camoufox.async_api import AsyncCamoufox

    username = creds["user"]
    password = creds["pass"]

    async with AsyncCamoufox(headless=True, geoip=True) as browser:
        page = await browser.new_page()

        progress("Connexion à DIGIPHARMACIE (Cloudflare ~15s)…")
        await page.goto(f"{BASE_URL}/login/", timeout=90_000)

        try:
            await page.wait_for_selector("input[type='email']", timeout=60_000)
        except Exception:
            raise RuntimeError(f"Formulaire de login DIGIPHARMACIE introuvable (URL: {page.url})")

        await page.locator("input[type='email']").first.fill(username)
        await page.locator("input[type='password']").first.fill(password)
        await page.locator("input[type='password']").first.press("Enter")

        try:
            await page.wait_for_url("**/dashboard**", timeout=20_000)
        except Exception:
            try:
                await page.wait_for_function(
                    "() => !window.location.pathname.includes('/login')",
                    timeout=15_000,
                )
            except Exception:
                pass

        if "/login" in page.url:
            raise RuntimeError("Identifiants DIGIPHARMACIE incorrects")

        invoices = await _fetch_invoices(page, progress)
        if not invoices:
            progress("Aucune facture générique 2025 trouvée")
            return []

        progress(f"{len(invoices)} factures trouvées — extraction PDF…")

        all_lines = []
        for i, inv in enumerate(invoices, 1):
            progress(f"PDF {i}/{len(invoices)} — {inv.get('provider_ref', '?')}")
            lines = await _process_pdf(page, inv)
            all_lines.extend(lines)
            await page.wait_for_timeout(150)

        progress(f"Extraction terminée — {len(all_lines)} lignes produits")

    return all_lines


def run_scraper(creds: dict, progress: Callable) -> list[dict]:
    return asyncio.run(_run_scraper_async(creds, progress))
