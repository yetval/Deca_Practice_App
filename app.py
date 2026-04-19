import io
import json
import os
import re
import random
import tempfile
import uuid
import shutil
import concurrent.futures
import time
import sqlite3
import copy
import secrets
from pathlib import Path

from typing import Dict, List, Any, Optional, IO

from flask import Flask, jsonify, render_template, request, abort, redirect, url_for, session
from pypdf import PdfReader
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

# --- Logging Configuration ---
import logging
import sys

# Suppress default Flask/Werkzeug access logs (e.g. "GET / ... 200")
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Configure app logger to output to Console (StreamHandler)
# This ensures logs are visible in the platform dashboard and avoids filesystem issues
logging.basicConfig(
    level=logging.WARNING, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO) # Capture INFO for activity tracking (New User, Uploads, etc.)

BASE_DIR = Path(__file__).parent.resolve()
TESTS_DIR = BASE_DIR / "tests"
INSTANCE_DIR = BASE_DIR / "instance"
SESSION_DATA_DIR = INSTANCE_DIR / "sessions"

try:
    TESTS_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    logger.warning("Could not create TESTS_DIR. Uploads might fail if not using /tmp.")

try:
    SESSION_DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    
    logger.warning("Read-only filesystem detected. Using /tmp for sessions.")
    SESSION_DATA_DIR = Path(tempfile.gettempdir()) / "deca_app_sessions"
    SESSION_DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_QUESTIONS_PER_RUN = int(os.getenv("MAX_QUESTIONS_PER_RUN", "100"))
MAX_TIME_LIMIT_MINUTES = int(os.getenv("MAX_TIME_LIMIT_MINUTES", "1440"))
DEFAULT_RANDOM_ORDER = os.getenv("DEFAULT_RANDOM_ORDER", "false").lower() in {"1", "true", "yes", "on"}
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", "12582912"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "150"))
PDF_PAGE_TIMEOUT_SECONDS = int(os.getenv("PDF_PAGE_TIMEOUT_SECONDS", "8"))
TRUSTED_PROXY_HOPS = max(int(os.getenv("TRUSTED_PROXY_HOPS", "0")), 0)
SECRET_KEY = os.getenv("SECRET_KEY")
ENVIRONMENT = os.getenv("ENVIRONMENT", "").strip().lower()
IS_PRODUCTION = ENVIRONMENT == "production"
if not SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError("SECRET_KEY must be set in production.")
    SECRET_KEY = secrets.token_hex(32)
    logger.warning("SECRET_KEY not set. Generated ephemeral development key.")
SESSION_CLEANUP_AGE_SECONDS = 86400




# Ensure DB is in a writable location
DB_PATH = SESSION_DATA_DIR / "sessions.db"

def _init_db():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT, updated_at REAL)")
            conn.execute("CREATE TABLE IF NOT EXISTS active_users (ip TEXT PRIMARY KEY, ua TEXT, last_seen REAL)")
            conn.commit()
        logger.info(f"Database initialized at {DB_PATH}")
    except Exception as e:
        logger.critical(f"FATAL: Database initialization failed: {e}")
        logger.critical(f"Database path: {DB_PATH}")
        logger.critical("Application cannot continue without database.")
        import sys
        sys.exit(1)

_init_db()  

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = SECRET_KEY
app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    SESSION_TYPE="filesystem",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    PERMANENT_SESSION_LIFETIME=int(os.getenv("PERMANENT_SESSION_LIFETIME_SECONDS", "259200")),
)
if TRUSTED_PROXY_HOPS > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXY_HOPS, x_proto=TRUSTED_PROXY_HOPS, x_host=TRUSTED_PROXY_HOPS)



# -----------------------------

import threading
def _background_cleanup():
    """Run cleanup periodically in background"""
    import time
    while True:
        time.sleep(3600)
        try:
            _cleanup_old_sessions()
        except Exception as e:
            logger.error(f"Background cleanup error: {e}")

cleanup_thread = threading.Thread(target=_background_cleanup, daemon=True)
cleanup_thread.start()


# Words that commonly appear smashed onto the end of a previous word in PDF extraction.
# Used by _normalize_whitespace to surgically split run-ons without breaking valid words.
_RUNON_SPLIT_WORDS = {
    'The', 'This', 'That', 'These', 'Those', 'Then', 'There', 'Their', 'They', 'Them',
    'When', 'Where', 'Which', 'While', 'What', 'Who', 'Why', 'How',
    'However', 'Therefore', 'Because', 'Although', 'Since', 'Before', 'After',
    'For', 'From', 'With', 'About', 'Into', 'Over', 'Under', 'Between',
    'And', 'But', 'Not', 'Also', 'Each', 'Every', 'Most', 'Some', 'All',
    'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten',
    'SOURCE', 'Rationale', 'Answer', 'Note',
    'An', 'As', 'At', 'Be', 'By', 'Do', 'Go', 'He', 'If', 'In', 'Is', 'It',
    'Me', 'My', 'No', 'Of', 'On', 'Or', 'So', 'To', 'Up', 'Us', 'We',
    'Are', 'Can', 'Did', 'Has', 'Had', 'His', 'Her', 'Its', 'May', 'Our', 'Own',
    'Any', 'New', 'Old', 'Per', 'Use', 'Was', 'Way', 'Set',
    'Should', 'Would', 'Could', 'Must', 'Will', 'Shall', 'Does', 'Have',
}
# Build a regex alternation sorted longest-first so greedier words match first
_RUNON_ALTS = '|'.join(sorted(_RUNON_SPLIT_WORDS, key=len, reverse=True))
_RUNON_RE = re.compile(r'([a-z])(' + _RUNON_ALTS + r')(?=[^a-z]|$)')

def _normalize_whitespace(text: str) -> str:
    if not isinstance(text, str):
        return ""

    # Split only at TRUE run-on word boundaries (e.g. "companyThe" -> "company The")
    # instead of blindly splitting every lowercase-uppercase transition.
    text = _RUNON_RE.sub(r'\1 \2', text)

    # Fix specific common broken words
    text = text.replace("SOURC E", "SOURCE")
    text = re.sub(r"\b(SOURC)\s+(E)\b", "SOURCE", text)

    return re.sub(r"\s+", " ", text).strip()

def _strip_leading_number(text: str) -> str:
    return re.sub(r"^\s*(?:\d{1,3}[).:\-]|[A-E][).:\-])\s*", "", text).strip()

def _get_client_ip():
    """Get client IP while only trusting forwarding headers via ProxyFix."""
    if not request: return "0.0.0.0"
    return request.remote_addr or "0.0.0.0"

def _safe_log_value(value: Any) -> str:
    text = str(value if value is not None else "")
    return re.sub(r"[\r\n\t]+", " ", text)[:512]

def _get_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token

def _require_csrf():
    token = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token")
    if not expected or not token or token != expected:
        abort(403, "CSRF validation failed")
    origin = request.headers.get("Origin")
    if origin:
        trusted_origin = f"{request.scheme}://{request.host}"
        if origin != trusted_origin:
            abort(403, "Invalid request origin")

@app.before_request
def track_active_user():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.path.startswith("/api/"):
        _require_csrf()
    try:
        # Use valid IP extraction
        ip = _get_client_ip()
            
        if ip:
            ua = request.headers.get("User-Agent", "Unknown")
            now = time.time()
            with sqlite3.connect(DB_PATH) as conn:
                # Check if new user
                cursor = conn.execute("SELECT last_seen FROM active_users WHERE ip = ?", (ip,))
                row = cursor.fetchone()
                if not row:
                    logger.info(f"NEW USER ARRIVED: {_safe_log_value(ip)} | UA: {_safe_log_value(ua)}")
                
                conn.execute("INSERT OR REPLACE INTO active_users (ip, ua, last_seen) VALUES (?, ?, ?)",
                             (ip, ua, now))
                conn.commit()
    except Exception:
        pass  # Don't fail request if tracking fails



@app.errorhandler(HTTPException)
def _json_http_error(exc: HTTPException):
    if request.path.startswith("/api/"):
        response = exc.get_response()
        payload = {"error": exc.name, "description": exc.description}
        response.data = json.dumps(payload)
        response.content_type = "application/json"
        response.status_code = exc.code or 500
        return response
    return exc

@app.errorhandler(Exception)
def _json_generic_error(exc: Exception):
    if isinstance(exc, HTTPException):
        return _json_http_error(exc)
    if request.path.startswith("/api/"):
        app.logger.exception("Unhandled error during API request")
        return jsonify({"error": "Internal Server Error", "description": "An unexpected error occurred."}), 500
    raise exc

def _looks_like_header_line(text: str) -> bool:
    # Don't treat option lines as headers
    if re.match(r"^\s*[A-E]\s*[).:\-]", text):
        return False
        
    patterns = [
        r"(?i)\bcluster\b",
        r"(?i)\bcareer\s+cluster\b",
        r"(?i)\btest\s*(number|#)\b",
        r"(?i)\bdeca\b",
        r"(?i)\bexam\b",
        r"(?i)^page\s+\d+",
        r"^\d+\s*(of|/)\s*\d+$",
        # Only match actual copyright notices (with © or year), not answer content
        r"(?i)copyright\s*©",
        r"(?i)copyright\s*\d{4}",
        r"^[A-Z]{3,4}\s+-\s+[A-Z]", 
    ]
    if any(re.search(p, text) for p in patterns):
        return True
    
    # Stricter check for all-caps lines to avoid false positives on short question text
    tokens = text.split()
    if len(tokens) >= 3 and all(tok.isupper() or re.fullmatch(r"[A-Z0-9\-]+", tok) for tok in tokens):
        # Exclude common question words even if capitalized
        if "WHICH" in text.upper() or "WHAT" in text.upper():
            return False
        return True
    return False


def _worker_process_page(source_path: str, page_num: int, temp_file_path: str = None) -> List[str]:
    try:
        # Re-open the file in the worker
        # If temp_file is provided, use that
        path_to_use = temp_file_path if temp_file_path else source_path
        
        reader = PdfReader(path_to_use)
        if page_num >= len(reader.pages):
            return []
        page = reader.pages[page_num]
        
        lines = []
        splitter = re.compile(r"\s{2,}(?=(?:\d{1,3}|[A-E])\s*[.:\-])")
        

        raw_text = page.extract_text() or ""
        for raw_line in raw_text.splitlines():
            if splitter.search(raw_line):
                parts = splitter.split(raw_line)
            else:
                parts = [raw_line]

            for line in parts:
                line = line.strip()
                if not line:
                    continue

                line = re.sub(r"\s{2,}", " ", line)

                footer_regex = re.compile(r"(?:^|\s+)\b([A-Z]{3,5}\s*[-–—]\s*[A-Z])")
                footer_match = footer_regex.search(line)
                if footer_match:
                     line = line[:footer_match.start()].strip()

                     line = re.sub(r"\s+(and|Cluster)$", "", line).strip()
                     line = re.sub(r"\s+(Business Management|Hospitality|Finance|Marketing|Entrepreneurship|Administration)\s*$", "", line).strip()


                if "specialist levels." in line:
                    line = line.replace("specialist levels.", "").strip()

                # Handle copyright lines that may have answer key concatenated (e.g., "Ohio1.A")
                ohio_match = re.search(r"(Center®?,?\s*Columbus,?\s*Ohio)\s*(\d{1,3}\s*[.:,-]?\s*[A-E].*)?$", line, re.IGNORECASE)
                if ohio_match:
                    # Keep the answer part if present
                    answer_part = ohio_match.group(2)
                    line = line[:ohio_match.start()].strip()
                    if answer_part:
                        lines.append(answer_part.strip())

                if "career -sustaining" in line:
                    line = line.split("career -sustaining")[0].strip()
                if line.endswith("Business Management and"):
                    line = line[:-23].strip() 
                if "sustaining, specialist, supervi" in line:
                    line = line.split("sustaining, specialist, supervi")[0].strip()

                # Enhanced strict footer stripping
                line = re.sub(r"(?:^|\s+)Hospitality and Tourism.*$", "", line, flags=re.IGNORECASE).strip()
                line = re.sub(r"(?:^|\s+)Business Management.*$", "", line, flags=re.IGNORECASE).strip()
                line = re.sub(r"(?:^|\s+)\d{4}-\d{4}.*$", "", line).strip()
                # Only strip actual copyright notices (with © symbol or year pattern), not answer content
                line = re.sub(r"(?:^|\s+)Copyright\s*©.*$", "", line, flags=re.IGNORECASE).strip()
                line = re.sub(r"(?:^|\s+)Copyright\s*\d{4}.*$", "", line, flags=re.IGNORECASE).strip()
                line = re.sub(r"(?:^|\s+)CAUTION: Posting these materials.*$", "", line, flags=re.IGNORECASE).strip()
                line = re.sub(r"(?:^|\s+)Test questions were developed by.*$", "", line, flags=re.IGNORECASE).strip()
                line = re.sub(r"(?:^|\s+)Performance indicators for these.*$", "", line, flags=re.IGNORECASE).strip()
                line = re.sub(r"(?:^|\s+)are at the prerequisite.*$", "", line, flags=re.IGNORECASE).strip()
                line = re.sub(r"(?:^|\s+)Competitive Events.*$", "", line, flags=re.IGNORECASE).strip()
                line = re.sub(r"(?:^|\s+)Test-Item Bank.*$", "", line, flags=re.IGNORECASE).strip()

                # Check for header/footer but be careful not to trigger on question text
                if _looks_like_header_line(line):
                    cleaned = re.sub(r"(?i)^.*?copyright.*?ohio\s*", "", line)
                    if cleaned and cleaned != line:
                        line = cleaned
                        if _looks_like_header_line(line):
                             continue
                    else:
                        continue

                lines.append(line)

        return lines
    except Exception as e:
        # Use print in worker as logging config might not be propagated
        # or rely on stderr
        print(f"Worker Parsing Error on page {page_num}: {e}", file=sys.stderr)
        return []


