import hmac
import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Any
from urllib.parse import urlencode

import psycopg2
import psycopg2.extras
import requests
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("crm-expert-connect-gateway")
partner_request_lock = Lock()
partner_next_request_at = 0.0

HIDDEN_SYSTEM_MESSAGES = {
    "api access marked this issue as resolve",
    "api access marked this issue as resolved",
}

app = FastAPI(title="CRM Expert Connect Gateway", version="1.0.0")


class MessagePayload(BaseModel):
    client_id: int
    contact_id: int
    contact_name: str
    phone: str
    text: str
    team_id: str | None = None
    # Con ticket_id se responde dentro de ese ticket abierto en lugar de abrir uno nuevo.
    ticket_id: str | None = None


class DocumentPayload(BaseModel):
    """AGRONORTE-500: a sales document (Presupuesto / Factura proforma) sent from ventas-be.

    The PDF travels as a real attachment of the same public note that carries the
    text; the text also includes the public link because, outside the 24-hour
    window, Expert Connect wraps the note in its own WhatsApp template.
    """

    phone: str
    contact_name: str
    text: str
    team_id: str
    tag_name: str | None = None
    file_url: str
    file_name: str | None = None
    external_ref: str


class CloseTicketPayload(BaseModel):
    ticket_id: str
    phone: str
    # Motivo de cierre opcional: las etiquetas de Cobranzas no tienen subetiquetas.
    tag_id: str | None = None


class ReadNotificationsPayload(BaseModel):
    phone: str
    area_id: str


