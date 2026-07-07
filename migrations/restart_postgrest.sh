#!/usr/bin/env bash
# =============================================================================
#  restart_postgrest.sh — relance PostgREST (API REST Supabase self-hosted)
#  et le rend persistant aux reboots. À lancer sur le VPS Hetzner (root).
#
#  Usage (une seule ligne à taper dans la console Hetzner) :
#    curl -s https://raw.githubusercontent.com/onizard/pharmacie-remises/claude/confident-newton-0scy4n/migrations/restart_postgrest.sh | bash
# =============================================================================
set -uo pipefail
echo "=============================================================="
echo " Relance PostgREST — $(date)"
echo "=============================================================="

# 1) Trouver le conteneur PostgREST (par nom OU par image postgrest/*).
CID="$(docker ps -a --format '{{.ID}} {{.Names}} {{.Image}}' \
        | grep -iE 'postgrest|(^| )rest( |$)|/rest' | awk '{print $1}' | head -1)"
if [ -z "$CID" ]; then
  CID="$(docker ps -a --filter ancestor=postgrest/postgrest -q | head -1)"
fi

if [ -z "$CID" ]; then
  echo "!! Conteneur PostgREST introuvable. Conteneurs présents :"
  docker ps -a --format '   {{.Names}}\t{{.Status}}\t{{.Image}}'
  echo
  echo ">> Si tu utilises docker-compose, lance plutôt, depuis le dossier du compose :"
  echo "     docker compose up -d rest   (ou: docker compose up -d)"
  exit 1
fi

NAME="$(docker inspect --format '{{.Name}}' "$CID" | sed 's#^/##')"
echo "→ Conteneur PostgREST : $NAME ($CID)"

# 2) (Re)démarrer.
echo "→ Redémarrage…"
docker restart "$CID" >/dev/null 2>&1 || docker start "$CID" >/dev/null 2>&1

# 3) Politique de redémarrage automatique (survit aux reboots).
docker update --restart unless-stopped "$CID" >/dev/null 2>&1 \
  && echo "→ restart policy = unless-stopped (repartira tout seul au prochain reboot)"

# 4) Vérifier qu'il tient debout.
sleep 4
STATUS="$(docker inspect --format '{{.State.Status}}' "$CID" 2>/dev/null)"
echo "→ État : $STATUS"
if [ "$STATUS" != "running" ]; then
  echo "!! PostgREST ne reste pas debout. 30 dernières lignes de log :"
  docker logs --tail 30 "$CID" 2>&1 | sed 's/^/   /'
  echo
  echo "   (Souvent : Postgres pas encore prêt, ou variable PGRST_DB_URI/JWT.)"
  exit 1
fi

# 5) Test local de l'API (port 3000 exposé en interne).
echo "→ Test local http://127.0.0.1:3000/ :"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:3000/ 2>/dev/null)"
echo "   HTTP $code  (200/400/401 = PostgREST répond ; 000 = pas encore prêt)"

echo
echo "✅ Terminé. Attends ~15 s puis recharge break-pharma.fr."
echo "   (Claude peut confirmer côté public : https://api.break-pharma.fr/rest/v1/)"