def _extract_clean_lines(source: Path | IO[bytes]) -> List[str]:
    # Handle ByteIO by dumping to temp file
    temp_path = None
    is_bytes = isinstance(source, (io.BytesIO, bytes)) or (hasattr(source, 'read') and not isinstance(source, (str, Path)))
    
    try:
        if is_bytes:
            # Create a named temp file that persists so workers can read it
            # Close it so workers can open it
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                if hasattr(source, 'read'):
                    source.seek(0)
                    shutil.copyfileobj(source, tmp)
                else:
                    tmp.write(source)
                temp_path = tmp.name
            
            # Open reader just to get page count
            reader = PdfReader(temp_path)
            source_path_arg = None # Don't pass source_path if using temp
            path_for_worker = temp_path
        else:
            source = Path(source)
            reader = PdfReader(source)
            source_path_arg = str(source)
            path_for_worker = None # Worker uses source_path_arg
            
        num_pages = len(reader.pages)
        if num_pages > MAX_PDF_PAGES:
            raise ValueError(f"PDF has {num_pages} pages; maximum allowed is {MAX_PDF_PAGES}.")
        lines = []
        
        # Determine strict header threshold first?
        # No, we need lines first.
        # But we need page count for threshold, which we have.
        
        # Parallel Execution
        # 4 workers is usually sweet spot for PDF extraction
        max_workers = min(8, num_pages) if num_pages > 0 else 1
        
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
            # Map page numbers to workers
            # Pass source_path_arg as first arg (if real file), or None
            # Pass temp_path as third arg
            
            futures = []
            for i in range(num_pages):
                # Args: (source_path, page_num, temp_file_path)
                futures.append(executor.submit(_worker_process_page, source_path_arg, i, path_for_worker))
                
            for future in futures:
                try:
                    page_lines = future.result(timeout=PDF_PAGE_TIMEOUT_SECONDS)
                    lines.extend(page_lines)
                except Exception as e:
                    logger.error(f"Page processing error: {e}")
                    
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except:
                pass

    # Remove duplicates that appear on almost every page (headers/footers)
    # (Original logic follows)
    counts = {}
    for l in lines:
        counts[l] = counts.get(l, 0) + 1
    
    # Calculate a dynamic threshold based on page count
    # If a line appears on > 40% of pages, it's likely a header
    # page_count variable needs to be reused.
    page_count = num_pages 
    threshold = max(2, int(page_count * 0.4))
    
    final_lines = []
    for l in lines:
        # If line is very common, skip it
        if counts[l] > threshold:
            continue
        # If line looks like a header (and we missed it earlier), skip it
        if _looks_like_header_line(l):
            continue
        final_lines.append(l)
        
    return final_lines


