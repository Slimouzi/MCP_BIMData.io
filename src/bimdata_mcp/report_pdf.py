"""Rapports PDF d'audit, à la charte BIMData, structurés comme un rapport d'AMO / BIM manager.

Plan type :

0. Page de garde (identification, avertissement) ;
1. Synthèse (avis global, chiffres clés, points majeurs, décisions attendues) ;
2. Objet, périmètre et documents d'entrée ;
3. Hypothèses et paramètres de calcul (valeur retenue, défaut, plage, source ; modifiés et à fournir signalés) ;
4. Référentiels et textes applicables ;
5. Méthode ;
6. Résultats détaillés par audit (règles, couverture, principales anomalies, points d'attention) ;
7. Recommandations et plan d'actions ;
8. Limites, réserves et avertissement ;
Annexe — Liste des anomalies (extrait ; liste complète dans l'annexe Excel).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import audit_params as AP
from .charte import (
    GRANITE,
    GRANITE_LIGHT,
    HIGH,
    LOGO_PATH,
    LOGO_SMALL,
    PRIMARY,
    SECONDARY,
    SEVERITY_COLORS,
    SILVER_DARK,
    SUCCESS,
    Contexte,
)

#: Teintes discrètes : en-têtes de tableau, filets, surlignage des valeurs modifiées.
HEAD_BG = "#F2F4F8"
RULE = "#DADFE7"
HIGHLIGHT = "#FFF8E1"

SEV_ORDER = {"bloquant": 0, "majeur": 1, "mineur": 2, "info": 3}
SEV_LABEL = {"bloquant": "Bloquant", "majeur": "Majeur", "mineur": "Mineur", "info": "Information"}
#: Action type par famille de règles (chapitre « Recommandations »).
ACTIONS = {
    "S": ("Modeleur", "Corriger la structuration dans le logiciel auteur (rattachement aux étages, réservations, "
                      "identifiants), puis réexporter l'IFC."),
    "G": ("Modeleur / BET", "Reprendre la modélisation des objets signalés (locaux, murs, escaliers, garde-corps) ; "
                            "faire confirmer par le BET les cas relevant de la conception."),
    "A": ("Architecte / AMO accessibilité", "Vérifier les cotes et aménagements signalés au regard du référentiel PMR "
                                             "retenu ; arbitrer avec le maître d'ouvrage."),
    "F": ("BET / coordonnateur SSI", "Analyser les distances et dégagements signalés avec le bureau de contrôle ; "
                                    "désigner les sorties, escaliers protégés et EAS manquants."),
    "clash": ("Coordinateur BIM / synthèse", "Traiter les conflits en réunion de synthèse, du plus grave au moins "
                                             "grave ; arbitrer et corriger dans les maquettes métier."),
    "DEG": ("Architecte", "Libérer ou justifier les zones de dégagement encombrées."),
    "": ("Modeleur", "Compléter ou corriger les données signalées dans le logiciel auteur."),
}


@dataclass
class AuditSection:
    id: str
    label: str
    check_id: str = ""
    rules: list[dict] = field(default_factory=list)  # per_requirement
    issues: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    hypotheses: list[dict] = field(default_factory=list)
    duration_s: float | None = None
    references: list[str] = field(default_factory=list)  # référentiels propres au contrôle (CCBIM du projet)


# ── Polices ─────────────────────────────────────────────────────────────

_FONTS: tuple[str, str] | None = None


def _fonts() -> tuple[str, str]:
    """Police de la charte : Roboto, sinon Arial (repli de la charte), sinon DejaVu Sans (Unicode complet)."""
    global _FONTS
    if _FONTS:
        return _FONTS
    import os

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    here = Path(__file__).parent / "assets" / "fonts"
    env = Path(os.getenv("MCP_REPORT_FONT_DIR", "")) if os.getenv("MCP_REPORT_FONT_DIR") else None
    cands = []
    for d in [env, here]:
        if d:
            cands.append((d / "Roboto-Regular.ttf", d / "Roboto-Bold.ttf"))
    cands += [(Path("/System/Library/Fonts/Supplemental/Arial.ttf"), Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")),
              (Path("/usr/share/fonts/truetype/msttcorefonts/Arial.ttf"),
               Path("/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf"))]
    try:
        import matplotlib

        mp = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        cands.append((mp / "DejaVuSans.ttf", mp / "DejaVuSans-Bold.ttf"))
    except Exception:  # noqa: BLE001
        pass
    for reg, bold in cands:
        if reg.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("Charte", str(reg)))
            pdfmetrics.registerFont(TTFont("Charte-Bold", str(bold)))
            from reportlab.pdfbase.pdfmetrics import registerFontFamily

            registerFontFamily("Charte", normal="Charte", bold="Charte-Bold", italic="Charte", boldItalic="Charte-Bold")
            _FONTS = ("Charte", "Charte-Bold")
            return _FONTS
    _FONTS = ("Helvetica", "Helvetica-Bold")
    return _FONTS


def _styles():
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle

    reg, bold = _fonts()
    base = ParagraphStyle("base", fontName=reg, fontSize=9.5, leading=13, textColor=HexColor(PRIMARY))
    return {
        "body": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10.5, textColor=HexColor(GRANITE)),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8, leading=10),
        "cellb": ParagraphStyle("cellb", parent=base, fontName=bold, fontSize=8, leading=10),
        "head": ParagraphStyle("head", parent=base, fontName=bold, fontSize=7.5, leading=9.5, textColor=HexColor(GRANITE)),
        "h1": ParagraphStyle("h1", parent=base, fontName=bold, fontSize=14, leading=18, spaceBefore=4, spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=base, fontName=bold, fontSize=11, leading=14, spaceBefore=10, spaceAfter=3),
        "h3": ParagraphStyle("h3", parent=base, fontName=bold, fontSize=9.5, leading=12, spaceBefore=8, spaceAfter=2,
                             textColor=HexColor(GRANITE)),
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=12, bulletIndent=2),
        "box": ParagraphStyle("box", parent=base, fontSize=8.5, leading=11.5),
    }


def _esc(x) -> str:
    from xml.sax.saxutils import escape

    return escape(str(x if x is not None else "—"))


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "oui" if v else "non"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".").replace(".", ",")
    return "—" if v is None else str(v)


# ── Gabarit de page ─────────────────────────────────────────────────────


def _numbered_canvas(title: str):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    reg, bold = _fonts()

    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._saved = []

        def showPage(self):  # noqa: N802 — API reportlab
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            n = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                if self._pageNumber > 1:
                    self._chrome(n)
                super().showPage()
            super().save()

        def _chrome(self, total: int):
            w, h = self._pagesize
            self.setStrokeColor(HexColor(RULE))
            self.setLineWidth(0.4)
            self.line(18 * mm, h - 16 * mm, w - 18 * mm, h - 16 * mm)
            if LOGO_SMALL.exists():
                self.drawImage(str(LOGO_SMALL), w - 18 * mm - 22 * mm, h - 14.5 * mm, width=22 * mm, height=6 * mm,
                               preserveAspectRatio=True, mask="auto", anchor="e")
            self.setFont(reg, 7.5)
            self.setFillColor(HexColor(GRANITE))
            self.drawString(18 * mm, h - 13 * mm, title[:110])
            self.setStrokeColor(HexColor(RULE))
            self.setLineWidth(0.4)
            self.line(18 * mm, 14 * mm, w - 18 * mm, 14 * mm)
            self.setFont(reg, 6.5)
            self.drawString(18 * mm, 9.5 * mm, AP.DISCLAIMER_SHORT)
            self.setFont(reg, 7)
            self.drawRightString(w - 18 * mm, 9.5 * mm, f"BIMData.io — page {self._pageNumber} / {total}")

    return NumberedCanvas


def _cover(canv, doc, ctx: Contexte, title: str, subtitle: str):
    """Page de garde sobre : fond blanc, logo, titre, court filet jaune, fiche d'identification à filets fins."""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm

    reg, bold = _fonts()
    w, h = doc.pagesize
    x = 22 * mm
    canv.saveState()
    if LOGO_PATH.exists():
        canv.drawImage(str(LOGO_PATH), x, h - 34 * mm, width=40 * mm, height=12 * mm, preserveAspectRatio=True,
                       mask="auto", anchor="w")
    y = h * 0.64
    canv.setFillColor(HexColor(GRANITE))
    canv.setFont(reg, 8.5)
    canv.drawString(x, y + 14 * mm, ctx.type_livrable.upper())
    canv.setFillColor(HexColor(PRIMARY))
    canv.setFont(bold, 22)
    for line in _wrap(title, 42)[:3]:
        canv.drawString(x, y, line)
        y -= 9 * mm
    canv.setStrokeColor(HexColor(SECONDARY))
    canv.setLineWidth(1.6)
    canv.line(x, y + 4 * mm, x + 16 * mm, y + 4 * mm)
    canv.setFillColor(HexColor(GRANITE))
    canv.setFont(reg, 11)
    canv.drawString(x, y - 4 * mm, subtitle[:95])
    y = h * 0.40
    canv.setStrokeColor(HexColor(RULE))
    canv.setLineWidth(0.4)
    for k, v in ctx.infos():
        canv.line(x, y + 4.2 * mm, w - x, y + 4.2 * mm)
        canv.setFillColor(HexColor(GRANITE))
        canv.setFont(reg, 8)
        canv.drawString(x, y, k)
        canv.setFillColor(HexColor(PRIMARY))
        canv.setFont(reg, 9)
        canv.drawString(x + 44 * mm, y, str(v)[:85])
        y -= 7 * mm
    canv.line(x, y + 4.2 * mm, w - x, y + 4.2 * mm)
    from reportlab.platypus import Paragraph

    para = Paragraph("<b>Avertissement.</b> " + _esc(AP.DISCLAIMER), _styles()["small"])
    pw, ph = para.wrap(w - 2 * x, 80 * mm)
    canv.line(x, 22 * mm + ph, w - x, 22 * mm + ph)
    para.drawOn(canv, x, 18 * mm)
    canv.restoreState()


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width) or [""]


