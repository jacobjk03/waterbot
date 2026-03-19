import uvicorn
import uuid
import secrets
import socket
import logging

from typing import Annotated

import mappings.custom_tags as custom_tags

from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException
from fastapi import Request, Form
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware  # ✅ ADDED: CORS middleware import

from managers.memory_manager import MemoryManager
from managers.rag_manager import RAGManager
from sources_verifier import should_show_sources
from managers.pgvector_store import PgVectorStore
from managers.s3_manager import S3Manager

from adapters.claude import ClaudeAdapter
from adapters.bedrock_kb import BedrockKnowledgeBase
from starlette.middleware.sessions import SessionMiddleware
from langdetect import detect, DetectorFactory

import asyncio

from dotenv import load_dotenv

import os
import json
import datetime
import pathlib
import csv
import io
from starlette.middleware.base import BaseHTTPMiddleware

### Postgres
import psycopg2
from psycopg2.extras import execute_values, DictCursor

# Configure logging — write to stdout so CloudWatch can capture logs from ECS containers
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Ensure reproducibility by setting the seed
DetectorFactory.seed = 0

def detect_language(text):
    try:
        language = detect(text)
        logging.info(f"Detected language: {language}")
        return language
    except Exception as e:
        logging.error(f"Language detection failed: {str(e)}")
        return None

def resolve_language(preferred_language: str | None, detected_language: str | None) -> str:
    normalized_preference = (preferred_language or "").lower()
    if normalized_preference in ("en", "es"):
        return normalized_preference
    if detected_language == 'es':
        return 'es'
    return 'en'

def determine_prompt_language(chat_language: str, preferred_language: str | None) -> str:
    normalized_preference = (preferred_language or "").lower()
    if normalized_preference in ("en", "es"):
        return normalized_preference
    return chat_language

# Set the cookie name to match the one configured in the CDK
COOKIE_NAME = "USER_SESSION"  # Changed from WATERBOT
# Optional: set COOKIE_DOMAIN (e.g. ".azwaterbot.org") when frontend and API use different subdomains
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN") or None

class SetCookieMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        print("🔧 SetCookieMiddleware initialized")
    
    async def dispatch(self, request: Request, call_next):
        # Get existing cookie or generate a new UUID for this request
        session_value = request.cookies.get(COOKIE_NAME)
        
        if not session_value:
            session_value = str(uuid.uuid4())
            print(f"🆕 NEW USER - Generated UUID: {session_value}")
        else:
            print(f"🔄 RETURNING USER - Cookie UUID: {session_value}")
        
        # Store in request state - this is unique per request
        request.state.client_cookie_disabled_uuid = session_value
        print(f"✅ Set request.state.client_cookie_disabled_uuid = {session_value}")
        
        response = await call_next(request)
        
        # Determine whether the request was over HTTPS (including when SSL
        # is terminated upstream and signalled via X-Forwarded-Proto).
        is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"

        # Set the application cookie in the response headers. Browsers drop
        # Secure cookies on plain HTTP, so we only set Secure/None when we
        # know the request is HTTPS; otherwise fall back to lax for local/EC2
        # HTTP so the session sticks and sources work.
        cookie_kwargs = {
            "key": COOKIE_NAME,
            "value": session_value,
            "max_age": 7200,  # 2 hours
            "path": "/",
            "httponly": True,
            "secure": is_https,
            "samesite": "none" if is_https else "lax",
        }
        # Only set an explicit domain when it matches the current request host.
        # If we force a mismatched domain (e.g., COOKIE_DOMAIN=.azwaterbot.org
        # while testing on an EC2 public DNS/IP), browsers drop the cookie.
        if COOKIE_DOMAIN:
            req_host = request.url.hostname or ""
            domain_match = req_host.endswith(COOKIE_DOMAIN.lstrip("."))
            if domain_match:
                cookie_kwargs["domain"] = COOKIE_DOMAIN
        response.set_cookie(**cookie_kwargs)
        print(f"🍪 Set cookie {COOKIE_NAME} = {session_value}")
        
        return response

# Take environment variables from .env
load_dotenv(override=True)  

# FastaAPI startup
app = FastAPI()


@app.on_event("startup")
def startup_ensure_db():
    """Ensure messages and rag_chunks tables exist when using PostgreSQL (e.g. Railway/Render without db_init Lambda)."""
    _ensure_messages_table()
    if not AWS_KB_ID:
        _ensure_rag_chunks_table()


# ✅ ADDED: CORS Middleware - MUST be added before other middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://azwaterbot.org",
        "https://djl31v9y2vbwy.cloudfront.net",
        "https://waterbot-2qo4tjmdo-shankerram3s-projects.vercel.app",
        "https://*.vercel.app",  # Allow all Vercel preview deployments
        "http://localhost:5173",  # Vite default port for local development
        "http://localhost:3000",  # Alternative local port
        "http://localhost:8000",  # Local backend for testing
    ],
    allow_credentials=True,  # ✅ CRITICAL: Sets Access-Control-Allow-Credentials: true
    allow_methods=["GET", "POST", "OPTIONS"],  # Allow required HTTP methods
    allow_headers=["Content-Type", "Authorization", "Accept"],  # Allow required headers
    expose_headers=["Set-Cookie"],  # Expose Set-Cookie header to frontend
)

security = HTTPBasic()
# Jinja templates (splash screen, waterbot chat, etc.) - path relative to this file
_templates_dir = pathlib.Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))
# Mount static files from the React frontend build
# Get the path to the frontend dist directory
# In Docker container: /app/frontend/dist (since WORKDIR is /app and main.py is at /app/main.py)
# In local dev: ../frontend/dist (relative to application directory)

# Try multiple possible paths
possible_paths = [
    pathlib.Path(__file__).parent / "frontend" / "dist",  # /app/frontend/dist in container
    pathlib.Path(__file__).parent.parent / "frontend" / "dist",  # ../frontend/dist in local dev
    pathlib.Path("/app") / "frontend" / "dist",  # Explicit container path
]

frontend_dist_path = None
for path in possible_paths:
    resolved_path = path.resolve()  # Convert to absolute path
    assets_check = resolved_path / "assets"
    logging.info(f"Checking path: {resolved_path}, exists: {resolved_path.exists()}, assets exists: {assets_check.exists()}")
    if resolved_path.exists() and assets_check.exists():
        frontend_dist_path = resolved_path
        logging.info(f"Found frontend dist directory at: {frontend_dist_path} (absolute: {frontend_dist_path.resolve()})")
        break

