"""Serveur MCP BIMData : déclaration des outils, routes HTTP, middleware."""

from __future__ import annotations

import functools
import html
import logging
from typing import Annotated, Any, Literal

import anyio
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import __version__, files, service
from .auth import build_auth_provider, current_identity
from .bimdata import BIMDataAuthError
from .config import get_settings
from .quality.requirements import Requirement
from .selection import SelectionSpec
from .target import InvalidTargetError

logger = logging.getLogger("bimdata_mcp")

INSTRUCTIONS = """\
Serveur MCP BIMData : sélection d'éléments d'une maquette IFC hébergée sur BIMData, exports Excel,
COBie, Smart Views / BCF, contrôles de nommage / complétude / cahier des charges et corrections.

Démarrage : set_active_model(bimdata_url=<URL viewer BIMData>) — ou bimdata_browse pour trouver la maquette.

Syntaxe des champs (sélection, export, color_by, règles) :
  GlobalId, IfcClass, Name, LongName, Description, ObjectType, Tag, PredefinedType (attributs IFC),
  Building, Storey, Space, Zone, Classification, Material, Layer (lecture seule),
  Pset_X.Prop (propriété ou quantité d'un Pset précis), *.Prop ou Prop (cherchée dans tous les Psets).
  list_properties montre les champs réellement présents et leur taux de remplissage.

Flux usuels :
  • Sélectionner : select_elements(name, criteria) → show_selection / combine_selections.
  • Exporter : export_selection_excel(selection) → attributs IFC + Psets + Qto, une feuille par classe ;
    generate_cobie(selection?).
  • Publier : prepare_smart_view / prepare_bcf_topics → apply_plan(plan_id, confirm=True).
  • Contrôler : check_naming_completeness (règles génériques + custom_rules) ;
    cahier des charges : requirements_template → import_data_requirements → run_requirements_check.
    Chaque contrôle produit un rapport Excel (données contrôlées + « Erreurs à corriger »).
  • Corriger : prepare_corrections_from_check (propositions fiables) ou prepare_corrections_from_file
    (rapport complété, ou export de sélection modifié) → apply_plan(plan_id, confirm=True) → relancer le contrôle.

Trame de réponse (identique pour toutes les demandes, comme les fichiers produits) :
  1. Périmètre — maquette, sélection, phase.
  2. Chiffres clés — repris de « synthese ».
  3. Livrable — titre + lien download_url de « livrable » (tel quel).
  4. Points d'attention — « avertissements » et risques éventuels.
  5. Suite proposée — une action parmi « suite ».
  Tous les fichiers Excel suivent la charte BIMData (Synthèse → données → Lisez-moi) : ne pas en inventer d'autre.

Règles :
  • Aucune écriture BIMData sans plan : montrer le résumé du plan à l'utilisateur et n'appeler
    apply_plan(confirm=True) qu'après son accord explicite.
  • Les fichiers produits sont des liens de téléchargement signés (download_url) : les donner tels quels.
  • Pour un fichier de l'utilisateur : create_upload_link (dépôt via navigateur) → file_id, ou document de la GED
    BIMData (list_bimdata_documents → bimdata_document_id).
  • Les corrections écrites dans BIMData doivent aussi être reportées dans le logiciel auteur.
"""

mcp = FastMCP("bimdata-mcp", instructions=INSTRUCTIONS, version=__version__, auth=build_auth_provider())


# ── Exécution des outils ────────────────────────────────────────────────

_EXPECTED = (ValueError, PermissionError, FileNotFoundError, InvalidTargetError, KeyError)


