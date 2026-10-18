import json
import asyncio
from unittest.mock import patch, AsyncMock
from app.api.chat_routes import get_or_create_state, select_tone, ToneSelectRequest
from app.services.compliance_service import check_template_compliance

async def main():
    session_id = "sess_3msg_capture"
    state = get_or_create_state(session_id)
    state["business_type"] = "Wild Animal Sanctuary"
    state["slots"] = {
        "business_name": {"value": "SaveThePaws"},
        "campaign_name": {"value": "Winter Shelter Drive"},
        "campaign_goal": {"value": "Heating shelters for rescued wolves"},
        "messages_per_contact": {"value": 3}
    }
    
    # 3-template real sample sequence
    mock_templates = [
        {
            "message_index": 1,
            "stage": "Launch",
            "body": "Hello {{name}}, winter freezing temps have arrived at SaveThePaws! Help us heat the wolf sanctuary shelters today at https://savethepaws.org. Reply STOP to unsubscribe.",
            "word_count": 25,
            "word_limit": 320,
            "compliant": True,
            "tone": "Urgent & Direct",
            "violations": [],
            "previews": []
        },
        {
            "message_index": 2,
            "stage": "Reminder",
            "body": "Hi {{name}}, temperatures drop to -5° tonight. We are halfway to our heating goal for the rescued animals at SaveThePaws. Every $10 keeps a pen warm: https://savethepaws.org. Reply STOP to unsubscribe.",
            "word_count": 31,
            "word_limit": 320,
            "compliant": True,
            "tone": "Urgent & Direct",
            "violations": [],
            "previews": []
        },
        {
            "message_index": 3,
            "stage": "Urgency",
            "body": "Final call {{name}}: sanctuary winter heating installation starts tomorrow morning. Protect our wolves from extreme frostbite at SaveThePaws. Donate: https://savethepaws.org. Reply STOP to unsubscribe.",
            "word_count": 27,
            "word_limit": 320,
            "compliant": True,
            "tone": "Urgent & Direct",
            "violations": [],
            "previews": []
        }
    ]
    
    mock_plan = {
        "plan_summary": "3-touchpoint sequence for Wild Animal Sanctuary",
        "steps": [
            {"step_id": "structure_sequence", "title": "Sequence Structuring", "icon": "📐", "description": "3 touchpoints", "sub_steps": ["Plan mapped"]},
            {"step_id": "draft_templates", "title": "Template Generation", "icon": "✍️", "description": "Drafting 3 templates", "sub_steps": ["Touchpoint 1 drafted", "Touchpoint 2 drafted", "Touchpoint 3 drafted"]}
        ]
    }
    
    req = ToneSelectRequest(session_id=session_id, tone_id="tone_urgent", tone_name="Urgent & Direct")
    
    with patch("app.api.chat_routes.engine.generate_plan", new=AsyncMock(return_value=mock_plan)), \
         patch("app.api.chat_routes.engine.generate_and_validate_templates", new=AsyncMock(return_value={"templates": mock_templates, "recommended_schedule": "Day 1 → Day 4 → Day 7"})):
        
        response = await select_tone(req)
        
        events = []
        templates_ready_payload = None
        
        async for chunk in response.body_iterator:
            for line in chunk.split("\n\n"):
                line = line.strip()
                if line.startswith("data: "):
                    data_str = line[6:]
                    try:
                        ev = json.loads(data_str)
                        events.append(ev)
                        if ev.get("type") == "templates_ready":
                            templates_ready_payload = ev
                    except Exception:
                        pass
                        
    output_result = {
        "total_sse_events": len(events),
        "event_types": [e.get("type") for e in events],
        "templates_ready_event": templates_ready_payload,
        "backend_returned_count": len(templates_ready_payload.get("templates", [])),
        "templates_summary": [
            {
                "message_index": t["message_index"],
                "stage": t["stage"],
                "word_count": t["word_count"],
                "word_limit": t["word_limit"],
                "tone": t["tone"],
                "body": t["body"]
            }
            for t in templates_ready_payload.get("templates", [])
        ]
    }
    
    with open(".apitool/evidence/sse_templates_payload.json", "w") as f:
        json.dump(output_result, f, indent=2)
    
    print(f"Captured {len(events)} SSE events.")
    print(f"Backend templates count: {output_result['backend_returned_count']}")

if __name__ == "__main__":
    asyncio.run(main())