# Only mount assets if the directory exists
if frontend_dist_path:
    assets_path = frontend_dist_path / "assets"
    assets_path_str = str(assets_path.resolve())  # Get absolute path as string
    
    # Double-check the directory exists before mounting
    if not pathlib.Path(assets_path_str).exists():
        logging.error(f"Assets directory does not exist at {assets_path_str}. Cannot mount.")
        frontend_dist_path = None
    else:
        try:
            # Verify it's actually a directory
            if not pathlib.Path(assets_path_str).is_dir():
                logging.error(f"Assets path exists but is not a directory: {assets_path_str}")
                frontend_dist_path = None
            else:
                app.mount("/assets", StaticFiles(directory=assets_path_str), name="assets")
                logging.info(f"Mounted React assets from: {assets_path_str}")
                # Also mount public images copied by Vite from frontend/public/images → frontend/dist/images
                images_path = frontend_dist_path / "images"
                if images_path.exists() and images_path.is_dir():
                    try:
                        app.mount("/images", StaticFiles(directory=str(images_path.resolve())), name="images")
                        logging.info(f"Mounted public images from: {images_path.resolve()}")
                    except Exception as e:
                        logging.error(f"Failed to mount images directory at {images_path.resolve()}: {e}")
        except Exception as e:
            logging.error(f"Failed to mount assets directory at {assets_path_str}: {e}")
            frontend_dist_path = None
else:
    logging.warning(f"Frontend dist directory not found. Tried: {[str(p.resolve()) for p in possible_paths]}. React frontend will not be served.")

app.mount("/static", StaticFiles(directory="static"), name="static")

# Middleware management
secret_key = os.getenv("SESSION_SECRET", secrets.token_urlsafe(32))
app.add_middleware(SetCookieMiddleware)
app.add_middleware(SessionMiddleware, secret_key=secret_key)

TRANSCRIPT_BUCKET_NAME=os.getenv("TRANSCRIPT_BUCKET_NAME")

# adapter choices
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-4-6"
ADAPTERS: dict[str, object] = {
    "claude-default": ClaudeAdapter(CLAUDE_MODEL),
}

# Set adapter choice
llm_adapter = ADAPTERS["claude-default"]

# If AWS_KB_ID is set, we will route RAG to Bedrock KB instead of pgvector
AWS_KB_ID = os.getenv("AWS_KB_ID") or os.getenv("BEDROCK_KB_ID")
AWS_REGION = os.getenv("AWS_REGION") or "us-west-2"
AWS_KB_MODEL_ARN = os.getenv("AWS_KB_MODEL_ARN")

# Manager classes
memory = MemoryManager()  # Assuming you have a MemoryManager class
s3_manager = S3Manager(bucket_name=TRANSCRIPT_BUCKET_NAME)

# Database connection: DATABASE_URL (e.g. Railway) or DB_HOST/DB_USER/DB_PASSWORD/DB_NAME
DATABASE_URL = os.getenv("DATABASE_URL")
db_host = os.getenv("DB_HOST")
db_user = os.getenv("DB_USER")
db_password = os.getenv("DB_PASSWORD")
db_name = os.getenv("DB_NAME")
db_port = os.getenv("DB_PORT", "5432")

POSTGRES_ENABLED = bool(DATABASE_URL) or all([db_host, db_user, db_password, db_name])

DB_PARAMS = {
    "dbname": db_name,
    "user": db_user,
    "password": db_password,
    "host": db_host,
    "port": db_port,
}


def _pg_connect():
    """Return a PostgreSQL connection (for messages table). Use for both psycopg2 paths."""
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return psycopg2.connect(**DB_PARAMS)


