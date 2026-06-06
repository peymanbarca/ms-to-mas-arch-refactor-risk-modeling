# client.py
import sys
sys.path.insert(0, "gen_py")

from thrift.transport import TSocket, TTransport
from thrift.protocol  import TBinaryProtocol
from ms_baseline.dsb_social.gen_py.social_network import UniqueIdService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import PostType

def make_client(host="127.0.0.1", port=9090):
    sock      = TSocket.TSocket(host, port)
    transport = TTransport.TFramedTransport(sock)
    protocol  = TBinaryProtocol.TBinaryProtocol(transport)
    client    = UniqueIdService.Client(protocol)
    transport.open()
    return client, transport

if __name__ == "__main__":
    client, transport = make_client()
    try:
        for i in range(5):
            uid = client.ComposeUniqueId(
                req_id=i,
                post_type=PostType.POST,
                carrier={}
            )
            print(f"req_id={i}  ->  uid={uid}")
    finally:
        transport.close()