import os, time, sqlite3, logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.resources import Resource

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "backend")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector:4317")
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

# 4. FastAPI Instrumentation
app = FastAPI()
FastAPIInstrumentor.instrument_app(app)

tracer = trace.get_tracer(SERVICE_NAME)
meter = metrics.get_meter(SERVICE_NAME)

login_attempts = meter.create_counter("auth_login_attempts", description="Total login attempts")
login_success = meter.create_counter("auth_login_success", description="Successful logins")
login_duration = meter.create_histogram("auth_login_duration_seconds", description="Login validation time")

DB_PATH = "/data/users.db"
os.makedirs("/data", exist_ok=True)
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT)")
        c.execute("INSERT OR IGNORE INTO users VALUES ('admin', 'admin123')")
        c.execute("INSERT OR IGNORE INTO users VALUES ('user', 'secret')")
        conn.commit()
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
    login_attempts.add(1, {"auth_method": "password"})
    logger.info(f"Login attempt: {req.username}")

    with tracer.start_as_current_span("validate_credentials") as span:
        span.set_attribute("user.username", req.username)
        try:
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                c.execute("SELECT password FROM users WHERE username=?", (req.username,))
                row = c.fetchone()
            
            time.sleep(0.05)
            valid = row and row[0] == req.password
            login_duration.record(time.time() - start, {"status": "success" if valid else "failure"})

            if valid:
                span.set_attribute("auth.valid", True)
                login_success.add(1, {"username": req.username})
                return {"status": "success"}
            raise HTTPException(status_code=401, detail="Invalid credentials")
        except Exception as e:
            span.record_exception(e)
            raise HTTPException(status_code=500, detail=str(e))