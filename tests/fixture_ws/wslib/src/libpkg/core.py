class cached:
    """A descriptor, like Werkzeug's cached_property: reading the attribute runs the function."""

    def __init__(self, fn):
        self.fn = fn

    def __get__(self, obj, owner=None):
        return self.fn(obj)


class Base:
    limit = 10

    def handle(self, x):
        return min(self.limit, self.hook(x))

    def hook(self, x):
        return x


def helper(x):
    return x * 2