COMMON_FIXES_RAW = [
    # === CRITICAL: Single letter + common word = fix ===
    # These patterns fix cases like "h as" -> "has" where the first letter got separated
    (r'\bh\s+as\b', 'has'),
    (r'\bw\s+as\b', 'was'),
    (r'\bh\s+is\b', 'his'),
    (r'\bh\s+er\b', 'her'),
    (r'\bh\s+im\b', 'him'),
    (r'\bh\s+ad\b', 'had'),
    (r'\bh\s+ave\b', 'have'),
    (r'\bh\s+ow\b', 'how'),
    (r'\bw\s+ho\b', 'who'),
    (r'\bw\s+hy\b', 'why'),
    (r'\bw\s+ay\b', 'way'),
    (r'\bw\s+ill\b', 'will'),
    (r'\bw\s+ere\b', 'were'),
    (r'\bw\s+ould\b', 'would'),
    (r'\bw\s+ant\b', 'want'),
    (r'\bw\s+hen\b', 'when'),
    (r'\bw\s+hat\b', 'what'),
    (r'\bw\s+here\b', 'where'),
    (r'\bt\s+he\b', 'the'),
    (r'\bt\s+his\b', 'this'),
    (r'\bt\s+hat\b', 'that'),
    (r'\bt\s+hen\b', 'then'),
    (r'\bt\s+hey\b', 'they'),
    (r'\bt\s+hem\b', 'them'),
    (r'\bt\s+here\b', 'there'),
    (r'\bt\s+hose\b', 'those'),
    (r'\bt\s+heir\b', 'their'),
    
    # === ADDITIONAL PATTERN FIXES (from analysis) ===
    (r'\brece\s+ives?\b', 'receives'),
    (r'\bbenef\s+its?\b', 'benefits'),
    (r'\bbenef\s+it\b', 'benefit'),
    (r'\bcom\s+pany\b', 'company'),
    (r'\bpro\s+duct\b', 'product'),
    (r'\bser\s+vice\b', 'service'),
    (r'\bcus\s+tomer\b', 'customer'),
    (r'\bman\s+age\b', 'manage'),
    (r'\bpro\s+vide\b', 'provide'),
    
    # === CAPITAL LETTER SPLITS (most common from analysis) ===
    # Words ending in -it that get split with capital I
    (r'\bbenef\s*[Ii]t\b', 'benefit'),
    (r'\bbenef\s*[Ii]ts\b', 'benefits'),
    (r'\bcred\s*[Ii]t\b', 'credit'),
    (r'\bcred\s*[Ii]ts\b', 'credits'),
    (r'\btrans\s*[Ii]t\b', 'transit'),
    (r'\bsubm\s*[Ii]t\b', 'submit'),
    (r'\bsubm\s*[Ii]ts\b', 'submits'),
    (r'\bprof\s*[Ii]t\b', 'profit'),
    (r'\bprof\s*[Ii]ts\b', 'profits'),
    (r'\blim\s*[Ii]t\b', 'limit'),
    (r'\blim\s*[Ii]ts\b', 'limits'),
    (r'\baud\s*[Ii]t\b', 'audit'),
    (r'\baud\s*[Ii]ts\b', 'audits'),
    (r'\bdepos\s*[Ii]t\b', 'deposit'),
    (r'\bdepos\s*[Ii]ts\b', 'deposits'),
    (r'\bexh\s*[Ii]b\s*[Ii]t\b', 'exhibit'),
    (r'\bperm\s*[Ii]t\b', 'permit'),
    (r'\bperm\s*[Ii]ts\b', 'permits'),
    (r'\bun\s*[Ii]t\b', 'unit'),
    (r'\bun\s*[Ii]ts\b', 'units'),
    (r'\bexpl\s*[Ii]c\s*[Ii]t\b', 'explicit'),
    
    # Words ending in -as that get split with capital A
    (r'\bpurch\s*[Aa]s\b', 'purchase'),  
    (r'\bpurch\s*[Aa]ses\b', 'purchases'),
    (r'\bextr\s*[Aa]s\b', 'extras'),
    (r'\bextr\s*[Aa]\b', 'extra'),
    (r'\bide\s*[Aa]s\b', 'ideas'),
    (r'\bare\s*[Aa]s\b', 'areas'),
    (r'\brele\s*[Aa]s\b', 'release'),
    (r'\brele\s*[Aa]ses\b', 'releases'),
    
    # Other common capital letter splits
    (r'\b[Rr]etrieved\s+[Aa]ugust\b', 'Retrieved August'),
    (r'\betrieved\s+[Aa]ugust\b', 'Retrieved August'),
    (r'\bengage\s+[Ll]earning\b', 'Engage Learning'),
    (r'\bonsumer\s+[Pp]roduct\b', 'Consumer Product'),
    (r'\bwith\s*[Ii]n\b', 'within'),
    (r'\bwith\s*[Oo]ut\b', 'without'),
    
    # -ity splits with capital letters  
    (r'\bquant\s*[Ii]ty\b', 'quantity'),
    (r'\bqual\s*[Ii]ty\b', 'quality'),
    (r'\babil\s*[Ii]ty\b', 'ability'),
    (r'\bliabil\s*[Ii]ty\b', 'liability'),
    (r'\bpersonal\s*[Ii]ty\b', 'personality'),
    (r'\bcapabil\s*[Ii]ty\b', 'capability'),
    (r'\bactiv\s*[Ii]ty\b', 'activity'),
    (r'\bactiv\s*[Ii]ties\b', 'activities'),
    (r'\bopportun\s*[Ii]ty\b', 'opportunity'),
    (r'\butil\s*[Ii]ty\b', 'utility'),
    
    # -er/-or splits
    (r'\bcustom\s*[Ee]r\b', 'customer'),
    (r'\bcustom\s*[Ee]rs\b', 'customers'),
    (r'\bmanag\s*[Ee]r\b', 'manager'),
    (r'\bmanag\s*[Ee]rs\b', 'managers'),
    (r'\bemploy\s*[Ee]r\b', 'employer'),
    (r'\bemploy\s*[Ee]rs\b', 'employers'),
    (r'\binvest\s*[Oo]r\b', 'investor'),
    (r'\binvest\s*[Oo]rs\b', 'investors'),
    
    # === CRITICAL EDGE CASES: Short word splits ===
    # These happen when common words get split at unusual points
    (r'\ba\s+nd\b', 'and'),
    (r'\ba\s+ndthe\b', 'and the'),
    (r'\ba\s+s\b', 'as'),  # Only when followed by space
    (r'\bo\s+f\b', 'of'),

    (r'\bo\s+n\b', 'on'),
    (r'\bo\s+r\b', 'or'),
    (r'\bi\s+n\b', 'in'),
    (r'\bi\s+s\b', 'is'),
    (r'\bi\s+t\b', 'it'),
    (r'\bt\s+o\b', 'to'),
    (r'\bt\s+he\b', 'the'),
    (r'\bw\s+e\b', 'we'),
    (r'\bb\s+e\b', 'be'),
    (r'\bb\s+y\b', 'by'),
    (r'\ba\s+t\b', 'at'),
    (r'\bu\s+p\b', 'up'),
    (r'\bn\s+o\b', 'no'),
    (r'\bs\s+o\b', 'so'),
    (r'\bm\s+y\b', 'my'),
    (r'\bh\s+e\b', 'he'),
    (r'\bf\s+or\b', 'for'),
    (r'\bf\s+rom\b', 'from'),
    (r'\bw\s+ith\b', 'with'),
    (r'\bth\s+at\b', 'that'),
    (r'\bth\s+is\b', 'this'),
    (r'\bth\s+ey\b', 'they'),
    (r'\bth\s+em\b', 'them'),
    (r'\bth\s+en\b', 'then'),
    (r'\bwh\s+en\b', 'when'),
    (r'\bwh\s+at\b', 'what'),
    (r'\bwh\s+o\b', 'who'),
    (r'\bwh\s+ich\b', 'which'),
    (r'\bha\s+ve\b', 'have'),
    (r'\bha\s+s\b', 'has'),
    (r'\bha\s+d\b', 'had'),
    (r'\bca\s+n\b', 'can'),
    (r'\bwi\s+ll\b', 'will'),
    (r'\bwo\s+uld\b', 'would'),
    (r'\bwi\s+th\b', 'with'),
    (r'\bev\s+al\s*uating\b', 'evaluating'),
    (r'\beval\s+uating\b', 'evaluating'),
    (r'\bsitu\s+ation\b', 'situation'),
    
    # === BUSINESS/FINANCE CORE TERMS ===
    (r'\bbusi?\s*ness\b', 'business'),
    (r'\bbus\s+iness\b', 'business'),
    (r'\bfi\s*nance\b', 'finance'),
    (r'\bfi\s*nan\s*cial\b', 'financial'),
    (r'\bin\s*for\s*ma\s*tion\b', 'information'),
    (r'\binfor\s*mation\b', 'information'),
    (r'\bman\s*age\s*ment\b', 'management'),
    (r'\bmanage\s*ment\b', 'management'),
    (r'\bcus\s*tom\s*er\b', 'customer'),
    (r'\bcustom\s*er\b', 'customer'),
    (r'\bcom\s*pa\s*ny\b', 'company'),
    (r'\bcompan\s*y\b', 'company'),
    (r'\bpro\s*duct\b', 'product'),
    (r'\bproduc\s*t\b', 'product'),
    (r'\bser\s*vice\b', 'service'),
    (r'\bservic\s*e\b', 'service'),
    (r'\bmar\s*ket\s*ing\b', 'marketing'),
    (r'\bmarket\s*ing\b', 'marketing'),
    (r'\bem\s*ploy\s*ee\b', 'employee'),
    (r'\bemploy\s*ee\b', 'employee'),
    (r'\bor\s*gan\s*iza\s*tion\b', 'organization'),
    (r'\borgan\s*ization\b', 'organization'),
    (r'\borganiza\s*tion\b', 'organization'),
    (r'\bcom\s*mu\s*ni\s*ca\s*tion\b', 'communication'),
    (r'\bcommunica\s*tion\b', 'communication'),
    (r'\bde\s*ci\s*sion\b', 'decision'),
    (r'\bdeci\s*sion\b', 'decision'),
    (r'\bop\s*er\s*a\s*tion\b', 'operation'),
    (r'\bopera\s*tion\b', 'operation'),
    
    # === COMMON VERBS ===
    (r'\bSOURC\s*E\b', 'SOURCE'),
    (r'\bsourc\s*e\b', 'source'),
    (r'\bre\s*triev\s*ed\b', 'retrieved'),
    (r'\bRetriev\s*ed\b', 'Retrieved'),
    (r'\bdeter\s*mine\b', 'determine'),
    (r'\bunder\s*stand\b', 'understand'),
    (r'\bunder\s*standing\b', 'understanding'),
    (r'\bpro\s*vide\b', 'provide'),
    (r'\bprovid\s*ing\b', 'providing'),
    (r'\bim\s*prove\b', 'improve'),
    (r'\bimprov\s*ing\b', 'improving'),
    (r'\bcon\s*sider\b', 'consider'),
    (r'\bcon\s*tact\b', 'contact'),
    (r'\bcon\s*trol\b', 'control'),
    (r'\bcon\s*tract\b', 'contract'),
    (r'\bcon\s*sumer\b', 'consumer'),
    (r'\bcon\s*tinue\b', 'continue'),
    (r'\bex\s*ample\b', 'example'),
    (r'\bex\s*plain\b', 'explain'),
    (r'\bex\s*pect\b', 'expect'),
    (r'\bex\s*perience\b', 'experience'),
    (r'\bre\s*quire\b', 'require'),
    (r'\bre\s*sponse\b', 'response'),
    (r'\bre\s*sult\b', 'result'),
    (r'\bre\s*port\b', 'report'),
    (r'\bre\s*ceive\b', 'receive'),
    (r'\bre\s*view\b', 'review'),
    (r'\bre\s*search\b', 'research'),
    (r'\bper\s*form\b', 'perform'),
    (r'\bper\s*son\b', 'person'),
    (r'\bper\s*sonal\b', 'personal'),
    
    # === COMMON NOUNS ===
    (r'\bprofes\s*sional\b', 'professional'),
    (r'\brel\s*ation\s*ship\b', 'relationship'),
    (r'\brelation\s*ship\b', 'relationship'),
    (r'\bdevel\s*op\s*ment\b', 'development'),
    (r'\bdevelop\s*ment\b', 'development'),
    (r'\benviron\s*ment\b', 'environment'),
    (r'\btech\s*nol\s*ogy\b', 'technology'),
    (r'\btechnol\s*ogy\b', 'technology'),
    (r'\badver\s*tis\s*ing\b', 'advertising'),
    (r'\badvertis\s*ing\b', 'advertising'),
    (r'\bexplan\s*ation\b', 'explanation'),
    (r'\binstru\s*ment\b', 'instrument'),
    (r'\bques\s*tion\b', 'question'),
    (r'\bregu\s*la\s*tion\b', 'regulation'),
    (r'\bregula\s*tion\b', 'regulation'),
    (r'\bdocu\s*ment\b', 'document'),
    (r'\bstate\s*ment\b', 'statement'),
    (r'\binvest\s*ment\b', 'investment'),
    (r'\bequip\s*ment\b', 'equipment'),
    (r'\brequire\s*ment\b', 'requirement'),
    (r'\bachieve\s*ment\b', 'achievement'),
    (r'\badvan\s*tage\b', 'advantage'),
    (r'\bknowl\s*edge\b', 'knowledge'),
    (r'\bstra\s*tegy\b', 'strategy'),
    (r'\bstrateg\s*y\b', 'strategy'),
    (r'\bactiv\s*ity\b', 'activity'),
    (r'\bopportun\s*ity\b', 'opportunity'),
    (r'\brespons\s*ibility\b', 'responsibility'),
    (r'\bresponsi\s*bility\b', 'responsibility'),
    (r'\babil\s*ity\b', 'ability'),
    (r'\bqual\s*ity\b', 'quality'),
    (r'\bquant\s*ity\b', 'quantity'),
    (r'\butil\s*ity\b', 'utility'),
    (r'\bsecur\s*ity\b', 'security'),
    (r'\bauthor\s*ity\b', 'authority'),
    (r'\bprior\s*ity\b', 'priority'),
    (r'\bcomplex\s*ity\b', 'complexity'),
    
    # === MORE BUSINESS TERMS ===
    (r'\bemploy\s*er\b', 'employer'),
    (r'\bemploy\s*ment\b', 'employment'),
    (r'\bsales\s*person\b', 'salesperson'),
    (r'\bread\s*ing\b', 'reading'),
    (r'\bwrit\s*ing\b', 'writing'),
    (r'\bspeak\s*ing\b', 'speaking'),
    (r'\blisten\s*ing\b', 'listening'),
    (r'\blearn\s*ing\b', 'learning'),
    (r'\btrain\s*ing\b', 'training'),
    (r'\bplan\s*ning\b', 'planning'),
    (r'\bbudget\s*ing\b', 'budgeting'),
    (r'\baccount\s*ing\b', 'accounting'),
    (r'\bbank\s*ing\b', 'banking'),
    (r'\bpric\s*ing\b', 'pricing'),
    (r'\bbrand\s*ing\b', 'branding'),
    (r'\bsell\s*ing\b', 'selling'),
    (r'\bbuy\s*ing\b', 'buying'),
    (r'\bship\s*ping\b', 'shipping'),
    (r'\bpack\s*aging\b', 'packaging'),
    (r'\bpromot\s*ion\b', 'promotion'),
    (r'\bpromo\s*tion\b', 'promotion'),
    (r'\bdistri\s*bution\b', 'distribution'),
    (r'\bproduct\s*ion\b', 'production'),
    (r'\bcompet\s*ition\b', 'competition'),
    (r'\bcompeti\s*tion\b', 'competition'),
    (r'\bposi\s*tion\b', 'position'),
    (r'\bcondi\s*tion\b', 'condition'),
    (r'\btransi\s*tion\b', 'transition'),
    (r'\bsolu\s*tion\b', 'solution'),
    (r'\beval\s*uation\b', 'evaluation'),
    (r'\bsitu\s*ation\b', 'situation'),
    (r'\bpresen\s*tation\b', 'presentation'),
    (r'\bappli\s*cation\b', 'application'),
    (r'\binforma\s*tion\b', 'information'),
    (r'\bimportant\b', 'important'),
    (r'\bimport\s*ant\b', 'important'),
    (r'\bdifferent\b', 'different'),
    (r'\bdiffer\s*ent\b', 'different'),
    (r'\beffect\s*ive\b', 'effective'),
    (r'\bproduct\s*ive\b', 'productive'),
    (r'\bposit\s*ive\b', 'positive'),
    (r'\bnegat\s*ive\b', 'negative'),
    (r'\bcreate\s*ive\b', 'creative'),
    (r'\bcompet\s*itive\b', 'competitive'),
    
    # === ADDITIONAL COMMON WORDS ===
    (r'\bfollow\s*ing\b', 'following'),
    (r'\binclu\s*ding\b', 'including'),
    (r'\bbecome\s*ing\b', 'becoming'),
    (r'\bbehav\s*ior\b', 'behavior'),
    (r'\binter\s*est\b', 'interest'),
    (r'\binter\s*net\b', 'internet'),
    (r'\binter\s*view\b', 'interview'),
    (r'\binter\s*nal\b', 'internal'),
    (r'\binter\s*action\b', 'interaction'),
    (r'\bextern\s*al\b', 'external'),
    (r'\borigin\s*al\b', 'original'),
    (r'\bperson\s*al\b', 'personal'),
    (r'\bproces\s*s\b', 'process'),
    (r'\bprogr\s*am\b', 'program'),
    (r'\bprob\s*lem\b', 'problem'),
    (r'\bpur\s*pose\b', 'purpose'),
    (r'\bpur\s*chase\b', 'purchase'),
    (r'\bstand\s*ard\b', 'standard'),
    (r'\bpart\s*ner\b', 'partner'),
    (r'\bpart\s*nership\b', 'partnership'),
    (r'\bleader\s*ship\b', 'leadership'),
    (r'\bmember\s*ship\b', 'membership'),
    (r'\bowner\s*ship\b', 'ownership'),
    (r'\bspons\s*orship\b', 'sponsorship'),
    (r'\bintern\s*ship\b', 'internship'),
    (r'\bscholar\s*ship\b', 'scholarship'),
    (r'\bcitizen\s*ship\b', 'citizenship'),
    (r'\bfriend\s*ship\b', 'friendship'),
    (r'\bwork\s*place\b', 'workplace'),
    (r'\bmarket\s*place\b', 'marketplace'),
    
    # === FIX COMMON SHORT SPLITS ===
    (r'\bwi\s*th\b', 'with'),
    (r'\bwit\s*h\b', 'with'),
    (r'\bth\s*at\b', 'that'),
    (r'\btha\s*t\b', 'that'),
    (r'\bth\s*is\b', 'this'),
    (r'\bthi\s*s\b', 'this'),
    (r'\bth\s*ey\b', 'they'),
    (r'\bthe\s*y\b', 'they'),
    (r'\bth\s*em\b', 'them'),
    (r'\bthe\s*m\b', 'them'),
    (r'\bth\s*eir\b', 'their'),
    (r'\bthei\s*r\b', 'their'),
    (r'\bth\s*ere\b', 'there'),
    (r'\bther\s*e\b', 'there'),
    (r'\bth\s*ese\b', 'these'),
    (r'\bthes\s*e\b', 'these'),
    (r'\bwh\s*ich\b', 'which'),
    (r'\bwhic\s*h\b', 'which'),
    (r'\bwh\s*en\b', 'when'),
    (r'\bwhe\s*n\b', 'when'),
    (r'\bwh\s*ere\b', 'where'),
    (r'\bwher\s*e\b', 'where'),
    (r'\bwh\s*at\b', 'what'),
    (r'\bwha\s*t\b', 'what'),
    (r'\bab\s*out\b', 'about'),
    (r'\babou\s*t\b', 'about'),
    (r'\bfr\s*om\b', 'from'),
    (r'\bfro\s*m\b', 'from'),
    (r'\bhave\b', 'have'),
    (r'\bha\s*ve\b', 'have'),
    (r'\bsh\s*ould\b', 'should'),
    (r'\bshou\s*ld\b', 'should'),
    (r'\bwo\s*uld\b', 'would'),
    (r'\bwoul\s*d\b', 'would'),
    (r'\bco\s*uld\b', 'could'),
    (r'\bcoul\s*d\b', 'could'),
    (r'\bbe\s*cause\b', 'because'),
    (r'\bbecau\s*se\b', 'because'),
    (r'\bbefor\s*e\b', 'before'),
    (r'\baft\s*er\b', 'after'),
    (r'\bafte\s*r\b', 'after'),
    (r'\both\s*er\b', 'other'),
    (r'\bothe\s*r\b', 'other'),
    (r'\beff\s*ect\b', 'effect'),
    (r'\beffec\s*t\b', 'effect'),
]

