"""Outils géométriques et menu d'audit (lot 1) : index, structure IFC, géométrie, audits.

Aucun de ces outils n'écrit dans BIMData. Ils lisent la maquette active, acceptent
une ``selection`` comme les outils existants et enregistrent leurs anomalies dans le
même registre de contrôles (``list_checks``, ``get_check_issues``, Smart View / BCF
par ``check_id``).
"""

from __future__ import annotations

import time
from collections import Counter

from . import files
from .charte import Col, Contexte, Livrable, nom_fichier
from .config import get_settings
from .geometry import index as gindex
from .geometry.source import fetch_model_ifc
from .model_view import records_by_uuid
from .quality.engine import CheckResult
from .quality.rules import run_geometry_rules, run_structure_rules
from .quality.rules_report import write_rules_report
from .service import Call, _ctx, _doc_ctx, _livrable, _selection_records, _target
from .state import new_id, now_iso

#: Estimation de la durée de construction de l'index (débit mesuré : ~3 Mo d'IFC par seconde).
_S_PER_MB = 1 / gindex.BUILD_MB_PER_S


def _background_mb() -> float:
    """Taille de fichier IFC au-delà de laquelle l'index se calcule en tâche de fond (MCP_GEOMETRY_BACKGROUND_MB,
    défaut 100 Mo ≈ 30 s ; 0 = toujours en direct)."""
    import os

    try:
        return float(os.getenv("MCP_GEOMETRY_BACKGROUND_MB", "") or 100)
    except ValueError:
        return 100.0


# ── Index géométrique ───────────────────────────────────────────────────


def _geometry(call: Call, *, refresh: bool = False):
    target, client, snap, _ = _ctx(call)
    path, src = fetch_model_ifc(client, target, refresh=refresh)
    t0 = time.time()
    idx, built = gindex.get_or_build(path, get_settings().cache_dir, key=target.key, refresh=refresh,
                                     background_over_mb=_background_mb())
    info = {"construit_maintenant": built, "duree_s": round(time.time() - t0, 1), "fichier_ifc": src}
    renamed = apply_bimdata_names(idx, records_by_uuid(snap))
    if renamed:
        doc = (snap.model or {}).get("document") or {}
        info["noms_bimdata"] = (f"{renamed} élément(s) renommé(s) dans BIMData depuis l'export IFC "
                                f"({doc.get('file_name') or 'fichier IFC'} du {str(doc.get('created_at') or '?')[:10]}) : "
                                "les rapports affichent les noms BIMData ; la géométrie reste celle du fichier IFC.")
    return target, snap, idx, info


def apply_bimdata_names(idx, records: dict) -> int:
    """Noms actuels de BIMData dans les messages (l'index garde le nom du fichier IFC dans ``ifc_name``)."""
    n = 0
    for g, e in idx.elements.items():
        rec = records.get(e.get("global_id") or g.split("#")[0])
        if rec is None or not rec.name:
            continue
        ifc_name = e.setdefault("ifc_name", e.get("name"))
        if _plain_name(rec.name) != _plain_name(ifc_name):  # pas le simple suffixe d'identifiant Revit « :181719 »
            n += 1
        e["name"] = rec.name
    return n


def _plain_name(name: str | None) -> str:
    import re

    return re.sub(r"\s+", " ", re.sub(r":\d+$", "", str(name or ""))).strip()


def _index_state(idx, info: dict) -> dict:
    m = idx.meta
    return {
        "etat": "construit" if info.get("construit_maintenant") else "en cache",
        "fichier_ifc": m["ifc_file"], "taille_mo": m["ifc_size_mb"], "schema": m["schema"],
        "elements_physiques": m["n_elements"], "locaux": m["n_spaces"], "indexes_avec_geometrie": m["n_indexed"],
        "sans_geometrie": m["n_without_geometry"], "unite": m["unit"], "echelle_unite_fichier": m["unit_scale"],
        "controle_unites": m["unit_check"], "durees_construction_s": m["durations_s"], "construit_le": m["built_at"],
        "bibliotheque": m.get("geometry_library"), "repli": m.get("fallback"),
        "memoire": {k: m.get(k) for k in ("threads", "memory_estimate_mb", "memory_peak_mb", "memory_limit_mb")},
        "duree_appel_s": info.get("duree_s"), "noms_bimdata": info.get("noms_bimdata"),
    }


def build_geometry_index(call: Call, refresh: bool) -> dict:
    try:
        _, _, idx, info = _geometry(call, refresh=refresh)
    except gindex.IndexBuilding as exc:
        return {**exc.info, "message": str(exc),
                "suite": [f"Rappeler build_geometry_index dans ≈ {exc.info['reste_estime_s']} s (ou lancer le contrôle "
                          "voulu : il répondra dès que l'index sera prêt)."]}
    out = _index_state(idx, info)
    warnings = []
    if idx.meta["unit_check"].get("status") == "ecart":
        warnings.append("Unités douteuses : hauteur déclarée des portes et fenêtres ≠ hauteur mesurée.")
    if idx.meta["n_without_geometry"]:
        warnings.append(f"{idx.meta['n_without_geometry']} élément(s) physique(s) sans géométrie : listés par la règle "
                        "S10 (run_structure_check), jamais ignorés en silence.")
    return {**out, "avertissements": warnings,
            "suite": ["run_structure_check", "run_geometry_quality_check", "geometry_info(uuids=…)", "list_audits"]}


def geometry_info(call: Call, selection, uuids, limit: int) -> dict:
    target, snap, idx, info = _geometry(call)
    if uuids:
        wanted = [k for u in dict.fromkeys(uuids) for k in (idx.keys_for(u) or [u])]
    else:
        recs, _ = _selection_records(call, selection, target, snap)
        wanted = idx.resolve(r.uuid for r in recs)
    names = idx.storey_names()
    out, missing = [], []
    for g in wanted[:limit]:
        e = idx.elements.get(g)
        if not e:
            missing.append(g)
            continue
        out.append({"GlobalId": g, "ifc_class": e["ifc_class"], "name": e["name"],
                    "storey": names.get(e.get("storey") or "", ""), "has_geometry": e["has_geometry"],
                    "aabb": [round(x, 3) for x in e["aabb"]] if e["aabb"] else None,
                    "dimensions_m": [round(e["aabb"][i + 3] - e["aabb"][i], 3) for i in range(3)] if e["aabb"] else None,
                    "obb": e["obb"], "volume_m3": None if e["volume"] is None else round(e["volume"], 4),
                    "is_closed": e["is_closed"], "volume_fiable": bool(e["is_closed"]), "local_axes": e["axes"],
                    "reason": e.get("reason")})
    return {"elements": out, "total": len(wanted), "returned": len(out), "introuvables": missing[:20],
            "unite": "m", "repere": "coordonnées projet du fichier IFC",
            "note": "OBB : axes[0] = plus grande dimension horizontale, axes[2] = vertical ; half = demi-dimensions."}


# ── Enregistrement d'un contrôle par règles ─────────────────────────────


def _store(call: Call, target, snap, result: CheckResult, notes: list[str], *, title: str, prefix: str,
           type_livrable: str, sel_name: str, selection, intro: str, extra: dict | None = None,
           report: dict | None = None, options: dict | None = None) -> dict:
    check_id = new_id("chk")
    st = call.state
    renamed = ((extra or {}).get("index") or {}).get("noms_bimdata")
    if renamed:
        notes = [renamed] + list(notes)
    ctx: Contexte = _doc_ctx(call, snap, target, type_livrable, title, perimetre=sel_name if selection else "")
    path = files.new_output_path(st, nom_fichier(prefix, ctx.projet, ctx.maquette))
    write_rules_report(path, result, ctx=ctx, check_id=check_id, notes=notes, intro=intro, **(report or {}))
    meta = files.register_file(st, path, kind="check-report", label=title)
    summary = result.summary()
    from . import audit_params as AP
    from .service import write_check_pdf

    aid = AUDIT_OF_PREFIX.get(prefix, "")
    hyp = AP.apply_to_options(aid, {}, options or {})[1] if aid else []
    sheets = ["Synthèse", *[f"{p['req_id']}" for p in result.per_requirement if p["non_conformes"]],
              *[x["name"] for x in (report or {}).get("extra_sheets", [])], "Lisez-moi"]
    st.save("checks", check_id, {
        "check_id": check_id, "title": title, "target": target.to_dict(), "requirements_id": None, "phase": None,
        "selection": sel_name, "created_at": now_iso(), "summary": summary, "kind": "rules",
        "per_requirement": result.per_requirement, "issues": [i.to_dict() for i in result.issues],
        "checked_uuids": result.checked_uuids, "report_file_id": meta["file_id"], "notes": notes,
        "report": {"audit_id": aid, "title": title, "type_livrable": type_livrable, "excel_name": path.name,
                   "excel_file_id": meta["file_id"], "perimetre": sel_name if selection else "", "hypotheses": hyp,
                   "references": [], "excel_label": "Annexe Excel", "excel_sheets": sheets},
    }, gz=True)
    by_rule = [{"regle": p["req_id"], "libelle": p["label"], "methode": p["field"], "controles": p["applicable"],
                "anomalies": p["non_conformes"]} for p in result.per_requirement]
    # rapport PDF (livrable principal, encart « L'essentiel » en tête) ; le classeur Excel devient l'annexe
    pdf_out = write_check_pdf(call, check_id, snap=snap, target=target)
    return {
        "livrable": pdf_out["livrable"], "analyse_automatique": pdf_out["analyse_automatique"],
        "proposition_ged": _ged_offer(check_id, snap),
        "check_id": check_id, "selection": sel_name, "summary": summary,
        "synthese": {"elements_controles": summary["elements_controlled"], "anomalies": summary["non_conformities"],
                     "par_severite": summary["by_severity"]},
        "regles": by_rule,
        "sample_issues": [{k: i.to_dict()[k] for k in ("issue_id", "rule_id", "severity", "ifc_class", "name",
                                                       "storey", "message")} for i in result.issues[:12]],
        **(extra or {}),
        "avertissements": notes,
        **_next_steps(check_id, title, [i.to_dict() for i in result.issues], pdf_out["analyse_automatique"]),
        "suite": ["get_check_issues(check_id, req_id='S3') pour détailler une règle",
                  "après avoir présenté le rapport PDF : poser question_suite (topics BCF type Solibri ou Smart "
                  "View) ; ne rien préparer sans accord explicite"],
    }


