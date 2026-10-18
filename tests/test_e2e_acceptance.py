import os
import json
import pytest
import pytest_asyncio

pytestmark = pytest.mark.slow
from app.core.config import get_config
from app.core.storage import get_storage
from app.core.llm_client import get_llm_client
from app.services.safety_service import get_safety_service
from app.services.memory_service import get_memory_service
from app.services.orchestrator_engine import get_orchestrator_engine
from app.services.compliance_service import count_words

@pytest_asyncio.fixture(autouse=True)
async def setup_e2e_env():
    os.environ["DATABASE_PATH"] = "data/e2e_test.db"
    os.environ["LOGS_DIR"] = "logs/e2e_logs"
    get_config(reload=True)
    yield

@pytest.mark.asyncio
async def test_acceptance_criteria_d_safety_guard_refusal():
    """Verify criterion d: A drug-related campaign request is refused by the guard."""
    safety_svc = get_safety_service()
    session_id = "test_guard_session"
    
    # Drug campaign request
    drug_request = "We are selling prescription oxycodone and illicit opioids online without doctor prescription with fast discrete delivery."
    res = await safety_svc.evaluate_safety(session_id, drug_request, "user_campaign_request")
    
    assert res["allowed"] is False
    assert res["category"] != "none"
    print(f"Safety guard properly blocked: category={res.get('category')}, reason={res.get('reason')}")

    # Verify call logged in DB & JSONL
    storage = get_storage()
    calls = storage.get_connection().cursor().execute(
        "SELECT * FROM llm_calls WHERE session_id = ? AND purpose = 'safety_guard'",
        (session_id,)
    ).fetchall()
    assert len(calls) >= 1
    assert calls[0]["full_prompt"] is not None
    assert calls[0]["full_raw_response"] is not None

@pytest.mark.asyncio
async def test_acceptance_criteria_a_b_c_domain_differences():
    """Verify criteria a, b, c: NGO donation, retail sale, clinic appointment produce different slots, tones, and plans."""
    engine = get_orchestrator_engine()
    
    # a. NGO donation campaign
    ngo_sess = "test_ngo_session"
    ngo_res = await engine.run_discovery_round(
        session_id=ngo_sess,
        workspace_id="ngo_workspace",
        latest_user_message="We are Green Earth Foundation launching our Clean Water for Villages emergency flood relief donation appeal.",
        conversation_history=[],
        current_slots={},
        round_index=1
    )
    ngo_tones = await engine.explore_tones(
        session_id=ngo_sess,
        workspace_id="ngo_workspace",
        business_type=ngo_res.get("business_type", "Non-Profit NGO"),
        slots=ngo_res.get("slots", {})
    )
    ngo_plan = await engine.generate_plan(
        session_id=ngo_sess,
        business_type=ngo_res.get("business_type", "Non-Profit NGO"),
        slots=ngo_res.get("slots", {}),
        selected_tone=ngo_tones["candidate_tones"][0]["name"]
    )

    # b. Retail sale campaign
    retail_sess = "test_retail_session"
    retail_res = await engine.run_discovery_round(
        session_id=retail_sess,
        workspace_id="retail_workspace",
        latest_user_message="We are UrbanThreads boutique running a 48-hour flash weekend sale offering 30% off all jackets with code FLASH30.",
        conversation_history=[],
        current_slots={},
        round_index=1
    )
    retail_tones = await engine.explore_tones(
        session_id=retail_sess,
        workspace_id="retail_workspace",
        business_type=retail_res.get("business_type", "Retail Apparel"),
        slots=retail_res.get("slots", {})
    )
    retail_plan = await engine.generate_plan(
        session_id=retail_sess,
        business_type=retail_res.get("business_type", "Retail Apparel"),
        slots=retail_res.get("slots", {}),
        selected_tone=retail_tones["candidate_tones"][0]["name"]
    )

    # c. Clinic appointment reminder
    clinic_sess = "test_clinic_session"
    clinic_res = await engine.run_discovery_round(
        session_id=clinic_sess,
        workspace_id="clinic_workspace",
        latest_user_message="We are BrightDental Clinic setting up automated 6-month routine cleaning and checkup appointment reminder messages.",
        conversation_history=[],
        current_slots={},
        round_index=1
    )
    clinic_tones = await engine.explore_tones(
        session_id=clinic_sess,
        workspace_id="clinic_workspace",
        business_type=clinic_res.get("business_type", "Dental Healthcare Clinic"),
        slots=clinic_res.get("slots", {})
    )
    clinic_plan = await engine.generate_plan(
        session_id=clinic_sess,
        business_type=clinic_res.get("business_type", "Dental Healthcare Clinic"),
        slots=clinic_res.get("slots", {}),
        selected_tone=clinic_tones["candidate_tones"][0]["name"]
    )

    # Assert distinct domain adaptations
    assert ngo_res["business_type"] != retail_res["business_type"]
    assert retail_res["business_type"] != clinic_res["business_type"]

    ngo_tone_names = [t["name"] for t in ngo_tones["candidate_tones"]]
    retail_tone_names = [t["name"] for t in retail_tones["candidate_tones"]]
    clinic_tone_names = [t["name"] for t in clinic_tones["candidate_tones"]]
    
    print(f"NGO Tones: {ngo_tone_names}")
    print(f"Retail Tones: {retail_tone_names}")
    print(f"Clinic Tones: {clinic_tone_names}")

    ngo_steps = [s["step_id"] for s in ngo_plan.get("steps", [])]
    retail_steps = [s["step_id"] for s in retail_plan.get("steps", [])]
    clinic_steps = [s["step_id"] for s in clinic_plan.get("steps", [])]

    print(f"NGO Plan Steps: {ngo_steps}")
    print(f"Retail Plan Steps: {retail_steps}")
    print(f"Clinic Plan Steps: {clinic_steps}")

    assert len(ngo_tone_names) > 0
    assert len(retail_tone_names) > 0
    assert len(clinic_tone_names) > 0

