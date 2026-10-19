"""Integration test of every SQL statement against a real Postgres.

Runs only with CRM_TEST_DATABASE_URL pointing to a throwaway database (never
the preview or production): it creates the migrated tables the gateway reads
(`chatbot_conversations`, `chatbot_messages`, `parametrosaplicacion`) in the
`vps4_agronorte_prod` schema with the same columns as the migration, and then
exercises the gateway's own tables and queries. Expert Connect is mocked.

    docker run -d --rm --name ec-gateway-pg-test -e POSTGRES_PASSWORD=prueba \\
      -e POSTGRES_USER=expert_app -e POSTGRES_DB=expert_connect_pg -p 127.0.0.1:55499:5432 postgres:17-alpine
    CRM_TEST_DATABASE_URL=postgresql://expert_app:prueba@127.0.0.1:55499/expert_connect_pg python -m unittest -v test_postgres_integration.py
"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

import app

TEST_URL = os.getenv("CRM_TEST_DATABASE_URL", "")

MIGRATED_SCHEMA = """
DROP SCHEMA IF EXISTS vps4_agronorte_prod CASCADE;
CREATE SCHEMA vps4_agronorte_prod;
SET search_path = vps4_agronorte_prod;
CREATE TABLE chatbot_conversations (
    id BIGSERIAL PRIMARY KEY, conversation_key VARCHAR(255) NOT NULL, channel VARCHAR(50) NOT NULL,
    external_conversation_id VARCHAR(255), participant_phone VARCHAR(50), account_id VARCHAR(100),
    area_id VARCHAR(100), status VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_message_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX uq_chatbot_conversation_key ON chatbot_conversations (conversation_key);
CREATE TABLE chatbot_messages (
    id BIGSERIAL PRIMARY KEY, conversation_id BIGINT, channel VARCHAR(50), external_message_id VARCHAR(255),
    external_conversation_id VARCHAR(255), direction VARCHAR(10), sender_type VARCHAR(50), sender_name VARCHAR(255),
    sender_external_id VARCHAR(255), advisor_racfid VARCHAR(100), is_system_message BOOLEAN, sender_phone VARCHAR(50),
    text TEXT, account_id VARCHAR(100), area_id VARCHAR(100), raw_payload_json TEXT, created_at TIMESTAMPTZ
);
CREATE TABLE parametrosaplicacion (
    id BIGSERIAL PRIMARY KEY, "accessToken" VARCHAR(255), company_id_expert_connect VARCHAR(255),
    base_url_expert_connect VARCHAR(255), token_expert_connect TEXT, x_subscription_expert_connect VARCHAR(255)
);
INSERT INTO parametrosaplicacion ("accessToken", company_id_expert_connect, base_url_expert_connect, token_expert_connect, x_subscription_expert_connect)
VALUES ('token-camel', 'company-1', 'https://service-api.test', NULL, 'sub-1');
"""


@unittest.skipUnless(TEST_URL, "CRM_TEST_DATABASE_URL no definida")
class PostgresIntegrationTests(unittest.TestCase):
    phone = "5493498416451"

    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {"DATABASE_URL": TEST_URL, "CRM_DB_SCHEMA": "vps4_agronorte_prod", "CRM_EXPERT_CONNECT_GATEWAY_TOKEN": "gw"})
        cls.env.start()
        with app.database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(MIGRATED_SCHEMA)
        app.create_journal_table()
        app.create_journal_table()  # idempotente

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def test_credentials_read_the_quoted_camel_case_token(self):
        current = app.credentials()
        self.assertEqual((current["token"], current["company_id"], current["base_url"]), ("token-camel", "company-1", "https://service-api.test"))

    def test_conversation_suppression_is_an_upsert(self):
        app.suppress_ticket_conversation("ticket-1", self.phone)
        app.suppress_ticket_conversation("ticket-1", self.phone)
        with app.database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) AS n, max(status) AS status, max(area_id) AS area FROM chatbot_conversations WHERE conversation_key='expert_connect:ticket-1'")
                row = cursor.fetchone()
        self.assertEqual((row["n"], row["status"], row["area"]), (1, "advisor_handoff_ignored", "Ventas de Maquinaria"))
        self.assertIn("ticket-1", app.ticket_ids_for_phone(self.phone))

    def test_history_notifications_and_reads(self):
        app.suppress_ticket_conversation("ticket-2", "+54 9 3498 41-6451")
        app.record_crm_message("note-1", "ticket-2", self.phone, "Presupuesto enviado")
        app.record_crm_message("note-1", "ticket-2", self.phone, "Presupuesto enviado")  # idempotente
        with app.database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM chatbot_conversations WHERE conversation_key='expert_connect:ticket-2'")
                conversation_id = cursor.fetchone()["id"]
                cursor.execute(
                    "INSERT INTO chatbot_messages (conversation_id, direction, sender_phone, text, created_at) VALUES (%s, 'in', %s, 'Hola, lo vi', %s)",
                    (conversation_id, "+54 9 3498 41-6451", datetime.now(timezone.utc) - timedelta(minutes=1)),
                )
                cursor.execute("SELECT count(*) AS n FROM crm_expert_connect_messages WHERE external_note_id='note-1'")
                self.assertEqual(cursor.fetchone()["n"], 1)
        client = TestClient(app.app)
        headers = {"Authorization": "Bearer gw"}
        with patch.object(app, "live_ticket_notes", return_value=[]):
            history = client.get("/internal/crm/expert-connect/conversations", params={"phone": self.phone, "area_id": "Ventas de Maquinaria"}, headers=headers)
            pending = client.get("/internal/crm/expert-connect/notifications", params={"area_id": "Ventas de Maquinaria"}, headers=headers)
        self.assertEqual(history.status_code, 200, history.text)
        texts = [message["text"] for message in history.json()["messages"]]
        self.assertIn("Presupuesto enviado", texts)
        self.assertIn("Hola, lo vi", texts)
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertTrue(any(item["phone_tail"] == "3498416451" for item in pending.json()["notifications"]))
        read = client.post("/internal/crm/expert-connect/notifications/read", json={"phone": self.phone, "area_id": "Ventas de Maquinaria"}, headers=headers)
        self.assertEqual(read.status_code, 200, read.text)
        client.post("/internal/crm/expert-connect/notifications/read", json={"phone": self.phone, "area_id": "Ventas de Maquinaria"}, headers=headers)
        with patch.object(app, "live_ticket_notes", return_value=[]):
            after = client.get("/internal/crm/expert-connect/notifications", params={"area_id": "Ventas de Maquinaria"}, headers=headers)
        self.assertFalse(any(item["phone_tail"] == "3498416451" for item in after.json()["notifications"]))

    def test_summary_upsert(self):
        messages = [{"direction": "entrante", "sender_type": "user", "occurred_at": "2026-10-01", "text": "Pago mañana."}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            first = app.summarize_closed_ticket("ticket-3", messages, "2026-10-01T10:00:00Z")
            second = app.summarize_closed_ticket("ticket-3", messages, "2026-10-01T10:00:00Z")
        self.assertEqual((first["generation_status"], second["generation_status"]), ("pending", "pending"))
        self.assertEqual(app.stored_summary("ticket-3")["generation_status"], "pending")


if __name__ == "__main__":
    unittest.main()
