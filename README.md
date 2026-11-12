# sequendia-workflows

Projet de workflows Mistral pour la production Sequendia. Basé sur le scaffold
officiel `mistral workflows init`, adapté pour le rendu PDF de la charte
graphique Sequendia.

## Workflow : `html-to-pdf`

Convertit un HTML print-ready (charte Sequendia, A4) en PDF stylisé via
**WeasyPrint**, avec les polices **Lora**, **Source Sans 3** et **OpenDyslexic**
embarquées dans l'image → aucun accès réseau au rendu, typographie identique à
chaque exécution.

Entrée :
```json
{
  "input": {
    "html": "<html>…print-ready…</html>",
    "filename": "fiche-eleve-seance1.pdf"
  }
}
```

Sortie : `{ "filename", "pdf_base64", "page_count" }`

Fichiers clés :
- `apps/worker/src/worker/workflows/html_to_pdf.py` — le workflow (1 activité)
- `apps/worker/assets/` — polices OFL + `fonts.css` (+ licences)
- `deploy/docker/worker-packages/sequendia-pdf.txt` — paquets système Pango
- `deploy/docker/Dockerfile.worker` — image multi-stage du déploiement managé

## Mise en route en local (optionnel)

```bash
bash tools/install.sh        # installe uv + les dépendances
bash tools/uv.sh lock        # met à jour uv.lock après un changement de deps
bash tools/uv.sh run --package workflows-app pytest apps/worker/tests
# avec MISTRAL_API_KEY dans .env :
mistral apps dev
```

## Déploiement managé (production)

1. Créer le dépôt GitHub **privé** `LooKhaa/sequendia-workflows` et pousser
   ce dossier à sa racine :
   ```bash
   git init && git add . && git commit -m "workflow html-to-pdf (WeasyPrint)"
   git remote add origin https://github.com/LooKhaa/sequendia-workflows.git
   git branch -M main && git push -u origin main
   ```
2. Installer la **Mistral GitHub App** (github.com/apps/mistralai) sur ce dépôt,
   puis connecter GitHub dans la console Mistral (Connectors → GitHub App).
3. Déployer (console → Workflows → Deployments, ou via l'agent Vibe) :
   - repository : `LooKhaa/sequendia-workflows`
   - revision : `main`
   - Dockerfile path : `deploy/docker/Dockerfile.worker`
4. Attendre le statut **Active**, puis exécuter `html-to-pdf`.

Aucun secret à configurer : le worker s'authentifie avec le token de service
injecté par la plateforme (`.env` requis uniquement en dev local).

> Note : le `Dockerfile.worker` fait un `uv sync` non figé, le lock se met à
> jour automatiquement pendant le build cloud si `uv.lock` a pris du retard.
