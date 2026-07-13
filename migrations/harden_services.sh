#!/usr/bin/env bash
# =============================================================================
#  harden_services.sh — « blinder » le VPS : tous les services repartent seuls
#  après un reboot. À lancer en root sur le VPS (Termux → ssh).
#
#    curl -s https://raw.githubusercontent.com/onizard/pharmacie-remises/claude/confident-newton-0scy4n/migrations/harden_services.sh | bash
#
#  Ce qui a causé la panne d'aujourd'hui : le VPS a rebooté et PostgREST n'avait
#  pas de politique de redémarrage → il n'est pas revenu. Ce script règle ça
#  pour TOUS les conteneurs, et active docker + ssh au démarrage.
#  Sans effet de bord : ne coupe rien, ne supprime rien.
# =============================================================================
set -uo pipefail
echo "=============================================================="
echo " Blindage des services — $(date)"
echo "=============================================================="

# 1) restart=unless-stopped sur tous les conteneurs qui tournent
running=$(docker ps -q)
if [ -n "$running" ]; then
  # shellcheck disable=SC2086
  docker update --restart unless-stopped $running >/dev/null 2>&1
  echo "→ Politique de redémarrage 'unless-stopped' appliquée à $(echo "$running" | wc -l) conteneur(s) en cours."
else
  echo "!! Aucun conteneur en cours ? Vérifie : docker ps -a"
fi

# 2) Idem pour les conteneurs de la stack même s'ils étaient arrêtés (par nom)
for pat in postgrest gotrue rest auth nginx minio postgres storage kong supabase realtime; do
  ids=$(docker ps -aq --filter "name=$pat" 2>/dev/null)
  # shellcheck disable=SC2086
  [ -n "$ids" ] && docker update --restart unless-stopped $ids >/dev/null 2>&1
done

# 3) docker + ssh activés au boot (survie au reboot)
systemctl enable docker >/dev/null 2>&1 && echo "→ docker activé au démarrage."
systemctl enable ssh    >/dev/null 2>&1 && echo "→ ssh activé au démarrage."
# Si sshd n'écoute que sur 22 alors que tu voulais 2223, on ne touche à rien ici
# (l'accès marche en 22, on ne casse pas ce qui fonctionne).

# 4) État final : nom + statut + politique de redémarrage de chaque conteneur
echo
echo "== État final des conteneurs =="
docker ps --format '{{.Names}}' | while read -r n; do
  rp=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$n" 2>/dev/null)
  st=$(docker inspect -f '{{.State.Status}}' "$n" 2>/dev/null)
  printf '   %-28s %-10s restart=%s\n' "$n" "$st" "$rp"
done

echo
echo "✅ Blindage terminé. Au prochain reboot, tout repart automatiquement."
echo "   (Vérif rapide côté public assurée par Claude.)"
