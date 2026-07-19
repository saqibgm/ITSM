"""RCA Governance (specs/08, Phase 1+2) — lifecycle, permissions, evidence,
action items, AI draft graceful-degrade, dashboards."""
import pytest


async def _create_rca(client, **over):
    body = {"title": "RCA for outage"} | over
    r = await client.post("/api/v1/rca", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _waive_all_evidence(admin_client, rca_id):
    """Test helper — clears the evidence-checklist gate so submit() can proceed
    without having to wire up every linked entity."""
    checklist = (await admin_client.get(f"/api/v1/rca/{rca_id}/evidence-checklist")).json()["items"]
    for item in checklist:
        r = await admin_client.post(
            f"/api/v1/rca/{rca_id}/evidence-checklist/{item['id']}/waive",
            json={"reason": "test setup — not applicable"},
        )
        assert r.status_code == 200, r.text


async def _fill_required_sections(admin_client, rca_id):
    r = await admin_client.patch(f"/api/v1/rca/{rca_id}", json={
        "root_cause_statement": "Root cause confirmed.",
        "customer_impact": "No customer impact.",
    })
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_create_rca_seeds_evidence_checklist(agent_client):
    rca = await _create_rca(agent_client)
    assert rca["status"] == "draft"
    checklist = (await agent_client.get(f"/api/v1/rca/{rca['id']}/evidence-checklist")).json()["items"]
    assert len(checklist) == 11
    assert all(i["status"] == "missing" for i in checklist)


@pytest.mark.asyncio
async def test_submit_blocked_until_evidence_and_sections_complete(agent_client):
    rca = await _create_rca(agent_client)
    blocked = await agent_client.post(f"/api/v1/rca/{rca['id']}/submit")
    assert blocked.status_code == 400, blocked.text


@pytest.mark.asyncio
async def test_full_lifecycle_draft_to_completed(agent_client, tenant_admin_client):
    rca = await _create_rca(agent_client)
    rca_id = rca["id"]

    await _fill_required_sections(tenant_admin_client, rca_id)
    await _waive_all_evidence(tenant_admin_client, rca_id)

    submitted = await agent_client.post(f"/api/v1/rca/{rca_id}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "under_review"

    approved = await tenant_admin_client.post(f"/api/v1/rca/{rca_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    action = await agent_client.post(f"/api/v1/rca/{rca_id}/actions", json={
        "description": "Add monitoring", "priority": "critical",
    })
    assert action.status_code == 200, action.text
    action_id = action.json()["id"]

    # creating the action item auto-advances approved -> actions_in_progress
    mid = await agent_client.get(f"/api/v1/rca/{rca_id}")
    assert mid.json()["status"] == "actions_in_progress"

    blocked_complete = await tenant_admin_client.post(f"/api/v1/rca/{rca_id}/waive", json={"reason": "n/a"})
    assert blocked_complete.status_code == 409, blocked_complete.text  # actions_in_progress has no "waived" edge

    mark_done = await agent_client.patch(f"/api/v1/rca/{rca_id}/actions/{action_id}", json={"status": "done"})
    assert mark_done.status_code == 200, mark_done.text

    # critical + done but unverified -> completion still blocked
    still_blocked = await agent_client.post(f"/api/v1/rca/{rca_id}/complete")
    assert still_blocked.status_code == 400, still_blocked.text

    verified = await agent_client.post(f"/api/v1/rca/{rca_id}/actions/{action_id}/verify")
    assert verified.status_code == 200, verified.text

    completed = await agent_client.post(f"/api/v1/rca/{rca_id}/complete")
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_agent_cannot_approve_or_waive(agent_client):
    rca = await _create_rca(agent_client)
    rca_id = rca["id"]
    assert (await agent_client.post(f"/api/v1/rca/{rca_id}/approve")).status_code == 403
    assert (await agent_client.post(f"/api/v1/rca/{rca_id}/waive", json={"reason": "x"})).status_code == 403


@pytest.mark.asyncio
async def test_waive_requires_reason(tenant_admin_client, agent_client):
    rca = await _create_rca(agent_client)
    rca_id = rca["id"]
    r = await tenant_admin_client.post(f"/api/v1/rca/{rca_id}/waive", json={})
    assert r.status_code == 400, r.text
    ok = await tenant_admin_client.post(f"/api/v1/rca/{rca_id}/waive", json={"reason": "not needed"})
    assert ok.status_code == 200 and ok.json()["status"] == "waived"


@pytest.mark.asyncio
async def test_reject_requires_reason_and_returns_to_draft(agent_client, tenant_admin_client):
    rca = await _create_rca(agent_client)
    rca_id = rca["id"]
    await _fill_required_sections(tenant_admin_client, rca_id)
    await _waive_all_evidence(tenant_admin_client, rca_id)
    await agent_client.post(f"/api/v1/rca/{rca_id}/submit")

    missing_reason = await tenant_admin_client.post(f"/api/v1/rca/{rca_id}/reject", json={})
    assert missing_reason.status_code == 400

    rejected = await tenant_admin_client.post(f"/api/v1/rca/{rca_id}/reject", json={"reason": "needs more detail"})
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"

    back_to_draft = await agent_client.post(f"/api/v1/rca/{rca_id}/submit")
    # rejected -> draft is the only legal edge, not under_review directly
    assert back_to_draft.status_code == 409


@pytest.mark.asyncio
async def test_invalid_transition_returns_409_with_valid_next(agent_client, tenant_admin_client):
    rca = await _create_rca(agent_client)
    # draft's only legal edges are under_review/waived — reject isn't reachable directly
    r = await tenant_admin_client.post(f"/api/v1/rca/{rca['id']}/reject", json={"reason": "x"})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"]["code"] == "INVALID_STATE_TRANSITION"
    assert body["error"]["details"]["valid_transitions"] == ["under_review", "waived"]


@pytest.mark.asyncio
async def test_generate_ai_draft_graceful_degrades_without_api_key(agent_client):
    """No ANTHROPIC_API_KEY is configured in the test environment, so the AI
    call must fail internally and the draft must still come back usable
    (template fallback) instead of raising into the route."""
    rca = await _create_rca(agent_client)
    r = await agent_client.post(f"/api/v1/rca/{rca['id']}/generate-ai-draft")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]
    assert isinstance(body["action_items"], list)


