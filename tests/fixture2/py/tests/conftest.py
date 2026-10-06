import pytest

import pkg


@pytest.fixture
def engine():
    engine = pkg.Engine("fixture")
    return engine


@pytest.fixture
def child(engine):
    return engine.child()