async def _run(ctx: Context | None, fn, /, **kwargs) -> dict[str, Any]:
    try:
        identity = current_identity()
    except PermissionError as exc:
        raise ToolError(str(exc)) from None
    session_id = (getattr(ctx, "session_id", None) if ctx else None) or "default"
    call = service.Call(identity=identity, session_id=str(session_id))
    try:
        return await anyio.to_thread.run_sync(functools.partial(fn, call, **kwargs))
    except BIMDataAuthError as exc:
        raise ToolError(f"Accès BIMData refusé ({exc}). Vérifier les droits sur le projet / la maquette "
                        "ou reconnecter le connecteur.") from None
    except _EXPECTED as exc:
        raise ToolError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        import requests

        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            raise ToolError("API BIMData injoignable depuis le serveur MCP (réseau / proxy / délai dépassé). "
                            "Réessayer ; si le problème persiste, vérifier BIMDATA_API_URL et la sortie réseau "
                            "du serveur.") from None
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            code = exc.response.status_code
            body = (exc.response.text or "")[:300]
            raise ToolError(f"Erreur API BIMData {code} : {body}") from None
        logger.exception("tool %s failed", getattr(fn, "__name__", fn))
        detail = "" if get_settings().is_production else f" ({type(exc).__name__}: {exc})"
        raise ToolError(f"Erreur interne du serveur MCP BIMData{detail}.") from None


Str = Annotated[str | None, Field(description="Optionnel.")]
SelName = Annotated[str | None, Field(description="Nom d'une sélection (select_elements). Vide = maquette entière.")]
FileArgs = dict


# ── Maquette ────────────────────────────────────────────────────────────


@mcp.tool(annotations={"readOnlyHint": True})
async def bimdata_browse(
    cloud_id: Annotated[int | None, Field(description="Vide = lister les clouds (espaces).")] = None,
    project_id: Annotated[int | None, Field(description="Avec cloud_id : lister les maquettes du projet.")] = None,
    ctx: Context = None,
) -> dict:
    """Parcourt les espaces (clouds), projets et maquettes BIMData accessibles, avec l'URL viewer de chaque maquette."""
    return await _run(ctx, service.browse, cloud_id=cloud_id, project_id=project_id)


@mcp.tool(annotations={"readOnlyHint": True})
async def set_active_model(
    bimdata_url: Annotated[str | None, Field(description="URL viewer : https://platform.bimdata.io/spaces/<c>/projects/<p>/viewer/<m>")] = None,
    cloud_id: int | None = None,
    project_id: int | None = None,
    model_id: int | None = None,
    refresh: Annotated[bool, Field(description="Forcer la relecture complète depuis l'API.")] = False,
    ctx: Context = None,
) -> dict:
    """Active une maquette (URL viewer OU les trois identifiants), vérifie l'accès et charge ses données. À appeler en premier."""
    return await _run(ctx, service.set_active_model, bimdata_url=bimdata_url, cloud_id=cloud_id,
                      project_id=project_id, model_id=model_id, refresh=refresh)


@mcp.tool(annotations={"readOnlyHint": True})
async def model_overview(ctx: Context = None) -> dict:
    """Vue d'ensemble de la maquette active : étages, classes IFC, Psets les plus fréquents."""
    return await _run(ctx, service.model_overview)


@mcp.tool(annotations={"readOnlyHint": True})
async def list_properties(
    ifc_classes: Annotated[list[str] | None, Field(description="Restreindre à ces classes (sous-types inclus).")] = None,
    contains: Annotated[str | None, Field(description="Filtre texte sur le nom du champ.")] = None,
    limit: int = 200,
    ctx: Context = None,
) -> dict:
    """Catalogue des attributs et propriétés présents (Pset.Prop) avec taux de remplissage."""
    return await _run(ctx, service.list_properties, ifc_classes=ifc_classes, contains=contains, limit=limit)


# ── Sélection ───────────────────────────────────────────────────────────


@mcp.tool(annotations={"readOnlyHint": True})
async def select_elements(
    name: Annotated[str, Field(description="Nom de la sélection (réutilisable dans les autres outils).")],
    criteria: Annotated[SelectionSpec | None, Field(description="Critères (ET entre critères). Vide = tous les éléments physiques.")] = None,
    description: str = "",
    preview: int = 10,
    ctx: Context = None,
) -> dict:
    """Sélectionne des éléments de la maquette active (classes IFC, étages, pièces, nom, propriétés, classification…) et enregistre la sélection."""
    return await _run(ctx, service.select_elements, name=name, criteria=criteria, description=description, preview=preview)


