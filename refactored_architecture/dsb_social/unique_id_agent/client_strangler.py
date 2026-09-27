# client.py
import sys
import random
import os
sys.path.insert(0, "gen_py")

from thrift.transport import TSocket, TTransport
from thrift.protocol  import TBinaryProtocol
from ms_baseline.dsb_social.gen_py.social_network import UniqueIdService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import PostType
# ---------------- STRANGLER CONFIG ----------------
STRANGLER_V2_RATE = int(os.environ.get("STRANGLER_V2_RATE", "90"))

HOST_V1 = os.environ.get("HOST_V1", "127.0.0.1")
PORT_V1 = int(os.environ.get("PORT_V1", 9090))

HOST_V2 = os.environ.get("HOST_V2", "127.0.0.1")
PORT_V2 = int(os.environ.get("PORT_V2", 10090))


def make_client():
    """
    Dynamically routes the Thrift client connection to V1 or V2 
    based on the configured STRANGLER_V2_RATE percentage.
    Returns (client, transport, version_tag).
    """
    if random.randint(1, 100) <= STRANGLER_V2_RATE:
        host, port, version = HOST_V2, PORT_V2, "V2"
    else:
        host, port, version = HOST_V1, PORT_V1, "V1"

    sock      = TSocket.TSocket(host, port)
    transport = TTransport.TFramedTransport(sock)
    protocol  = TBinaryProtocol.TBinaryProtocol(transport)
    client    = UniqueIdService.Client(protocol)
    transport.open()
    return client, transport, version


if __name__ == "__main__":
    client, transport, active_version = make_client()
    try:
        print(f"=== UniqueIdService Client Demo [{active_version}] (Strangler Rate: {STRANGLER_V2_RATE}% V2) ===\n")
        for i in range(3):
            uid = client.ComposeUniqueId(
                req_id=i,
                post_type=PostType.POST,
                carrier={}
            )
            print(f"req_id={i}  ->  uid={uid} [{active_version}]")
    finally:
        transport.close()