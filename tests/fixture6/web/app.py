import typing as t

from .config import Config


class Tag:
    def dump(self, value):
        return str(value)


class Context:
    def __init__(self, app: "App"):
        self.app = app

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def dump(self, value):
        return repr(value)


class App:
    def __init__(self, name):
        self.name = name
        self.config = self.make_config()
        self.tags: list[Tag] = [Tag()]

    def make_config(self) -> Config:
        return Config()

    def route(self, rule):
        def decorator(f):
            self.add_url_rule(rule, f)
            return f

        return decorator

    def add_url_rule(self, rule, f):
        return rule, f

    def context(self) -> Context:
        return Context(self)

    @t.overload
    def get(self, key: int) -> int: ...

    def get(self, key):
        return key

    def render(self, value):
        for tag in self.tags:
            tag.dump(value)
        return self.config.load("render")

    def wsgi(self, environ, start):
        return environ

    def __call__(self, environ, start):
        return self.wsgi(environ, start)


class Middleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start):
        return self.app(environ, start)


def run_app(app, environ):
    return app(environ, None)


def describe(x):
    if isinstance(x, Tag):
        return "tag"
    return x.dump(1)


def wrap(app):
    app = Middleware(app)
    return app


def serve():
    describe(Tag())
    run_app(wrap(App("served")), {})
    return run_app(App("plain"), {})


def current_app_name(ctx: Context):
    app = ctx.app
    return app.wsgi({}, None)


site = App("site")


@site.route("/")
def index():
    return "hello"


def check(obj):
    if isinstance(obj, Context):
        return obj.dump(1)
    return None


def first():
    from . import config

    return config


def second(source):
    config = source
    return config.load("x")


def make_base():
    return object


class Dyn(make_base()):
    pass


def use_dyn(d: Dyn):
    return d.make_config()
