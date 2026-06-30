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


# Labs cibles pour la recherche "par labo" — presta coop (Movianto/Alloga facture au nom du labo)
PRESTA_SEARCH_LABS = [
    "biogaran", "teva", "viatris", "mylan", "sandoz", "zentiva",
    "arrow", "cristers", "evolupharm", "zydus", "eg labo",
    "movianto", "alloga", "cegedim",
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

    parsed_ep   = urlparse(clean_url)
    endpoint_base = f"{parsed_ep.scheme}://{parsed_ep.netloc}{parsed_ep.path}"

    invoices = await _paginate_fetch(page, clean_url, csrf, progress)
    return invoices, endpoint_base, csrf


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
    # Playwright limite la taille des réponses dans page.evaluate.
    # Pour les grandes réponses on chunke en JS et on réassemble côté Python.
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
            if (!resp.ok) return { status: resp.status, text: '' };
            // Lire le corps en ArrayBuffer pour éviter la limite Content-Length
            const buf = await resp.arrayBuffer();
            const decoder = new TextDecoder('utf-8');
            return { status: resp.status, text: decoder.decode(buf) };
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
    content = b""
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

    # Fallback : téléchargement via le contexte navigateur (pour les URLs auth-protégées)
    if len(content) < 500 or not content.startswith(b"%PDF"):
        try:
            csrf = await _get_csrf(page)
            result = await page.evaluate("""async ([url, csrf]) => {
                const r = await fetch(url, {
                    credentials: 'include',
                    headers: {'X-CSRFToken': csrf}
                });
                if (!r.ok) return null;
                const ab = await r.arrayBuffer();
                const bytes = new Uint8Array(ab);
                let bin = '';
                for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                return btoa(bin);
            }""", [file_url, csrf])
            if result:
                content = base64.b64decode(result)
                if _os.environ.get("PDF_DEBUG") == "1":
                    print(f"[PDF] Fallback browser OK : {len(content)} bytes", flush=True)
        except Exception as _fb_e:
            if _os.environ.get("PDF_DEBUG") == "1":
                print(f"[PDF] Fallback browser échoué : {_fb_e}", flush=True)

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

_camoufox_ready = False

async def _ensure_camoufox():
    global _camoufox_ready
    if _camoufox_ready:
        return
    import subprocess, sys
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "camoufox", "fetch",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    _camoufox_ready = True


async def _run_scraper_async(creds: dict, progress: Callable) -> list[dict]:
    from camoufox.async_api import AsyncCamoufox
    await _ensure_camoufox()

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

        invoices, _inv_endpoint, _csrf = await _fetch_invoices(page, progress)
        if not invoices:
            progress("Aucune facture générique trouvée")

        # Appliquer le filtre labo si défini (ex: LABS_FILTER=biogaran)
        if LABS_FILTER and invoices:
            filtered = [inv for inv in invoices
                        if LABS_FILTER in (inv.get("provider_ref") or inv.get("provider_name") or "").lower()]
            progress(f"{len(invoices)} factures → {len(filtered)} après filtre '{LABS_FILTER}'")
            invoices = filtered

        # ── Cache incrémental : clé = "billing_date|provider_ref" ────────────────
        # On ne télécharge que les PDFs non encore en cache — comme OSPHARM.
        _cache_in  = _run_scraper_async._invoice_cache  # dict passé via attribut
        _cache_out = {}                                  # cache mis à jour cette run
        _on_progress = _run_scraper_async._on_progress  # callback enrichi (pour SIGTERM)

        # Recherche presta coop "par labo" — complète la liste standard
        _known_keys = {_inv_key(inv) for inv in invoices}
        _presta_extra = await _search_presta_by_lab(
            page, _inv_endpoint, _csrf, _known_keys, progress
        )
        invoices = invoices + _presta_extra

        n_cached   = sum(1 for inv in invoices
                         if _inv_key(inv) in _cache_in)
        n_new      = len(invoices) - n_cached
        progress(f"{len(invoices)} factures total ({n_cached} en cache, {n_new} à télécharger)…")

        all_lines = []
        _t0_loop  = time.time()
        _MAX_LOOP = 50 * 60  # budget 50 min pour les PDFs nouveaux

        for i, inv in enumerate(invoices, 1):
            provider     = inv.get("provider_ref") or inv.get("provider_name") or "?"
            billing_date = inv.get("billing_date", "?")
            ck           = _inv_key(inv)
            lines        = []  # reset pour chaque itération — évite la fuite de valeur entre itérations

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

        # ── GED : documents MDL (CERP Marge de Distribution en Licence) ────────
        ged_docs = await _fetch_ged_documents(page, progress)
        for i, doc in enumerate(ged_docs, 1):
            ck   = f"ged:{_inv_key(doc)}"
            date = doc.get("billing_date", "?")
            lines = []
            if ck in _cache_in:
                _cache_out[ck] = True
            else:
                try:
                    lines = await asyncio.wait_for(_process_pdf(page, doc), timeout=70)
                except asyncio.TimeoutError:
                    progress(f"GED {i}/{len(ged_docs)} — MDL {date} TIMEOUT 70s, ignoré")
                _cache_out[ck] = lines
                if lines:
                    progress(f"GED {i}/{len(ged_docs)} — MDL {date} → {len(lines)} lignes")
                else:
                    progress(f"GED {i}/{len(ged_docs)} — MDL {date} → 0 lignes")
                await page.wait_for_timeout(150)
            all_lines.extend(lines)
            if _on_progress:
                _on_progress(all_lines, _cache_out)

        # Synthèse par labo
        labos_count: dict[str, int] = {}
        for l in all_lines:
            k = l.get("labo") or l.get("fournisseur") or "?"
            labos_count[k] = labos_count.get(k, 0) + 1
        progress(f"Extraction terminée — {len(all_lines)} lignes : {labos_count}")

    return all_lines, _cache_out


async def _search_presta_by_lab(
    page,
    endpoint_base: str,
    csrf: str,
    known_keys: set,
    progress: Callable,
) -> list[dict]:
    """
    Navigation UI Digi — pour chaque labo cible :
      1. Navigue vers /factures/ avec filtre fournisseur (URL param ou interaction DOM)
      2. Boucle "Charger plus" jusqu'à épuisement de la liste
      3. Intercepte les réponses API JSON pour récupérer les factures (RSF/RDP/presta)
    Le PDF est ensuite téléchargé + classifié par _process_pdf comme pour les factures normales.
    Fallback : requête directe API avec param URL si la navigation ne capture rien.
    """
    from urllib.parse import quote_plus as _qp

    _CHARGER_PLUS = ["Charger plus", "Load more", "Voir plus", "Voir tout"]
    _FOURN_SELS   = [
        "select[name*='fourn' i]", "select[id*='fourn' i]",
        "select[aria-label*='ournisseur' i]",
        "input[placeholder*='ournisseur' i]",
        "[data-filter*='fourn' i]",
        ".filter-fournisseur select", "#id_fournisseur",
    ]
    _INV_KWS = ("invoice", "facture", "bill", "document")

    progress("Recherche factures par fournisseur…")
    extra: list[dict] = []

    # ── Diagnostic page courante (une seule fois) ──────────────────────────────
    try:
        _diag = await page.evaluate("""() => {
            const all_links = Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({href:(a.href||'').replace(location.origin,''), text:(a.textContent||'').trim().slice(0,60)}))
                .filter(l => l.href && l.href.startsWith('/'));
            const fourn_links = all_links.filter(l =>
                /fourn|provider|supplier|labo|fourniss/i.test(l.href+l.text));
            const selects = Array.from(document.querySelectorAll('select'))
                .map(s => ({
                    sel: (s.name||s.id||s.className||'?').slice(0,40),
                    opts: Array.from(s.options).slice(0,8).map(o=>o.text.trim())
                }));
            const inputs = Array.from(document.querySelectorAll('input'))
                .filter(i => /fourn|search|filtr|provider|labo/i.test(i.name+i.id+i.placeholder+i.className))
                .map(i => ({name:(i.name||i.id||i.placeholder||'?').slice(0,40), type:i.type}));
            return {url: location.pathname, fourn_links: fourn_links.slice(0,10),
                    all_links_sample: all_links.slice(0,15), selects, inputs};
        }""")
        progress(f"[DIAG] URL courante : {_diag.get('url')}")
        progress(f"[DIAG] Liens fourn  : {_diag.get('fourn_links')}")
        progress(f"[DIAG] Liens (15)   : {_diag.get('all_links_sample')}")
        progress(f"[DIAG] Selects      : {_diag.get('selects')}")
        progress(f"[DIAG] Inputs fourn : {_diag.get('inputs')}")
    except Exception as _de:
        progress(f"[DIAG] Erreur : {_de}")

    # ── Découverte de la page fournisseur ──────────────────────────────────────
    # Chercher un lien "Fournisseur(s)" dans la navigation
    _fourn_page_url: str | None = None
    try:
        _fourn_candidates = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('a[href]'))
                .filter(a => /fourn|provider|supplier/i.test((a.textContent||'')+(a.href||'')))
                .map(a => a.href)
                .filter(h => h.startsWith(location.origin))
                .slice(0, 5)
        """)
        if _fourn_candidates:
            _fourn_page_url = _fourn_candidates[0]
            progress(f"[DIAG] Page fournisseur candidate : {_fourn_page_url}")
    except Exception:
        pass

    # Si une page fournisseurs existe, naviguer et lister les labos disponibles
    if _fourn_page_url:
        try:
            await page.goto(_fourn_page_url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(3000)
            _fourn_list = await page.evaluate("""() =>
                Array.from(document.querySelectorAll('a[href]'))
                    .map(a => ({href: a.href.replace(location.origin,''), text:(a.textContent||'').trim().slice(0,60)}))
                    .filter(l => l.href.length > 1)
                    .slice(0, 30)
            """)
            progress(f"[DIAG] Page fournisseur — liens ({len(_fourn_list)}) : {_fourn_list[:10]}")
        except Exception as _fe:
            progress(f"[DIAG] Page fournisseur erreur : {_fe}")
        # Revenir sur /factures/ pour la suite
        try:
            await page.goto(f"{BASE_URL}/factures/", wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(2000)
        except Exception:
            pass

    for lab in PRESTA_SEARCH_LABS:
        progress(f"  Fournisseur : {lab}…")
        _lab_resps: list = []

        # Closure avec default-arg pour capturer la liste courante (pas la variable rebindée)
        def _capture(r, _store=_lab_resps):
            ct = r.headers.get("content-type", "")
            if ("json" in ct and r.status == 200 and BASE_URL in r.url and
                    any(kw in r.url.lower() for kw in _INV_KWS)):
                _store.append(r)

        page.on("response", _capture)
        try:
            # ── Étape 1 : navigation avec paramètre fournisseur dans l'URL ──────
            nav_ok = False
            for url_tpl in [
                f"{BASE_URL}/factures/?fournisseur={_qp(lab)}",
                f"{BASE_URL}/factures/?search={_qp(lab)}",
                f"{BASE_URL}/factures/?provider={_qp(lab)}",
                # variantes avec section dédiée
                f"{BASE_URL}/fournisseurs/{_qp(lab)}/factures/",
                f"{BASE_URL}/fournisseurs/?name={_qp(lab)}",
            ]:
                try:
                    await page.goto(url_tpl, wait_until="domcontentloaded", timeout=25_000)
                    await page.wait_for_timeout(4000)
                    nav_ok = True
                    if _lab_resps:
                        break  # URL param fonctionne → pas besoin d'interaction DOM
                except Exception:
                    pass

            # ── Étape 2 : si aucune réponse, chercher le filtre Fournisseur dans le DOM ─
            if not _lab_resps:
                if not nav_ok:
                    try:
                        await page.goto(f"{BASE_URL}/factures/", wait_until="load", timeout=30_000)
                        await page.wait_for_timeout(4000)
                    except Exception:
                        pass

                for fourn_sel in _FOURN_SELS:
                    el = page.locator(fourn_sel).first
                    if not await el.count():
                        continue
                    try:
                        tag = await el.evaluate("e => e.tagName.toLowerCase()")
                        if tag == "select":
                            opts = await el.evaluate(
                                "e => Array.from(e.options).map(o => ({v:o.value,t:o.text.toLowerCase()}))"
                            )
                            match = next((o["v"] for o in opts if lab in o["t"]), None)
                            if match:
                                await el.select_option(value=match)
                                await page.wait_for_timeout(3500)
                                break
                        else:
                            await el.fill(lab)
                            await el.press("Enter")
                            await page.wait_for_timeout(3500)
                            break
                    except Exception:
                        continue

            # ── Étape 3 : boucle "Charger plus" ─────────────────────────────────
            for _ in range(15):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)
                clicked = False
                for txt in _CHARGER_PLUS:
                    btn = page.locator(
                        f"button:has-text('{txt}'), a:has-text('{txt}'), span:has-text('{txt}')"
                    ).first
                    if await btn.count():
                        try:
                            await btn.click(timeout=5000)
                        except Exception:
                            await btn.evaluate("el => el.click()")
                        await page.wait_for_timeout(3000)
                        clicked = True
                        break
                if not clicked:
                    break

            # ── Étape 4 : traiter les réponses capturées ─────────────────────────
            n_new = 0
            for r in _lab_resps:
                try:
                    data    = await r.json()
                    results = data.get("results", data if isinstance(data, list) else [])
                    for inv in results:
                        date = str(inv.get("billing_date", ""))
                        if date[:4] not in YEARS:
                            continue
                        ck = _inv_key(inv)
                        if ck not in known_keys:
                            extra.append(inv)
                            known_keys.add(ck)
                            n_new += 1
                except Exception:
                    pass

            # ── Fallback : requête API directe si navigation sans résultat ────────
            if not n_new and not _lab_resps:
                for param in ["search", "q", "fournisseur", "provider_ref"]:
                    furl = f"{endpoint_base}?{param}={_qp(lab)}&page_size={PAGE_SIZE}&ordering=-billing_date"
                    try:
                        data    = await _js_fetch_json(page, furl, csrf)
                        results = data.get("results", data if isinstance(data, list) else [])
                        for inv in results:
                            date = str(inv.get("billing_date", ""))
                            if date[:4] not in YEARS:
                                continue
                            ck = _inv_key(inv)
                            if ck not in known_keys:
                                extra.append(inv)
                                known_keys.add(ck)
                                n_new += 1
                        if n_new:
                            progress(f"    [API ?{param}] {lab} → {n_new} nouvelle(s)")
                            break
                    except Exception:
                        continue

            if n_new:
                progress(f"  {lab} → {n_new} nouvelle(s) facture(s)")

        except Exception as e:
            progress(f"  {lab} → erreur : {e}")
        finally:
            page.remove_listener("response", _capture)

        await page.wait_for_timeout(500)

    progress(f"Navigation fournisseur : {len(extra)} facture(s) supplémentaire(s)")
    return extra


async def _fetch_ged_documents(page, progress: Callable) -> list[dict]:
    """
    Cherche les documents MDL dans la section GED de Digipharmacie.
    Retourne une liste de dicts compatibles avec _process_pdf :
      {file, provider_ref, provider_name, billing_date}
    """
    MDL_KEYWORDS = ("mdl", "marge dépositaire", "marge depositaire",
                    "marché de distribution", "marche de distribution",
                    "smr générique", "smr generique")
    GED_NAV_KW   = ("ged", "document", "biblioth", "ressource", "fichier", "media")
    MONTH_MAP    = {
        "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    }

    progress("Recherche documents GED (MDL CERP)…")

    # ── Phase 1 : chercher un lien GED dans la navigation de la page courante ──
    ged_url = None
    try:
        nav_links = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({href: a.href || '', text: (a.textContent||'').trim().toLowerCase()}))
                .filter(l => l.href && l.href.startsWith('http'))
        """)
        for link in nav_links:
            h = link.get("href", "").lower()
            t = link.get("text", "")
            if any(kw in h or kw in t for kw in GED_NAV_KW):
                ged_url = link["href"]
                progress(f"Lien GED trouvé : {link['href']!r} ({link['text']!r})")
                break
    except Exception:
        pass

    # ── Phase 2 : essayer les URLs candidates si aucun lien trouvé ────────────
    if not ged_url:
        for path in ("/ged/", "/documents/", "/bibliotheque/", "/ressources/", "/fichiers/"):
            try:
                r = await page.goto(f"{BASE_URL}{path}", wait_until="load", timeout=20_000)
                if r and r.status < 400 and "/login" not in page.url:
                    ged_url = page.url
                    progress(f"GED accessible via {path}")
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

    if not ged_url:
        progress("Section GED introuvable — documents MDL à déposer manuellement")
        return []

    # ── Phase 3 : naviguer et capturer les réponses API ───────────────────────
    _caps: list[tuple[str, object]] = []

    def _on_resp(response):
        ct = response.headers.get("content-type", "")
        if "json" in ct and response.status == 200 and BASE_URL in response.url:
            _caps.append((response.url, response))

    page.on("response", _on_resp)
    if ged_url != page.url:
        try:
            await page.goto(ged_url, wait_until="load", timeout=30_000)
        except Exception:
            pass
    await page.wait_for_timeout(4000)

    # Scroll pour charger plus si pagination lazy
    try:
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
    except Exception:
        pass
    page.remove_listener("response", _on_resp)

    # ── Phase 4 : identifier l'endpoint document dans les réponses capturées ──
    DOC_PATH_KW = ("document", "ged", "file", "biblio", "ressource", "media", "upload",
                   "attachment", "piece", "pièce")
    csrf = await _get_csrf(page)

    doc_results = []
    seen_endpoints: set = set()
    for url, _resp in _caps:
        parsed_path = urlparse(url).path.lower()
        if not any(kw in parsed_path for kw in DOC_PATH_KW):
            continue
        if url in seen_endpoints:
            continue
        seen_endpoints.add(url)
        try:
            data  = await _js_fetch_json(page, url, csrf)
            items = data.get("results", data if isinstance(data, list) else [])
            if items and isinstance(items[0], dict):
                doc_results.extend(items)
                progress(f"GED endpoint : {urlparse(url).path} → {len(items)} docs")
        except Exception as _e:
            progress(f"GED endpoint {urlparse(url).path} ignoré : {_e}")
            continue

    # ── Phase 5 : fallback — appel direct aux endpoints API connus ────────────
    if not doc_results:
        progress("Pas de réponse capturée — tentative endpoints API directs…")
        for ep in ("/api/v1/documents/", "/api/v1/ged/", "/api/v1/files/",
                   "/api/v1/bibliotheque/", "/api/v1/media/"):
            try:
                data  = await _js_fetch_json(
                    page,
                    f"{BASE_URL}{ep}?page_size=100&ordering=-date",
                    csrf,
                )
                items = data.get("results", data if isinstance(data, list) else [])
                if items and isinstance(items[0], dict):
                    doc_results.extend(items)
                    progress(f"GED fallback {ep} → {len(items)} docs")
                    break
            except Exception:
                continue

    if not doc_results:
        progress("GED accessible mais aucun document récupéré")
        return []

    # ── Phase 6 : filtrer les documents MDL ───────────────────────────────────
    def _doc_text(d: dict) -> str:
        return " ".join(str(v) for v in d.values() if isinstance(v, str)).lower()

    mdl_docs = [d for d in doc_results if any(kw in _doc_text(d) for kw in MDL_KEYWORDS)]

    if not mdl_docs:
        progress(f"GED : {len(doc_results)} docs total, aucun MDL (mots-clés : {MDL_KEYWORDS})")
        return []

    progress(f"GED : {len(mdl_docs)} document(s) MDL trouvé(s)")

    # ── Phase 7 : normaliser en format compatible avec _process_pdf ───────────
    normalized = []
    for doc in mdl_docs:
        file_url = (doc.get("file") or doc.get("file_url") or
                    doc.get("url") or doc.get("download_url") or
                    doc.get("attachment") or doc.get("path") or "")
        if not file_url:
            continue
        # Rendre l'URL absolue si relative
        if file_url.startswith("/"):
            file_url = f"{BASE_URL}{file_url}"

        # Extraire la date depuis le titre ("MDL Février 2026" → "2026-02-01")
        title = str(doc.get("title") or doc.get("name") or doc.get("filename") or
                    doc.get("label") or "")
        billing_date = (doc.get("billing_date") or doc.get("date") or
                        doc.get("created_at") or "")
        if not billing_date or len(billing_date) < 7:
            m_year = re.search(r"(20\d{2})", title)
            year   = m_year.group(1) if m_year else ""
            month_num = 0
            for word, num in MONTH_MAP.items():
                if word in title.lower():
                    month_num = num
                    break
            if year and month_num:
                billing_date = f"{year}-{month_num:02d}-01"
            elif billing_date and len(billing_date) >= 7:
                billing_date = billing_date[:7] + "-01"
            else:
                billing_date = ""

        if not billing_date:
            progress(f"GED MDL : date non trouvée pour {title!r} — ignoré")
            continue

        normalized.append({
            "file":          file_url,
            "provider_ref":  "CERP",
            "provider_name": "CERP",
            "billing_date":  billing_date,
        })

    return normalized


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
