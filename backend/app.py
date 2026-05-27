import os, time, sqlite3, logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram

# OTel Imports
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "backend")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "jaeger:4317")
resource = Resource.create({"service.name": SERVICE_NAME})

trace.set_tracer_provider(TracerProvider(resource=resource))
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
)

# ✅ 1. Create App
app = FastAPI()

# ✅ 2. Instrument App (Must be after creation to extract headers correctly)
FastAPIInstrumentor.instrument_app(app)

# ✅ 3. Setup Metrics
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

tracer = trace.get_tracer(SERVICE_NAME)
login_attempts = Counter("auth_login_attempts", "Total login attempts", ["auth_method"])
login_success = Counter("auth_login_success", "Successful logins", ["username"])
login_duration = Histogram("auth_login_duration_seconds", "Login validation time", ["status"])

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
    login_attempts.labels(auth_method="password").inc()
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
            login_duration.labels(status="success" if valid else "failure").observe(time.time() - start)

            if valid:
                span.set_attribute("auth.valid", True)
                login_success.labels(username=req.username).inc()
                return {"status": "success"}
            raise HTTPException(status_code=401, detail="Invalid credentials")
        except Exception as e:
            span.record_exception(e)
            raise HTTPException(status_code=500, detail=str(e))