@mcp.tool(annotations={"readOnlyHint": True})
async def combine_selections(
    name: str,
    operation: Literal["union", "intersection", "difference"],
    selections: list[str],
    ctx: Context = None,
) -> dict:
    """Combine des sélections (union, intersection, différence = première moins les suivantes)."""
    return await _run(ctx, service.combine_selections, name=name, operation=operation, selections=selections)


@mcp.tool(annotations={"readOnlyHint": True})
async def list_selections(ctx: Context = None) -> dict:
    """Liste les sélections enregistrées."""
    return await _run(ctx, service.list_selections)


@mcp.tool(annotations={"readOnlyHint": True})
async def show_selection(
    name: str,
    fields: Annotated[list[str] | None, Field(description="Champs à afficher (syntaxe des champs).")] = None,
    offset: int = 0,
    limit: int = 50,
    ctx: Context = None,
) -> dict:
    """Affiche les éléments d'une sélection avec les champs demandés (paginé)."""
    return await _run(ctx, service.show_selection, name=name, fields=fields, offset=offset, limit=min(limit, 200))


@mcp.tool(annotations={"destructiveHint": True})
async def delete_selection(name: str, ctx: Context = None) -> dict:
    """Supprime une sélection enregistrée (n'affecte pas la maquette)."""
    return await _run(ctx, service.delete_selection, name=name)


# ── Exports ─────────────────────────────────────────────────────────────


@mcp.tool()
async def export_selection_excel(
    selection: SelName = None,
    properties: Annotated[list[str] | Literal["all"] | None, Field(
        description="Propriétés des Psets à exporter : 'all' (défaut, toutes) ou liste de 'Pset.Prop' / 'Pset_X.*'.")] = "all",
    include_quantities: Annotated[bool, Field(description="Toutes les quantités Qto_* / BaseQuantities (minimum requis).")] = True,
    include_location: Annotated[bool, Field(description="Colonnes Bâtiment, Étage, Espace, Zone, Classification, Matériaux.")] = True,
    layout: Annotated[Literal["par_classe", "une_feuille"], Field(description="Une feuille par classe IFC (défaut) ou une seule.")] = "par_classe",
    long_format: Annotated[bool, Field(description="Ajouter une feuille « une ligne par propriété ».")] = False,
    min_fill_rate: Annotated[float, Field(ge=0, le=1, description="Avec 'all' : ignorer les propriétés moins remplies.")] = 0.0,
    file_name: Str = None,
    publish_to_bimdata: Annotated[bool, Field(description="Déposer aussi le fichier dans la GED du projet BIMData.")] = False,
    bimdata_folder_id: Annotated[int | None, Field(description="Dossier GED cible (optionnel).")] = None,
    ctx: Context = None,
) -> dict:
    """Exporte une sélection en Excel, structurée comme le panneau Propriétés du viewer BIMData : une feuille par
    classe IFC ; colonnes = attributs IFC (UUID, Type, Nom, Description, Type d'objet, PredefinedType, Tag…),
    localisation, toutes les propriétés des Psets et toutes les quantités Qto (en-tête : groupe / propriété).
    Le fichier est réimportable pour corriger la maquette."""
    return await _run(ctx, service.export_selection_excel, selection=selection, properties=properties,
                      include_quantities=include_quantities, include_location=include_location, layout=layout,
                      long_format=long_format, min_fill_rate=min_fill_rate, file_name=file_name,
                      publish_to_bimdata=publish_to_bimdata, bimdata_folder_id=bimdata_folder_id)


@mcp.tool()
async def generate_cobie(
    selection: Annotated[str | None, Field(description="Restreindre les composants à une sélection. Vide = maquette entière.")] = None,
    file_name: Str = None,
    publish_to_bimdata: bool = False,
    bimdata_folder_id: int | None = None,
    ctx: Context = None,
) -> dict:
    """Génère un classeur COBie (Facility, Floor, Space, Zone, Type, Component, System, Attribute)."""
    return await _run(ctx, service.generate_cobie, selection=selection, file_name=file_name,
                      publish_to_bimdata=publish_to_bimdata, bimdata_folder_id=bimdata_folder_id)


