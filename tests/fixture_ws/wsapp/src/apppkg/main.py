from libpkg import helper
from libpkg.core import Base, cached


class App(Base):
    @property
    def limit(self):
        return configured_limit()

    def hook(self, x):
        return helper(x)

    @cached
    def name(self):
        return "app"


def configured_limit():
    return 5


def run():
    app = App()
    return app.handle(1) + len(app.name)