@pytest.mark.asyncio
async def test_accept_ai_draft_populates_rca_fields(agent_client):
    rca = await _create_rca(agent_client)
    rca_id = rca["id"]
    draft = (await agent_client.post(f"/api/v1/rca/{rca_id}/generate-ai-draft")).json()
    accepted = await agent_client.post(f"/api/v1/rca/{rca_id}/ai-draft/{draft['id']}/accept")
    assert accepted.status_code == 200, accepted.text
    got = await agent_client.get(f"/api/v1/rca/{rca_id}")
    assert got.json()["executive_summary"]


@pytest.mark.asyncio
async def test_rca_dashboard_summary_counts_created_rca(agent_client):
    await _create_rca(agent_client)
    r = await agent_client.get("/api/v1/dashboards/rca/summary")
    assert r.status_code == 200
    assert r.json()["required_count"] >= 1


@pytest.mark.asyncio
async def test_rca_pipeline_dashboard_groups_by_status(agent_client):
    await _create_rca(agent_client)
    r = await agent_client.get("/api/v1/dashboards/rca/pipeline")
    assert r.status_code == 200
    assert "draft" in r.json()["pipeline"]


@pytest.mark.asyncio
async def test_rca_policy_admin_requires_manager_or_admin(agent_client, tenant_admin_client):
    assert (await agent_client.get("/api/v1/tenant/rca-policies")).status_code == 403
    r = await tenant_admin_client.post("/api/v1/tenant/rca-policies", json={
        "name": "Sev1 always required", "status": "active",
        "conditions": [{"field": "severity_rank", "op": "eq", "value": 1}],
        "outputs": {"required": True, "due_days": 3},
    })
    assert r.status_code == 200, r.text
    policy_id = r.json()["id"]

    test_match = await tenant_admin_client.post(f"/api/v1/tenant/rca-policies/{policy_id}/test", json={
        "severity_rank": 1,
    })
    assert test_match.status_code == 200
    assert test_match.json()["required"] is True

    test_no_match = await tenant_admin_client.post(f"/api/v1/tenant/rca-policies/{policy_id}/test", json={
        "severity_rank": 3,
    })
    assert test_no_match.json()["required"] is False
