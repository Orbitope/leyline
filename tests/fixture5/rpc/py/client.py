import grpc

import greeter_pb2_grpc


def hello():
    stub = greeter_pb2_grpc.GreeterStub(grpc.insecure_channel("localhost:50051"))
    return stub.SayHello("me")
