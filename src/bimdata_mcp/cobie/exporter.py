# Repris de MCP_COBIe (mcp_cobie.cobie.exporter, v0.3.0) sans modification fonctionnelle.
# Vendorisé car mcp-cobie épingle bim-core<0.4, incompatible avec bimdata-read 0.1.10.
"""Export XLSX COBie sécurisé."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO, NamedTuple

import xlsxwriter
from bim_reporting.excel import write_safe

from .schema import COBIE_SHEET_ORDER, SHEET_COLORS, SHEET_HEADERS

MAX_COL_WIDTH = 60

#: Limite dure du format XLSX : 1 048 576 lignes, en-tête comprise.
#: Au-delà, ``xlsxwriter`` cesse d'écrire **sans rien signaler** — le classeur
#: s'ouvre, paraît normal, et il manque des lignes. Sur une maquette réelle la
#: feuille ``Attribute`` a dépassé ce plafond de 170 000 lignes.
MAX_XLSX_ROWS = 1_048_576
MAX_DATA_ROWS = MAX_XLSX_ROWS - 1

__all__ = ["MAX_DATA_ROWS", "SheetOverflow", "export_xlsx", "split_oversized_sheets"]


class SheetOverflow(NamedTuple):
    """Une feuille a dû être tronquée pour tenir dans le format."""

    sheet: str
    produced: int
    written: int

    @property
    def dropped(self) -> int:
        return self.produced - self.written


def _stringify(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return str(value)
    return value


def _write_sheet(
    wb: xlsxwriter.Workbook,
    sheet_name: str,
    headers: list[str],
    rows: list[Mapping[str, Any]],
    color: str,
) -> int:
    ws = wb.add_worksheet(sheet_name[:31] or "Sheet")
    header_fmt = wb.add_format(
        {
            "bold": True,
            "font_color": "#000000",
            "bg_color": color,
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
            "border": 1,
        }
    )
    cell_fmt = wb.add_format({"valign": "top", "border": 1})

    for col_idx, header in enumerate(headers):
        write_safe(ws, 0, col_idx, header, header_fmt)

    col_widths = [len(header) for header in headers]
    written = rows[:MAX_DATA_ROWS]
    for row_idx, row_data in enumerate(written, start=1):
        for col_idx, header in enumerate(headers):
            value = _stringify(row_data.get(header, ""))
            write_safe(ws, row_idx, col_idx, value, cell_fmt)
            col_widths[col_idx] = max(col_widths[col_idx], len(str(value)))

    for col_idx, width in enumerate(col_widths):
        ws.set_column(col_idx, col_idx, min(width + 2, MAX_COL_WIDTH))
    ws.freeze_panes(1, 0)
    return len(written)


def export_xlsx(
    sheets: dict[str, list[dict[str, Any]]], output: str | Path | BinaryIO
) -> list[SheetOverflow]:
    """Écrit un classeur COBie multi-onglets.

    Les feuilles connues suivent l'ordre COBie canonique. Les feuilles
    additionnelles éventuelles sont écrites ensuite, sans être rejetées.

    Returns:
        Les feuilles qui **n'ont pas tenu** dans le format, avec le nombre de
        lignes perdues. La liste est vide dans le cas normal.

        Ce retour existe parce que le silence est le vrai danger : au-delà de
        1 048 576 lignes, ``xlsxwriter`` cesse d'écrire sans rien lever. Le
        classeur s'ouvre, paraît complet, et l'auditeur travaille sur des
        données amputées. Un appelant qui ignore ce retour produit un livrable
        faux ; il vaut mieux qu'il ait à l'ignorer explicitement.
    """
    overflows: list[SheetOverflow] = []
    wb = xlsxwriter.Workbook(output, {"constant_memory": True, "strings_to_formulas": False})
    try:
        seen: set[str] = set()
        for sheet_name in COBIE_SHEET_ORDER:
            rows = sheets.get(sheet_name, [])
            written = _write_sheet(
                wb,
                sheet_name,
                SHEET_HEADERS[sheet_name],
                rows,
                SHEET_COLORS.get(sheet_name, "#FFFFFF"),
            )
            if written < len(rows):
                overflows.append(SheetOverflow(sheet_name, len(rows), written))
            seen.add(sheet_name)
        for sheet_name, rows in sheets.items():
            if sheet_name in seen:
                continue
            headers = list(rows[0].keys()) if rows else []
            written = _write_sheet(
                wb, sheet_name, headers, rows, SHEET_COLORS.get(sheet_name, "#FFFFFF")
            )
            if written < len(rows):
                overflows.append(SheetOverflow(sheet_name, len(rows), written))
    finally:
        wb.close()
    return overflows


def split_oversized_sheets(
    sheets: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    """Scinde les feuilles trop longues en ``Nom``, ``Nom_2``, ``Nom_3``…

    La troncature reste le garde-fou de dernier recours ; ce n'est pas un
    livrable. Une feuille ``Attribute`` de 1,2 million de lignes est un cas
    normal sur une maquette reelle, et perdre 170 000 attributs n'est pas un
    compromis acceptable pour un gestionnaire de patrimoine.

    Le filtrage des Psets **n'est pas fait ici** : decider quels attributs ne
    valent pas d'etre livres est un arbitrage metier, qui depend de la
    convention du client. Le scindage, lui, ne perd rien.

    Returns:
        ``(sheets, split_map)`` — ``split_map`` ne liste que les feuilles
        reellement scindees, avec les noms produits dans l'ordre.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    split_map: dict[str, list[str]] = {}

    for name, rows in sheets.items():
        if len(rows) <= MAX_DATA_ROWS:
            out[name] = rows
            continue
        produced: list[str] = []
        for index, start in enumerate(range(0, len(rows), MAX_DATA_ROWS), start=1):
            part = name if index == 1 else f"{name}_{index}"
            out[part] = rows[start : start + MAX_DATA_ROWS]
            produced.append(part)
        split_map[name] = produced
    return out, split_map
