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

from . import __version__, files, service, service_bcf, service_geometry
from .auth import build_auth_provider, current_identity
from .bimdata import BIMDataAuthError
from .config import get_settings
from .quality.requirements import Requirement
from .selection import SelectionSpec
from .target import InvalidTargetError

logger = logging.getLogger("bimdata_mcp")

INSTRUCTIONS = """\
Langue : échanger avec l'utilisateur exclusivement en français (questions, menus, synthèses, propositions), même si
les noms de la maquette, des outils ou des paramètres sont en anglais. Écrire des phrases claires, sans jargon
d'outil : pas d'identifiants techniques (c5_pmr, check_id…) dans les messages, sauf demande.

Serveur MCP BIMData : sélection d'éléments d'une maquette IFC hébergée sur BIMData, exports Excel,
COBie, Smart Views / BCF, contrôles de nommage / complétude / cahier des charges et corrections.

Démarrage : set_active_model(bimdata_url=<URL viewer BIMData>) — ou bimdata_browse pour trouver la maquette.

Syntaxe des champs (sélection, export, color_by, règles) :
  GlobalId, IfcClass, Name, LongName, Description, ObjectType, Tag, PredefinedType (attributs IFC),
  Classification (« système:code », modifiable), Building, Storey, Space, Zone, Material, Layer (lecture seule),
  Pset_X.Prop (propriété ou quantité d'un Pset précis), *.Prop ou Prop (cherchée dans tous les Psets).
  list_properties montre les champs réellement présents et leur taux de remplissage.

Menu d'audit : les audits ne se lancent jamais tous ensemble. Quand l'utilisateur demande « un audit » (ou « tous les
audits »), appeler list_audits et afficher son message_utilisateur (familles : audit général de la maquette, contrôle
du nommage, conflits, réglementaire — PMR, dégagements, incendie —, cahier des charges, contrôles avancés), puis lui
faire CHOISIR un audit. Sans cahier des charges importé, demander s'il souhaite un contrôle sur un cahier des charges
à déposer (GED BIMData → import_data_requirements, ou gabarit requirements_template). Puis, en deux temps :
  1. run_audit(audit) sans jeton → hypotheses : présenter les paramètres (valeur retenue, défaut, plage, source), le
     référentiel et la méthode ; poser les questions a_demander (référentiel PMR, référentiel incendie, EAS…) ;
     proposer de modifier les valeurs (set_audit_parameters ou page à barres glissantes reglages_url) ;
  2. après validation explicite des hypothèses : run_audit(audit, hypotheses_validees=jeton_hypotheses).
Audit spécifique CCBIM (toujours proposé en plus dans le menu) : s'il est choisi, demander le CCBIM (chemin du fichier
sur l'ordinateur, document de la GED BIMData ou pièce jointe), read_ccbim (lire toutes les pages), extraire les
attendus contrôlables avec leur source sans rien inventer, set_ccbim_expectations, puis run_audit('ccbim') : présenter
les attendus pour validation, lancer avec le jeton ; livrables : rapport PDF et classeur Excel.
Si la demande nomme déjà un audit (« audite les classifications »), passer directement à l'étape 1 pour cet audit.
À la fin de l'audit : présenter le rapport PDF, puis poser question_suite (topics BCF ou Smart View). L'audit suivant
se choisit et se valide à son tour.
Rapports : PDF à la charte BIMData (synthèse, hypothèses, référentiels, méthode, résultats, recommandations, limites,
avertissement IA) ET classeur Excel (livrable.annexe_excel). Les deux fichiers sont joints au résultat de l'outil
(fichiers, pas des liens) : toujours présenter les deux livrables, le PDF puis l'Excel, chacun avec son chemin local
(chemin_local, dossier des rapports) quand il existe ; ne jamais omettre l'Excel.
L'intelligence de l'audit prime sur le volume : après chaque audit ou contrôle, analyser les résultats comme un
BIM manager (règles fiables ou non et pourquoi : tolérance trop stricte, contacts, données d'entrée manquantes comme le
sens d'ouverture des portes, objets hors champ comme éviers ou gaines ; ce qu'il faut retenir ; prérequis et qui les
porte ; recommandation, notamment ne pas envoyer en BCF des anomalies douteuses), appeler annotate_audit_report pour
placer cette analyse en tête du PDF, et commencer la réponse par elle.
Opérations longues : si une réponse contient job_id (préparation de nombreux topics BCF, envoi d'un gros plan), ne
pas attendre ni relancer : informer l'utilisateur, interroger job_status(job_id) toutes les 20 à 30 s en donnant
l'avancement, puis présenter le résultat. Un envoi interrompu se reprend avec apply_plan (sans doublon).
Topics BCF : une seule image par topic, point de vue d'origine « ifc3d », pour qu'ils s'ouvrent dans le viewer
BIMData avec la maquette, la caméra et la sélection. Si des topics créés auparavant affichent « informations de mise
en page non disponibles » : repair_bcf_topics_for_viewer, montrer le plan, apply_plan après accord.
Après l'envoi de topics BCF (apply_plan, ou résultat de job_status), poser la question de proposition_rapport :
rapport Excel des topics (export_bcf_report) ; ne le produire qu'avec l'accord de l'utilisateur.
GED : après chaque audit, poser la question de proposition_ged (déposer le rapport PDF et l'Excel dans la GED du
projet, dossier proposé par défaut, « Audits BIM/<maquette> », ou un autre nom au choix de l'utilisateur ;
list_ged_folders pour montrer les dossiers existants) ; si accord : prepare_ged_publication, montrer le plan, puis
apply_plan après accord. Tout document (fichier produit ou fichier local) peut aussi être publié dans la GED.
Smart View demandée depuis un contrôle : si prepare_smart_view renvoie etape « choix_selection_smart_view », poser
sa question (sélection par gravité, règle, type, niveau, ou une Smart View par règle / niveau ; la recommandée en
premier) et ne rappeler l'outil qu'avec l'option choisie.
BCF demandés depuis un contrôle : si prepare_bcf_topics renvoie etape « choix_perimetre_bcf », poser sa question
(sélection par gravité, par règle ou par type de conflit, la recommandée en premier) et ne rappeler l'outil qu'avec
l'option choisie ; « tout envoyer » seulement sur demande explicite (perimetre_valide=True).
Après un rapport de contrôle, poser question_suite : topics BCF (proposition_bcf : vue, éléments colorés, coupe et
image) ou Smart View (proposition_smart_view : éléments colorés par criticité) ; si l'utilisateur accepte, appeler
l'outil proposé avec ses arguments, montrer le plan (show_plan), puis apply_plan seulement après accord. Ne
jamais présenter un lien http://127.0.0.1 ou localhost comme lien de téléchargement. Rappeler que les résultats sont une
aide à la décision produite avec assistance d'IA et ne remplacent pas un bureau d'études.

Flux usuels :
  • Sélectionner : select_elements(name, criteria) → show_selection / combine_selections.
  • Exporter : export_selection_excel(selection) → attributs IFC + Psets + Qto, une feuille par classe ;
    generate_cobie(selection?).
  • Publier (uniquement à la demande de l'utilisateur) : prepare_smart_view / prepare_bcf_topics
    → montrer le plan → apply_plan(plan_id, confirm=True) après son accord.
  • Contrôler : check_naming_completeness (règles génériques + custom_rules) ;
    cahier des charges : requirements_template → import_data_requirements → run_requirements_check.
    Chaque contrôle produit un rapport Excel (données contrôlées + « Erreurs à corriger »).
  • Auditer les classifications (toute demande « audit / contrôle des classifications ») : audit_classifications
    → manquantes, incohérences classe IFC / type / format / doublons, propositions de correction. Toujours donner
    le lien du rapport Excel et proposer l'export Excel des éléments concernés (export_selection_excel).
    Corriger : prepare_corrections_from_check(check_id) ou rapport complété → prepare_corrections_from_file.
  • Conflits : run_clash_check(set_a, set_b) pour un couple, run_clash_matrix (matrice par défaut ou fichier
    clash_matrix_template) pour toutes les disciplines.
  • Distances / dégagements : run_distance_check(set_a, set_b, mode, distance_m), run_clearance_check(preset='portes').
  • PMR : run_accessibility_check(referentiel, scope). Incendie : run_fire_check(exits, max_distance_m…). Seuils
    indicatifs, à faire valider : toujours le rappeler.
  • Structure IFC : run_structure_check (S1–S11 : site, étages, contenance, réservations, sens d'ouverture, local hors bâtiment,
    GlobalId, géoréférencement, éléments sans géométrie). Géométrie : run_geometry_quality_check (G1 à G8 :
    locaux, doublons, raccords et appuis de murs, escaliers, garde-corps). L'index géométrique (build_geometry_index) se construit tout seul au
    premier appel ; geometry_info donne boîtes, volume et axes d'éléments.
  • Classer : prepare_classification_update(system, notation, selection|uuids) → apply_plan(plan_id, confirm=True).
    Déclasser (retirer sans remplacer) : prepare_classification_removal(system, notation?, selection|uuids).
  • Corriger (Psets, attributs, classifications) : prepare_corrections_from_check (propositions fiables) ou prepare_corrections_from_file
    (rapport complété, ou export de sélection modifié) → apply_plan(plan_id, confirm=True) → relancer le contrôle.

Trame de réponse (identique pour toutes les demandes, comme les fichiers produits) :
  1. Périmètre — maquette, sélection, phase.
  2. Chiffres clés — repris de « synthese ».
  3. Livrables — le rapport PDF et son classeur Excel (livrable.annexe_excel), chacun avec son titre et son chemin
     local (chemin_local) ou son lien download_url tel quel.
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
  • BCF et Smart Views : jamais de création de sa propre initiative — ni à la fin d'un audit, d'un contrôle ou
    d'une sélection, même quand cela paraît utile. Proposer en une ligne à la fin du travail : « Je peux créer une
    Smart View / un sujet BCF pour ces éléments — voulez-vous ? », puis attendre la demande explicite de
    l'utilisateur avant de préparer le plan. Ensuite : montrer le plan, et n'appeler apply_plan(confirm=True)
    qu'après son accord explicite.
"""

