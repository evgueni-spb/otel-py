import os, time, sqlite3, logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram

# OTel imports (stable APIs for v1.24.0)
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

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "backend")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://fluentbit:4318")
resource = Resource.create({"service.name": SERVICE_NAME})

try:
    # Tracing
    trace.set_tracer_provider(TracerProvider(resource=resource))
    trace.get_tracer_provider().add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces"))
    )
    # Metrics
    metrics.set_meter_provider(MeterProvider(resource=resource))
    metrics.get_meter_provider().add_metric_reader(
        PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{OTEL_ENDPOINT}/v1/metrics"), export_interval_millis=5000)
    )
    # Logs via LoggingInstrumentor bridge
    LoggingInstrumentor().instrument()
    logger.info("✅ OpenTelemetry initialized")
except Exception as e:
    logger.warning(f"⚠️ OTel init failed (app continues without telemetry): {e}")

app = FastAPI(title="Auth Backend")
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

meter = metrics.get_meter(SERVICE_NAME)
tracer = trace.get_tracer(SERVICE_NAME)

# Prometheus Metrics
login_attempts = Counter("auth_login_attempts_total", "Total login attempts", ["auth_method"])
login_success = Counter("auth_login_success_total", "Successful logins", ["username"])
login_duration = Histogram("auth_login_duration_seconds", "Login validation time", ["status"])

# SQLite Setup
DB_PATH = "/data/users.db"
os.makedirs("/data", exist_ok=True)
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT)")
        c.execute("INSERT OR IGNORE INTO users VALUES ('admin', 'admin123')")
        c.execute("INSERT OR IGNORE INTO users VALUES ('user', 'secret')")
        conn.commit()
    logger.info("✅ Database initialized")
init_db()

class LoginRequest(BaseModel):
    username: str
    password: str

@app.get("/health")
async def health():
    return {"status": "healthy", "service": SERVICE_NAME}

@app.post("/login")
async def login(req: LoginRequest):
    start = time.time()
    login_attempts.labels(auth_method="password").inc()
    logger.info(f"Login attempt: {req.username}")

    with tracer.start_as_current_span("validate_credentials") as span:
        span.set_attribute("user.username", req.username)
        try:
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                c.execute("SELECT password FROM users WHERE username=?", (req.username,))
                row = c.fetchone()
            
            time.sleep(0.05)  # Simulate latency
            valid = row and row[0] == req.password
            duration = time.time() - start
            login_duration.labels(status="success" if valid else "failure").observe(duration)

            if valid:
                login_success.labels(username=req.username).inc()
                logger.info(f"✅ Login success: {req.username}")
                return {"status": "success", "message": "Validated"}
            else:
                logger.warning("❌ Invalid credentials")
                raise HTTPException(status_code=401, detail="Invalid credentials")
        except sqlite3.Error as e:
            logger.error(f"DB Error: {e}")
            raise HTTPException(status_code=500, detail="Database error")