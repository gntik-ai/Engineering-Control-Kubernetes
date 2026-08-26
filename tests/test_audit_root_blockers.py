from engineering_control.workflows.definitions import AuditCampaignWorkflow


def test_static_audit_never_requires_a_live_pair_snapshot() -> None:
    stages = AuditCampaignWorkflow._audit_stages(e2e=False, preset="static-full")

    assert "deployment.render" in stages
    assert "deployment.compare_live" not in stages
    assert "audit.kubernetes_observe" not in stages
    assert "audit.correlate_runtime" not in stages


def test_runtime_audit_routes_live_comparison_and_observation() -> None:
    stages = AuditCampaignWorkflow._audit_stages(e2e=True, preset="e2e-smoke")

    assert stages.index("deployment.compare_live") < stages.index("audit.api_flow")
    assert "audit.kubernetes_observe" in stages
    assert "audit.correlate_runtime" in stages
