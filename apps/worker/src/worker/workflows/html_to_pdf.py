"""Workflow html-to-pdf : rendu PDF print-ready de la charte Sequendia.

WeasyPrint + polices embarquées (Lora, Source Sans 3, OpenDyslexic) :
aucune dépendance réseau au rendu, typographie déterministe.

Règles d'architecture respectées :
- toute la logique fichiers (assets, CSS des polices, rendu WeasyPrint) vit
  dans l'activité `render_pdf`, hors du sandbox déterministe du workflow ;
  le module ne fait aucun I/O à l'import ;
- l'activité renvoie le PDF déjà encodé en base64 : la plateforme sérialise
  les résultats d'activité en JSON, qui n'accepte pas d'octets bruts ;
- les erreurs d'entrée (HTML vide ou non-HTML) sont non-retryables : retenter
  ne changerait rien, le workflow échoue immédiatement avec un message clair.
"""

import base64

import structlog
from pydantic import BaseModel, Field

import mistralai.workflows as workflows

logger = structlog.get_logger("workflows.html_to_pdf")


class HtmlToPdfInput(BaseModel):
    html: str = Field(..., description="Document HTML complet, print-ready (A4).")
    filename: str = Field(
        "document.pdf",
        description="Nom du fichier PDF produit.",
        pattern=r"^[\w\-. ]+\.pdf$",
    )


class HtmlToPdfOutput(BaseModel):
    filename: str
    pdf_base64: str = Field(..., description="Contenu PDF encodé en base64.")
    page_count: int


@workflows.activity()
async def render_pdf(html: str) -> tuple[str, int]:
    """Convertit le HTML en PDF (base64) via WeasyPrint, polices embarquées."""
    from pathlib import Path

    from temporalio.exceptions import ApplicationError

    if not html or not html.strip():
        raise ApplicationError(
            "Le champ 'html' est vide : fournir un document HTML print-ready.",
            non_retryable=True,
        )

    def _assets_dir() -> Path:
        """Localise apps/worker/assets, en dev (source) comme en image."""
        candidates = [
            Path(__file__).resolve().parents[3] / "assets",  # dev : src/worker/workflows
            Path.cwd() / "assets",
            Path("/app/apps/worker/assets"),  # image managée
        ]
        for candidate in candidates:
            if (candidate / "fonts.css").is_file():
                return candidate
        raise ApplicationError(
            "Dossier assets introuvable (fonts.css attendu à côté de fonts/).",
            non_retryable=True,
        )

    assets_dir = _assets_dir()
    fonts_css = (assets_dir / "fonts.css").read_text(encoding="utf-8")

    if "</head>" in html:
        full_html = html.replace(
            "</head>", f"<style>{fonts_css}</style></head>", 1
        )
    else:
        full_html = f"<html><head><style>{fonts_css}</style></head>{html}</html>"

    from weasyprint import HTML

    try:
        document = HTML(string=full_html, base_url=str(assets_dir)).render()
    except Exception as err:
        # HTML invalide : retenter ne sert à rien.
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
    )
    return pdf_base64, len(document.pages)


@workflows.workflow.define(
    name="html-to-pdf",
    workflow_display_name="HTML vers PDF (Sequendia)",
    workflow_description=(
        "Convertit un document HTML print-ready (charte graphique Sequendia) "
        "en PDF stylisé via WeasyPrint, avec les polices Lora, Source Sans 3 "
        "et OpenDyslexic embarquées. Retourne le PDF en base64, le nombre de "
        "pages et le nom de fichier demandé."
    ),
)
class HtmlToPdfWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: HtmlToPdfInput) -> HtmlToPdfOutput:
        pdf_base64, page_count = await render_pdf(input.html)
        return HtmlToPdfOutput(
            filename=input.filename,
            pdf_base64=pdf_base64,
            page_count=page_count,
        )
