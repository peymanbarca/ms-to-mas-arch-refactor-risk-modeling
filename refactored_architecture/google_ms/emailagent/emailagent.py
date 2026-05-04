"""
emailservice/agent.py

LangGraph email confirmation agent.

Every call to SendOrderConfirmation runs this graph:

    ┌──────────────────────┐
    │  prepare_order_data  │  (deterministic) convert proto → clean dict
    └──────────┬───────────┘
               │
    ┌──────────▼───────────┐
    │  personalise_message │  (LLM / Ollama llama3)
    │                      │  generates a warm, personalised paragraph
    │                      │  based on the order contents
    └──────────┬───────────┘
               │
    ┌──────────▼───────────┐
    │  render_template     │  (deterministic) Jinja2 renders confirmation.html
    │                      │  injecting personalised_message into the template
    └──────────┬───────────┘
               │
    ┌──────────▼───────────┐
    │  send_email          │  (deterministic / pluggable)
    │                      │  logs today; swap in SendGrid / SES / etc.
    └──────────┬───────────┘
               │
    ┌──────────▼───────────┐
    │  persist_email_log   │  (deterministic) writes audit record to MongoDB
    └──────────┬───────────┘
               │
              END

Node roles
──────────
prepare_order_data   Converts the proto OrderResult into a serialisable dict
                     used by both the LLM prompt and the Jinja2 template.

personalise_message  LLM node (Ollama llama3).  Writes a short, friendly
                     personalised paragraph acknowledging the specific items
                     ordered, then suggests one complementary product category.
                     This is the only non-deterministic step.

render_template      Jinja2 renders templates/confirmation.html with the order
                     dict and the LLM-generated message injected.

send_email           Pluggable delivery node.  Today it logs the rendered HTML
                     at INFO level (same as original dummy implementation).
                     Set EMAIL_PROVIDER=sendgrid/ses to enable real sending.

persist_email_log    Writes a structured audit document to MongoDB.
                     Failure is non-fatal — never blocks the gRPC response.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import (
    Environment,
    FileSystemLoader,
    TemplateError,
    select_autoescape,
)
from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph
from motor.motor_asyncio import AsyncIOMotorClient
from typing_extensions import TypedDict

logger = logging.getLogger("emailagent")

# ── LLM ───────────────────────────────────────────────────────────────────────
llm = ChatOllama(model="llama3.2:3b", temperature=0.0, reasoning=False)

# ── MongoDB reference (wired by main.py on startup) ───────────────────────────
db: Any = None

# ── Jinja2 environment (loaded once at import time) ───────────────────────────
_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
Path(_TEMPLATE_DIR).mkdir(parents=True, exist_ok=True)

_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


# ════════════════════════════════════════════════════════════════════════════
# Agent state
# ════════════════════════════════════════════════════════════════════════════

class EmailAgentState(TypedDict):
    """
    Shared state threaded through every node in the graph.

    Input fields (set before ainvoke):
        recipient_email  – destination email address
        order_proto      – the raw demo_pb2.OrderResult message

    Intermediate / output fields (written by nodes):
        order_dict           – serialisable dict built by prepare_order_data
        personalised_message – LLM-generated paragraph
        rendered_html        – final Jinja2 output
        send_status          – "sent" | "logged" | "failed"
        error                – set if any node fails; None otherwise

    Metrics:
        total_input_tokens
        total_output_tokens
        total_llm_calls
    """
    # inputs
    recipient_email: str
    order_proto:     Any          # demo_pb2.OrderResult

    # intermediate
    order_dict:           Optional[Dict[str, Any]]
    personalised_message: Optional[str]
    rendered_html:        Optional[str]
    send_status:          Optional[str]
    error:                Optional[str]

    # metrics
    total_input_tokens:  int
    total_output_tokens: int
    total_llm_calls:     int


# ════════════════════════════════════════════════════════════════════════════
# Node 1 – prepare_order_data  (deterministic)
# ════════════════════════════════════════════════════════════════════════════

async def prepare_order_data_node(state: EmailAgentState) -> EmailAgentState:
    """
    Convert the proto OrderResult into a clean Python dict.

    Also fixes the previously commented-out items list in the original
    _render_confirmation — items are now fully serialised including cost.

    This dict is used by both the LLM prompt (Node 2) and the Jinja2
    template (Node 3) so proto objects never cross node boundaries.
    """
    order = state["order_proto"]
    logger.info(
        "[prepare_order_data] order_id=%s recipient=%s items=%d",
        order.order_id,
        state["recipient_email"],
        len(order.items),
    )

    # Serialise items (was commented out in the original _render_confirmation)
    items = [
        {
            "product_id": item.item.product_id,
            "quantity":   item.item.quantity,
            "cost": {
                "currency_code": item.cost.currency_code,
                "units":         item.cost.units,
                "nanos":         item.cost.nanos,
            },
        }
        for item in order.items
    ]

    order_dict = {
        "order_id":             order.order_id,
        "shipping_tracking_id": order.shipping_tracking_id,
        "shipping_cost": {
            "currency_code": order.shipping_cost.currency_code,
            "units":         order.shipping_cost.units,
            "nanos":         order.shipping_cost.nanos,
        },
        "shipping_address": {
            "street_address": order.shipping_address.street_address,
            "city":           order.shipping_address.city,
            "state":          order.shipping_address.state,
            "country":        order.shipping_address.country,
            "zip_code":       order.shipping_address.zip_code,
        },
        "items": items,
    }

    return {**state, "order_dict": order_dict, "error": None}


# ════════════════════════════════════════════════════════════════════════════
# Node 2 – personalise_message  (LLM node)
# ════════════════════════════════════════════════════════════════════════════

def _parse_personalised_message(text: str) -> str:
    """
    Extract just the personalised message text from the LLM response.
    Strips JSON wrapper if the model wrapped it, otherwise returns cleaned text.
    """
    # Try JSON wrapper first: {"message": "..."}
    try:
        m = re.search(r'\{.*?"message"\s*:\s*"(.*?)"\s*\}', text, re.DOTALL)
        if m:
            return m.group(1).strip()
    except Exception:
        pass

    # Try plain JSON string
    try:
        data = json.loads(text.strip())
        if isinstance(data, dict):
            return str(data.get("message", text)).strip()
    except Exception:
        pass

    # Return raw text, strip markdown fences
    cleaned = re.sub(r"```[a-z]*\n?", "", text).strip()
    return cleaned


async def personalise_message_node(state: EmailAgentState) -> EmailAgentState:
    """
    LLM node — the only non-deterministic step.

    Receives the serialised order dict and generates a warm, personalised
    paragraph for the confirmation email that:
      • Acknowledges the specific products ordered
      • Mentions the shipping destination (city, country)
      • Suggests one complementary product category

    Returns JSON: {"message": "<paragraph text>"}
    Falls back to a friendly default if the LLM response is unparseable.
    """
    order = state["order_dict"]
    cents = order["shipping_cost"]["nanos"] // 10_000_000

    # Build a human-readable items summary for the prompt
    items_summary = ", ".join(
        f"{it['quantity']}× {it['product_id']}" for it in order["items"]
    ) or "various items"

    shipping_dest = (
        f"{order['shipping_address']['city']}, {order['shipping_address']['country']}"
    )

    prompt = f"""