mcp = FastMCP("bimdata-mcp", instructions=INSTRUCTIONS, version=__version__, auth=build_auth_provider())


# ── Exécution des outils ────────────────────────────────────────────────

_EXPECTED = (ValueError, PermissionError, FileNotFoundError, InvalidTargetError, KeyError)


#: Rapports joints au résultat de l'outil (le client reçoit les fichiers eux-mêmes, pas seulement des liens) : le PDF
#: et son classeur Excel en annexe.
MAX_ATTACHED_PDF = 8
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_ATTACHED_MB = 20


def _report_pdfs(call, result) -> list:
    from pathlib import Path

    found: list[Path] = []

    def add(d):
        try:
            p = files.get_file(call.state, d["file_id"])
            if p.exists() and p.stat().st_size <= MAX_ATTACHED_MB * 2**20 and p not in found:
                found.append(p)
        except FileNotFoundError:
            pass

    def walk(x):
        if isinstance(x, dict):
            if str(x.get("fichier") or "").lower().endswith(".pdf") and x.get("file_id"):
                add(x)
                annex = x.get("annexe_excel")
                if isinstance(annex, dict) and str(annex.get("fichier") or "").lower().endswith(".xlsx") \
                        and annex.get("file_id"):
                    add(annex)  # le classeur Excel accompagne toujours le rapport PDF
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(result)
    return found[:MAX_ATTACHED_PDF]


def _with_pdf_attachments(call, result):
    """Joint les rapports produits par l'outil (PDF et son annexe Excel, ressources intégrées) en plus du JSON."""
    if not isinstance(result, dict):
        return result
    pdfs = _report_pdfs(call, result)
    if not pdfs:
        return result
    import json as _json

    from fastmcp.tools.tool import ToolResult
    from fastmcp.utilities.types import File
    from mcp.types import TextContent

    result = dict(result)
    result["rapports_joints"] = [p.name for p in pdfs]
    blocks = [TextContent(type="text", text=_json.dumps(result, ensure_ascii=False, default=str))]
    blocks += [File(path=p, name=p.stem).to_resource_content(
        mime_type="application/pdf" if p.suffix.lower() == ".pdf" else XLSX_MIME) for p in pdfs]
    return ToolResult(content=blocks, structured_content=result)


async def _run(ctx: Context | None, fn, /, **kwargs) -> dict[str, Any]:
    try:
        identity = current_identity()
    except PermissionError as exc:
        raise ToolError(str(exc)) from None
    session_id = (getattr(ctx, "session_id", None) if ctx else None) or "default"
    call = service.Call(identity=identity, session_id=str(session_id))
    try:
        result = await anyio.to_thread.run_sync(functools.partial(fn, call, **kwargs))
        return _with_pdf_attachments(call, result)
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
    severities: Annotated[list[Literal["bloquant", "majeur", "mineur", "info"]] | None, Field(
        description="Depuis un contrôle : seulement ces gravités.")] = None,
    rules: Annotated[list[str] | None, Field(description="Depuis un contrôle : seulement ces règles (A5, S4…).")] = None,
    types: Annotated[list[str] | None, Field(
        description="Depuis un contrôle de conflits : types (doublon, inclus, chevauchement) ou cases de matrice.")] = None,
    storeys: Annotated[list[str] | None, Field(description="Depuis un contrôle : seulement ces niveaux.")] = None,
    split_by: Annotated[Literal["regle", "gravite", "niveau"] | None, Field(
        description="Depuis un contrôle : une Smart View par règle, par gravité ou par niveau.")] = None,
    perimetre_valide: Annotated[bool, Field(
        description="True : tout le contrôle en une Smart View (conformes en vert), choix confirmé par "
                    "l'utilisateur. Sinon, sans filtre, l'outil propose d'abord des sélections.")] = False,
    ctx: Context = None,
) -> dict:
    """À n'appeler que sur demande explicite de l'utilisateur (jamais de sa propre initiative).
    Prépare une Smart View BIMData (coloration dans le viewer) depuis une sélection, plusieurs sélections, un champ ou
    un contrôle. N'écrit rien : retourne un plan à appliquer avec apply_plan."""
    return await _run(ctx, service.prepare_smart_view, title=title, selection=selection, color=color, color_by=color_by,
                      selections=selections, colors=colors, check_id=check_id, severities=severities, rules=rules,
                      types=types, storeys=storeys, split_by=split_by, perimetre_valide=perimetre_valide)