def setting(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def postgres_connection() -> Any:
    """Expert Connect runs on the GCP VM over Cloud SQL Postgres (the MariaDB
    data was migrated into the vps4_agronorte_prod schema). Connect like the
    migrated services: DATABASE_URL (SQLAlchemy style is accepted) or
    DB_POSTGRES_*. The search_path points to the migrated schema
    (CRM_DB_SCHEMA, vps4_agronorte_prod by default) and the session is UTC,
    because every timestamp written here is UTC."""
    options = f"-c search_path={setting('CRM_DB_SCHEMA', 'vps4_agronorte_prod')},public -c TimeZone=UTC"
    url = setting("DATABASE_URL")
    if url.lower().startswith("postgres"):
        dsn = re.sub(r"^postgres(ql)?(\+[a-z0-9_]+)?://", "postgresql://", url, flags=re.IGNORECASE)
        connection = psycopg2.connect(dsn, options=options, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        connection = psycopg2.connect(
            host=setting("DB_POSTGRES_HOST"),
            port=int(setting("DB_POSTGRES_PORT", "5432")),
            user=setting("DB_POSTGRES_USER"),
            password=setting("DB_POSTGRES_PASSWORD"),
            dbname=setting("DB_POSTGRES_NAME"),
            options=options,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    connection.autocommit = True
    return connection


@contextmanager
def database_connection() -> Any:
    """Autocommit connection with dict rows, always closed on exit (psycopg2
    connections do not close themselves in a `with`)."""
    connection = postgres_connection()
    try:
        yield connection
    finally:
        connection.close()


# Small SQL helpers shared by the queries below.
def phone_tail_sql(expression: str) -> str:
    """Last 10 digits of a phone column ('g' strips every non-digit)."""
    return f"RIGHT(REGEXP_REPLACE({expression}, '[^0-9]', '', 'g'), 10)"


def text_cast(expression: str) -> str:
    """CAST to TEXT (ids are returned as strings to the CRM)."""
    return f"CAST({expression} AS TEXT)"


def upsert_sql(conflict_column: str, columns: list[str]) -> str:
    """ON CONFLICT … DO UPDATE with the new values of `columns`."""
    return f"ON CONFLICT ({conflict_column}) DO UPDATE SET " + ", ".join(f"{column} = EXCLUDED.{column}" for column in columns)


def insert_ignore_sql(table: str, columns: list[str], conflict_column: str) -> str:
    """INSERT … ON CONFLICT DO NOTHING (idempotent journal rows)."""
    placeholders = ", ".join(["%s"] * len(columns))
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT ({conflict_column}) DO NOTHING"


def record_crm_message(note_id: str, ticket_id: str, phone: str, text: str) -> None:
    """Journal of messages sent from the CRM/Ventas (the conversation history
    merges it with the bot messages). Idempotent per Expert Connect note."""
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                insert_ignore_sql("crm_expert_connect_messages", ["external_note_id", "ticket_id", "phone_tail", "text", "created_at"], "external_note_id"),
                (note_id, ticket_id, phone[-10:], text, datetime.now(timezone.utc).replace(tzinfo=None)),
            )


def require_gateway_access(authorization: str = Header(default="")) -> None:
    expected = setting("CRM_EXPERT_CONNECT_GATEWAY_TOKEN") or setting("EXPERT_CONNECT_GATEWAY_TOKEN")
    received = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not expected or not received or not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Gateway authentication failed.")


def phone_number(phone: str) -> str:
    tail = phone_tail(phone)
    if len(tail) != 10:
        raise HTTPException(status_code=422, detail="Invalid phone.")
    return f"549{tail}"


def phone_tail(phone: str) -> str:
    return "".join(re.findall(r"\d", str(phone or "")))[-10:]


def occurred_at_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        # Partner notes and the stored timestamps are UTC.
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def occurred_at_json(value: Any) -> str:
    return occurred_at_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def customer_name(name: str) -> tuple[str, str]:
    parts = str(name or "").strip().split(maxsplit=1)
    return (parts[0] if parts else "Cliente", parts[1] if len(parts) == 2 else "CRM")


def credentials() -> dict[str, str]:
    # The migration kept the camel-case column, which Postgres only finds quoted.
    query = """
        SELECT company_id_expert_connect, base_url_expert_connect,
               token_expert_connect, "accessToken" AS access_token, x_subscription_expert_connect
        FROM parametrosaplicacion
        WHERE x_subscription_expert_connect IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
    """
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            row = cursor.fetchone() or {}
    token = str(row.get("token_expert_connect") or row.get("access_token") or "").strip()
    base_url = str(row.get("base_url_expert_connect") or setting("BASE_URL_EXPERT_CONNECT") or "").strip()
    subscription_key = str(row.get("x_subscription_expert_connect") or "").strip()
    company_id = str(row.get("company_id_expert_connect") or "").strip()
    if not all((token, base_url, subscription_key, company_id)):
        raise RuntimeError("Expert Connect credentials are incomplete.")
    return {
        "base_url": base_url.rstrip("/"),
        "token": token,
        "subscription_key": subscription_key,
        "company_id": company_id,
    }


def expert_connect_request(
    method: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the Partner API serialized behind the shared rate-limit lock.

    `files` switches to a multipart upload (only `/api/v1/media` uses it): the
    provider then requires the `CompanyId` header and the body is not JSON.
    """
    global partner_next_request_at
    with partner_request_lock:
        for attempt in range(3):
            delay = partner_next_request_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            current = credentials()
            headers = {
                "Authorization": f"Bearer {current['token']}",
                "X-Subscription-Key": current["subscription_key"],
            }
            if files:
                headers["CompanyId"] = current["company_id"]
            else:
                headers["Content-Type"] = "application/json"
            response = requests.request(
                method,
                f"{current['base_url']}/{endpoint.lstrip('/')}",
                headers=headers,
                json=None if files else payload,
                files=files,
                timeout=25,
            )
            partner_next_request_at = time.monotonic() + 0.5
            if response.status_code != 429:
                break
            retry_after = response.headers.get("Retry-After", "")
            try:
                retry_delay = float(retry_after)
            except ValueError:
                retry_delay = float(attempt + 1)
            partner_next_request_at = time.monotonic() + min(max(retry_delay, 0.5), 10.0)
            logger.warning("Expert Connect rate limited endpoint=%s retry=%s", endpoint, attempt + 1)
        if not response.ok:
            logger.warning("Expert Connect request failed method=%s endpoint=%s status=%s", method, endpoint, response.status_code)
            raise HTTPException(status_code=502, detail="Expert Connect could not accept the message.")
    if not response.content:
        return {}
    data = response.json()
    return data if isinstance(data, dict) else {}


def response_data(value: dict[str, Any]) -> dict[str, Any]:
    data = value.get("data") or value.get("Data")
    return data if isinstance(data, dict) else value


def ticket_is_closed(value: Any) -> bool:
    """Expert Connect uses numeric status 2 for closed/resolved tickets; status 1 is not terminal."""
    return str(value or "").strip().lower() in {"2", "closed", "resolved", "cerrado"}


def visible_message_text(value: Any) -> bool:
    return str(value or "").strip().lower() not in HIDDEN_SYSTEM_MESSAGES


def suppress_ticket_conversation(ticket_id: str, phone: str) -> tuple[str, str]:
    area_id = setting("CRM_EXPERT_CONNECT_AREA_ID", "Ventas de Maquinaria")
    conversation_status = "advisor_handoff_ignored"
    if not area_id:
        raise HTTPException(status_code=503, detail="CRM Expert Connect area is not configured.")
    conversation_key = f"expert_connect:{ticket_id}"
    try:
        with database_connection() as connection:
            with connection.cursor() as cursor:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                cursor.execute(
                    """
                    INSERT INTO chatbot_conversations (
                        conversation_key, channel, external_conversation_id,
                        participant_phone, area_id, status, last_message_at, updated_at
                    ) VALUES (%s, 'expert_connect', %s, %s, %s, %s, %s, %s)
                    """ + upsert_sql("conversation_key", ["external_conversation_id", "participant_phone", "area_id", "status", "last_message_at", "updated_at"]),
                    (conversation_key, ticket_id, phone, area_id, conversation_status, now, now),
                )
    except Exception as exc:
        logger.exception("Could not suppress CRM-created Expert Connect ticket ticket_id=%s", ticket_id)
        raise HTTPException(status_code=502, detail="Could not register the CRM-created ticket routing state.") from exc
    return area_id, conversation_status


def live_ticket_notes(ticket_ids: list[str]) -> list[dict[str, Any]]:
    current = credentials()
    company_id = current["company_id"]
    notes: list[dict[str, Any]] = []
    for ticket_id in ticket_ids[-3:]:
        notes_query = urlencode({"Paging.Page": 1, "Paging.Size": 100})
        try:
            response = expert_connect_request(
                "GET",
                f"/api/v1/companies/{company_id}/tickets/{ticket_id}/notes?{notes_query}",
            )
        except HTTPException:
            logger.exception("Live Expert Connect notes unavailable ticket_id=%s", ticket_id)
            continue
        for item in response.get("items") or []:
            note = response_data(item)
            note_id = str(note.get("id") or "").strip()
            if note_id and not bool(note.get("isPrivate")) and not bool(note.get("isDraft")) and visible_message_text(note.get("text")):
                notes.append({"ticket_id": ticket_id, "note_id": note_id, "note": note})
    return notes


def ticket_notes(ticket_id: str, customer_phone: str = "") -> list[dict[str, Any]]:
    current = credentials()
    result: list[dict[str, Any]] = []
    page = 1
    seen_note_ids: set[str] = set()
    while True:
        query = urlencode({"Paging.Page": page, "Paging.Size": 100})
        payload = expert_connect_request("GET", f"/api/v1/companies/{current['company_id']}/tickets/{ticket_id}/notes?{query}")
        items = payload.get("items") or []
        new_items = 0
        for item in items:
            note = response_data(item)
            note_id = str(note.get("id") or "")
            if note_id and note_id in seen_note_ids:
                continue
            if note_id:
                seen_note_ids.add(note_id)
            new_items += 1
            if bool(note.get("isPrivate")) or bool(note.get("isDraft")) or not visible_message_text(note.get("text")):
                continue
            owner = note.get("owner") if isinstance(note.get("owner"), dict) else {}
            is_customer = bool(customer_phone) and phone_tail(str(owner.get("phone") or "")) == phone_tail(customer_phone)
            result.append({
                "id": str(note.get("id") or ""), "external_id": str(note.get("id") or ""),
                "ticket_id": ticket_id, "occurred_at": occurred_at_json(note.get("createdDate")),
                "direction": "entrante" if is_customer else "saliente",
                "sender_type": "user" if is_customer else "agent", "content_type": "text",
                "text": str(note.get("text") or ""), "template_name": None,
            })
        if len(items) < 100:
            break
        if new_items == 0:
            break
        page += 1
    return sorted(result, key=lambda item: item["occurred_at"])


def stored_summary(ticket_id: str) -> dict[str, Any]:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT summary, generation_status FROM crm_expert_connect_ticket_summaries WHERE ticket_id=%s", (ticket_id,))
            return cursor.fetchone() or {}


def ticket_ids_for_phone(phone: str) -> list[str]:
    """Use the locally synchronized conversation index instead of the partner's unbounded ticket listing."""
    tail = phone_tail(phone)
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT COALESCE(
                    NULLIF(external_conversation_id, ''),
                    REGEXP_REPLACE(conversation_key, '^.*:', '')
                ) AS ticket_id
                FROM chatbot_conversations
                WHERE channel = 'expert_connect'
                  AND participant_phone LIKE %s
                ORDER BY ticket_id
                """,
                (f"%{tail}",),
            )
            return [str(row.get("ticket_id") or "").strip() for row in cursor.fetchall() if row.get("ticket_id")]


SUMMARY_PROMPT_VERSION = "crm-cobranzas-v2"
SUMMARY_INSTRUCTIONS = """Sos un asistente especializado en resumir conversaciones de cobranza entre clientes y Agronorte.
Recibís un ticket cerrado de Expert Connect, la plataforma de WhatsApp de Agronorte. En origen, chatbot_conversations identifica la conversación y el teléfono participante; chatbot_messages contiene sus mensajes. direction=in indica que escribió el cliente y direction=out que escribió Agronorte. Dentro de los salientes, un owner humano es advisor y una automatización es bot. crm_expert_connect_messages registra los envíos manuales iniciados desde el CRM y también representan mensajes de Agronorte. El input ya resolvió esas relaciones y normalizó cada mensaje: participant_role=client es el cliente; advisor es una persona de Agronorte; bot es una automatización. Notas privadas, borradores y eventos técnicos ya fueron excluidos.
Generá una sola oración verificable, profesional y de hasta 45 palabras. Indicá motivo, gestión de Agronorte y resultado o próximo paso solo si aparecen expresamente. Priorizá documentos, vencimientos, pagos, transferencias, comprobantes, reclamos, compromisos, fechas y derivaciones. No inventes ni confundas una promesa con un pago realizado, al bot con el cliente, o falta de respuesta con rechazo."""


def normalized_transcript(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    transcript: list[dict[str, str]] = []
    for message in messages:
        sender = str(message.get("sender_type") or "").lower()
        role = "client" if message.get("direction") == "entrante" else ("advisor" if sender in {"agent", "advisor"} else "bot")
        transcript.append({"occurred_at": str(message.get("occurred_at") or ""), "participant_role": role, "content": str(message.get("text") or "")})
    return transcript


def response_output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") == "output_text":
                return str(content.get("text") or "")
    return ""


def valid_summary(value: str) -> bool:
    """Enforce the timeline contract even if the model ignores its instructions."""
    words = value.split()
    terminal_marks = len(re.findall(r"[.!?](?:\s|$)", value))
    return bool(words) and len(words) <= 45 and terminal_marks <= 1


def summarize_closed_ticket(ticket_id: str, messages: list[dict[str, Any]], closed_at: Any) -> dict[str, Any]:
    transcript = normalized_transcript(messages)
    serialized = json.dumps(transcript, ensure_ascii=False, separators=(",", ":"))
    content_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT content_hash, summary, generation_status FROM crm_expert_connect_ticket_summaries WHERE ticket_id=%s", (ticket_id,))
            cached = cursor.fetchone() or {}
    if cached.get("content_hash") == content_hash and cached.get("generation_status") == "generated":
        return cached
    api_key = setting("OPENAI_API_KEY")
    model = setting("OPENAI_SUMMARY_MODEL", "gpt-4.1-mini")
    status_value, summary, sufficient, error_text = "pending", None, None, None
    if api_key and transcript:
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model, "store": False, "instructions": SUMMARY_INSTRUCTIONS,
                    "input": serialized, "max_output_tokens": 180,
                    "text": {"format": {"type": "json_schema", "name": "ticket_summary", "strict": True,
                        "schema": {"type": "object", "properties": {"summary": {"type": "string"}, "evidence_sufficient": {"type": "boolean"}}, "required": ["summary", "evidence_sufficient"], "additionalProperties": False}}},
                },
                timeout=45,
            )
            response.raise_for_status()
            parsed = json.loads(response_output_text(response.json()))
            summary = " ".join(str(parsed.get("summary") or "").split())
            sufficient = bool(parsed.get("evidence_sufficient"))
            if valid_summary(summary):
                status_value = "generated"
            else:
                status_value, error_text, summary = "failed", "OpenAI summary violated the one-sentence/45-word contract", None
        except Exception as exc:
            status_value, error_text = "failed", str(exc)[:1000]
            logger.exception("OpenAI ticket summary failed ticket_id=%s", ticket_id)
    elif not transcript:
        status_value, error_text = "failed", "Ticket has no public messages"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    closed_value = occurred_at_utc(closed_at).replace(tzinfo=None) if closed_at else None
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO crm_expert_connect_ticket_summaries
                    (ticket_id, content_hash, summary, evidence_sufficient, message_count, model, prompt_version, generation_status, generation_error, ticket_closed_at, generated_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """ + upsert_sql("ticket_id", ["content_hash", "summary", "evidence_sufficient", "message_count", "model", "prompt_version", "generation_status", "generation_error", "ticket_closed_at", "generated_at", "updated_at"]), (ticket_id, content_hash, summary, sufficient, len(transcript), model, SUMMARY_PROMPT_VERSION, status_value, error_text, closed_value, now if status_value == "generated" else None, now))
    return {"summary": summary, "generation_status": status_value}


def sync_ticket_summary(ticket_id: str, phone: str, closed_at: Any) -> None:
    """Generate outside the listing response so OpenAI latency never blocks the CRM timeline."""
    summarize_closed_ticket(ticket_id, ticket_notes(ticket_id, phone), closed_at)


# Las llamadas al proveedor van de a una (medio segundo entre cada una): repetir
# el detalle de cada ticket en cada relectura del CRM las encolaba hasta superar
# el timeout. Un detalle reciente se reutiliza por unos segundos.
TICKET_DETAIL_CACHE_SECONDS = 15
ticket_detail_cache: dict[str, tuple[float, dict[str, Any]]] = {}
ticket_detail_cache_lock = Lock()


def ticket_detail(company_id: str, ticket_id: str, max_age: float = TICKET_DETAIL_CACHE_SECONDS) -> dict[str, Any]:
    """Expert Connect ticket detail, reusing a copy younger than `max_age` seconds (0 = always fresh)."""
    now = time.monotonic()
    with ticket_detail_cache_lock:
        cached = ticket_detail_cache.get(ticket_id)
    if cached and max_age > 0 and now - cached[0] < max_age:
        return cached[1]
    detail = response_data(expert_connect_request("GET", f"/api/v1/companies/{company_id}/tickets/{ticket_id}"))
    with ticket_detail_cache_lock:
        ticket_detail_cache[ticket_id] = (time.monotonic(), detail)
    return detail


UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Un ticket que el proveedor no devuelve se saltea sin consultarlo durante una
# hora: cada consulta tarda ~0,6 s y varios juntos superan el timeout del LB.
UNREADABLE_TICKET_SECONDS = 3600
unreadable_tickets: dict[str, float] = {}


def ticket_recently_unreadable(ticket_id: str) -> bool:
    with ticket_detail_cache_lock:
        failed_at = unreadable_tickets.get(ticket_id)
    return failed_at is not None and time.monotonic() - failed_at < UNREADABLE_TICKET_SECONDS


def remember_unreadable_ticket(ticket_id: str) -> None:
    with ticket_detail_cache_lock:
        unreadable_tickets[ticket_id] = time.monotonic()


@app.get("/internal/crm/expert-connect/tickets")
def tickets(background_tasks: BackgroundTasks, phone: str = Query(min_length=10, max_length=40), include_messages: bool = False,
            ticket_id: str | None = Query(default=None, max_length=100),
            _: None = Depends(require_gateway_access)) -> dict[str, list[dict[str, Any]]]:
    """Return open and closed CRM-owned tickets, filtered by canonical team and tag IDs.

    With `ticket_id` only that ticket of the phone is read (the conversation
    modal), instead of every ticket's detail and notes.
    """
    current = credentials()
    normalized_phone = phone_number(phone)
    team_id = setting("CRM_EXPERT_CONNECT_TEAM_ID")
    tag_id = setting("CRM_EXPERT_CONNECT_TAG_ID")
    output: list[dict[str, Any]] = []
    phone_ticket_ids = ticket_ids_for_phone(normalized_phone)
    requested = str(ticket_id or "").strip()
    if requested:
        phone_ticket_ids = [requested] if requested in phone_ticket_ids else []
    for ticket_id in phone_ticket_ids:
        # Los tickets reales de Expert Connect son UUID; los de pruebas (demo-,
        # e2e-) quedaron en el historial y no existen en el proveedor.
        if not UUID_PATTERN.match(ticket_id) or ticket_recently_unreadable(ticket_id):
            continue
        try:
            detail = ticket_detail(current["company_id"], ticket_id, 5 if include_messages else TICKET_DETAIL_CACHE_SECONDS)
        except HTTPException as exc:
            # Un ticket que el proveedor ya no devuelve (por ejemplo, una charla
            # donde el número participa como asesor) no debe ocultar el resto.
            logger.warning("Skipping unreadable Expert Connect ticket ticket_id=%s status=%s", ticket_id, exc.status_code)
            remember_unreadable_ticket(ticket_id)
            continue
        team = detail.get("team") if isinstance(detail.get("team"), dict) else {}
        tags = [response_data(tag) for tag in (detail.get("tags") or []) if isinstance(tag, dict)]
        if str(team.get("id") or "") != team_id or tag_id not in {str(tag.get("id") or "") for tag in tags}:
            continue
        status_value = detail.get("status")
        closed = ticket_is_closed(status_value)
        saved = stored_summary(ticket_id)
        messages = ticket_notes(ticket_id, normalized_phone) if include_messages else []
        if closed and saved.get("generation_status") != "generated":
            background_tasks.add_task(sync_ticket_summary, ticket_id, normalized_phone, detail.get("updatedDate"))
        output.append({
            "ticket_id": ticket_id, "status": "closed" if closed else "open",
            "created_at": occurred_at_json(detail.get("createdDate")),
            "updated_at": occurred_at_json(detail.get("updatedDate")),
            "closed_at": occurred_at_json(detail.get("updatedDate")) if closed else None,
            "summary": saved.get("summary"),
            "summary_status": saved.get("generation_status") or ("pending" if closed else "waiting_for_close"),
            "team": {"id": str(team.get("id") or ""), "name": str(team.get("name") or "")},
            "tags": [{"id": str(tag.get("id") or ""), "name": str(tag.get("name") or "")} for tag in tags],
            **({"messages": messages} if include_messages else {}),
        })
    return {"tickets": sorted(output, key=lambda item: item["updated_at"], reverse=True)}


POSTGRES_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS crm_expert_connect_messages (
        id BIGSERIAL PRIMARY KEY,
        external_note_id VARCHAR(255) NOT NULL UNIQUE,
        ticket_id VARCHAR(255) NOT NULL,
        phone_tail VARCHAR(10) NOT NULL,
        text TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS crm_expert_connect_messages_phone_created ON crm_expert_connect_messages (phone_tail, created_at)",
    """
    CREATE TABLE IF NOT EXISTS crm_expert_connect_notification_reads (
        conversation_id BIGINT NOT NULL PRIMARY KEY,
        read_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS crm_expert_connect_notification_reads_date ON crm_expert_connect_notification_reads (read_at)",
    """
    CREATE TABLE IF NOT EXISTS crm_expert_connect_ticket_summaries (
        ticket_id VARCHAR(255) NOT NULL PRIMARY KEY,
        content_hash CHAR(64) NOT NULL,
        summary TEXT NULL,
        evidence_sufficient BOOLEAN NULL,
        message_count INT NOT NULL DEFAULT 0,
        model VARCHAR(100) NULL,
        prompt_version VARCHAR(30) NOT NULL,
        generation_status VARCHAR(30) NOT NULL,
        generation_error TEXT NULL,
        ticket_closed_at TIMESTAMPTZ NULL,
        generated_at TIMESTAMPTZ NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS crm_ec_summary_status ON crm_expert_connect_ticket_summaries (generation_status, updated_at)",
)

def create_journal_table() -> None:
    """Create the gateway's own tables. The MariaDB→Postgres migration did not
    bring them; they are created in the migrated schema (first in the
    search_path). Idempotent."""
    with database_connection() as connection:
        with connection.cursor() as cursor:
            for statement in POSTGRES_TABLES:
                cursor.execute(statement)


@app.on_event("startup")
def startup() -> None:
    create_journal_table()


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/internal/crm/expert-connect/conversations")
def conversations(
    phone: str = Query(min_length=10, max_length=40),
    area_id: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=100, ge=1, le=200),
    _: None = Depends(require_gateway_access),
) -> dict[str, list[dict[str, Any]]]:
    tail = phone_number(phone)[-10:]
    query = f"""
        SELECT id, external_id, ticket_id, occurred_at, direction, sender_type, text
        FROM (
            SELECT
                {text_cast("m.id")} AS id,
                COALESCE(m.external_message_id, '') AS external_id,
                COALESCE(c.external_conversation_id, '') AS ticket_id,
                m.created_at AS occurred_at,
                CASE WHEN m.direction = 'in' THEN 'entrante' ELSE 'saliente' END AS direction,
                CASE WHEN m.direction = 'in' THEN 'user' ELSE 'bot' END AS sender_type,
                COALESCE(m.text, '') AS text
            FROM chatbot_conversations AS c
            JOIN chatbot_messages AS m ON m.conversation_id = c.id
            WHERE (
                {phone_tail_sql("COALESCE(c.participant_phone, '')")} = %s
                OR {phone_tail_sql("COALESCE(m.sender_phone, '')")} = %s
            )
              AND c.area_id = %s
              AND LOWER(TRIM(COALESCE(m.text, ''))) NOT IN (
                'api access marked this issue as resolve',
                'api access marked this issue as resolved'
              )
            UNION ALL
            SELECT
                CONCAT('crm-', id) AS id,
                external_note_id AS external_id,
                ticket_id,
                created_at AS occurred_at,
                'saliente' AS direction,
                'advisor' AS sender_type,
                text
            FROM crm_expert_connect_messages
            WHERE phone_tail = %s
        ) AS messages
        ORDER BY occurred_at DESC, id DESC
        LIMIT %s
    """
    try:
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, (tail, tail, area_id, tail, limit))
                rows = cursor.fetchall()
    except Exception as exc:
        logger.exception("CRM conversation lookup failed")
        raise HTTPException(status_code=502, detail="Expert Connect history is unavailable.") from exc
    messages_by_identity = {
        (str(row.get("ticket_id") or ""), str(row.get("external_id") or f"local-{row['id']}")): row
        for row in rows
    }
    ticket_ids = list(
        dict.fromkeys(
            str(row.get("ticket_id") or "").strip()
            for row in sorted(rows, key=lambda row: str(row.get("occurred_at") or ""))
            if str(row.get("ticket_id") or "").strip()
        )
    )
    try:
        for live in live_ticket_notes(ticket_ids):
            note = live["note"]
            note_id = live["note_id"]
            owner = note.get("owner") if isinstance(note.get("owner"), dict) else {}
            is_customer = phone_tail(str(owner.get("phone") or "")) == tail
            identity = (live["ticket_id"], note_id)
            message = messages_by_identity.get(identity, {})
            message.update({
                "id": f"expert-connect-{note_id}",
                "external_id": note_id,
                "ticket_id": live["ticket_id"],
                "occurred_at": occurred_at_json(note.get("createdDate")),
                "direction": "entrante" if is_customer else "saliente",
                "sender_type": "user" if is_customer else "agent",
                "text": str(note.get("text") or message.get("text") or ""),
            })
            messages_by_identity[identity] = message
    except Exception:
        # Preserve the stored transcript if Expert Connect is temporarily unavailable.
        logger.exception("Live Expert Connect note lookup failed")
    rows = sorted(
        messages_by_identity.values(),
        key=lambda row: (
            occurred_at_utc(row.get("occurred_at")),
            str(row.get("ticket_id") or ""),
            str(row.get("external_id") or row.get("id") or ""),
        ),
    )[-limit:]
    for row in rows:
        row["occurred_at"] = occurred_at_json(row.get("occurred_at"))
        row["content_type"] = "text"
        row["template_name"] = None
    return {"messages": rows}


@app.get("/internal/crm/expert-connect/notifications")
def notifications(
    area_id: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    _: None = Depends(require_gateway_access),
) -> dict[str, list[dict[str, Any]]]:
    query = f"""
        SELECT
            {text_cast("c.id")} AS conversation_id,
            COALESCE(c.external_conversation_id, '') AS ticket_id,
            {phone_tail_sql("COALESCE(c.participant_phone, m.sender_phone, '')")} AS phone_tail,
            COALESCE(m.text, '') AS text,
            m.created_at AS occurred_at
        FROM chatbot_conversations AS c
        JOIN chatbot_messages AS m ON m.id = (
            SELECT incoming.id
            FROM chatbot_messages AS incoming
            WHERE incoming.conversation_id = c.id
              AND incoming.direction = 'in'
            ORDER BY incoming.created_at DESC, incoming.id DESC
            LIMIT 1
        )
        LEFT JOIN crm_expert_connect_notification_reads AS notification_read
          ON notification_read.conversation_id = c.id
        WHERE c.area_id = %s
          AND LOWER(COALESCE(c.status, '')) <> 'closed'
          AND (notification_read.read_at IS NULL OR m.created_at > notification_read.read_at)
        ORDER BY m.created_at DESC, c.id DESC
        LIMIT %s
    """
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (area_id, limit))
            rows = list(cursor.fetchall())
            latest_incoming_phone = """COALESCE(
                        c.participant_phone,
                        (SELECT incoming.sender_phone
                         FROM chatbot_messages AS incoming
                         WHERE incoming.conversation_id = c.id AND incoming.direction = 'in'
                         ORDER BY incoming.created_at DESC, incoming.id DESC LIMIT 1),
                        ''
                    )"""
            cursor.execute(
                f"""
                SELECT
                    c.id AS conversation_id,
                    c.external_conversation_id AS ticket_id,
                    {phone_tail_sql(latest_incoming_phone)} AS phone_tail,
                    notification_read.read_at
                FROM chatbot_conversations AS c
                LEFT JOIN crm_expert_connect_notification_reads AS notification_read
                  ON notification_read.conversation_id = c.id
                WHERE c.area_id = %s
                  AND LOWER(COALESCE(c.status, '')) <> 'closed'
                  AND COALESCE(c.external_conversation_id, '') <> ''
                ORDER BY GREATEST(
                    COALESCE(notification_read.read_at, '1970-01-01'),
                    COALESCE((
                        SELECT MAX(recent_message.created_at)
                        FROM chatbot_messages AS recent_message
                        WHERE recent_message.conversation_id = c.id
                    ), '1970-01-01')
                ) DESC, c.id DESC
                LIMIT 3
                """,
                (area_id,),
            )
            recent_conversations = cursor.fetchall()

    # Webhooks can lag behind Expert Connect. Recheck recent tickets directly,
    # including conversations that the CRM user has never opened.
    recent_by_ticket = {
        str(item.get("ticket_id") or ""): item
        for item in recent_conversations
        if str(item.get("ticket_id") or "").strip()
    }
    existing_conversations = {str(row.get("conversation_id") or "") for row in rows}
    for live in live_ticket_notes(list(recent_by_ticket)):
        stored = recent_by_ticket.get(live["ticket_id"])
        if not stored or str(stored.get("conversation_id") or "") in existing_conversations:
            continue
        note = live["note"]
        owner = note.get("owner") if isinstance(note.get("owner"), dict) else {}
        if phone_tail(str(owner.get("phone") or "")) != str(stored.get("phone_tail") or ""):
            continue
        if occurred_at_utc(note.get("createdDate")) <= occurred_at_utc(stored.get("read_at")):
            continue
        rows.append({
            "conversation_id": str(stored["conversation_id"]),
            "ticket_id": live["ticket_id"],
            "phone_tail": stored["phone_tail"],
            "text": str(note.get("text") or ""),
            "occurred_at": note.get("createdDate"),
        })
        existing_conversations.add(str(stored["conversation_id"]))
    rows.sort(key=lambda row: occurred_at_utc(row.get("occurred_at")), reverse=True)
    rows = rows[:limit]
    for row in rows:
        row["occurred_at"] = occurred_at_json(row.get("occurred_at"))
    return {"notifications": rows}


