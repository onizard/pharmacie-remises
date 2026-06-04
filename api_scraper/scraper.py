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
import re
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

from pdf_extractor import extract_invoice_lines

# ── Configuration ──────────────────────────────────────────────────────────────

BASE_URL     = "https://app.digipharmacie.fr"
PAGE_SIZE    = 100
YEARS        = {"2025", "2026"}   # années à collecter
STOP_YEAR    = "2025"             # arrêter quand billing_date < 2025
PROXY_URL    = os.environ.get("PROXY_URL", "")
# Filtre optionnel : ne traiter que les PDFs de ce labo (ex: "biogaran"). Vide = tous.
LABS_FILTER  = os.environ.get("LABS_FILTER", "").lower().strip()

# Fournisseurs potentiels de factures génériqueurs :
# — labos génériqueurs eux-mêmes
# — dépositaires/grossistes qui facturent en leur nom
# — répartiteurs pharmaceutiques (CERP, OCP, Alliance, Phoenix)
LABOS_GENERIQUES = [
    # Labos génériqueurs cibles
    "biogaran", "teva", "mylan", "viatris", "zydus", "sandoz", "zentiva",
    "arrow", "cristers", "eg labo", "eg labs", "evolupharm",
    # Autres génériqueurs secondaires
    "ranbaxy", "ratiopharm", "actavis", "hexal", "aurobindo", "intas",
    "sun pharma", "pharmaki", "strides", "qualimed", "almus", "ibigen",
    "substipharm", "medipha", "phlorogine",
    # Dépositaires (facturent au nom des labos)
    "alloga", "cegedim", "movianto",
    # Répartiteurs pharmaceutiques (grossistes)
    "cerp", "ocp", "alliance", "phoenix",
    "cooperation pharmaceutique", "cooperation pharma",
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

    # Sauvegarder les objets Response (pas leur corps) — response.json() est une coroutine,
    # elle ne peut pas être attendue dans un handler synchrone.
    _captured: list[tuple[str, object]] = []

    def on_response(response):
        url = response.url
        ct  = response.headers.get("content-type", "")
        # Capturer uniquement les réponses JSON venant du même domaine
        if "json" in ct and response.status == 200 and BASE_URL in url:
            _captured.append((url, response))

    page.on("response", on_response)
    try:
        await page.goto(f"{BASE_URL}/factures/", wait_until="load", timeout=60_000)
    except Exception:
        pass  # domcontentloaded au minimum, on continue
    # Laisser la SPA charger ses données (appels API asynchrones)
    await page.wait_for_timeout(6000)

    # Cliquer "Charger plus" si présent — l'API limite à 6 mois par défaut,
    # ce bouton déclenche un appel API sans cette restriction.
    # Le bouton n'apparaît qu'après que la SPA a chargé la fin de la liste 6 mois.
    # Stratégie : scroll en bas + chercher le bouton + cliquer
    _charger_plus_texts = ["Charger plus", "Load more", "charger plus", "load more",
                           "Voir plus", "Voir tout"]
    _btn_found = False
    try:
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)
        for _txt in _charger_plus_texts:
            _sel = f"button:has-text('{_txt}'), a:has-text('{_txt}'), span:has-text('{_txt}')"
            _btn = page.locator(_sel).first
            if await _btn.count() > 0:
                progress(f"Clic '{_txt}' pour élargir la recherche…")
                try:
                    await _btn.click(timeout=5_000)
                except Exception:
                    # Element non actionable (caché, overlay) → forcer via JS
                    try:
                        await _btn.evaluate("el => el.click()")
                    except Exception:
                        pass
                _btn_found = True
                await page.wait_for_timeout(6000)  # attendre les réponses API étendues
                break
        if not _btn_found:
            progress("Bouton 'Charger plus' non trouvé (OK — données déjà chargées ou absent)")
    except Exception as _e:
        progress(f"Charger plus : {_e}")

    page.remove_listener("response", on_response)

    # Sélectionner l'endpoint factures/invoices parmi les réponses JSON capturées
    all_urls = [u for u, _ in _captured]
    progress(f"{len(_captured)} réponses JSON du domaine : {all_urls[:5]}")
    invoice_urls = [u for u in all_urls if any(kw in u.lower() for kw in ("invoice", "facture", "bill"))]
    if invoice_urls:
        progress(f"URLs invoice capturées : {invoice_urls}")

    INVOICE_KEYWORDS = ("invoice", "facture", "bill", "order")

    def _build_clean_url(url: str) -> str:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        for key in list(params):
            if any(k in key for k in ("gte", "lte", "since", "created", "start", "end")):
                del params[key]
        params["page_size"] = [str(PAGE_SIZE)]
        params["page"]      = ["1"]
        if "ordering" not in params or not params["ordering"][0]:
            params["ordering"] = ["-billing_date"]
        new_query = urlencode({k: v[0] for k, v in params.items()})
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{new_query}"

    clean_url: str | None = None
    # Collecter TOUTES les URLs candidate (invoice/facture), puis prendre la DERNIÈRE
    # (capturée après le clic "Charger plus" si présent — URL avec le bon contexte étendu)
    invoice_candidates: list[str] = []
    for url, _resp in _captured:
        parsed   = urlparse(url)
        segments = [s for s in parsed.path.split("/") if s]
        kw_idx   = next((i for i, s in enumerate(segments)
                         if any(kw in s.lower() for kw in INVOICE_KEYWORDS)), -1)
        if kw_idx < 0:
            continue
        base_path = "/" + "/".join(segments[:kw_idx + 1]) + "/"
        base_url  = f"{parsed.scheme}://{parsed.netloc}{base_path}"
        invoice_candidates.append(base_url)

    if invoice_candidates:
        # Utiliser la DERNIÈRE candidate (post "Charger plus" si cliqué)
        chosen = invoice_candidates[-1]
        try:
            clean_url = _build_clean_url(chosen)
            progress(f"Endpoint factures : {urlparse(chosen).path} → {clean_url}")
        except Exception:
            pass

    csrf = await _get_csrf(page)
    if not csrf:
        await page.wait_for_timeout(800)
        csrf = await _get_csrf(page)

    if not clean_url:
        clean_url = f"{BASE_URL}/api/v1/invoices/?ordering=-billing_date&page_size={PAGE_SIZE}&page=1"
        progress("Endpoint non détecté — URL de fallback utilisée")

    return await _paginate_fetch(page, clean_url, csrf, progress)


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
            if date and date[:4] < STOP_YEAR:
                stop_early = True
                break
            if date[:4] in YEARS and _is_generic(provider):
                invoices.append(inv)

        progress(f"Page {page_num} — {total_seen} lues · {len(invoices)} génériques {'/'.join(sorted(YEARS))}")

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
            if date and date[:4] < STOP_YEAR:
                stop_early = True
                break
            if date[:4] in YEARS and _is_generic(provider):
                invoices.append(inv)

        progress(f"Page {page_num} — {total_seen} lues · {len(invoices)} génériques {'/'.join(sorted(YEARS))}")

        next_url = data.get("next")
        if stop_early:
            progress(f"Stop : billing_date < {STOP_YEAR} atteinte")
            break
        if not next_url:
            progress("Stop : fin de la liste (pas de next)")
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