# ── Briques ─────────────────────────────────────────────────────────────


def _table(rows: list[list], widths: list[float], *, header: bool = True, sev_col: int | None = None,
           highlight: list[int] | None = None):
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import LongTable, Paragraph, TableStyle

    st = _styles()
    data = []
    for r, row in enumerate(rows):
        style = st["head"] if header and r == 0 else st["cell"]
        data.append([c if not isinstance(c, (str, int, float)) and c is not None else Paragraph(_esc(c), style)
                     for c in row])
    t = LongTable(data, colWidths=widths, repeatRows=1 if header else 0)
    cmds = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.3, HexColor(RULE)),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    if header:
        cmds += [("BACKGROUND", (0, 0), (-1, 0), HexColor(HEAD_BG)),
                 ("LINEBELOW", (0, 0), (-1, 0), 0.6, HexColor(SILVER_DARK))]
    for r in highlight or []:
        cmds.append(("BACKGROUND", (0, r), (-1, r), HexColor(HIGHLIGHT)))
    if sev_col is not None:
        for r, row in enumerate(rows[1:] if header else rows, start=1 if header else 0):
            sev = str(row[sev_col]).lower()
            key = next((k for k, v in SEV_LABEL.items() if v.lower() == sev or k == sev), None)
            if key:
                cmds.append(("TEXTCOLOR", (sev_col, r), (sev_col, r), HexColor(SEVERITY_COLORS[key])))
    t.setStyle(TableStyle(cmds))
    return t