def _settings(call: Call, target, aid: str, explicit: dict) -> tuple[dict, list[str]]:
    """Paramètres d'un contrôle : valeur explicite, sinon réglage enregistré pour la maquette, sinon défaut.
    Renvoie les options complétées et une note listant les réglages enregistrés appliqués."""
    from . import audit_params as AP
    from . import audit_settings as AS

    def clean(d):
        return {k: (clean(v) if isinstance(v, dict) else v) for k, v in (d or {}).items() if v is not None}

    exp = clean(explicit)
    saved = AS.load(call.identity.storage_key, target.key)
    opts, rows = AP.apply_to_options(aid, saved, exp)
    used = [f"{r['parametre']} = {r['valeur']} {r['unite']}".rstrip() for r in rows
            if r["cle"] in saved and AP._get(exp, tuple(next(p for p in AP.PARAMS[aid] if p.key == r["cle"]).path)) is None]
    notes = [f"Réglages enregistrés pour la maquette appliqués : {', '.join(used)}."] if used else []
    return opts, notes


#: Familles où un topic par anomalie est la norme (diapositive Solibri) ; ailleurs, un topic par règle.
_PER_ISSUE = ("clash", "structure", "geometrie", "incendie", "pmr", "degagements")


def bcf_offer(check_id: str, title: str, issues: list[dict]) -> dict | None:
    """Proposition de topics BCF type Solibri après un rapport : appel prêt à lancer, nombre de topics, contenu, et
    la question à poser. Au-delà de 20 topics, prepare_bcf_topics fait d'abord choisir le périmètre (gravité, règle,
    type de conflit). Ne prépare rien (Smart Views et BCF seulement sur demande explicite)."""
    from . import bcf
    from .service_bcf import BCF_CHOICE_MIN

    real = [i for i in issues if i.get("status") != "exclu"]
    if not real:
        return None
    sev = Counter(i.get("severity") for i in real)
    fam = Counter(bcf.family_of(i.get("rule_id") or i.get("req_id")) for i in real).most_common(1)[0][0]
    per_issue = fam in _PER_ISSUE
    n_topics = len(real) if per_issue else len({i.get("req_id") for i in real})
    args = {"check_id": check_id, "check_group_by": "issue" if per_issue else "requirement"}
    rules = Counter(i.get("rule_id") or i.get("req_id") for i in real)
    what = ", ".join(f"{sev[s]} {s}{'s' if sev[s] > 1 else ''}" for s in ("bloquant", "majeur", "mineur", "info")
                     if sev.get(s))
    choose = n_topics > BCF_CHOICE_MIN
    return {
        "controle": title, "check_id": check_id, "topics_estimes": n_topics, "anomalies": dict(sev),
        "par_regle": dict(rules.most_common(10)),
        "appel": {"outil": "prepare_bcf_topics", "arguments": args},
        "contenu": "un topic par anomalie" if per_issue else "un topic par règle",
        "question": (f"Voulez-vous des topics BCF pour « {title} » ({what}) ? Chaque topic porte une vue cadrée sur "
                     "l'anomalie, les éléments en cause colorés par rôle, la coupe et une image ; rien n'est envoyé à "
                     "BIMData avant votre accord sur le plan."
                     + (f" Avec {n_topics} topics possibles, je vous proposerai d'abord de choisir quoi envoyer : par "
                        "gravité, par règle ou par type de conflit." if choose else "")),
    }


#: Préfixe de rapport → audit (paramètres et référentiels du rapport PDF).
AUDIT_OF_PREFIX = {"CONTROLE-STRUCTURE": "c1_structure", "CLASH": "c2_clash", "CLASH-MATRICE": "c2_clash",
                   "CONTROLE-GEOMETRIE": "c4_geometrie", "PMR": "c5_pmr", "DEGAGEMENTS": "c6_degagements",
                   "INCENDIE": "c7_incendie"}


def _scope(call: Call, selection, target, snap, idx=None):
    recs, sel_name = _selection_records(call, selection, target, snap)
    if not selection:
        return None, sel_name
    uu = {r.uuid for r in recs}
    return (set(idx.resolve(uu)) | uu if idx is not None else uu), sel_name


def run_structure_check(call: Call, selection, checks, expected_buildings, s4_thresholds, unique_property,
                        door_direction_property, georef_expected, title) -> dict:
    target, snap, idx, info = _geometry(call)
    opts, set_notes = _settings(call, target, "c1_structure", {"s4_thresholds": s4_thresholds})
    s4_thresholds = opts.get("s4_thresholds")
    uuids, sel_name = _scope(call, selection, target, snap, idx)
    displayed = {s.get("uuid"): s.get("name") for s in snap.storeys or [] if s.get("uuid")}
    result, notes = run_structure_rules(
        idx, records_by_uuid(snap), uuids=uuids, checks=checks, displayed_storeys=displayed,
        expected_buildings=expected_buildings, s4_thresholds=s4_thresholds,
        unique_property=unique_property, door_direction_property=door_direction_property,
        georef_expected=georef_expected)
    notes = set_notes + notes
    if selection:
        notes = ["Sélection : S1, S3 et S9 portent toujours sur la maquette entière."] + notes
    return _store(call, target, snap, result, notes, title=title or "Structuration et intégrité IFC",
                  prefix="CONTROLE-STRUCTURE", type_livrable="Contrôle de structuration IFC", sel_name=sel_name,
                  selection=selection, extra={"index": _index_state(idx, info)}, options=opts,
                  intro="Structuration spatiale, relations et identifiants du fichier IFC (règles S1 à S11). "
                        "Méthode Données : relations IFC ; Bbox : boîtes englobantes de l'index géométrique.")


def run_geometry_quality_check(call: Call, selection, checks, thresholds, title) -> dict:
    target, _, snap, _ = _ctx(call)
    opts, notes = _settings(call, target, "c4_geometrie", {"thresholds": thresholds})
    thresholds = opts.get("thresholds")
    idx = info = None
    try:
        _, snap, idx, info = _geometry(call)
    except (ValueError, RuntimeError) as exc:
        notes.append(f"Index géométrique indisponible ({exc}) : règles sur propriétés seulement.")
    uuids, sel_name = _scope(call, selection, target, snap, idx)
    labels = {s.get("uuid"): s.get("name") for s in snap.storeys or [] if s.get("uuid")}
    result, n2 = run_geometry_rules(idx, records_by_uuid(snap), uuids=uuids, checks=checks, thresholds=thresholds,
                                    storey_labels=labels)
    return _store(call, target, snap, result, notes + n2, title=title or "Géométrie et modélisation",
                  prefix="CONTROLE-GEOMETRIE", type_livrable="Contrôle géométrie et modélisation", sel_name=sel_name,
                  selection=selection, extra={"index": _index_state(idx, info) if idx else None}, options=opts,
                  intro="Locaux (surface, hauteur, volume fermé) et garde-corps (hauteur, nom du type). "
                        "Les gaines repérées par leur nom sont signalées pour information.")


# ── Conflits (clash) ────────────────────────────────────────────────────


def _resolve_set(call: Call, spec, target, snap, idx, disc: dict[str, str]) -> list[str]:
    """Ensemble d'une case : discipline, sélection enregistrée, GlobalId ou critères de select_elements."""
    from .model_view import build_records
    from .quality.clash_check import DISCIPLINES, parse_set
    from .selection import SelectionSpec, select
    from .service import _load_selection

    if spec is None:
        return [g for g in disc if idx.elements[g]["has_geometry"]]
    d = parse_set(spec)
    if "discipline" in d:
        name = str(d["discipline"]).strip().capitalize()
        if name not in DISCIPLINES and name not in set(disc.values()):
            raise ValueError(f"Discipline « {d['discipline']} » inconnue ({', '.join(DISCIPLINES)}).")
        return [g for g, x in disc.items() if x == name]
    if "selection" in d:
        wanted = _load_selection(call, d["selection"], target)["uuids"]
    elif "uuids" in d and len(d) == 1:
        wanted = list(d["uuids"])
    elif set(d) <= {"ifc_classes", "exclude_ifc_classes", "include_subtypes"}:  # classes seules : lues sur l'index
        from .selection import _class_match

        sub = d.get("include_subtypes", True)
        wanted = [g for g, e in idx.elements.items() if _class_match(e["ifc_class"], d.get("ifc_classes") or [], sub)
                  and not _class_match(e["ifc_class"], d.get("exclude_ifc_classes") or [], sub)]
    else:
        try:
            crit = SelectionSpec(**d)
        except Exception as exc:  # noqa: BLE001 — message pydantic lisible
            raise ValueError(f"Critères d'ensemble invalides : {exc}") from exc
        wanted = [r.uuid for r in select(build_records(snap), crit)]
    return idx.resolve(dict.fromkeys(wanted))  # GlobalId → clé(s) d'index (plusieurs si GlobalId en double)


