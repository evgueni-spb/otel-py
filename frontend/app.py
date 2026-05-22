import os, time, httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from opentelemetry import trace, metrics, logs
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.logs import LoggerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.log_exporter import OTLPLogExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.logs.export import BatchLogRecordProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "frontend")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://fluentbit:4318")
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
resource = Resource.create({"service.name": SERVICE_NAME})

# OpenTelemetry Setup (Same as backend)
trace.set_tracer_provider(TracerProvider(resource=resource))
trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces")))
metrics.set_meter_provider(MeterProvider(resource=resource))
metrics.get_meter_provider().add_metric_reader(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{OTEL_ENDPOINT}/v1/metrics"), export_interval_millis=5000))
logs.set_logger_provider(LoggerProvider(resource=resource))
logs.get_logger_provider().add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{OTEL_ENDPOINT}/v1/logs")))

app = FastAPI()
FastAPIInstrumentor.instrument_app(app)
templates = Jinja2Templates(directory="templates")

meter = metrics.get_meter(SERVICE_NAME)
logger = logs.get_logger(SERVICE_NAME)

frontend_requests = meter.create_counter("frontend_form_submissions_total", unit="1", description="Frontend validations triggered")
backend_call_duration = meter.create_histogram("frontend_backend_call_duration_seconds", unit="s", description="Time to call backend API")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/validate", response_class=HTMLResponse)
async def validate(request: Request):
    form = await request.form()
    username, password = form.get("username"), form.get("password")
    frontend_requests.add(1)
    logger.emit(body=f"Frontend validation submitted for {username}")

    start = time.time()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{BACKEND_URL}/login", json={"username": username, "password": password})
        success = resp.status_code == 200
    except Exception as e:
        success = False
        logger.emit(body=f"Backend call failed: {str(e)}", severity_number=13)

    backend_call_duration.record(time.time() - start, {"status": "ok" if success else "fail"})
    msg = "✅ Credentials Validated Successfully!" if success else "❌ Validation Failed or Backend Unreachable"
    return templates.TemplateResponse("index.html", {"request": request, "username": username, "result": msg})