import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _sample(name):
    """Sample POs are real business documents and may be kept out of the repo."""
    from pdf_source import parse_po_pdf

    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"sample PO {name} not present in tests/fixtures")
    return parse_po_pdf(path.read_bytes(), name)


@pytest.fixture(scope="session")
def po_107439():
    return _sample("PO_586_107439_0_US.pdf")


@pytest.fixture(scope="session")
def po_36690():
    return _sample("PO_306_36690_0_US-1.pdf")


@pytest.fixture
def cfg():
    from settings import load_defaults

    return load_defaults()