def _clash_report(idx, info: dict, scope: set[str], result) -> dict:
    from .quality.clash_check import GROUP_LABELS, VOLUME_COLS, volumes_rows

    clashes = info["clashes"]
    groups = Counter(c.group for c in clashes)
    tables = [("Regroupement", [Col("Catégorie"), Col("Conflits", kind="integer")],
               [[GROUP_LABELS[g], groups.get(g, 0)] for g in GROUP_LABELS] or [["—", 0]])]
    if len(info["cases"]) > 1 or info["cases"]:
        tables.append(("Par case", [Col("Case"), Col("Ensemble A", kind="integer"), Col("Ensemble B", kind="integer"),
                                    Col("Paires candidates", kind="integer"), Col("Conflits", kind="integer"),
                                    Col("Sous tolérance", kind="integer"), Col("Exceptions")],
                       [[c["case"], c["set_a"], c["set_b"], c["paires_candidates"], c["conflits"], c["sous_tolerance"],
                         ", ".join(f"{k} : {v}" for k, v in c["exceptions"].items()) or "—"]
                        for c in info["cases"]]))
    names = {g: (e["ifc_class"], e["name"]) for g, e in idx.elements.items()}
    return {"extra_tables": tables, "names": names,
            "extra_sheets": [{"name": "Volumes", "cols": VOLUME_COLS, "rows": volumes_rows(idx, clashes, scope),
                              "description": "Par type de construction : volume total des éléments testés (maillages "
                                             "fermés), volume intersecté par les conflits et part intersectée."}]}


def _run_clash_cells(call: Call, cells: list[dict], *, discipline_property, title, prefix: str,
                     type_livrable: str, label_cells: bool = True) -> dict:
    from .quality.clash_check import disciplines, run_cells

    target, snap, idx, info = _geometry(call)
    # tolérances : celles de la case, sinon les réglages enregistrés pour la maquette, sinon les défauts
    keys = ("tol_h_m", "tol_v_m", "contact_m")
    opts, set_notes = _settings(call, target, "c2_clash", {k: cells[0].get(k) for k in keys} if len(cells) == 1 else {})
    for c in cells:
        for k in keys:
            if c.get(k) is None and opts.get(k) is not None:
                c[k] = opts[k]
    recs = records_by_uuid(snap)
    disc = disciplines(idx, recs, discipline_property)
    cache: dict[str, list[str]] = {}

    def resolve(spec):
        key = repr(spec)
        if key not in cache:
            cache[key] = _resolve_set(call, spec, target, snap, idx, disc)
        return cache[key]

    result, notes, cinfo = run_cells(idx, recs, cells, resolve=resolve, disc=disc, label_cells=label_cells)
    scope = {g for c in cells if c.get("active", True) for g in resolve(c["a"]) + resolve(c["b"])}
    groups = Counter(c.group for c in cinfo["clashes"])
    out = _store(call, target, snap, result, set_notes + notes, title=title, prefix=prefix,
                 type_livrable=type_livrable, options=opts,
                 sel_name="(maquette entière)", selection=None, report=_clash_report(idx, cinfo, scope, result),
                 extra={"cases": cinfo["cases"], "regroupement": dict(groups),
                        "disciplines": dict(Counter(disc.values())), "index": _index_state(idx, info)},
                 intro="Détection de conflits sur le modèle de la règle General Intersection de Solibri : paires "
                       "candidates par boîtes englobantes, puis intersection confirmée et mesurée sur les maillages. "
                       "Doublons et inclusions bloquants ; chevauchements selon leur taille.")
    out["synthese"]["par_type"] = dict(Counter(i.clash_type for i in result.issues))
    return out


def run_clash_check(call: Call, set_a, set_b, tol_h_m, tol_v_m, tol_volume_m3, types, ignore_same_system,
                    ignore_same_layer, exceptions, severity_rules, discipline_property, title, contact_m=None) -> dict:
    cell = {"id": "A × B", "a": set_a, "b": set_b, "tol_h_m": tol_h_m, "tol_v_m": tol_v_m,
            "tol_volume_m3": tol_volume_m3, "types": types, "ignore_same_system": ignore_same_system,
            "ignore_same_layer": ignore_same_layer, "exceptions": exceptions, "severity_rules": severity_rules,
            "contact_m": contact_m}
    return _run_clash_cells(call, [cell], discipline_property=discipline_property, title=title or "Détection de conflits",
                            prefix="CLASH", type_livrable="Détection de conflits", label_cells=False)


def run_clash_matrix(call: Call, matrix, file_id, bimdata_document_id, discipline_property, title) -> dict:
    from .quality.clash_check import DEFAULT_MATRIX, read_matrix

    if matrix:
        cells = [dict(c) for c in matrix]
        origin = "matrice fournie"
    elif file_id or bimdata_document_id not in (None, ""):
        from . import service

        client = (service.make_client(_target(call), call.identity)
                  if bimdata_document_id not in (None, "") else None)
        path = files.resolve_input(call.state, file_id=file_id, bimdata_document_id=bimdata_document_id,
                                   client=client)
        cells = read_matrix(path)
        origin = f"fichier « {path.name} »"
    else:
        cells = [dict(c) for c in DEFAULT_MATRIX]
        origin = "matrice par défaut"
    for i, c in enumerate(cells, start=1):
        if "a" not in c or "b" not in c:
            raise ValueError(f"Case {c.get('id') or i} : ensembles « a » et « b » obligatoires.")
        c.setdefault("id", f"C{i}")
    out = _run_clash_cells(call, cells, discipline_property=discipline_property,
                           title=title or "Matrice de détection de conflits", prefix="CLASH-MATRICE",
                           type_livrable="Matrice de conflits")
    out["matrice"] = origin
    return out


def clash_matrix_template(call: Call) -> dict:
    from .quality.clash_check import write_matrix_template

    titre = "Gabarit de matrice de conflits"
    path = files.new_output_path(call.state, nom_fichier("GABARIT-MATRICE-CLASH", "BIMData", "matrice"))
    write_matrix_template(path, Contexte(type_livrable="Gabarit", titre=titre, auteur=call.identity.display))
    meta = files.register_file(call.state, path, kind="template", label=titre)
    return {"livrable": _livrable(meta, type_livrable="Gabarit", titre=titre, feuilles=["Synthèse", "Matrice"]),
            "synthese": {"cases_preremplies": 5}, "avertissements": [],
            "suite": ["Compléter la feuille « Matrice », la déposer (create_upload_link) puis "
                      "run_clash_matrix(file_id=…)."]}


