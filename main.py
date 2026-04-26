
import os
import sys
from pathlib import Path

# Configuration
CATALOG_DIR = "./catalogues"
DATABASE_FILE = "./medicaments.db"

def setup_directories():
    """Crée les répertoires nécessaires"""
    os.makedirs(CATALOG_DIR, exist_ok=True)
    print("✓ Répertoires configurés")

def download_catalogs():
    """Télécharge les catalogues depuis Astera"""
    print("\n--- Étape 1: Téléchargement des catalogues ---")
    try:
        # Importer et exécuter le script de scraping
        from astera_scraper import main as scraper_main
        scraper_main()
    except Exception as e:
        print(f"✗ Erreur lors du téléchargement : {e}")
        return False
    return True

def parse_catalogs():
    """Parse les fichiers PDF et extrait les données"""
    print("\n--- Étape 2: Parsing des catalogues ---")
    try:
        pdf_files = list(Path(CATALOG_DIR).glob("*.pdf"))
        print(f"✓ {len(pdf_files)} fichier(s) PDF trouvé(s)")
        
        if not pdf_files:
            print("⚠ Aucun PDF à traiter")
            return False
            
        # À implémenter : parsing des PDFs
        print("⏳ Parsing en cours...")
        # from pdf_parser import parse_pdfs
        # parse_pdfs(pdf_files, DATABASE_FILE)
        
    except Exception as e:
        print(f"✗ Erreur lors du parsing : {e}")
        return False
    return True

def start_web_app():
    """Lance l'application web"""
    print("\n--- Étape 3: Démarrage de l'application web ---")
    try:
        # À implémenter : lancer Flask/Django
        print("⏳ Application web en cours de démarrage...")
        # from web_app import app
        # app.run(debug=True, port=5000)
        
    except Exception as e:
        print(f"✗ Erreur lors du démarrage : {e}")
        return False
    return True

def main():
    """Fonction principale"""
    print("╔════════════════════════════════════════╗")
    print("║  Comparateur de Prix de Médicaments    ║")
    print("╚════════════════════════════════════════╝\n")
    
    # Étape 1 : Configuration
    setup_directories()
    
    # Étape 2 : Téléchargement des catalogues
    if not download_catalogs():
        print("Abandon du processus")
        sys.exit(1)
    
    # Étape 3 : Parsing des catalogues
    if not parse_catalogs():
        print("Abandon du processus")
        sys.exit(1)
    
    # Étape 4 : Lancer l'application web
    if not start_web_app():
        print("Abandon du processus")
        sys.exit(1)
    
    print("\n✓ Application prête!")

if __name__ == "__main__":
    main()
