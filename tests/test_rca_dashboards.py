"""Data-driven verification of the RCA dashboard aggregations (specs/08 §5).

Unlike test_rca.py's dashboard smoke tests (which only assert "at least 1"),
these seed an EXACT, known set of RCAs in known lifecycle states and assert
the dashboard endpoints return the EXACT computed values — counts, the
pipeline-by-status breakdown, evidence-completeness ratios, and the
compliance percentage — rather than just checking they're non-zero.
"""
import pytest


async def _create_rca(client, **over):
    body = {"title": "RCA for outage"} | over
    r = await client.post("/api/v1/rca", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _waive_all_evidence(admin_client, rca_id):
    checklist = (await admin_client.get(f"/api/v1/rca/{rca_id}/evidence-checklist")).json()["items"]
    for item in checklist:
        r = await admin_client.post(
            f"/api/v1/rca/{rca_id}/evidence-checklist/{item['id']}/waive",
            json={"reason": "test setup — not applicable"},
        )
        assert r.status_code == 200, r.text


async def _waive_one_evidence_item(admin_client, rca_id, evidence_type):
    checklist = (await admin_client.get(f"/api/v1/rca/{rca_id}/evidence-checklist")).json()["items"]
    item = next(i for i in checklist if i["evidence_type"] == evidence_type)
    r = await admin_client.post(
        f"/api/v1/rca/{rca_id}/evidence-checklist/{item['id']}/waive",
        json={"reason": "test setup"},
    )
    assert r.status_code == 200, r.text


async def _fill_required_sections(admin_client, rca_id):
    r = await admin_client.patch(f"/api/v1/rca/{rca_id}", json={
        "root_cause_statement": "Root cause confirmed.",
        "customer_impact": "No customer impact.",
    })
    assert r.status_code == 200, r.text


async def _drive_to_completed(agent_client, admin_client, rca_id):
    await _fill_required_sections(admin_client, rca_id)
    await _waive_all_evidence(admin_client, rca_id)
    assert (await agent_client.post(f"/api/v1/rca/{rca_id}/submit")).status_code == 200
    assert (await admin_client.post(f"/api/v1/rca/{rca_id}/approve")).status_code == 200
    action = (await agent_client.post(f"/api/v1/rca/{rca_id}/actions", json={
        "description": "Follow-up", "priority": "low",
    })).json()
    assert (await agent_client.patch(
        f"/api/v1/rca/{rca_id}/actions/{action['id']}", json={"status": "done"},
    )).status_code == 200
    assert (await agent_client.post(f"/api/v1/rca/{rca_id}/complete")).status_code == 200


async def _drive_to_rejected(agent_client, admin_client, rca_id):
    await _fill_required_sections(admin_client, rca_id)
    await _waive_all_evidence(admin_client, rca_id)
    assert (await agent_client.post(f"/api/v1/rca/{rca_id}/submit")).status_code == 200
    assert (await admin_client.post(
        f"/api/v1/rca/{rca_id}/reject", json={"reason": "needs more detail"},
    )).status_code == 200


@pytest.mark.asyncio
async def test_dashboard_summary_and_pipeline_exact_counts(agent_client, tenant_admin_client):
    """Seed exactly 4 RCAs — 1 completed, 1 rejected, 1 waived, 1 left in
    draft — and assert every dashboard number matches precisely, not just
    ">= 1"."""
    completed = await _create_rca(agent_client, title="Completed one")
    await _drive_to_completed(agent_client, tenant_admin_client, completed["id"])

    rejected = await _create_rca(agent_client, title="Rejected one")
    await _drive_to_rejected(agent_client, tenant_admin_client, rejected["id"])

    waived = await _create_rca(agent_client, title="Waived one")
    assert (await tenant_admin_client.post(
        f"/api/v1/rca/{waived['id']}/waive", json={"reason": "not needed"},
    )).status_code == 200

    await _create_rca(agent_client, title="Still draft")

    summary = (await agent_client.get("/api/v1/dashboards/rca/summary")).json()
    assert summary["required_count"] == 4
    assert summary["completed_on_time_count"] == 1  # no due_at set -> counts as on-time
    assert summary["rejected_count"] == 1
    assert summary["waived_count"] == 1
    assert summary["overdue_count"] == 0
    assert summary["compliance_pct"] == 25.0  # 1 completed / 4 total

    pipeline = (await agent_client.get("/api/v1/dashboards/rca/pipeline")).json()["pipeline"]
    assert pipeline.get("completed") == 1
    assert pipeline.get("rejected") == 1
    assert pipeline.get("waived") == 1
    assert pipeline.get("draft") == 1
    assert sum(pipeline.values()) == 4


@pytest.mark.asyncio
async def test_dashboard_evidence_completeness_exact_ratios(agent_client, tenant_admin_client):
    """One RCA with all 11 evidence items waived, one with exactly one item
    provided and the rest still missing — assert the per-evidence-type
    missing/waived/provided counts add up exactly, for a specific type."""
    fully_waived = await _create_rca(agent_client, title="All waived")
    await _waive_all_evidence(tenant_admin_client, fully_waived["id"])

    partially_done = await _create_rca(agent_client, title="Partially evidenced")
    await _waive_one_evidence_item(tenant_admin_client, partially_done["id"], "timeline_completed")

    completeness = (await agent_client.get("/api/v1/dashboards/rca/evidence-completeness")).json()["evidence_completeness"]

    # "timeline_completed" was waived on BOTH RCAs (once explicitly, once as
    # part of the full waive) -> 2 waived, 0 missing for that specific type.
    assert completeness["timeline_completed"] == {"waived": 2}
    # A type only touched by the full-waive RCA -> exactly 1 waived, 1 still
    # missing (from the partially-evidenced one).
    assert completeness["customer_impact_documented"] == {"waived": 1, "missing": 1}


@pytest.mark.asyncio
async def test_recordings_dashboard_summary_counts_evidence_links(agent_client):
    """Link one recording as RCA evidence and one as a plain support-call
    reference on two different tickets, and assert the summary distinguishes
    total recordings from the subset linked as RCA evidence."""
    from unittest.mock import patch as mock_patch

    async def _create_ticket():
        with mock_patch("app.workers.tasks_ai_ticket.process_new_ticket.delay", return_value=None):
            r = await agent_client.post("/api/v1/tickets", json={
                "title": "Ticket needing a recording", "description": "Customer escalation call.",
                "type": "incident", "priority": "medium",
            })
        assert r.status_code == 201, r.text
        return r.json()

    ticket_a = await _create_ticket()
    ticket_b = await _create_ticket()

    rec_a = (await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket_a["id"], "url": "https://teams.microsoft.com/l/a", "title": "Call A",
        "evidence_weight": "required_for_rca",
    })).json()
    rec_b = (await agent_client.post("/api/v1/support-recordings/link-from-url", json={
        "ticket_id": ticket_b["id"], "url": "https://teams.microsoft.com/l/b", "title": "Call B",
    })).json()
    assert rec_a["id"] and rec_b["id"]

    summary = (await agent_client.get("/api/v1/dashboards/recordings/summary")).json()
    assert summary["recordings"] == 2
    assert summary["linked_to_rca"] == 1
