import os
import requests
from urllib.parse import unquote
from config import ASTERA_USERNAME, ASTERA_PASSWORD, OUTPUT_DIR

# Configuration récupérée depuis config.py
USERNAME = ASTERA_USERNAME
PASSWORD = ASTERA_PASSWORD

# Liste des URLs des catalogues
CATALOG_URLS = [
    "https://pro.astera.coop/DNL/PTN/GE13%20-%20Zydus%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE01%20-%20Arrow%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE02%20-%20Biogaran%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE12%20-%20Cristers%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/EG%20LABO%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE04%20-%20Viatris%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE05%20-%20Pfizer%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE06%20-%20Sandoz%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE08%20-%20Zentiva%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE07%20-%20Teva%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/P510%20-%20CORREVIO%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/P656%20-%20ABACUS%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
]

def main():
    # Créer le dossier de destination
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("╔════════════════════════════════════════╗")
    print("║  Téléchargement des catalogues Astera  ║")
    print("╚════════════════════════════════════════╝\n")
    
    # Créer une session avec authentification
    session = requests.Session()
    session.auth = (USERNAME, PASSWORD)
    
    # Headers pour simuler un navigateur
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    
    total = len(CATALOG_URLS)
    success = 0
    failed = 0
    
    for index, url in enumerate(CATALOG_URLS, 1):
        # Extraire le nom du fichier de l'URL
        filename = unquote(url.split('/')[-1])
        filepath = os.path.join(OUTPUT_DIR, filename)
        
        try:
            print(f"[{index}/{total}] Téléchargement : {filename[:50]}...", end=" ")
            
            response = session.get(url, timeout=30, allow_redirects=True)
            response.raise_for_status()
            
            # Afficher les informations de débogage
            content_type = response.headers.get('content-type', 'unknown')
            content_length = len(response.content)
            
            print(f"\n         Content-Type: {content_type}, Taille: {content_length} bytes")
            
            # Vérifier que c'est bien un PDF
            if content_type.startswith('application/pdf') or response.content[:4] == b'%PDF':
                with open(filepath, 'wb') as f:
                    f.write(response.content)
                print(f"         ✓ Sauvegardé ({content_length // 1024} KB)\n")
                success += 1
            else:
                # Sauvegarder quand même pour inspection
                debug_file = filepath.replace('.pdf', '_debug.txt')
                with open(debug_file, 'wb') as f:
                    f.write(response.content[:500])  # Premières 500 octets
                print(f"         ✗ Pas un PDF. Contenu saved in {debug_file}\n")
                failed += 1
                
        except requests.exceptions.Timeout:
            print("✗ Délai d'attente dépassé\n")
            failed += 1
        except requests.exceptions.HTTPError as e:
            print(f"✗ Erreur HTTP {e.response.status_code}\n")
            failed += 1
        except Exception as e:
            print(f"✗ Erreur : {str(e)}\n")
            failed += 1
    
    print(f"{'='*50}")
    print(f"✓ Réussi : {success}/{total}")
    print(f"✗ Échoué : {failed}/{total}")
    print(f"Fichiers stockés dans : {OUTPUT_DIR}")
    print(f"{'='*50}\n")
    
    return success > 0

if __name__ == "__main__":
    main()