def _ensure_messages_table():
    """Create messages table and indexes if they don't exist (e.g. Railway/Render without db_init Lambda)."""
    if not POSTGRES_ENABLED:
        return
    try:
        conn = _pg_connect()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                session_uuid VARCHAR(255) NOT NULL,
                msg_id VARCHAR(255) NOT NULL,
                user_query TEXT NOT NULL,
                response_content TEXT NOT NULL,
                source JSONB,
                chatbot_type VARCHAR(50) DEFAULT 'waterbot',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                reaction SMALLINT,
                user_comment TEXT
            );
        """)
        # Add columns for existing tables that predate the rating feature
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS reaction SMALLINT;")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS user_comment TEXT;")
        for idx in (
            "CREATE INDEX IF NOT EXISTS idx_session_uuid ON messages(session_uuid);",
            "CREATE INDEX IF NOT EXISTS idx_created_at ON messages(created_at);",
            "CREATE INDEX IF NOT EXISTS idx_msg_id ON messages(msg_id);",
            "CREATE INDEX IF NOT EXISTS idx_session_created ON messages(session_uuid, created_at);",
            "CREATE INDEX IF NOT EXISTS idx_chatbot_type ON messages(chatbot_type);",
        ):
            cur.execute(idx)
        cur.close()
        conn.close()
        logging.info("Messages table ready.")
    except Exception as e:
        logging.warning("Could not ensure messages table (non-fatal): %s", e)


def _ensure_rag_chunks_table():
    """Create pgvector extension and rag_chunks table if they don't exist (e.g. local/Railway without db_init Lambda). Uses RAG DB (DATABASE_URL or DB_*)."""
    if not POSTGRES_ENABLED:
        return
    try:
        conn = _pg_connect()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        logging.info("pgvector extension enabled (or already present).")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id TEXT PRIMARY KEY,
                doc_id TEXT,
                chunk_index INT,
                content TEXT NOT NULL,
                embedding vector(1536),
                metadata JSONB,
                content_hash TEXT,
                locale TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            );
        """)
        for idx in (
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc_id ON rag_chunks (doc_id);",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_metadata ON rag_chunks USING GIN (metadata);",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_locale ON rag_chunks (locale);",
        ):
            try:
                cur.execute(idx)
            except Exception as idx_e:
                logging.warning("Could not create RAG index (non-fatal): %s", idx_e)
        try:
            cur.execute("""
                CREATE INDEX IF NOT EXISTS rag_chunks_embedding_idx
                ON rag_chunks USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100);
            """)
        except Exception as ivf_e:
            logging.warning("Could not create ivfflat index (table may be empty): %s", ivf_e)
        cur.close()
        conn.close()
        logging.info("rag_chunks table ready.")
    except Exception as e:
        logging.warning("Could not ensure rag_chunks table (non-fatal): %s", e)


# RAG backend selection: Bedrock KB if configured, else pgvector
def get_vector_store_or_kb():
    if AWS_KB_ID:
        logging.info("Using Bedrock Knowledge Base %s (region=%s)", AWS_KB_ID, AWS_REGION)
        return BedrockKnowledgeBase(kb_id=AWS_KB_ID, model_arn=AWS_KB_MODEL_ARN, region=AWS_REGION)

    if not POSTGRES_ENABLED:
        raise ValueError(
            "RAG requires PostgreSQL: set DATABASE_URL (RAG-only) or DB_HOST, DB_USER, DB_PASSWORD, DB_NAME."
        )
    # Only initialize embeddings if we actually need pgvector.
    embedding_function = llm_adapter.get_embeddings()
    if DATABASE_URL:
        return PgVectorStore(db_url=DATABASE_URL, embedding_function=embedding_function)
    return PgVectorStore(db_params=DB_PARAMS, embedding_function=embedding_function)


# Ensure rag_chunks table exists only when using pgvector
if not AWS_KB_ID:
    _ensure_rag_chunks_table()

try:
    backend = get_vector_store_or_kb()
    knowledge_base = RAGManager(backend) if isinstance(backend, PgVectorStore) else backend
except Exception as e:
    knowledge_base = None
    logging.warning("RAG disabled: %s", e)

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "supersecurepassword"

# Authentication: accepts session cookie OR HTTP Basic Auth (for API clients)
def authenticate(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    admin = request.session.get("admin")
    if admin:
        return admin
    if credentials.username == ADMIN_USERNAME and credentials.password == ADMIN_PASSWORD:
        return credentials.username
    raise HTTPException(status_code=401, detail="Unauthorized")

# Authentication for admin pages: session-only, redirects to login form
def authenticate_admin_page(request: Request):
    admin = request.session.get("admin")
    if admin:
        return admin
    raise HTTPException(status_code=307, headers={"Location": "/admin/login"})

# Secure endpoint
@app.get("/messages")
def get_messages(user: str = Depends(authenticate)):  # Requires authentication
    """Read the messages from the PostgreSQL database"""
    if not POSTGRES_ENABLED:
        return json.dumps([])
    try:
        conn = _pg_connect()
        cursor = conn.cursor(cursor_factory=DictCursor)
        cursor.execute("SELECT * FROM messages ORDER BY created_at DESC LIMIT 100;")
        messages = cursor.fetchall()
        cursor.close()
        conn.close()

        # Convert datetime objects to strings
        def convert_datetime_to_str(obj):
            if isinstance(obj, (datetime.datetime, datetime.date)):
                return obj.isoformat()  # Convert datetime to ISO format string
            return obj

        # Convert each message (dict) to a JSON-serializable format
        serializable_messages = []
        for msg in messages:
            msg_dict = dict(msg)
            serializable_messages.append({k: convert_datetime_to_str(v) for k, v in msg_dict.items()})

        return json.dumps(serializable_messages)
    except Exception as e:
        logging.error("Database Error: %s", e, exc_info=True)
        return json.dumps([])

@app.get("/download-messages")
def download_messages(
    user: str = Depends(authenticate),
    format: str = "csv",
    chatbot_type: str = None,
    start_date: str = None,
    end_date: str = None,
):
    """Download all messages as CSV or JSON file."""
    if not POSTGRES_ENABLED:
        raise HTTPException(status_code=503, detail="Database not configured")

    if format not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'json'")

    try:
        conn = _pg_connect()
        cursor = conn.cursor(cursor_factory=DictCursor)

        query = "SELECT id, session_uuid, msg_id, chatbot_type, user_query, response_content, source, created_at, reaction, user_comment FROM messages"
        conditions = []
        params = []
        if chatbot_type:
            conditions.append("chatbot_type = %s")
            params.append(chatbot_type)
        if start_date:
            conditions.append("created_at >= %s")
            params.append(start_date)
        if end_date:
            conditions.append("created_at <= %s")
            params.append(end_date + " 23:59:59")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"

        cursor.execute(query, params)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        if not rows:
            raise HTTPException(status_code=404, detail="No messages found for the selected filters")

        today = datetime.date.today().isoformat()

        if format == "json":
            def serialize(obj):
                if isinstance(obj, (datetime.datetime, datetime.date)):
                    return obj.isoformat()
                return obj

            data = []
            for row in rows:
                d = dict(row)
                data.append({k: serialize(v) for k, v in d.items()})

            content = json.dumps(data, indent=2)
            return StreamingResponse(
                iter([content]),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="messages_{today}.json"'},
            )

        # CSV format
        output = io.StringIO()
        writer = csv.writer(output)
        columns = ["id", "session_uuid", "msg_id", "chatbot_type", "user_query", "response_content", "source", "created_at", "reaction", "user_comment"]
        writer.writerow(columns)
        for row in rows:
            d = dict(row)
            writer.writerow([
                d.get(col, "") if not isinstance(d.get(col), (datetime.datetime, datetime.date))
                else d[col].isoformat()
                for col in columns
            ])

        output.seek(0)
        return StreamingResponse(
            output,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="messages_{today}.csv"'},
        )
    except Exception as e:
        logging.error("Download error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to export messages")


@app.get("/admin/data", response_class=HTMLResponse)
def admin_data_page(request: Request, user: str = Depends(authenticate_admin_page)):
    return templates.TemplateResponse("admin_data.html", {"request": request})


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, error: int = 0):
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": error})


@app.post("/admin/login")
def admin_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        request.session["admin"] = username
        return RedirectResponse(url="/admin/data", status_code=303)
    return RedirectResponse(url="/admin/login?error=1", status_code=303)


@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


def log_message(session_uuid, msg_id, user_query, response_content, source):
    """Insert a message into the PostgreSQL database. No-op if DB is not configured or connection fails."""
    if not POSTGRES_ENABLED:
        return
    source_json = json.dumps(source)  # Convert source (list/dict) to a JSON string
    msg_id_str = str(msg_id)  # Ensure msg_id is a string
    query = """
        INSERT INTO messages (session_uuid, msg_id, user_query, response_content, source, created_at)
        VALUES (%s, %s, %s, %s, %s, %s);
    """
    args = (session_uuid, msg_id_str, user_query, response_content, source_json, datetime.datetime.utcnow())

    for attempt in range(2):
        try:
            conn = _pg_connect()
            cursor = conn.cursor()
            cursor.execute(query, args)
            conn.commit()
            cursor.close()
            conn.close()
            logging.info("Message logged successfully in PostgreSQL.")
            return
        except psycopg2.OperationalError as e:
            logging.warning("PostgreSQL unavailable (message not logged): %s", e)
            return
        except psycopg2.ProgrammingError as e:
            if getattr(e, "pgcode", None) == "42P01" and attempt == 0:  # undefined_table
                logging.info("Messages table missing, creating it and retrying.")
                _ensure_messages_table()
            else:
                logging.error("Database Error: %s", e, exc_info=True)
                return
        except Exception as e:
            logging.error("Database Error: %s", e, exc_info=True)
            return

def update_rating_pg(session_uuid, msg_id, reaction=None, user_comment=None):
    """Update reaction/user_comment on an existing message row. No-op if DB is not configured."""
    if not POSTGRES_ENABLED:
        return
    sets, vals = [], []
    if reaction is not None:
        sets.append("reaction = %s")
        vals.append(int(reaction))
    if user_comment is not None:
        sets.append("user_comment = %s")
        vals.append(user_comment)
    if not sets:
        return
    vals.extend([session_uuid, str(msg_id)])
    query = f"UPDATE messages SET {', '.join(sets)} WHERE session_uuid = %s AND msg_id = %s;"
    try:
        conn = _pg_connect()
        cur = conn.cursor()
        cur.execute(query, vals)
        conn.commit()
        cur.close()
        conn.close()
        logging.info("Rating updated in PostgreSQL for session=%s msg=%s", session_uuid, msg_id)
    except Exception as e:
        logging.error("Failed to update rating in PostgreSQL: %s", e, exc_info=True)


@app.post("/session-transcript")
async def session_transcript_post(request: Request):
    session_uuid = request.cookies.get(COOKIE_NAME) or request.state.client_cookie_disabled_uuid

    print("=" * 60)
    print(f"📥 TRANSCRIPT REQUEST")
    print(f"Cookie value: {request.cookies.get(COOKIE_NAME)}")
    print(f"State value: {request.state.client_cookie_disabled_uuid}")
    print(f"Final session_uuid: {session_uuid}")
    print(f"All sessions: {list(memory.sessions.keys())}")

    session_history = await memory.get_session_history_all(session_uuid)
    print(f"This session has {len(session_history)} messages")

    if session_history:
        print(f"First message: {session_history[0] if session_history else 'None'}")
        print(f"Last message: {session_history[-1] if session_history else 'None'}")
    print("=" * 60)

    if not session_history or not isinstance(session_history, list):
        return {"message": "No chat history found for this session."}

    filename = f"{session_uuid}_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"
    session_text = ""
    for entry in session_history:
        if isinstance(entry, dict) and "role" in entry and "content" in entry:
            session_text += f"Role: {entry['role']}\nContent: {entry['content']}\n\n"

    if TRANSCRIPT_BUCKET_NAME:
        object_key = f"session-transcript/{filename}"
        await s3_manager.upload(key=object_key, body=session_text)
        url = await s3_manager.generate_presigned(key=object_key)
        return {"presigned_url": url}
    # No S3 bucket configured: return transcript inline so frontend can still trigger download
    return {"presigned_url": None, "transcript": session_text, "filename": filename}

@app.post('/submit_rating_api')
async def submit_rating_api_post(
        request: Request,
        message_id: str = Form(..., description="The ID of the message"),
        reaction: str = Form(None, description="Optional reaction to the message"),
        userComment: str = Form(None, description="Optional user comment")
    ):
    try:
        session_uuid = request.cookies.get(COOKIE_NAME) or request.state.client_cookie_disabled_uuid
        counter_uuid = await memory.get_message_count_uuid(session_uuid)
        msg_id = counter_uuid + "." + message_id
        update_rating_pg(session_uuid, msg_id, reaction=reaction, user_comment=userComment)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating rating: {str(e)}")

# Max texts and chars for translate to avoid timeouts/cost
TRANSLATE_MAX_TEXTS = 20
TRANSLATE_MAX_TOTAL_CHARS = 50_000


async def _translate_one(text: str, target_lang: str) -> str:
    """Translate a single text via LLM. Returns translated string."""
    lang_name = "Spanish" if target_lang == "es" else "English"
    system = (
        "You are a translator. Translate the following to "
        + lang_name
        + ". Output only the translation, no preamble or explanation. "
        "Preserve line breaks and HTML-like tags (e.g. <br>) as in the original."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]
    llm_body = json.dumps({"messages": messages, "temperature": 0.3})
    out = await llm_adapter.generate_response(llm_body=llm_body)
    return out or ""


@app.post("/translate")
async def translate_post(request: Request):
    """Translate a list of texts to the target language. Used when user switches UI language."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    texts = body.get("texts")
    target_lang = body.get("target_lang")
    if not isinstance(texts, list) or not target_lang or target_lang not in ("es", "en"):
        raise HTTPException(
            status_code=400,
            detail="Body must include 'texts' (array of strings) and 'target_lang' ('es' or 'en').",
        )
    if len(texts) > TRANSLATE_MAX_TEXTS:
        raise HTTPException(
            status_code=400,
            detail=f"At most {TRANSLATE_MAX_TEXTS} texts allowed.",
        )
    total_chars = sum(len(t) for t in texts if isinstance(t, str))
    if total_chars > TRANSLATE_MAX_TOTAL_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Total length of texts exceeds {TRANSLATE_MAX_TOTAL_CHARS} characters.",
        )
    # Filter to non-empty strings and translate in parallel
    to_translate = [t for t in texts if isinstance(t, str) and t.strip()]
    if not to_translate:
        return {"translations": list(texts)}
    try:
        translations = await asyncio.gather(
            *[_translate_one(t, target_lang) for t in to_translate]
        )
    except Exception as e:
        logging.warning("Translate LLM failed: %s", e)
        raise HTTPException(status_code=503, detail="Translation service unavailable.")
    # Map back: preserve order; empty/non-string slots get original
    out = []
    idx = 0
    for t in texts:
        if isinstance(t, str) and t.strip():
            out.append(translations[idx] if idx < len(translations) else t)
            idx += 1
        else:
            out.append(t if isinstance(t, str) else "")
    return {"translations": out}


