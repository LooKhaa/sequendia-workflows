"""Workflow html-to-pdf : rendu PDF print-ready de la charte Sequendia.

WeasyPrint + polices embarquées (Lora, Source Sans 3, OpenDyslexic) :
aucune dépendance réseau au rendu, typographie déterministe. Le CSS des
polices est injecté dans le <head> du document avant le rendu, et
`base_url` pointe sur le dossier des assets pour résoudre les url().
"""

import base64
from pathlib import Path

from pydantic import BaseModel, Field

import mistralai.workflows as workflows


def _assets_dir() -> Path:
    """Localise apps/worker/assets, en dev (source) comme en image (site-packages)."""
    candidates = [
        Path(__file__).resolve().parents[3] / "assets",  # dev : src/worker/workflows
        Path.cwd() / "assets",
        Path("/app/apps/worker/assets"),  # image managée
    ]
    for c in candidates:
        if (c / "fonts.css").is_file():
            return c
    raise FileNotFoundError(
        "Dossier assets introuvable (fonts.css attendu à côté de fonts/)."
    )


ASSETS_DIR = _assets_dir()
FONTS_CSS = (ASSETS_DIR / "fonts.css").read_text(encoding="utf-8")


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
async def render_pdf(html: str) -> tuple[bytes, int]:
    """Convertit le HTML en PDF via WeasyPrint, avec les polices embarquées."""
    from weasyprint import HTML

    if "</head>" in html:
        full_html = html.replace(
            "</head>", f"<style>{FONTS_CSS}</style></head>", 1
        )
    else:
        full_html = f"<html><head><style>{FONTS_CSS}</style></head>{html}</html>"
    document = HTML(string=full_html, base_url=str(ASSETS_DIR)).render()
    return document.write_pdf(), len(document.pages)


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
        pdf_bytes, page_count = await render_pdf(input.html)
        return HtmlToPdfOutput(
            filename=input.filename,
            pdf_base64=base64.b64encode(pdf_bytes).decode("ascii"),
            page_count=page_count,
        )
