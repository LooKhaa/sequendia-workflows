"""Workflow html-to-pdf : rendu PDF print-ready de la charte Sequendia.

WeasyPrint + polices embarquées (Lora, Source Sans 3, OpenDyslexic) :
aucune dépendance réseau pour le rendu, typographie déterministe.

Illustrations : optionnellement, le workflow génère des images via l'agent
Mistral `image_generation` (API conversations), les télécharge via l'API
files et les intègre au rendu. Le HTML les référence par leur nom de
fichier relatif, ex. <img src="paysage.png">.

Règles d'architecture respectées :
- toute la logique fichiers et réseau vit dans les activités, hors du
  sandbox déterministe du workflow ; le module ne fait aucun I/O à l'import ;
- les activités renvoient des chaînes base64 : la plateforme sérialise les
  résultats d'activité en JSON, qui n'accepte pas d'octets bruts ;
- les erreurs d'entrée sont non-retryables : retenter ne changerait rien.
"""

import base64
import shutil
import tempfile

import structlog
from pydantic import BaseModel, Field

import mistralai.workflows as workflows

logger = structlog.get_logger("workflows.html_to_pdf")

STYLE_DEFAUT = (
    "Illustration vectorielle plate (flat design), élégante et minimaliste, "
    "style manuel scolaire français premium : formes simples et épurées, "
    "palette douce et cohérente de bleu royal (#2563EB), bleu nuit (#1E50C8), "
    "bleu pâle (#DBEAFE) et touches d'ambre (#D97706), fond blanc ou bleu "
    "très pâle. Aucun texte, aucune lettre, aucun mot."
)


class Illustration(BaseModel):
    filename: str = Field(
        ...,
        description="Nom de fichier par lequel le HTML référence l'image (ex. paysage.png).",
        pattern=r"^[\w\-. ]+\.(png|jpg|jpeg)$",
    )
    prompt: str = Field(..., description="Description de l'illustration à générer.")


class HtmlToPdfInput(BaseModel):
    html: str = Field(..., description="Document HTML complet, print-ready (A4).")
    filename: str = Field(
        "document.pdf",
        description="Nom du fichier PDF produit.",
        pattern=r"^[\w\-. ]+\.pdf$",
    )
    illustrations: list[Illustration] = Field(
        default_factory=list,
        description="Illustrations à générer puis à intégrer (référencées par nom de fichier dans le HTML).",
    )
    style: str = Field(
        default=STYLE_DEFAUT,
        description="Style commun appliqué à chaque prompt d'illustration.",
    )


class HtmlToPdfOutput(BaseModel):
    filename: str
    pdf_base64: str = Field(..., description="Contenu PDF encodé en base64.")
    page_count: int


def _assets_dir():
    """Localise apps/worker/assets, en dev (source) comme en image."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[3] / "assets",  # dev : src/worker/workflows
        Path.cwd() / "assets",
        Path("/app/apps/worker/assets"),  # image managée
    ]
    for candidate in candidates:
        if (candidate / "fonts.css").is_file():
            return candidate
    return None


@workflows.activity()
async def generate_illustration(prompt: str, style: str) -> str:
    """Génère une image via l'agent Mistral image_generation, renvoie du PNG base64."""
    from temporalio.exceptions import ApplicationError
    from utils.mistral import mistral_client

    client = mistral_client()  # token de service injecté par la plateforme
    agent = client.beta.agents.create(
        model="mistral-medium-latest",
        name="Sequendia illustrateur",
        description="Génère des illustrations pour les supports pédagogiques.",
        instructions=(
            "Utilise l'outil image_generation pour répondre. "
            "Ne génère qu'une seule image par demande."
        ),
        tools=[{"type": "image_generation"}],
    )
    try:
        conversation = client.beta.conversations.start(
            agent_id=agent.id, inputs=f"{style}. {prompt}"
        )
        file_id = None
        for entry in conversation.outputs or []:
            if getattr(entry, "type", "") != "message.output":
                continue
            for part in getattr(entry, "content", []) or []:
                if getattr(part, "type", "") == "tool_file" and getattr(part, "file_id", None):
                    file_id = part.file_id
                    break
            if file_id:
                break
        if file_id is None:
            raise ApplicationError(
                f"Aucun fichier image renvoyé par l'agent pour : {prompt[:80]}",
                non_retryable=True,
            )
        response = client.files.download(file_id=file_id)
        return base64.b64encode(response.content).decode("ascii")
    finally:
        try:
            client.beta.agents.delete(agent_id=agent.id)
        except Exception:  # nettoyage best-effort
            pass


@workflows.activity()
async def render_pdf(html: str, images: dict[str, str]) -> tuple[str, int]:
    """Convertit le HTML en PDF (base64) via WeasyPrint, polices embarquées."""
    from pathlib import Path

    from temporalio.exceptions import ApplicationError

    if not html or not html.strip():
        raise ApplicationError(
            "Le champ 'html' est vide : fournir un document HTML print-ready.",
            non_retryable=True,
        )

    assets_dir = _assets_dir()
    if assets_dir is None:
        raise ApplicationError(
            "Dossier assets introuvable (fonts.css attendu à côté de fonts/).",
            non_retryable=True,
        )

    # Espace de rendu : copie des assets (polices) + images générées.
    render_dir = Path(tempfile.mkdtemp(prefix="sequendia-render-"))
    try:
        shutil.copytree(assets_dir, render_dir, dirs_exist_ok=True)
        for filename, image_b64 in (images or {}).items():
            (render_dir / filename).write_bytes(base64.b64decode(image_b64))

        fonts_css = (render_dir / "fonts.css").read_text(encoding="utf-8")

        if "</head>" in html:
            full_html = html.replace(
                "</head>", f"<style>{fonts_css}</style></head>", 1
            )
        else:
            full_html = f"<html><head><style>{fonts_css}</style></head>{html}</html>"

        from weasyprint import HTML

        try:
            document = HTML(string=full_html, base_url=str(render_dir)).render()
        except Exception as err:
            raise ApplicationError(
                f"Rendu WeasyPrint impossible (HTML invalide ?) : {err}",
                non_retryable=True,
            ) from err

        pdf_bytes = document.write_pdf()
        pdf_base64 = base64.b64encode(pdf_bytes).decode("ascii")
        logger.info(
            "PDF rendu",
            pages=len(document.pages),
            pdf_base64_size=len(pdf_base64),
            images=len(images or {}),
        )
        return pdf_base64, len(document.pages)
    finally:
        shutil.rmtree(render_dir, ignore_errors=True)


@workflows.workflow.define(
    name="html-to-pdf",
    workflow_display_name="HTML vers PDF (Sequendia)",
    workflow_description=(
        "Convertit un document HTML print-ready (charte graphique Sequendia) "
        "en PDF stylisé via WeasyPrint, avec les polices Lora, Source Sans 3 "
        "et OpenDyslexic embarquées. Optionnellement génère des illustrations "
        "via l'agent Mistral image_generation et les intègre au rendu. "
        "Retourne le PDF en base64, le nombre de pages et le nom de fichier."
    ),
)
class HtmlToPdfWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: HtmlToPdfInput) -> HtmlToPdfOutput:
        images: dict[str, str] = {}
        for illustration in input.illustrations:
            images[illustration.filename] = await generate_illustration(
                prompt=illustration.prompt, style=input.style
            )
        pdf_base64, page_count = await render_pdf(input.html, images)
        return HtmlToPdfOutput(
            filename=input.filename,
            pdf_base64=pdf_base64,
            page_count=page_count,
        )
