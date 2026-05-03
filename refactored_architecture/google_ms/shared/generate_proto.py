#!/usr/bin/env python3
"""
Run this script once to generate the gRPC Python stubs from demo.proto.
The generated files (demo_pb2.py and demo_pb2_grpc.py) will be placed in
this shared/ directory and should be copied or symlinked into each service.
"""
import subprocess
import sys
import os

def generate():
    proto_dir = os.path.join(os.path.dirname(__file__), "..", "protos")
    out_dir = os.path.dirname(__file__)

    print("Installing grpcio-tools ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "grpcio-tools", "-q"])

    print("Generating protobuf stubs ...")
    subprocess.check_call([
        sys.executable, "-m", "grpc_tools.protoc",
        f"--proto_path={proto_dir}",
        f"--python_out={out_dir}",
        f"--grpc_python_out={out_dir}",
        "demo.proto",
    ])
    print("Done! Files written to:", out_dir)

if __name__ == "__main__":
    generate()