def _box(text: str, color: str = "#F7F8FA", border: str = SILVER_DARK):
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Paragraph, Table, TableStyle

    t = Table([[Paragraph(text, _styles()["box"])]], colWidths=[174 * 2.8346])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), HexColor(color)),
                           ("LINEBEFORE", (0, 0), (0, -1), 1, HexColor(border)),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


VERDICT_COLORS = {"fiable": SUCCESS, "à confirmer": "#E08A00", "peu fiable": HIGH, "non fiable": HIGH}


def _insight_box(W: float, expert: dict | None, auto: dict | None):
    """Encart « L'essentiel » en tête du rapport : l'analyse d'expert (fiabilité par règle, à retenir, prérequis,
    recommandation), sinon les indicateurs automatiques de fiabilité."""
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    st = _styles()
    if not expert and not (auto and (auto.get("regles") or auto.get("donnees_manquantes"))):
        return None
    body = [Paragraph("<b>L'essentiel — lecture critique de l'audit</b>", st["h2"])]

    def verdict_rows(rows):
        for r in rows:
            v = str(r.get("verdict") or "").lower()
            col = VERDICT_COLORS.get(v, PRIMARY)
            why = r.get("raisons") or []
            why = why if isinstance(why, list) else [why]
            head = (f"<b>{_esc(r.get('regle'))}</b>" + (f" {_esc(r.get('libelle'))}" if r.get("libelle") else "")
                    + (f" — {r['anomalies']} anomalie(s)" if r.get("anomalies") else "")
                    + f" : <font color='{col}'><b>{_esc(v or '—')}</b></font>")
            body.append(Paragraph(head, st["bullet"], bulletText="•"))
            for w in why:
                body.append(Paragraph(_esc(w), _indent_style(st)))

    if expert:
        if expert.get("a_retenir"):
            body += [Paragraph("<b>Ce qu'il faut retenir.</b> " + _esc(expert["a_retenir"]), st["body"]), Spacer(1, 4)]
        if expert.get("fiabilite"):
            body.append(Paragraph("<b>Fiabilité des résultats</b>", st["body"]))
            verdict_rows(expert["fiabilite"])
        if expert.get("prerequis"):
            body.append(Paragraph("<b>Avant un contrôle définitif</b>", st["body"]))
            for x in expert["prerequis"]:
                x = x if isinstance(x, dict) else {"action": x}
                body.append(Paragraph(_esc(x.get("action")) + (f" <i>({_esc(x['responsable'])})</i>"
                                                               if x.get("responsable") else ""),
                                      st["bullet"], bulletText="•"))
        if expert.get("recommandation"):
            body += [Spacer(1, 3), Paragraph("<b>Recommandation.</b> " + _esc(expert["recommandation"]), st["body"])]
        body.append(Paragraph("Analyse de l'auditeur, rédigée avec assistance d'IA à partir des résultats du contrôle.",
                              st["small"]))
    else:
        body.append(Paragraph("Indicateurs automatiques de fiabilité : ils signalent les règles dont les résultats "
                              "demandent une vérification avant toute action.", st["small"]))
        rows = [r for r in auto.get("regles") or [] if r["verdict"] != "fiable"] or (auto.get("regles") or [])[:3]
        verdict_rows(rows)
        if auto.get("donnees_manquantes"):
            body.append(Paragraph("<b>Données d'entrée manquantes</b>", st["body"]))
            for n in auto["donnees_manquantes"]:
                body.append(Paragraph(_esc(n[:300]), st["bullet"], bulletText="•"))
        if auto.get("recommandation_bcf"):
            body += [Spacer(1, 3), Paragraph("<b>Recommandation.</b> " + _esc(auto["recommandation_bcf"]), st["body"])]
    t = Table([[body]], colWidths=[W])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), HexColor("#F7F8FA")),
                           ("LINEBEFORE", (0, 0), (0, -1), 2, HexColor(SECONDARY)),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return t


