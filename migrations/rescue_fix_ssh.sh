#!/usr/bin/env bash
# =============================================================================
#  rescue_fix_ssh.sh — À LANCER DANS LE SYSTÈME DE SECOURS (RESCUE) HETZNER.
#  Réactive le login root par mot de passe sur le système normal + te fait
#  définir un mot de passe root connu. Après ça : désactive rescue, reboot,
#  et tu peux te connecter en SSH (Termux) en root + ce mot de passe.
#
#  IMPORTANT : télécharge-le puis lance-le (pour que le passwd interactif marche) :
#    curl -s https://raw.githubusercontent.com/onizard/pharmacie-remises/claude/confident-newton-0scy4n/migrations/rescue_fix_ssh.sh -o fix.sh && bash fix.sh
# =============================================================================
set -uo pipefail
echo "=============================================================="
echo " Réparation accès SSH (depuis le rescue Hetzner)"
echo "=============================================================="

# 1) Trouver la partition racine du système normal (plus grosse ext4).
part=$(lsblk -rpno NAME,FSTYPE,SIZE | awk '$2=="ext4"{print $1" "$3}' | sort -k2 -h | tail -1 | awk '{print $1}')
if [ -z "$part" ]; then echo "!! Aucune partition ext4 trouvée. Disques :"; lsblk; exit 1; fi
echo "→ Partition racine détectée : $part"

mp=/mnt/root; mkdir -p "$mp"
mountpoint -q "$mp" || mount "$part" "$mp" || { echo "!! Échec de montage de $part"; exit 1; }
if [ ! -d "$mp/etc/ssh" ]; then
  echo "!! $part n'est pas la racine (pas de /etc/ssh). Partitions présentes :"; lsblk; umount "$mp" 2>/dev/null; exit 1
fi

# 2) Autoriser root + mot de passe dans la conf sshd.
cfg="$mp/etc/ssh/sshd_config"
cp -a "$cfg" "$cfg.bak.$(date +%s)" 2>/dev/null || true
sed -i -E 's/^#?\s*PermitRootLogin.*/PermitRootLogin yes/; s/^#?\s*PasswordAuthentication.*/PasswordAuthentication yes/' "$cfg"
grep -qE '^PermitRootLogin yes'        "$cfg" || echo 'PermitRootLogin yes'        >> "$cfg"
grep -qE '^PasswordAuthentication yes' "$cfg" || echo 'PasswordAuthentication yes' >> "$cfg"
if [ -d "$mp/etc/ssh/sshd_config.d" ]; then
  for f in "$mp"/etc/ssh/sshd_config.d/*.conf; do [ -e "$f" ] || continue
    sed -i -E 's/^\s*PasswordAuthentication.*/PasswordAuthentication yes/; s/^\s*PermitRootLogin.*/PermitRootLogin yes/' "$f"
  done
fi
echo "→ sshd : PermitRootLogin yes + PasswordAuthentication yes"

# 3) Définir un mot de passe root connu (chroot pour écrire le bon /etc/shadow).
for d in dev proc sys; do mount --bind "/$d" "$mp/$d" 2>/dev/null; done
echo
echo "== Choisis un NOUVEAU mot de passe root (tu le tapes 2 fois) =="
chroot "$mp" passwd root

# 4) Nettoyage.
for d in dev proc sys; do umount "$mp/$d" 2>/dev/null; done
sync; umount "$mp" 2>/dev/null

echo
echo "=============================================================="
echo "✅ FINI. Étapes suivantes :"
echo "  1) Hetzner → serveur → Rescue → DÉSACTIVE le rescue"
echo "  2) Hetzner → Power → Reset  (reboot sur le système normal)"
echo "  3) Termux :  ssh root@178.104.40.21   + le mot de passe choisi"
echo "  4) Colle :   curl -s https://raw.githubusercontent.com/onizard/pharmacie-remises/claude/confident-newton-0scy4n/migrations/restart_postgrest.sh | bash"
echo "=============================================================="