def compare_clash_results(call: Call, check_id, file_id, bimdata_document_id, tool_name, objective_pct, title) -> dict:
    """Compare un contrôle de conflits du MCP avec l'export d'un autre outil, paire par paire."""
    from . import service
    from .quality.clash_compare import compare, read_external

    target, snap, idx, info = _geometry(call)
    check = service._load_check(call, check_id, target)
    pairs = []
    for i in check["issues"]:
        if not i.get("clash_type"):
            continue
        grouped = (i.get("measure") or {}).get("paires")  # doublons regroupés : chaque paire compte
        for a, b in grouped or [(i["uuid"], next((e["uuid"] for e in i.get("elements") or [] if e.get("role") == "B"),
                                                None))]:
            if b:
                pairs.append({"issue_id": i["issue_id"], "a": a, "b": b, "type": i["clash_type"],
                              "severity": i["severity"], "group": i.get("group"),
                              "depth": (i.get("measure") or {}).get("value")})
    if not pairs and check["issues"]:
        raise ValueError(f"Le contrôle {check_id} n'est pas un contrôle de conflits (run_clash_check).")
    if not file_id and bimdata_document_id in (None, ""):
        raise ValueError("Fournir l'export de l'autre outil : file_id (create_upload_link) ou bimdata_document_id.")
    client = service.make_client(target, call.identity) if bimdata_document_id not in (None, "") else None
    path = files.resolve_input(call.state, file_id=file_id, bimdata_document_id=bimdata_document_id, client=client)
    entries, meta_ext = read_external(path, known=set(idx.elements))
    res = compare(pairs, entries)
    ok = res["ecart_nombre_pct"] is not None and res["ecart_nombre_pct"] <= objective_pct
    tool = tool_name or "Solibri"

    def name(g):
        e = idx.elements.get(g) or {}
        return e.get("ifc_class", ""), e.get("name", ""), idx.storey_names().get(e.get("storey") or "", "")

    def pair_rows(items, with_ext):
        rows = []
        for p in items:
            (ca, na, sa), (cb, nb, _) = name(p["a"]), name(p["b"])
            rows.append([p["issue_id"], p["type"], p["severity"], p["depth"], p["a"], ca, na, sa, p["b"], cb, nb]
                        + ([p.get("externe"), p.get("libelle_externe")] if with_ext else []))
        return rows

    pc = [Col("N° MCP", "MCP", width=9), Col("Type", "MCP", width=13), Col("Criticité", "MCP", kind="severity"),
          Col("Pénétration (m)", "MCP", kind="number"), Col("UUID A", "Élément A", width=24),
          Col("Classe A", "Élément A", width=18), Col("Nom A", "Élément A", width=30), Col("Étage", "Élément A"),
          Col("UUID B", "Élément B", width=24), Col("Classe B", "Élément B", width=18),
          Col("Nom B", "Élément B", width=30)]
    ext_rows = []
    for e in res["details"]["externe_seulement"]:
        ns = [name(g) for g in e["guids"][:2]]
        ext_rows.append([e["id"], e["label"], e["guids"][0], ns[0][0], ns[0][1], e["guids"][1], ns[1][0], ns[1][1],
                         len(e["guids"])])
    st = call.state
    titre = title or f"Comparaison des conflits MCP / {tool}"
    ctx = _doc_ctx(call, snap, target, "Comparaison de conflits", titre)
    out_path = files.new_output_path(st, nom_fichier("COMPARAISON-CLASH", ctx.projet, ctx.maquette))
    lv = Livrable(out_path, ctx)
    lv.synthese(kpis=[("Conflits MCP", res["mcp"], "integer"), (f"Conflits {tool}", res["externe"], "integer"),
                      ("Communs", res["communs"], "integer"), ("MCP seulement", res["mcp_seulement"], "integer"),
                      (f"{tool} seulement", res["externe_seulement"], "integer"),
                      ("Écart de nombre", (res["ecart_nombre_pct"] or 0) / 100, "percent"),
                      ("Écart paire par paire", (res["ecart_paires_pct"] or 0) / 100, "percent")],
                intro=f"Comparaison paire par paire du contrôle {check_id} ({check.get('title')}) avec l'export "
                      f"{meta_ext['format']} de {tool} « {path.name} ». Objectif : écart de {objective_pct} % au plus "
                      f"— {'atteint' if ok else 'non atteint'}.")
    lv.tableau("Lecture de l'export", [Col("Élément"), Col("Valeur")],
               [["Format", meta_ext["format"]], ["Conflits lus", res["externe"]],
                ["GlobalId absents de la maquette (ignorés)", meta_ext["guids_inconnus"]]])
    lv.feuille("Communs", pc + [Col(f"Réf. {tool}", tool, width=14), Col(f"Libellé {tool}", tool, width=40)],
               pair_rows(res["details"]["communs"], True), description="Conflits trouvés par les deux outils.")
    lv.feuille("MCP seulement", pc, pair_rows(res["details"]["mcp_seulement"], False),
               description=f"Conflits trouvés par le MCP et absents de l'export {tool}.")
    lv.feuille(f"{tool[:15]} seulement", [Col(f"Réf. {tool}", "Conflit", width=14), Col("Libellé", "Conflit", width=40),
                                          Col("UUID A", "Élément A", width=24), Col("Classe A", "Élément A"),
                                          Col("Nom A", "Élément A", width=30), Col("UUID B", "Élément B", width=24),
                                          Col("Classe B", "Élément B"), Col("Nom B", "Élément B", width=30),
                                          Col("GlobalId", "Conflit", kind="integer")], ext_rows,
               description=f"Conflits de l'export {tool} sans équivalent dans le contrôle MCP.")
    lv.lisez_moi(f"Comparaison d'un contrôle de conflits du MCP avec {tool}, paire par paire (deux GlobalId).", [
        ("Méthode", ["Même fichier IFC et mêmes réglages dans les deux outils (règle General Intersection, mêmes "
                     "tolérances horizontale et verticale, sans tolérance de volume).",
                     "Une entrée de l'export correspond à un conflit MCP si elle porte les GlobalId de ses deux "
                     "éléments ; chaque conflit n'est compté qu'une fois.",
                     "Écart de nombre = |MCP − autre outil| / autre outil ; écart paire par paire = (MCP seulement + "
                     "autre outil seulement) / autre outil."])])
    lv.meta({"format": "bimdata-mcp/clash-compare/v1", "check_id": check_id})
    lv.close()
    meta = files.register_file(st, out_path, kind="check-report", label=titre)
    sample = lambda items: [{k: x.get(k) for k in ("issue_id", "a", "b", "type", "id", "label")}  # noqa: E731
                            for x in items[:8]]
    return {"livrable": _livrable(meta, type_livrable="Comparaison de conflits", titre=titre,
                                  feuilles=["Synthèse", "Communs", "MCP seulement", f"{tool[:15]} seulement"]),
            "synthese": {k: res[k] for k in ("mcp", "externe", "communs", "mcp_seulement", "externe_seulement",
                                            "ecart_nombre_pct", "ecart_paires_pct")},
            "objectif_pct": objective_pct, "objectif_atteint": ok, "export": {**meta_ext, "fichier": path.name},
            "exemples": {"mcp_seulement": sample(res["details"]["mcp_seulement"]),
                         "externe_seulement": sample(res["details"]["externe_seulement"])},
            "avertissements": ([f"{meta_ext['guids_inconnus']} GlobalId de l'export absents de la maquette active : "
                                "vérifier que les deux outils ont lu le même fichier IFC."]
                               if meta_ext["guids_inconnus"] else []),
            "suite": ["Examiner les feuilles « MCP seulement » et « … seulement » : tolérances, exceptions, éléments "
                      "exclus (hôtes, agrégats) ou différences de lecture de la géométrie."]}


# ── Lot 3 : distances, dégagements, PMR, incendie ───────────────────────

CLEARANCE_PRESETS = {
    "portes": {"targets": "classes:IfcDoor", "front_m": 1.00, "back_m": 0.0, "side_m": 0.0, "height_m": 2.00,
               "use_opening_side": True},
    "fenetres": {"targets": "classes:IfcWindow"},
    "equipements": {"targets": "classes:IfcElectricDistributionBoard,IfcUnitaryEquipment,IfcAirToAirHeatRecovery,"
                               "IfcElectricDistributionPoint"},
    "detecteurs": {"targets": "classes:IfcSensor,IfcAlarm,IfcAirTerminal,IfcFireSuppressionTerminal"},
}


def _sets(call: Call, target, snap, idx, *specs):
    return [(_resolve_set(call, sp, target, snap, idx, {}) if sp is not None else []) for sp in specs]


def run_distance_check(call: Call, set_a, set_b, mode, distance_m, direction, same_space_only, title) -> dict:
    from .quality.lot3 import run_distance_rules

    target, snap, idx, info = _geometry(call)
    if set_a is None or set_b is None:
        raise ValueError("set_a et set_b sont obligatoires (ex. set_a='classes:IfcSanitaryTerminal', "
                         "set_b='classes:IfcOutlet').")
    a, b = _sets(call, target, snap, idx, set_a, set_b)
    result, notes = run_distance_rules(idx, records_by_uuid(snap), a, b, mode=mode, distance_m=distance_m,
                                       direction=direction, same_space_only=same_space_only)
    if not a or not b:
        notes.append(f"Ensemble {'A' if not a else 'B'} vide.")
    return _store(call, target, snap, result, notes, title=title or "Distance entre composants", prefix="DISTANCE",
                  type_livrable="Contrôle de distances", sel_name="(maquette entière)", selection=None,
                  extra={"set_a": len(a), "set_b": len(b), "mode": mode, "direction": direction},
                  intro=f"Distance {'minimale' if mode == 'min' else 'maximale'} de {distance_m} m entre les éléments "
                        "A et l'élément B le plus proche (mesurée sur les maillages).")


def run_clearance_check(call: Call, preset, targets, obstacles, front_m, back_m, side_m, height_m, width,
                        use_opening_side, tolerance_m, title) -> dict:
    from .quality.lot3 import run_clearance_rules

    cfg = dict(CLEARANCE_PRESETS.get(preset or "", {}))
    if preset and preset not in CLEARANCE_PRESETS:
        raise ValueError(f"Préréglage « {preset} » inconnu : {', '.join(CLEARANCE_PRESETS)}.")
    # réglages enregistrés : tolérance toujours ; profondeur et hauteur de zone pour le préréglage « portes »
    explicit = {"tolerance_m": tolerance_m}
    if preset == "portes":
        explicit.update(front_m=front_m, height_m=height_m)
    opts, set_notes = _settings(call, _target(call), "c6_degagements", explicit)
    tolerance_m = opts.get("tolerance_m", 0.05)
    if preset == "portes":
        front_m, height_m = opts.get("front_m", front_m), opts.get("height_m", height_m)
    for k, v in (("targets", targets), ("front_m", front_m), ("back_m", back_m), ("side_m", side_m),
                 ("height_m", height_m), ("use_opening_side", use_opening_side)):
        if v is not None:
            cfg[k] = v
    if not cfg.get("targets"):
        raise ValueError("Préciser targets (ou un préréglage : portes, fenetres, equipements, detecteurs).")
    if cfg.get("front_m") is None and not cfg.get("back_m"):
        raise ValueError(f"Préréglage « {preset} » : les distances sont à fournir par le projet (front_m, back_m, "
                         "side_m, height_m)." if preset else "Préciser front_m (et/ou back_m).")
    target, snap, idx, info = _geometry(call)
    tg, = _sets(call, target, snap, idx, cfg["targets"])
    obs = set(_sets(call, target, snap, idx, obstacles)[0]) if obstacles is not None else None
    w = None if width in (None, "component", "composant") else float(width)
    result, notes = run_clearance_rules(
        idx, records_by_uuid(snap), tg, obstacles=obs, front_m=float(cfg.get("front_m") or 0),
        back_m=float(cfg.get("back_m") or 0), side_m=float(cfg.get("side_m") or 0),
        height_m=float(cfg.get("height_m") or 2.0), width_m=w, use_opening_side=bool(cfg.get("use_opening_side")),
        tolerance_m=tolerance_m, source="préréglage " + preset if preset else "paramètres")
    notes = notes + ["Côté d'ouverture : axe +Y du placement de la porte (convention IFC ; OperationType ne donne que "
                     "le côté des gonds). Zone du seul côté +Y pour une porte battante ; des deux côtés quand "
                     "OperationType ne dit pas que la porte est battante. Sols minces (≤ 10 cm) au seuil : pas des "
                     "obstacles, voir A7."]
    return _store(call, target, snap, result, set_notes + notes, title=title or "Espaces libres et dégagements",
                  prefix="DEGAGEMENTS", type_livrable="Contrôle des dégagements", sel_name="(maquette entière)",
                  selection=None, options={**opts, "front_m": cfg.get("front_m"), "height_m": cfg.get("height_m"),
                                           "tolerance_m": tolerance_m}, extra={"cibles": len(tg), "zone": {k: cfg.get(k) for k in (
                      "front_m", "back_m", "side_m", "height_m", "use_opening_side")}, "largeur": width or "composant"},
                  intro="Zone libre devant / derrière / autour de chaque cible, construite dans son repère local ; "
                        "obstacles cherchés dans l'index puis empiètement mesuré sur les maillages.")


