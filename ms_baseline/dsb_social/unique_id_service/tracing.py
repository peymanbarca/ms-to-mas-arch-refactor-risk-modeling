"""
Jaeger tracer initialisation.

Mirrors the OpenTracing setup used in all DeathStarBench C++ services
(via opentracing-cpp + jaegertracing/jaeger-client-cpp).

The config dict shape matches service-config.json ["jaeger"]:
  {
    "service_name":      "unique-id-service",
    "agent_host":        "jaeger-agent",
    "agent_port":        6831,
    "sampler_type":      "const",    # "const" | "probabilistic" | "rateLimiting"
    "sampler_param":     1,          # 1 = sample everything (const)
    "reporter_log_spans": false
  }

If Jaeger is not reachable the service still starts — we fall back to a
no-op tracer so the rest of the code is unaffected.
"""

import logging

import opentracing

logger = logging.getLogger("unique-id-service.tracing")


def init_tracer(jaeger_cfg: dict) -> opentracing.Tracer:
    """
    Initialise and return a Jaeger tracer, or a no-op tracer on failure.
    Sets the global opentracing.tracer as a side-effect (matches C++ behaviour).
    """
    try:
        import jaeger_client

        service_name = jaeger_cfg.get("service_name", "unique-id-service")
        agent_host   = jaeger_cfg.get("agent_host",   "jaeger-agent")
        agent_port   = int(jaeger_cfg.get("agent_port", 6831))
        sampler_type = jaeger_cfg.get("sampler_type",  "const")
        sampler_param = jaeger_cfg.get("sampler_param", 1)
        log_spans    = bool(jaeger_cfg.get("reporter_log_spans", False))

        cfg = jaeger_client.Config(
            config={
                "sampler": {
                    "type":  sampler_type,
                    "param": sampler_param,
                },
                "local_agent": {
                    "reporting_host": agent_host,
                    "reporting_port": agent_port,
                },
                "logging": log_spans,
            },
            service_name=service_name,
            validate=True,
        )

        tracer = cfg.initialize_tracer()
        logger.info(
            "Jaeger tracer initialised: service=%s agent=%s:%d",
            service_name, agent_host, agent_port,
        )
        return tracer

    except Exception as exc:
        logger.warning(
            "Failed to initialise Jaeger tracer (%s) — using no-op tracer", exc
        )
        return opentracing.tracer   # global no-op tracer