ADDITIONAL_FIXES_RAW = [
    # === FIX: Common word splits that the general merge logic misses ===
    (r'\ban\s+d\b', 'and'),  # "an d" → "and" (common split missed by prefix merge)
    (r'\bExclu\s*sive\b', 'Exclusive'),
    (r'\bexclu\s*sive\b', 'exclusive'),
    (r'\binclu\s*sive\b', 'inclusive'),
    (r'\binc\s*ome\b', 'income'),
    (r'\boutc\s*ome\b', 'outcome'),
    (r'\bresou\s*rces\b', 'resources'),
    (r'\bresou\s*rce\b', 'resource'),
    (r'\bth\s*rough\b', 'through'),
    (r'\bth\s*an\b', 'than'),
    (r'\bYo\s*ucan\b', 'You can'),
    (r'\bus\s*er\b', 'user'),
    (r'\bunabl\s*e\b', 'unable'),
    (r'\brobo\s*ts\b', 'robots'),
    (r'\brisk\s*s\b', 'risks'),
    (r'\bAlthoug\s*h\b', 'Although'),
    (r'\bThoug\s*h\b', 'Though'),
    (r'\bThroug\s*h\b', 'Through'),
    (r'\bEnoug\s*h\b', 'Enough'),
    (r'\balthoug\s*h\b', 'although'),
    (r'\bthoug\s*h\b', 'though'),
    (r'\bthroug\s*h\b', 'through'),
    (r'\benoug\s*h\b', 'enough'),
    (r'\bpar\s*t\b', 'part'),
    (r'\bpar\s*ts\b', 'parts'),
    (r'\bcos\s*t\b', 'cost'),
    (r'\bcos\s*ts\b', 'costs'),
    (r'\bno\s*t\b', 'not'),
    (r'\bmus\s*t\b', 'must'),
    (r'\bbes\s*t\b', 'best'),
    (r'\btes\s*t\b', 'test'),
    (r'\blis\s*t\b', 'list'),
    (r'\bjus\s*t\b', 'just'),
    (r'\blas\s*t\b', 'last'),
    (r'\bfirs\s*t\b', 'first'),
    (r'\bmos\s*t\b', 'most'),
    (r'\bpos\s*t\b', 'post'),
    (r'\brus\s*t\b', 'rust'),
    (r'\btrus\s*t\b', 'trust'),
    (r'\binteres\s*t\b', 'interest'),
    (r'\bconsis\s*t\b', 'consist'),
    (r'\bexis\s*t\b', 'exist'),
    (r'\bproduc\s*t\b', 'product'),
    (r'\bimpac\s*t\b', 'impact'),
    (r'\bcontac\s*t\b', 'contact'),
    (r'\bexac\s*t\b', 'exact'),
    (r'\bdirec\s*t\b', 'direct'),
    (r'\bshor\s*t\b', 'short'),
    (r'\brepor\s*t\b', 'report'),
    (r'\bmar\s*ket\b', 'market'),
    (r'\bbene\s*fit\b', 'benefit'),
    (r'\bben\s*efit\b', 'benefit'),
    (r'\bprofes\s*sion\b', 'profession'),
    (r'\bimpor\s*t\b', 'import'),
    (r'\bexpor\s*t\b', 'export'),
    (r'\bcomfor\s*t\b', 'comfort'),
    (r'\bsuppor\s*t\b', 'support'),
    (r'\btranspor\s*t\b', 'transport'),
    (r'\beffec\s*t\b', 'effect'),
    (r'\bselec\s*t\b', 'select'),
    (r'\bprojec\s*t\b', 'project'),
    (r'\bsubjec\s*t\b', 'subject'),
    (r'\bobjec\s*t\b', 'object'),
    (r'\bprotec\s*t\b', 'protect'),
    (r'\bdetec\s*t\b', 'detect'),
    (r'\bexpec\s*t\b', 'expect'),
    (r'\brespec\s*t\b', 'respect'),
    (r'\binsec\s*t\b', 'insect'),
    (r'\binfec\s*t\b', 'infect'),
    (r'\bcollec\s*t\b', 'collect'),
    (r'\bconnec\s*t\b', 'connect'),
    (r'\bproduc\s*ts\b', 'products'),
    (r'\bcontrac\s*t\b', 'contract'),
    (r'\bcontrac\s*ts\b', 'contracts'),
    (r'\bus\s*e\b', 'use'),
    (r'\bthos\s*e\b', 'those'),
    (r'\bthes\s*e\b', 'these'),
    (r'\bclos\s*e\b', 'close'),
    (r'\bpurpos\s*e\b', 'purpose'),
    (r'\bpurchas\s*e\b', 'purchase'),
    (r'\bincreas\s*e\b', 'increase'),
    (r'\bdecreas\s*e\b', 'decrease'),
    (r'\breleas\s*e\b', 'release'),
    (r'\bchoos\s*e\b', 'choose'),
    (r'\brespons\s*e\b', 'response'),
    (r'\bexpens\s*e\b', 'expense'),
    (r'\bdefens\s*e\b', 'defense'),
    (r'\blicens\s*e\b', 'license'),
    (r'\bpromis\s*e\b', 'promise'),
    (r'\bexercis\s*e\b', 'exercise'),
    (r'\bpractic\s*e\b', 'practice'),
    (r'\bservic\s*e\b', 'service'),

    # Words found in spacing analysis that were still broken
    (r'\bciv\s*il\b', 'civil'),
    (r'\bmaj\s*ority\b', 'majority'),
    (r'\bret\s*ailers\b', 'retailers'),
    (r'\brath\s*er\b', 'rather'),
    (r'\bcons\s*umers\b', 'consumers'),
    (r'\bcontroll\s*ing\b', 'controlling'),
    (r'\bslott\s*ing\b', 'slotting'),
    (r'\bsimplifyi\s*ng\b', 'simplifying'),
    (r'\beffecti\s*vely\b', 'effectively'),
    (r'\blisteni\s*ng\b', 'listening'),
    (r'\bmaki\s*ng\b', 'making'),
    (r'\btaki\s*ng\b', 'taking'),
    (r'\bhavi\s*ng\b', 'having'),
    (r'\bgivi\s*ng\b', 'giving'),
    (r'\busi\s*ng\b', 'using'),
    (r'\bmeani\s*ng\b', 'meaning'),
    (r'\bbec\s*ause\b', 'because'),
    (r'\bmes\s*sage\b', 'message'),
    (r'\baff\s*ect\b', 'affect'),
    (r'\bspe\s*cific\b', 'specific'),
    (r'\bdiffi\s*cult\b', 'difficult'),
    (r'\bsemi\s*nar\b', 'seminar'),
    (r'\binformati\s*on\b', 'information'),
    (r'\brel\s*y\b', 'rely'),
    (r'\bYo\s*ucan\b', 'You can'),
    (r'\bwit\s*htheir\b', 'with their'),
    (r'\bwit\s*hout\b', 'without'),
    (r'\bwhi\s*ch\b', 'which'),
    (r'\bmone\s*y\b', 'money'),
    (r'\bsho\s*uld\b', 'should'),
    (r'\bcou\s*ld\b', 'could'),
    (r'\bwou\s*ld\b', 'would'),
    (r'\ba\s+re\s+based\b', 'are based'),
    (r'\bsteppings\s*tones\b', 'steppingstones'),
    (r'\btriggerne\s*w\b', 'trigger new'),
    (r'\bveryoutlandish\b', 'very outlandish'),
    (r'\blisteni\s*ngand\b', 'listening and'),
    (r'\bwhi\s*chmay\b', 'which may'),
    (r'\bsimplifyi\s*ngexisting\b', 'simplifying existing'),
    (r'\brath\s*erthan\b', 'rather than'),
    (r'\bciv\s*illitigation\b', 'civil litigation'),
    # More -ing splits
    (r'\bkee\s*ping\b', 'keeping'),
    (r'\bsel\s*ling\b', 'selling'),
    (r'\btel\s*ling\b', 'telling'),
    (r'\bgett\s*ing\b', 'getting'),
    (r'\bsett\s*ing\b', 'setting'),
    (r'\blett\s*ing\b', 'letting'),
    (r'\bputt\s*ing\b', 'putting'),
    (r'\bcutt\s*ing\b', 'cutting'),
    (r'\bhitt\s*ing\b', 'hitting'),
    (r'\bsitt\s*ing\b', 'sitting'),
    # More common splits
    (r'\binfor\s*mation\b', 'information'),
    (r'\beffici\s*ent\b', 'efficient'),
    (r'\beffici\s*ency\b', 'efficiency'),
    (r'\bsuffi\s*cient\b', 'sufficient'),
    (r'\bdefici\s*ent\b', 'deficient'),
    
    # === NEW: -ity word splits (from analysis) ===
    (r'\bprofitabilit\s*y\b', 'profitability'),
    (r'\babilit\s*y\b', 'ability'),
    (r'\bqualit\s*y\b', 'quality'),
    (r'\bliabilit\s*y\b', 'liability'),
    (r'\bfacilit\s*y\b', 'facility'),
    (r'\bflexibilit\s*y\b', 'flexibility'),
    (r'\bresponsibilit\s*y\b', 'responsibility'),
    (r'\bquantit\s*y\b', 'quantity'),
    (r'\bactivit\s*y\b', 'activity'),
    (r'\brealit\s*y\b', 'reality'),
    (r'\bvariet\s*y\b', 'variety'),
    (r'\bcurrenc\s*y\b', 'currency'),
    (r'\bpolic\s*y\b', 'policy'),
    (r'\bphilosoph\s*y\b', 'philosophy'),
    (r'\bentiret\s*y\b', 'entirety'),
    (r'\bhonest\s*y\b', 'honesty'),
    (r'\bwarrant\s*y\b', 'warranty'),
    
    # === NEW: -ly word splits (from analysis) ===
    (r'\bquickl\s*y\b', 'quickly'),
    (r'\blikel\s*y\b', 'likely'),
    (r'\bpositivel\s*y\b', 'positively'),
    (r'\binitiall\s*y\b', 'initially'),
    (r'\bstrictl\s*y\b', 'strictly'),
    (r'\bsimilarl\s*y\b', 'similarly'),
    (r'\bfriendl\s*y\b', 'friendly'),
    (r'\bnecessar\s*y\b', 'necessary'),
    (r'\bhorsepla\s*y\b', 'horseplay'),
    (r'\bhapp\s*y\b', 'happy'),
    (r'\bjul\s*y\b', 'july'),
    (r'\bvar\s*y\b', 'vary'),
    
    # === NEW: -ic word splits (from analysis) ===
    (r'\bstrategi\s*c\b', 'strategic'),
    (r'\bspecifi\s*c\b', 'specific'),
    (r'\bethi\s*c\b', 'ethic'),
    
    # === NEW: -ew/-ow/-elf word splits ===
    (r'\bvie\s*w\b', 'view'),
    (r'\bfollo\s*w\b', 'follow'),
    (r'\bhersel\s*f\b', 'herself'),
    (r'\byoursel\s*f\b', 'yourself'),
    
    # === NEW: Compound word fixes ===
    (r'\brightha\s*nd\b', 'righthand'),
    (r'\bcleanai\s*r\b', 'clean air'),
    (r'\banden\s*d\b', 'and end'),
    (r'\bandh\s*e\b', 'and he'),
    (r'\bthewa\s*y\b', 'the way'),
    (r'\bhisbo\s*ss\b', 'his boss'),
    (r'\bnationalla\s*w\b', 'national law'),
    (r'\bpowerfulwa\s*y\b', 'powerful way'),
    (r'\binformationma\s*y\b', 'information may'),
    (r'\bhelpyo\s*u\b', 'help you'),
    (r'\buseshi\s*gh\b', 'uses high'),
    (r'\briverlogi\s*c\b', 'riverlogic'),
    
    # === NEW: More -ity splits found in analysis ===
    (r'\btangibil\s*ity\b', 'tangibility'),
    (r'\bintegr\s*ity\b', 'integrity'),
    (r'\bliabil\s*ity\b', 'liability'),
    (r'\bcommun\s*ity\b', 'community'),
    (r'\bhospital\s*ity\b', 'hospitality'),
    (r'\bfacil\s*ity\b', 'facility'),
    (r'\bequ\s*ity\b', 'equity'),
    (r'\bviabil\s*ity\b', 'viability'),
    (r'\bresponsibil\s*ity\b', 'responsibility'),
    (r'\bcapabil\s*ity\b', 'capability'),
    (r'\bpossibil\s*ity\b', 'possibility'),
    (r'\bstabil\s*ity\b', 'stability'),
    (r'\bvisibil\s*ity\b', 'visibility'),
    (r'\bflexibil\s*ity\b', 'flexibility'),
    (r'\bcredibil\s*ity\b', 'credibility'),
    (r'\bdurabil\s*ity\b', 'durability'),
    (r'\bavailabil\s*ity\b', 'availability'),
    (r'\baccountabil\s*ity\b', 'accountability'),
    (r'\breliabil\s*ity\b', 'reliability'),
    (r'\bsustainabil\s*ity\b', 'sustainability'),
    
    # === NEW: Run-on word fixes ===
    (r'\byo\s*ucan\b', 'you can'),
    (r'\by\s*ouachieve\b', 'you achieve'),
    (r'\by\s*ouhave\b', 'you have'),
    (r'\by\s*oushould\b', 'you should'),
    (r'\by\s*ounext\b', 'you next'),
    (r'\byo\s*uare\b', 'you are'),
    (r'\bt\s*ocalculate\b', 'to calculate'),
    (r'\bt\s*oinfluence\b', 'to influence'),
    (r'\bt\s*ocheck\b', 'to check'),
    (r'\bo\s*wnstore\b', 'own store'),
    (r'\bo\s*wnideas\b', 'own ideas'),
    (r'\bo\s*raffect\b', 'or affect'),
    (r'\bo\s*fcompetitors\b', 'of competitors'),
    (r'\bo\s*ffinancial\b', 'of financial'),
    (r'\bo\s*fnegotiating\b', 'of negotiating'),
    (r'\bo\s*nanyone\b', 'on anyone'),
    (r'\bb\s*eexperts\b', 'be experts'),
    (r'\bb\s*ylogical\b', 'by logical'),
    (r'\bb\s*ycombining\b', 'by combining'),
    (r'\bb\s*utdaily\b', 'but daily'),
    (r'\bb\s*yfollowing\b', 'by following'),
    (r'\bb\s*eviewed\b', 'be viewed'),
    (r'\bb\s*ymultiplying\b', 'by multiplying'),
    (r'\bs\s*etassumptions\b', 'set assumptions'),
    (r'\bs\s*othey\b', 'so they'),
    (r'\bf\s*argreater\b', 'far greater'),
    (r'\bf\s*ewquestions\b', 'few questions'),
    (r'\bho\s*wclosely\b', 'how closely'),
    (r'\bw\s*eall\b', 'we all'),
    (r'\bj\s*obapplicant\b', 'job applicant'),
    (r'\bx\s*yzgrocery\b', 'xyz grocery'),
    (r'\bda\s*ysago\b', 'days ago'),
    (r'\bd\s*aycare\b', 'daycare'),
    (r'\ban\s*y\b', 'any'),
    (r'\bcantr\s*y\b', 'can try'),
    (r'\bsinc\s*ey\b', 'since y'),
    (r'\bcall\s*y\b', 'cally'),  # Likely a name
    
    # === NEW: Fixes found from explanation spacing check ===
    (r'\bnego\s*tiates\b', 'negotiates'),
    (r'\bnego\s*tiate\b', 'negotiate'),
    (r'\bnego\s*tiation\b', 'negotiation'),
    (r'\bals\s*oallow\b', 'also allow'),
    (r'\bals\s*o\b', 'also'),
    (r'\ban\s*dsearch\b', 'and search'),
    (r'\band\s*earch\b', 'and search'),
    (r'\bpurchas\s*ing\b', 'purchasing'),
    (r'\bpublish\s*ing\b', 'publishing'),
    (r'\bresignati\s*on\b', 'resignation'),
    
    # Common run-on word fixes
    (r'\bthey\s*als\s*o\b', 'they also'),
    (r'\bwe\s*do\s*not\b', 'we do not'),
    (r'\bwould\s*not\b', 'would not'),
    (r'\bcould\s*not\b', 'could not'),
    (r'\bshould\s*not\b', 'should not'),
    (r'\bdoes\s*not\b', 'does not'),
    (r'\bdid\s*not\b', 'did not'),
    (r'\bwill\s*not\b', 'will not'),
    (r'\bcan\s*not\b', 'cannot'),
    
    # Additional word splits from PDF extraction
    (r'\bwhichs\s*/\s*he\b', 'which s/he'),
    (r'\breprimand\s*orfire\b', 'reprimand or fire'),
    (r'\borfire\b', 'or fire'),
    
    # === COMPREHENSIVE RUN-ON WORD FIXES (found in analysis) ===
    # Words ending in ...the (run-on with 'the')
    (r'\boutsi\s*dethe\b', 'outside the'),
    (r'\binsi\s*dethe\b', 'inside the'),
    (r'\bunderstandthe\b', 'understand the'),
    (r'\bdeterminethe\b', 'determine the'),
    (r'\bincreasethe\b', 'increase the'),
    (r'\bdecreasethe\b', 'decrease the'),
    (r'\bimprovethe\b', 'improve the'),
    (r'\breducethe\b', 'reduce the'),
    (r'\bachievethe\b', 'achieve the'),
    (r'\breceivethe\b', 'receive the'),
    (r'\bprovidethe\b', 'provide the'),
    (r'\brequirethe\b', 'require the'),
    (r'\bdescribethe\b', 'describe the'),
    (r'\bfollowthe\b', 'follow the'),
    (r'\benterthe\b', 'enter the'),
    (r'\bexitthe\b', 'exit the'),
    (r'\bwiththe\b', 'with the'),
    (r'\bforthe\b', 'for the'),
    (r'\bfromthe\b', 'from the'),
    (r'\btothe\b', 'to the'),
    (r'\bofthe\b', 'of the'),
    (r'\binthe\b', 'in the'),
    (r'\bonthe\b', 'on the'),
    (r'\batthe\b', 'at the'),
    (r'\bbythe\b', 'by the'),
    (r'\basthe\b', 'as the'),
    (r'\bandthe\b', 'and the'),
    (r'\borthe\b', 'or the'),
    (r'\bbutthe\b', 'but the'),
    (r'\bifthe\b', 'if the'),
    (r'\bwhenthe\b', 'when the'),
    (r'\bwherethe\b', 'where the'),
    (r'\bwhilethe\b', 'while the'),
    (r'\bbeforethe\b', 'before the'),
    (r'\bafterthe\b', 'after the'),
    (r'\baboutthe\b', 'about the'),
    (r'\bacrossthe\b', 'across the'),
    (r'\bagainstthe\b', 'against the'),
    (r'\bduringthe\b', 'during the'),
    (r'\bbetweenthe\b', 'between the'),
    (r'\bthroughthe\b', 'through the'),
    (r'\bunderthe\b', 'under the'),
    (r'\boverthe\b', 'over the'),
    (r'\bintothea\b', 'into the'),
    
    # More run-on fixes
    (r'\bthe\s*re\b', 'there'),
    (r'\bmo\s*re\b', 'more'),
    (r'\bwhe\s*re\b', 'where'),
    (r'\bthe\s*se\b', 'these'),
    (r'\bthe\s*ir\b', 'their'),
    (r'\bthe\s*y\b', 'they'),
    (r'\bthe\s*m\b', 'them'),
    (r'\bthe\s*n\b', 'then'),
    (r'\bwhe\s*n\b', 'when'),
    (r'\bwhi\s*ch\b', 'which'),
    (r'\bwit\s*h\b', 'with'),
    (r'\bth\s*at\b', 'that'),
    (r'\bth\s*is\b', 'this'),
    (r'\bfro\s*m\b', 'from'),
    (r'\bint\s*o\b', 'into'),
    (r'\bont\s*o\b', 'onto'),
    (r'\bont\s*o\b', 'onto'),
    (r'\babo\s*ut\b', 'about'),
    
    # Explanation specific run-ons
    # FIXED: Use negative lookbehind to avoid breaking words like credit, profit, limit, benefit
    # These patterns should ONLY match true run-ons (e.g., "companyThe", "businessIt")
    # NOT valid words that just happen to end with these letters
    (r'\b([a-z]{3,})(?<!cred)(?<!prof)(?<!lim)(?<!benef)(?<!subm)(?<!aud)(?<!depos)(?<!perm)(?<!un)(?<!exh)(?<!trans)The\b', r'\1 The'),
    (r'\b([a-z]{3,})(?<!cred)(?<!prof)(?<!lim)(?<!benef)(?<!subm)(?<!aud)(?<!depos)(?<!perm)(?<!un)(?<!exh)(?<!trans)This\b', r'\1 This'),
    # REMOVED: These patterns break too many valid words like credit, profit, limit
    # (r'\b([a-z]{3,})It\b', r'\1 It'),  
    # (r'\b([a-z]{3,})If\b', r'\1 If'),
    (r'\b([a-z]{3,})(?<!cred)(?<!prof)(?<!lim)(?<!benef)(?<!subm)When\b', r'\1 When'),
    (r'\b([a-z]{3,})However\b', r'\1 However'),
    (r'\b([a-z]{3,})Therefore\b', r'\1 Therefore'),
    (r'\b([a-z]{3,})(?<!cred)(?<!prof)For\b', r'\1 For'),
    # REMOVED: Pattern for 'As' breaks words like purchase, release
    # (r'\b([a-z]{3,})As\b', r'\1 As'),
    

    # -ation split fixes
    (r'\borganiz\s*ation\b', 'organization'),
    (r'\binform\s*ation\b', 'information'),
    (r'\bcommuni\s*cation\b', 'communication'),
    (r'\bpresent\s*ation\b', 'presentation'),
    (r'\bdocument\s*ation\b', 'documentation'),
    (r'\bimplementa\s*tion\b', 'implementation'),
    (r'\bregistr\s*ation\b', 'registration'),
    (r'\bconsider\s*ation\b', 'consideration'),
    (r'\bevalua\s*tion\b', 'evaluation'),
    (r'\bnegocia\s*tion\b', 'negotiation'),
    (r'\bnegocia\s*tions\b', 'negotiations'),
    (r'\bdemonstrat\s*ion\b', 'demonstration'),
    (r'\bcompensa\s*tion\b', 'compensation'),
    (r'\btransport\s*ation\b', 'transportation'),
    (r'\bclassifica\s*tion\b', 'classification'),
    (r'\brecommend\s*ation\b', 'recommendation'),
    (r'\bexplana\s*tion\b', 'explanation'),
    
    # -ating split fixes
    (r'\bdemonstrat\s*ing\b', 'demonstrating'),
    (r'\bparticipat\s*ing\b', 'participating'),
    (r'\bcommunicat\s*ing\b', 'communicating'),
    (r'\bnegotiat\s*ing\b', 'negotiating'),
    (r'\boperating\b', 'operating'),
    (r'\breveal\s*ing\b', 'revealing'),
    (r'\bincorporat\s*ing\b', 'incorporating'),
    (r'\billustr\s*ating\b', 'illustrating'),
    
    # -ment split fixes
    (r'\bmanage\s*ment\b', 'management'),
    (r'\bdevelop\s*ment\b', 'development'),
    (r'\benviron\s*ment\b', 'environment'),
    (r'\bequip\s*ment\b', 'equipment'),
    (r'\bdocu\s*ment\b', 'document'),
    (r'\bstate\s*ment\b', 'statement'),
    (r'\binvest\s*ment\b', 'investment'),
    (r'\brequire\s*ment\b', 'requirement'),
    (r'\bachieve\s*ment\b', 'achievement'),
    (r'\bemploy\s*ment\b', 'employment'),
    (r'\bassess\s*ment\b', 'assessment'),
    (r'\badvance\s*ment\b', 'advancement'),
    (r'\bagree\s*ment\b', 'agreement'),
    (r'\bpay\s*ment\b', 'payment'),
    (r'\bship\s*ment\b', 'shipment'),
    (r'\btreat\s*ment\b', 'treatment'),
    (r'\bdepart\s*ment\b', 'department'),
    (r'\breplace\s*ment\b', 'replacement'),
    (r'\bsettle\s*ment\b', 'settlement'),
    
    # -ness split fixes
    (r'\bbusi\s*ness\b', 'business'),
    (r'\baware\s*ness\b', 'awareness'),
    (r'\beffective\s*ness\b', 'effectiveness'),
    (r'\bwilling\s*ness\b', 'willingness'),
    (r'\bfair\s*ness\b', 'fairness'),
    (r'\bweak\s*ness\b', 'weakness'),
    (r'\bopen\s*ness\b', 'openness'),
    (r'\bhappy\s*ness\b', 'happiness'),
    (r'\breadiness\b', 'readiness'),
    
    # -ity split fixes  
    (r'\bresponsibil\s*ity\b', 'responsibility'),
    (r'\bopportun\s*ity\b', 'opportunity'),
    (r'\bactiv\s*ity\b', 'activity'),
    (r'\bqual\s*ity\b', 'quality'),
    (r'\bquant\s*ity\b', 'quantity'),
    (r'\babil\s*ity\b', 'ability'),
    (r'\bsecur\s*ity\b', 'security'),
    (r'\bauthor\s*ity\b', 'authority'),
    (r'\bcommun\s*ity\b', 'community'),
    (r'\bperson\s*ality\b', 'personality'),
    (r'\bflex\s*ibility\b', 'flexibility'),
    
    # -ally split fixes
    (r'\bbasic\s*ally\b', 'basically'),
    (r'\bessential\s*ly\b', 'essentially'),
    (r'\bprofession\s*ally\b', 'professionally'),
    (r'\bperson\s*ally\b', 'personally'),
    (r'\bfinanci\s*ally\b', 'financially'),
    (r'\btypic\s*ally\b', 'typically'),
    (r'\bspecific\s*ally\b', 'specifically'),
    (r'\belectric\s*ally\b', 'electrically'),
    
    # youwill and similar
    (r'\byouwill\b', 'you will'),
    (r'\byoucan\b', 'you can'),
    (r'\byoumay\b', 'you may'),
    (r'\byoumight\b', 'you might'),
    (r'\byoushould\b', 'you should'),
    (r'\byouwould\b', 'you would'),
    (r'\byoucould\b', 'you could'),
    (r'\byouhave\b', 'you have'),
    (r'\byouare\b', 'you are'),
    (r'\btheywill\b', 'they will'),
    (r'\btheycan\b', 'they can'),
    (r'\btheymay\b', 'they may'),
    (r'\btheyhave\b', 'they have'),
    (r'\btheyare\b', 'they are'),
    (r'\bwewill\b', 'we will'),
    (r'\bwecan\b', 'we can'),
    (r'\bwemay\b', 'we may'),
    (r'\bwehave\b', 'we have'),
    (r'\bweare\b', 'we are'),
    (r'\bitwill\b', 'it will'),
    (r'\bitcan\b', 'it can'),
    (r'\bitmay\b', 'it may'),
    (r'\bitis\b', 'it is'),
    (r'\bitwas\b', 'it was'),
    
    # preventrisk and similar compound errors
    (r'\bpreventrisk\b', 'prevent risk'),
    
    # Space after hyphen fix (word- something -> word-something)
    (r'(\w)-\s+(\w)', r'\1-\2'),
]


