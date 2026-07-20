#!/usr/bin/env python3
"""
digi_camoufox_probe.py — Teste si camoufox (vrai navigateur furtif) franchit le
défi JavaScript Cloudflare de Digipharmacie, à travers un proxy SOCKS (= sortie
par l'IP résidentielle du NAS).

Contrairement à curl_cffi (qui n'imite que le TLS), camoufox exécute le JS du défi
« Just a moment… » et obtient le cookie cf_clearance — ce qui, combiné à une IP
résidentielle, doit passer là où une IP datacenter échoue.

Prérequis (sur le VPS, tunnel SOCKS déjà ouvert sur 127.0.0.1:1080) :
    pip install camoufox
    python3 -m camoufox fetch
Usage :
    PROXY=socks5://127.0.0.1:1080 python3 digi_camoufox_probe.py
(sans PROXY → test direct, pour comparer)
"""
import asyncio
import os
import sys

PROXY = os.environ.get("PROXY", "")
URL   = "https://app.digipharmacie.fr/login/"


async def main():
    from camoufox.async_api import AsyncCamoufox
    kw = {"headless": True}
    if PROXY:
        kw["proxy"] = {"server": PROXY}
    print(f"→ Lancement camoufox (proxy={PROXY or 'aucun — sortie directe VPS'})…")
    async with AsyncCamoufox(**kw) as browser:
        page = await browser.new_page()
        page.set_default_timeout(90_000)
        # IP de sortie réelle du navigateur (confirme qu'on passe bien par le NAS).
        try:
            await page.goto("https://api.ipify.org", wait_until="domcontentloaded", timeout=30_000)
            print(f"  IP de sortie du navigateur : {(await page.content())[:120]}")
        except Exception as e:
            print(f"  (ipify indisponible : {e})")

        print(f"→ Navigation vers {URL}…")
        await page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
        # Laisser le défi JS Cloudflare se résoudre (page « Just a moment… »).
        title = ""
        for i in range(8):
            await page.wait_for_timeout(3_000)
            title = (await page.title()) or ""
            if "just a moment" not in title.lower() and "moment" not in title.lower():
                break
            print(f"  … défi CF en cours ({i+1}/8) — titre={title!r}")

        title = (await page.title()) or ""
        html  = await page.content()
        try:
            cookies = await page.context.cookies()
        except Exception:
            cookies = []
        cf = [c for c in cookies if c.get("name") == "cf_clearance"]
        has_pwd = await page.locator("input[type='password']").count()

        print()
        print(f"Titre final                : {title!r}")
        print(f"Cookie cf_clearance        : {'OUI' if cf else 'non'}")
        print(f"Champ mot de passe présent : {'OUI (' + str(has_pwd) + ')' if has_pwd else 'non'}")

        blocked = "just a moment" in title.lower() or "just a moment" in html[:600].lower()
        if not blocked and (cf or has_pwd):
            print("\n\033[32m🎉 SUCCÈS — Cloudflare franchi : vrai navigateur + IP résidentielle.\033[0m")
            print("    → On peut industrialiser : le scraper camoufop (Render/GHA) route par cette sortie.")
            sys.exit(0)
        print("\n\033[31m❌ Toujours bloqué par Cloudflare (défi non résolu).\033[0m")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