def _optional_geometry(call: Call):
    target, _, snap, _ = _ctx(call)
    try:
        _, snap, idx, info = _geometry(call)
    except ValueError as exc:
        return target, snap, None, None, f"Index géométrique indisponible ({exc})."
    return target, snap, idx, info, None


def run_accessibility_check(call: Call, referentiel, scope, thresholds, checks, title) -> dict:
    from .quality.lot3 import run_accessibility_rules

    target, snap, idx, info, warn = _optional_geometry(call)
    opts, set_notes = _settings(call, target, "c5_pmr", {"referentiel": referentiel, "thresholds": thresholds})
    referentiel, thresholds = opts.get("referentiel"), opts.get("thresholds")
    if not referentiel:  # pas de valeur par défaut silencieuse
        raise ValueError("Référentiel PMR à choisir : « ERP 2017 », « BHC 2015 » ou « personnalisé » (paramètre "
                         "referentiel, ou réglage c5_pmr.referentiel avec set_audit_parameters / la page de réglage).")
    recs = records_by_uuid(snap)
    if scope is not None:
        if idx is None:
            raise ValueError("scope demande l'index géométrique, indisponible sur cette maquette.")
        sc = set(_sets(call, target, snap, idx, scope)[0])
    else:
        sc = None
    doors = [g for g, r in recs.items() if r.ifc_class.startswith("IfcDoor") and (sc is None or g in sc)]
    spaces = [g for g, r in recs.items() if r.ifc_class == "IfcSpace" and sc is not None and g in sc]
    result, notes, extra = run_accessibility_rules(idx, recs, referentiel=referentiel, thresholds=thresholds,
                                                   doors=doors, spaces=spaces, checks=checks, scoped=sc is not None)
    return _store(call, target, snap, result, ([warn] if warn else []) + set_notes + notes, options=opts,
                  title=title or f"Accessibilité PMR — {referentiel}", prefix="PMR",
                  type_livrable="Contrôle accessibilité PMR", sel_name="(maquette entière)", selection=None,
                  extra={"referentiel": referentiel, **extra},
                  intro="Contrôles d'aide à la décision sur l'accessibilité : seuils indicatifs du référentiel choisi, "
                        "à faire valider. Cheminements continus hors version 1.")


def run_fire_check(call: Call, compartments, required_ratings, compartment_rating, door_rating, exits, stairs,
                   max_distance_m, exit_up, stairs_up, checks, title, access_storeys=None,
                   stair_door_distance_m=None, distance_method="cheminement", path_cell_m=None,
                   open_stairs=None, f3_referentiel=None, f3_origin=None, protected_stairs=None, landing_doors=None,
                   eas=None, effectif=None, dead_end_max_m=None, pmr_door_min_m=None) -> dict:
    from .quality.lot3 import run_fire_rules

    target, snap, idx, info, warn = _optional_geometry(call)
    opts, set_notes = _settings(call, target, "c7_incendie", {
        "f3_referentiel": f3_referentiel, "max_distance_m": max_distance_m, "dead_end_max_m": dead_end_max_m,
        "pmr_door_min_m": pmr_door_min_m, "path_cell_m": path_cell_m})
    f3_referentiel, max_distance_m = opts.get("f3_referentiel"), opts.get("max_distance_m")
    dead_end_max_m, pmr_door_min_m, path_cell_m = opts.get("dead_end_max_m"), opts.get("pmr_door_min_m"), opts.get("path_cell_m")
    recs = records_by_uuid(snap)
    ex, st = ([], [])
    exits_info: dict = {}
    if idx is not None:
        from .geometry.zones import SpaceLocator
        from .quality.lot3 import auto_exits, is_balcony_door

        if isinstance(exits, str) and exits.strip().lower() in ("auto", "automatique"):
            ex, exits_info = auto_exits(idx, recs, access_storeys=access_storeys,
                                        stair_door_distance_m=stair_door_distance_m)
            crit = ", ".join(f"{k} : {v}" for k, v in exits_info["par_critere"].items()) or "aucune"
            lv = ", ".join(f"« {n} » ({k})" for n, k in exits_info["exterieures_par_niveau"].items()) or "aucun"
            notes_ex = [f"Niveaux d'accès retenus ({exits_info['origine_niveaux_acces']}) : {lv} — à confirmer ou "
                        "corriger avec access_storeys.",
                        f"Sorties détectées automatiquement : {exits_info['exterieures']} porte(s) extérieure(s) des "
                        f"niveaux d'accès + {exits_info['escaliers']} porte(s) "
                        f"d'accès aux escaliers ({crit}) ; {exits_info['balcons_exclus']} porte(s) de balcon et "
                        f"{exits_info['gaines_exclues']} trappe(s) de gaine ouvrant sur une cage et "
                        f"{exits_info['etroites_exclues']} porte(s) de moins de 0,60 m exclue(s). À vérifier."]
            if exits_info["etages_sans_sortie"]:
                notes_ex.append("Étages NON CONTRÔLÉS en F3 (des locaux, mais aucune sortie détectée) : "
                                + ", ".join(f"« {n} »" for n in exits_info["etages_sans_sortie"])
                                + " — désigner les sorties (exits) pour les contrôler.")
            if exits_info["etages_sans_objet"]:
                notes_ex.append("Étages sans objet pour F3 (aucun local) : "
                                + ", ".join(f"« {n} »" for n in exits_info["etages_sans_objet"]) + ".")
        else:
            ex = _sets(call, target, snap, idx, exits)[0]
            loc = SpaceLocator(idx)
            balc = [g for g in ex if is_balcony_door(idx, recs, g, loc)]
            ex = [g for g in ex if g not in set(balc)]
            notes_ex = [f"{len(balc)} porte(s) de balcon retirée(s) des sorties (jamais une sortie)."] if balc else []
            info_ = auto_exits(idx, recs, access_storeys=access_storeys)[1]
            exits_info = {"escaliers_ouverts": info_["escaliers_ouverts"],
                          "escaliers_ouverts_relies": info_["escaliers_ouverts_relies"]}
        opens = exits_info.get("escaliers_ouverts") or []
        if opens:
            names = idx.storey_names()
            lbl = ", ".join(f"« {(recs[g].attrs.get('LongName') if g in recs else '') or idx.elements[g]['name']} "
                            f"{(recs[g].attrs.get('Name') if g in recs else '') or ''} » "
                            f"({names.get(idx.elements[g].get('storey'), '?')})" for g in opens)
            linked = set(exits_info.get("escaliers_ouverts_relies") or [])
            counted = opens if open_stairs is True else ([] if open_stairs is False else
                                                         [g for g in opens if g in linked])
            notes_ex.append(f"Escaliers ouverts (local contenant un escalier, sans porte) : {lbl} — "
                            f"{len(counted)} compté(s) comme dégagement non protégé (30 m, CO 49)"
                            + (" : ceux dont la cage rejoint le niveau d'accès (défaut ; open_stairs=True / False pour "
                               "tous / aucun)." if open_stairs is None else " (open_stairs)."))
            exits_info["escaliers_ouverts_comptes"] = counted
        st = _sets(call, target, snap, idx, stairs)[0]
        from .quality.lot3 import access_levels_of

        acc = {x["guid"] for x in access_levels_of(idx, access_storeys)}
        if isinstance(protected_stairs, str) and protected_stairs.strip().lower() in ("all", "tous", "none", "aucun"):
            prot = "all" if protected_stairs.strip().lower() in ("all", "tous") else "none"
        elif protected_stairs:
            prot = set(_sets(call, target, snap, idx, protected_stairs)[0])
        else:
            prot = None
        lands = _sets(call, target, snap, idx, landing_doors)[0] if landing_doors else None
        eas_l = _sets(call, target, snap, idx, eas)[0] if eas else None
    else:
        notes_ex = []
        acc, prot, lands, eas_l = None, None, None, None
    result, notes, extra = run_fire_rules(
        idx, recs, required_ratings=required_ratings, compartments=compartments,
        compartment_rating=compartment_rating, door_rating=door_rating, exits=ex, stairs=st,
        max_distance_m=max_distance_m, exit_up=exit_up, stairs_up=stairs_up, checks=checks,
        distance_method=distance_method or "cheminement", path_cell_m=path_cell_m,
        exit_spaces=exits_info.get("escaliers_ouverts_comptes") or None,
        f3_referentiel=f3_referentiel, f3_origin=f3_origin, protected_stairs=prot, access_levels=acc,
        landing_doors=lands, eas=eas_l, effectif=effectif, dead_end_max_m=dead_end_max_m,
        pmr_door_min_m=pmr_door_min_m or 0.90)
    out = _store(call, target, snap, result, ([warn] if warn else []) + set_notes + notes_ex + notes, options=opts,
                 title=title or "Sécurité incendie et évacuation", prefix="INCENDIE",
                 type_livrable="Contrôle incendie", sel_name="(maquette entière)", selection=None,
                 extra={**extra, "sorties": len(ex), "detail_sorties": exits_info, "escaliers": len(st)},
                 intro="Contrôles d'aide à la décision : degrés coupe-feu, parois entre compartiments, distance "
                       "de marche indicative aux sorties, unités de passage. Pas une attestation de conformité.")
    return out


