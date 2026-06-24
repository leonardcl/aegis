"""Seed the Aegis CFO database with realistic demo data.

Usage:
    python seed.py
"""
from datetime import date, datetime, timedelta

from app import create_app
from app.extensions import db
from app.models import (
    AgentMessage,
    ApprovalRequest,
    AuditException,
    AuditReport,
    LedgerEntry,
    ProcurementRequest,
    VendorOption,
)
from app.services.procurement_service import generate_recommendation
from app.services.guardrail_service import send_to_guardrail

app = create_app()


def run():
    with app.app_context():
        # Reset
        db.drop_all()
        db.create_all()

        now = datetime.utcnow()
        today = date.today()

        # ----------------------------------------------------------------- #
        # Procurement requests + vendors
        # ----------------------------------------------------------------- #
        r1 = ProcurementRequest(
            title="Cloud GPU compute for Q3 model training",
            description="Reserved GPU capacity for the ML team's summer training runs.",
            category="Cloud Infrastructure",
            quantity_or_usage="8x A100 / 720 hrs per month",
            deadline=today + timedelta(days=12),
            budget_ceiling=42000,
            must_haves="A100 80GB, US region, committed-use discount",
            nice_to_haves="Spot fallback, dashboard",
            priority_price=5, priority_time=3, priority_risk=4, priority_quality=4, priority_terms=2,
            status="analyzing",
        )
        r1.vendors = [
            VendorOption(name="NimbusCloud", price=38500, lead_time_days=3,
                         score_price=82, score_time=88, score_risk=75, score_quality=80, score_terms=70,
                         notes="Best committed-use discount."),
            VendorOption(name="HyperScale GPU", price=41200, lead_time_days=7,
                         score_price=74, score_time=66, score_risk=85, score_quality=88, score_terms=78,
                         notes="Premium support, slower onboarding."),
            VendorOption(name="BudgetGrid", price=33900, lead_time_days=14,
                         score_price=92, score_time=45, score_risk=55, score_quality=62, score_terms=60,
                         notes="Cheapest but longer lead time."),
        ]

        r2 = ProcurementRequest(
            title="Annual SOC 2 audit engagement",
            description="External auditor for the FY26 SOC 2 Type II report.",
            category="Compliance",
            quantity_or_usage="1 engagement",
            deadline=today + timedelta(days=30),
            budget_ceiling=60000,
            must_haves="Big-4 or equivalent, SOC 2 Type II",
            nice_to_haves="ISO 27001 add-on",
            priority_price=3, priority_time=2, priority_risk=5, priority_quality=5, priority_terms=4,
            status="recommended",
        )
        r2.vendors = [
            VendorOption(name="Meridian Assurance", price=58000, lead_time_days=20,
                         score_price=70, score_time=72, score_risk=92, score_quality=90, score_terms=85,
                         notes="Strong reputation."),
            VendorOption(name="ClearAudit LLP", price=47500, lead_time_days=25,
                         score_price=84, score_time=60, score_risk=80, score_quality=82, score_terms=78),
        ]

        r3 = ProcurementRequest(
            title="Office laptop refresh (15 units)",
            description="Replace aging dev laptops.",
            category="Hardware",
            quantity_or_usage="15 laptops",
            deadline=today + timedelta(days=20),
            budget_ceiling=45000,
            must_haves="32GB RAM, 1TB SSD, 3yr warranty",
            nice_to_haves="On-site service",
            priority_price=4, priority_time=3, priority_risk=2, priority_quality=4, priority_terms=3,
            status="draft",
        )
        r3.vendors = [
            VendorOption(name="TechSupply Co", price=44250, lead_time_days=10,
                         score_price=78, score_time=80, score_risk=70, score_quality=82, score_terms=75),
            VendorOption(name="Unverified Vendor", price=39000, lead_time_days=6,
                         score_price=88, score_time=85, score_risk=30, score_quality=60, score_terms=40,
                         notes="No track record — flagged."),
        ]

        r4 = ProcurementRequest(
            title="Enterprise data warehouse expansion",
            description="Additional warehouse capacity and BI seats.",
            category="Cloud Infrastructure",
            quantity_or_usage="Tier-3 plan, 50 seats",
            deadline=today + timedelta(days=8),
            budget_ceiling=80000,
            must_haves="SSO, row-level security",
            nice_to_haves="Reserved pricing",
            priority_price=3, priority_time=4, priority_risk=4, priority_quality=4, priority_terms=3,
            status="recommended",
        )
        r4.vendors = [
            VendorOption(name="DataVault Enterprise", price=72000, lead_time_days=15,
                         score_price=68, score_time=70, score_risk=82, score_quality=86, score_terms=80),
        ]

        db.session.add_all([r1, r2, r3, r4])
        db.session.commit()

        # Generate agent recommendations / vendor scores
        for r in (r1, r2, r4):
            generate_recommendation(r)
        # r3: explicitly recommend the blocklisted vendor so the guardrail's
        # payee-refusal beat is demonstrable (a price-attractive but unvetted payee).
        unverified = next(v for v in r3.vendors if v.name == "Unverified Vendor")
        r3.recommended_vendor_id = unverified.id
        db.session.commit()

        # ----------------------------------------------------------------- #
        # Approvals — produced by the REAL guardrail so the policy decisions and
        # rules always match the live engine (no hand-typed, drift-prone values).
        #   r4 ($72k) / r2 ($58k)  -> BLOCK  per_transaction_cap
        #   r3 (Unverified Vendor) -> BLOCK  payee_blocklist  (the refusal beat)
        #   r1 ($38.5k NimbusCloud)-> NEEDS_APPROVAL  above_auto_approve_limit
        # ----------------------------------------------------------------- #
        for r in (r1, r2, r3, r4):
            send_to_guardrail(r)

        # ----------------------------------------------------------------- #
        # Ledger entries
        # ----------------------------------------------------------------- #
        ledger = [
            LedgerEntry(request_id=None, timestamp=now - timedelta(days=6), action="approve_spend",
                        payee="CloudFlare Inc", amount=1200, reason="CDN monthly renewal",
                        policy_decision="ALLOW", policy_rule="within_auto_approve_limit",
                        outcome="posted", transaction_id="txn_cf_001", created_by="hermes_agent"),
            LedgerEntry(request_id=None, timestamp=now - timedelta(days=5), action="approve_spend",
                        payee="Atlassian", amount=3400, reason="Jira + Confluence seats",
                        policy_decision="ALLOW", policy_rule="within_auto_approve_limit",
                        outcome="posted", transaction_id="txn_atl_014", created_by="hermes_agent"),
            LedgerEntry(request_id=None, timestamp=now - timedelta(days=4), action="approve_spend",
                        payee="Slack", amount=2100, reason="Workspace upgrade",
                        policy_decision="ALLOW", policy_rule="within_auto_approve_limit",
                        outcome="posted", transaction_id="txn_slk_009", created_by="management"),
            LedgerEntry(request_id=r1.id, timestamp=now - timedelta(days=3), action="approve_spend",
                        payee="NimbusCloud", amount=12000, reason="Partial GPU prepayment",
                        policy_decision="NEEDS_APPROVAL", policy_rule="above_auto_approve_limit",
                        outcome="posted", transaction_id="txn_nim_201", created_by="management"),
            LedgerEntry(request_id=None, timestamp=now - timedelta(days=2), action="block_spend",
                        payee="Sanctioned Ltd", amount=9000, reason="Payee on blocklist",
                        policy_decision="BLOCK", policy_rule="payee_blocklist",
                        outcome="blocked", transaction_id="txn_blk_777", created_by="hermes_agent"),
            LedgerEntry(request_id=None, timestamp=now - timedelta(days=1), action="approve_spend",
                        payee="AWS", amount=8600, reason="Monthly cloud bill",
                        policy_decision="NEEDS_APPROVAL", policy_rule="above_auto_approve_limit",
                        outcome="posted", transaction_id="txn_aws_330", created_by="management"),
            LedgerEntry(request_id=None, timestamp=now - timedelta(hours=4), action="approve_spend",
                        payee="GitHub", amount=2400, reason="Enterprise seats",
                        policy_decision="ALLOW", policy_rule="within_auto_approve_limit",
                        outcome="posted", transaction_id="txn_gh_120", created_by="hermes_agent"),
        ]
        db.session.add_all(ledger)
        db.session.commit()

        # ----------------------------------------------------------------- #
        # Audit report — GENERATED by the deterministic engine so its numbers
        # (total spend, projected savings, reconciliation status, exceptions)
        # always match what a live audit produces. We force the offline path here
        # so seeding stays fast + deterministic regardless of Hermes availability.
        # ----------------------------------------------------------------- #
        from app.services import hermes_service
        app.config["HERMES_API_URL"] = ""   # deterministic local council for seeding
        hermes_service.run_audit_council(period_days=30, persist=True)

        # ----------------------------------------------------------------- #
        # Sample agent chat history
        # ----------------------------------------------------------------- #
        db.session.add_all([
            AgentMessage(role="user", content="What needs my approval today?", page_context="dashboard"),
            AgentMessage(role="assistant",
                         content="Three requests need attention: two are blocked by the hard spend cap and one cloud GPU request needs your sign-off.",
                         page_context="dashboard"),
        ])
        db.session.commit()

        print("✅ Seeded Aegis CFO database.")
        print(f"   Procurement requests: {ProcurementRequest.query.count()}")
        print(f"   Vendors:              {VendorOption.query.count()}")
        print(f"   Approvals:            {ApprovalRequest.query.count()}")
        print(f"   Ledger entries:       {LedgerEntry.query.count()}")
        print(f"   Audit reports:        {AuditReport.query.count()}")
        print(f"   Audit exceptions:     {AuditException.query.count()}")


if __name__ == "__main__":
    run()
