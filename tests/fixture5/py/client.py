import grpc

import greeter_pb2_grpc


def greet(name):
    with grpc.insecure_channel("localhost:5001") as channel:
        stub = greeter_pb2_grpc.GreeterStub(channel)
        return stub.SayHello(name)