def _indent_style(st):
    from reportlab.lib.styles import ParagraphStyle

    return ParagraphStyle("ind", parent=st["small"], leftIndent=22)


def _h1(n: str, text: str):
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import HRFlowable, Paragraph

    return [Paragraph(f"{n}  {_esc(text)}" if n else _esc(text), _styles()["h1"]),
            HRFlowable(width="6%", thickness=1, color=HexColor(SECONDARY), spaceBefore=2, spaceAfter=10, hAlign="LEFT")]


def _rule_family(rule: str) -> str:
    r = (rule or "").upper()
    if r == "CLASH":
        return "clash"
    if r == "DEG":
        return "DEG"
    return r[:1] if r[:1] in ("S", "G", "A", "F") and r[1:2].isdigit() else ""


def _severity_rows(audits: list[AuditSection]) -> list[list]:
    rows = [["Audit", "Règles contrôlées", "Anomalies", "Bloquant", "Majeur", "Mineur", "Info"]]
    for a in audits:
        c = Counter(i.get("severity") for i in a.issues)
        ctrl = sum(1 for r in a.rules if (r.get("applicable") or 0) > 0 or (r.get("non_conformes") or 0) > 0)
        rows.append([a.label, f"{ctrl} / {len(a.rules)}", len(a.issues), c.get("bloquant", 0), c.get("majeur", 0),
                     c.get("mineur", 0), c.get("info", 0)])
    return rows


