import json
import os


class App:
    def route(self, path):
        return lambda f: f


app = App()


@app.route("/items/<int:item_id>")
def show(item_id):
    return item_id


def save(rows, out_dir):
    with open(os.path.join(out_dir, "reports", "rows.parity.json"), "w") as f:
        json.dump(rows, f)
