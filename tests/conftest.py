import os

os.environ["CITYPULSE_ROLE"] = "api"  # tests never start the simulator

import pytest
from fastapi.testclient import TestClient

from citypulse import api


@pytest.fixture(scope="session")
def client():
    with TestClient(api.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_cookies(request):
    if "client" in request.fixturenames:
        request.getfixturevalue("client").cookies.clear()
    yield