# ── Publication ─────────────────────────────────────────────────────────


@mcp.tool()
async def prepare_smart_view(
    title: str,
    selection: SelName = None,
    color: Annotated[str, Field(description="Couleur #RRGGBB (mode une couleur). Défaut : rouge High de la charte.")] = "#FF3D1E",
    color_by: Annotated[str | None, Field(description="Colorer par valeur d'un champ (ex. 'Storey', 'Pset_WallCommon.FireRating').")] = None,
    selections: Annotated[list[str] | None, Field(description="Une couleur par sélection (légende multi-groupes).")] = None,
    colors: Annotated[list[str] | None, Field(description="Couleurs associées à `selections`.")] = None,
    check_id: Annotated[str | None, Field(description="Colorer selon un contrôle : conforme / non conforme par criticité.")] = None,
    ctx: Context = None,
) -> dict:
    """Prépare une Smart View BIMData (coloration dans le viewer) depuis une sélection, plusieurs sélections, un champ ou
    un contrôle. N'écrit rien : retourne un plan à appliquer avec apply_plan."""
    return await _run(ctx, service.prepare_smart_view, title=title, selection=selection, color=color, color_by=color_by,
                      selections=selections, colors=colors, check_id=check_id)


@mcp.tool()
async def prepare_bcf_topics(
    title: str,
    selection: SelName = None,
    description: str = "",
    priority: Literal["Critical", "High", "Medium", "Low", "Information"] = "Medium",
    topic_type: str = "Issue",
    labels: list[str] | None = None,
    assigned_to: Annotated[str | None, Field(description="E-mail d'un membre du projet.")] = None,
    due_date: Annotated[str | None, Field(description="AAAA-MM-JJ")] = None,
    group_by: Annotated[str | None, Field(description="Un topic par valeur de ce champ (ex. 'Storey').")] = None,
    one_topic_per_element: bool = False,
    check_id: Annotated[str | None, Field(description="Créer les topics à partir des anomalies d'un contrôle.")] = None,
    check_group_by: Literal["requirement", "severity", "element", "status"] = "requirement",
    max_topics: int = 100,
    ctx: Context = None,
) -> dict:
    """Prépare des topics BCF (issues) : un topic pour la sélection, un par valeur, un par élément, ou un par exigence non
    respectée d'un contrôle. N'écrit rien : retourne un plan à appliquer avec apply_plan."""
    return await _run(ctx, service.prepare_bcf_topics, title=title, selection=selection, description=description,
                      priority=priority, topic_type=topic_type, labels=labels, assigned_to=assigned_to, due_date=due_date,
                      group_by=group_by, one_topic_per_element=one_topic_per_element, check_id=check_id,
                      check_group_by=check_group_by, max_topics=max_topics)


@mcp.tool(annotations={"destructiveHint": True})
async def apply_plan(
    plan_id: str,
    confirm: Annotated[bool, Field(description="True uniquement après accord explicite de l'utilisateur.")] = False,
    ctx: Context = None,
) -> dict:
    """Applique un plan d'écriture (Smart View, BCF, corrections de propriétés) sur BIMData. Sans confirm=True : aperçu seul."""
    return await _run(ctx, service.apply_plan, plan_id=plan_id, confirm=confirm)


@mcp.tool(annotations={"readOnlyHint": True})
async def list_plans(limit: int = 20, ctx: Context = None) -> dict:
    """Liste les plans d'écriture préparés / appliqués."""
    return await _run(ctx, service.list_plans, limit=limit)


@mcp.tool(annotations={"readOnlyHint": True})
async def show_plan(plan_id: str, offset: int = 0, limit: int = 50, ctx: Context = None) -> dict:
    """Détaille les opérations d'un plan (paginé) et son résultat s'il a été appliqué."""
    return await _run(ctx, service.show_plan, plan_id=plan_id, offset=offset, limit=min(limit, 200))


