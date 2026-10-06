from pkg import make_engine


def test_start(engine):
    assert engine.start() == "fixture"


def test_child(child):
    def inner():
        return child.start()
    assert inner()


def test_made():
    assert make_engine().name == "made"
