import os, time, httpx, logging
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram

# OTel Imports
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.resources import Resource

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "frontend")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://fluentbit:4318")
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
resource = Resource.create({"service.name": SERVICE_NAME})

try:
    trace.set_tracer_provider(TracerProvider(resource=resource))
    trace.get_tracer_provider().add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces"))
    )
    metrics.set_meter_provider(MeterProvider(resource=resource))
    metrics.get_meter_provider().add_metric_reader(
        PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{OTEL_ENDPOINT}/v1/metrics"), export_interval_millis=5000)
    )
    LoggingInstrumentor().instrument()
    logger.info("✅ OpenTelemetry initialized")
except Exception as e:
    logger.warning(f"⚠️ OTel init failed: {e}")

app = FastAPI()
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")
templates = Jinja2Templates(directory="templates")

meter = metrics.get_meter(SERVICE_NAME)
tracer = trace.get_tracer(SERVICE_NAME)

frontend_submissions = Counter("frontend_form_submissions_total", "Form submissions")
backend_call_duration = Histogram("frontend_backend_call_duration_seconds", "Backend API call time", ["status"])

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/validate", response_class=HTMLResponse)
async def validate(request: Request):
    # python-multipart is now installed, so this works
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    
    frontend_submissions.inc()
    logger.info(f"Validation request: {username}")

    start = time.time()
    try:
        with tracer.start_as_current_span("call_backend") as span:
            span.set_attribute("backend.url", BACKEND_URL)
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(f"{BACKEND_URL}/login", json={"username": username, "password": password})
            success = resp.status_code == 200
    except Exception as e:
        success = False
        logger.error(f"Backend call failed: {e}")

    backend_call_duration.labels(status="ok" if success else "fail").observe(time.time() - start)
    result = "✅ Credentials Validated!" if success else "❌ Validation Failed or Service Unreachable"
    return templates.TemplateResponse("index.html", {"request": request, "username": username, "result": result})