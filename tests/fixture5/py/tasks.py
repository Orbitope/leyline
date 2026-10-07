from celery import Celery

app = Celery("shop")


@app.task
def send_receipt(order_id):
    return order_id
