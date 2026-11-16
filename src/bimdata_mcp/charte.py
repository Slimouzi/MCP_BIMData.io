"""Charte graphique BIMData et **trame commune** de tous les livrables Excel.

Tous les fichiers produits par le serveur (export de données, COBie, rapport de
contrôle, gabarit de cahier des charges…) sont construits avec :class:`Livrable`.
Ils partagent donc, sans exception :

1. **Même ordre de feuilles** : ``Synthèse`` → feuilles de données → ``Lisez-moi``
   → ``_bimdata`` (masquée, métadonnées de réimport).
2. **Même Synthèse** : logo BIMData, surtitre « BIMDATA — <TYPE> », titre, bloc
   d'identification aux libellés et à l'ordre fixes (Projet, Maquette,
   Identifiants, Périmètre, Phase, Généré le, Généré par, Outil), ligne de
   chiffres clés, puis tableaux de synthèse.
3. **Mêmes tableaux** : ligne de groupes (filet jaune Secondary) puis ligne
   d'en-têtes (Primary, texte blanc), lignes zébrées Blue Neutral Light,
   filtres, volets figés, formats numériques identiques.
4. **Même code couleur** : jaune clair = cellule à saisir ; vert / orange /
   rouge réservés aux états (conforme, avertissement, non conforme) ; criticité
   bloquant (High) / majeur (Warning) / mineur (Secondary) / info (Royal blue).
5. **Même Lisez-moi** : objet du document, sommaire des feuilles généré,
   sections propres au livrable, légende commune.
6. **Même nommage de fichier** : ``AAAAMMJJ_<projet>_<maquette>_<TYPE>.xlsx``.
7. **Même mise en page d'impression** : paysage, ajusté en largeur, en-têtes
   répétés, logo en en-tête, « BIMData.io — page n / N » en pied.

Tokens : ``bim_reporting.theming`` (BIMData Brand Guidelines 2022 v1.0).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.resources import files as _res
from pathlib import Path
from typing import Any

import xlsxwriter
from bim_reporting import theming as T
from bim_reporting.excel import neutralize_formula

from . import __version__

# ── Tokens ──────────────────────────────────────────────────────────────

def _h(x: str) -> str:
    return "#" + x.lstrip("#")


PRIMARY = _h(T.BIMDATA_PRIMARY)
SECONDARY = _h(T.BIMDATA_SECONDARY)
ROYAL_BLUE = _h(T.BIMDATA_ROYAL_BLUE)
GRANITE = _h(T.BIMDATA_GRANITE)
GRANITE_LIGHT = _h(T.BIMDATA_GRANITE_LIGHT)
SILVER_DARK = _h(T.BIMDATA_SILVER_DARK)
SILVER_LIGHT = _h(T.BIMDATA_SILVER_LIGHT)
BLUE_LIGHT = _h(T.BIMDATA_BLUE_NEUTRAL_LIGHT)
HIGH = _h(T.BIMDATA_HIGH)
WARNING = _h(T.BIMDATA_WARNING)
SUCCESS = _h(T.BIMDATA_SUCCESS)
FONT = T.BIMDATA_FONT_PRIMARY

#: Teintes claires des couleurs d'état (fonds de cellules lisibles à l'impression).
TINT = {"ok": "#DDF3E6", "ko": "#FFE0DA", "warn": "#FFEBD0", "edit": "#FEF4D5", "na": SILVER_LIGHT}
SEVERITY_COLORS = {"bloquant": HIGH, "majeur": WARNING, "mineur": SECONDARY, "info": ROYAL_BLUE}
#: Palette de séries (Smart Views « par valeur »), compatible charte : marque d'abord, états exclus.
SERIES = [ROYAL_BLUE, SECONDARY, PRIMARY, "#7A4FBF", "#2E9BD6", GRANITE_LIGHT, "#C2185B", "#8BC34A",
          "#00BCD4", "#795548", "#9C27B0", "#3F51B5", "#009688", "#CDDC39", "#E91E63", "#607D8B",
          SILVER_DARK, "#5D4037", "#1A237E", "#FFB300"]
#: Couleur d'onglet par rôle de feuille.
TAB = {"synthese": PRIMARY, "donnees": ROYAL_BLUE, "saisie": SECONDARY, "reference": GRANITE_LIGHT,
       "aide": SILVER_DARK}

LOGO_PATH = Path(str(_res("bimdata_mcp") / "assets" / "bimdata_logo.png"))
LOGO_SMALL = Path(str(_res("bimdata_mcp") / "assets" / "bimdata_logo_small.png"))
NUM_FMT = {"number": "#,##0.000", "integer": "#,##0", "percent": "0.0%"}

# ── Nommage ─────────────────────────────────────────────────────────────


def _slug(text: str, n: int) -> str:
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\.(ifc|ifczip|ifcxml)$", "", s, flags=re.I)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s[:n] or "NA"


def nom_fichier(type_court: str, projet: str = "", maquette: str = "", complement: str = "") -> str:
    """``AAAAMMJJ_<projet>_<maquette>[_<complément>]_<TYPE>.xlsx`` — identique pour tous les livrables."""
    parts = [datetime.now().strftime("%Y%m%d"), _slug(projet, 30), _slug(maquette, 40)]
    if complement:
        parts.append(_slug(complement, 30))
    parts.append(type_court.upper())
    return "_".join(parts) + ".xlsx"


# ── Modèle ──────────────────────────────────────────────────────────────


@dataclass
class Contexte:
    """Identification du livrable — affichée à l'identique en tête de chaque Synthèse."""

    type_livrable: str  # « EXPORT DE DONNÉES », « COBIE », « CONTRÔLE », « GABARIT »
    titre: str
    projet: str = ""
    maquette: str = ""
    cible: dict | None = None
    perimetre: str = ""
    phase: str = ""
    auteur: str = ""
    genere_le: str = field(default_factory=lambda: datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M"))

    def infos(self) -> list[tuple[str, str]]:
        c = self.cible or {}
        ids = f"espace {c.get('cloud_id')} / projet {c.get('project_id')} / maquette {c.get('model_id')}" if c else "—"
        return [
            ("Projet", self.projet or "—"),
            ("Maquette", self.maquette or "—"),
            ("Identifiants BIMData", ids),
            ("Périmètre", self.perimetre or "Maquette entière"),
            ("Phase", self.phase or "Toutes phases"),
            ("Généré le", self.genere_le),
            ("Généré par", self.auteur or "—"),
            ("Outil", f"BIMData MCP {__version__}"),
        ]


@dataclass
class Col:
    """Colonne d'une feuille. ``kind`` : text | number | integer | percent | edit | severity."""

    label: str
    group: str = ""
    kind: str = "text"
    width: float | None = None


def safe(value: Any) -> Any:
    """Valeur prête à écrire : listes jointes, booléens IFC, formules neutralisées."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple, set)):
        value = " | ".join(str(v) for v in value)
    elif isinstance(value, dict):
        value = "; ".join(f"{k}={v}" for k, v in value.items())
    text = str(value)
    if len(text) > 32000:
        text = text[:32000] + "…"
    return neutralize_formula(text)


# ── Livrable ────────────────────────────────────────────────────────────


class Livrable:
    """Classeur à la charte BIMData. Usage :

    ``lv = Livrable(path, ctx)`` → ``lv.synthese(kpis)`` → ``lv.tableau(...)`` →
    ``lv.feuille(...)`` (×n) → ``lv.lisez_moi(...)`` → ``lv.meta(...)`` → ``lv.close()``
    """

    def __init__(self, path: Path | str, ctx: Contexte):
        self.path = Path(path)
        self.ctx = ctx
        self.wb = xlsxwriter.Workbook(str(self.path), {"strings_to_formulas": False, "strings_to_urls": False,
                                                       "nan_inf_to_errors": True})
        self.wb.set_properties({"title": ctx.titre, "subject": ctx.type_livrable.title(), "author": "BIMData MCP",
                                "company": "BIMData.io", "comments": f"Généré par BIMData MCP {__version__}"})
        self._cache: dict[tuple, Any] = {}
        self.sommaire: list[tuple[str, str]] = []
        self._ws_synthese = None
        self._row = 0
        self.f = {
            "supertitle": self._f(bold=True, font_size=9, font_color=GRANITE),
            "title": self._f(bold=True, font_size=18, font_color=PRIMARY),
            "h2": self._f(bold=True, font_size=12, font_color=PRIMARY, bottom=2, bottom_color=SECONDARY),
            "filet": self._f(bg_color=SECONDARY),
            "info_key": self._f(bold=True, font_color=PRIMARY, bg_color=BLUE_LIGHT, border=1, border_color=SILVER_DARK),
            "info_val": self._f(border=1, border_color=SILVER_DARK, text_wrap=True),
            "kpi_label": self._f(font_size=9, font_color=GRANITE, align="center", valign="vcenter", text_wrap=True,
                                 bg_color=BLUE_LIGHT, top=2, top_color=SECONDARY, left=1, right=1,
                                 left_color=SILVER_DARK, right_color=SILVER_DARK),
            "group": self._f(bold=True, font_size=9, font_color=PRIMARY, bg_color=SECONDARY, align="center",
                             valign="vcenter", border=1, border_color="#FFFFFF"),
            "header": self._f(bold=True, font_color="#FFFFFF", bg_color=PRIMARY, align="center", valign="vcenter",
                              text_wrap=True, border=1, border_color=GRANITE),
            "text": self._f(text_wrap=True, valign="top"),
            "small": self._f(font_size=9, font_color=GRANITE, italic=True),
        }

    # formats ---------------------------------------------------------
    def _f(self, **kw):
        return self.wb.add_format({"font_name": FONT, "font_size": 10, **kw})

    def cell_fmt(self, kind: str = "text", *, alt: bool = False, state: str | None = None,
                 severity: str | None = None, wrap: bool = False):
        key = (kind, alt, state, severity, wrap)
        if key not in self._cache:
            kw: dict[str, Any] = {"border": 1, "border_color": SILVER_DARK, "valign": "top", "text_wrap": wrap}
            if alt:
                kw["bg_color"] = BLUE_LIGHT
            if kind in NUM_FMT:
                kw["num_format"] = NUM_FMT[kind]
            if kind == "edit":
                kw.update(bg_color=TINT["edit"], border_color=SECONDARY, locked=False)
            if state in TINT:
                kw["bg_color"] = TINT[state]
            if severity in SEVERITY_COLORS:
                kw.update(bg_color=SEVERITY_COLORS[severity], bold=True, align="center",
                          font_color=PRIMARY if severity == "mineur" else "#FFFFFF")
            self._cache[key] = self._f(**kw)
        return self._cache[key]

    def _kpi_fmt(self, kind: str):
        key = ("kpi", kind)
        if key not in self._cache:
            kw = {"bold": True, "font_size": 16, "font_color": PRIMARY, "align": "center", "valign": "vcenter",
                  "bg_color": BLUE_LIGHT, "bottom": 1, "left": 1, "right": 1, "border_color": SILVER_DARK}
            if kind in NUM_FMT:
                kw["num_format"] = NUM_FMT[kind]
            self._cache[key] = self._f(**kw)
        return self._cache[key]

    def _print(self, ws, repeat_rows: tuple[int, int] | None = None) -> None:
        ws.set_landscape()
        ws.set_paper(9)  # A4
        ws.fit_to_pages(1, 0)
        ws.set_margins(left=0.4, right=0.4, top=0.8, bottom=0.6)
        if LOGO_SMALL.exists():
            ws.set_header("&L&G&R&8" + self.ctx.type_livrable.replace("&", "&&"),
                          {"image_left": str(LOGO_SMALL), "scale_with_doc": False})
        ws.set_footer("&L&8BIMData.io — " + self.ctx.titre.replace("&", "&&")[:80] + "&R&8page &P / &N")
        if repeat_rows:
            ws.repeat_rows(*repeat_rows)

    # Synthèse --------------------------------------------------------
    def synthese(self, kpis: list[tuple[str, Any, str]] | None = None, intro: str = ""):
        ws = self.wb.add_worksheet("Synthèse")
        ws.set_tab_color(TAB["synthese"])
        ws.hide_gridlines(2)
        ws.set_column(0, 0, 2)
        ws.set_column(1, 1, 26)
        ws.set_column(2, 9, 17)
        ws.set_row(0, 8)
        if LOGO_PATH.exists():
            ws.insert_image(1, 1, str(LOGO_SMALL), {"x_scale": 0.55, "y_scale": 0.55, "object_position": 3})
        ws.write(1, 3, f"BIMDATA — {self.ctx.type_livrable.upper()}", self.f["supertitle"])
        ws.write(2, 3, safe(self.ctx.titre), self.f["title"])
        ws.set_row(2, 26)
        for c in range(3, 10):
            ws.write_blank(3, c, None, self.f["filet"])
        ws.set_row(3, 3)
        r = 6
        for k, v in self.ctx.infos():
            ws.write(r, 1, k, self.f["info_key"])
            ws.merge_range(r, 2, r, 6, safe(v), self.f["info_val"])
            r += 1
        r += 1
        if intro:
            ws.merge_range(r, 1, r, 8, safe(intro), self.f["text"])
            ws.set_row(r, 30)
            r += 2
        if kpis:
            ws.write(r, 1, "Chiffres clés", self.f["h2"])
            r += 1
            for i, (label, value, kind) in enumerate(kpis[:8]):
                ws.write(r, 1 + i, label, self.f["kpi_label"])
                ws.write(r + 1, 1 + i, safe(value), self._kpi_fmt(kind))
            ws.set_row(r, 28)
            ws.set_row(r + 1, 30)
            r += 3
        self._ws_synthese, self._row = ws, r + 1
        self.sommaire.append(("Synthèse", "Identification, chiffres clés et tableaux de synthèse."))
        self._print(ws)
        return ws

    def tableau(self, titre: str, cols: list[Col], rows: list[list], *, states: Callable | None = None,
                max_rows: int = 200, note: str = "") -> None:
        """Tableau de synthèse sous la Synthèse (même style que les feuilles de données)."""
        ws, r = self._ws_synthese, self._row
        ws.write(r, 1, titre, self.f["h2"])
        r += 1
        for c, col in enumerate(cols):
            ws.write(r, 1 + c, col.label, self.f["header"])
        ws.set_row(r, 28)
        for i, row in enumerate(rows[:max_rows]):
            for c, v in enumerate(row):
                st = states(i, c, v) if states else None
                kind = cols[c].kind if c < len(cols) else "text"
                fmt = self.cell_fmt(kind if kind != "severity" else "text", alt=bool(i % 2),
                                    state=st if kind != "severity" else None,
                                    severity=v if kind == "severity" else None, wrap=True)
                ws.write(r + 1 + i, 1 + c, safe(v), fmt)
        r += 1 + min(len(rows), max_rows)
        if len(rows) > max_rows:
            ws.write(r, 1, f"… {len(rows) - max_rows} ligne(s) supplémentaire(s) — voir les feuilles de détail.",
                     self.f["small"])
            r += 1
        if note:
            ws.write(r, 1, note, self.f["small"])
            r += 1
        self._row = r + 2

    # Feuilles de données ----------------------------------------------
    def feuille(self, nom: str, cols: list[Col], rows: list[list], *, description: str, role: str = "donnees",
                group_row: bool = True, freeze_cols: int = 1, states: Callable | None = None,
                validations: dict[int, list[str]] | None = None):
        ws = self.wb.add_worksheet(nom)
        ws.set_tab_color(TAB.get(role, ROYAL_BLUE))
        h = 0
        if group_row:
            start = 0
            for i in range(1, len(cols) + 1):
                if i == len(cols) or cols[i].group != cols[start].group:
                    g = cols[start].group or ""
                    if i - 1 > start:
                        ws.merge_range(0, start, 0, i - 1, safe(g), self.f["group"])
                    else:
                        ws.write(0, start, safe(g), self.f["group"])
                    start = i
            ws.set_row(0, 18)
            h = 1
        for c, col in enumerate(cols):
            ws.write(h, c, col.label, self.f["header"])  # en-têtes toujours Primary ; jaune = cellules à saisir
        ws.set_row(h, 32)
        for i, row in enumerate(rows):
            alt = bool(i % 2)
            for c, v in enumerate(row):
                kind = cols[c].kind if c < len(cols) else "text"
                st = states(i, c, v) if states else None
                if kind == "severity":
                    fmt = self.cell_fmt("text", alt=alt, severity=str(v) if v else None)
                elif kind == "text" and isinstance(v, float):
                    fmt = self.cell_fmt("number", alt=alt, state=st)
                else:
                    fmt = self.cell_fmt(kind, alt=alt, state=st)
                ws.write(h + 1 + i, c, safe(v), fmt)
        last = h + max(len(rows), 1)
        ws.autofilter(h, 0, last, max(len(cols) - 1, 0))
        ws.freeze_panes(h + 1, freeze_cols)
        for c, col in enumerate(cols):
            if col.width:
                w = col.width
            else:
                w = len(col.label)
                for row in rows[:400]:
                    if c < len(row) and row[c] is not None:
                        w = max(w, min(len(str(safe(row[c]))), 48))
                w = max(9, min(w + 2, 50))
            ws.set_column(c, c, w)
        for c, values in (validations or {}).items():
            ws.data_validation(h + 1, c, max(last, h + 1000), c, {"validate": "list", "source": values})
        self._print(ws, repeat_rows=(0, h))
        self.sommaire.append((nom, description))
        return ws

    # Lisez-moi --------------------------------------------------------
    def lisez_moi(self, objet: str, sections: list[tuple[str, list[str]]] | None = None) -> None:
        ws = self.wb.add_worksheet("Lisez-moi")
        ws.set_tab_color(TAB["aide"])
        ws.hide_gridlines(2)
        ws.set_column(0, 0, 2)
        ws.set_column(1, 1, 28)
        ws.set_column(2, 2, 100)
        ws.write(1, 1, f"BIMDATA — {self.ctx.type_livrable.upper()}", self.f["supertitle"])
        ws.write(2, 1, "Lisez-moi", self.f["title"])
        r = 4
        ws.write(r, 1, "Objet du document", self.f["h2"])
        ws.merge_range(r + 1, 1, r + 1, 2, safe(objet), self.f["text"])
        ws.set_row(r + 1, 32)
        r += 3
        ws.write(r, 1, "Contenu du classeur", self.f["h2"])
        r += 1
        for nom, desc in self.sommaire + [("Lisez-moi", "Cette notice.")]:
            ws.write(r, 1, nom, self.f["info_key"])
            ws.write(r, 2, safe(desc), self.f["info_val"])
            r += 1
        for titre, lignes in (sections or []):
            r += 1
            ws.write(r, 1, titre, self.f["h2"])
            r += 1
            for ligne in lignes:
                ws.merge_range(r, 1, r, 2, safe(ligne), self.f["text"])
                r += 1
        r += 1
        ws.write(r, 1, "Légende", self.f["h2"])
        r += 1
        legend = [
            (self.f["group"], "Groupe", "Ligne 1 des tableaux : groupe de colonnes (Attributs IFC, Pset, Qto, Constat…)."),
            (self.f["header"], "En-tête", "Ligne 2 : nom de la colonne / propriété."),
            (self.cell_fmt("edit"), "À saisir", "Cellule modifiable, relue lors de la réimport du fichier."),
            (self.cell_fmt("text", state="ok"), "Conforme", "Valeur conforme à l'exigence."),
            (self.cell_fmt("text", state="warn"), "À vérifier", "Valeur optionnelle absente ou point d'attention."),
            (self.cell_fmt("text", state="ko"), "Non conforme", "Valeur absente, vide ou invalide."),
        ] + [(self.cell_fmt("text", severity=s), s.capitalize(), f"Criticité « {s} ».") for s in SEVERITY_COLORS]
        for fmt, label, desc in legend:
            ws.write(r, 1, label, fmt)
            ws.write(r, 2, desc, self.f["info_val"])
            r += 1
        r += 1
        ws.merge_range(r, 1, r, 2, "Document généré par BIMData MCP — charte BIMData.io (Roboto, Primary #2F374A, "
                                   "Secondary #F9C72C).", self.f["small"])
        self._print(ws)

    def meta(self, data: dict) -> None:
        ws = self.wb.add_worksheet("_bimdata")
        ws.write(0, 0, "key")
        ws.write(0, 1, "value")
        base = {"generator": f"bimdata-mcp {__version__}", "type": self.ctx.type_livrable,
                "target": self.ctx.cible or {}, "generated_at": self.ctx.genere_le}
        for i, (k, v) in enumerate({**base, **data}.items(), start=1):
            ws.write(i, 0, k)
            ws.write(i, 1, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str))
        ws.hide()

    def close(self) -> None:
        if self._ws_synthese is not None:
            self._ws_synthese.activate()
        self.wb.close()
