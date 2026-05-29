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
import os
import tempfile
import time
from pathlib import Path
from typing import Callable

from pdf_extractor import extract_invoice_lines

# ── Configuration ──────────────────────────────────────────────────────────────

BASE_URL  = "https://app.digipharmacie.fr"
PAGE_SIZE = 100
YEAR      = "2025"
PROXY_URL = os.environ.get("PROXY_URL", "")

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


async def _get_csrf(page) -> str:
    for c in await page.context.cookies():
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
    csrf = await _get_csrf(page)
    if not csrf:
        await page.wait_for_timeout(800)
        csrf = await _get_csrf(page)
    api_url = (
        f"{BASE_URL}/api/v1/invoices/"
        f"?ordering=-billing_date&page_size={PAGE_SIZE}&page=1"
    )
    return await _paginate_fetch(page, api_url, csrf, progress)


async def _paginate_from_browser(page, first_data: dict, first_url: str, progress: Callable) -> list[dict]:
    invoices    = []
    total_seen  = 0
    page_num    = 1
    csrf        = await _get_csrf(page)

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

    proxy_cfg = None
    if PROXY_URL:
        import urllib.parse as _up
        _p = _up.urlparse(PROXY_URL)
        proxy_cfg = {
            "server":   f"{_p.scheme}://{_p.hostname}:{_p.port}",
            "username": _p.username or "",
            "password": _p.password or "",
        }

    # ── Phase 1 : curl_cffi login (fast path — ~5s, bypass Cloudflare CSRF) ──────
    session_cookies: dict = {}
    try:
        from curl_cffi import requests as cffi_requests
        proxy_kw = {"proxy": PROXY_URL} if PROXY_URL else {}
        session  = cffi_requests.Session(impersonate="chrome124")
        r = session.get(f"{BASE_URL}/login/",
                        headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                 "Accept-Language": "fr-FR,fr;q=0.9"},
                        timeout=25, allow_redirects=True, **proxy_kw)
        csrf = session.cookies.get("csrftoken", "")
        progress(f"curl_cffi GET /login/ → {r.status_code}  csrf={'ok' if csrf else 'manquant'}")
        if csrf:
            api_hdrs = {
                "Accept": "application/json", "Content-Type": "application/json",
                "X-CSRFToken": csrf, "Referer": f"{BASE_URL}/login/", "Origin": BASE_URL,
            }
            for ep in ["/api/v1/auth/login/", "/api/auth/login/", "/api/v1/token/", "/api/token/"]:
                try:
                    rp = session.post(f"{BASE_URL}{ep}",
                                      json={"email": username, "password": password},
                                      headers=api_hdrs, timeout=15,
                                      allow_redirects=False, **proxy_kw)
                    if rp.status_code == 200:
                        session_cookies = dict(session.cookies)
                        progress(f"curl_cffi login OK via {ep}")
                        break
                    if rp.status_code in (400, 401):
                        raise RuntimeError("Identifiants DIGIPHARMACIE incorrects")
                except RuntimeError:
                    raise
                except Exception:
                    continue
            if not session_cookies:
                rp = session.post(f"{BASE_URL}/login/",
                                  data={"email": username, "password": password,
                                        "csrfmiddlewaretoken": csrf},
                                  headers={"Content-Type": "application/x-www-form-urlencoded",
                                           "Referer": f"{BASE_URL}/login/"},
                                  timeout=15, allow_redirects=True, **proxy_kw)
                if "/login" not in rp.url:
                    session_cookies = dict(session.cookies)
                    progress("curl_cffi form login OK")
    except RuntimeError:
        raise
    except Exception as ce:
        progress(f"curl_cffi échoué ({ce}) — fallback camoufox…")

    # ── Phase 2 : camoufox (gère les challenges Cloudflare restants) ──────────────
    async with AsyncCamoufox(headless=True, geoip=False) as browser:
        ctx  = await browser.new_context(**({"proxy": proxy_cfg} if proxy_cfg else {}))

        if session_cookies:
            await ctx.add_cookies([
                {"name": k, "value": v, "domain": "app.digipharmacie.fr", "path": "/",
                 "sameSite": "Lax"}
                for k, v in session_cookies.items()
            ])
            progress(f"Cookies curl_cffi injectés ({len(session_cookies)} cookies)")

        page = await ctx.new_page()
        page.on("pageerror", lambda e: None)
        page.set_default_timeout(120_000)

        if not session_cookies:
            progress(f"Login via camoufox… (user: {username[:4]}***)")
            await page.goto(f"{BASE_URL}/login/", timeout=90_000)
            title = await page.title()
            _cf_kw = ("just a moment", "checking", "verifying", "cloudflare")
            if any(k in title.lower() for k in _cf_kw):
                progress("Challenge Cloudflare — attente résolution (90s max)…")
                await page.wait_for_function(
                    "() => !['just a moment','checking','verifying','cloudflare']"
                    ".some(k => document.title.toLowerCase().includes(k))",
                    timeout=90_000, polling=2000,
                )

            # Phase 2a : login via JS fetch depuis le contexte browser
            # (le cookie CF clearance est déjà présent — même stratégie que test_connector.py)
            _js_login = """async ([email, password]) => {
                const endpoints = [
                    '/api/v1/auth/login/',
                    '/api/auth/login/',
                    '/api/v1/token/',
                    '/api/token/',
                ];
                const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
                for (const ep of endpoints) {
                    try {
                        const r = await fetch(ep, {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-CSRFToken': csrf,
                            },
                            body: JSON.stringify({email, password}),
                            credentials: 'include',
                        });
                        if (r.status === 200) return {ok: true, ep};
                        if (r.status === 400 || r.status === 401)
                            return {ok: false, bad_creds: true, status: r.status, ep};
                    } catch(e) {}
                }
                return {ok: false, bad_creds: false};
            }"""
            _api_ok = False
            try:
                _res = await page.evaluate(_js_login, [username, password])
                progress(f"API login: {_res}")
                if _res.get("ok"):
                    progress(f"Login API OK via {_res.get('ep')}")
                    _api_ok = True
                elif _res.get("bad_creds"):
                    raise RuntimeError("Identifiants DIGIPHARMACIE incorrects")
            except RuntimeError:
                raise
            except Exception as _je:
                progress(f"API login échoué ({_je}) — fallback formulaire…")

            if not _api_ok:
                # Phase 2b : fallback formulaire HTML
                _email_sel = ("input[type='email'], input[name='email'], "
                              "input[name='username'], input[type='text']")
                try:
                    await page.wait_for_selector(_email_sel, timeout=30_000)
                except Exception:
                    raise RuntimeError(f"Formulaire de login introuvable (URL: {page.url})")

                await page.locator(_email_sel).first.fill(username)
                await page.locator("input[type='password']").first.fill(password)
                progress("Formulaire rempli")

                submitted = False
                for btn_sel in [
                    "button[type='submit']", "input[type='submit']",
                    "button:has-text('Connexion')", "button:has-text('Se connecter')",
                    "button:has-text('Login')",
                ]:
                    if await page.locator(btn_sel).count() > 0:
                        await page.locator(btn_sel).first.click()
                        submitted = True
                        progress(f"Submit via '{btn_sel}'")
                        break
                if not submitted:
                    await page.locator("input[type='password']").first.press("Enter")
                    progress("Submit via Enter")

                try:
                    await page.wait_for_url(f"{BASE_URL}/**", wait_until="load", timeout=30_000)
                except Exception:
                    pass
                if "/login" in page.url:
                    err_txt = await page.evaluate(
                        "() => document.querySelector('[class*=error],[class*=alert],[class*=invalid]')?.innerText || ''"
                    )
                    if err_txt:
                        progress(f"Erreur page : {err_txt[:200]}")
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
