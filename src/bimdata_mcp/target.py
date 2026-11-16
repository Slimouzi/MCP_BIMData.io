"""Ciblage d'une maquette BIMData : URL viewer ou identifiants.

Repris de ``mcp_cobie.bimdata_target`` (MCP_COBIe) et élargi aux variantes
d'URL du viewer (paramètres de requête, sous-domaines, ``/viewer/<id>`` suivi
d'un fragment). Aucune requête réseau ici.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

__all__ = ["BIMDataTarget", "InvalidTargetError", "parse_viewer_url", "resolve_target"]

_SPACE_PROJECT = re.compile(r"/spaces/(?P<cloud_id>\d+)/projects/(?P<project_id>\d+)")
_VIEWER_MODEL = re.compile(r"/viewer/(?P<model_id>\d+)")


class InvalidTargetError(ValueError):
    """Cible inutilisable : URL non reconnue ou identifiant non numérique."""


@dataclass(frozen=True)
class BIMDataTarget:
    cloud_id: str
    project_id: str
    model_id: str

    def to_dict(self) -> dict[str, str]:
        return {"cloud_id": self.cloud_id, "project_id": self.project_id, "model_id": self.model_id}

    @property
    def key(self) -> str:
        return f"{self.cloud_id}/{self.project_id}/{self.model_id}"

    def viewer_url(self, platform_url: str = "https://platform.bimdata.io") -> str:
        return (
            f"{platform_url}/spaces/{self.cloud_id}/projects/{self.project_id}"
            f"/viewer/{self.model_id}"
        )

    @classmethod
    def from_dict(cls, data: dict) -> BIMDataTarget:
        return cls(str(data["cloud_id"]), str(data["project_id"]), str(data["model_id"]))


def parse_viewer_url(url: str) -> BIMDataTarget:
    """``https://platform.bimdata.io/spaces/<cloud>/projects/<project>/viewer/<model>``."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not host.endswith("bimdata.io"):
        raise InvalidTargetError(
            "URL BIMData invalide : attendu 'https://platform.bimdata.io/spaces/<cloud_id>/"
            "projects/<project_id>/viewer/<model_id>'."
        )
    sp = _SPACE_PROJECT.search(parsed.path)
    if not sp:
        raise InvalidTargetError("URL BIMData : /spaces/<id>/projects/<id> introuvable.")
    model_id = None
    vm = _VIEWER_MODEL.search(parsed.path)
    if vm:
        model_id = vm.group("model_id")
    else:
        qs = parse_qs(parsed.query)
        for key in ("model", "modelId", "model_id", "modelIds"):
            if qs.get(key):
                model_id = qs[key][0].split(",")[0]
                break
    if not model_id or not model_id.isdigit():
        raise InvalidTargetError(
            "URL BIMData : identifiant de maquette introuvable (ouvrir la maquette dans le "
            "viewer et copier l'URL)."
        )
    return BIMDataTarget(sp.group("cloud_id"), sp.group("project_id"), model_id)


def _clean_id(label: str, value) -> str:
    if value in (None, ""):
        raise InvalidTargetError(f"{label} est requis (ou fournir `bimdata_url`).")
    text = str(value).strip()
    if text.lower().startswith(("http://", "https://")):
        raise InvalidTargetError(f"{label} est une URL : la passer dans `bimdata_url`.")
    if not text.isdigit():
        raise InvalidTargetError(f"{label} doit être un identifiant numérique (reçu {text!r}).")
    return text


def resolve_target(
    *, bimdata_url: str | None = None, cloud_id=None, project_id=None, model_id=None
) -> BIMDataTarget:
    if bimdata_url:
        if any(v not in (None, "") for v in (cloud_id, project_id, model_id)):
            raise InvalidTargetError(
                "Fournir soit `bimdata_url`, soit cloud_id/project_id/model_id, pas les deux."
            )
        return parse_viewer_url(bimdata_url)
    return BIMDataTarget(
        _clean_id("cloud_id", cloud_id),
        _clean_id("project_id", project_id),
        _clean_id("model_id", model_id),
    )