@app.post("/internal/crm/expert-connect/notifications/read")
def read_notifications(payload: ReadNotificationsPayload, _: None = Depends(require_gateway_access)) -> dict[str, bool]:
    tail = phone_number(payload.phone)[-10:]
    statement = f"""
        INSERT INTO crm_expert_connect_notification_reads (conversation_id, read_at)
        SELECT DISTINCT c.id, CAST(%s AS TIMESTAMPTZ)
        FROM chatbot_conversations AS c
        LEFT JOIN chatbot_messages AS m ON m.conversation_id = c.id
        WHERE c.area_id = %s
          AND (
            {phone_tail_sql("COALESCE(c.participant_phone, '')")} = %s
            OR {phone_tail_sql("COALESCE(m.sender_phone, '')")} = %s
          )
    """ + upsert_sql("conversation_id", ["read_at"])
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(statement, (datetime.now(timezone.utc).replace(tzinfo=None), payload.area_id, tail, tail))
    return {"read": True}


def find_or_create_contact(company_id: str, phone: str, name: str, external_id: str) -> tuple[str, bool]:
    """Return the Expert Connect contact of `phone`, creating it when missing."""
    contact_query = urlencode({"Paging.Page": 1, "Paging.Size": 100, "Where.Phone[eq]": phone})
    contacts = expert_connect_request("GET", f"/api/v1/companies/{company_id}/contacts?{contact_query}").get("items") or []
    contact_id = str(response_data(contacts[0]).get("id") or "") if contacts else ""
    if contact_id:
        return contact_id, False
    first_name, last_name = customer_name(name)
    contact = expert_connect_request(
        "POST",
        f"/api/v1/companies/{company_id}/contacts",
        {"firstName": first_name, "lastName": last_name, "phone": {"number": phone}, "externalId": external_id},
    )
    contact_id = str(response_data(contact).get("id") or "")
    if not contact_id:
        raise HTTPException(status_code=502, detail="Expert Connect did not return a contact id.")
    return contact_id, True


