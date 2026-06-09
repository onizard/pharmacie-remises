"""
explore_espace_client.py — Explore la section Achats > Espaces clients de Digipharmacie.
Intercepte toutes les requêtes API et sauvegarde les résultats dans la base NAS.

Usage : python3 api_scraper/explore_espace_client.py
Env requis : USER_ID, SUPABASE_SERVICE_KEY
"""

import asyncio
import json
import os
import urllib.request

SUPA_URL    = "https://api.break-pharma.fr"
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
USER_ID     = os.environ.get("USER_ID", "")
PROXY_URL   = os.environ.get("PROXY_URL", "")
BASE_URL    = "https://app.digipharmacie.fr"


def _get_creds() -> dict:
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}&select=connectors&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    conns = (rows[0].get("connectors") or {}) if rows else {}
    cred  = conns.get("digipharmacie", {})
    return {"user": cred.get("user", ""), "pass": cred.get("pass", "")}


def _save_result(result: dict):
    url  = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}&select=state_json&limit=1"
    req  = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    state = rows[0]["state_json"] if rows else {}
    state["digi_espace_client_explore"] = result
    body = json.dumps({"state_json": state}).encode()
    req2 = urllib.request.Request(
        f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}",
        data=body, method="PATCH",
        headers={
            "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal",
        },
    )
    with urllib.request.urlopen(req2, timeout=15): pass
    print("  ✅ Résultats sauvegardés dans state_json.digi_espace_client_explore")


async def _explore(creds: dict):
    from playwright.async_api import async_playwright

    api_calls = []   # toutes les requêtes /api/*
    pages_visited = []

    proxy_cfg = None
    if PROXY_URL:
        import urllib.parse as _up
        _p = _up.urlparse(PROXY_URL)
        proxy_cfg = {
            "server":   f"{_p.scheme}://{_p.hostname}:{_p.port}",
            "username": _p.username or "",
            "password": _p.password or "",
        }

    _ARGS = [
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        "--disable-extensions", "--disable-background-networking",
        "--mute-audio", "--no-first-run",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_ARGS, proxy=proxy_cfg)
        context = await browser.new_context()

        # Intercepte toutes les réponses JSON des appels API
        async def on_response(response):
            url = response.url
            if "/api/" in url and response.status < 400:
                try:
                    body = await response.json()
                    api_calls.append({
                        "url":    url,
                        "status": response.status,
                        "body":   body if not isinstance(body, list) else body[:3],
                        "count":  len(body) if isinstance(body, list) else None,
                    })
                    print(f"  API {response.status} {url.split(BASE_URL)[-1][:80]}")
                except Exception:
                    pass

        page = await context.new_page()
        page.on("response", on_response)

        # ── Login ──────────────────────────────────────────────────────────────
        print("→ Login...")
        await page.goto(f"{BASE_URL}/login/", timeout=30000)
        await page.wait_for_selector("input[type=email], input[name=email]", timeout=10000)
        await page.fill("input[type=email], input[name=email]", creds["user"])
        await page.fill("input[type=password]", creds["pass"])
        await page.keyboard.press("Enter")
        await page.wait_for_url(f"**{BASE_URL}/**", timeout=20000)
        print(f"  Connecté : {page.url}")
        pages_visited.append({"label": "login", "url": page.url})

        # ── Navigate to Achats ─────────────────────────────────────────────────
        print("→ Navigation vers /achat/...")
        await page.goto(f"{BASE_URL}/achat/", timeout=20000)
        await page.wait_for_load_state("networkidle", timeout=10000)
        pages_visited.append({"label": "achat", "url": page.url})

        # Chercher le lien "Espaces clients"
        link = page.locator("a:has-text('Espaces clients'), a:has-text('espace client'), a[href*='espace']")
        count = await link.count()
        print(f"  Liens 'espaces clients' trouvés : {count}")

        espace_url = None
        if count > 0:
            href = await link.first.get_attribute("href")
            print(f"  Href: {href}")
            espace_url = href if href and href.startswith("http") else f"{BASE_URL}{href}"
        else:
            # Essai direct
            espace_url = f"{BASE_URL}/achat/espaces-clients/"

        # ── Navigate to Espaces clients ────────────────────────────────────────
        print(f"→ Navigation vers {espace_url}...")
        await page.goto(espace_url, timeout=20000)
        await page.wait_for_load_state("networkidle", timeout=10000)
        pages_visited.append({"label": "espaces_clients", "url": page.url})

        # Screenshot
        await page.screenshot(path="/tmp/digi_espace_client.png", full_page=True)
        print("  Screenshot: /tmp/digi_espace_client.png")

        # Récupérer le HTML de la page
        html = await page.content()

        # Chercher les liens dans la section
        all_links = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('a')).map(a => ({
                text: a.textContent.trim().slice(0, 80),
                href: a.href
            })).filter(a => a.text && a.href.includes('digipharmacie'))
        """)

        # Chercher les éléments qui ressemblent à des connecteurs/tokens
        connectors_html = await page.evaluate("""() => {
            const kw = ['connecteur', 'token', 'api', 'clé', 'key', 'intégration', 'lien'];
            const els = Array.from(document.querySelectorAll('*'));
            return els
                .filter(e => kw.some(k => e.textContent.toLowerCase().includes(k)) && e.children.length < 5)
                .map(e => e.outerHTML.slice(0, 300))
                .slice(0, 20);
        }""")

        await context.close()
        await browser.close()

    return {
        "pages_visited": pages_visited,
        "api_calls":     api_calls,
        "links":         all_links[:30],
        "connectors_html": connectors_html,
        "html_snippet":  html[html.find("<main"):html.find("<main") + 3000] if "<main" in html else html[:3000],
    }


def main():
    print("🔍  Exploration Digipharmacie — Achats > Espaces clients")
    creds = _get_creds()
    if not creds["user"]:
        print("❌  Pas de credentials Digipharmacie en base")
        return

    print(f"  User: {creds['user']}")
    result = asyncio.run(_explore(creds))

    print("\n── Résumé ──────────────────────────────────────────────────────")
    print(f"  Pages visitées : {[p['label'] for p in result['pages_visited']]}")
    print(f"  Appels API interceptés : {len(result['api_calls'])}")
    for c in result["api_calls"]:
        print(f"    {c['status']} {c['url'].replace(BASE_URL, '')} (count={c['count']})")
    print(f"  Liens : {len(result['links'])}")
    print(f"  Éléments connecteur/token : {len(result['connectors_html'])}")

    _save_result(result)
    print("\n  JSON complet dans state_json.digi_espace_client_explore")


if __name__ == "__main__":
    main()
