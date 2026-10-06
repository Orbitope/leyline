import json


def load(path):
    with open(path) as f:
        return json.load(f)


class Report:
    def __init__(self, rows):
        self.rows = rows

    def total(self):
        return sum(self.rows)
