import os, time, sqlite3
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
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

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "backend")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://fluentbit:4318")
resource = Resource.create({"service.name": SERVICE_NAME})

# OpenTelemetry Setup
trace.set_tracer_provider(TracerProvider(resource=resource))
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces"))
)

metrics.set_meter_provider(MeterProvider(resource=resource))
metrics.get_meter_provider().add_metric_reader(
    PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{OTEL_ENDPOINT}/v1/metrics"), export_interval_millis=5000)
)

logs.set_logger_provider(LoggerProvider(resource=resource))
logs.get_logger_provider().add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{OTEL_ENDPOINT}/v1/logs"))
)

app = FastAPI(title="Auth Backend")
FastAPIInstrumentor.instrument_app(app)

meter = metrics.get_meter(SERVICE_NAME)
logger = logs.get_logger(SERVICE_NAME)

# Sample Metrics
login_attempts = meter.create_counter("auth_login_attempts_total", unit="1", description="Total login attempts")
login_success = meter.create_counter("auth_login_success_total", unit="1", description="Successful logins")
login_duration = meter.create_histogram("auth_login_duration_seconds", unit="s", description="Login validation time")

# DB Init
DB_PATH = "/data/users.db"
os.makedirs("/data", exist_ok=True)
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT)")
    c.execute("INSERT OR IGNORE INTO users VALUES ('admin', 'admin123'), ('user', 'secret')")
    conn.commit()
    conn.close()

init_db()

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/login")
async def login(req: LoginRequest):
    tracer = trace.get_tracer(SERVICE_NAME)
    start = time.time()
    login_attempts.add(1, {"auth_method": "password"})
    logger.emit(body="Login attempt received", severity_number=9) # INFO

    with tracer.start_as_current_span("validate_credentials"):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT password_hash FROM users WHERE username=?", (req.username,))
        row = c.fetchone()
        conn.close()

        time.sleep(0.05) # Simulate DB latency
        valid = row and row[0] == req.password
        dur = time.time() - start
        login_duration.record(dur, {"status": "success" if valid else "failure"})

        if valid:
            login_success.add(1, {"username": req.username})
            logger.emit(body=f"Login successful for {req.username}", severity_number=9)
            return {"status": "success", "message": "Validated"}
        logger.emit(body="Invalid credentials", severity_number=13) # WARN
        raise HTTPException(status_code=401, detail="Invalid credentials")