@mcp.tool()
async def repair_bcf_topics_for_viewer(ctx: Context = None) -> dict:
    """Topics BCF déjà créés par le MCP qui ne s'ouvrent pas avec la maquette dans le viewer BIMData (avertissement
    « informations de mise en page non disponibles ») : prépare un plan de réparation (origine des points de vue
    « ifc3d », suppression des vues complémentaires illustrées). N'écrit rien : apply_plan après accord."""
    return await _run(ctx, service_bcf.prepare_bcf_viewer_repair)


@mcp.tool()
async def prepare_bcf_topics(
    title: Annotated[str | None, Field(description="Préfixe des titres (depuis un contrôle, le titre est "
                                                     "« [règle] résumé — niveau »).")] = None,
    selection: SelName = None,
    description: str = "",
    priority: Literal["Critical", "High", "Medium", "Low", "Information"] = "Medium",
    topic_type: Annotated[str | None, Field(description="Type de topic ; depuis un contrôle : Clash pour les conflits "
                                                         "(s'il existe dans le projet), Issue sinon.")] = None,
    labels: list[str] | None = None,
    assigned_to: Annotated[str | None, Field(description="E-mail d'un membre du projet.")] = None,
    due_date: Annotated[str | None, Field(description="AAAA-MM-JJ")] = None,
    group_by: Annotated[str | None, Field(description="Un topic par valeur de ce champ (ex. 'Storey').")] = None,
    one_topic_per_element: bool = False,
    check_id: Annotated[str | None, Field(description="Créer les topics à partir des anomalies d'un contrôle.")] = None,
    check_group_by: Annotated[Literal["requirement", "severity", "element", "status", "issue"], Field(
        description="Regroupement des anomalies d'un contrôle ; 'issue' = un topic par anomalie (recommandé pour un "
                    "contrôle géométrique ou un clash).")] = "requirement",
    max_topics: int = 100,
    frame_bbox: Annotated[bool, Field(description="Cadrer la zone de l'anomalie (intersection A∩B pour un conflit).")] = True,
    isolate: Annotated[bool, Field(description="Ne laisser visibles que les éléments en cause.")] = False,
    section_box: Annotated[bool | None, Field(
        description="Coupe. Défaut (auto) : boîte autour de la zone pour conflits, structure et géométrie ; plan "
                    "horizontal à l'étage + 1,20 m pour PMR, dégagements et incendie. False : aucune.")] = None,
    snapshot: Annotated[bool, Field(description="Image PNG rendue côté serveur dans chaque point de vue.")] = True,
    camera: Annotated[Literal["auto", "iso", "plan", "elevation"], Field(
        description="Caméra principale : auto (réglage de la famille), iso (perspective aux trois quarts), plan, "
                    "elevation.")] = "auto",
    field_of_view_deg: Annotated[float, Field(gt=5, lt=170, description="Angle vertical de la perspective.")] = 60.0,
    extra_viewpoint: Annotated[Literal["auto", "none", "plan", "section"], Field(
        description="Vue supplémentaire, sans image (pour Solibri, BIMcollab…) : none (défaut ; le viewer BIMData "
                    "n'ouvre directement qu'un topic à une seule image), auto, plan, section.")] = "none",
    max_components: Annotated[int, Field(ge=1, le=5000, description="Éléments au plus par point de vue ; au-delà, "
                                                                     "topic découpé par étage puis par lots.")] = 1000,
    dedupe: Annotated[bool, Field(description="Rapprocher des topics existants par clé d'anomalie : pas de doublon, "
                                               "commentaire « toujours présent », Resolved proposé si disparue.")] = True,
    severities: Annotated[list[Literal["bloquant", "majeur", "mineur", "info"]] | None, Field(
        description="Depuis un contrôle : ne transmettre que ces sévérités (ex. ['bloquant', 'majeur']).")] = None,
    rules: Annotated[list[str] | None, Field(
        description="Depuis un contrôle : ne transmettre que ces règles (ex. ['A5', 'S4', 'CLASH']).")] = None,
    types: Annotated[list[str] | None, Field(
        description="Depuis un contrôle de conflits : types (doublon, inclus, chevauchement) ou cases de matrice "
                    "(ex. 'ARC-STR').")] = None,
    perimetre_valide: Annotated[bool, Field(
        description="True : tout transmettre sans filtre, choix confirmé par l'utilisateur. Sinon, au-delà de 20 "
                    "topics sans filtre, l'outil renvoie d'abord le choix du périmètre.")] = False,
    ctx: Context = None,
) -> dict:
    """À n'appeler que sur demande explicite de l'utilisateur (jamais de sa propre initiative).
    Prépare des topics BCF (issues). Depuis un contrôle (check_id), chaque topic porte un point de vue complet, comme
    une diapositive d'anomalie Solibri : caméra calculée sur la zone, sélection de tous les éléments en cause colorés
    selon leur rôle (rouge : A / cible ; orange : B / obstacle ; bleu : contexte), coupe, chemin mesuré (F3, F5), image
    PNG rendue côté serveur ; clé d'anomalie stable (pas de doublon à la relance). Sans contrôle : un topic pour la
    sélection, un par valeur, un par élément. N'écrit rien : plan à montrer (show_plan, miniatures) puis à appliquer
    avec apply_plan(confirm=True) après accord explicite."""
    return await _run(ctx, service.prepare_bcf_topics, title=title, selection=selection, description=description,
                      priority=priority, topic_type=topic_type, labels=labels, assigned_to=assigned_to, due_date=due_date,
                      group_by=group_by, one_topic_per_element=one_topic_per_element, check_id=check_id,
                      check_group_by=check_group_by, max_topics=max_topics, frame_bbox=frame_bbox, isolate=isolate,
                      section_box=section_box, snapshot=snapshot, camera=camera, field_of_view_deg=field_of_view_deg,
                      extra_viewpoint=extra_viewpoint, max_components=max_components, dedupe=dedupe,
                      severities=severities, rules=rules, types=types, perimetre_valide=perimetre_valide)


@mcp.tool()
async def prepare_classification_update(
    system: Annotated[str, Field(description="Système de classification, ex. 'uniformat'.")],
    notation: Annotated[str, Field(description="Code à affecter, ex. 'D5010200'.")],
    selection: SelName = None,
    uuids: Annotated[list[str] | None, Field(description="GlobalId des éléments (si pas de sélection).")] = None,
    title: Annotated[str | None, Field(description="Libellé du code, utilisé s'il doit être créé.")] = None,
    replace_existing: Annotated[bool, Field(description="Retirer l'ancien code du même système.")] = True,
    ctx: Context = None,
) -> dict:
    """Prépare l'affectation d'un code de classification à des éléments (sélection ou GlobalId) : code trouvé ou créé,
    associé, ancien code du même système retiré. N'écrit rien : retourne un plan à appliquer avec apply_plan."""
    return await _run(ctx, service.prepare_classification_update, selection=selection, uuids=uuids, system=system,
                      notation=notation, title=title, replace_existing=replace_existing)