NO_SOURCES_MESSAGE = {"en": "Sources are not available for this reply.", "es": "Las fuentes no están disponibles para esta respuesta."}


@app.post('/riverbot_chat_sources_api')
async def riverbot_chat_sources_post(request: Request, background_tasks:BackgroundTasks):
    session_uuid = request.cookies.get(COOKIE_NAME) or request.state.client_cookie_disabled_uuid
    docs=await memory.get_latest_memory( session_id=session_uuid, read="documents")
    user_query=await memory.get_latest_memory( session_id=session_uuid, read="content")
    sources=await memory.get_latest_memory( session_id=session_uuid, read="sources")
    user_question = await memory.get_latest_memory(session_id=session_uuid, read="content", travel=-2)
    bot_response = await memory.get_latest_memory(session_id=session_uuid, read="content", travel=-1)
    show_sources = await should_show_sources(user_question or "", bot_response or "", sources or [])
    if not show_sources:
        lang = "es" if detect_language(user_query or user_question or "") == "es" else "en"
        await memory.increment_message_count(session_uuid)
        return {"resp": NO_SOURCES_MESSAGE[lang], "msgID": await memory.get_message_count(session_uuid)}
    language = detect_language(user_query)

    memory_payload={
        "documents":docs,
        "sources":sources
    }
    
    formatted_source_list=await memory.format_sources_as_html(source_list=sources)
    instruction_text = "Proporcióname las fuentes." if language == 'es' else "Provide me sources."
    generated_user_query = f'{custom_tags.tags["SOURCE_REQUEST"][0]}{instruction_text}{custom_tags.tags["SOURCE_REQUEST"][1]}'
    generated_user_query += f'{custom_tags.tags["OG_QUERY"][0]}{user_query}{custom_tags.tags["OG_QUERY"][1]}'
    bot_response=formatted_source_list

    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"user","content":generated_user_query},
        source_list=memory_payload
    )
    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"assistant","content":bot_response},
        source_list=memory_payload
    )
    await memory.increment_message_count(session_uuid)

    background_tasks.add_task(log_message,
        session_uuid=session_uuid,
        msg_id=await memory.get_message_count_uuid_combo(session_uuid), 
        user_query=generated_user_query, 
        response_content=bot_response,
        source=[] 
    )

    return {
        "resp":bot_response,
        "msgID": await memory.get_message_count(session_uuid)
    }