# Same ceiling the chatbot applies before uploading media (about 31 MiB).
DOCUMENT_MAX_BYTES = 32505856


def download_pdf(file_url: str) -> bytes:
    """Download the public PDF published by ventas-be and make sure it is a PDF."""
    url = str(file_url or "").strip()
    if not url.lower().startswith(("https://", "http://")):
        raise HTTPException(status_code=422, detail="The document URL is invalid.")
    try:
        response = requests.get(url, timeout=20, stream=True)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="The document could not be downloaded.") from exc
    with response:
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f"The document could not be downloaded (HTTP {response.status_code}).")
        content = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            content.extend(chunk)
            if len(content) > DOCUMENT_MAX_BYTES:
                raise HTTPException(status_code=422, detail="The document exceeds the Expert Connect media limit.")
    if not bytes(content[:5]) == b"%PDF-":
        raise HTTPException(status_code=422, detail="The document is not a PDF.")
    return bytes(content)


def upload_pdf_media(content: bytes, file_name: str) -> str:
    """Upload a PDF to `/api/v1/media` and return its mediaId (e.g. `<uuid>.pdf`)."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(file_name or "").strip()) or "documento.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    media = expert_connect_request("POST", "/api/v1/media", files={"file": (name, content, "application/pdf")})
    data = response_data(media)
    media_id = str(data.get("mediaId") or data.get("id") or data.get("guid") or "").strip()
    if not media_id:
        raise HTTPException(status_code=502, detail="Expert Connect did not return a media id.")
    return media_id


def find_tag_path(tags: list[Any], wanted: str, parents: list[str]) -> list[str]:
    """Depth-first search by name through `subTags`; returns the ids from the
    top-level tag down to the match (empty when not found)."""
    for item in tags:
        tag = response_data(item) if isinstance(item, dict) else {}
        tag_id = str(tag.get("id") or "").strip()
        path = parents + [tag_id] if tag_id else parents
        if tag_id and str(tag.get("name") or tag.get("description") or "").strip().casefold() == wanted:
            return path
        found = find_tag_path(tag.get("subTags") or [], wanted, path)
        if found:
            return found
    return []


def tag_ids_by_name(company_id: str, name: str) -> list[str]:
    """Tags are company-wide and hierarchical in Expert Connect: the sales tags
    (Nuevo/Usado con/sin canje, Plan de ahorro) are created by hand as
    sub-tags of VENTAS. Returns the parent chain plus the match, so the ticket
    shows up when filtering by VENTAS or by the specific sub-tag."""
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return []
    query = urlencode({"Paging.Page": 1, "Paging.Size": 100})
    return find_tag_path(expert_connect_request("GET", f"/api/v1/companies/{company_id}/tags?{query}").get("items") or [], wanted, [])


@app.post("/internal/crm/expert-connect/documents")
def send_document(payload: DocumentPayload, _: None = Depends(require_gateway_access)) -> dict[str, Any]:
    """AGRONORTE-500: send a sales document to a customer through Expert Connect.

    The PDF is downloaded and uploaded first, so a broken file never leaves an
    empty ticket behind. Then: contact, a new ticket in the requested team (not
    assigned to any advisor), bot suppression, the sales tag (best effort: the
    tags are created by hand and a missing one must not block the sale) and one
    public note with the text and the PDF. The provider has no idempotency, so
    the caller keeps its own "sending" record and never retries by itself.
    """
    message = str(payload.text or "").strip()
    if not message or len(message) > 1000:
        raise HTTPException(status_code=422, detail="Message text must contain 1 to 1000 characters.")
    team_id = str(payload.team_id or "").strip()
    external_ref = str(payload.external_ref or "").strip()
    if not team_id or not external_ref:
        raise HTTPException(status_code=422, detail="Team and external reference are required.")
    phone = phone_number(payload.phone)
    media_id = upload_pdf_media(download_pdf(payload.file_url), payload.file_name or payload.file_url.rsplit("/", 1)[-1])

    current = credentials()
    company_id = current["company_id"]
    contact_id, contact_created = find_or_create_contact(company_id, phone, payload.contact_name, external_ref)
    ticket = expert_connect_request(
        "POST",
        f"/api/v1/companies/{company_id}/tickets",
        {"title": f"Cotización enviada desde Ventas - {payload.contact_name}"[:250], "description": external_ref[:1000], "customerId": contact_id, "teamId": team_id},
    )
    ticket_id = str(response_data(ticket).get("id") or "")
    if not ticket_id:
        raise HTTPException(status_code=502, detail="Expert Connect did not return a ticket id.")

    try:
        area_id, conversation_status = suppress_ticket_conversation(ticket_id, phone)
        tag_id = ""
        if payload.tag_name:
            try:
                tag_ids = tag_ids_by_name(company_id, payload.tag_name)
                if tag_ids:
                    expert_connect_request("POST", f"/api/v1/companies/{company_id}/tickets/{ticket_id}/tags", {"tagIds": tag_ids})
                    tag_id = tag_ids[-1]
            except HTTPException:
                logger.warning("Sales tag could not be applied ticket_id=%s tag=%s", ticket_id, payload.tag_name)
                tag_id = ""
        note = expert_connect_request(
            "POST",
            f"/api/v1/companies/{company_id}/tickets/{ticket_id}/notes",
            {"isPrivate": False, "isDraft": False, "text": message, "mediaId": media_id},
        )
        note_id = str(response_data(note).get("id") or "")
        if not note_id:
            raise HTTPException(status_code=502, detail="Expert Connect did not return a note id.")
    except HTTPException as exc:
        # The ticket already exists: return it so the caller can audit it and
        # nobody retries blindly into a duplicate conversation.
        raise HTTPException(status_code=exc.status_code, detail={"message": str(exc.detail), "ticket_id": ticket_id}) from exc

    try:
        record_crm_message(note_id, ticket_id, phone, message)
    except Exception:
        # The message was already delivered to Expert Connect; the local CRM
        # index is rebuilt from the provider notes, so this must not fail it.
        logger.exception("Could not index sales document note ticket_id=%s note_id=%s", ticket_id, note_id)
    return {
        "contact_id": contact_id,
        "contact_created": contact_created,
        "ticket_id": ticket_id,
        "team_id": team_id,
        "area_id": area_id,
        "status": conversation_status,
        "tag_id": tag_id,
        "tag_applied": bool(tag_id),
        "note_id": note_id,
        "media_id": media_id,
    }


def crm_ticket_detail(company_id: str, ticket_id: str, max_age: float = 0) -> dict[str, Any] | None:
    """Detail of a ticket owned by the CRM collections flow (canonical team and tag), else None."""
    detail = ticket_detail(company_id, ticket_id, max_age)
    team = detail.get("team") if isinstance(detail.get("team"), dict) else {}
    tag_ids = {str(response_data(tag).get("id") or "") for tag in (detail.get("tags") or []) if isinstance(tag, dict)}
    if str(team.get("id") or "") != setting("CRM_EXPERT_CONNECT_TEAM_ID") or setting("CRM_EXPERT_CONNECT_TAG_ID") not in tag_ids:
        return None
    return detail


def reply_in_ticket(company_id: str, ticket_id: str, phone: str, message: str) -> dict[str, Any]:
    """Post a public note inside an open CRM ticket of this phone.

    Keeps the whole conversation in one ticket (one card in the CRM history).
    A closed or foreign ticket is rejected so the caller decides to open a new one.
    """
    if ticket_id not in ticket_ids_for_phone(phone):
        raise HTTPException(status_code=404, detail="Ticket does not belong to the contact.")
    detail = crm_ticket_detail(company_id, ticket_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Ticket is not a CRM collections ticket.")
    if ticket_is_closed(detail.get("status")):
        raise HTTPException(status_code=409, detail="Ticket is closed.")
    note = expert_connect_request("POST", f"/api/v1/companies/{company_id}/tickets/{ticket_id}/notes", {"isPrivate": False, "text": message})
    note_id = str(response_data(note).get("id") or "")
    if not note_id:
        raise HTTPException(status_code=502, detail="Expert Connect did not return a note id.")
    try:
        record_crm_message(note_id, ticket_id, phone, message)
    except Exception:
        # The note was already delivered; the index is rebuilt from provider notes.
        logger.exception("Could not index CRM reply ticket_id=%s note_id=%s", ticket_id, note_id)
    customer = detail.get("customer") if isinstance(detail.get("customer"), dict) else {}
    return {"contact_id": str(customer.get("id") or ""), "contact_created": False, "ticket_id": ticket_id, "ticket_created": False, "note_id": note_id}


PENDING_LOOKBACK_DAYS = 14
PENDING_CACHE_SECONDS = 5
# Una revisión de un ticket se reutiliza mientras no haya mensajes nuevos.
PENDING_TICKET_TTL_SECONDS = 600
# Tope de tiempo para consultar al proveedor en un pedido; lo que falte se
# completa en los siguientes (bot-nosis-go corta a los 25 s).
PENDING_REFRESH_BUDGET_SECONDS = 12
pending_cache: dict[str, Any] = {"at": 0.0, "items": []}
pending_ticket_state: dict[str, dict[str, Any]] = {}
pending_cache_lock = Lock()


@app.get("/internal/crm/expert-connect/pending")
def pending_replies(_: None = Depends(require_gateway_access)) -> dict[str, list[dict[str, Any]]]:
    """Open CRM collections tickets whose latest public note is the customer's.

    Unlike notifications this does not depend on read marks: a ticket leaves
    the list once Agronorte answers it or it is closed. The local index only
    says which CRM conversations (they carry participant_phone) had activity:
    its `direction` is not reliable (customer replies inside a CRM ticket are
    stored as outgoing agent messages), so Expert Connect notes decide who
    wrote last. Each ticket is re-read only when its latest message changes.
    """
    with pending_cache_lock:
        if time.monotonic() - pending_cache["at"] < PENDING_CACHE_SECONDS:
            return {"pending": pending_cache["items"]}
        query = f"""
            SELECT
                c.external_conversation_id AS ticket_id,
                {phone_tail_sql("c.participant_phone")} AS phone_tail,
                MAX(m.created_at) AS last_at
            FROM chatbot_conversations AS c
            JOIN chatbot_messages AS m ON m.conversation_id = c.id
            WHERE c.channel = 'expert_connect'
              AND c.area_id = %s
              AND COALESCE(c.participant_phone, '') <> ''
              AND COALESCE(c.external_conversation_id, '') <> ''
            GROUP BY c.external_conversation_id, c.participant_phone
            HAVING MAX(m.created_at) > NOW() - (%s * INTERVAL '1 day')
            ORDER BY MAX(m.created_at) DESC
            LIMIT 200
        """
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, (setting("CRM_EXPERT_CONNECT_AREA_ID", "Ventas de Maquinaria"), PENDING_LOOKBACK_DAYS))
                candidates = list(cursor.fetchall())
        company_id = credentials()["company_id"]
        started = time.monotonic()
        items: list[dict[str, Any]] = []
        for row in candidates:
            ticket_id = str(row.get("ticket_id") or "").strip()
            tail = str(row.get("phone_tail") or "")
            last_at = occurred_at_json(row.get("last_at"))
            state = pending_ticket_state.get(ticket_id)
            fresh = (
                state is not None
                and state["last_at"] == last_at
                and time.monotonic() - state["checked"] < PENDING_TICKET_TTL_SECONDS
            )
            if not fresh and time.monotonic() - started < PENDING_REFRESH_BUDGET_SECONDS:
                try:
                    detail = crm_ticket_detail(company_id, ticket_id)
                    item = None
                    if detail is not None and not ticket_is_closed(detail.get("status")):
                        notes = ticket_notes(ticket_id, tail)
                        if notes and notes[-1]["direction"] == "entrante":
                            item = {"ticket_id": ticket_id, "phone_tail": tail, "text": notes[-1]["text"], "occurred_at": notes[-1]["occurred_at"]}
                    state = {"last_at": last_at, "checked": time.monotonic(), "item": item}
                    pending_ticket_state[ticket_id] = state
                except HTTPException:
                    logger.exception("Pending reply check failed ticket_id=%s", ticket_id)
            # Sin presupuesto (o si falló) se usa la última revisión conocida.
            if state and state.get("item"):
                items.append(state["item"])
        items.sort(key=lambda item: occurred_at_utc(item["occurred_at"]), reverse=True)
        pending_cache.update(at=time.monotonic(), items=items)
        return {"pending": items}


@app.post("/internal/crm/expert-connect/messages")
def send_message(payload: MessagePayload, _: None = Depends(require_gateway_access)) -> dict[str, Any]:
    message = str(payload.text or "").strip()
    if not message or len(message) > 1000:
        raise HTTPException(status_code=422, detail="Message text must contain 1 to 1000 characters.")
    phone = phone_number(payload.phone)
    current = credentials()
    company_id = current["company_id"]
    requested_ticket_id = str(payload.ticket_id or "").strip()
    if requested_ticket_id:
        return reply_in_ticket(company_id, requested_ticket_id, phone, message)
    requested_team_id = str(payload.team_id or "").strip()
    contact_id, contact_created = find_or_create_contact(company_id, phone, payload.contact_name, f"crm-contact-{payload.contact_id}")

    team_id = requested_team_id or setting("CRM_EXPERT_CONNECT_TEAM_ID")
    if not team_id:
        raise HTTPException(status_code=503, detail="CRM Expert Connect team is not configured.")
    ticket = expert_connect_request(
        "POST",
        f"/api/v1/companies/{company_id}/tickets",
        {"title": f"Mensaje iniciado desde CRM - {payload.contact_name}"[:250], "description": f"CRM client {payload.client_id}, contact {payload.contact_id}", "customerId": contact_id, "teamId": team_id},
    )
    ticket_id = str(response_data(ticket).get("id") or "")
    if not ticket_id:
        raise HTTPException(status_code=502, detail="Expert Connect did not return a ticket id.")

    area_id, conversation_status = suppress_ticket_conversation(ticket_id, phone)

    tag_id = setting("CRM_EXPERT_CONNECT_TAG_ID")
    if not tag_id:
        raise HTTPException(status_code=503, detail="CRM Expert Connect ticket tag is not configured.")
    expert_connect_request(
        "POST",
        f"/api/v1/companies/{company_id}/tickets/{ticket_id}/tags",
        {"tagIds": [tag_id]},
    )

    note = expert_connect_request("POST", f"/api/v1/companies/{company_id}/tickets/{ticket_id}/notes", {"isPrivate": False, "text": message})
    note_id = str(response_data(note).get("id") or "")
    if not note_id:
        raise HTTPException(status_code=502, detail="Expert Connect did not return a note id.")
    record_crm_message(note_id, ticket_id, phone, message)
    return {"contact_id": contact_id, "contact_created": contact_created, "ticket_id": ticket_id, "ticket_created": True, "team_id": team_id, "area_id": area_id, "status": conversation_status, "tag_id": tag_id, "note_id": note_id}


@app.post("/internal/crm/expert-connect/tickets/close")
def close_ticket(payload: CloseTicketPayload, background_tasks: BackgroundTasks, _: None = Depends(require_gateway_access)) -> dict[str, Any]:
    """Close a CRM collections ticket of this phone.

    Ownership comes from the local index (like replies), not from the partner's
    first page of tickets. The optional close reason is applied as a tag. The AI
    summary runs after answering: it reads every note and calls OpenAI, which
    used to push the request past the caller's 25 s timeout.
    """
    ticket_id = str(payload.ticket_id or "").strip()
    phone = phone_number(payload.phone)
    tag_id = str(payload.tag_id or "").strip()
    if not ticket_id:
        raise HTTPException(status_code=422, detail="Ticket id is required.")
    company_id = credentials()["company_id"]
    if ticket_id not in ticket_ids_for_phone(phone):
        raise HTTPException(status_code=404, detail="Ticket does not belong to the contact.")
    detail = crm_ticket_detail(company_id, ticket_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Ticket is not a CRM collections ticket.")
    if not ticket_is_closed(detail.get("status")):
        if tag_id:
            tags = expert_connect_request("GET", f"/api/v1/companies/{company_id}/tags?{urlencode({'Paging.Page': 1, 'Paging.Size': 100})}").get("items") or []
            if tag_id not in {str(response_data(tag).get("id") or "") for tag in tags}:
                raise HTTPException(status_code=422, detail="Invalid close reason.")
            expert_connect_request("POST", f"/api/v1/companies/{company_id}/tickets/{ticket_id}/tags", {"tagIds": [tag_id]})
        expert_connect_request("PUT", f"/api/v1/companies/{company_id}/tickets/{ticket_id}/close", {})
    # El estado cambió: que el historial y los pendientes no usen copias viejas.
    with ticket_detail_cache_lock:
        ticket_detail_cache.pop(ticket_id, None)
    pending_ticket_state.pop(ticket_id, None)
    pending_cache.update(at=0.0)
    background_tasks.add_task(sync_ticket_summary, ticket_id, phone, datetime.now(timezone.utc))
    return {"ticket_id": ticket_id, "closed": True, "summary": None, "summary_status": "pending"}


@app.get("/internal/crm/expert-connect/ticket-tags")
def ticket_tags(_: None = Depends(require_gateway_access)) -> dict[str, Any]:
    current = credentials()
    query = urlencode({"Paging.Page": 1, "Paging.Size": 100})
    items = expert_connect_request("GET", f"/api/v1/companies/{current['company_id']}/tags?{query}").get("items") or []
    tags = []
    for item in items:
        tag = response_data(item)
        tag_id = str(tag.get("id") or "").strip()
        name = str(tag.get("name") or tag.get("description") or "").strip()
        if tag_id and name:
            tags.append({"id": tag_id, "name": name})
    tags.sort(key=lambda value: value["name"].casefold())
    return {"tags": tags}