def _chart(audits: list[AuditSection]):
    """Histogramme horizontal empilé des anomalies par sévérité et par audit."""
    from reportlab.graphics.charts.barcharts import HorizontalBarChart
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib.colors import HexColor

    reg, _ = _fonts()
    sev = ["bloquant", "majeur", "mineur", "info"]
    data = [[Counter(i.get("severity") for i in a.issues).get(s, 0) for a in audits] for s in sev]
    if not audits or not any(any(d) for d in data):
        return None
    h = 24 + 18 * len(audits)
    d = Drawing(480, h + 24)
    ch = HorizontalBarChart()
    ch.x, ch.y, ch.width, ch.height = 150, 18, 300, h
    ch.data = data
    ch.categoryAxis.categoryNames = [a.label[:32] for a in audits]
    ch.categoryAxis.labels.fontName = reg
    ch.categoryAxis.labels.fontSize = 7.5
    ch.valueAxis.labels.fontName = reg
    ch.valueAxis.labels.fontSize = 7
    ch.valueAxis.valueMin = 0
    ch.categoryAxis.style = "stacked"
    ch.barWidth = 7
    ch.valueAxis.strokeColor = HexColor(RULE)
    ch.categoryAxis.strokeColor = HexColor(RULE)
    ch.valueAxis.visibleGrid = False
    for k, s in enumerate(sev):
        ch.bars[k].fillColor = HexColor(SEVERITY_COLORS[s])
        ch.bars[k].strokeColor = None
    d.add(ch)
    x = 150
    for s in sev:
        d.add(String(x, h + 28, "■", fillColor=HexColor(SEVERITY_COLORS[s]), fontName=reg, fontSize=9))
        d.add(String(x + 9, h + 28, SEV_LABEL[s], fontName=reg, fontSize=7.5, fillColor=HexColor(GRANITE)))
        x += 70
    return d


# ── Rapport ─────────────────────────────────────────────────────────────