@app.post('/chat_sources_api')
async def chat_sources_post(
    request: Request,
    background_tasks:BackgroundTasks,
    language_preference: Annotated[str | None, Form()] = None
):
    session_uuid = request.cookies.get(COOKIE_NAME) or request.state.client_cookie_disabled_uuid
    docs=await memory.get_latest_memory( session_id=session_uuid, read="documents")
    user_query=await memory.get_latest_memory( session_id=session_uuid, read="content")
    sources=await memory.get_latest_memory( session_id=session_uuid, read="sources")
    user_question = await memory.get_latest_memory(session_id=session_uuid, read="content", travel=-2)
    bot_response_content = await memory.get_latest_memory(session_id=session_uuid, read="content", travel=-1)
    show_sources = await should_show_sources(user_question or "", bot_response_content or "", sources or [])
    if not show_sources:
        detected_language = detect_language(user_query or user_question or "")
        language = resolve_language(language_preference, detected_language)
        lang = "es" if language == "es" else "en"
        await memory.increment_message_count(session_uuid)
        return {"resp": NO_SOURCES_MESSAGE[lang], "msgID": await memory.get_message_count(session_uuid)}
    detected_language = detect_language(user_query)
    language = resolve_language(language_preference, detected_language)
    response_language = determine_prompt_language(language, language_preference)
    logging.info(
        "[LANG][chat_api] preference=%s detected=%s kb_language=%s prompt_language=%s",
        language_preference,
        detected_language,
        language,
        response_language
    )
    logging.info(
        "[LANG][chat_detailed_api] preference=%s detected=%s kb_language=%s prompt_language=%s",
        language_preference,
        detected_language,
        language,
        response_language
    )
    logging.info(
        "[LANG][chat_actionItems_api] preference=%s detected=%s kb_language=%s prompt_language=%s",
        language_preference,
        detected_language,
        language,
        response_language
    )
    logging.info(
        "[LANG][chat_sources_api] preference=%s detected=%s kb_language=%s prompt_language=%s",
        language_preference,
        detected_language,
        language,
        response_language
    )

    memory_payload={
        "documents":docs,
        "sources":sources
    }
    
    formatted_source_list=await memory.format_sources_as_html(source_list=sources)
    instruction_text = "Proporcióname las fuentes." if language == 'es' else "Provide me sources."
    generated_user_query = f'{custom_tags.tags["SOURCE_REQUEST"][0]}{instruction_text}{custom_tags.tags["SOURCE_REQUEST"][1]}'
    generated_user_query += f'{custom_tags.tags["OG_QUERY"][0]}{user_query}{custom_tags.tags["OG_QUERY"][1]}'
    bot_response=formatted_source_list

    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"user","content":generated_user_query},
        source_list=memory_payload
    )
    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"assistant","content":bot_response},
        source_list=memory_payload
    )
    await memory.increment_message_count(session_uuid)

    background_tasks.add_task(log_message,
        session_uuid=session_uuid,
        msg_id=await memory.get_message_count_uuid_combo(session_uuid), 
        user_query=generated_user_query, 
        response_content=bot_response,
        source=[] 
    )

    return {
        "resp":bot_response,
        "msgID": await memory.get_message_count(session_uuid)
    }

