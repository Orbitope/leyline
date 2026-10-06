class Base:
    def stop(self):
        pass


class Engine(Base):
    def __init__(self, name):
        self.name = name

    def start(self):
        return self.name

    def stop(self):
        super().stop()

    def child(self) -> "Engine":
        return Engine(self.name)


def make_engine():
    return Engine("made")


def poke(thing):
    handler = thing.child
    return thing.child()


class Journal:
    def __init__(self):
        self.items = []
        self.by = {}

    def note(self, x):
        self.items.append(x)
        self.by[x].add(x)

    def count(self):
        return len(self.items) + len(self.by)