# ── Contrôles ───────────────────────────────────────────────────────────


@mcp.tool()
async def check_naming_completeness(
    selection: SelName = None,
    custom_rules: Annotated[list[Requirement] | None, Field(
        description="Règles supplémentaires (ex. charte de nommage : field='LongName', ifc_classes=['IfcSpace'], "
                    "allowed_values=[…] ou pattern='^R\\\\d{2}-\\\\d{3}$', unique_scope='storey').")] = None,
    include_default_rules: Annotated[bool, Field(description="Règles génériques : noms renseignés/uniques, espaces, caractères interdits, type, étage, matériau, IsExternal, LoadBearing.")] = True,
    include_default_names_rule: Annotated[bool, Field(description="Signaler les noms par défaut du logiciel (ex. « Famille:Type:123456 »).")] = True,
    include_classification: bool = False,
    phase: Str = None,
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Contrôle le nommage et la complétude des objets IFC, propose des corrections et produit un rapport Excel
    (synthèse, erreurs à corriger, données contrôlées)."""
    return await _run(ctx, service.check_naming_completeness, selection=selection, custom_rules=custom_rules,
                      include_default_rules=include_default_rules, include_default_names_rule=include_default_names_rule,
                      include_classification=include_classification, phase=phase, title=title)


@mcp.tool()
async def requirements_template(ctx: Context = None) -> dict:
    """Fournit le gabarit Excel de cahier des charges de données (à compléter puis importer)."""
    return await _run(ctx, service.requirements_template)


@mcp.tool()
async def import_data_requirements(
    name: Str = None,
    file_id: Annotated[str | None, Field(description="Fichier déposé (create_upload_link) ou produit par le serveur.")] = None,
    bimdata_document_id: Annotated[int | None, Field(description="Document de la GED du projet actif.")] = None,
    file_url: Annotated[str | None, Field(description="URL https publique.")] = None,
    file_base64: Annotated[str | None, Field(description="Contenu encodé en base64 (petits fichiers).")] = None,
    file_name: Str = None,
    sheet: Annotated[str | None, Field(description="Feuille à lire (défaut : toutes celles qui ont un en-tête reconnu).")] = None,
    header_row: Annotated[int | None, Field(description="Ligne d'en-tête (1 = première) si la détection échoue.")] = None,
    column_map: Annotated[dict[str, Any] | None, Field(
        description="Forcer les colonnes : {'ifc_class':'C','property':'D','pset':'E','value_type':'F',"
                    "'allowed_values':'G','required':'H','phases_columns':{'J':'APD','K':'PRO'}}")] = None,
    default_ifc_classes: Annotated[list[str] | None, Field(description="Classes à appliquer aux lignes sans classe IFC.")] = None,
    ctx: Context = None,
) -> dict:
    """Importe un cahier des charges de données (gabarit du serveur ou fichier du projet : charte de données, annexe contractuelle…) et le
    convertit en exigences contrôlables. Détecte en-têtes, colonnes de phases et cellules fusionnées."""
    return await _run(ctx, service.import_data_requirements, name=name, file_id=file_id,
                      bimdata_document_id=bimdata_document_id, file_url=file_url, file_base64=file_base64,
                      file_name=file_name, sheet=sheet, header_row=header_row, column_map=column_map,
                      default_ifc_classes=default_ifc_classes)


@mcp.tool(annotations={"readOnlyHint": True})
async def list_requirement_sets(ctx: Context = None) -> dict:
    """Liste les cahiers des charges importés."""
    return await _run(ctx, service.list_requirement_sets)


@mcp.tool(annotations={"readOnlyHint": True})
async def show_requirements(requirements_id: str, ifc_class: Str = None, offset: int = 0, limit: int = 50,
                            ctx: Context = None) -> dict:
    """Affiche les exigences d'un cahier des charges importé (paginé, filtrable par classe)."""
    return await _run(ctx, service.show_requirements, rid=requirements_id, ifc_class=ifc_class, offset=offset,
                      limit=min(limit, 200))


@mcp.tool()
async def edit_requirements(
    requirements_id: str,
    upsert: Annotated[list[Requirement] | None, Field(description="Exigences à ajouter ou remplacer (même id).")] = None,
    remove_ids: list[str] | None = None,
    ctx: Context = None,
) -> dict:
    """Ajuste un cahier des charges importé (corriger un nom de propriété, une classe, ajouter une règle…)."""
    return await _run(ctx, service.edit_requirements, rid=requirements_id, upsert=upsert, remove_ids=remove_ids)


@mcp.tool()
async def run_requirements_check(
    requirements_id: str,
    selection: SelName = None,
    phase: Annotated[str | None, Field(description="Phase à contrôler (APS, APD, PRO, DCE, EXE, DOE…). Vide = toutes.")] = None,
    include_naming_rules: bool = False,
    title: Str = None,
    auto_fill_threshold: Annotated[float, Field(ge=0, le=1, description="Confiance mini pour pré-remplir « Valeur corrigée ».")] = 0.8,
    ctx: Context = None,
) -> dict:
    """Contrôle la maquette (ou une sélection) contre un cahier des charges importé et produit l'extraction Excel :
    données contrôlées + erreurs à corriger (avec propositions)."""
    return await _run(ctx, service.run_requirements_check, rid=requirements_id, selection=selection, phase=phase,
                      include_naming_rules=include_naming_rules, title=title, auto_fill_threshold=auto_fill_threshold)


@mcp.tool(annotations={"readOnlyHint": True})
async def list_checks(ctx: Context = None) -> dict:
    """Liste les contrôles réalisés."""
    return await _run(ctx, service.list_checks)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_check_issues(
    check_id: str,
    status: Str = None,
    req_id: Str = None,
    ifc_class: Str = None,
    severity: Annotated[Literal["bloquant", "majeur", "mineur", "info"] | None, Field()] = None,
    with_proposal_only: bool = False,
    offset: int = 0,
    limit: int = 50,
    ctx: Context = None,
) -> dict:
    """Détaille les anomalies d'un contrôle (filtrables) avec les propositions de correction."""
    return await _run(ctx, service.get_check_issues, check_id=check_id, status=status, req_id=req_id,
                      ifc_class=ifc_class, severity=severity, with_proposal_only=with_proposal_only, offset=offset,
                      limit=min(limit, 200))


# ── Corrections ─────────────────────────────────────────────────────────


@mcp.tool()
async def prepare_corrections_from_file(
    file_id: Str = None,
    bimdata_document_id: int | None = None,
    file_url: Str = None,
    file_base64: Str = None,
    file_name: Str = None,
    ctx: Context = None,
) -> dict:
    """Lit un fichier corrigé (rapport de contrôle complété, export de sélection modifié ou tableau GlobalId +
    propriétés) et prépare le plan d'écriture des corrections. N'écrit rien."""
    return await _run(ctx, service.prepare_corrections_from_file, file_id=file_id,
                      bimdata_document_id=bimdata_document_id, file_url=file_url, file_base64=file_base64,
                      file_name=file_name)


@mcp.tool()
async def prepare_corrections_from_check(
    check_id: str,
    min_confidence: Annotated[float, Field(ge=0, le=1)] = 0.8,
    req_ids: list[str] | None = None,
    statuses: list[str] | None = None,
    issue_ids: Annotated[list[str] | None, Field(description="Retenir exactement ces anomalies (ignore min_confidence).")] = None,
    ctx: Context = None,
) -> dict:
    """Prépare un plan de corrections à partir des propositions automatiques d'un contrôle. N'écrit rien."""
    return await _run(ctx, service.prepare_corrections_from_check, check_id=check_id, min_confidence=min_confidence,
                      req_ids=req_ids, statuses=statuses, issue_ids=issue_ids)


# ── Fichiers ────────────────────────────────────────────────────────────


@mcp.tool()
async def create_upload_link(purpose: str = "", ctx: Context = None) -> dict:
    """Crée un lien de dépôt (navigateur) pour envoyer un fichier au serveur : cahier des charges, fichier corrigé…"""
    return await _run(ctx, service.create_upload_link, purpose=purpose)


@mcp.tool(annotations={"readOnlyHint": True})
async def list_files(kind: Annotated[str | None, Field(description="export | cobie | check-report | template | upload")] = None,
                     ctx: Context = None) -> dict:
    """Liste les fichiers de l'utilisateur sur le serveur (exports, rapports, dépôts) avec leur lien de téléchargement."""
    return await _run(ctx, service.list_files, kind=kind)


@mcp.tool(annotations={"readOnlyHint": True})
async def list_bimdata_documents(search: Str = None, ctx: Context = None) -> dict:
    """Liste les documents de la GED du projet actif (pour importer un cahier des charges ou un fichier corrigé)."""
    return await _run(ctx, service.list_bimdata_documents, search=search)


# ── Routes HTTP ─────────────────────────────────────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok", "version": __version__, "auth_mode": get_settings().auth_mode})


@mcp.custom_route("/files/{owner}/{file_id}/{name}", methods=["GET"])
async def download(request: Request) -> Response:
    p = request.path_params
    path = files.verify_download(p["owner"], p["file_id"], request.query_params.get("exp", ""),
                                 request.query_params.get("sig", ""))
    if path is None:
        return Response("Lien invalide ou expiré.", status_code=403)
    return FileResponse(path, media_type=files.guess_mime(path), filename=path.name)


_UPLOAD_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Dépôt — BIMData MCP</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:Roboto,Arial,sans-serif;background:#F4F5F8;color:#2F374A;margin:0;padding:40px}}
.card{{max-width:560px;margin:auto;background:#fff;border-radius:8px;padding:28px;box-shadow:0 2px 10px #0001}}
h1{{font-size:20px;margin:0 0 6px}} p{{color:#555}} button{{background:#2F374A;color:#fff;border:0;border-radius:4px;
padding:10px 18px;font-size:14px;cursor:pointer}} input{{margin:16px 0}} code{{background:#FFF1C9;padding:3px 6px;
border-radius:3px;font-size:15px}}</style></head><body><div class="card">{body}</div></body></html>"""


@mcp.custom_route("/upload/{owner}/{exp}/{sig}", methods=["GET", "POST"])
async def upload(request: Request) -> Response:
    p = request.path_params
    if not files.verify_upload(p["owner"], p["exp"], p["sig"]):
        return HTMLResponse(_UPLOAD_PAGE.format(body="<h1>Lien expiré</h1><p>Demander un nouveau lien à Claude.</p>"),
                            status_code=403)
    purpose = html.escape(request.query_params.get("purpose", ""))
    if request.method == "GET":
        body = (f"<h1>Déposer un fichier</h1><p>{purpose or 'Cahier des charges, fichier corrigé…'}</p>"
                "<form method='post' enctype='multipart/form-data'><input type='file' name='file' required "
                "accept='.xlsx,.xlsm,.xls,.csv,.json'><br><button type='submit'>Envoyer</button></form>")
        return HTMLResponse(_UPLOAD_PAGE.format(body=body))
    s = get_settings()
    form = await request.form(max_files=1, max_fields=5)
    up = form.get("file")
    if up is None or not getattr(up, "filename", None):
        return HTMLResponse(_UPLOAD_PAGE.format(body="<h1>Aucun fichier reçu</h1>"), status_code=400)
    content = await up.read(s.max_upload_mb * 1024 * 1024 + 1)
    try:
        meta = files.store_upload(p["owner"], up.filename, content)
    except ValueError as exc:
        return HTMLResponse(_UPLOAD_PAGE.format(body=f"<h1>Refusé</h1><p>{html.escape(str(exc))}</p>"), status_code=400)
    body = (f"<h1>Fichier reçu ✓</h1><p>{html.escape(meta['label'])} ({meta['size'] // 1024} Ko)</p>"
            f"<p>Indiquer à Claude ce file_id :</p><p><code>{meta['file_id']}</code></p>")
    return HTMLResponse(_UPLOAD_PAGE.format(body=body))
