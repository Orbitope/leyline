import requests
from sqlalchemy.orm import DeclarativeBase

PROFILE_URL = "https://example.com/profile"


class Model(DeclarativeBase):
    pass


class Invoice(Model):
    pass


def bill(session):
    invoice = Invoice.model_validate({})
    session.add(invoice)


def invoices(session):
    return session.query(Invoice).all()


def profile():
    return requests.get(PROFILE_URL)
