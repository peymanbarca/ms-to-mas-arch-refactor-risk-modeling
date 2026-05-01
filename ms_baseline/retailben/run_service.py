import uvicorn
from fastapi import FastAPI
import sys

app = FastAPI()

service_name = sys.argv[1]
port = int(sys.argv[2])

if __name__ == "__main__":
    uvicorn.run(f"{service_name}:app", host="0.0.0.0", port=port, reload=True)
