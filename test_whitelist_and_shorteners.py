import asyncio
import json
import httpx
from app.services.safety_service import get_safety_service
from app.services.supervisor import get_supervisor
from app.services.compliance_rules_v2 import check_banned_urls

async def run_unit_tests():
    print("=== 1. UNIT TESTS: SafetyService.evaluate_safety ===")
    safety = get_safety_service()

    # Test 1: Compliant Charity Campaign
    r1 = await safety.evaluate_safety("test_sess_1", "Hope Wildlife Foundation annual charity gala fundraiser for endangered species. RSVP at https://hopewildlife.org/gala", "chat_message")
    print(f"Test 1 (Charity Valid Domain): allowed={r1.get('allowed')} category={r1.get('category')}")
    assert r1.get("allowed") is True, f"Expected allowed True, got {r1}"

    # Test 2: Compliant Political Campaign
    r2 = await safety.evaluate_safety("test_sess_2", "Vote Maria Sanchez for Mayor town hall campaign rally this Saturday. Details at https://sanchezformayor.com/rally", "chat_message")
    print(f"Test 2 (Political Valid Domain): allowed={r2.get('allowed')} category={r2.get('category')}")
    assert r2.get("allowed") is True, f"Expected allowed True, got {r2}"

    # Test 3: Charity Campaign with tiny.url
    r3 = await safety.evaluate_safety("test_sess_3", "Donate to our charity food bank drive https://tiny.url/food50 today", "chat_message")
    print(f"Test 3 (Charity with tiny.url): allowed={r3.get('allowed')} category={r3.get('category')}")
    assert r3.get("allowed") is False, f"Expected allowed False, got {r3}"
    assert r3.get("category") == "prohibited_url_shortener_carrier_policy"

    # Test 4: Political Campaign with bit.ly
    r4 = await safety.evaluate_safety("test_sess_4", "Join the voter turnout rally for Senator Davis at https://bit.ly/davis2026", "chat_message")
    print(f"Test 4 (Political with bit.ly): allowed={r4.get('allowed')} category={r4.get('category')}")
    assert r4.get("allowed") is False, f"Expected allowed False, got {r4}"
    assert r4.get("category") == "prohibited_url_shortener_carrier_policy"

    # Test 5: Commercial Retail Promotion (Blocked by Whitelist)
    r5 = await safety.evaluate_safety("test_sess_5", "Flash sale at Aura Boutique: get 30% discount on all shoes and fashion clothing using promo code FLASH30 at https://auraboutique.com", "chat_message")
    print(f"Test 5 (Retail Flash Sale): allowed={r5.get('allowed')} category={r5.get('category')}")
    assert r5.get("allowed") is False, f"Expected allowed False, got {r5}"
    assert r5.get("category") == "out_of_scope_non_whitelisted_domain"

    # Test 6: Dental Clinic Appointment (Blocked by Whitelist)
    r6 = await safety.evaluate_safety("test_sess_6", "Apex Dental Clinic reminder: your 6-month teeth cleaning and dental checkup appointment is due tomorrow.", "chat_message")
    print(f"Test 6 (Dental Clinic): allowed={r6.get('allowed')} category={r6.get('category')}")
    assert r6.get("allowed") is False, f"Expected allowed False, got {r6}"
    assert r6.get("category") == "out_of_scope_non_whitelisted_domain"

    # Test 7: Utility Electric Bill (Blocked by Whitelist)
    r7 = await safety.evaluate_safety("test_sess_7", "Metro Power utility bill overdue disconnection warning. Pay your electric bill at https://metropower.com", "chat_message")
    print(f"Test 7 (Utility Electric Bill): allowed={r7.get('allowed')} category={r7.get('category')}")
    assert r7.get("allowed") is False, f"Expected allowed False, got {r7}"
    assert r7.get("category") == "out_of_scope_non_whitelisted_domain"

    # Test 8: Chit-chat / Greeting (Allowed to proceed to welcome text)
    r8 = await safety.evaluate_safety("test_sess_8", "Hello, how can you help me?", "chat_message")
    print(f"Test 8 (Greeting Chit-chat): allowed={r8.get('allowed')}")
    assert r8.get("allowed") is True

    print("\n=== 2. UNIT TESTS: Supervisor.check_scope ===")
    sup = get_supervisor()
    ok_charity, _ = sup.check_scope("Charity non-profit fundraiser gala for homeless shelter")
    assert ok_charity is True
    ok_pol, _ = sup.check_scope("Maria Sanchez mayoral campaign town hall voter rally")
    assert ok_pol is True
    ok_shortener, msg = sup.check_scope("Charity drive bit.ly/give")
    assert ok_shortener is False and "shortener" in msg.lower()
    ok_comm, _ = sup.check_scope("Retail flash sale 30% discount at shoe store")
    assert ok_comm is False

    print("\n=== 3. UNIT TESTS: check_banned_urls in compliance_rules_v2 ===")
    res_b1 = check_banned_urls("Please visit https://tiny.url/event123 for tickets")
    assert res_b1.passed is False
    res_b2 = check_banned_urls("Please visit https://bit.ly/vote-now to register")
    assert res_b2.passed is False
    res_b3 = check_banned_urls("Please visit https://tinyurl.com/save-trees for info")
    assert res_b3.passed is False
    res_b4 = check_banned_urls("Please visit https://hopefoundation.org/gala to RSVP")
    assert res_b4.passed is True

    print("\n=== ALL UNIT TESTS PASSED ===")

