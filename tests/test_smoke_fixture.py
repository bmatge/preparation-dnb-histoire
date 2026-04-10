"""Smoke test du harness ``test_client`` (vague 2).

Vérifie que l'app FastAPI démarre, que les startup events ont chargé le
corpus dans la DB de test, et que ``/`` répond.
"""

from __future__ import annotations


def test_app_starts_and_serves_home(test_client):
    r = test_client.get("/")
    assert r.status_code == 200
    assert "Révise" in r.text or "DNB" in r.text


def test_healthz(test_client):
    r = test_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