COMMON_FIXES = [(re.compile(p, re.IGNORECASE), r) for p, r in COMMON_FIXES_RAW]

# Compile ADDITIONAL_FIXES case-sensitively since patterns contain explicit case.
# COMMON_FIXES (compiled with IGNORECASE) already handles case-insensitive matching.
ADDITIONAL_FIXES = [(re.compile(p), r) for p, r in ADDITIONAL_FIXES_RAW]
def _fix_broken_words(text: str) -> str:
    # Skip empty or very short strings (like "A", "Yes")
    if not text or len(text) < 4: return text
    
    # =========================================================================
    # 1. FIX COMMON SPLIT WORDS (highest impact - 140k+ fixes)
    # =========================================================================
    # Quick early exit if text doesn't have spaces (no broken words possible)
    if ' ' not in text:
        return text
    
    # Fix spacing after common explanation labels (Run this FIRST to separate words)
    if ':' in text:
        text = re.sub(r'\b(SOURCE|Rationale|Answer|Note):([^\s])', r'\1: \2', text, flags=re.IGNORECASE)
        text = re.sub(r'\bSOURC\s*E\b', 'SOURCE', text)
        text = re.sub(r'\bSOURCE\s+:\s*', 'SOURCE: ', text)

    # Apply all pattern fixes
    for pattern, replacement in COMMON_FIXES:
        text = pattern.sub(replacement, text)
    
    # =========================================================================
    # 2. FIX HYPHENATION ISSUES (11k+ fixes)
    # =========================================================================
    # Fix "word -word" → "word-word"
    text = re.sub(r'(\w)\s+-(\w)', r'\1-\2', text)
    # Fix "word- word" → "word-word"  
    text = re.sub(r'(\w)-\s+(\w)', r'\1-\2', text)
    # Fix "word - word" → "word-word"
    text = re.sub(r'(\w)\s+-\s+(\w)', r'\1-\2', text)
    
    # =========================================================================
    # 3. FIX PUNCTUATION SPACING (1.3k+ fixes)
    # =========================================================================
    # Fix "word,word" -> "word, word"
    text = re.sub(r'(\w),(\w)', r'\1, \2', text)
    
    # Remove space before punctuation
    text = re.sub(r'\s+([.,;:!?])', r'\1', text)
    # Ensure space after punctuation (but not in URLs or numbers)
    text = re.sub(r'([.!?])([A-Z])', r'\1 \2', text)
    
    
    # =========================================================================
    # 4. FIX DOUBLE/MULTIPLE SPACES
    # =========================================================================
    text = re.sub(r'\s{2,}', ' ', text)
    
    # =========================================================================
    # 4.5. FIX POSSESSIVE/CONTRACTION MISSING SPACES (5k+ issues)
    # =========================================================================
    # Fix patterns like "business'slegal" → "business's legal"
    # Fix patterns like "isn'tshe" → "isn't she"
    # Fix patterns like "don'tget" → "don't get"
    
    # Possessive 's followed by lowercase letter (need space)
    text = re.sub(r"(\w+)'s([a-z])", r"\1's \2", text)
    
    # Force capitalization after specific labels
    def cap_after_label(m):
        return m.group(1) + ": " + m.group(2).upper()
    text = re.sub(r'\b(SOURCE|Rationale|Answer|Note):\s*([a-z])', cap_after_label, text, flags=re.IGNORECASE)
    
    # Contraction 't followed by lowercase letter (need space) - e.g., isn't, don't, won't
    text = re.sub(r"(\w+)'t([a-z])", r"\1't \2", text)
    
    # Contraction 've followed by lowercase letter (need space) - e.g., would've
    text = re.sub(r"(\w+)'ve([a-z])", r"\1've \2", text)
    
    # Contraction 're followed by lowercase letter (need space) - e.g., they're
    text = re.sub(r"(\w+)'re([a-z])", r"\1're \2", text)
    
    # Contraction 'll followed by lowercase letter (need space) - e.g., they'll
    text = re.sub(r"(\w+)'ll([a-z])", r"\1'll \2", text)
    
    # Contraction 'd followed by lowercase letter (need space) - e.g., they'd
    text = re.sub(r"(\w+)'d([a-z])", r"\1'd \2", text)
    
    # =========================================================================
    # 4.6. FIX ADDITIONAL BROKEN WORDS (found in analysis)
    # =========================================================================
    # Used global ADDITIONAL_FIXES
    
    for pattern, replacement in ADDITIONAL_FIXES:
        text = pattern.sub(replacement, text)
    
    # =========================================================================
    # 5. GENERAL SPLIT WORD FIX (remaining cases)
    # =========================================================================
    # Valid small words that should NOT be merged
    valid_short = {
        'a', 'i', 'am', 'an', 'as', 'at', 'be', 'by', 'do', 'go', 'he', 'if', 
        'in', 'is', 'it', 'me', 'my', 'no', 'of', 'on', 'or', 'so', 'to', 'up', 
        'us', 'we', 'a.', 'b.', 'c.', 'd.', 'e.', 'vs', 'ok', 'th'
    }
    
    # Common prefixes that look like short words but should merge with following text
    merge_prefixes = {'re', 'ex', 'un', 'in', 'im', 'ir', 'il', 'de', 'en', 'em', 'co'}
    
    def merge_prefix_careful(match):
        p, w = match.group(1), match.group(2)
        p_lower = p.lower()
        # Special case: "th" + vowel-starting word is almost always "the" + word
        # (e.g., "th emethods" → should stay as "th emethods" not merge to "themethods")
        if p_lower == 'th' and w[0].lower() in 'aeiou':
            return match.group(0)
        # Don't merge if it would create a camelCase run-on (e.g., "th" + "eProject")
        if len(p) <= 2 and w[0].islower():
            merged = p + w
            if any(c.isupper() for c in merged[1:]):
                return match.group(0)
        # Always merge known word-forming prefixes when followed by 4+ chars
        if p_lower in merge_prefixes and len(w) >= 4:
            return p + w
        if p_lower in valid_short: 
            return match.group(0)
        return p + w

    # Merge isolated 1-2 chars followed by 3+ chars (e.g., "th eir" → "their")
    # Added (?<!') to prevent merging possessives like "owner's invention" -> "owner'sinvention"
    text = re.sub(r"(?<!')\b([a-zA-Z]{1,2})\s+([a-zA-Z]{3,})\b", merge_prefix_careful, text)
    
    # Known common words formed by single-letter + following text
    # Used to decide if a trailing single letter starts a new word or is a broken suffix
    _common_words_by_start = {
        'h': {'has', 'his', 'her', 'him', 'had', 'have', 'how', 'here', 'held', 'he'},
        'w': {'was', 'with', 'will', 'were', 'why', 'when', 'what', 'who', 'way', 'would', 'want', 'we'},
        't': {'the', 'this', 'that', 'then', 'they', 'them', 'there', 'those', 'thus', 'their', 'to'},
    }
    
    def merge_suffix_smart(match):
        w, s, next_word = match.group(1), match.group(2), match.group(3)
        full_text = match.group(0)
        
        if s.lower() in valid_short: 
            return full_text
        # Don't merge with answer options A-E
        if s in {'A','B','C','D','E'}: 
            return full_text
            
        # For single char suffixes, use CONTEXT to decide
        if len(s) == 1:
            letter = s.lower()
            if letter in _common_words_by_start and next_word:
                # Check if letter + next_word forms a known common word
                candidate = letter + next_word.lower()
                if candidate in _common_words_by_start[letter]:
                    # The single letter IS the start of a real word (e.g., "h" + "as" = "has")
                    # Don't merge it with the preceding fragment
                    return full_text
            # Safe to merge - it's a broken word suffix
            # (but still only merge known word-ending characters)
            if letter not in {'s', 'd', 'r', 'n', 't', 'l', 'e', 'h', 'k', 'p', 'g', 'm', 'w', 'y', 'f', 'c', 'x'}: 
                return full_text
        
        # For 2-char suffixes, keep existing logic
        if len(s) == 2 and s.lower() in valid_short:
            return full_text
        
        # Merge: reconstruct without the space between w and s, but keep next_word separate
        if next_word:
            return w + s + ' ' + next_word
        return w + s

    # Merge 2+ chars followed by isolated 1-2 chars (e.g., "wit h" → "with")
    # Now captures the NEXT word too for context-aware merging decisions
    text = re.sub(r'\b([a-zA-Z]{2,})\s+([a-zA-Z]{1,2})(?:\s+([a-zA-Z]+))?\b', merge_suffix_smart, text)

    # After merging, re-apply run-on word splitting to catch newly-created run-ons
    # e.g., "th" + "emethods" merged to "themethods" → should be "the methods"
    text = _RUNON_RE.sub(r'\1 \2', text)
    
    # Fix remaining "th e..." patterns: "th" + vowel-starting word = "the" + word
    # (e.g., "th esame" → "the same", "th emethods" → "the methods")
    text = re.sub(r'\bth\s+e([a-z]{2,})\b', lambda m: 'the ' + m.group(1), text, flags=re.IGNORECASE)

    
    # =========================================================================
    # 6. UNIVERSAL FALLBACK: Catch remaining run-on patterns
    # =========================================================================
    # Catch any word ending in 'the' that should be 'word the'
    # But exclude actual words ending in 'the' like 'breathe', 'loathe', 'clothe'
    real_the_words = {'breathe', 'loathe', 'clothe', 'soothe', 'bathe', 'tithe', 'scythe', 'writhe', 'blithe'}
    
    def split_wordthe(match):
        word = match.group(0)
        if word.lower() in real_the_words:
            return word
        # Split before 'the'
        base = word[:-3]
        if len(base) >= 2:  # Only split if base word is at least 2 chars
            return base + ' the'
        return word
    
    text = re.sub(r'\b[a-zA-Z]{4,}the\b', split_wordthe, text, flags=re.IGNORECASE)
    
    # =========================================================================
    # 7. FINAL CLEANUP
    # =========================================================================
    # One more pass for double spaces that may have been created
    text = re.sub(r'\s{2,}', ' ', text)
    
    # Final cleanup for specific edge cases (Must be last)
    text = re.sub(r'SOURCE:\s*Http', 'SOURCE: http', text)
    text = re.sub(r'Note:\s*this', 'Note: This', text, flags=re.IGNORECASE)

    return text.strip()