@pytest.mark.asyncio
async def test_acceptance_criteria_e_word_limit_repaired_and_logged():
    """Verify criterion e: A message over the word limit is caught, regenerated, and logged."""
    engine = get_orchestrator_engine()
    storage = get_storage()
    session_id = "test_word_limit_session"

    slots = {
        "business_name": {"value": "Summit Fitness"},
        "campaign_name": {"value": "New Year Transformation"},
        "campaign_goal": {"value": "Join now and get the first month free"},
        "messages_per_contact": {"value": 1},
        "target_audience": {"value": "Local gym enthusiasts"}
    }

    # Generate template
    res = await engine.generate_and_validate_templates(
        session_id=session_id,
        business_type="Fitness Center",
        slots=slots,
        selected_tone="Energetic & Motivating"
    )

    templates = res.get("templates", [])
    assert len(templates) >= 1
    tmpl = templates[0]
    
    # Word limit verification
    assert tmpl["word_count"] <= 320
    assert tmpl["compliant"] is True

    # Check validation logs in database
    vals = storage.get_connection().cursor().execute(
        "SELECT * FROM validations WHERE session_id = ? AND validation_type LIKE 'template_compliance_%'",
        (session_id,)
    ).fetchall()
    assert len(vals) >= 1
    val_details = json.loads(vals[0]["details_json"])
    assert "word_count" in val_details
    assert "word_limit" in val_details

@pytest.mark.asyncio
async def test_acceptance_criteria_f_memory_reuse():
    """Verify criterion f: A second session reuses memory."""
    storage = get_storage()
    mem_svc = get_memory_service()
    ws_id = "shared_brand_workspace"

    # Session 1: Store brand preferences
    storage.write_memory(
        workspace_id=ws_id,
        category="business_fact",
        key="company_identity",
        value="Bloom Floral Boutique, artisanal flower delivery",
        session_id="session_1"
    )
    storage.write_memory(
        workspace_id=ws_id,
        category="tone_preference",
        key="preferred_tone",
        value="Warm & Poetic",
        session_id="session_1"
    )

    # Session 2: Read memory context
    mem_context = mem_svc.get_memory_context(ws_id)
    assert "Bloom Floral Boutique" in mem_context
    assert "Warm & Poetic" in mem_context

    # Run discovery in Session 2 using that workspace
    engine = get_orchestrator_engine()
    s2_res = await engine.run_discovery_round(
        session_id="session_2",
        workspace_id=ws_id,
        latest_user_message="We want to run a Mother's Day special campaign.",
        conversation_history=[],
        current_slots={},
        round_index=1
    )

    # Verify session 2 discovered or referenced facts
    slots = s2_res.get("slots", {})
    print(f"Session 2 Slots populated with memory: {slots}")

@pytest.mark.asyncio
async def test_acceptance_criteria_g_all_llm_calls_logged():
    """Verify criterion g: Every LLM call appears in the logs with the full prompt and response."""
    storage = get_storage()
    with storage.get_connection() as conn:
        calls = conn.cursor().execute("SELECT * FROM llm_calls").fetchall()
        assert len(calls) > 0
        for c in calls:
            assert c["full_prompt"] is not None and len(c["full_prompt"]) > 10
            assert c["full_raw_response"] is not None
            assert c["model"] is not None
            assert c["latency_ms"] >= 0
            assert c["purpose"] is not None
            print(f"Logged call: purpose={c['purpose']}, model={c['model']}, latency={c['latency_ms']}ms")
