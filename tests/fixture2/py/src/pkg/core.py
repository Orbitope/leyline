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