# ── Menu d'audit ────────────────────────────────────────────────────────

#: Paramètres que le menu doit demander avant de lancer (pas de valeur par défaut silencieuse).
AUDIT_QUESTIONS: dict[str, list[dict]] = {
    "c5_pmr": [{"option": "referentiel", "question": "Quel référentiel PMR appliquer ?",
                "choix": ["ERP 2017", "BHC 2015", "personnalisé"]}],
    "c7_incendie": [
        {"option": "f3_referentiel", "question": "Quel référentiel pour les distances d'évacuation (F3, F5) ?",
         "choix": ["ERP", "ERP_J", "habitation_3A", "habitation_3B_4", "code_travail", "personnalise"]},
        {"option": "eas", "question": "Quels espaces d'attente sécurisés (F6) ? (sélection de locaux, ou null "
                                      "si aucun : F6 non contrôlée)", "choix": None}],
}

AUDITS = [
    ("general", "Audit général de la maquette", "run_audit"),
    ("nommage", "Contrôle du nommage", "check_naming_completeness"),
    ("c2_clash", "Conflits (clash)", "run_clash_matrix"),
    ("c5_pmr", "Accessibilité PMR", "run_accessibility_check"),
    ("c6_degagements", "Dégagements devant les portes", "run_clearance_check"),
    ("c7_incendie", "Sécurité incendie et évacuation", "run_fire_check"),
    ("ccbim", "Audit spécifique selon votre CCBIM", "run_audit"),
    ("c3_informations", "Contrôle sur tableau d'exigences (gabarit Excel)", "run_requirements_check"),
    ("c1_structure", "Structuration et intégrité IFC", "run_structure_check"),
    ("c4_geometrie", "Géométrie et modélisation", "run_geometry_quality_check"),
    ("classifications", "Classifications", "audit_classifications"),
]
#: Menu présenté à l'utilisateur : familles d'audits, dans cet ordre.
MENU_GROUPS = [
    ("Audit général", "Vue d'ensemble de la maquette : inventaire, niveaux, locaux, typage des objets, nommage et "
                      "complétude des données de base. Rapide, sans calcul géométrique.", ["general"]),
    ("Contrôle du nommage", "Noms renseignés, non génériques, sans caractère interdit ; niveaux, locaux et zones "
                            "nommés et uniques.", ["nommage"]),
    ("Conflits (clash)", "Doublons, inclusions et chevauchements entre disciplines (matrice architecture, structure, "
                         "fluides…).", ["c2_clash"]),
    ("Réglementaire", "Accessibilité PMR, dégagements devant les portes, sécurité incendie et évacuation. Aide à la "
                      "décision : référentiel à choisir.", ["c5_pmr", "c6_degagements", "c7_incendie"]),
    ("Audit spécifique selon votre CCBIM", "Contrôle de la maquette selon les attendus de votre cahier des charges "
     "BIM : nommage, propriétés, classification, niveaux, version IFC… Rapport PDF et classeur Excel.", ["ccbim"]),
    ("Contrôles avancés", "Contrôles détaillés type Solibri : structuration IFC, géométrie, classifications ; "
                          "contrôle sur un tableau d'exigences au format gabarit Excel.",
     ["c1_structure", "c4_geometrie", "classifications", "c3_informations"]),
]
NOT_YET: dict[str, str] = {}


def _audit_status(call: Call, snap, recs) -> dict[str, dict]:
    from .geometry.source import ifc_url

    has_ifc = bool(ifc_url(snap.model or {}))
    size = ((snap.model or {}).get("document") or {}).get("size") or 0
    est = round(size / 2**20 * _S_PER_MB) if size else None
    bg = size and size / 2**20 > _background_mb() > 0
    geo_reason = (f"index géométrique calculé au premier lancement (≈ {est} s"
                  + (", en tâche de fond : relancer l'audit quand il est prêt)" if bg else ")") if est else
                  "index géométrique calculé au premier lancement")
    reqsets = call.state.list("requirements")
    n_cls = sum(1 for r in recs if r.classifications)
    out: dict[str, dict] = {}
    for aid, label, tool in AUDITS:
        d = {"id": aid, "label": label, "outil": tool}
        if aid in NOT_YET:
            d.update(status="bloque", reason=f"Pas encore disponible sur ce serveur (prévu au {NOT_YET[aid]}).")
        elif aid in ("nommage", "general"):
            d.update(status="pret")
        elif aid == "classifications":
            d.update(status="pret") if n_cls else d.update(
                status="partiel", reason="Aucune classification dans la maquette : tous les éléments seront "
                                         "signalés « manquante », sans proposition.")
        elif aid == "ccbim":
            from .service import latest_ccbim

            cc = latest_ccbim(call)
            if cc:
                n = len((cc["expectations"] or {}).get("requirements") or [])
                d.update(status="pret", ccbim_id=cc["ccbim_id"],
                         reason=f"CCBIM « {cc['name']} » analysé ({n} exigence(s) de données).")
            else:
                d.update(status="bloque", reason="Aucun CCBIM transmis.", next="read_ccbim", question=CCBIM_QUESTION)
        elif aid == "c3_informations":
            if reqsets:
                d.update(status="pret", requirements_id=reqsets[0]["requirements_id"],
                         reason=f"Cahier des charges « {reqsets[0]['name']} » (le plus récent).")
            else:
                d.update(status="bloque", reason="Aucun tableau d'exigences importé.",
                         next="import_data_requirements", question=CDC_QUESTION)
        elif aid == "c1_structure":
            d.update(status="pret", reason=geo_reason) if has_ifc else d.update(
                status="bloque", reason="Fichier IFC source indisponible sur la maquette : relations et géométrie "
                                        "non lisibles.")
        elif aid == "c4_geometrie":
            d.update(status="pret", reason=geo_reason) if has_ifc else d.update(
                status="partiel", reason="Fichier IFC indisponible : G1, G7 (données) et G8 seulement.")
        elif aid == "c2_clash":
            from .quality.clash_check import discipline_of

            present = Counter(discipline_of({"ifc_class": r.ifc_class}, r) for r in recs if r.is_physical)
            if not has_ifc:
                d.update(status="bloque", reason="Fichier IFC source indisponible : géométrie non lisible.")
            elif len(present) < 2:
                d.update(status="partiel", reason=f"Une seule discipline détectée ({', '.join(present) or 'aucune'}) :"
                                                  " seules les cases intra-discipline (doublons) ont un objet.")
            else:
                d.update(status="pret", reason="Matrice par défaut : " + ", ".join(
                    f"{k} ({v})" for k, v in present.most_common()) + f" ; {geo_reason}")
        elif aid == "c5_pmr":
            d.update(status="partiel", reason="Référentiel à choisir (options c5_pmr.referentiel : BHC 2015 ou ERP "
                                              "2017) ; A2 et A4 à paramétrer, A3 : locaux à désigner (scope) ; seuils "
                                              "à valider.", a_demander=AUDIT_QUESTIONS["c5_pmr"])
        elif aid == "c6_degagements":
            d.update(status="pret", reason="Préréglage portes (1,00 m côté ouverture) ; " + geo_reason) if has_ifc \
                else d.update(status="bloque", reason="Fichier IFC source indisponible : géométrie non lisible.")
        elif aid == "c7_incendie":
            from .quality.lot3 import FIRE_CLASSES, _fire_value, _is

            rated = sum(1 for r in recs if _is(r.ifc_class, FIRE_CLASSES) and _fire_value(r) not in (None, ""))
            why = []
            if not rated:
                why.append("Aucune propriété FireRating : F1 et F2 non contrôlables")
            why.append("sorties détectées automatiquement (portes extérieures des niveaux d'accès + accès "
                       "escaliers, à vérifier) ; F3 et F5 demandent le référentiel (f3_referentiel), F6 les EAS (eas)")
            d.update(status="partiel", reason=" ; ".join(why) + ".", a_demander=AUDIT_QUESTIONS["c7_incendie"])
        out[aid] = d
    return out


CCBIM_QUESTION = ("Souhaitez-vous en plus un audit spécifique selon votre cahier des charges BIM (CCBIM) ? Si oui, "
                  "transmettez-le (PDF, Word ou Excel) : chemin du fichier sur votre ordinateur, document de la GED "
                  "BIMData, ou pièce jointe à la conversation. J'en extrais les attendus contrôlables, vous les "
                  "validez, puis je contrôle la maquette (rapport PDF et classeur Excel).")
CDC_QUESTION = ("Souhaitez-vous un contrôle sur votre cahier des charges ? Déposez-le (Excel) dans la GED du projet "
                "BIMData ou joignez-le à la conversation ; à défaut, je vous fournis un gabarit à compléter.")


def _fr_int(n) -> str:
    return f"{n:,}".replace(",", "\u202f") if isinstance(n, int) else str(n)


#: Précisions affichées dans le menu, en clair.
MENU_HINTS = {"c5_pmr": "référentiel à choisir : ERP 2017, ou BHC 2015 pour l'habitation",
              "c7_incendie": "référentiel à choisir : habitation 3A, 3B ou 4, ERP, code du travail…"}


