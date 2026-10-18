import pytest

pytestmark = pytest.mark.slow
from app.core.config import get_config
from app.services.orchestrator_engine import get_orchestrator_engine

@pytest.mark.asyncio
async def test_min_tone_candidates_guaranteed():
    config = get_config(reload=True)
    assert config.orchestrator.min_tone_candidates == 3

    engine = get_orchestrator_engine()
    slots = {
        "business_name": {"value": "Hope Wildlife Sanctuary"},
        "campaign_name": {"value": "Winter Shelter Appeal"},
        "campaign_goal": {"value": "Build heated shelters for rescued animals"},
        "target_audience": {"value": "Animal lovers and donors"}
    }

    tones_res = await engine.explore_tones(
        session_id="test_tone_min_session",
        workspace_id="test_ws",
        business_type="Wildlife Rescue NGO",
        slots=slots
    )

    candidate_tones = tones_res.get("candidate_tones", [])
    assert len(candidate_tones) >= config.orchestrator.min_tone_candidates
    # Check that each tone has distinct name, description, and example_message
    names = [t["name"] for t in candidate_tones]
    assert len(set(names)) == len(names), "Tone names must be distinct"
    for t in candidate_tones:
        assert "description" in t and len(t["description"]) > 0
        assert "example_message" in t and len(t["example_message"]) > 0