@mcp.tool()
async def prepare_classification_removal(
    system: Annotated[str, Field(description="Système de classification, ex. 'uniformat'.")],
    notation: Annotated[str | None, Field(description="Code à retirer, ex. 'D4090100'. Omis : tous les codes du système.")] = None,
    selection: SelName = None,
    uuids: Annotated[list[str] | None, Field(description="GlobalId des éléments (si pas de sélection).")] = None,
    ctx: Context = None,
) -> dict:
    """Prépare le retrait d'une classification d'éléments (sélection ou GlobalId), sans en affecter de nouvelle :
    le code précis, ou tous les codes du système si notation est omise. Le code reste dans le projet.
    N'écrit rien : retourne un plan à appliquer avec apply_plan."""
    return await _run(ctx, service.prepare_classification_removal, selection=selection, uuids=uuids, system=system,
                      notation=notation)


@mcp.tool(annotations={"destructiveHint": True})
async def apply_plan(
    plan_id: str,
    confirm: Annotated[bool, Field(description="True uniquement après accord explicite de l'utilisateur.")] = False,
    ctx: Context = None,
) -> dict:
    """Applique un plan d'écriture (Smart View, BCF, classifications, corrections de propriétés) sur BIMData. Sans confirm=True : aperçu seul."""
    return await _run(ctx, service.apply_plan, plan_id=plan_id, confirm=confirm)


@mcp.tool(annotations={"readOnlyHint": True})
async def list_plans(limit: int = 20, ctx: Context = None) -> dict:
    """Liste les plans d'écriture préparés / appliqués."""
    return await _run(ctx, service.list_plans, limit=limit)


