from models import Invoice


def test_bill(session):
    session.add(Invoice())
    assert session.query(Invoice).count() == 1