def write_audit_pdf(path: Path, ctx: Contexte, *, title: str, audits: list[AuditSection], skipped: list[dict],
                    model_info: dict | None = None, excel_name: str | None = None, expert: dict | None = None,
                    insight: dict | None = None) -> Path:
    """Écrit le rapport PDF d'un ou plusieurs audits."""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        NextPageTemplate,
        PageBreak,
        PageTemplate,
        Paragraph,
        Spacer,
    )

    st = _styles()
    W = A4[0] - 36 * mm
    model_info = model_info or {}
    doc = BaseDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=22 * mm,
                          bottomMargin=20 * mm, title=title, author="BIMData MCP", subject=ctx.type_livrable)
    frame = Frame(18 * mm, 20 * mm, W, A4[1] - 42 * mm, id="f")
    subtitle = f"{ctx.projet or 'Projet'} — {ctx.maquette or 'Maquette'}"
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[frame], onPage=lambda c, d: _cover(c, d, ctx, title, subtitle)),
        PageTemplate(id="body", frames=[frame]),
    ])
    S: list = [NextPageTemplate("body"), PageBreak()]
    all_issues = [dict(i, _audit=a.label) for a in audits for i in a.issues]
    sev = Counter(i.get("severity") for i in all_issues)

    # L'essentiel : lecture critique en tête (l'intelligence de l'audit avant les chiffres)
    box = _insight_box(W, expert, insight)
    if box is not None:
        S += [box, Spacer(1, 12)]
    # 1. Synthèse
    S += _h1("1.", "Synthèse")
    if not audits:
        verdict = "Aucun audit n'a pu être lancé : voir les décisions attendues ci-dessous."
    elif sev.get("bloquant"):
        verdict = (f"La maquette présente <b>{sev['bloquant']} anomalie(s) bloquante(s)</b> qui doivent être levées "
                   "avant tout usage de la maquette comme référence (synthèse, quantitatifs, DOE).")
    elif sev.get("majeur"):
        verdict = (f"Aucune anomalie bloquante ; <b>{sev['majeur']} anomalie(s) majeure(s)</b> appellent une "
                   "correction ou une justification avant la prochaine diffusion.")
    else:
        verdict = "Aucune anomalie bloquante ni majeure sur les règles contrôlées ; les points mineurs restent à traiter."
    S.append(Paragraph(f"<b>Avis global.</b> {verdict} Les résultats portent sur {len(audits)} audit(s) ; "
                       f"{len(all_issues)} anomalie(s) ont été relevées au total.", st["body"]))
    S.append(Spacer(1, 6))
    S.append(_table(_severity_rows(audits), [W * 0.3, W * 0.15, W * 0.12, W * 0.11, W * 0.1, W * 0.1, W * 0.12]))
    chart = _chart(audits)
    if chart:
        S += [Spacer(1, 18), chart]
    top = sorted([i for i in all_issues if i.get("severity") in ("bloquant", "majeur")],
                 key=lambda i: (SEV_ORDER.get(i.get("severity"), 9), i.get("_audit")))[:8]
    if top:
        S.append(Paragraph("Points majeurs", st["h2"]))
        for i in top:
            S.append(Paragraph(f"<font color='{SEVERITY_COLORS.get(i['severity'], PRIMARY)}'><b>"
                               f"{SEV_LABEL.get(i['severity'], i['severity'])}</b></font> — {_esc(i.get('_audit'))}, "
                               f"règle {_esc(i.get('rule_id') or i.get('req_id'))} : {_esc((i.get('message') or '')[:220])}",
                               st["bullet"], bulletText="•"))
    asks = [(a.label, h) for a in audits for h in a.hypotheses if h.get("a_fournir")] + \
           [(s.get("audit"), {"parametre": q.get("option"), "source": q.get("question")})
            for s in skipped for q in s.get("a_demander", [])]
    not_run = [s for s in skipped if not s.get("a_demander")]
    if asks or not_run:
        S.append(Paragraph("Décisions et données attendues", st["h2"]))
        for lbl, h in asks:
            S.append(Paragraph(f"{_esc(lbl)} : fournir « {_esc(h.get('parametre'))} »"
                               + (f" — {_esc(h.get('source'))}" if h.get("source") else ""), st["bullet"], bulletText="•"))
        for s in not_run:
            S.append(Paragraph(f"{_esc(s.get('audit'))} non lancé : {_esc(s.get('raison'))}", st["bullet"],
                               bulletText="•"))

    # 2. Objet, périmètre
    S += [PageBreak()] + _h1("2.", "Objet, périmètre et documents d'entrée")
    S.append(Paragraph("Le présent rapport rend compte d'une revue automatisée de la maquette numérique, réalisée "
                       "pour le compte du maître d'ouvrage ou de son assistant, dans le cadre du suivi BIM du projet. "
                       "Il vise à objectiver la qualité de la maquette (structuration, données, géométrie) et à "
                       "repérer les points de conception à faire vérifier (accessibilité, dégagements, évacuation).",
                       st["body"]))
    rows = [["Élément", "Valeur"]] + [[k, v] for k, v in ctx.infos()]
    rows += [["Fichier IFC analysé", model_info.get("fichier") or "—"], ["Date du fichier IFC", model_info.get("date") or "—"],
             ["Schéma IFC", model_info.get("schema") or "—"],
             ["Audits réalisés", ", ".join(a.label for a in audits) or "—"],
             ["Annexe tableur", excel_name or "—"]]
    S += [Spacer(1, 6), _table(rows, [W * 0.3, W * 0.7])]

    # 3. Hypothèses
    S += [PageBreak()] + _h1("3.", "Hypothèses et paramètres de calcul")
    S.append(Paragraph("Les valeurs ci-dessous ont été appliquées. Elles sont <b>indicatives</b> et paramétrables ; "
                       "les lignes surlignées ont été modifiées par rapport aux valeurs par défaut. Les paramètres « à "
                       "fournir » conditionnent le lancement de certaines règles.", st["body"]))
    for a in audits:
        if not a.hypotheses:
            continue
        rows = [["Paramètre", "Valeur retenue", "Défaut", "Plage", "Source / justification"]]
        hl = []
        for k, h in enumerate(a.hypotheses, start=1):
            val = "À fournir" if h.get("a_fournir") else f"{_fmt(h['valeur'])} {h.get('unite') or ''}".strip()
            rows.append([h["parametre"], val, f"{_fmt(h['defaut'])} {h.get('unite') or ''}".strip(), h.get("plage") or "—",
                         h.get("source") or "—"])
            if h.get("modifie") or h.get("a_fournir"):
                hl.append(k)
        S += [Paragraph(_esc(a.label), st["h3"]),
              _table(rows, [W * 0.32, W * 0.13, W * 0.1, W * 0.13, W * 0.32], highlight=hl)]

    # 4. Référentiels
    S += [Spacer(1, 6)] + _h1("4.", "Référentiels et textes applicables")
    S.append(Paragraph("Les textes ci-dessous ont servi à fixer les seuils indicatifs. Leur application effective au "
                       "projet (classement, catégorie, dérogations) relève du maître d'ouvrage, de la maîtrise d'œuvre "
                       "et du bureau de contrôle.", st["body"]))
    for a in audits:
        refs = a.references or AP.REFERENCES.get(a.id) or []
        if refs:
            S.append(Paragraph(_esc(a.label), st["h3"]))
            for r in refs:
                S.append(Paragraph(_esc(r), st["bullet"], bulletText="•"))

    # 5. Méthode
    S += [Spacer(1, 6)] + _h1("5.", "Méthode")
    S.append(Paragraph("Chaque contrôle est une règle identifiée (code stable), appliquée à la maquette selon l'une "
                       "des méthodes suivantes : <b>Données</b> (propriétés et relations IFC), <b>Bbox</b> (boîtes "
                       "englobantes), <b>Maillage</b> (géométrie triangulée). Une anomalie est une non-conformité "
                       "à une règle, avec sa sévérité (bloquant, majeur, mineur, information).", st["body"]))
    for a in audits:
        if AP.METHODS.get(a.id):
            S += [Paragraph(_esc(a.label), st["h3"]), Paragraph(_esc(AP.METHODS[a.id]), st["body"])]

    # 6. Résultats détaillés
    S += [PageBreak()] + _h1("6.", "Résultats détaillés")
    for n, a in enumerate(audits, start=1):
        ctrl = sum(1 for r in a.rules if (r.get("applicable") or 0) > 0 or (r.get("non_conformes") or 0) > 0)
        c = Counter(i.get("severity") for i in a.issues)
        S.append(Paragraph(f"6.{n}  {_esc(a.label)}", st["h2"]))
        S.append(Paragraph(f"{ctrl} règle(s) contrôlée(s) sur {len(a.rules)} ; {len(a.issues)} anomalie(s) dont "
                           f"{c.get('bloquant', 0)} bloquante(s) et {c.get('majeur', 0)} majeure(s)."
                           + (f" Contrôle {a.check_id}." if a.check_id else ""), st["body"]))
        if a.rules:
            rows = [["Règle", "Libellé", "Contrôlés", "Anomalies", "Taux", "Statut"]]
            for r in a.rules:
                appl, bad = r.get("applicable") or 0, r.get("non_conformes") or 0
                taux = r.get("taux")
                status = ("Non contrôlée" if not appl and not bad else "Conforme" if not bad else "À corriger")
                rows.append([r.get("req_id"), r.get("label"), appl, bad,
                             "—" if taux is None else f"{100 * taux:.0f} %", status])
            t = _table(rows, [W * 0.07, W * 0.41, W * 0.12, W * 0.12, W * 0.09, W * 0.19])
            from reportlab.platypus import TableStyle

            colors = {"Conforme": SUCCESS, "À corriger": HIGH, "Non contrôlée": GRANITE_LIGHT}
            t.setStyle(TableStyle([("TEXTCOLOR", (5, k), (5, k), HexColor(colors[row[5]]))
                                   for k, row in enumerate(rows[1:], start=1)]))
            S += [Spacer(1, 4), t]
        top = sorted(a.issues, key=lambda i: (SEV_ORDER.get(i.get("severity"), 9), i.get("rule_id") or ""))[:15]
        if top:
            rows = [["Sévérité", "Règle", "Élément", "Niveau", "Constat"]]
            for i in top:
                rows.append([SEV_LABEL.get(i.get("severity"), i.get("severity")), i.get("rule_id") or i.get("req_id"),
                             f"{i.get('ifc_class') or ''} « {(i.get('name') or '')[:40]} »", i.get("storey") or "—",
                             (i.get("message") or "")[:260]])
            S += [Paragraph("Principales anomalies", st["h3"]),
                  _table(rows, [W * 0.1, W * 0.07, W * 0.25, W * 0.12, W * 0.46], sev_col=0)]
            if len(a.issues) > len(top):
                S.append(Paragraph(f"… {len(a.issues) - len(top)} autre(s) en annexe.", st["small"]))
        if a.notes:
            S.append(Paragraph("Points d'attention", st["h3"]))
            for note in a.notes[:12]:
                S.append(Paragraph(_esc(note[:400]), st["bullet"], bulletText="•"))

    # 7. Recommandations
    S += [PageBreak()] + _h1("7.", "Recommandations et plan d'actions")
    S.append(Paragraph("Les actions sont classées par priorité : d'abord les règles comportant des anomalies "
                       "bloquantes, puis majeures, puis mineures. La responsabilité proposée est indicative.", st["body"]))
    acts = Counter()
    worst: dict[str, str] = {}
    for i in all_issues:
        rule = i.get("rule_id") or i.get("req_id") or ""
        key = (i.get("_audit"), rule, i.get("req_label") or "")
        acts[key] += 1
        s = i.get("severity")
        if SEV_ORDER.get(s, 9) < SEV_ORDER.get(worst.get(key, "info"), 9):
            worst[key] = s
        worst.setdefault(key, s)
    rows = [["Priorité", "Audit / règle", "Anomalies", "Action proposée", "Responsable"]]
    for (aud, rule, lbl), cnt in sorted(acts.items(), key=lambda kv: (SEV_ORDER.get(worst[kv[0]], 9), -kv[1]))[:40]:
        who, what = ACTIONS.get(_rule_family(rule), ACTIONS[""])
        rows.append([SEV_LABEL.get(worst[(aud, rule, lbl)], "—"), f"{aud} — {rule} {lbl}"[:90], cnt, what, who])
    if len(rows) > 1:
        S += [Spacer(1, 4), _table(rows, [W * 0.1, W * 0.28, W * 0.09, W * 0.38, W * 0.15], sev_col=0)]
    S.append(Spacer(1, 6))
    S.append(Paragraph("Une fois les corrections faites dans les logiciels auteurs, réexporter l'IFC, le recharger "
                       "dans BIMData et relancer les audits : les anomalies corrigées disparaissent et les topics BCF "
                       "correspondants peuvent être clôturés.", st["body"]))

    # 8. Limites
    S += _h1("8.", "Limites, réserves et avertissement")
    lim = ["Les contrôles portent sur la maquette dans l'état et à la date indiqués au chapitre 2 ; ils ne couvrent "
           "pas les éléments non modélisés (mobilier, réservations non dessinées, équipements absents).",
           "Les distances et dégagements sont mesurés sur une géométrie simplifiée (grille de cheminement, boîtes "
           "englobantes, maillages triangulés) ; les écarts de quelques centimètres sont dans la précision de la "
           "méthode.",
           "Les seuils sont ceux du chapitre 3 ; ils ne préjugent pas du classement ni de la catégorie de l'ouvrage.",
           "Les règles non contrôlées (données absentes, paramètre non fourni) ne valent pas conformité."]
    for x in lim:
        S.append(Paragraph(_esc(x), st["bullet"], bulletText="•"))
    S += [Spacer(1, 8), _box("<b>Avertissement.</b> " + _esc(AP.DISCLAIMER))]

    # Annexe
    if all_issues:
        S += [PageBreak()] + _h1("", "Annexe — Liste des anomalies (extrait)")
        S.append(Paragraph(f"Extrait des {min(len(all_issues), 300)} premières anomalies par sévérité"
                           + (f" ; liste complète dans « {_esc(excel_name)} »." if excel_name else "."), st["small"]))
        rows = [["Sév.", "Audit", "Règle", "Élément", "Niveau", "Constat"]]
        for i in sorted(all_issues, key=lambda i: SEV_ORDER.get(i.get("severity"), 9))[:300]:
            rows.append([SEV_LABEL.get(i.get("severity"), "")[:5], (i.get("_audit") or "")[:22],
                         i.get("rule_id") or i.get("req_id"), f"« {(i.get('name') or i.get('uuid') or '')[:34]} »",
                         i.get("storey") or "—", (i.get("message") or "")[:200]])
        S.append(_table(rows, [W * 0.07, W * 0.15, W * 0.07, W * 0.22, W * 0.11, W * 0.38], sev_col=0))
    S = [x for x in S if x is not None]
    doc.build(S, canvasmaker=_numbered_canvas(f"{title} — {ctx.projet or ''} / {ctx.maquette or ''}"))
    return path


def pdf_name(xlsx_name: str) -> str:
    return xlsx_name[:-5] + ".pdf" if xlsx_name.endswith(".xlsx") else xlsx_name + ".pdf"
