"""
currencyservice/main.py

Replaces the original Node.js currencyservice.
- gRPC server on port 7000
- FastAPI HTTP server on port 8080
- Reads currency rates from currency_data.json
"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.base_service import make_health_app, run_service

logger = logging.getLogger(__name__)
GRPC_PORT = int(os.getenv("PORT", "5053"))

# ── Currency data loader ─────────────────────────────────────────────────────

_CURRENCY_DATA_PATH = os.getenv(
    "CURRENCY_DATA_JSON",
    os.path.join(os.path.dirname(__file__), "currency_conversion.json"),
)

def _load_currency_data() -> dict[str, float]:
    with open(_CURRENCY_DATA_PATH) as f:
        data = json.load(f)
    return data


# ── Helper function for carrying decimals ────────────────────────────────────

def _carry(amount: dict) -> dict:
    fraction_size = 10**9
    amount["nanos"] += (amount["units"] % 1) * fraction_size
    amount["units"] = int(amount["units"]) + int(amount["nanos"] // fraction_size)
    amount["nanos"] = amount["nanos"] % fraction_size
    return amount


# ── gRPC Servicer ────────────────────────────────────────────────────────────

class CurrencyServiceServicer(demo_pb2_grpc.CurrencyServiceServicer):

    def __init__(self):
        self._rates: dict = _load_currency_data()
        logger.info("Loaded currency rates for %d currencies", len(self._rates))

    async def GetSupportedCurrencies(self, request, context):
        return demo_pb2.GetSupportedCurrenciesResponse(
            currency_codes=list(self._rates.keys())
        )

    async def Convert(self, request, context):
        from_code = request.from_.currency_code
        to_code = request.to_code
        amount_units = request.from_.units
        amount_nanos = request.from_.nanos

        if from_code not in self._rates.keys() or to_code not in self._rates.keys():
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Unsupported currency")

        # Convert to EUR first
        euros = _carry({
            "units": amount_units / float(self._rates[from_code]),
            "nanos": amount_nanos / float(self._rates[from_code])
        })
        euros["nanos"] = round(euros["nanos"])

        # Convert to target currency
        result = _carry({
            "units": euros["units"] * float(self._rates[to_code]),
            "nanos": euros["nanos"] * float(self._rates[to_code])
        })

        return demo_pb2.Money(
            currency_code=to_code,
            units=int(result["units"]),
            nanos=int(result["nanos"]),
        )


import grpc  # noqa: E402

# ── FastAPI ──────────────────────────────────────────────────────────────────

app = make_health_app("currencyservice")

_svc = None  # lazy singleton

def _get_svc() -> CurrencyServiceServicer:
    global _svc
    if _svc is None:
        _svc = CurrencyServiceServicer()
    return _svc

@app.get("/currencies", summary="Get supported currencies")
async def rest_get_supported_currencies():
    svc = _get_svc()
    resp = await svc.GetSupportedCurrencies(demo_pb2.Empty(), None)
    return {"currency_codes": resp.currency_codes}

@app.post("/convert", summary="Convert currency")
async def rest_convert(request: dict):
    svc = _get_svc()
    from_money = demo_pb2.Money(
        currency_code=request["from"]["currency_code"],
        units=request["from"]["units"],
        nanos=request["from"]["nanos"],
    )
    grpc_request = demo_pb2.CurrencyConversionRequest(
        from_=from_money,
        to_code=request["to_code"],
    )
    resp = await svc.Convert(grpc_request, None)
    return {
        "currency_code": resp.currency_code,
        "units": resp.units,
        "nanos": resp.nanos,
    }


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_service(
        demo_pb2_grpc.add_CurrencyServiceServicer_to_server,
        CurrencyServiceServicer,
        GRPC_PORT,
        app,
    )
