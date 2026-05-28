import os, time, httpx, logging
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from opentelemetry import trace, metrics
from opentelemetry.propagate import inject
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "frontend")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector:4317")
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
resource = Resource.create({"service.name": SERVICE_NAME})

# 1. Tracing
trace.set_tracer_provider(TracerProvider(resource=resource))
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
)

# 2. Metrics ✅ Fixed: metric_readers passed to constructor
reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True),
    export_interval_millis=5000
)
metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

# 3. Logs
LoggingInstrumentor().instrument()

# 4. Instrumentation
app = FastAPI()
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()
templates = Jinja2Templates(directory="templates")

tracer = trace.get_tracer(SERVICE_NAME)
meter = metrics.get_meter(SERVICE_NAME)

frontend_submissions = meter.create_counter("frontend_form_submissions", description="Form submissions")
backend_call_duration = meter.create_histogram("frontend_backend_call_duration_seconds", description="Backend API call time")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/validate", response_class=HTMLResponse)
async def validate(request: Request):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    frontend_submissions.add(1)
    logger.info(f"Validation request: {username}")

    start = time.time()
    try:
        with tracer.start_as_current_span("call_backend") as span:
            span.set_attribute("backend.url", BACKEND_URL)
            
            headers = {}
            inject(headers)
            
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(f"{BACKEND_URL}/login", json={"username": username, "password": password}, headers=headers)
            success = resp.status_code == 200
            span.set_attribute("http.status_code", resp.status_code)
    except Exception as e:
        success = False
        logger.error(f"Backend call failed: {e}")
        if 'span' in locals(): span.record_exception(e)

    backend_call_duration.record(time.time() - start, {"status": "ok" if success else "fail"})
    result = "✅ Credentials Validated!" if success else "❌ Validation Failed"
    return templates.TemplateResponse("index.html", {"request": request, "username": username, "result": result})