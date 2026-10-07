import web
from web import current


def test_routes():
    app = web.App("t")

    @app.route("/x")
    def view():
        return "x"

    with app.context():
        app.render(1)


def test_local_subclass():
    class App(web.App):
        def wsgi(self, environ, start):
            return super().wsgi(environ, start)

        def get(self, key):
            return None

    a = App("local")
    a.route("/y")
    return a.wsgi({}, None)


def test_annotated(app: web.App):
    return app.get(1)


def test_module_variable():
    return current.render(2)