def _parse_answer_key(lines: List[str]) -> Dict[int, Dict[str, str]]:
    start_idx = -1
    
    # Try explicit headers first
    for i in range(len(lines) - 1, -1, -1):
        if re.search(r"answer\s*(key|section)", lines[i], re.IGNORECASE):
            start_idx = i
            break
            
    if start_idx == -1:
         for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip().upper() == "KEY":
                start_idx = i
                break

    # If no header, use sequence detection (robust)
    if start_idx == -1:
        # Scan from 10% to find "1. X" followed by "2. Y"
        search_start = int(len(lines) * 0.1)
        pat_num = re.compile(r"^\s*(\d{1,3})\s*[:.-]?\s*([A-E])\b", re.IGNORECASE)
        
        for i in range(search_start, len(lines)):
            m = pat_num.match(lines[i])
            if m and int(m.group(1)) == 1:
                # Potential start, verify sequence
                # Look for 2, 3 in next 50 lines
                found_next = False
                cur_next = 2
                look_ahead_range = 50
                for j in range(i + 1, min(i + look_ahead_range * cur_next, len(lines))):
                     m2 = pat_num.match(lines[j])
                     if m2:
                         num_found = int(m2.group(1))
                         if num_found == cur_next:
                             cur_next += 1
                             if cur_next > 3: # Found 1, 2, 3 - confident
                                 found_next = True
                                 break
                
                if found_next:
                    start_idx = i
                    break

    # Last resort fallback
    if start_idx == -1:
        start_idx = max(0, int(len(lines) * 0.8))

    answers = {}
    # Strict pattern for answer key line: Number + Sep + Letter + Explanation
    pattern = re.compile(r"^\s*(\d{1,3})\s*[:.-]?\s*([A-E])\b\s*(.*)", re.IGNORECASE)
    
    i = start_idx
    while i < len(lines):
        line = lines[i]
        # skip header lines in the key section
        if _looks_like_header_line(line) or "answer key" in line.lower():
            i += 1
            continue
            
        match = pattern.search(line)
        if match:
            num = int(match.group(1))
            let = match.group(2).upper()
            expl = match.group(3).strip()
            
            # Simple multiline capture for explanation
            i += 1
            while i < len(lines):
                next_line = lines[i]
                # Stop if next line looks like new answer or header
                if pattern.search(next_line) or _looks_like_header_line(next_line):
                    break
                expl += " " + _fix_broken_words(next_line.strip())
                i += 1
                
            if 1 <= num <= 100:
                answers[num] = {"letter": let, "explanation": _fix_broken_words(expl)}
        else:
            i += 1
            
    return answers

