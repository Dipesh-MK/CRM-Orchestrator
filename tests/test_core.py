import os
import json
import pytest
from app.core.config import get_config
from app.core.storage import get_storage
from app.adapters.input_adapter import (
    TextInputAdapter,
    CsvInputAdapter,
    detect_columns,
    normalize_phone_e164,
    mask_phone
)
from app.services.compliance_service import check_template_compliance, count_words
from app.services.contact_service import get_contact_service
from app.services.safety_service import get_safety_service
from app.services.memory_service import get_memory_service
from app.services.orchestrator_engine import get_orchestrator_engine

@pytest.fixture(autouse=True)
def setup_test_env():
    # Use test db
    os.environ["DATABASE_PATH"] = "data/test_campaignforge.db"
    os.environ["LOGS_DIR"] = "logs/test_logs"
    yield

def test_contact_adapters_and_normalization():
    # 1. Text input adapter
    pasted = b"John Doe, +14155552671\nJane Smith, 9876543210\nInvalid Row"
    adapter = TextInputAdapter()
    rows = adapter.parse(pasted)
    assert len(rows) == 3
    assert rows[0]["Name"] == "John Doe"

    # 2. Phone normalization & masking
    norm_us = normalize_phone_e164("+1 (415) 555-2671")
    assert norm_us == "+14155552671"
    masked = mask_phone(norm_us)
    assert masked.startswith("+1")
    assert masked.endswith("2671")
    assert "••" in masked

    # 3. Country code fallback
    norm_in = normalize_phone_e164("9876543210", default_country="IN")
    assert norm_in == "+919876543210"

    # 4. Column detection
    sample = {"Donor Name": "Alice", "Mobile No": "9999999999", "Amount": "500"}
    n_col, p_col, extra = detect_columns(sample)
    assert n_col == "Donor Name"
    assert p_col == "Mobile No"
    assert "Amount" in extra

def test_template_compliance_rules():
    word_limit = 50
    optout = "Reply STOP to unsubscribe."

    # Compliant message
    valid_msg = f"Hello {{{{customer_name}}}}, welcome to Apex Clinic! We have an opening tomorrow. Visit {{{{link}}}}.\n\n{optout}"
    res = check_template_compliance(valid_msg, "Apex Clinic", word_limit, optout, link_or_contact="https://clinic.com")
    assert res["compliant"] is True
    assert res["word_count"] < word_limit

    # Over word limit
    long_msg = "word " * 60 + optout
    res_long = check_template_compliance(long_msg, "Apex Clinic", word_limit, optout)
    assert res_long["compliant"] is False
    assert any("Exceeds word limit" in v for v in res_long["violations"])

    # Missing optout
    no_optout = "Hello from Apex Clinic! Special promo today."
    res_no_optout = check_template_compliance(no_optout, "Apex Clinic", word_limit, optout)
    assert res_no_optout["compliant"] is False
    assert any("opt-out" in v for v in res_no_optout["violations"])

    # Missing business name
    missing_name = f"Hello! Special promo today at our office.\n\n{optout}"
    res_no_name = check_template_compliance(missing_name, "Apex Clinic", word_limit, optout)
    assert res_no_name["compliant"] is False
    assert any("Missing business name" in v for v in res_no_name["violations"])

    # ALL-CAPS sentence check
    all_caps = f"THIS IS A MAJOR ANNOUNCEMENT FOR EVERYONE. Please come to Apex Clinic.\n\n{optout}"
    res_caps = check_template_compliance(all_caps, "Apex Clinic", word_limit, optout)
    assert res_caps["compliant"] is False
    assert any("ALL-CAPS" in v for v in res_caps["violations"])

    # Excessive punctuation
    punct = f"Amazing discount at Apex Clinic!!! Visit now.\n\n{optout}"
    res_punct = check_template_compliance(punct, "Apex Clinic", word_limit, optout)
    assert res_punct["compliant"] is False
    assert any("excessive punctuation" in v for v in res_punct["violations"])

def test_suppression_list_handling():
    contact_svc = get_contact_service()
    # Write temporary suppression
    supp_path = "data/test_suppression.json"
    os.environ["SUPPRESSION_FILE"] = supp_path
    with open(supp_path, "w") as f:
        json.dump(["+14155552671"], f)

    pasted = b"John Doe, +14155552671\nJane Smith, +14155559999\nJane Smith, +14155559999"
    res = contact_svc.process_contacts("test_sess", pasted, "test.txt", country_code="US")
    assert res["success"] is True
    assert res["suppressed_count"] == 1
    assert res["duplicates_count"] == 1
    assert res["valid_count"] == 1
    assert res["contacts"][0]["name"] == "Jane Smith"

def test_memory_storage_and_retrieval():
    storage = get_storage()
    ws_id = "test_workspace_alpha"

    # Write memory
    storage.write_memory(ws_id, "business_fact", "brand_name", "Gourmet Bites", 1.0, session_id="s1")
    storage.write_memory(ws_id, "tone_preference", "favorite_tone", "Witty & Playful", 0.9, session_id="s1")

    # Read memory
    mem = storage.get_workspace_memory(ws_id)
    assert len(mem) >= 2
    keys = {m["key"] for m in mem}
    assert "brand_name" in keys
    assert "favorite_tone" in keys

    # Context string check
    mem_svc = get_memory_service()
    context = mem_svc.get_memory_context(ws_id)
    assert "Gourmet Bites" in context
    assert "Witty & Playful" in context

    # Delete memory
    storage.delete_workspace_memory(ws_id, "business_fact", "brand_name", session_id="s1")
    mem_after = storage.get_workspace_memory(ws_id)
    assert not any(m["key"] == "brand_name" for m in mem_after)
