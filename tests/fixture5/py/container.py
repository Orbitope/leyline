import punq


class Alerts:
    def notify(self, text):
        raise NotImplementedError


class EmailAlerts:
    def notify(self, text):
        return text


def build():
    container = punq.Container()
    container.register(Alerts, EmailAlerts)
    return container


def alert(alerts: Alerts):
    alerts.notify("disk full")