You are a friendly customer service assistant for an online boutique.

Write a SHORT (1 sentence), warm, personalised paragraph for an order confirmation email.

Guidelines:
- Mention the specific items ordered (listed below)
- Reference the shipping destination city
- Keep the tone upbeat and professional
- Do NOT mention prices, tracking IDs, or internal order IDs
- Return ONLY a JSON object — no markdown, no preamble

Output schema:
{{ "message": "<your paragraph here>" }}

Order details:
  Items ordered  : {items_summary}
  Ships to       : {shipping_dest}
  Shipping cost  : {order['shipping_cost']['currency_code']} {order['shipping_cost']['units']}.{cents:02d}
""".strip()

    # can further extended with suggestion

    logger.info("[personalise_message] invoking LLM | order_id=%s items=%d",
                order["order_id"], len(order["items"]))

    try:
        response    = await asyncio.to_thread(llm.invoke, prompt)
        raw         = response.text()
        in_tokens   = response.usage_metadata.get("input_tokens",  0)
        out_tokens  = response.usage_metadata.get("output_tokens", 0)

        logger.info("[personalise_message] LLM raw: %s", raw[:200])
        logger.info("[personalise_message] tokens in=%d out=%d", in_tokens, out_tokens)

        message = _parse_personalised_message(raw)
        if not message:
            raise ValueError("Empty message from LLM")

        return {
            **state,
            "personalised_message": message,
            "total_input_tokens":   state["total_input_tokens"]  + in_tokens,
            "total_output_tokens":  state["total_output_tokens"] + out_tokens,
            "total_llm_calls":      state["total_llm_calls"]     + 1,
        }

    except Exception as exc:
        logger.warning("[personalise_message] LLM failed (%s) — using fallback", exc)
        fallback = (
            f"Thank you for your order! Your items are on their way to "
            f"{shipping_dest}. We hope you enjoy your purchase and look "
            f"forward to serving you again."
        )
        return {
            **state,
            "personalised_message": fallback,
            # Don't increment llm metrics on failure
        }


# ════════════════════════════════════════════════════════════════════════════
# Node 3 – render_template  (deterministic)
# ════════════════════════════════════════════════════════════════════════════

# Inline fallback used when templates/confirmation.html is absent
_FALLBACK_TEMPLATE = """\
<html><body>
<h1>Order Confirmation</h1>
<p>Dear Customer, thank you for your order!</p>
<p><strong>Order ID:</strong> {{ order.order_id }}</p>
<p><strong>Tracking:</strong> {{ order.shipping_tracking_id }}</p>
<p><strong>Ships to:</strong>
   {{ order.shipping_address.street_address }},
   {{ order.shipping_address.city }},
   {{ order.shipping_address.state }} {{ order.shipping_address.zip_code }},
   {{ order.shipping_address.country }}</p>
