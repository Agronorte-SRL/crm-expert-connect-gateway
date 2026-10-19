"""Opt-in live smoke test for the closed-ticket summary path.

Run with OPENAI_API_KEY in the process environment. The database is replaced by
an in-memory recorder so this test never writes production data.
"""

import os
from unittest.mock import patch

import app


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required")
    executed: list[tuple[str, tuple]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, statement, params):
            executed.append((statement, params))

        def fetchone(self):
            return {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def cursor(self):
            return Cursor()

    messages = [
        {
            "direction": "saliente",
            "sender_type": "agent",
            "occurred_at": "2026-09-03T14:00:00Z",
            "text": "Le informamos que la cuota está vencida. ¿Cuándo podrá abonarla?",
        },
        {
            "direction": "entrante",
            "sender_type": "user",
            "occurred_at": "2026-09-03T14:05:00Z",
            "text": "La transferiré mañana y enviaré el comprobante.",
        },
    ]
    with patch.object(app, "database_connection", return_value=Connection()):
        result = app.summarize_closed_ticket("live-smoke-ticket", messages, "2026-09-03T14:10:00Z")

    insert = next(
        params
        for statement, params in executed
        if "INSERT INTO crm_expert_connect_ticket_summaries" in statement
    )
    if result.get("generation_status") != "generated" or not result.get("summary"):
        raise SystemExit(f"live summary failed: status={result.get('generation_status')}")
    if insert[2] != result["summary"] or insert[7] != "generated":
        raise SystemExit("summary persistence contract failed")
    if not app.valid_summary(result["summary"]):
        raise SystemExit("summary violated the one-sentence/45-word contract")
    print("LIVE_OPENAI_SMOKE_OK")
    print(f"summary={result['summary']}")


if __name__ == "__main__":
    main()
