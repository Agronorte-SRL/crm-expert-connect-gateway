import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

import app


class SummaryPromptTests(unittest.TestCase):
    """Protect the participant semantics used to produce auditable summaries."""

    def test_normalized_transcript_distinguishes_client_advisor_and_bot(self):
        messages = [
            {"occurred_at": "1", "direction": "entrante", "sender_type": "user", "text": "Cliente"},
            {"occurred_at": "2", "direction": "saliente", "sender_type": "agent", "text": "Asesor"},
            {"occurred_at": "3", "direction": "saliente", "sender_type": "bot", "text": "Bot"},
        ]
        self.assertEqual(
            [item["participant_role"] for item in app.normalized_transcript(messages)],
            ["client", "advisor", "bot"],
        )

    def test_response_output_text_does_not_assume_first_output_item(self):
        payload = {"output": [{"type": "reasoning"}, {"content": [{"type": "output_text", "text": '{"summary":"ok"}'}]}]}
        self.assertEqual(app.response_output_text(payload), '{"summary":"ok"}')

    def test_summary_contract_rejects_multiple_sentences_or_more_than_45_words(self):
        self.assertTrue(app.valid_summary("El cliente enviará el comprobante mañana."))
        self.assertFalse(app.valid_summary("El cliente pagó. Agronorte verificará el comprobante."))
        self.assertFalse(app.valid_summary(" ".join(["palabra"] * 46)))

    def test_expert_connect_status_two_is_closed_but_one_is_not_terminal(self):
        self.assertTrue(app.ticket_is_closed(2))
        self.assertTrue(app.ticket_is_closed("resolved"))
        self.assertFalse(app.ticket_is_closed(1))
        self.assertFalse(app.ticket_is_closed(0))

    @patch.object(app, "stored_summary", return_value={})
    @patch.object(app, "ticket_ids_for_phone", return_value=["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"])
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_tickets_filters_by_canonical_team_and_tag(self, request, setting, _credentials, _ticket_ids, _stored):
        setting.side_effect = lambda name, default="": {"CRM_EXPERT_CONNECT_TEAM_ID": "team-ok", "CRM_EXPERT_CONNECT_TAG_ID": "tag-ok"}.get(name, default)
        request.side_effect = [
            {"data": {"id": "00000000-0000-4000-8000-000000000001", "status": 0, "createdDate": "2026-09-01", "updatedDate": "2026-09-02", "team": {"id": "team-ok", "name": "Administración de Ventas"}, "tags": [{"id": "tag-ok", "name": "Cobranzas maquinarias"}]}},
            {"data": {"id": "00000000-0000-4000-8000-000000000002", "status": 0, "createdDate": "2026-09-01", "updatedDate": "2026-09-02", "team": {"id": "other"}, "tags": [{"id": "tag-ok"}]}},
        ]
        result = app.tickets(BackgroundTasks(), "3415551234", False, None)
        self.assertEqual([item["ticket_id"] for item in result["tickets"]], ["00000000-0000-4000-8000-000000000001"])
        self.assertEqual(request.call_count, 2)

    @patch.object(app, "stored_summary", return_value={})
    @patch.object(app, "ticket_ids_for_phone", return_value=["00000000-0000-4000-8000-000000000003", "00000000-0000-4000-8000-000000000001"])
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_a_ticket_the_provider_no_longer_returns_does_not_hide_the_rest(self, request, setting, _credentials, _ticket_ids, _stored):
        app.ticket_detail_cache.clear()
        setting.side_effect = lambda name, default="": {"CRM_EXPERT_CONNECT_TEAM_ID": "team-ok", "CRM_EXPERT_CONNECT_TAG_ID": "tag-ok"}.get(name, default)
        request.side_effect = [
            app.HTTPException(status_code=502, detail="Expert Connect request failed."),
            {"data": {"id": "00000000-0000-4000-8000-000000000001", "status": 0, "createdDate": "2026-10-09", "updatedDate": "2026-10-09", "team": {"id": "team-ok"}, "tags": [{"id": "tag-ok"}]}},
        ]
        result = app.tickets(BackgroundTasks(), "3415551234", False, None)
        app.ticket_detail_cache.clear()
        self.assertEqual([item["ticket_id"] for item in result["tickets"]], ["00000000-0000-4000-8000-000000000001"])
        # El que falló no se vuelve a consultar en la próxima lectura.
        self.assertTrue(app.ticket_recently_unreadable("00000000-0000-4000-8000-000000000003"))
        app.unreadable_tickets.clear()

    @patch.object(app, "stored_summary", return_value={})
    @patch.object(app, "ticket_ids_for_phone", return_value=["demo-ticket-004", "e2e-agrosem3-d4ab9d"])
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_test_ticket_ids_are_skipped_without_calling_the_provider(self, request, setting, _credentials, _ticket_ids, _stored):
        result = app.tickets(BackgroundTasks(), "3415551234", False, None)
        self.assertEqual(result["tickets"], [])
        request.assert_not_called()

    @patch.object(app, "stored_summary", return_value={})
    @patch.object(app, "ticket_ids_for_phone", return_value=["00000000-0000-4000-8000-000000000004"])
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_tickets_http_contract_requires_auth_and_returns_aggregate(self, request, setting, _credentials, _ticket_ids, _stored):
        values = {
            "CRM_EXPERT_CONNECT_GATEWAY_TOKEN": "gateway-token",
            "CRM_EXPERT_CONNECT_TEAM_ID": "team-ok",
            "CRM_EXPERT_CONNECT_TAG_ID": "tag-ok",
        }
        setting.side_effect = lambda name, default="": values.get(name, default)
        request.side_effect = [
            {"data": {"id": "00000000-0000-4000-8000-000000000004", "status": 0, "createdDate": "2026-09-01", "updatedDate": "2026-09-02", "team": {"id": "team-ok", "name": "Administración de Ventas"}, "tags": [{"id": "tag-ok", "name": "Cobranzas maquinarias"}]}},
        ]
        client = TestClient(app.app)

        unauthorized = client.get("/internal/crm/expert-connect/tickets", params={"phone": "3415551234"})
        response = client.get(
            "/internal/crm/expert-connect/tickets",
            params={"phone": "3415551234"},
            headers={"Authorization": "Bearer gateway-token"},
        )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tickets"][0]["ticket_id"], "00000000-0000-4000-8000-000000000004")

    def test_closed_ticket_uses_structured_output_and_persists_summary(self):
        executed = []

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, statement, params): executed.append((statement, params))
            def fetchone(self): return {}

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def cursor(self): return Cursor()

        class Response:
            def raise_for_status(self): return None
            def json(self):
                return {"output": [{"content": [{"type": "output_text", "text": '{"summary":"El cliente confirmó el pago.","evidence_sufficient":true}'}]}]}

        settings = {"OPENAI_API_KEY": "test-key", "OPENAI_SUMMARY_MODEL": "test-model"}
        with patch.object(app, "database_connection", return_value=Connection()), patch.object(app, "setting", side_effect=lambda name, default="": settings.get(name, default)), patch.object(app.requests, "post", return_value=Response()) as post:
            result = app.summarize_closed_ticket("ticket-1", [{"id": "m1", "direction": "entrante", "sender_type": "user", "occurred_at": "2026-09-01", "text": "Ya pagué."}], "2026-09-02")

        self.assertEqual(result, {"summary": "El cliente confirmó el pago.", "generation_status": "generated"})
        payload = post.call_args.kwargs["json"]
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(any("INSERT INTO crm_expert_connect_ticket_summaries" in statement for statement, _ in executed))

    def test_invalid_openai_summary_is_not_published(self):
        executed = []

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, statement, params): executed.append((statement, params))
            def fetchone(self): return {}

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def cursor(self): return Cursor()

        class Response:
            def raise_for_status(self): return None
            def json(self):
                return {"output": [{"content": [{"type": "output_text", "text": '{"summary":"El cliente pagó. Agronorte verificará el comprobante.","evidence_sufficient":true}'}]}]}

        settings = {"OPENAI_API_KEY": "test-key", "OPENAI_SUMMARY_MODEL": "test-model"}
        with patch.object(app, "database_connection", return_value=Connection()), patch.object(app, "setting", side_effect=lambda name, default="": settings.get(name, default)), patch.object(app.requests, "post", return_value=Response()):
            result = app.summarize_closed_ticket("ticket-invalid", [{"direction": "entrante", "sender_type": "user", "text": "Ya pagué."}], "2026-09-02")

        self.assertEqual(result, {"summary": None, "generation_status": "failed"})
        insert_params = next(params for statement, params in executed if "INSERT INTO crm_expert_connect_ticket_summaries" in statement)
        self.assertIsNone(insert_params[2])
        self.assertEqual(insert_params[7], "failed")


