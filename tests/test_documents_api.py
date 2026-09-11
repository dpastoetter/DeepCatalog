"""Documents, retrieve, ask, and reveal API coverage."""

from __future__ import annotations

from pathlib import Path

from deepcatalog.tools import metadata_db, rag_index


def _seed_document(isolated_data, monkeypatch):
    archive = isolated_data / "archive" / "invoice" / "2022" / "doc.pdf"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"%PDF-1.4 filed")

    def fake_embed(texts):
        vectors = []
        for t in texts:
            vec = [0.0] * 8
            for i, ch in enumerate(t.encode("utf-8")):
                vec[i % 8] += (ch % 17) / 17.0
            vectors.append(vec)
        return vectors

    monkeypatch.setattr(rag_index, "embed_texts", fake_embed)
    saved = metadata_db.upsert_metadata(
        original_name="scan.pdf",
        filename="2022-09-05_Invoice_Acme_EUR10.pdf",
        path=str(archive),
        doc_type="invoice",
        doc_date="2022-09-05",
        counterparties="Acme",
        amount=10,
        currency="EUR",
        summary="Invoice FA-1 from Acme for EUR 10.",
    )
    doc_id = saved["document_id"]
    rag_index.index_document(
        document_id=doc_id,
        text="Invoice FA-1 from Acme dated 2022-09-05. Total EUR 10.",
        filename="2022-09-05_Invoice_Acme_EUR10.pdf",
        doc_type="invoice",
    )
    return doc_id, archive


def test_documents_list_and_search(client, isolated_data, monkeypatch):
    doc_id, _archive = _seed_document(isolated_data, monkeypatch)
    listed = client.get("/api/documents")
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] >= 1
    assert "has_more" in body
    assert body["offset"] == 0

    found = client.get("/api/documents", params={"q": "Acme", "doc_type": "invoice"})
    assert found.status_code == 200
    assert any(d["id"] == doc_id for d in found.json()["documents"])

    one = client.get(f"/api/documents/{doc_id}")
    assert one.status_code == 200
    assert one.json()["document"]["id"] == doc_id


def _seed_extra_docs(isolated_data):
    rows = [
        ("invoice", "2022-01-01", "Acme", "Early Acme invoice"),
        ("invoice", "2023-06-15", "Beta LLC", "Beta mid invoice"),
        ("receipt", "2023-07-01", "Acme", "Acme receipt"),
        ("tax", "2024-01-10", "Finanzamt", "Tax notice"),
        ("letter", "2024-02-20", "Landlord", "Rent letter"),
    ]
    ids = []
    for i, (doc_type, doc_date, party, summary) in enumerate(rows):
        path = isolated_data / "archive" / doc_type / doc_date[:4] / f"doc{i}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4 x")
        saved = metadata_db.upsert_metadata(
            original_name=f"scan{i}.pdf",
            filename=f"{doc_date}_{doc_type}_{party}.pdf",
            path=str(path),
            doc_type=doc_type,
            doc_date=doc_date,
            counterparties=party,
            subject=summary,
            summary=summary,
            amount=10 + i if doc_type in {"invoice", "receipt", "tax"} else None,
            currency="EUR" if doc_type in {"invoice", "receipt", "tax"} else None,
        )
        ids.append(saved["document_id"])
    return ids


def test_documents_filters_dates_counterparty_and_pagination(client, isolated_data, monkeypatch):
    _seed_document(isolated_data, monkeypatch)
    _seed_extra_docs(isolated_data)

    by_party = client.get("/api/documents", params={"counterparty": "Acme"})
    assert by_party.status_code == 200
    parties = by_party.json()["documents"]
    assert parties
    assert all(
        "Acme" in (d.get("counterparties") or "") or "Acme" in (d.get("subject") or "")
        for d in parties
    )

    dated = client.get(
        "/api/documents",
        params={"date_from": "2023-01-01", "date_to": "2023-12-31"},
    )
    assert dated.status_code == 200
    for d in dated.json()["documents"]:
        assert d["doc_date"] >= "2023-01-01"
        assert d["doc_date"] <= "2023-12-31"

    combo = client.get(
        "/api/documents",
        params={
            "doc_type": "invoice",
            "counterparty": "Acme",
            "date_from": "2022-01-01",
            "date_to": "2022-12-31",
        },
    )
    assert combo.status_code == 200
    assert combo.json()["count"] >= 1
    assert all(d["doc_type"] == "invoice" for d in combo.json()["documents"])

    page1 = client.get("/api/documents", params={"limit": 2, "offset": 0})
    assert page1.status_code == 200
    body1 = page1.json()
    assert body1["count"] == 2
    assert body1["has_more"] is True
    assert body1["limit"] == 2
    assert body1["offset"] == 0

    page2 = client.get("/api/documents", params={"limit": 2, "offset": 2})
    assert page2.status_code == 200
    body2 = page2.json()
    assert body2["offset"] == 2
    ids1 = {d["id"] for d in body1["documents"]}
    ids2 = {d["id"] for d in body2["documents"]}
    assert ids1.isdisjoint(ids2)


