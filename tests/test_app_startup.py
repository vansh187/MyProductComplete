import time

import pytest
from fastapi.testclient import TestClient

import app as app_module


class BlockingShoonya:
    def __init__(self):
        self.is_connected = False

    def connect(self):
        time.sleep(1.5)
        return False

    def auto_login(self):
        return False


def test_app_starts_without_waiting_for_broker_bootstrap(monkeypatch):
    monkeypatch.setattr(app_module, "_SHOONYA_IMPORTABLE", True)
    monkeypatch.setattr(app_module, "_BREEZE_IMPORTABLE", False)
    monkeypatch.setattr(app_module, "ShoonyaConnection", lambda: BlockingShoonya())

    started = time.monotonic()
    with TestClient(app_module.app) as client:
        response = client.get("/")
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 1.0