class SalesDocumentTests(unittest.TestCase):
    """AGRONORTE-500: sales documents go to the Ventas team with the PDF in the same note."""

    body = {
        "phone": "93498416451",
        "contact_name": "Juan Pérez",
        "text": "Hola Juan, te compartimos el presupuesto N° PRE 0001-00000001 de Agronorte: https://example.test/p.pdf",
        "team_id": "team-ventas",
        "tag_name": "Nuevo sin canje",
        "file_url": "https://example.test/p.pdf",
        "file_name": "presupuesto_PRE-0001-00000001.pdf",
        "external_ref": "ventas-documento-7-5493498416451",
    }

    def client(self, setting):
        setting.side_effect = lambda name, default="": {"CRM_EXPERT_CONNECT_GATEWAY_TOKEN": "gateway-token"}.get(name, default)
        return TestClient(app.app)

    @patch.object(app, "database_connection", side_effect=RuntimeError("index offline"))
    @patch.object(app, "suppress_ticket_conversation", return_value=("Ventas de Maquinaria", "advisor_handoff_ignored"))
    @patch.object(app, "download_pdf", return_value=b"%PDF-1.4 test")
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_document_is_attached_to_the_same_note_in_the_requested_team(self, request, setting, _credentials, _download, _suppress, _db):
        request.side_effect = [
            {"data": {"mediaId": "media-1.pdf"}},  # POST /api/v1/media
            {"items": []},  # GET contacts
            {"data": {"id": "contact-1"}},  # POST contacts
            {"data": {"id": "ticket-1"}},  # POST tickets
            # Las etiquetas de ventas son subetiquetas de VENTAS.
            {"items": [{"data": {"id": "tag-cobranzas", "name": "Cobranzas maquinarias", "subTags": []}},
                       {"data": {"id": "tag-ventas", "name": "VENTAS", "subTags": [
                           {"id": "tag-other", "name": "Usado con canje", "subTags": []},
                           {"id": "tag-1", "name": "Nuevo sin canje", "subTags": []}]}}]},
            {},  # POST ticket tags
            {"data": {"id": "note-1"}},  # POST notes
        ]
        response = self.client(setting).post("/internal/crm/expert-connect/documents", json=self.body, headers={"Authorization": "Bearer gateway-token"})

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["ticket_id"], result["note_id"], result["media_id"], result["tag_id"]), ("ticket-1", "note-1", "media-1.pdf", "tag-1"))
        media_call, ticket_call, note_call = request.call_args_list[0], request.call_args_list[3], request.call_args_list[6]
        self.assertEqual(media_call.args[:2], ("POST", "/api/v1/media"))
        self.assertEqual(media_call.kwargs["files"]["file"][2], "application/pdf")
        self.assertEqual(ticket_call.args[2]["teamId"], "team-ventas")
        self.assertNotIn("advisorId", ticket_call.args[2])
        self.assertEqual(note_call.args[2]["mediaId"], "media-1.pdf")
        self.assertEqual(note_call.args[2]["text"], self.body["text"])
        self.assertEqual(request.call_args_list[5].args[2], {"tagIds": ["tag-ventas", "tag-1"]})
        self.assertFalse(note_call.args[2]["isPrivate"])

    @patch.object(app, "database_connection", side_effect=RuntimeError("index offline"))
    @patch.object(app, "suppress_ticket_conversation", return_value=("Ventas de Maquinaria", "advisor_handoff_ignored"))
    @patch.object(app, "download_pdf", return_value=b"%PDF-1.4 test")
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_missing_tag_does_not_block_the_send(self, request, setting, _credentials, _download, _suppress, _db):
        request.side_effect = [
            {"data": {"mediaId": "media-1.pdf"}},
            {"items": [{"data": {"id": "contact-1"}}]},
            {"data": {"id": "ticket-1"}},
            {"items": []},  # the tag has not been created yet
            {"data": {"id": "note-1"}},
        ]
        response = self.client(setting).post("/internal/crm/expert-connect/documents", json=self.body, headers={"Authorization": "Bearer gateway-token"})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["tag_applied"])
        self.assertEqual(request.call_count, 5)

    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_a_file_that_is_not_a_pdf_is_rejected_before_creating_a_ticket(self, request, setting):
        class Response:
            status_code = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def iter_content(self, chunk_size): return [b"<html>not found</html>"]

        with patch.object(app.requests, "get", return_value=Response()):
            response = self.client(setting).post("/internal/crm/expert-connect/documents", json=self.body, headers={"Authorization": "Bearer gateway-token"})

        self.assertEqual(response.status_code, 422)
        request.assert_not_called()

    @patch.object(app, "suppress_ticket_conversation", return_value=("Ventas de Maquinaria", "advisor_handoff_ignored"))
    @patch.object(app, "download_pdf", return_value=b"%PDF-1.4 test")
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_note_failure_reports_the_created_ticket(self, request, setting, _credentials, _download, _suppress):
        request.side_effect = [
            {"data": {"mediaId": "media-1.pdf"}},
            {"items": [{"data": {"id": "contact-1"}}]},
            {"data": {"id": "ticket-1"}},
            {"items": []},
            app.HTTPException(status_code=502, detail="Expert Connect could not accept the message."),
        ]
        response = self.client(setting).post("/internal/crm/expert-connect/documents", json=self.body, headers={"Authorization": "Bearer gateway-token"})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["ticket_id"], "ticket-1")