def test_documents_rejects_invalid_date_and_limit(client, isolated_data, monkeypatch):
    _seed_document(isolated_data, monkeypatch)
    bad = client.get("/api/documents", params={"date_from": "01-01-2024"})
    assert bad.status_code == 422

    bad_to = client.get("/api/documents", params={"date_to": "not-a-date"})
    assert bad_to.status_code == 422

    bad_limit = client.get("/api/documents", params={"limit": 0})
    assert bad_limit.status_code == 422

    bad_offset = client.get("/api/documents", params={"offset": -1})
    assert bad_offset.status_code == 422


def test_document_file_and_path_confinement(client, isolated_data, monkeypatch):
    doc_id, archive = _seed_document(isolated_data, monkeypatch)
    ok = client.get(f"/api/documents/{doc_id}/file")
    assert ok.status_code == 200
    assert ok.headers.get("content-type", "").startswith("application/pdf")

    # Point metadata at a path outside archive roots → 403.
    escape = isolated_data.parent / "secret.pdf"
    escape.write_bytes(b"%PDF secret")
    metadata_db.upsert_metadata(
        document_id=doc_id,
        original_name="scan.pdf",
        filename="escape.pdf",
        path=str(escape),
        doc_type="other",
    )
    denied = client.get(f"/api/documents/{doc_id}/file")
    assert denied.status_code == 403


def test_document_reveal_mocked(client, isolated_data, monkeypatch):
    doc_id, _archive = _seed_document(isolated_data, monkeypatch)
    monkeypatch.setattr(
        "app.routers.documents.reveal_in_explorer",
        lambda path: {"status": "success", "path": path, "opened": "explorer"},
    )
    resp = client.post(f"/api/documents/{doc_id}/reveal")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_document_open_mocked(client, isolated_data, monkeypatch):
    doc_id, archive = _seed_document(isolated_data, monkeypatch)
    opened: list[str] = []

    def fake_open(path: str):
        opened.append(path)
        return {"status": "success", "path": path, "opened": "os"}

    monkeypatch.setattr("app.routers.documents.open_with_os", fake_open)
    resp = client.post(f"/api/documents/{doc_id}/open")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert opened and Path(opened[0]).name == Path(archive).name


def test_retrieve_and_ask(client, isolated_data, monkeypatch):
    _seed_document(isolated_data, monkeypatch)

    hits = client.get("/api/retrieve", params={"q": "Acme invoice"})
    assert hits.status_code == 200
    assert hits.json()["status"] == "success"

    async def fake_ask(question: str, history=None):
        return {
            "status": "success",
            "reply": f"Answer to: {question}",
            "sources": [],
            "grounded": True,
            "evidence": "strong",
            "retrieval_count": 1,
            "metadata_count": 0,
        }

    monkeypatch.setattr("app.routers.documents.run_query", fake_ask)
    asked = client.post("/api/ask", json={"question": "How much did Acme charge?"})
    assert asked.status_code == 200
    assert "Acme" in asked.json()["reply"] or "Answer" in asked.json()["reply"]

    follow = client.post(
        "/api/ask",
        json={
            "question": "And the date?",
            "history": [
                {"role": "user", "content": "How much did Acme charge?"},
                {"role": "assistant", "content": "EUR 10"},
            ],
        },
    )
    assert follow.status_code == 200


def test_ask_rejects_empty_question(client):
    resp = client.post("/api/ask", json={"question": ""})
    assert resp.status_code == 422