async def run_api_e2e_tests():
    print("\n=== 4. E2E TESTS: Chat SSE API (http://127.0.0.1:8080/chat/send) ===")
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", timeout=30.0) as client:
        # E2E Test 1: Retail promotion rejected via API
        r_retail = await client.post("/api/chat/send", json={
            "session_id": "e2e_retail_test",
            "message": "Launch a flash sale campaign offering 40% discount on summer shoes and dresses at our retail boutique store https://auraboutique.com"
        })
        assert r_retail.status_code == 200
        retail_sse = r_retail.text
        assert "Policy Violation" in retail_sse or "Charity & Political Events Whitelist" in retail_sse or "Request Declined" in retail_sse
        print("E2E Test 1 Passed: Retail promotion correctly rejected by Gateway API.")

        # E2E Test 2: Banned tiny.url rejected via API
        r_tiny = await client.post("/api/chat/send", json={
            "session_id": "e2e_tiny_test",
            "message": "Hope Foundation charity fundraiser gala this Saturday. Donate now at https://tiny.url/hope2026"
        })
        assert r_tiny.status_code == 200
        tiny_sse = r_tiny.text
        assert "prohibited_url_shortener_carrier_policy" in tiny_sse or "tiny.url" in tiny_sse
        print("E2E Test 2 Passed: tiny.url link in charity campaign correctly blocked with carrier 10DLC policy violation.")

        # E2E Test 3: Banned bit.ly rejected via API
        r_bitly = await client.post("/api/chat/send", json={
            "session_id": "e2e_bitly_test",
            "message": "Join the campaign rally for Maria Sanchez for Mayor this weekend! RSVP at https://bit.ly/sanchez-rally"
        })
        assert r_bitly.status_code == 200
        bitly_sse = r_bitly.text
        assert "prohibited_url_shortener_carrier_policy" in bitly_sse or "bit.ly" in bitly_sse
        print("E2E Test 3 Passed: bit.ly link in political campaign correctly blocked with carrier 10DLC policy violation.")

        # E2E Test 4: Compliant Charity Campaign via API
        r_charity = await client.post("/api/chat/send", json={
            "session_id": "e2e_charity_valid",
            "message": "Hope Foundation annual charity gala fundraiser for clean water relief on Dec 15th. Full details at https://hopefoundation.org/gala"
        })
        assert r_charity.status_code == 200
        charity_sse = r_charity.text
        assert "Policy Violation" not in charity_sse
        print("E2E Test 4 Passed: Compliant Charity campaign accepted and processed.")

        # E2E Test 5: Compliant Political Campaign via API
        r_political = await client.post("/api/chat/send", json={
            "session_id": "e2e_political_valid",
            "message": "Vote for Maria Sanchez for Mayor town hall campaign rally on Nov 20th. RSVP at https://sanchezformayor.com/rally"
        })
        assert r_political.status_code == 200
        political_sse = r_political.text
        assert "Policy Violation" not in political_sse
        print("E2E Test 5 Passed: Compliant Political campaign accepted and processed.")

    print("\n=== ALL E2E TESTS PASSED SUCCESSFULLY! ===")

if __name__ == "__main__":
    asyncio.run(run_unit_tests())
    asyncio.run(run_api_e2e_tests())
