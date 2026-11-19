"""Retrait d'une classification : robustesse face aux 500 de BIMData."""

import pytest
import requests

from bimdata_mcp.bimdata import BIMDataClient


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class _Session:
    def __init__(self, delete_status: dict, classifications: list):
        self.delete_status, self.classifications, self.calls = delete_status, classifications, []

    def delete(self, url, timeout=None):
        self.calls.append(("DELETE", url))
        for frag, status in self.delete_status.items():
            if frag in url:
                if status < 400:
                    self.classifications[:] = []
                return _Resp(status)
        return _Resp(404)

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url))
        return _Resp(200, list(self.classifications))


def _client(session):
    c = BIMDataClient(base_url="https://api.test", cloud_id=1, project_id=2, model_id=3, access_token="t")
    c.session = session
    return c


def test_remove_falls_back_to_classification_route_on_500():
    s = _Session({"/element/U1/classification/9": 500, "/classification/9/element/U1": 204}, [{"id": 9}])
    _client(s).remove_element_classification("U1", 9)
    assert [m for m, _ in s.calls] == ["DELETE", "GET", "DELETE"]
    assert s.classifications == []


def test_remove_accepts_500_when_relation_actually_gone():
    s = _Session({"/element/U1/classification/9": 500}, [])
    _client(s).remove_element_classification("U1", 9)
    assert [m for m, _ in s.calls] == ["DELETE", "GET"]


def test_remove_raises_when_both_routes_fail():
    s = _Session({"/element/U1/classification/9": 500, "/classification/9/element/U1": 500}, [{"id": 9}])
    with pytest.raises(requests.HTTPError):
        _client(s).remove_element_classification("U1", 9)
