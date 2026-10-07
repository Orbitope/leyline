class Base:
    pass


class Order(Base):
    __tablename__ = "orders"


class Session:
    def query(self, model):
        return []


def get_db():
    return Session()
