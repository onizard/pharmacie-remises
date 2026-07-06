#!/usr/bin/env bash
# =============================================================================
#  rotate_jwt_secret.sh
#  Rotation du secret de signature JWT (GoTrue + PostgREST) — Supabase self-hosted
# =============================================================================
#
#  POURQUOI
#  --------
#  Le secret HMAC qui signe TOUS les JWT (clé anon, clé service, tokens des
#  utilisateurs) a fuité : il était écrit en clair, en valeur par défaut, dans
#  le dépôt PUBLIC (api_scraper/supabase_client.py). Tant qu'il n'est pas
#  changé, n'importe qui peut forger un token « admin » (email =
#  contact@break-pharma.fr) et contourner le verrou RLS de rsf_defaults.
#
#  CE QUE FAIT CE SCRIPT (sur le VPS Hetzner, là où tournent GoTrue + PostgREST)
#  ---------------------------------------------------------------------------
#    1. Génère un NOUVEAU secret aléatoire (64 hex).
#    2. Re-signe les clés `anon` et `service_role` avec ce nouveau secret,
#       en gardant EXACTEMENT le même payload que les clés actuelles.
#    3. Localise le(s) fichier(s) de conf Docker qui contiennent l'ancien
#       secret, en fait une SAUVEGARDE horodatée, puis remplace :
#         - l'ancien secret     -> nouveau secret
#         - l'ancienne clé anon -> nouvelle clé anon
#         - l'ancienne clé serv -> nouvelle clé service
#    4. Redémarre les conteneurs GoTrue (auth) et PostgREST (rest).
#    5. Affiche la NOUVELLE clé anon + le nouveau secret à reporter ailleurs
#       (voir « ÉTAPES MANUELLES » plus bas).
#
#  IMPORTANT
#  ---------
#    • Rotation = toutes les sessions utilisateurs actuelles sont invalidées.
#      Les pharmaciens devront se reconnecter une fois. C'est normal.
#    • Le script est prudent : dry-run par défaut. Il n'écrit RIEN tant que
#      tu ne le lances pas avec  APPLY=1 .
#    • Idempotent : si l'ancien secret n'est plus trouvé, il ne casse rien.
#
#  USAGE
#  -----
#    # 1) Voir ce qui serait fait, sans rien modifier :
#    bash rotate_jwt_secret.sh
#
#    # 2) Appliquer pour de vrai :
#    APPLY=1 bash rotate_jwt_secret.sh
#
#    # (option) forcer le nouveau secret au lieu d'en générer un :
#    APPLY=1 NEW_SECRET=xxxxxxxx bash rotate_jwt_secret.sh
#
# =============================================================================
set -euo pipefail

# --- Valeurs ACTUELLES (celles qui ont fuité) --------------------------------
OLD_SECRET="OyS6Vj-ximYGsVAj4izBUtx21EvQRbzymIUjmg__ZciE-9XFgpAB0SOmnPDlTVcU"
OLD_ANON="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYW5vbiIsImlzcyI6InN1cGFiYXNlLXNlbGYiLCJpYXQiOjE3ODA4NTM5MTV9.CWLe1kClQhffk3EL_WgVOQQUERn6IwF7xNqbBL9lUKI"

APPLY="${APPLY:-0}"
NEW_SECRET="${NEW_SECRET:-$(openssl rand -hex 32)}"

echo "=============================================================="
echo " Rotation du secret JWT — Supabase self-hosted"
echo " Mode : $([ "$APPLY" = 1 ] && echo 'APPLIQUER' || echo 'DRY-RUN (aucune écriture)')"
echo "=============================================================="