def _smart_parse_questions(lines: List[str], answers: Dict[int, Any]) -> List[Dict[str, Any]]:
    questions = []
    current_q = None
    last_q_num = 0
    
    # Enhanced regex patterns
    q_start_re = re.compile(r"^(\d{1,3})\s*[).:\-]\s+(.*)")
    # Allow (A) or A) or A. - Ensures letter is always in group 1
    # CHANGED: \s* instead of \s+ for the content part to handle 'A.Text'
    opt_start_re = re.compile(r"^\s*\(?([A-E])(?:[).:\-]|\))\s*(.*)")
    # Inline options: (A) ... (B) ... - Ensures letter is always in group 1
    inline_opt_re = re.compile(r"(?:\s{2,}|\s+)\(?([A-E])(?:[).:\-]|\))\s*")
    
    answer_key_entry_re = re.compile(r"^(\d{1,3})\s*[).:\-]\s*([A-E])\s*$", re.IGNORECASE)

    def finalize_current():
        nonlocal current_q, last_q_num
        if current_q:
            # First normalize standard whitespace
            prompt = _normalize_whitespace(current_q["prompt"])
            # Then fix broken word splits
            current_q["prompt"] = _fix_broken_words(prompt)
            
            # Clean up options
            cleaned_opts = []
            for opt in current_q["options"]:
                text = _normalize_whitespace(opt["text"])
                text = _fix_broken_words(text)
                cleaned_opts.append({"label": opt["label"], "text": text})
            current_q["options"] = cleaned_opts
            
            # Ensure we have a valid question number
            questions.append(current_q)
            last_q_num = current_q["number"]
        current_q = None

    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        
        # Stop at explicit answer key header
        if line.lower().strip() == "answer key":
             break
        
        # Stop if we hit answer key entries (e.g., "1. A" with nothing else)
        if answer_key_entry_re.match(line):
            # Check if next few lines also look like answer key entries
            is_answer_key = True
            for j in range(i, min(i + 3, len(lines))):
                if not answer_key_entry_re.match(lines[j]) and lines[j].strip():
                    is_answer_key = False
                    break
            if is_answer_key and last_q_num >= 50:  # Only if we're decently far into the test
                break

        q_match = q_start_re.match(line)
        if q_match:
            num = int(q_match.group(1))
            if not (1 <= num <= 100):
                # If number is crazy high, probably not a question
                # But if it's close to expected, might be valid
                pass
            
            text = q_match.group(2).strip()
            # Skip if this looks like an answer key entry
            if re.match(r"^[A-E]$", text, re.IGNORECASE):
                continue
                
            finalize_current()
            current_q = {
                "number": num,
                "prompt": text,
                "options": []
            }
            continue

        opt_match = opt_start_re.match(line)
        if opt_match:
            label = opt_match.group(1).upper()
            text = opt_match.group(2)
            
            # If option text is empty, check next line
            if not text.strip() and i < len(lines):
                 next_line = lines[i]
                 if not opt_start_re.match(next_line) and not q_start_re.match(next_line):
                      text = next_line
                      i += 1
            
            # Handle orphan option A - implies we missed the question start
            if current_q and label == "A" and any(o["label"] == "A" for o in current_q["options"]):
                # We already have an A, so this must be a new question
                prev_num = current_q["number"]
                finalize_current()
                
                # Infer the new question number
                new_num = prev_num + 1
                current_q = {
                    "number": new_num,
                    "prompt": "[Prompt text missing/merged]",
                    "options": []
                }
            
            if not current_q:
                # Starting fresh with an option but no question context
                if label == "A":
                    inferred_num = last_q_num + 1 if last_q_num > 0 else 1
                    current_q = {
                        "number": inferred_num,
                        "prompt": "[Prompt text missing/merged]",
                        "options": []
                    }
                else:
                    # Non-A option without context - try to attach to previous question if it exists in list
                    if questions and questions[-1]["options"] and questions[-1]["options"][-1]["label"] < label:
                         # Re-open last question
                         current_q = questions.pop()
                         last_q_num = current_q["number"] - 1 # Reset last_q_num temporarily
                    else:
                        continue

            current_q["options"].append({"label": label, "text": text})
            
            # ---------------------------------------------------------
            # Handle inline options (e.g. "A. Text B. Text ...")
            # ---------------------------------------------------------
            # We want to split by pattern " (B) " or " B. "
            # Our regex finds the *separators*.
            
            # We already have the first part (label A + text).
            # Now check if that 'text' contains subsequent options.
            
            def split_inline_options(full_text):
                # Find all occurrences of option patterns
                matches = list(inline_opt_re.finditer(full_text))
                if not matches:
                    return None
                
                parts = []
                last_end = 0
                
                # First chunk matches the initial option text (from opt_match)
                # But wait, opt_match group(2) includes EVERYTHING after "A. "
                
                for idx, m in enumerate(matches):
                    # Text before this match is the content of the previous option
                    content = full_text[last_end:m.start()].strip()
                    parts.append(content)
                    
                    # Next label
                    lbl = m.group(1).upper()
                    if idx == len(matches) - 1:
                        # Last match, content is everything after
                        content = full_text[m.end():].strip()
                        parts.append((lbl, content))
                    else:
                        # Store label to pair with next content
                        parts.append((lbl, None)) # Placeholder
                        
                    last_end = m.end()
                
                # If we had matches, we need to reconstruct
                # The first 'part' belongs to the option we are currently processing (e.g. A)
                # The subsequent parts are new options (B, C, ...)

            # Attempt to split the text we just found
            # Note: opt_match gave us the text for the current label
            inline_parts = []
            
            # Iterate to find hidden options
            cursor = 0
            # Look for " B. " or " (B) " inside text
            found_split = False
            
            # Special logic: The text might contain "B. something".
            
            found_opts = list(inline_opt_re.finditer(text))
            if found_opts:
                # The text for the *current* extracted option (e.g. A) ends at the start of the next option
                first_opt_text = text[:found_opts[0].start()].strip()
                current_q["options"][-1]["text"] = first_opt_text
                
                # Now add the others
                for j, m in enumerate(found_opts):
                    lbl = m.group(1).upper()
                    
                    # Content is from end of this match to start of next match (or end of string)
                    start_content = m.end()
                    if j < len(found_opts) - 1:
                        end_content = found_opts[j+1].start()
                    else:
                        end_content = len(text)
                    
                    val = text[start_content:end_content].strip()
                    current_q["options"].append({"label": lbl, "text": val})
            
            continue

        # Continuation line - append to current context
        if current_q:
            # CRITICAL: Don't append lines that are clearly answer key section
            lower_line = line.lower().strip()
            is_answer_section = False
            
            # If we already have Q100 parsed, stop appending anything new
            if current_q.get("number") == 100 and len(current_q.get("options", [])) >= 4:
                is_answer_section = True
            
            # Markers that indicate we've hit the answer key/footer section
            answer_markers = [
                'center®', 'columbus', 'mba research', 'key', 'copyright', 
                'dba mba', 'herein is', 'test item', 'individual items',
                'specifically authorized', 'source:', 'retrieved august',
                'retrieved september', 'retrieved october', 'retrieved november',
                'retrieved december', 'retrieved january', 'retrieved february',
                'contract law', 'constitutional law', 'probate', 'patent',
                ').', ']. ', 'http://', 'https://'
            ]
            
            for marker in answer_markers:
                if marker in lower_line:
                    is_answer_section = True
                    break
            
            # Also check if line looks like an answer key entry (e.g., "1. D")
            if not is_answer_section and answer_key_entry_re.match(line):
                is_answer_section = True
            
            # Skip blank lines
            if not is_answer_section and not line.strip():
                is_answer_section = True
            
            if not is_answer_section:
                if current_q["options"]:
                    # Check if this line is actually a new question start that regex missed?
                    # e.g. "12. " without text? No, regex handles that.
                    # Just append to last option
                    current_q["options"][-1]["text"] += " " + line
                else:
                    current_q["prompt"] += " " + line

    finalize_current()



    final_questions = []
    seen_ids = set()
    
    for q in questions:
        num = q["number"]
        if num in seen_ids: continue
        
        # Sort options by label
        q["options"].sort(key=lambda x: x["label"])
        
        # Ensure we have A, B, C, D
        labels = [o["label"] for o in q["options"]]
        if labels:
            expected_labels = ['A','B','C','D']
            
            new_options = []
            current_src_idx = 0
            
            # We want to fill exactly 4 slots if possible, or more if E exists
            # Find max label present to know how far to go
            max_label_idx = 3 # Default to D
            for l in labels:
                if l in expected_labels:
                    max_label_idx = max(max_label_idx, expected_labels.index(l))
                elif l == 'E':
                    max_label_idx = max(max_label_idx, 4)

            target_count = max(4, max_label_idx + 1)
            
            # Filter valid options
            valid_src_options = {o["label"]: o for o in q["options"]}
            
            final_opt_list = []
            for i in range(target_count):
                if i < len(expected_labels):
                    lbl = expected_labels[i]
                else:
                    lbl = chr(ord('A') + i)
                
                if lbl in valid_src_options:
                    final_opt_list.append(valid_src_options[lbl]["text"])
                else:
                    final_opt_list.append("[Option missing from PDF]")
            
        else:
            final_opt_list = ["[Option missing]" for _ in "ABCD"]
        
        # Answer matching
        ans_data = answers.get(num)
        ans_letter = ans_data["letter"] if ans_data else None
        explanation = ans_data["explanation"] if ans_data else ""
        
        correct_idx = -1
        if ans_letter:
            # Map letter to index 0-3
            if len(ans_letter) == 1 and 'A' <= ans_letter <= 'E':
                 correct_idx = ord(ans_letter) - ord('A')
                 
        q_id = f"q-{num}"
        
        final_questions.append({
            "id": q_id,
            "number": num,
            "question": q["prompt"],
            "options": final_opt_list,
            "correct_index": correct_idx,
            "correct_letter": ans_letter if ans_letter else "?",
            "explanation": explanation if explanation else "No explanation available (Parse failed)"
        })
        seen_ids.add(num)
        
    return final_questions

# Cache for parsed PDFs - keyed by (file_path, mtime) to invalidate on change
_pdf_cache: Dict[str, Dict[str, Any]] = {}

