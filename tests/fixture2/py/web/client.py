def fetch(session):
    return session.get("/items/3")


def other(session):
    return session.get("/nothing/here")
