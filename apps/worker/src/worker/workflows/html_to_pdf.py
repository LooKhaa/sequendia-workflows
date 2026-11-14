"""Workflow html-to-pdf : rendu PDF print-ready de la charte Sequendia.

WeasyPrint + polices embarquées (Lora, Source Sans 3, OpenDyslexic) :
aucune dépendance réseau pour le rendu, typographie déterministe.

Illustrations : le client fournit les images en base64 (générées en amont,
ex. via l'outil image_generation). Le HTML les référence par leur nom de
fichier relatif, ex. <img src="paysage.png">.

Règles d'architecture respectées :
- toute la logique fichiers vit dans les activités, hors du sandbox
  déterministe du workflow ; le module ne fait aucun I/O à l'import ;
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


class Illustration(BaseModel):
    filename: str = Field(
        ...,
        description="Nom de fichier par lequel le HTML référence l'image (ex. paysage.png).",
        pattern=r"^[\w\-. ]+\.(png|jpg|jpeg)$",
    )
    data_base64: str = Field(
        ...,
        min_length=100,
        description="Contenu de l'image encodé en base64 (PNG ou JPEG), généré en amont.",
    )


class BundledIllustration(BaseModel):
    filename: str = Field(
        ...,
        description="Nom de fichier par lequel le HTML référence l'image (ex. cover.png).",
        pattern=r"^[\w\-. ]+\.(png|jpg|jpeg)$",
    )
    path: str = Field(
        ...,
        description=(
            "Chemin du fichier source dans l'image worker (ex. diapo/cover.png.b64). "
            "Un suffixe .b64 indique un fichier base64 à décoder."
        ),
        pattern=r"^[\w\-./ ]+\.(b64|png|jpg|jpeg)$",
    )


class HtmlToPdfInput(BaseModel):
    html: str = Field(..., description="Document HTML complet, print-ready (A4).")
    filename: str = Field(
        "document.pdf",
        description="Nom du fichier PDF produit.",
        pattern=r"^[\w\-. ]+\.pdf$",
    )
    illustrations: list[Illustration] = Field(
        default_factory=list,
        description="Illustrations (base64) à intégrer, référencées par nom de fichier dans le HTML.",
    )
    bundled_illustrations: list[BundledIllustration] = Field(
        default_factory=list,
        description=(
            "Illustrations embarquées dans l'image worker (fichiers du dépôt, "
            "ex. diapo/cover.png.b64), décodées au rendu ; référencées par filename dans le HTML."
        ),
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
async def validate_illustration(filename: str, data_base64: str) -> tuple[str, str]:
    """Valide le base64 d'une illustration, renvoie (filename, base64 décodé re-encodé)."""
    from temporalio.exceptions import ApplicationError

    try:
        raw = base64.b64decode(data_base64, validate=True)
    except Exception as err:
        raise ApplicationError(
            f"Illustration {filename}: base64 invalide ({err}).",
            non_retryable=True,
        ) from err
    if not raw.startswith((b"\x89PNG", b"\xff\xd8")):
        raise ApplicationError(
            f"Illustration {filename}: format non supporté (PNG ou JPEG attendu).",
            non_retryable=True,
        )
    return filename, base64.b64encode(raw).decode("ascii")


def _bundled_roots() -> list:
    """Répertoires candidats où chercher les fichiers embarqués (repo complet dans l'image)."""
    from pathlib import Path

    candidates = [Path.cwd()]
    try:
        candidates.append(Path(__file__).resolve().parents[5])
    except IndexError:  # mode source : parents remonte moins haut
        pass
    candidates.append(Path("/app"))
    return candidates


@workflows.activity()
async def render_pdf(html: str, images: dict[str, str], bundled: dict[str, str]) -> tuple[str, int]:
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

    # Espace de rendu : copie des assets (polices) + images.
    render_dir = Path(tempfile.mkdtemp(prefix="sequendia-render-"))
    try:
        shutil.copytree(assets_dir, render_dir, dirs_exist_ok=True)
        for filename, image_b64 in (images or {}).items():
            (render_dir / filename).write_bytes(base64.b64decode(image_b64))
        for filename, path in (bundled or {}).items():
            raw = None
            for root in _bundled_roots():
                candidate = root / path
                if candidate.is_file():
                    raw = candidate.read_bytes()
                    break
            if raw is None:
                raise ApplicationError(
                    f"Illustration embarquée introuvable : {path}",
                    non_retryable=True,
                )
            if path.endswith(".b64"):
                raw = base64.b64decode(raw)
            if not raw.startswith((b"\x89PNG", b"\xff\xd8")):
                raise ApplicationError(
                    f"Illustration embarquée {filename} : format non supporté (PNG ou JPEG attendu).",
                    non_retryable=True,
                )
            (render_dir / filename).write_bytes(raw)

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
        "et OpenDyslexic embarquées. Les illustrations sont fournies en base64 "
        "et intégrées au rendu (base64 en entrée, ou fichiers embarqués du dépôt). "
        "Retourne le PDF en base64, le nombre de pages "
        "et le nom de fichier."
    ),
)
class HtmlToPdfWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: HtmlToPdfInput) -> HtmlToPdfOutput:
        images: dict[str, str] = {}
        for illustration in input.illustrations:
            filename, image_b64 = await validate_illustration(
                filename=illustration.filename,
                data_base64=illustration.data_base64,
            )
            images[filename] = image_b64
        bundled_map = {
            bundled.filename: bundled.path for bundled in input.bundled_illustrations
        }
        pdf_base64, page_count = await render_pdf(input.html, images, bundled_map)
        return HtmlToPdfOutput(
            filename=input.filename,
            pdf_base64=pdf_base64,
            page_count=page_count,
        )