def _extract_pdf_in_thread(tmp_path: Path, provider: str, billing_date: str) -> list[dict]:
    """Extraction pdfplumber dans un thread — appelée via run_in_executor."""
    try:
        return extract_invoice_lines(tmp_path, provider, billing_date)
    except Exception as e:
        print(f"[PDF] Erreur extraction — {provider} {billing_date} : {e}", flush=True)
        return []


def _download_pdf_sync(file_url: str) -> bytes:
    """Téléchargement synchrone dans un thread — appelé via run_in_executor."""
    import urllib.request as _ul
    req = _ul.Request(file_url, headers={"User-Agent": "Mozilla/5.0"})
    with _ul.urlopen(req, timeout=30) as r:
        return r.read()


async def _process_pdf(page, inv: dict) -> list[dict]:
    file_url = inv.get("file") or inv.get("file_url") or ""
    if not file_url:
        return []

    provider     = inv.get("provider_ref") or inv.get("provider_name") or ""
    billing_date = inv.get("billing_date", "")
    loop         = asyncio.get_event_loop()

    # ── Téléchargement dans un thread (libère le event loop, timeout réel de 35s) ─
    import os as _os
    if _os.environ.get("PDF_DEBUG") == "1":
        print(f"[PDF] Téléchargement : {file_url[:140]}", flush=True)
    try:
        content = await asyncio.wait_for(
            loop.run_in_executor(None, _download_pdf_sync, file_url),
            timeout=35,
        )
        if _os.environ.get("PDF_DEBUG") == "1":
            print(f"[PDF] OK : {len(content)} bytes", flush=True)
    except (asyncio.TimeoutError, Exception) as _e:
        if _os.environ.get("PDF_DEBUG") == "1":
            print(f"[PDF] Échec download : {_e}", flush=True)
        return []

    if len(content) < 500:
        return []

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        # Mode diagnostic
        if _os.environ.get("PDF_DEBUG") == "1":
            import pdfplumber
            print(f"\n{'='*60}\nPDF DEBUG : {provider} | {billing_date} | {len(content)} bytes\n{'='*60}", flush=True)
            with pdfplumber.open(str(tmp_path)) as _pdf:
                for _pn, _pg in enumerate(_pdf.pages, 1):
                    print(f"\n--- PAGE {_pn} ---\n{(_pg.extract_text() or '')[:4000]}", flush=True)

        # ── Extraction dans un thread (libère le event loop, timeout réel de 25s) ──
        try:
            lines = await asyncio.wait_for(
                loop.run_in_executor(None, _extract_pdf_in_thread, tmp_path, provider, billing_date),
                timeout=25,
            )
        except asyncio.TimeoutError:
            print(f"[PDF] TIMEOUT extraction 25s — {provider} {billing_date} ignoré", flush=True)
            lines = []
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
                progress("Challenge Cloudflare détecté — attente formulaire (120s)…")

            # Attendre directement l'apparition d'un input (couvre CF challenge + React mount)
            _form_sel = (
                "input[type='email'], input[name='email'], "
                "input[name='username'], input[type='text'], input[type='password']"
            )
            try:
                await page.wait_for_selector(_form_sel, timeout=120_000)
                progress(f"Formulaire présent. Title: {await page.title()!r}")
            except Exception:
                raise RuntimeError(f"Formulaire introuvable après 120s (URL: {page.url})")

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
                progress(f"Remplissage formulaire. URL: {page.url}")
                await page.locator(_email_sel).first.fill(username)
                await page.locator("input[type='password']").first.fill(password)
                await page.locator("input[type='password']").first.press("Enter")
                progress("Formulaire soumis via Enter")

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

        # Appliquer le filtre labo si défini (ex: LABS_FILTER=biogaran)
        if LABS_FILTER:
            filtered = [inv for inv in invoices
                        if LABS_FILTER in (inv.get("provider_ref") or inv.get("provider_name") or "").lower()]
            progress(f"{len(invoices)} factures → {len(filtered)} après filtre '{LABS_FILTER}'")
            invoices = filtered

        # ── Cache incrémental : clé = "billing_date|provider_ref" ────────────────
        # On ne télécharge que les PDFs non encore en cache — comme OSPHARM.
        _cache_in  = _run_scraper_async._invoice_cache  # dict passé via attribut
        _cache_out = {}                                  # cache mis à jour cette run
        _on_progress = _run_scraper_async._on_progress  # callback enrichi (pour SIGTERM)

        n_cached   = sum(1 for inv in invoices
                         if _inv_key(inv) in _cache_in)
        n_new      = len(invoices) - n_cached
        progress(f"{len(invoices)} factures ({n_cached} en cache, {n_new} à télécharger)…")

        all_lines = []
        _t0_loop  = time.time()
        _MAX_LOOP = 50 * 60  # budget 50 min pour les PDFs nouveaux

        for i, inv in enumerate(invoices, 1):
            provider     = inv.get("provider_ref") or inv.get("provider_name") or "?"
            billing_date = inv.get("billing_date", "?")
            ck           = _inv_key(inv)

            if ck in _cache_in:
                # Facture déjà traitée — marquée True dans le cache compact.
                # Ses lignes sont déjà intégrées dans digi_month_stats existant,
                # on ne les réintègre pas dans all_lines pour éviter les doublons.
                _cache_out[ck] = True
            else:
                # Timeout enveloppe : download(35s) + extraction(25s) + marge
                try:
                    lines = await asyncio.wait_for(_process_pdf(page, inv), timeout=70)
                except asyncio.TimeoutError:
                    progress(f"PDF {i}/{len(invoices)} — {provider} TIMEOUT 70s, ignoré")
                    lines = []
                _cache_out[ck] = lines
                if lines:
                    labo = lines[0].get("labo", "")
                    progress(f"PDF {i}/{len(invoices)} — {provider} ({billing_date}) → {len(lines)} lignes [{labo}]")
                else:
                    progress(f"PDF {i}/{len(invoices)} — {provider} ({billing_date}) → 0 lignes")
                await page.wait_for_timeout(150)

            all_lines.extend(lines)
            if _on_progress:
                _on_progress(all_lines, _cache_out)  # mise à jour pour SIGTERM

            if time.time() - _t0_loop > _MAX_LOOP:
                progress(f"⚠ Budget 50 min atteint à PDF {i}/{len(invoices)} — sauvegarde partielle")
                break

        # Synthèse par labo
        labos_count: dict[str, int] = {}
        for l in all_lines:
            k = l.get("labo") or l.get("fournisseur") or "?"
            labos_count[k] = labos_count.get(k, 0) + 1
        progress(f"Extraction terminée — {len(all_lines)} lignes : {labos_count}")

    return all_lines, _cache_out


def _inv_key(inv: dict) -> str:
    """Clé stable pour une facture : date + fournisseur."""
    return f"{inv.get('billing_date','?')}|{inv.get('provider_ref') or inv.get('provider_name','?')}"


def run_scraper(creds: dict, progress: Callable,
                invoice_cache: dict = None,
                on_partial: Callable = None) -> tuple[list, dict]:
    """Retourne (lignes, cache_mis_à_jour). on_partial(lines, cache) est appelé après chaque PDF."""
    _run_scraper_async._invoice_cache = invoice_cache or {}
    _run_scraper_async._on_progress   = on_partial
    return asyncio.run(_run_scraper_async(creds, progress))