@app.post('/riverbot_chat_actionItems_api')
async def riverbot_chat_action_items_api_post(request: Request, background_tasks:BackgroundTasks):
    session_uuid = request.cookies.get(COOKIE_NAME) or request.state.client_cookie_disabled_uuid
    docs=await memory.get_latest_memory( session_id=session_uuid, read="documents")
    sources=await memory.get_latest_memory( session_id=session_uuid, read="sources")

    memory_payload={
        "documents":docs,
        "sources":sources
    }

    user_query=await memory.get_latest_memory( session_id=session_uuid, read="content",travel=-2)
    bot_response=await memory.get_latest_memory( session_id=session_uuid, read="content")
    
    language = detect_language(user_query)

    if not knowledge_base:
        raise HTTPException(503, "RAG is not available. Configure PostgreSQL with pgvector.")
    doc_content_str = await knowledge_base.knowledge_to_string({"documents": docs})

    llm_body=await llm_adapter.get_llm_nextsteps_body( kb_data=doc_content_str,user_query=user_query,bot_response=bot_response )
    response_content = await llm_adapter.generate_response(llm_body=llm_body)

    generated_user_query = f'{custom_tags.tags["NEXTSTEPS_REQUEST"][0]}Provide me the action items{custom_tags.tags["NEXTSTEPS_REQUEST"][1]}'
    generated_user_query += f'{custom_tags.tags["OG_QUERY"][0]}{user_query}{custom_tags.tags["OG_QUERY"][1]}'

    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"user","content":generated_user_query},
        source_list=[]
    )
    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"assistant","content":response_content},
        source_list=memory_payload
    )
    await memory.increment_message_count(session_uuid)

    background_tasks.add_task(log_message,
        session_uuid=session_uuid,
        msg_id=await memory.get_message_count_uuid_combo(session_uuid),
        user_query=generated_user_query,
        response_content=response_content,
        source=sources
    )

    return {
        "resp":response_content,
        "msgID": await memory.get_message_count(session_uuid)
    }

@app.post('/chat_actionItems_api')
async def chat_action_items_api_post(
    request: Request,
    background_tasks:BackgroundTasks,
    language_preference: Annotated[str | None, Form()] = None
):
    session_uuid = request.cookies.get(COOKIE_NAME) or request.state.client_cookie_disabled_uuid
    docs=await memory.get_latest_memory( session_id=session_uuid, read="documents")
    sources=await memory.get_latest_memory( session_id=session_uuid, read="sources")

    memory_payload={
        "documents":docs,
        "sources":sources
    }

    user_query=await memory.get_latest_memory( session_id=session_uuid, read="content",travel=-2)
    bot_response=await memory.get_latest_memory( session_id=session_uuid, read="content")
    
    detected_language = detect_language(user_query)
    language = resolve_language(language_preference, detected_language)
    
    response_language = determine_prompt_language(language, language_preference)

    if not knowledge_base:
        raise HTTPException(503, "RAG is not available. Configure PostgreSQL with pgvector.")
    doc_content_str = await knowledge_base.knowledge_to_string({"documents": docs})

    llm_body=await llm_adapter.get_llm_nextsteps_body(
        kb_data=doc_content_str,
        user_query=user_query,
        bot_response=bot_response,
        language=response_language
    )
    response_content = await llm_adapter.generate_response(llm_body=llm_body)

    instruction_text = "Proporcióname los pasos a seguir" if response_language == 'es' else "Provide me the action items"
    generated_user_query = f'{custom_tags.tags["NEXTSTEPS_REQUEST"][0]}{instruction_text}{custom_tags.tags["NEXTSTEPS_REQUEST"][1]}'
    generated_user_query += f'{custom_tags.tags["OG_QUERY"][0]}{user_query}{custom_tags.tags["OG_QUERY"][1]}'

    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"user","content":generated_user_query},
        source_list=[]
    )
    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"assistant","content":response_content},
        source_list=memory_payload
    )
    await memory.increment_message_count(session_uuid)

    background_tasks.add_task(log_message,
        session_uuid=session_uuid,
        msg_id=await memory.get_message_count_uuid_combo(session_uuid),
        user_query=generated_user_query,
        response_content=response_content,
        source=sources
    )

    return {
        "resp":response_content,
        "msgID": await memory.get_message_count(session_uuid)
    }

@app.post('/riverbot_chat_detailed_api')
async def riverbot_chat_detailed_api_post(request: Request, background_tasks:BackgroundTasks):
    session_uuid = request.cookies.get(COOKIE_NAME) or request.state.client_cookie_disabled_uuid
    docs=await memory.get_latest_memory( session_id=session_uuid, read="documents")
    sources=await memory.get_latest_memory( session_id=session_uuid, read="sources")

    memory_payload={
        "documents":docs,
        "sources":sources
    }

    user_query=await memory.get_latest_memory( session_id=session_uuid, read="content",travel=-2)
    bot_response=await memory.get_latest_memory( session_id=session_uuid, read="content")
    
    language = detect_language(user_query)

    if not knowledge_base:
        raise HTTPException(503, "RAG is not available. Configure PostgreSQL with pgvector.")
    doc_content_str = await knowledge_base.knowledge_to_string({"documents": docs})

    llm_body=await llm_adapter.get_llm_detailed_body( kb_data=doc_content_str,user_query=user_query,bot_response=bot_response )
    response_content = await llm_adapter.generate_response(llm_body=llm_body)

    generated_user_query = f'{custom_tags.tags["MOREDETAIL_REQUEST"][0]}Provide me a more detailed response.{custom_tags.tags["MOREDETAIL_REQUEST"][1]}'
    generated_user_query += f'{custom_tags.tags["OG_QUERY"][0]}{user_query}{custom_tags.tags["OG_QUERY"][1]}'

    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"user","content":generated_user_query},
        source_list=[]
    )
    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"assistant","content":response_content},
        source_list=memory_payload
    )
    await memory.increment_message_count(session_uuid)

    background_tasks.add_task(log_message,
        session_uuid=session_uuid,
        msg_id=await memory.get_message_count_uuid_combo(session_uuid), 
        user_query=generated_user_query, 
        response_content=response_content,
        source=sources
    )

    return {
        "resp":response_content,
        "msgID": await memory.get_message_count(session_uuid)
    }

