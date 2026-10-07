import pika

from tasks import send_receipt

QUEUE = "orders.placed"


def on_order(ch, method, props, body):
    send_receipt.delay(body)


def main():
    channel = pika.BlockingConnection().channel()
    channel.basic_consume(queue=QUEUE, on_message_callback=on_order)
    channel.start_consuming()


if __name__ == "__main__":
    main()
