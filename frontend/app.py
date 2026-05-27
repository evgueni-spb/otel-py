import os, time, httpx, logging
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram

# OTel Imports
from opentelemetry import trace
from opentelemetry.propagate import inject  # ✅ For manual context propagation
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "frontend")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "jaeger:4317")
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
resource = Resource.create({"service.name": SERVICE_NAME})

trace.set_tracer_provider(TracerProvider(resource=resource))
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
)

app = FastAPI()
# Instrument FastAPI (Handles incoming requests)
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")
templates = Jinja2Templates(directory="templates")

tracer = trace.get_tracer(SERVICE_NAME)
frontend_submissions = Counter("frontend_form_submissions", "Form submissions")
backend_call_duration = Histogram("frontend_backend_call_duration_seconds", "Backend API call time", ["status"])

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/validate", response_class=HTMLResponse)
async def validate(request: Request):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    frontend_submissions.inc()
    logger.info(f"Validation request: {username}")

    start = time.time()
    try:
        # ✅ Create span for this operation
        with tracer.start_as_current_span("call_backend") as span:
            span.set_attribute("backend.url", BACKEND_URL)
            
            # ✅ 1. Prepare headers
            headers = {}
            # 2. Inject current trace context into headers (adds traceparent)
            inject(headers)
            
            # 3. Send headers with the request
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{BACKEND_URL}/login", 
                    json={"username": username, "password": password},
                    headers=headers
                )
            success = resp.status_code == 200
            span.set_attribute("http.status_code", resp.status_code)
    except Exception as e:
        success = False
        logger.error(f"Backend call failed: {e}")
        if 'span' in locals(): span.record_exception(e)

    backend_call_duration.labels(status="ok" if success else "fail").observe(time.time() - start)
    result = "✅ Credentials Validated!" if success else "❌ Validation Failed"
    return templates.TemplateResponse("index.html", {"request": request, "username": username, "result": result})