@app.post('/chat_detailed_api')
async def chat_detailed_api_post(
    request: Request,
    background_tasks:BackgroundTasks,
    language_preference: Annotated[str | None, Form()] = None
):
    session_uuid = request.cookies.get(COOKIE_NAME) or request.state.client_cookie_disabled_uuid
    docs=await memory.get_latest_memory( session_id=session_uuid, read="documents")
    sources=await memory.get_latest_memory( session_id=session_uuid, read="sources")

    memory_payload={
        "documents":docs,
        "sources":sources
    }

    user_query=await memory.get_latest_memory( session_id=session_uuid, read="content",travel=-2)
    bot_response=await memory.get_latest_memory( session_id=session_uuid, read="content")
    
    detected_language = detect_language(user_query)
    language = resolve_language(language_preference, detected_language)

    response_language = determine_prompt_language(language, language_preference)

    if not knowledge_base:
        raise HTTPException(503, "RAG is not available. Configure PostgreSQL with pgvector.")
    doc_content_str = await knowledge_base.knowledge_to_string({"documents": docs})

    llm_body=await llm_adapter.get_llm_detailed_body(
        kb_data=doc_content_str,
        user_query=user_query,
        bot_response=bot_response,
        language=response_language
    )
    response_content = await llm_adapter.generate_response(llm_body=llm_body)

    detail_instruction = "Dame una respuesta más detallada." if response_language == 'es' else "Provide me a more detailed response."
    generated_user_query = f'{custom_tags.tags["MOREDETAIL_REQUEST"][0]}{detail_instruction}{custom_tags.tags["MOREDETAIL_REQUEST"][1]}'
    generated_user_query += f'{custom_tags.tags["OG_QUERY"][0]}{user_query}{custom_tags.tags["OG_QUERY"][1]}'

    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"user","content":generated_user_query},
        source_list=[]
    )
    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"assistant","content":response_content},
        source_list=memory_payload
    )
    await memory.increment_message_count(session_uuid)

    background_tasks.add_task(log_message,
        session_uuid=session_uuid,
        msg_id=await memory.get_message_count_uuid_combo(session_uuid), 
        user_query=generated_user_query, 
        response_content=response_content,
        source=sources
    )

    return {
        "resp":response_content,
        "msgID": await memory.get_message_count(session_uuid)
    }

@app.post('/chat_api')
async def chat_api_post(
    request: Request,
    user_query: Annotated[str, Form()],
    background_tasks: BackgroundTasks,
    language_preference: Annotated[str | None, Form()] = None
):
    user_query=user_query
    session_uuid = request.cookies.get(COOKIE_NAME) or request.state.client_cookie_disabled_uuid

    print("=" * 60)
    print(f"📨 CHAT REQUEST RECEIVED")
    print(f"User query: {user_query[:50]}...")
    print(f"Cookie value: {request.cookies.get(COOKIE_NAME)}")
    print(f"State value: {request.state.client_cookie_disabled_uuid}")
    print(f"Final session_uuid: {session_uuid}")
    print(f"Current sessions in memory: {list(memory.sessions.keys())}")
    print("=" * 60)

    await memory.create_session(session_uuid)
        
    moderation_result,intent_result = await llm_adapter.safety_checks(user_query)

    user_intent=1
    prompt_injection=1
    unrelated_topic=1
    not_handled="I am sorry, your request cannot be handled."
    data = {}
    try:
        data = json.loads(intent_result)
        user_intent=data["user_intent"]
        prompt_injection=data["prompt_injection"]
        unrelated_topic=data["unrelated_topic"]
    except Exception as e:
        print(intent_result)
        print("ERROR", str(e))

    if( moderation_result or (prompt_injection or unrelated_topic)):
        response_content= "I am sorry, your request is inappropriate and I cannot answer it." if moderation_result else not_handled

        await memory.increment_message_count(session_uuid)

        generated_user_query = f'{custom_tags.tags["SECURITY_CHECK"][0]}{data}{custom_tags.tags["SECURITY_CHECK"][1]}'
        generated_user_query += f'{custom_tags.tags["OG_QUERY"][0]}{user_query}{custom_tags.tags["OG_QUERY"][1]}'

        background_tasks.add_task(log_message,
            session_uuid=session_uuid,
            msg_id=await memory.get_message_count_uuid_combo(session_uuid),
            user_query=generated_user_query,
            response_content=response_content,
            source=[]
        )

        return {
            "resp":response_content,
            "msgID": await memory.get_message_count(session_uuid)
        }

    await memory.add_message_to_session(
        session_id=session_uuid,
        message={"role":"user","content":user_query},
        source_list=[]
    )

    detected_language = detect_language(user_query)
    language = resolve_language(language_preference, detected_language)
    response_language = determine_prompt_language(language, language_preference)

    if not knowledge_base:
        raise HTTPException(503, "RAG is not available. Configure PostgreSQL (DATABASE_URL or DB_*) with pgvector.")
    docs = await knowledge_base.ann_search(user_query, locale=language)
    doc_content_str = await knowledge_base.knowledge_to_string(docs)
    logging.info(f"🔍 RAG Search ({language}): Found {len(docs.get('documents', []))} documents, {len(docs.get('sources', []))} sources")
    
    if docs.get('sources'):
        logging.info(f"📚 Sources: {[s.get('filename', 'unknown') for s in docs['sources']]}")
    else:
        logging.warning("⚠️  No sources found in RAG search - vector store may be empty")
    
    logging.info(f"📄 Knowledge base content length: {len(doc_content_str)} characters")
    
    llm_body = await llm_adapter.get_llm_body( 
        chat_history=await memory.get_session_history_all(session_uuid), 
        kb_data=doc_content_str,
        temperature=.5,
        max_tokens=500,
        endpoint_type="spanish" if response_language == 'es' else "default" )

    response_content = await llm_adapter.generate_response(llm_body=llm_body)

    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"assistant","content":response_content},
        source_list=docs
    )

    await memory.increment_message_count(session_uuid)
    background_tasks.add_task(log_message,
        session_uuid=session_uuid,
        msg_id=await memory.get_message_count_uuid_combo(session_uuid), 
        user_query=user_query, 
        response_content=response_content,
        source=docs["sources"]
    )

    return {
        "resp": response_content.replace('\n\n', '</p><p>').replace('\n', '<br>'),
        "msgID": await memory.get_message_count(session_uuid)
    }

