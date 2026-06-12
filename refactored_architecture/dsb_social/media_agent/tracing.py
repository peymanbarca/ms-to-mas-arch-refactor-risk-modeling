"""Jaeger/OpenTracing tracer initialisation."""
import logging
import opentracing

logger = logging.getLogger("media-agent.tracing")


def init_tracer(jaeger_cfg: dict) -> opentracing.Tracer:
    try:
        import jaeger_client
        cfg = jaeger_client.Config(
            config={
                "sampler": {
                    "type":  jaeger_cfg.get("sampler_type",  "const"),
                    "param": jaeger_cfg.get("sampler_param", 1),
                },
                "local_agent": {
                    "reporting_host": jaeger_cfg.get("agent_host", "jaeger-agent"),
                    "reporting_port": int(jaeger_cfg.get("agent_port", 6831)),
                },
                "logging": bool(jaeger_cfg.get("reporter_log_spans", False)),
            },
            service_name=jaeger_cfg.get("service_name", "media-agent"),
            validate=True,
        )
        tracer = cfg.initialize_tracer()
        logger.info("Jaeger tracer initialised")
        return tracer
    except Exception as exc:
        logger.warning("Jaeger init failed (%s) — using no-op tracer", exc)
        return opentracing.tracer