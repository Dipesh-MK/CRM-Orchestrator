import asyncio
import httpx

async def test_tone_guardrails():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", timeout=120.0) as client:
        # 1. Test Aggressive Tone Request (Pre-Guardrail Block)
        payload_aggressive = {
            "session_id": "test_tone_session_1",
            "business_type": "Healthcare & Clinic",
            "context": "Annual dental checkup discount",
            "organization_name": "Apex Dental Clinic",
            "link_or_contact": "https://apexdentalclinic.com/clean",
            "user_feedback": "very very aggresive"
        }
        res_agg = await client.post("/api/campaign/regenerate_tone_previews", json=payload_aggressive)
        print("Aggressive tone HTTP code:", res_agg.status_code)
        data_agg = res_agg.json()
        print("Aggressive tone response status:", data_agg.get("status"))
        print("Aggressive tone reason:", data_agg.get("reason"))
        assert data_agg.get("status") == "blocked", "Expected aggressive tone to be blocked by pre-guardrail"
        assert "prohibited tone register detected" in data_agg.get("reason", "").lower(), "Expected carrier ethics reason"
        assert len(data_agg.get("question_options", [])) > 0, "Expected remediation options"
        print("✓ Pre-guardrail successfully blocked 'very very aggresive' tone request!")

        # 2. Test Compliant Tone Request (Pre-Guard and Post-Guard Pass)
        payload_compliant = {
            "session_id": "test_tone_session_2",
            "business_type": "Healthcare & Clinic",
            "context": "Annual dental checkup discount",
            "organization_name": "Apex Dental Clinic",
            "link_or_contact": "https://apexdentalclinic.com/clean",
            "user_feedback": "Warm & Community-Driven"
        }
        res_comp = await client.post("/api/campaign/regenerate_tone_previews", json=payload_compliant)
        print("Compliant tone HTTP code:", res_comp.status_code)
        data_comp = res_comp.json()
        print("Compliant tone response status:", data_comp.get("status"))
        assert data_comp.get("status") == "success", "Expected compliant tone to succeed"
        assert len(data_comp.get("tone_candidates", [])) >= 1, "Expected candidate tones"
        print(f"✓ Compliant tone succeeded with {len(data_comp['tone_candidates'])} verified preview candidates!")

        print("\nALL TONE GUARDRAIL TESTS PASSED!")

if __name__ == "__main__":
    asyncio.run(test_tone_guardrails())