CRM_SETTINGS = {"CRM_EXPERT_CONNECT_TEAM_ID": "team-ok", "CRM_EXPERT_CONNECT_TAG_ID": "tag-ok"}
CRM_TICKET = {"id": "ticket-1", "status": 1, "team": {"id": "team-ok"}, "tags": [{"id": "tag-ok"}], "customer": {"id": "contact-1"}}


class ReplyInTicketTests(unittest.TestCase):
    @patch.object(app, "record_crm_message")
    @patch.object(app, "ticket_ids_for_phone", return_value=["ticket-1"])
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_reply_posts_a_note_in_the_same_open_ticket(self, request, setting, _credentials, _ticket_ids, record):
        setting.side_effect = lambda name, default="": CRM_SETTINGS.get(name, default)
        request.side_effect = [{"data": CRM_TICKET}, {"data": {"id": "note-9"}}]
        payload = app.MessagePayload(client_id=1, contact_id=2, contact_name="Juan", phone="3415551234", text="Hola", ticket_id="ticket-1")
        result = app.send_message(payload, None)
        self.assertEqual(result["ticket_id"], "ticket-1")
        self.assertFalse(result["ticket_created"])
        self.assertEqual(result["note_id"], "note-9")
        self.assertEqual(request.call_args_list[1].args[:2], ("POST", "/api/v1/companies/company/tickets/ticket-1/notes"))
        self.assertEqual(request.call_count, 2)
        record.assert_called_once()

    @patch.object(app, "ticket_ids_for_phone", return_value=["ticket-1"])
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_closed_ticket_is_not_reused(self, request, setting, _credentials, _ticket_ids):
        setting.side_effect = lambda name, default="": CRM_SETTINGS.get(name, default)
        request.side_effect = [{"data": {**CRM_TICKET, "status": 2}}]
        payload = app.MessagePayload(client_id=1, contact_id=2, contact_name="Juan", phone="3415551234", text="Hola", ticket_id="ticket-1")
        with self.assertRaises(app.HTTPException) as raised:
            app.send_message(payload, None)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(request.call_count, 1)

    @patch.object(app, "ticket_ids_for_phone", return_value=["other"])
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "expert_connect_request")
    def test_ticket_of_another_phone_is_rejected(self, request, _credentials, _ticket_ids):
        payload = app.MessagePayload(client_id=1, contact_id=2, contact_name="Juan", phone="3415551234", text="Hola", ticket_id="ticket-1")
        with self.assertRaises(app.HTTPException) as raised:
            app.send_message(payload, None)
        self.assertEqual(raised.exception.status_code, 404)
        request.assert_not_called()