def _parse_pdf_source(source: Path | IO[bytes], name_hint: str) -> Dict[str, Any]:
    # Check cache for file sources
    cache_key = None
    if isinstance(source, Path):
        try:
            mtime = source.stat().st_mtime
            cache_key = f"{source}:{mtime}"
            if cache_key in _pdf_cache:
                return copy.deepcopy(_pdf_cache[cache_key])
        except:
            pass
    
    try:
        lines = _extract_clean_lines(source)
        answers = _parse_answer_key(lines)
        questions = _smart_parse_questions(lines, answers)

        
        questions.sort(key=lambda x: x["number"])
        
        test_id = re.sub(r"[^a-z0-9]+", "-", name_hint.lower()).strip("-")
        if not test_id:
            test_id = f"test-{uuid.uuid4().hex[:8]}"
            
        for q in questions:
            q["id"] = f"{test_id}-q{q['number']}"

        result = {
            "id": test_id,
            "name": name_hint,
            "description": "",
            "questions": questions,
            "question_count": len(questions)
        }
        
        # Cache the result
        if cache_key:
            _pdf_cache[cache_key] = result
            
        return copy.deepcopy(result)
    except Exception as e:

        
        app.logger.error(f"PDF parsing error for '{name_hint}': {e}", exc_info=True)
        logger.warning(f"Parsing error for '{name_hint}': {e}")
        return {}

def _sanitize_questions(questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sanitized = []
    for q in questions:
        q_copy = dict(q)
        for key in ("correct_index", "correct_letter", "explanation"):
            q_copy.pop(key, None)
        sanitized.append(q_copy)
    return sanitized

def _get_session_id() -> str:
    if "sid" not in session:
        session["sid"] = uuid.uuid4().hex
    return session["sid"]

def _get_session_data_db(sid: str) -> Dict[str, Any]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT data FROM sessions WHERE id = ?", (sid,))
            row = cur.fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        app.logger.error(f"DB Read Error: {e}")
    return {"uploads": {}, "missed": {}}

def _save_session_data_db(sid: str, data: Dict[str, Any]):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO sessions (id, data, updated_at) VALUES (?, ?, ?)",
                         (sid, json.dumps(data), time.time()))
            conn.commit()
    except Exception as e:
        app.logger.error(f"DB Write Error: {e}")

def _load_session_data(sid: str) -> Dict[str, Any]:
    return _get_session_data_db(sid)

def _save_session_data(sid: str, data: Dict[str, Any]):
    _save_session_data_db(sid, data)

def _track_started_quiz(sid: str, test_id: str, question_ids: List[str]):
    data = _load_session_data(sid)
    quiz_access = data.setdefault("quiz_access", {})
    quiz_access[test_id] = {
        "questions": question_ids,
        "attempted": [],
        "revealed": [],
        "updated_at": time.time(),
    }
    _save_session_data(sid, data)

def _mark_attempted_question(sid: str, test_id: str, question_id: str):
    data = _load_session_data(sid)
    quiz_access = data.setdefault("quiz_access", {}).setdefault(test_id, {"questions": [], "attempted": [], "revealed": [], "updated_at": time.time()})
    if question_id not in quiz_access.get("attempted", []):
        quiz_access["attempted"].append(question_id)
    if question_id not in quiz_access.get("revealed", []):
        quiz_access["revealed"].append(question_id)
    quiz_access["updated_at"] = time.time()
    _save_session_data(sid, data)

def _question_allowed_for_session(sid: str, test_id: str, question_id: str) -> bool:
    data = _load_session_data(sid)
    quiz_access = data.get("quiz_access", {}).get(test_id, {})
    questions = set(quiz_access.get("questions", []))
    return question_id in questions

def _answer_revealed_for_session(sid: str, test_id: str, question_id: str) -> bool:
    data = _load_session_data(sid)
    quiz_access = data.get("quiz_access", {}).get(test_id, {})
    revealed = set(quiz_access.get("revealed", []))
    return question_id in revealed

def _cleanup_old_sessions():
    """Delete sessions and files older than 7 days"""
    try:
        now = time.time()
        # 7 days retention
        max_age = 7 * 24 * 3600 
        cutoff = now - max_age
        
        # 1. Clean DB Sessions
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
            deleted = conn.total_changes
            conn.commit()
            if deleted > 0:
                app.logger.info(f"Cleaned up {deleted} expired sessions (>7 days)")

        # 2. Clean PDF Files in TESTS_DIR
        if TESTS_DIR.exists():
            count = 0
            for item in TESTS_DIR.glob("*.pdf"):
                try:
                    stats = item.stat()
                    # Check modification time
                    if stats.st_mtime < cutoff:
                        item.unlink()
                        count += 1
                except Exception as e:
                    app.logger.error(f"Error deleting old file {item}: {e}")
            
            if count > 0:
                app.logger.info(f"Deleted {count} old PDF files (>7 days)")
                
    except Exception as e:
        app.logger.error(f"Error during cleanup: {e}")

def _get_all_tests_for_session(force_refresh=False) -> Dict[str, Any]:
    all_tests = {}
    
    global _STATIC_TESTS_CACHE
    if force_refresh:
        _STATIC_TESTS_CACHE.clear()

    if not _STATIC_TESTS_CACHE:
        for p in tests_dir_iter():
            parsed = _parse_pdf_source(p, p.stem)
            if parsed and parsed.get("questions"):
                _STATIC_TESTS_CACHE[parsed["id"]] = parsed
    
    all_tests.update(_STATIC_TESTS_CACHE)
    
    sid = _get_session_id()
    s_data = _load_session_data(sid)
    all_tests.update(s_data.get("uploads", {}))
    
    return all_tests

def tests_dir_iter():
    try:
        return TESTS_DIR.glob("*.pdf")
    except Exception as e:
        app.logger.warning(f"Failed to list tests directory: {e}")
        return []

_STATIC_TESTS_CACHE = {}

@app.route("/")
def home():
    sid = _get_session_id()
    _load_session_data(sid)
    csrf_token = _get_csrf_token()
    nonce = secrets.token_urlsafe(16)
    csp = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "object-src 'none'"
    )
    response = app.make_response(render_template("index.html", default_random_order=DEFAULT_RANDOM_ORDER, csrf_token=csrf_token, csp_nonce=nonce))
    response.headers["Content-Security-Policy"] = csp
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

@app.route("/settings")
def settings():
    return redirect(f"{url_for('home')}#/settings", code=302)

@app.route("/api/tests")
def list_tests():
    force = request.args.get("reload") == "1"
    data = _get_all_tests_for_session(force_refresh=force)
    payload = []
    for t in data.values():
        payload.append({
            "id": t["id"],
            "name": t["name"],
            "description": t.get("description", ""),
            "question_count": len(t.get("questions", []))
        })
    return jsonify(payload)

@app.route("/api/tests/<test_id>/questions")
def get_questions(test_id):
    all_t = _get_all_tests_for_session()
    test = all_t.get(test_id)
    if not test:
        abort(404, "Test not found")
        
    qs = test["questions"]
    count = request.args.get("count", type=int)
    if count and count > 0:
        qs = qs[:min(count, MAX_QUESTIONS_PER_RUN)]
    
    sanitized_questions = _sanitize_questions(qs)
        
    return jsonify({
        "test": {"id": test["id"], "name": test["name"], "total": len(test["questions"])},
        "questions": sanitized_questions,
        "selected_count": len(qs)
    })

@app.route("/api/tests/<test_id>/start_quiz", methods=["POST"])
def start_quiz(test_id):
    all_t = _get_all_tests_for_session()
    test = all_t.get(test_id)
    if not test:
        abort(404, "Test not found")
        
    payload = request.json or {}
    mode = payload.get("mode", "regular")
    
    # Log activity
    ip = _get_client_ip()
    logger.info(f"TEST STARTED: {_safe_log_value(test_id)} | Mode: {_safe_log_value(mode)} | IP: {_safe_log_value(ip)}")

    count = payload.get("count")
    
    questions = test["questions"]
    
    if mode == "review_incorrect":
        sid = _get_session_id()
        s_data = _load_session_data(sid)
        missed_ids = set(s_data.get("missed", {}).get(test_id, []))
        questions = [q for q in questions if q["id"] in missed_ids]
        if not questions:
            abort(400, "No missed questions recording for this test.")
            
    if count and isinstance(count, int) and count > 0:
        questions = questions[:min(count, MAX_QUESTIONS_PER_RUN)]
        
    try:
        limit = int(payload.get("time_limit_seconds", 0))
        if limit > MAX_TIME_LIMIT_MINUTES * 60:
            limit = MAX_TIME_LIMIT_MINUTES * 60
    except (ValueError, TypeError):
        limit = 0
        

    sanitized_questions = _sanitize_questions(questions)
    sid = _get_session_id()
    _track_started_quiz(sid, test_id, [q["id"] for q in questions])

    return jsonify({
        "test": {"id": test["id"], "name": test["name"], "total": len(test["questions"])},
        "questions": sanitized_questions,
        "selected_count": len(questions),
        "mode": mode,
        "time_limit_seconds": limit
    })

@app.route("/api/upload_pdf", methods=["POST"])
def upload_pdf():
    f = request.files.get("file")
    if not f: abort(400, "No file")

    safe_filename = _safe_log_value(f.filename)
    logger.info(f"TEST UPLOADED: {safe_filename} | IP: {_safe_log_value(_get_client_ip())}")
    
    raw = f.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        abort(413, "Too large")
    try:
        reader = PdfReader(io.BytesIO(raw))
        if len(reader.pages) > MAX_PDF_PAGES:
            abort(413, f"PDF has too many pages (max {MAX_PDF_PAGES}).")
    except HTTPException:
        raise
    except Exception:
        abort(400, "Invalid PDF file")
        
    parsed = _parse_pdf_source(io.BytesIO(raw), f.filename.replace(".pdf", ""))
    if not parsed or not parsed.get("questions"):
        abort(400, "Could not parse questions from PDF")
        
    sid = _get_session_id()
    uid = f"u-{uuid.uuid4().hex[:8]}"
    parsed["id"] = uid
    parsed["name"] = f.filename
    
    for q in parsed["questions"]:
        q["id"] = f"{uid}-q{q['number']}"
    
    data = _load_session_data(sid)
    if "uploads" not in data:
        data["uploads"] = {}
    data["uploads"][uid] = parsed
    _save_session_data(sid, data)
    
    return jsonify({
        "id": uid,
        "name": parsed["name"],
        "description": parsed.get("description", ""),
        "questions": parsed["questions"],
        "question_count": len(parsed["questions"]),
        "test": {"id": uid, "name": parsed["name"], "total": len(parsed["questions"])}
    })

@app.route("/api/tests/<test_id>/check/<question_id>", methods=["POST"])
def check_answer(test_id, question_id):
    all_t = _get_all_tests_for_session()
    test = all_t.get(test_id)
    if not test: abort(404, "Test not found")
    
    q = next((x for x in test["questions"] if x["id"] == question_id), None)
    if not q: abort(404, "Question not found")
    sid = _get_session_id()
    if not _question_allowed_for_session(sid, test_id, question_id):
        abort(403, "Question not available in current quiz session")
    
    if not request.json: abort(400, "JSON body required")
    choice = request.json.get("choice")
    if choice is None: abort(400, "Choice required")
    
    is_correct = (choice == q["correct_index"])
    _mark_attempted_question(sid, test_id, question_id)
    return jsonify({"correct": is_correct})

@app.route("/api/tests/<test_id>/results", methods=["POST"])
def store_results(test_id):
    sid = _get_session_id()
    payload = request.get_json(silent=True) or {}
    results = payload.get("results", [])
    if not results: return jsonify({"missed_count": 0})
    
    missed_ids = []
    for r in results:
        if r and r.get("correct") is False:
            missed_ids.append(r.get("question_id"))
            
    data = _load_session_data(sid)
    if "missed" not in data: data["missed"] = {}
    
    data["missed"][test_id] = missed_ids
    _save_session_data(sid, data)
    
    return jsonify({"missed_count": len(missed_ids)})

@app.route("/api/tests/<test_id>/answer/<question_id>")
def get_answer_details(test_id, question_id):
    all_t = _get_all_tests_for_session()
    test = all_t.get(test_id)
    if not test: abort(404)
    q = next((x for x in test["questions"] if x["id"] == question_id), None)
    if not q: abort(404)
    sid = _get_session_id()
    if not _question_allowed_for_session(sid, test_id, question_id):
        abort(403, "Question not available in current quiz session")
    if not _answer_revealed_for_session(sid, test_id, question_id):
        abort(403, "Answer not yet available for this question")
    
    return jsonify({
        "correct_index": q["correct_index"],
        "correct_letter": q["correct_letter"],
        "explanation": q["explanation"]
    })

@app.errorhandler(HTTPException)
def handle_exception(e):
    """Return JSON instead of HTML for HTTP errors."""
    return jsonify({
        "error": e.name,
        "message": e.description,
    }), e.code

@app.errorhandler(Exception)
def handle_generic_exception(e):
    """Return JSON for all unhandled exceptions."""
    app.logger.error(f"Unhandled exception: {e}", exc_info=True)
    return jsonify({
        "error": "Internal Server Error",
        "message": "An unexpected server error occurred.",
    }), 500

@app.after_request
def apply_security_headers(response):
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