def _menu_message(model: str, project: str, n_el: int, n_st: int, st: dict[str, dict]) -> str:
    """Message du menu, en français, prêt à afficher."""
    head = f"Maquette chargée : « {model} »" + (f" — {project}" if project else "")
    lines = [f"{head} ({_fr_int(n_el)} éléments, {n_st} niveaux).", "",
             "Les audits se lancent un par un : avant chaque audit, je vous présente ses hypothèses (paramètres, "
             "référentiel) pour validation ; à la fin, vous recevez un rapport PDF. Par lequel souhaitez-vous "
             "commencer ?", ""]
    k = 0
    for title, desc, ids in MENU_GROUPS:
        k += 1
        lines.append(f"{k}. {title} — {desc}")
        for aid in ids:
            d = st[aid]
            if aid == "ccbim":
                continue  # remplacé par la question du dépôt (ou prêt : rien à ajouter)
            if len(ids) > 1 or d["status"] != "pret":
                extra = MENU_HINTS.get(aid, "")
                if not extra and d["status"] in ("bloque", "partiel"):
                    r = (d.get("reason") or "").split(" : ")[0].rstrip(".")
                    extra = ("non disponible : " if d["status"] == "bloque" else "") + r[:1].lower() + r[1:]
                label = next(a[1] for a in AUDITS if a[0] == aid)
                lines.append(f"   • {label}" + (f" — {extra}" if extra else ""))
        if "ccbim" in ids:
            cc = st["ccbim"]
            lines.append("   " + (CCBIM_QUESTION if cc["status"] == "bloque" else
                                  cc["reason"] + " Un autre CCBIM peut être transmis à la place."))
    return "\n".join(lines)


def list_audits(call: Call) -> dict:
    target, _, snap, _ = _ctx(call)
    recs, _ = _selection_records(call, None, target, snap)
    st = _audit_status(call, snap, recs)
    from . import audit_settings as AS

    owner = call.identity.storage_key
    params = {b["audit"]: b["parametres"] for b in AS.parameters(owner, target.key)}
    out = []
    for a in AUDITS:
        d = dict(st[a[0]])
        if a[0] in params:
            d["parametres"] = [{"cle": p["key"], "libelle": p["label"], "valeur": p["valeur"], "defaut": p["default"],
                                "unite": p["unit"], "plage": [p["min"], p["max"]] if p["min"] is not None else p["choices"],
                                "modifie": p["modifie"], "a_fournir": p["optional"] and p["valeur"] is None}
                               for p in params[a[0]]]
        out.append(d)
    from .service import _doc_ctx

    ctx = _doc_ctx(call, snap, target, "", "")
    n_el = sum(1 for r in recs if r.is_physical)
    groups = [{"famille": t, "description": dsc, "audits": ids} for t, dsc, ids in MENU_GROUPS]
    return {"message_utilisateur": _menu_message(ctx.maquette, ctx.projet, n_el, len(snap.storeys or []), st),
            "familles": groups, "audits": out, **AS.link(owner, target.key),
            "consigne": "Répondre en français. Afficher message_utilisateur tel quel (ou très proche), puis faire "
                        "choisir UN audit (choix simple, pas d'option « Tous ») ; pour la famille « Réglementaire », "
                        "demander lequel des trois audits. Un audit bloqué est affiché mais non lançable ; pour le "
                        "cahier des charges, poser la question (dépôt dans la GED → import_data_requirements, ou "
                        "requirements_template). Pour l'audit choisi : run_audit(audit) pour ses hypothèses, les faire "
                        "valider (ou modifier via set_audit_parameters / reglages_url), puis run_audit(audit, "
                        "hypotheses_validees=jeton)."}


def audit_parameters(call: Call, audits) -> dict:
    from . import audit_settings as AS

    target, _, _, _ = _ctx(call)
    owner = call.identity.storage_key
    return {"maquette": target.to_dict(), "parametres": AS.parameters(owner, target.key, audits),
            **AS.link(owner, target.key, audits),
            "consigne": "Présenter, par audit, chaque paramètre avec sa valeur retenue, sa valeur par défaut, sa plage "
                        "et sa source ; proposer la page à curseurs (reglages_url) ou set_audit_parameters."}


def set_audit_parameters(call: Call, values, reset) -> dict:
    from . import audit_settings as AS

    target, _, _, _ = _ctx(call)
    owner = call.identity.storage_key
    saved = AS.save(owner, target.key, values or {}, merge=not reset)
    return {"enregistres": saved, "nombre_modifies": len(saved),
            "message": "Réglages enregistrés pour cette maquette : ils s'appliquent au prochain run_audit et figurent "
                       "au chapitre « Hypothèses » du rapport."}


def smart_view_offer(check_id: str, title: str, issues: list[dict]) -> dict | None:
    """Proposition de Smart View après un rapport : éléments colorés par criticité (non conformes) et conformes.
    Ne prépare rien (Smart Views seulement sur demande explicite)."""
    real = [i for i in issues if i.get("status") != "exclu" and i.get("uuid")]
    if not real:
        return None
    return {"controle": title, "check_id": check_id, "elements_en_anomalie": len({i["uuid"] for i in real}),
            "appel": {"outil": "prepare_smart_view", "arguments": {"check_id": check_id, "title": f"{title} — anomalies"}},
            "contenu": "coloration dans le viewer : non conformes par criticité (bloquant, majeur, mineur, info), "
                       "conformes en vert",
            "question": (f"Voulez-vous une Smart View « {title} » qui colore dans le viewer BIMData les éléments en "
                         "anomalie par criticité ? Je vous proposerai de choisir la sélection (par gravité, par règle, "
                         "par type ou par niveau, ou une Smart View par règle). Rien n'est écrit dans BIMData avant "
                         "votre accord sur le plan.")}


def _ged_offer(check_id: str, snap) -> dict:
    from .service import ged_offer

    return ged_offer(check_id, snap)


def _next_steps(check_id: str, title: str, issues: list[dict], insight: dict | None = None) -> dict:
    bcf = bcf_offer(check_id, title, issues)
    if bcf and insight and insight.get("recommandation_bcf"):  # résultats douteux : le dire avant de proposer
        bcf["mise_en_garde"] = insight["recommandation_bcf"]
        bcf["regles_douteuses"] = insight.get("regles_douteuses")
        bcf["question"] = "Attention : " + insight["recommandation_bcf"] + " " + bcf["question"]
    sv = smart_view_offer(check_id, title, issues)
    choices = [x for x in (("des topics BCF" if bcf else None), ("une Smart View" if sv else None)) if x]
    return {"proposition_bcf": bcf, "proposition_smart_view": sv,
            "question_suite": (f"Le rapport PDF est prêt. Souhaitez-vous {' ou '.join(choices)} pour traiter les "
                               "anomalies dans BIMData ?") if choices else None}


def _hypotheses_token(call: Call, target, aid: str, opts: dict, selection) -> str:
    import json

    from . import audit_settings as AS

    body = json.dumps(opts, sort_keys=True, default=str, ensure_ascii=False)
    return AS._sign("hypotheses", call.identity.storage_key, target.key, aid, selection or "", body)[:16]