@app.post('/riverbot_chat_api')
async def riverbot_chat_api_post(request: Request, user_query: Annotated[str, Form()], background_tasks:BackgroundTasks ):
    user_query=user_query
    session_uuid = request.cookies.get(COOKIE_NAME) or request.state.client_cookie_disabled_uuid

    await memory.create_session(session_uuid)
        
    moderation_result,intent_result = await llm_adapter.safety_checks(user_query)

    user_intent=1
    prompt_injection=1
    unrelated_topic=1
    not_handled="I am sorry, your request cannot be handled."
    data = {}
    try:
        data = json.loads(intent_result)
        user_intent=data["user_intent"]
        prompt_injection=data["prompt_injection"]
        unrelated_topic=data["unrelated_topic"]
    except Exception as e:
        print(intent_result)
        print("ERROR", str(e))

    if( moderation_result or (prompt_injection or unrelated_topic)):
        response_content= "I am sorry, your request is inappropriate and I cannot answer it." if moderation_result else not_handled

        await memory.increment_message_count(session_uuid)

        generated_user_query = f'{custom_tags.tags["SECURITY_CHECK"][0]}{data}{custom_tags.tags["SECURITY_CHECK"][1]}'
        generated_user_query += f'{custom_tags.tags["OG_QUERY"][0]}{user_query}{custom_tags.tags["OG_QUERY"][1]}'

        background_tasks.add_task(log_message,
            session_uuid=session_uuid,
            msg_id=await memory.get_message_count_uuid_combo(session_uuid),
            user_query=generated_user_query,
            response_content=response_content,
            source=[]
        )

        return {
            "resp":response_content,
            "msgID": await memory.get_message_count(session_uuid)
        }

    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"user","content":user_query},
        source_list=[]
    )
    
    language = detect_language(user_query)

    if not knowledge_base:
        raise HTTPException(503, "RAG is not available. Configure PostgreSQL (DATABASE_URL or DB_*) with pgvector.")
    docs = await knowledge_base.ann_search(user_query, locale=language)
    doc_content_str = await knowledge_base.knowledge_to_string(docs)
    logging.info(f"🔍 RAG Search ({language}): Found {len(docs.get('documents', []))} documents, {len(docs.get('sources', []))} sources")
    
    if docs.get('sources'):
        logging.info(f"📚 Sources: {[s.get('filename', 'unknown') for s in docs['sources']]}")
    else:
        logging.warning("⚠️  No sources found in RAG search - vector store may be empty")
    
    logging.info(f"📄 Knowledge base content length: {len(doc_content_str)} characters")
    
    logging.info("Using riverbot system prompt")
    
    llm_body = await llm_adapter.get_llm_body( 
        chat_history=await memory.get_session_history_all(session_uuid), 
        kb_data=doc_content_str,
        temperature=.5,
        max_tokens=500,
        endpoint_type="riverbot" )

    response_content = await llm_adapter.generate_response(llm_body=llm_body)

    await memory.add_message_to_session( 
        session_id=session_uuid, 
        message={"role":"assistant","content":response_content},
        source_list=docs
    )

    await memory.increment_message_count(session_uuid)
    background_tasks.add_task(log_message,
        session_uuid=session_uuid,
        msg_id=await memory.get_message_count_uuid_combo(session_uuid), 
        user_query=user_query, 
        response_content=response_content,
        source=docs["sources"]
    )

    return {
        "resp": response_content.replace('\n\n', '</p><p>').replace('\n', '<br>'),
        "msgID": await memory.get_message_count(session_uuid)
    }

# Serve React frontend static files (favicons)
@app.get("/favicon.ico")
async def favicon():
    if not frontend_dist_path:
        raise HTTPException(status_code=404)
    favicon_path = frontend_dist_path / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(str(favicon_path))
    raise HTTPException(status_code=404)

@app.get("/favicon-196x196.png")
async def favicon_png():
    if not frontend_dist_path:
        raise HTTPException(status_code=404)
    favicon_path = frontend_dist_path / "favicon-196x196.png"
    if favicon_path.exists():
        return FileResponse(str(favicon_path))
    raise HTTPException(status_code=404)

# Root and /waterbot: serve Jinja templates (splash screen, chat UI)
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Serve the splash screen (Jinja template)"""
    return templates.TemplateResponse("splashScreen.html", {"request": request})

@app.get("/waterbot", response_class=HTMLResponse)
async def waterbot(request: Request):
    """Serve the waterbot chat page (Jinja template)"""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/aboutwaterbot", response_class=HTMLResponse)
async def about_waterbot(request: Request):
    """Serve the about Waterbot page (Jinja template)"""
    return templates.TemplateResponse("aboutWaterbot.html", {"request": request})

@app.get("/Spanish_Translation_2.0.1.html", response_class=HTMLResponse)
async def spanish_translation(request: Request):
    """Serve the Spanish translation page (Jinja template)"""
    return templates.TemplateResponse("spanish.html", {"request": request})

@app.get("/riverbot", response_class=HTMLResponse)
async def riverbot_page(request: Request):
    """Serve the riverbot page (Jinja template)"""
    return templates.TemplateResponse("riverbot.html", {"request": request})

# React SPA at /museum
@app.get("/museum", response_class=HTMLResponse)
@app.get("/museum/", response_class=HTMLResponse)
async def museum_root():
    """Serve the React app's index.html for /museum"""
    if not frontend_dist_path:
        raise HTTPException(status_code=404, detail="React frontend not found. Please build the frontend first.")
    index_path = frontend_dist_path / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    raise HTTPException(status_code=404, detail="React frontend not found. Please build the frontend first.")

# Serve React frontend - catch-all for /museum SPA routing (e.g. /museum/foo)
# This must be defined AFTER all API routes
@app.get("/museum/{full_path:path}", response_class=HTMLResponse)
async def serve_react_app(full_path: str, request: Request):
    """
    Catch-all for React SPA at /museum (e.g. /museum/foo for client-side routing).
    Serves index.html for all /museum/* paths except API-like paths.
    """
    # Skip empty path (root is handled by root() function)
    if not full_path or full_path == "":
        raise HTTPException(status_code=404, detail="Not found")
    
    # Check if this is an API route - if so, let it 404 (should have been handled by specific routes)
    # This is a safety check in case the route wasn't defined above
    if (full_path.startswith("chat_") or 
        full_path.startswith("riverbot_chat_") or
        full_path.startswith("submit_rating") or
        full_path.startswith("session-transcript") or
        full_path.startswith("transcribe") or
        full_path.startswith("messages") or
        full_path.startswith("static/") or
        full_path.startswith("assets/")):
        raise HTTPException(status_code=404, detail="Not found")
    
    # Serve React app's index.html for all other routes (SPA routing)
    if not frontend_dist_path:
        raise HTTPException(status_code=404, detail="React frontend not found. Please build the frontend first.")
    index_path = frontend_dist_path / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    else:
        raise HTTPException(status_code=404, detail="React frontend not found. Please build the frontend first.")

if __name__ == "__main__":
    uvicorn.run(app, host='0.0.0.0', port=8000)