class PendingRepliesTests(unittest.TestCase):
    def setUp(self):
        app.pending_cache.update(at=0.0, items=[])
        app.pending_ticket_state.clear()
        app.ticket_detail_cache.clear()

    @patch.object(app, "ticket_notes")
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    @patch.object(app, "database_connection")
    def test_only_open_crm_tickets_whose_last_note_is_the_customer(self, database, request, setting, _credentials, notes):
        setting.side_effect = lambda name, default="": CRM_SETTINGS.get(name, default)
        rows = [{"ticket_id": t, "phone_tail": "3415551234", "last_at": "2026-10-08T12:10:00Z"} for t in ("waiting", "answered", "closed", "foreign")]

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, *args): pass
            def fetchall(self): return rows

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def cursor(self): return Cursor()

        database.return_value = Connection()
        details = {
            "waiting": CRM_TICKET, "answered": CRM_TICKET,
            "closed": {**CRM_TICKET, "status": 2}, "foreign": {**CRM_TICKET, "team": {"id": "other"}},
        }
        request.side_effect = lambda method, path, *args, **kwargs: {"data": details[path.rsplit("/", 1)[-1]]}
        notes.side_effect = lambda ticket_id, phone: {
            "waiting": [{"direction": "saliente", "text": "Recordatorio", "occurred_at": "2026-10-08T12:00:00Z"},
                        {"direction": "entrante", "text": "Hola", "occurred_at": "2026-10-08T12:05:00Z"}],
            "answered": [{"direction": "entrante", "text": "Hola", "occurred_at": "2026-10-08T12:00:00Z"},
                         {"direction": "saliente", "text": "Buen día", "occurred_at": "2026-10-08T12:10:00Z"}],
        }[ticket_id]
        result = app.pending_replies(None)
        self.assertEqual([item["ticket_id"] for item in result["pending"]], ["waiting"])
        self.assertEqual(result["pending"][0]["text"], "Hola")
        self.assertEqual(result["pending"][0]["phone_tail"], "3415551234")
        # Sin mensajes nuevos no se vuelve a consultar al proveedor.
        app.pending_cache.update(at=0.0)
        calls = request.call_count
        self.assertEqual([item["ticket_id"] for item in app.pending_replies(None)["pending"]], ["waiting"])
        self.assertEqual(request.call_count, calls)

    @patch.object(app, "stored_summary", return_value={})
    @patch.object(app, "ticket_notes", return_value=[])
    @patch.object(app, "ticket_ids_for_phone", return_value=["00000000-0000-4000-8000-000000000005", "00000000-0000-4000-8000-000000000006", "00000000-0000-4000-8000-000000000007"])
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_single_ticket_request_reads_only_that_ticket(self, request, setting, _credentials, _ids, notes, _stored):
        setting.side_effect = lambda name, default="": CRM_SETTINGS.get(name, default)
        request.return_value = {"data": {**CRM_TICKET, "id": "00000000-0000-4000-8000-000000000006", "createdDate": "2026-10-08", "updatedDate": "2026-10-08"}}
        result = app.tickets(BackgroundTasks(), "3415551234", True, "00000000-0000-4000-8000-000000000006", None)
        self.assertEqual([item["ticket_id"] for item in result["tickets"]], ["00000000-0000-4000-8000-000000000006"])
        self.assertEqual(request.call_count, 1)
        notes.assert_called_once()


