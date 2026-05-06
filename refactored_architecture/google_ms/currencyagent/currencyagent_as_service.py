

import json
import logging
import os
import sys

import grpc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from ..shared import demo_pb2
from ..shared import demo_pb2_grpc
from ..shared.base_service import make_health_app, run_service
from ..shared.metrics import build_llm_metrics, metrics_to_dict
from .currencyagent import run_currency_conversion_agent

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
        """
        gRPC Convert endpoint – now powered by CurrencyConversionAgent (LangGraph).

        Flow:
          1. Extract source/target currencies and amount from gRPC request.
          2. Invoke run_currency_conversion_agent() – orchestrates the agentic workflow:
             • validate_currencies:  check both currencies are supported
             • calculate_rate:       EUR-based conversion calculation
             • conversion_review:    LLM verifies calculation correctness
             • persist_conversion:   audit trail to MongoDB
          3. Return converted amount in Money protobuf (same interface as before).

        Key difference from baseline servicer:
          • Conversion calculation now goes through LLM verification (non-deterministic).
          • Full audit trail stored in MongoDB with rates, calculation, review.
          • Token metrics included for observability.
        """
        from_code = request.from_.currency_code
        to_code = request.to_code

        # Invoke the currency agent
        agent_result = await run_currency_conversion_agent(
            from_currency_code=from_code,
            from_units=request.from_.units,
            from_nanos=request.from_.nanos,
            to_currency_code=to_code,
            rates=self._rates,
        )

        # Extract decision
        decision_status = agent_result["decision"].get("status", "REJECTED")
        result_money = agent_result["decision"].get("result")

        if decision_status != "SUCCESS" or not result_money:
            logger.warning("[Convert] conversion rejected | from=%s to=%s reason=%s",
                          from_code, to_code, agent_result["decision"].get("reason", "Unknown"))
            await context.abort(grpc.StatusCode.INTERNAL, "Currency conversion failed")

        # Build LLM metrics
        llm_metrics = build_llm_metrics(
            total_input_tokens=agent_result.get("total_input_tokens", 0),
            total_output_tokens=agent_result.get("total_output_tokens", 0),
            total_llm_calls=agent_result.get("total_llm_calls", 0),
        )

        converted_amount = demo_pb2.Money(
            currency_code=result_money["currency_code"],
            units=int(result_money["units"]),
            nanos=int(result_money["nanos"]),
        )

        return demo_pb2.CurrencyConversionResponse(
            converted_amount=converted_amount,
            llm_metrics=llm_metrics,
        )


import grpc  # noqa: E402

# ── FastAPI ──────────────────────────────────────────────────────────────────

app = make_health_app("currencyagent")

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