@mcp.tool(annotations={"readOnlyHint": True})
async def export_bcf_report(
    plan_id: Annotated[str, Field(description="Plan de topics BCF (prepare_bcf_topics), envoyé ou non.")],
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Rapport Excel à la charte des topics BCF d'un plan : synthèse par priorité, règle, niveau et statut, puis un
    topic par ligne avec sa miniature, son constat, sa mesure et son seuil, ses éléments (nom, classe, rôle,
    GlobalId), sa clé d'anomalie et son GUID. N'écrit rien dans BIMData. À proposer après l'envoi des topics."""
    return await _run(ctx, service.export_bcf_report, plan_id=plan_id, title=title)


@mcp.tool(annotations={"readOnlyHint": True})
async def job_status(
    job_id: Annotated[str, Field(description="Identifiant renvoyé par une opération lancée en tâche de fond.")],
    ctx: Context = None,
) -> dict:
    """Avancement d'une opération longue lancée en tâche de fond (préparation de nombreux topics BCF avec images,
    envoi d'un plan BCF) : statut, fait / total, temps restant estimé, puis résultat. À interroger toutes les 20 à 30 s."""
    return await _run(ctx, service.job_status, job_id=job_id)


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
async def audit_classifications(
    selection: SelName = None,
    system: Annotated[str | None, Field(description="Système audité (ex. 'Uniformat'). Défaut : le plus utilisé dans la maquette.")] = None,
    title: Str = None,
    auto_fill_threshold: Annotated[float, Field(ge=0, le=1, description="Propositions préremplies dans le rapport à partir de cette confiance.")] = 0.8,
    ctx: Context = None,
) -> dict:
    """Audit des classifications (à utiliser pour toute demande « auditer / contrôler les classifications ») :
    classifications manquantes, plusieurs codes du même système, format invalide, code incohérent avec la classe IFC
    (familles UniFormat), code différent du reste du type — avec une proposition de correction par anomalie, reprise
    des éléments du même type. Produit toujours le rapport Excel (lien download_url). N'écrit rien dans BIMData."""
    return await _run(ctx, service.audit_classifications, selection=selection, system=system, title=title,
                      auto_fill_threshold=auto_fill_threshold)


# ── Géométrie, structure IFC et menu d'audit ───────────────────────────


@mcp.tool(annotations={"readOnlyHint": True})
async def build_geometry_index(
    refresh: Annotated[bool, Field(description="Recalculer même si l'index de cette version est en cache.")] = False,
    ctx: Context = None,
) -> dict:
    """Construit (ou relit) l'index géométrique de la maquette active depuis son fichier IFC : boîte alignée (AABB),
    boîte orientée (OBB), volume et maillage par élément, en mètres. Calculé une fois par version de fichier ; les
    outils géométriques l'appellent d'eux-mêmes. Retourne l'état du cache, le nombre d'éléments indexés et sans
    géométrie, l'unité détectée et la durée."""
    return await _run(ctx, service_geometry.build_geometry_index, refresh=refresh)


@mcp.tool(annotations={"readOnlyHint": True})
async def geometry_info(
    selection: SelName = None,
    uuids: Annotated[list[str] | None, Field(description="GlobalId des éléments (si pas de sélection).")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ctx: Context = None,
) -> dict:
    """Géométrie d'éléments : AABB, OBB, dimensions, volume, maillage fermé oui/non, axes locaux (m, repère projet)."""
    return await _run(ctx, service_geometry.geometry_info, selection=selection, uuids=uuids, limit=limit)


@mcp.tool(annotations={"readOnlyHint": True})
async def run_structure_check(
    selection: SelName = None,
    checks: Annotated[list[str] | None, Field(description="Règles à lancer (S1…S11). Vide = toutes.")] = None,
    expected_buildings: Annotated[int, Field(ge=1, description="Nombre de bâtiments attendus (S1).")] = 1,
    s4_thresholds: Annotated[dict | None, Field(
        description="Seuils de S4 (élément posé sur son étage), en m : base_tolerance_m (0.35, assise d'un élément "
                    "posé), carried_tolerance_m (0.35, dessus d'un élément porté sous son niveau ; à augmenter sous "
                    "une dalle épaisse), slab_top_below_m (0.35) / slab_top_above_m (0.10) (dessus de dalle autour "
                    "du niveau), ambiguous_gap_m (0.70, étages trop proches : non concluant).")] = None,
    unique_property: Annotated[str | None, Field(
        description="Identifiant projet à contrôler en unicité (S8), ex. 'Pset_X.BatID' ou 'BatID'.")] = None,
    door_direction_property: Annotated[str | None, Field(
        description="Propriété qui porte le sens d'ouverture si OperationType n'est pas renseigné (S7).")] = None,
    georef_expected: Annotated[dict | None, Field(
        description="Valeurs attendues (S9) : epsg, eastings, northings, orthogonal_height, latitude, longitude, "
                    "tolerance_m (défaut 1).")] = None,
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Contrôle de structuration et d'intégrité IFC (règles S1 à S11) : un site et un bâtiment, éléments contenus dans
    un étage, étages (doublons, vides, nom IFC ≠ libellé affiché, altitudes), éléments hors de la tranche de leur
    étage, portes et fenêtres sans réservation ou sur un autre étage que leur mur, sens d'ouverture, GlobalId uniques,
    géoréférencement, éléments sans géométrie. Rapport Excel + check_id. N'écrit rien dans BIMData."""
    return await _run(ctx, service_geometry.run_structure_check, selection=selection, checks=checks,
                      expected_buildings=expected_buildings, s4_thresholds=s4_thresholds,
                      unique_property=unique_property, door_direction_property=door_direction_property,
                      georef_expected=georef_expected, title=title)


@mcp.tool(annotations={"readOnlyHint": True})
async def run_geometry_quality_check(
    selection: SelName = None,
    checks: Annotated[list[str] | None, Field(description="Règles G1 à G8. Vide = toutes.")] = None,
    thresholds: Annotated[dict | None, Field(
        description="Seuils : space_min_area_m2 (1.0), space_min_height_m (2.50), shaft_pattern (regex des gaines "
                    "sur LongName), space_overlap_min_m3 (0.01), space_overlap_exclude_pattern (regex LongName des "
                    "locaux à ignorer en G2, ex. surfaces IPMS), wall_join_gap_m (0.02), wall_support_gap_m (0.02), "
                    "wall_support_search_m (1.0), stair_riser_max_m (0.17), stair_tread_min_m (0.28), "
                    "stair_blondel_min_m (0.60), stair_blondel_max_m (0.64), stair_headroom_min_m (2.00), "
                    "railing_min_height_m (1.00), railing_name_tolerance_m (0.05).")] = None,
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Contrôle géométrie et modélisation : G1 locaux trop petits ou trop bas (gaines pour information), G2 locaux qui
    se chevauchent, G3 locaux à maillage ouvert, G4 objets en double, G5 murs non raccordés, G6 murs sans appui,
    G7 escaliers (marche, giron, 2h + g, échappée), G8 garde-corps. Rapport Excel + check_id. N'écrit rien."""
    return await _run(ctx, service_geometry.run_geometry_quality_check, selection=selection, checks=checks,
                      thresholds=thresholds, title=title)


SetSpec = Annotated[str | dict | None, Field(
    description="Ensemble de composants : discipline ('Fluides', 'Structure', 'Architecture'), nom de sélection "
                "('selection:nom' ou le nom seul), 'classes:IfcDuctSegment,IfcPipeSegment', ou critères de "
                "select_elements (ex. {'ifc_classes': ['IfcWall'], 'storeys': ['R+1']}). Vide = tous les éléments.")]


@mcp.tool(annotations={"readOnlyHint": True})
async def run_clash_check(
    set_a: SetSpec = None,
    set_b: SetSpec = None,
    tol_h_m: Annotated[float | None, Field(ge=0, description="Tolérance horizontale : pénétration horizontale sous la "
                                                             "valeur = pas d'anomalie (0 = clash dur). Défaut : réglage "
                                                             "enregistré pour la maquette, sinon 0,005.")] = None,
    tol_v_m: Annotated[float | None, Field(ge=0, description="Tolérance verticale (idem). Défaut : réglage enregistré, "
                                                             "sinon 0,005.")] = None,
    tol_volume_m3: Annotated[float | None, Field(ge=0, description="Tolérance de volume (facultative, fiable pour "
                                                                   "les solides fermés).")] = None,
    types: Annotated[list[Literal["duplicate", "inside", "overlap"]] | None, Field(
        description="Types recherchés : duplicate (doublon), inside (inclus), overlap (chevauchement). Vide = tous.")] = None,
    ignore_same_system: Annotated[bool, Field(description="Ignorer deux éléments du même système (réseau).")] = False,
    ignore_same_layer: Annotated[bool, Field(description="Ignorer deux éléments du même calque.")] = False,
    exceptions: Annotated[list[str] | dict | None, Field(
        description="Exceptions actives (défaut : toutes) : traversee_mur, traversee_dalle (tuyau ou gaine à 89–90°, "
                    "angle_min_deg, min_protrusion_m), luminaire_faux_plafond, bouche_affleurante (max_depth_m), "
                    "isolant_reseau (isolant IfcCovering autour de son propre réseau, max_angle_deg, margin_m ; "
                    "vaut aussi pour les doublons et inclusions). "
                    "Liste de noms, [] pour aucune, ou {nom: {enabled, …}}.")] = None,
    contact_m: Annotated[float | None, Field(
        ge=0, le=0.005, description="Contacts à plat : surfaces à moins de cette distance (m) = contact, pas conflit "
                                    "(0.001 conseillé). Défaut 0 : méthode d'origine. Stabilise les contacts fenêtre / "
                                    "poteau mais change sensiblement le résultat (à valider).")] = None,
    severity_rules: Annotated[list[dict] | None, Field(
        description="Règles de sévérité : [{when: {clash_type, size (petit|moyen|grand), orientation "
                    "(parallele|perpendiculaire|oblique), class_a, class_b, discipline_a, discipline_b}, "
                    "severity}] ; la plus grave l'emporte.")] = None,
    discipline_property: Annotated[str | None, Field(
        description="Propriété qui porte la discipline sur ce projet (sinon déduite de la classe).")] = None,
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Détection de conflits entre deux ensembles (règle General Intersection de Solibri) : doublons, éléments inclus,
    chevauchements mesurés (pénétrations, volume), tolérances, exceptions (traversées à 90°, luminaire en faux
    plafond, bouche affleurante, isolant autour de son réseau), sévérité et regroupement en 6 catégories. Rapport Excel (dont feuille Volumes) +
    check_id. N'écrit rien dans BIMData."""
    return await _run(ctx, service_geometry.run_clash_check, set_a=set_a, set_b=set_b, tol_h_m=tol_h_m,
                      tol_v_m=tol_v_m, tol_volume_m3=tol_volume_m3, types=types, ignore_same_system=ignore_same_system,
                      ignore_same_layer=ignore_same_layer, exceptions=exceptions, severity_rules=severity_rules,
                      discipline_property=discipline_property, title=title, contact_m=contact_m)


@mcp.tool(annotations={"readOnlyHint": True})
async def run_clash_matrix(
    matrix: Annotated[list[dict] | None, Field(
        description="Cases : [{id, a, b, types, tol_h_m, tol_v_m, tol_volume_m3, severity, ignore_same_system, "
                    "ignore_same_layer, exceptions, severity_rules}] (a / b : même syntaxe que set_a). Vide et sans "
                    "fichier = matrice par défaut (Structure × Structure doublons, Architecture × Structure, "
                    "Fluides × Structure, Fluides × Architecture, Fluides × Fluides).")] = None,
    file_id: Annotated[str | None, Field(description="Matrice Excel déposée (gabarit clash_matrix_template).")] = None,
    bimdata_document_id: Annotated[int | None, Field(description="Matrice Excel de la GED BIMData.")] = None,
    discipline_property: Annotated[str | None, Field(description="Propriété portant la discipline.")] = None,
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Détection de conflits pour toutes les cases d'une matrice de disciplines (règle Solibri n°245) : un seul
    check_id, la case dans chaque anomalie, une paire signalée une seule fois. N'écrit rien dans BIMData."""
    return await _run(ctx, service_geometry.run_clash_matrix, matrix=matrix, file_id=file_id,
                      bimdata_document_id=bimdata_document_id, discipline_property=discipline_property, title=title)


@mcp.tool(annotations={"readOnlyHint": True})
async def compare_clash_results(
    check_id: Annotated[str, Field(description="Contrôle de conflits du MCP (run_clash_check, mêmes réglages).")],
    file_id: Annotated[str | None, Field(description="Export de l'autre outil déposé (BCF .bcfzip, Excel ou CSV "
                                                     "avec les GlobalId des deux éléments de chaque conflit).")] = None,
    bimdata_document_id: Annotated[int | None, Field(description="Export de la GED BIMData.")] = None,
    tool_name: Annotated[str, Field(description="Nom de l'autre outil (libellés du rapport).")] = "Solibri",
    objective_pct: Annotated[float, Field(ge=0, description="Écart maximal visé (%).")] = 5.0,
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Compare paire par paire un contrôle de conflits du MCP avec les résultats d'un autre outil (Solibri) lancé sur
    le même fichier IFC avec les mêmes réglages : conflits communs, MCP seulement, autre outil seulement, écarts (%)
    contre l'objectif. Rapport Excel. N'écrit rien dans BIMData."""
    return await _run(ctx, service_geometry.compare_clash_results, check_id=check_id, file_id=file_id,
                      bimdata_document_id=bimdata_document_id, tool_name=tool_name, objective_pct=objective_pct,
                      title=title)


@mcp.tool(annotations={"readOnlyHint": True})
async def clash_matrix_template(ctx: Context = None) -> dict:
    """Gabarit Excel de matrice de conflits (prérempli avec la matrice par défaut), à compléter puis à passer à
    run_clash_matrix(file_id=…)."""
    return await _run(ctx, service_geometry.clash_matrix_template)


@mcp.tool(annotations={"readOnlyHint": True})
async def run_distance_check(
    set_a: SetSpec = None,
    set_b: SetSpec = None,
    mode: Annotated[Literal["min", "max"], Field(
        description="min : anomalie si un B est plus proche que distance_m ; max : si le B le plus proche est plus "
                    "loin.")] = "min",
    distance_m: Annotated[float, Field(ge=0, description="Distance seuil (m), ex. 0.5 entre robinet et prise.")] = 0.5,
    direction: Annotated[Literal["any", "horizontal", "below", "above"], Field(
        description="any : distance 3D ; horizontal : en plan ; below / above : B sous / au-dessus de A.")] = "any",
    same_space_only: Annotated[bool, Field(description="Ne comparer que des éléments du même local.")] = False,
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Distance entre deux ensembles de composants (équivalent règle Solibri 222), mesurée sur les maillages.
    Rapport Excel + check_id. N'écrit rien dans BIMData."""
    return await _run(ctx, service_geometry.run_distance_check, set_a=set_a, set_b=set_b, mode=mode,
                      distance_m=distance_m, direction=direction, same_space_only=same_space_only, title=title)


@mcp.tool(annotations={"readOnlyHint": True})
async def run_clearance_check(
    preset: Annotated[Literal["portes", "fenetres", "equipements", "detecteurs"] | None, Field(
        description="Préréglage : portes (1,00 m côté ouverture) ; fenetres, equipements, detecteurs : distances à "
                    "fournir par le projet.")] = None,
    targets: SetSpec = None,
    obstacles: SetSpec = None,
    front_m: Annotated[float | None, Field(ge=0, description="Profondeur devant (côté ouverture).")] = None,
    back_m: Annotated[float | None, Field(ge=0, description="Profondeur derrière.")] = None,
    side_m: Annotated[float | None, Field(ge=0, description="Débord de chaque côté.")] = None,
    height_m: Annotated[float | None, Field(ge=0, description="Hauteur de la zone depuis la base de la cible.")] = None,
    width: Annotated[str | float | None, Field(description="'component' (largeur de la cible, défaut) ou valeur (m).")] = None,
    use_opening_side: Annotated[bool | None, Field(
        description="Zone côté ouverture seulement (OperationType battant) ; inconnu = les deux côtés.")] = None,
    tolerance_m: Annotated[float | None, Field(ge=0, description="Empiètement toléré (m). Défaut : réglage enregistré "
                                                                 "pour la maquette, sinon 0,05 (une boîte aux lettres "
                                                                 "ou une conduite plaquée au mur ne gênent pas).")] = None,
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Espace libre devant / autour de composants (équivalent règles Solibri 226 et 248) : zone construite dans le
    repère de chaque cible, obstacles de l'index, empiètement mesuré sur les maillages. N'écrit rien dans BIMData."""
    return await _run(ctx, service_geometry.run_clearance_check, preset=preset, targets=targets, obstacles=obstacles,
                      front_m=front_m, back_m=back_m, side_m=side_m, height_m=height_m, width=width,
                      use_opening_side=use_opening_side, tolerance_m=tolerance_m, title=title)


@mcp.tool(annotations={"readOnlyHint": True})
async def run_accessibility_check(
    referentiel: Annotated[Literal["BHC 2015", "ERP 2017", "personnalisé"] | None, Field(
        description="Jeu de seuils (valeurs indicatives, à valider). Défaut : réglage enregistré pour la maquette ; "
                    "sans réglage, à demander à l'utilisateur.")] = None,
    scope: SetSpec = None,
    thresholds: Annotated[dict | None, Field(
        description="Seuils : door_width_entry_m, door_width_interior_m, turning_diameter_m, turning_height_m (0.70), "
                    "riser_max_m, tread_min_m, a2_width_m / a2_push_m / a2_pull_m (A2, à paramétrer), "
                    "a4_width_m / a4_depth_m (A4, à paramétrer), threshold_step_max_m (A7, 0.02).")] = None,
    checks: Annotated[list[str] | None, Field(description="A1…A7. Vide = toutes.")] = None,
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Accessibilité PMR : A1 largeur des portes, A2 espace de manœuvre de porte, A3 cercle de demi-tour de 1,50 m
    dans les locaux désignés, A4 espace d'usage devant WC et lavabo, A5 marches et giron, A6 HandicapAccessible,
    A7 ressaut au seuil (information). scope = locaux et portes accessibles désignés par le projet (A3 et A6 ne
    portent que sur les locaux de scope). Aide à la décision, pas une attestation."""
    return await _run(ctx, service_geometry.run_accessibility_check, referentiel=referentiel, scope=scope,
                      thresholds=thresholds, checks=checks, title=title)


@mcp.tool(annotations={"readOnlyHint": True})
async def run_fire_check(
    compartments: Annotated[str | None, Field(
        description="'zones' (IfcZone des locaux) ou propriété des locaux portant le compartiment.")] = "zones",
    required_ratings: Annotated[dict | None, Field(
        description="Degrés exigés par classe ({'IfcDoor': 'EI30', 'IfcWall': 'EI60'}) ou par zone "
                    "({'zone:Logements': {'IfcWall': 'REI60'}}).")] = None,
    compartment_rating: Annotated[str | None, Field(description="Degré exigé des parois entre compartiments (F2).")] = None,
    door_rating: Annotated[str | None, Field(description="Degré exigé des portes de ces parois (F2).")] = None,
    exits: Annotated[str | dict | None, Field(
        description="Sorties : 'auto' (portes extérieures des niveaux d'accès + portes d'accès aux escaliers de chaque "
                    "étage ; jamais une porte de balcon), ou ensemble (même syntaxe que set_a).")] = "auto",
    access_storeys: Annotated[list[str] | str | None, Field(
        description="Niveaux d'accès pour exits='auto' (plusieurs pour un bâtiment en escalier). Défaut : étages dont "
                    "le nom commence par RDC, Rez ou Ground, sinon l'étage d'altitude la plus proche de 0. Les niveaux "
                    "retenus sont listés dans le rapport.")] = None,
    stair_door_distance_m: Annotated[float | None, Field(
        ge=0, description="exits='auto' : compter aussi les portes à moins de cette distance (m, en plan) d'un "
                          "escalier situé hors de tout local. Désactivé par défaut (faux positifs mesurés).")] = None,
    stairs: SetSpec = None,
    max_distance_m: Annotated[float | None, Field(ge=0, description="Distance maximale indicative à la sortie (F3).")] = None,
    distance_method: Annotated[str, Field(
        description="F3 : 'cheminement' (défaut, distance de marche à travers les locaux et les portes de l'étage, "
                    "vol d'oiseau en complément) ou 'vol_oiseau' (vol d'oiseau et orthogonale seulement).")] = "cheminement",
    path_cell_m: Annotated[float | None, Field(gt=0, le=1, description="F3 : maille de la grille de marche (m, défaut 0.10).")] = None,
    f3_referentiel: Annotated[str | None, Field(
        description="Référentiel de F3/F5 : 'ERP' (CO 43 au niveau d'accès : 50 m, 30 m sans choix de sortie ; CO 49 "
                    "en étage : 40 m vers un escalier protégé, 30 m en cul-de-sac ou vers un escalier non protégé ; "
                    "culs-de-sac ≤ 10 m), 'ERP_J', 'habitation_3A' (10 m depuis la porte palière), 'habitation_3B_4' "
                    "(25 m), 'code_travail' (40 m, 10 m en cul-de-sac), 'personnalise' (max_distance_m). Seuils "
                    "indicatifs.")] = None,
    f3_origin: Annotated[str | None, Field(
        description="Point de départ de F3 : 'local' (point le plus éloigné du local) ou 'porte_paliere' (porte du "
                    "logement). Défaut : celui du référentiel.")] = None,
    protected_stairs: Annotated[str | dict | None, Field(
        description="Escaliers protégés (ERP) : 'all', 'none', ou ensemble (même syntaxe que set_a : locaux "
                    "d'escalier, portes, circulations horizontales protégées). Inconnu : entre 30 et 40 m, « non "
                    "concluant ».")] = None,
    landing_doors: Annotated[str | dict | None, Field(
        description="Portes palières (habitation), même syntaxe que set_a. Défaut : détectées (nom « palière / "
                    "Ppal », ou porte entre un logement et une circulation commune).")] = None,
    eas: Annotated[str | dict | None, Field(
        description="Espaces d'attente sécurisés (F6), même syntaxe que set_a : locaux ou paliers désignés, ou locaux "
                    "du compartiment voisin en cas de transfert horizontal.")] = None,
    effectif: Annotated[int | dict | None, Field(
        description="Effectif du public (F6, capacité des EAS) : nombre, ou {nom d'étage: nombre}.")] = None,
    dead_end_max_m: Annotated[float | None, Field(ge=0, description="Longueur maximale de cul-de-sac (F5). Défaut : "
                                                                     "celle du référentiel (ERP : 10 m).")] = None,
    pmr_door_min_m: Annotated[float | None, Field(gt=0, description="F6 : largeur minimale d'une porte franchissable "
                                                                     "en fauteuil (défaut 0,90 m, comme A1).")] = None,
    open_stairs: Annotated[bool | None, Field(
        description="Escaliers ouverts (local contenant un escalier, sans porte) comptés comme dégagements non "
                    "protégés (30 m, CO 49) : on est sorti dès qu'on y entre. Défaut : ceux dont la cage rejoint le "
                    "niveau d'accès ; True : tous ; False : aucun.")] = None,
    exit_up: Annotated[int | dict | None, Field(description="Unités de passage exigées des sorties (F4).")] = 1,
    stairs_up: Annotated[int | dict | None, Field(description="Unités de passage exigées des escaliers (F4).")] = 1,
    checks: Annotated[list[str] | None, Field(description="F1…F6. Vide = toutes.")] = None,
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Sécurité incendie et évacuation : F1 FireRating présent et suffisant, F2 parois et portes entre compartiments,
    F3 distance suivant l'axe des circulations, par référentiel (ERP, type J, habitation, code du travail), F5 culs-de-sac,
    F6 espaces d'attente sécurisés (accès sans marche ni porte étroite, capacité), F4 largeur des sorties et escaliers en
    unités de passage. Données absentes signalées comme telles. Aide à la décision, pas une attestation."""
    return await _run(ctx, service_geometry.run_fire_check, compartments=compartments,
                      required_ratings=required_ratings, compartment_rating=compartment_rating,
                      door_rating=door_rating, exits=exits, access_storeys=access_storeys,
                      stair_door_distance_m=stair_door_distance_m, stairs=stairs,
                      max_distance_m=max_distance_m, distance_method=distance_method, path_cell_m=path_cell_m,
                      open_stairs=open_stairs, f3_referentiel=f3_referentiel, f3_origin=f3_origin,
                      protected_stairs=protected_stairs, landing_doors=landing_doors, eas=eas, effectif=effectif,
                      dead_end_max_m=dead_end_max_m, pmr_door_min_m=pmr_door_min_m,
                      exit_up=exit_up, stairs_up=stairs_up, checks=checks, title=title)


@mcp.tool(annotations={"readOnlyHint": True})
async def list_audits(ctx: Context = None) -> dict:
    """Menu des audits par famille (audit général, nommage, conflits, réglementaire, cahier des charges, contrôles
    avancés) avec l'état des prérequis : pret, partiel (avec la raison) ou bloque, et message_utilisateur, en
    français, prêt à afficher. À appeler quand l'utilisateur demande un audit sans préciser lequel."""
    return await _run(ctx, service_geometry.list_audits)


@mcp.tool(annotations={"readOnlyHint": True})
async def audit_parameters(
    audits: Annotated[list[str] | None, Field(description="Audits concernés (ex. ['c2_clash']). Vide = tous.")] = None,
    ctx: Context = None,
) -> dict:
    """Paramètres de calcul des audits pour la maquette active : valeur retenue, valeur par défaut, plage, unité,
    justification. Renvoie aussi le lien de la page de réglage à barres glissantes (reglages_url)."""
    return await _run(ctx, service_geometry.audit_parameters, audits=audits)


@mcp.tool()
async def set_audit_parameters(
    values: Annotated[dict, Field(description="Valeurs par clé « audit.paramètre », ex. {'c2_clash.tol_h_m': 0.01, "
                                              "'c7_incendie.f3_referentiel': 'ERP'} ; null = revenir au défaut.")],
    reset: Annotated[bool, Field(description="Repartir des valeurs par défaut avant d'appliquer values.")] = False,
    ctx: Context = None,
) -> dict:
    """Enregistre des réglages d'audit pour la maquette active (contrôlés : plage, choix admis). Ils s'appliquent au
    prochain run_audit et figurent au chapitre « Hypothèses » du rapport. N'écrit rien dans BIMData."""
    return await _run(ctx, service_geometry.set_audit_parameters, values=values, reset=reset)


@mcp.tool(annotations={"readOnlyHint": True})
async def list_ged_folders(ctx: Context = None) -> dict:
    """Dossiers de la GED du projet BIMData actif (chemins « A/B/C »), pour choisir où déposer des documents."""
    return await _run(ctx, service.list_ged_folders)


@mcp.tool()
async def prepare_ged_publication(
    check_id: Annotated[str | None, Field(description="Audit dont déposer les livrables (rapport PDF + Excel).")] = None,
    file_ids: Annotated[list[str] | None, Field(description="Fichiers produits par le serveur (file_id).")] = None,
    file_paths: Annotated[list[str] | None, Field(
        description="Fichiers de l'ordinateur de l'utilisateur (serveur lancé en local seulement) : PDF, Excel, "
                    "Word, IFC, BCF, images, zip.")] = None,
    folder: Annotated[str | None, Field(
        description="Dossier de la GED, chemin « A/B/C » (existant ou créé). Défaut : « Audits BIM/<maquette> ».")] = None,
    ctx: Context = None,
) -> dict:
    """Prépare le dépôt de documents dans la GED du projet BIMData (dossiers créés au besoin, documents déjà
    présents dans le dossier non redéposés). N'écrit rien : plan à montrer, puis apply_plan après accord."""
    return await _run(ctx, service.prepare_ged_publication, check_id=check_id, file_ids=file_ids,
                      file_paths=file_paths, folder=folder)


@mcp.tool()
async def annotate_audit_report(
    check_id: str,
    analyse: Annotated[dict, Field(description=(
        "Lecture critique de l'audit, en français : {'a_retenir': '…', 'fiabilite': [{'regle': 'A2', 'libelle': "
        "'Zone de manœuvre', 'verdict': 'fiable | à confirmer | peu fiable', 'raisons': ['…']}], 'prerequis': "
        "[{'action': '…', 'responsable': 'Architecte'}], 'recommandation': '…'}"))],
    ctx: Context = None,
) -> dict:
    """Place l'analyse d'expert de l'audit (fiabilité par règle, ce qu'il faut retenir, prérequis, recommandation)
    dans un encart « L'essentiel » en tête du rapport PDF, et renvoie le rapport mis à jour (PDF + Excel)."""
    return await _run(ctx, service.annotate_audit_report, check_id=check_id, analyse=analyse)


@mcp.tool()
async def read_ccbim(
    file_path: Annotated[str | None, Field(description="Chemin du CCBIM sur l'ordinateur de l'utilisateur (serveur "
                                                       "lancé en local par Claude Desktop seulement), ex. "
                                                       "'~/Downloads/CCBIM.pdf'.")] = None,
    file_id: Annotated[str | None, Field(description="Fichier déposé (create_upload_link).")] = None,
    bimdata_document_id: Annotated[int | None, Field(description="Document de la GED du projet BIMData actif.")] = None,
    file_url: Annotated[str | None, Field(description="URL https publique.")] = None,
    file_base64: Annotated[str | None, Field(description="Contenu encodé en base64 (petits fichiers).")] = None,
    file_name: Str = None,
    text: Annotated[str | None, Field(description="Texte du CCBIM (pièce jointe lue dans la conversation).")] = None,
    ccbim_id: Annotated[str | None, Field(description="CCBIM déjà lu : lire la suite (avec page_from).")] = None,
    page_from: Annotated[int | None, Field(ge=1, description="Première page à rendre (suite d'un long document).")] = None,
    ctx: Context = None,
) -> dict:
    """Lit le cahier des charges BIM (CCBIM : PDF, Word, Excel ou texte) pour un audit spécifique : rend le texte par
    page et les passages utiles. L'assistant en extrait ensuite les attendus contrôlables (set_ccbim_expectations).
    N'écrit rien dans BIMData."""
    return await _run(ctx, service.read_ccbim, file_path=file_path, file_id=file_id,
                      bimdata_document_id=bimdata_document_id, file_url=file_url, file_base64=file_base64,
                      file_name=file_name, text=text, ccbim_id=ccbim_id, page_from=page_from)


@mcp.tool()
async def set_ccbim_expectations(
    ccbim_id: str,
    requirements: Annotated[list[Requirement] | None, Field(
        description="Exigences de données du CCBIM, une par attendu, avec source ('p. 12, §4.2') : classes IFC, "
                    "champ (Name, Pset_X.Prop, Classification…), obligatoire, valeurs admises, format regex, "
                    "unicité, criticité.")] = None,
    model_checks: Annotated[dict | None, Field(
        description="Contrôles de maquette exigés : ifc_schema, file_name_pattern, expected_buildings, storey_names, "
                    "storey_name_pattern, classification_system, require_spaces, max_proxy_rate (0-1).")] = None,
    not_checkable: Annotated[list[dict] | None, Field(
        description="Attendus non contrôlables sur la maquette : [{exigence, source, raison}].")] = None,
    sources: Annotated[dict | None, Field(description="Source de chaque contrôle de maquette, ex. {'ifc_schema': "
                                                      "'p. 8, §3.1'}.")] = None,
    ctx: Context = None,
) -> dict:
    """Enregistre les attendus extraits du CCBIM (remplace les précédents) : ils deviennent les hypothèses de
    run_audit('ccbim'), à faire valider par l'utilisateur. N'écrit rien dans BIMData."""
    return await _run(ctx, service.set_ccbim_expectations, ccbim_id=ccbim_id, requirements=requirements,
                      model_checks=model_checks, not_checkable=not_checkable, sources=sources)


@mcp.tool(annotations={"readOnlyHint": True})
async def run_audit(
    audit: Annotated[str, Field(description="UN audit choisi par l'utilisateur dans list_audits (ex. 'c2_clash'). "
                                            "Pas de « tous » : un audit à la fois.")],
    hypotheses_validees: Annotated[str | None, Field(
        description="Jeton jeton_hypotheses rendu par le premier appel, à passer seulement après validation "
                    "explicite des hypothèses par l'utilisateur.")] = None,
    options: Annotated[dict | None, Field(
        description="Options de l'audit, ex. {'referentiel': 'ERP 2017'} ou {'f3_referentiel': 'ERP', 'eas': null}.")] = None,
    selection: SelName = None,
    title: Str = None,
    ctx: Context = None,
) -> dict:
    """Audit choisi, en deux temps. Sans jeton : renvoie les hypothèses (paramètres, défauts, plages, référentiel,
    méthode, questions) et jeton_hypotheses, sans rien calculer. Avec le jeton validé : lance l'audit, produit le
    rapport PDF (annexe Excel) et propose des topics BCF ou une Smart View (question_suite). N'écrit rien."""
    return await _run(ctx, service_geometry.run_audit, audit=audit, options=options, selection=selection,
                      title=title, hypotheses_validees=hypotheses_validees)


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


@mcp.custom_route("/reglages/{owner}/{exp}/{sig}", methods=["GET", "POST"])
async def reglages(request: Request) -> Response:
    from . import audit_settings as AS

    p = request.path_params
    target_key = request.query_params.get("t", "")
    if not AS.verify(p["owner"], target_key, p["exp"], p["sig"]):
        return HTMLResponse(_UPLOAD_PAGE.format(body="<h1>Lien expiré</h1><p>Demander un nouveau lien à Claude.</p>"),
                            status_code=403)
    audits = [a for a in request.query_params.get("a", "").split(",") if a] or None
    if request.method == "GET":
        return HTMLResponse(AS.page(p["owner"], target_key, audits))
    try:
        values = await request.json()
        saved = AS.save(p["owner"], target_key, values)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "values": saved})


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