class CloseTicketTests(unittest.TestCase):
    def setUp(self):
        app.ticket_detail_cache.clear()

    @patch.object(app, "ticket_ids_for_phone", return_value=["ticket-1"])
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "setting")
    @patch.object(app, "expert_connect_request")
    def test_close_without_reason_closes_and_defers_the_summary(self, request, setting, _credentials, _ids):
        setting.side_effect = lambda name, default="": CRM_SETTINGS.get(name, default)
        request.side_effect = [{"data": CRM_TICKET}, {}]
        tasks = BackgroundTasks()
        result = app.close_ticket(app.CloseTicketPayload(ticket_id="ticket-1", phone="3415551234"), tasks, None)
        self.assertTrue(result["closed"])
        self.assertEqual(request.call_args_list[1].args[:2], ("PUT", "/api/v1/companies/company/tickets/ticket-1/close"))
        self.assertEqual(len(tasks.tasks), 1)

    @patch.object(app, "ticket_ids_for_phone", return_value=["other"])
    @patch.object(app, "credentials", return_value={"company_id": "company"})
    @patch.object(app, "expert_connect_request")
    def test_ticket_of_another_phone_is_not_closed(self, request, _credentials, _ids):
        with self.assertRaises(app.HTTPException) as raised:
            app.close_ticket(app.CloseTicketPayload(ticket_id="ticket-1", phone="3415551234"), BackgroundTasks(), None)
        self.assertEqual(raised.exception.status_code, 404)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
