class Config(dict):
    def load(self, name):
        return self.get(name)