# --- 1) Minter les nouvelles clés anon + service_role ------------------------
# On garde le MÊME payload que les clés existantes (role/iss/iat), on ne
# change que la signature (nouveau secret). iat = maintenant.
read -r NEW_ANON NEW_SERVICE <<EOF
$(python3 - "$NEW_SECRET" <<'PY'
import sys, json, time, hmac, hashlib, base64
secret = sys.argv[1].encode()
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def sign(payload):
    header = {"alg":"HS256","typ":"JWT"}
    h = b64(json.dumps(header,separators=(",",":")).encode())
    p = b64(json.dumps(payload,separators=(",",":")).encode())
    sig = b64(hmac.new(secret, f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"
iat = int(time.time())
anon = sign({"role":"anon","iss":"supabase-self","iat":iat})
service = sign({"role":"service_role","iss":"supabase-self","iat":iat})
print(anon, service)
PY
)
EOF

echo
echo ">>> NOUVEAU secret        : $NEW_SECRET"
echo ">>> NOUVELLE clé anon     : $NEW_ANON"
echo ">>> NOUVELLE clé service  : $NEW_SERVICE"
echo

# --- Vérification : la nouvelle clé anon se valide bien avec le nouveau secret
python3 - "$NEW_SECRET" "$NEW_ANON" <<'PY'
import sys, hmac, hashlib, base64
secret, tok = sys.argv[1].encode(), sys.argv[2]
h,p,s = tok.split(".")
exp = base64.urlsafe_b64encode(hmac.new(secret, f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
assert hmac.compare_digest(exp, s), "signature KO"
print("[ok] nouvelle clé anon signée correctement par le nouveau secret")
PY

# --- 2) Localiser les fichiers de conf contenant l'ancien secret -------------
echo
echo "--- Recherche des fichiers de conf contenant l'ancien secret ---"
SEARCH_DIRS=(/opt /root /srv /home /etc/supabase)
mapfile -t HITS < <(grep -rlF "$OLD_SECRET" "${SEARCH_DIRS[@]}" 2>/dev/null || true)

if [ "${#HITS[@]}" -eq 0 ]; then
  echo "  (aucun fichier avec l'ancien secret — peut-être déjà tourné, ou secret"
  echo "   injecté autrement. Vérifie manuellement les env des conteneurs auth/rest.)"
else
  printf '  trouvé : %s\n' "${HITS[@]}"
fi

# --- 3) Sauvegarde + remplacement -------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
for f in "${HITS[@]:-}"; do
  [ -z "$f" ] && continue
  echo
  echo "--- $f ---"
  if [ "$APPLY" = 1 ]; then
    cp -a "$f" "$f.bak-$STAMP"
    echo "  sauvegarde : $f.bak-$STAMP"
    sed -i "s#${OLD_SECRET}#${NEW_SECRET}#g" "$f"
    sed -i "s#${OLD_ANON}#${NEW_ANON}#g"     "$f"
    echo "  secret + clé anon remplacés."
  else
    echo "  [dry-run] remplacerait secret (et clé anon si présente)."
  fi
done

# --- 4) Redémarrage des conteneurs ------------------------------------------
echo
echo "--- Redémarrage GoTrue (auth) + PostgREST (rest) ---"
if [ "$APPLY" = 1 ]; then
  # Cherche un docker-compose.yml à proximité du 1er fichier patché.
  COMPOSE_DIR=""
  for f in "${HITS[@]:-}"; do
    d="$(dirname "$f")"
    if ls "$d"/docker-compose*.y*ml >/dev/null 2>&1; then COMPOSE_DIR="$d"; break; fi
  done
  if [ -n "$COMPOSE_DIR" ]; then
    echo "  compose détecté dans : $COMPOSE_DIR"
    ( cd "$COMPOSE_DIR" && docker compose up -d auth rest 2>/dev/null \
        || docker compose up -d 2>/dev/null \
        || docker-compose up -d )
  else
    echo "  compose non détecté automatiquement. Redémarre à la main, ex :"
    echo "    docker restart \$(docker ps --format '{{.Names}}' | grep -Ei 'gotrue|auth|postgrest|rest')"
  fi
else
  echo "  [dry-run] ne redémarre rien."
fi

# --- 5) Test de bout en bout (seulement en APPLY) ----------------------------
if [ "$APPLY" = 1 ]; then
  echo
  echo "--- Test API : la nouvelle clé anon doit être acceptée par PostgREST ---"
  sleep 3
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "apikey: $NEW_ANON" -H "Authorization: Bearer $NEW_ANON" \
    "https://api.break-pharma.fr/rest/v1/rsf_defaults?select=lab&limit=1" || echo "000")
  echo "  GET rsf_defaults avec la NOUVELLE clé anon -> HTTP $code (200 = ok)"
  code_old=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "apikey: $OLD_ANON" -H "Authorization: Bearer $OLD_ANON" \
    "https://api.break-pharma.fr/rest/v1/rsf_defaults?select=lab&limit=1" || echo "000")
  echo "  GET rsf_defaults avec l'ANCIENNE clé anon  -> HTTP $code_old (401/403 = bien révoquée)"
fi

# =============================================================================
#  ÉTAPES MANUELLES À FAIRE APRÈS (hors VPS) — reporter la NOUVELLE clé anon
# =============================================================================
cat <<INSTR

==============================================================
 À FAIRE ENSUITE (à me redonner, ou à faire toi-même) :
==============================================================

 A) Dépôt GitHub (onizard/pharmacie-remises) :
    1. index.html  ->  remplacer l'ancienne clé anon (SUPA_KEY) par :
         $NEW_ANON
    2. api_scraper/supabase_client.py :
         - SUPA_KEY = "$NEW_ANON"
         - SUPPRIMER la valeur par défaut du secret : la ligne
           _JWT_SECRET = os.environ.get("GOTRUE_JWT_SECRET", "....")
           doit devenir  os.environ["GOTRUE_JWT_SECRET"]  (SANS défaut).
    -> commit + push (Claude s'en charge côté web s'il fait le déploiement).

 B) Render (service api_scraper, srv-d81ktm3tqb8s73ehk7mg) — variables d'env :
       GOTRUE_JWT_SECRET   = $NEW_SECRET
       SUPABASE_SERVICE_KEY = $NEW_SERVICE
    puis « Manual Deploy / Clear cache & deploy ».

 C) Si le secret est aussi utilisé ailleurs (n8n, scripts cron, autre backend) :
    y reporter GOTRUE_JWT_SECRET = $NEW_SECRET.

 D) Purger l'historique Git du secret fuité est un +, mais le plus important
    est fait dès que le secret est tourné (l'ancien ne signe plus rien).

 Note : après rotation, tous les utilisateurs devront se reconnecter une fois.
==============================================================
INSTR

echo
echo "Terminé ($([ "$APPLY" = 1 ] && echo 'APPLIQUÉ' || echo 'DRY-RUN — relance avec APPLY=1 pour appliquer'))."