def run_audit(call: Call, audit, options, selection, title, hypotheses_validees=None) -> dict:
    """Un audit à la fois, choisi par l'utilisateur, en deux temps : 1) présentation des hypothèses (paramètres
    retenus, défauts, plages, questions) avec un jeton ; 2) lancement avec ce jeton, une fois les hypothèses
    validées. Livrable : rapport PDF de l'audit (annexe Excel), puis proposition de topics BCF ou de Smart View."""
    from . import audit_params as AP
    from . import audit_settings as AS
    from . import service

    ids = [a[0] for a in AUDITS]
    if isinstance(audit, (list, tuple)):
        if len(audit) != 1:
            raise ValueError("Un seul audit à la fois : faire choisir à l'utilisateur un audit du menu "
                             f"(list_audits) parmi {ids}, puis valider ses hypothèses avant de le lancer.")
        audit = audit[0]
    if not audit:
        raise ValueError(f"Audit à choisir : présenter le menu (list_audits) et faire choisir un audit parmi {ids}.")
    if str(audit).lower() in ("tous", "all", "*"):
        raise ValueError("Les audits ne se lancent pas tous ensemble : faire choisir un audit à l'utilisateur "
                         "(list_audits), valider ses hypothèses, puis le lancer ; les suivants ensuite, un par un.")
    aid = audit
    if aid not in ids:
        raise ValueError(f"Audit inconnu : {aid!r}. Identifiants : {ids}")
    target, _, snap, _ = _ctx(call)
    recs, _ = _selection_records(call, None, target, snap)
    st = _audit_status(call, snap, recs)[aid]
    label = next(a[1] for a in AUDITS if a[0] == aid)
    if st["status"] == "bloque":
        raise ValueError(f"Audit « {label} » non lançable : {st.get('reason')}"
                         + (f" (étape suivante : {st['next']})" if st.get("next") else "")
                         + (f" {st['question']}" if st.get("question") else ""))
    explicit = options or {}
    if aid in explicit and isinstance(explicit[aid], dict):  # forme {audit: {...}} acceptée
        explicit = explicit[aid]
    saved = AS.load(call.identity.storage_key, target.key)
    o, hyp = AP.apply_to_options(aid, saved, explicit)
    refs, method = AP.REFERENCES.get(aid) or [], AP.METHODS.get(aid)
    if aid == "ccbim":  # hypothèses = attendus tirés du CCBIM (le jeton change s'ils sont modifiés)
        from . import ccbim as CC

        cc = service.latest_ccbim(call, o.get("ccbim_id") or st.get("ccbim_id"))
        exp = CC.Expectations(**cc["expectations"])
        o = {"ccbim_id": cc["ccbim_id"], "attendus": cc["expectations"]}
        hyp = CC.hypotheses(exp)
        refs = [f"Cahier des charges BIM du projet : « {cc['name']} »."]
        method = AP.METHODS.get("c3_informations")
    if aid == "c2_clash" and not o.get("matrix") and not o.get("file_id") and any(
            o.get(k) is not None for k in ("tol_h_m", "tol_v_m", "contact_m")):
        from .quality.clash_check import DEFAULT_MATRIX

        o["matrix"] = [dict(c, **{k: o[k] for k in ("tol_h_m", "tol_v_m", "contact_m") if o.get(k) is not None})
                       for c in DEFAULT_MATRIX]
    ask = [q for q in AUDIT_QUESTIONS.get(aid, []) if q["option"] not in o]
    token = None if ask else _hypotheses_token(call, target, aid, o, selection)
    if not token or hypotheses_validees != token:
        rows = [{"parametre": h["parametre"], "valeur": "à fournir" if h["a_fournir"] else h["valeur"],
                 "defaut": h["defaut"], "unite": h["unite"], "plage": h["plage"], "source": h["source"],
                 "modifie": h["modifie"], "cle": h["cle"]} for h in hyp]
        return {
            "etape": "validation_hypotheses", "audit": aid, "label": label, "statut_prerequis": st["status"],
            "raison_partiel": st.get("reason") if st["status"] == "partiel" else None,
            "hypotheses": rows, "references": refs, "methode": method,
            **({"non_controlables": exp.not_checkable} if aid == "ccbim" else {}),
            "a_demander": ask, "jeton_hypotheses": token, **AS.link(call.identity.storage_key, target.key, [aid]),
            "jeton_refuse": bool(hypotheses_validees) and hypotheses_validees != token,
            "consigne": ("Présenter les attendus tirés du CCBIM (tableau : référence, attendu, règle, portée, source) "
                         "et les attendus non contrôlables. Proposer de les corriger (set_ccbim_expectations). Ne "
                         "lancer l'audit qu'après validation explicite : run_audit('ccbim', "
                         "hypotheses_validees=jeton_hypotheses).") if aid == "ccbim" else (
                         "Présenter ces hypothèses à l'utilisateur (tableau : paramètre, valeur retenue, défaut, plage, "
                         "source) avec le référentiel et la méthode. "
                         + ("Poser d'abord les questions de a_demander (aucune valeur par défaut silencieuse), puis "
                            "rappeler run_audit avec les réponses dans options. " if ask else "")
                         + "Proposer de modifier les valeurs (set_audit_parameters ou page à curseurs reglages_url). "
                           "Ne lancer l'audit qu'après validation explicite : run_audit(audit, "
                           "hypotheses_validees=jeton_hypotheses). Tout changement de paramètre donne un nouveau "
                           "jeton, à faire valider à son tour."),
        }
    t0 = time.time()
    t = title or label
    if aid == "general":
        r = service.general_audit(call, selection=selection, title=t)
    elif aid == "ccbim":
        r = service.ccbim_audit(call, ccbim_id=o["ccbim_id"], selection=selection, title=title)
    elif aid == "nommage":
        r = service.check_naming_completeness(
            call, selection=selection, custom_rules=None, include_default_rules=True,
            include_default_names_rule=o.get("include_default_names_rule", True),
            include_classification=o.get("include_classification", False), phase=o.get("phase"), title=t)
    elif aid == "classifications":
        r = service.audit_classifications(call, selection=selection, system=o.get("system"), title=t,
                                          auto_fill_threshold=o.get("auto_fill_threshold", 0.8))
    elif aid == "c1_structure":
        r = run_structure_check(call, selection=selection, checks=o.get("checks"),
                                expected_buildings=o.get("expected_buildings", 1), s4_thresholds=o.get("s4_thresholds"),
                                unique_property=o.get("unique_property"),
                                door_direction_property=o.get("door_direction_property"),
                                georef_expected=o.get("georef_expected"), title=t)
    elif aid == "c3_informations":
        r = service.run_requirements_check(
            call, rid=o.get("requirements_id") or st["requirements_id"], selection=selection, phase=o.get("phase"),
            include_naming_rules=False, title=t, auto_fill_threshold=o.get("auto_fill_threshold", 0.8))
    elif aid == "c4_geometrie":
        r = run_geometry_quality_check(call, selection=selection, checks=o.get("checks"),
                                       thresholds=o.get("thresholds"), title=t)
    elif aid == "c5_pmr":
        r = run_accessibility_check(call, referentiel=o["referentiel"], scope=o.get("scope"),
                                    thresholds=o.get("thresholds"), checks=o.get("checks"), title=t)
    elif aid == "c6_degagements":
        r = run_clearance_check(call, preset=o.get("preset", "portes"), targets=o.get("targets"),
                                obstacles=o.get("obstacles"), front_m=o.get("front_m"), back_m=o.get("back_m"),
                                side_m=o.get("side_m"), height_m=o.get("height_m"), width=o.get("width"),
                                use_opening_side=o.get("use_opening_side"), tolerance_m=o.get("tolerance_m", 0.05),
                                title=t)
    elif aid == "c7_incendie":
        r = run_fire_check(call, compartments=o.get("compartments", "zones"),
                           required_ratings=o.get("required_ratings"), compartment_rating=o.get("compartment_rating"),
                           door_rating=o.get("door_rating"), exits=o.get("exits", "auto"), stairs=o.get("stairs"),
                           access_storeys=o.get("access_storeys") or o.get("access_storey"),
                           stair_door_distance_m=o.get("stair_door_distance_m"),
                           distance_method=o.get("distance_method", "cheminement"), path_cell_m=o.get("path_cell_m"),
                           open_stairs=o.get("open_stairs"), f3_referentiel=o.get("f3_referentiel"),
                           f3_origin=o.get("f3_origin"), protected_stairs=o.get("protected_stairs"),
                           landing_doors=o.get("landing_doors"), eas=o.get("eas"), effectif=o.get("effectif"),
                           dead_end_max_m=o.get("dead_end_max_m"), pmr_door_min_m=o.get("pmr_door_min_m"),
                           max_distance_m=o.get("max_distance_m"), exit_up=o.get("exit_up", 1),
                           stairs_up=o.get("stairs_up", 1), checks=o.get("checks"), title=t)
    else:  # c2_clash
        r = run_clash_matrix(call, matrix=o.get("matrix"), file_id=o.get("file_id"), bimdata_document_id=None,
                             discipline_property=o.get("discipline_property"), title=t)
    s = r["summary"]
    issues = (call.state.load("checks", r["check_id"]) or {}).get("issues") or []
    others = [a[1] for a in AUDITS if a[0] != aid]
    return {
        "etape": "termine", "livrable": r["livrable"],
        "livrables": [{"format": "PDF", **{k: v for k, v in r["livrable"].items() if k != "annexe_excel"}}]
        + ([{"format": "Excel", **r["livrable"]["annexe_excel"]}] if r["livrable"].get("annexe_excel") else []),
        "audit": {"id": aid, "audit": label, "check_id": r["check_id"],
                  "hypotheses_appliquees": [h["parametre"] + f" = {h['valeur']} {h['unite'] or ''}".rstrip()
                                            for h in hyp],
                  "parametres_modifies": [h["parametre"] + f" = {h['valeur']} {h['unite'] or ''}".rstrip()
                                          for h in hyp if h["modifie"]],
                  "regles_controlees": f"{s.get('rules_controlled', '?')} sur {s.get('rules_total', '?')}",
                  "anomalies": s["non_conformities"], "par_severite": s["by_severity"],
                  "elements_controles": s["elements_controlled"], "statut_prerequis": st["status"],
                  "raison_partiel": st.get("reason") if st["status"] == "partiel" else None,
                  "duree_s": round(time.time() - t0, 1), "avertissements": r.get("avertissements", [])},
        **_next_steps(r["check_id"], label, issues, r.get("analyse_automatique")),
        "proposition_ged": r.get("proposition_ged") or _ged_offer(r["check_id"], snap),
        "analyse_automatique": r.get("analyse_automatique"),
        "consigne": ("D'abord, analyser les résultats comme un BIM manager : fiabilité de chaque règle (faux positifs, "
                     "tolérances trop strictes, données d'entrée manquantes, objets hors champ — voir "
                     "analyse_automatique et get_check_issues), ce qu'il faut retenir, prérequis avant un contrôle "
                     "définitif (qui fait quoi), recommandation sur les BCF ; puis annotate_audit_report(check_id, "
                     "analyse) pour placer cette analyse en tête du PDF. Ensuite seulement, "
                     "présenter le rapport PDF et l'Excel (livrable renvoyé par annotate_audit_report) en commençant "
                     "par cette analyse, proposer de les déposer dans la GED (proposition_ged : dossier par défaut, "
                     "ou un autre nom au choix), puis poser question_suite : topics BCF "
                     "(proposition_bcf) ou Smart View (proposition_smart_view). Ne rien préparer sans accord "
                     "explicite. Un autre audit se lance ensuite à part, après choix et validation de ses "
                     "hypothèses."),
        "autres_audits": others,
    }


def _model_info(snap) -> dict:
    m = snap.model or {}
    doc = m.get("document") or {}
    return {"fichier": doc.get("file_name") or m.get("name"), "date": str(doc.get("created_at") or "")[:10] or None,
            "schema": m.get("version")}