<p><strong>Shipping cost:</strong>
   {{ order.shipping_cost.currency_code }}
   {{ order.shipping_cost.units }}.{{ "%02d"|format(order.shipping_cost.nanos // 10000000) }}</p>
{% if personalised_message %}<p>{{ personalised_message }}</p>{% endif %}
</body></html>"""


async def render_template_node(state: EmailAgentState) -> EmailAgentState:
    """
    Deterministic tool node — renders the Jinja2 HTML template.

    Tries templates/confirmation.html first; falls back to the inline
    default if the file is missing (matches original servicer behaviour).
    Also fixes the original: items list is now fully rendered in the template.
    """
    order    = state["order_dict"]
    message  = state.get("personalised_message") or ""

    logger.info("[render_template] rendering | order_id=%s", order["order_id"])

    try:
        try:
            tmpl = _jinja_env.get_template("confirmation.html")
        except Exception:
            logger.info("[render_template] confirmation.html not found — using fallback")
            tmpl = _jinja_env.from_string(_FALLBACK_TEMPLATE)

        html = tmpl.render(order=order, personalised_message=message)
        logger.info("[render_template] rendered %d chars", len(html))

        return {**state, "rendered_html": html}

    except TemplateError as exc:
        logger.error("[render_template] Jinja2 error: %s", exc, exc_info=True)
        return {
            **state,
            "rendered_html": None,
            "error": f"Template rendering failed: {exc}",
        }


# ════════════════════════════════════════════════════════════════════════════
# Node 4 – send_email  (deterministic / pluggable)
# ════════════════════════════════════════════════════════════════════════════

async def send_email_node(state: EmailAgentState) -> EmailAgentState:
    """
    Deterministic tool node — sends (or logs) the confirmation email.

    Current behaviour (mirrors original dummy implementation):
        Logs the rendered HTML at INFO level.

    To enable real sending set EMAIL_PROVIDER env var:
        sendgrid → uses SENDGRID_API_KEY
        ses      → uses AWS_REGION + boto3
        smtp     → uses SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS

    Returns send_status: "sent" | "logged" | "failed"
    """
    if state.get("error") or not state.get("rendered_html"):
        logger.warning(
            "[send_email] skipping send due to prior error: %s", state.get("error")
        )
        return {**state, "send_status": "failed"}

    email    = state["recipient_email"]
    order_id = state["order_dict"]["order_id"]
    html     = state["rendered_html"]
    provider = os.getenv("EMAIL_PROVIDER", "log").lower()

    logger.info(
        "[send_email] sending confirmation | to=%s order_id=%s provider=%s html_bytes=%d",
        email, order_id, provider, len(html),
    )

    try:
        if provider == "sendgrid":
            send_status = await _send_via_sendgrid(email, order_id, html)

        elif provider == "ses":
            send_status = await _send_via_ses(email, order_id, html)

        elif provider == "smtp":
            send_status = await _send_via_smtp(email, order_id, html)

        else:
            # Default: dummy / log mode (original implementation behaviour)
            logger.info(
                "[send_email] (dummy mode) confirmation email for order %s → %s",
                order_id, email,
            )
            logger.debug("[send_email] HTML preview: %s…", html[:300])
            send_status = "logged"

        logger.info("[send_email] status=%s | to=%s order_id=%s",
                    send_status, email, order_id)
        return {**state, "send_status": send_status}

    except Exception as exc:
        logger.error("[send_email] delivery failed | to=%s error=%s", email, exc, exc_info=True)
        return {**state, "send_status": "failed", "error": str(exc)}


# ── Provider stubs (replace bodies with real SDK calls) ───────────────────────

async def _send_via_sendgrid(to: str, order_id: str, html: str) -> str:
    """
    Send via SendGrid.
    Requires: pip install sendgrid  +  SENDGRID_API_KEY env var.
    """
    import sendgrid  # type: ignore
    from sendgrid.helpers.mail import Mail  # type: ignore

    sg   = sendgrid.SendGridAPIClient(api_key=os.environ["SENDGRID_API_KEY"])
    from_email = os.getenv("EMAIL_FROM", "noreply@onlineboutique.example")
    msg  = Mail(
        from_email=from_email,
        to_emails=to,
        subject=f"Your Online Boutique Order {order_id} is Confirmed!",
        html_content=html,
    )
    resp = await asyncio.to_thread(sg.send, msg)
    logger.info("[sendgrid] status_code=%d", resp.status_code)
    return "sent"


async def _send_via_ses(to: str, order_id: str, html: str) -> str:
    """
    Send via AWS SES.
    Requires: pip install boto3  +  AWS_REGION / AWS credentials.
    """
    import boto3  # type: ignore

    ses    = boto3.client("ses", region_name=os.getenv("AWS_REGION", "us-east-1"))
    source = os.getenv("EMAIL_FROM", "noreply@onlineboutique.example")
    resp   = await asyncio.to_thread(
        ses.send_email,
        Source=source,
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {"Data": f"Your Online Boutique Order {order_id} is Confirmed!"},
            "Body":    {"Html": {"Data": html}},
        },
    )
    logger.info("[ses] MessageId=%s", resp["MessageId"])
    return "sent"


async def _send_via_smtp(to: str, order_id: str, html: str) -> str:
    """
    Send via SMTP.
    Requires: SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS env vars.
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    host   = os.environ["SMTP_HOST"]
    port   = int(os.getenv("SMTP_PORT", "587"))
    user   = os.environ["SMTP_USER"]
    passwd = os.environ["SMTP_PASS"]
    source = os.getenv("EMAIL_FROM", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your Online Boutique Order {order_id} is Confirmed!"
    msg["From"]    = source
    msg["To"]      = to
    msg.attach(MIMEText(html, "html"))

    def _blocking_send():
        with smtplib.SMTP(host, port) as s:
            s.ehlo(); s.starttls(); s.login(user, passwd)
            s.sendmail(source, [to], msg.as_string())

    await asyncio.to_thread(_blocking_send)
    return "sent"


# ════════════════════════════════════════════════════════════════════════════
# Node 5 – persist_email_log  (deterministic)
# ════════════════════════════════════════════════════════════════════════════

async def persist_email_log_node(state: EmailAgentState) -> EmailAgentState:
    """
    Deterministic tool node — writes an audit record to MongoDB.

    Document schema:
        order_id             – from order_dict
        recipient_email      – destination address
        send_status          – "sent" | "logged" | "failed"
        personalised_message – LLM-generated paragraph (or fallback)
        html_bytes           – length of rendered HTML
        error                – None or error string
        llm_metrics          – { input_tokens, output_tokens, llm_calls }
        created_at           – UTC timestamp

    Failure is non-fatal — never blocks the gRPC response.
    """
    order_id = state["order_dict"]["order_id"] if state.get("order_dict") else "unknown"


    doc = {
        "order_id":            order_id,
        "recipient_email":     state["recipient_email"],
        "send_status":         state.get("send_status", "unknown"),
        "personalised_message": state.get("personalised_message"),
        "html_bytes":          len(state["rendered_html"]) if state.get("rendered_html") else 0,
        "error":               state.get("error"),
        "llm_metrics": {
            "input_tokens":  state["total_input_tokens"],
            "output_tokens": state["total_output_tokens"],
            "llm_calls":     state["total_llm_calls"],
        },
        "created_at": datetime.datetime.now(tz=datetime.timezone.utc),
    }
    
    logger.info(
        "[email_log]  order_id=%s status=%s, content=%s",
        order_id, state.get("send_status"), doc
    )


    return state


# ════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ════════════════════════════════════════════════════════════════════════════

def build_email_agent():
    """Assemble and compile the LangGraph email agent."""
    graph = StateGraph(EmailAgentState)

    graph.add_node("prepare_order_data",   prepare_order_data_node)
    graph.add_node("personalise_message",  personalise_message_node)
    graph.add_node("render_template",      render_template_node)
    graph.add_node("send_email",           send_email_node)
    graph.add_node("persist_email_log",    persist_email_log_node)

    graph.set_entry_point("prepare_order_data")

    graph.add_edge("prepare_order_data",  "personalise_message")
    graph.add_edge("personalise_message", "render_template")
    graph.add_edge("render_template",     "send_email")
    graph.add_edge("send_email",          "persist_email_log")
    graph.add_edge("persist_email_log",   END)

    compiled = graph.compile()
    logger.info("[EmailAgent] graph compiled successfully")
    return compiled


# Singleton graph
email_graph = build_email_agent()


# ════════════════════════════════════════════════════════════════════════════
# Public helper called by the gRPC servicer
# ════════════════════════════════════════════════════════════════════════════

async def run_email_agent(
    recipient_email: str,
    order_proto: Any,
) -> EmailAgentState:
    """
    Build initial state and invoke the compiled graph.

    Args:
        recipient_email: Destination email address.
        order_proto:     demo_pb2.OrderResult protobuf message.

    Returns:
        Final EmailAgentState after all nodes have run.
        Check state["error"] and state["send_status"] for outcome.
    """
    initial_state: EmailAgentState = {
        "recipient_email":     recipient_email,
        "order_proto":         order_proto,
        "order_dict":          None,
        "personalised_message": None,
        "rendered_html":       None,
        "send_status":         None,
        "error":               None,
        "total_input_tokens":  0,
        "total_output_tokens": 0,
        "total_llm_calls":     0,
    }

    logger.info(
        "[run_email_agent] invoking graph | to=%s order_id=%s",
        recipient_email,
        getattr(order_proto, "order_id", "?"),
    )

    result: EmailAgentState = await email_graph.ainvoke(initial_state)

    logger.info(
        "[run_email_agent] completed | status=%s llm_calls=%d error=%s",
        result.get("send_status"),
        result["total_llm_calls"],
        result.get("error"),
    )

    return result