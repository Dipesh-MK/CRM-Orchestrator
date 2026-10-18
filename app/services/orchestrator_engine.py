import json
import re
from typing import Dict, Any, List, Optional
from app.core.config import get_config
from app.core.storage import get_storage
from app.core.prompt_manager import get_prompt_manager
from app.core.llm_client import get_llm_client
from app.services.compliance_service import check_template_compliance, count_words
from app.services.safety_service import get_safety_service

class OrchestratorEngine:
    def __init__(self):
        self.config = get_config()
        self.storage = get_storage()
        self.prompts = get_prompt_manager()
        self.llm = get_llm_client()
        self.safety = get_safety_service()

    async def run_discovery_round(
        self,
        session_id: str,
        workspace_id: str,
        latest_user_message: str,
        conversation_history: List[Dict[str, str]],
        current_slots: Dict[str, Any],
        round_index: int
    ) -> Dict[str, Any]:
        """
        Executes one adaptive discovery step.
        Determines business type, extracts slots, evaluates confidence,
        generates the next 1-2 questions, or declares discovery complete.
        """
        # 1. First safety check on user input
        safety_eval = await self.safety.evaluate_safety(session_id, latest_user_message, "discovery_input")
        if not safety_eval.get("allowed", False):
            return {
                "blocked": True,
                "reason": safety_eval.get("reason"),
                "alternative": safety_eval.get("suggested_alternative"),
                "is_rate_limit": safety_eval.get("is_rate_limit", False)
            }

        # 2. Check memory
        from app.services.memory_service import get_memory_service
        memory_context = get_memory_service().get_memory_context(workspace_id)

        # 3. Format history
        history_text = "\n".join([f"{msg['role'].capitalize()}: {msg['content']}" for msg in conversation_history])

        # 4. Render discovery prompt
        prompt = self.prompts.render("discovery", {
            "memory_context": memory_context,
            "slots_json": json.dumps(current_slots, indent=2),
            "conversation_history": history_text,
            "latest_user_message": latest_user_message
        })

        system_msg = self.prompts.load_prompt("system")
        max_retries = self.config.orchestrator.max_retries
        parsed = None

        for attempt in range(max_retries + 1):
            llm_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="discovery",
                system_prompt=f"{system_msg}\n\n{prompt}",
                user_message=latest_user_message,
                step_id="discovery",
                json_mode=True
            )

            if llm_res.get("status") == "success":
                raw = llm_res.get("raw_response", "")
                try:
                    clean = re.sub(r"^```json\s*", "", raw.strip())
                    clean = re.sub(r"\s*```$", "", clean)
                    data = json.loads(clean)
                    if "slots" in data:
                        parsed = data
                        break
                except Exception as e:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="discovery_json_parse",
                        passed=False,
                        details={"error": str(e), "raw": raw, "attempt": attempt}
                    )

        if not parsed:
            # Fallback basic structure if model repeatedly failed schema
            parsed = {
                "business_type": "General Business",
                "slots": current_slots,
                "discovery_complete": round_index >= self.config.orchestrator.max_discovery_rounds,
                "assumptions_made": ["Assumed default CRM campaign configuration due to retry limits."],
                "next_question_to_user": "Could you confirm your business name and main campaign goal?"
            }

        # Check if max discovery rounds reached
        if round_index >= self.config.orchestrator.max_discovery_rounds:
            parsed["discovery_complete"] = True
            if "assumptions_made" not in parsed or not parsed["assumptions_made"]:
                parsed["assumptions_made"] = ["Standard campaign parameters assumed based on business type."]

        # Merge slots
        updated_slots = dict(current_slots)
        for k, v in parsed.get("slots", {}).items():
            updated_slots[k] = v

        parsed["slots"] = updated_slots
        return parsed

    async def explore_tones(
        self,
        session_id: str,
        workspace_id: str,
        business_type: str,
        slots: Dict[str, Any],
        user_tone_idea: str = ""
    ) -> Dict[str, Any]:
        """Proposes dynamic candidate tones with short real examples."""
        from app.services.memory_service import get_memory_service
        memory_context = get_memory_service().get_memory_context(workspace_id)

        business_name = slots.get("business_name", {}).get("value") or "Our Company"
        campaign_name = slots.get("campaign_name", {}).get("value") or "New Campaign"
        campaign_goal = slots.get("campaign_goal", {}).get("value") or "Special Offer"
        target_audience = slots.get("target_audience", {}).get("value") or "Valued Customers"

        prompt = self.prompts.render("tone_explorer", {
            "business_type": business_type,
            "business_name": business_name,
            "campaign_name": campaign_name,
            "campaign_goal": campaign_goal,
            "target_audience": target_audience,
            "user_tone_idea": user_tone_idea or "None specified",
            "memory_context": memory_context,
            "word_limit": str(self.config.orchestrator.word_limit),
            "optout_text": self.config.orchestrator.optout_text
        })

        system_msg = self.prompts.load_prompt("system")
        max_retries = self.config.orchestrator.max_retries
        parsed = None

        for attempt in range(max_retries + 1):
            llm_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="tone_proposal",
                system_prompt=f"{system_msg}\n\n{prompt}",
                user_message="Propose suitable candidate tones with preview examples for this campaign.",
                step_id="tone_explorer",
                json_mode=True
            )

            if llm_res.get("status") == "success":
                raw = llm_res.get("raw_response", "")
                try:
                    clean = re.sub(r"^```json\s*", "", raw.strip())
                    clean = re.sub(r"\s*```$", "", clean)
                    data = json.loads(clean)
                    if "candidate_tones" in data and len(data["candidate_tones"]) > 0:
                        parsed = data
                        break
                except Exception as e:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="tone_explorer_json_parse",
                        passed=False,
                        details={"error": str(e), "raw": raw, "attempt": attempt}
                    )

        if not parsed:
            # Fallback dynamic tone proposal with multi-message support
            display_name = business_name if business_name and business_name != "Our Company" else "{{organization_name}}"
            parsed = {
                "rationale": f"Adaptive tones selected for {business_type}",
                "candidate_tones": [
                    {
                        "id": "friendly_warm",
                        "name": "✨ Warm & Friendly",
                        "description": "Engaging, conversational tone focused on customer connection.",
                        "example_messages": [
                            f"Hi {{{{customer_name}}}}, {display_name} has an important update regarding {campaign_goal}. Tap {{{{link}}}} for details.\n\n{self.config.orchestrator.optout_text}",
                            f"Hello {{{{customer_name}}}}, quick follow-up from {display_name} about {campaign_goal}. Let us know if you need any help.\n\n{self.config.orchestrator.optout_text}",
                            f"Hi {{{{customer_name}}}}, final reminder from {display_name} to review {campaign_goal}. Visit {{{{link}}}}.\n\n{self.config.orchestrator.optout_text}"
                        ],
                        "example_message": f"Hi {{{{customer_name}}}}, {display_name} has an exciting update for you: {campaign_goal}. Visit {{{{link}}}} to learn more!\n\n{self.config.orchestrator.optout_text}"
                    },
                    {
                        "id": "clear_direct",
                        "name": "🎯 Clear & Direct",
                        "description": "Concise and action-driven message with clear next steps.",
                        "example_messages": [
                            f"Hello {{{{customer_name}}}}, notice from {display_name}: {campaign_goal}. Complete this at {{{{link}}}}.\n\n{self.config.orchestrator.optout_text}",
                            f"Reminder {{{{customer_name}}}}, your action with {display_name} regarding {campaign_goal} is still pending.\n\n{self.config.orchestrator.optout_text}",
                            f"Final notice: Please settle {campaign_goal} with {display_name} via {{{{link}}}}.\n\n{self.config.orchestrator.optout_text}"
                        ],
                        "example_message": f"Hello {{{{customer_name}}}}, important reminder from {display_name}: {campaign_goal}. Tap {{{{link}}}} now.\n\n{self.config.orchestrator.optout_text}"
                    },
                    {
                        "id": "formal_admin",
                        "name": "🏛️ Formal & Administrative",
                        "description": "Professional, structured communication adhering to official policy.",
                        "example_messages": [
                            f"Dear {{{{customer_name}}}}, official notification from {display_name} regarding {campaign_goal}. Please review: {{{{link}}}}.\n\n{self.config.orchestrator.optout_text}",
                            f"Dear {{{{customer_name}}}}, this is a follow-up concerning {campaign_goal} on your {display_name} record.\n\n{self.config.orchestrator.optout_text}",
                            f"Dear {{{{customer_name}}}}, final administrative notice regarding {campaign_goal} with {display_name}.\n\n{self.config.orchestrator.optout_text}"
                        ],
                        "example_message": f"Dear {{{{customer_name}}}}, official notice from {display_name} regarding {campaign_goal}. Review at {{{{link}}}}.\n\n{self.config.orchestrator.optout_text}"
                    }
                ]
            }

        return parsed

    async def generate_plan(
        self,
        session_id: str,
        business_type: str,
        slots: Dict[str, Any],
        selected_tone: str,
        contacts_count: int = 0
    ) -> Dict[str, Any]:
        """Generates the dynamic orchestrator pipeline steps based on the business use case."""
        business_name = slots.get("business_name", {}).get("value") or "Our Company"
        campaign_name = slots.get("campaign_name", {}).get("value") or "Campaign"
        target_audience = slots.get("target_audience", {}).get("value") or "Audience"
        messages_count = slots.get("messages_per_contact", {}).get("value") or 3

        prompt = self.prompts.render("planner", {
            "business_type": business_type,
            "business_name": business_name,
            "campaign_name": campaign_name,
            "target_audience": target_audience,
            "messages_count": str(messages_count),
            "selected_tone": selected_tone,
            "has_contacts": "true" if contacts_count > 0 else "false",
            "contact_count": str(contacts_count)
        })

        system_msg = self.prompts.load_prompt("system")
        max_retries = self.config.orchestrator.max_retries
        parsed = None

        for attempt in range(max_retries + 1):
            llm_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="planner",
                system_prompt=f"{system_msg}\n\n{prompt}",
                user_message="Generate the custom orchestrator pipeline steps for this campaign.",
                step_id="planner",
                json_mode=True
            )

            if llm_res.get("status") == "success":
                raw = llm_res.get("raw_response", "")
                try:
                    clean = re.sub(r"^```json\s*", "", raw.strip())
                    clean = re.sub(r"\s*```$", "", clean)
                    data = json.loads(clean)
                    if "steps" in data and len(data["steps"]) > 0:
                        parsed = data
                        break
                except Exception as e:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="planner_json_parse",
                        passed=False,
                        details={"error": str(e), "raw": raw, "attempt": attempt}
                    )

        if not parsed:
            # Fallback domain-tailored pipeline
            parsed = {
                "plan_summary": f"Orchestrator pipeline configured for {business_type}",
                "steps": [
                    {
                        "step_id": "structure_sequence",
                        "title": "Sequence Structuring",
                        "icon": "📐",
                        "description": f"Planning {messages_count}-touchpoint cadence for {business_type}",
                        "sub_steps": [
                            "Mapping touchpoint milestones",
                            "Configuring recipient time intervals",
                            "Defining personalization placeholders"
                        ]
                    },
                    {
                        "step_id": "draft_templates",
                        "title": "Template Generation",
                        "icon": "✍️",
                        "description": f"Drafting templates in tone '{selected_tone}'",
                        "sub_steps": [
                            f"Drafting touchpoint 1 (Announcement)",
                            f"Drafting touchpoint 2 (Follow-up)",
                            f"Drafting touchpoint 3 (Final Call)",
                            "Verifying business identity & opt-out"
                        ]
                    },
                    {
                        "step_id": "compliance_audit",
                        "title": "Compliance & Safety Audit",
                        "icon": "🛡️",
                        "description": f"Strict {self.config.orchestrator.word_limit}-word compliance and safety verification",
                        "sub_steps": [
                            f"Word count ceiling enforcement (≤ {self.config.orchestrator.word_limit} words)",
                            "Mandatory opt-out statement verification",
                            "Safety policy classification"
                        ]
                    },
                    {
                        "step_id": "campaign_ready",
                        "title": "Campaign Package Ready",
                        "icon": "🚀",
                        "description": "Ready for approval and export",
                        "sub_steps": [
                            "Packaging approved message templates",
                            "Generating sample recipient preview",
                            "Awaiting final user sign-off"
                        ]
                    }
                ]
            }

        return parsed

    async def generate_and_validate_templates(
        self,
        session_id: str,
        business_type: str,
        slots: Dict[str, Any],
        selected_tone: str,
        contacts_sample: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Generates sequence templates and validates against word count & compliance rules with auto-repair."""
        business_name = slots.get("business_name", {}).get("value") or "Our Business"
        campaign_name = slots.get("campaign_name", {}).get("value") or "Campaign"
        campaign_goal = slots.get("campaign_goal", {}).get("value") or "Special Offer"
        target_audience = slots.get("target_audience", {}).get("value") or "Customers"
        link_or_contact = slots.get("link_or_contact", {}).get("value") or ""
        messages_count = slots.get("messages_per_contact", {}).get("value") or 3
        gap_between_messages = slots.get("gap_between_messages", {}).get("value") or "3 days"
        language = slots.get("language", {}).get("value") or "English"
        must_include = slots.get("must_include", {}).get("value") or ""
        must_avoid = slots.get("must_avoid", {}).get("value") or ""

        word_limit = self.config.orchestrator.word_limit
        optout_text = self.config.orchestrator.optout_text

        prompt = self.prompts.render("template_writer", {
            "business_type": business_type,
            "business_name": business_name,
            "campaign_name": campaign_name,
            "campaign_goal": campaign_goal,
            "target_audience": target_audience,
            "link_or_contact": link_or_contact,
            "messages_count": str(messages_count),
            "gap_between_messages": str(gap_between_messages),
            "selected_tone": selected_tone,
            "must_include": must_include,
            "must_avoid": must_avoid,
            "language": language,
            "word_limit": str(word_limit),
            "optout_text": optout_text
        })

        system_msg = self.prompts.load_prompt("system")
        max_retries = self.config.orchestrator.max_retries
        parsed = None

        for attempt in range(max_retries + 1):
            llm_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="template_write",
                system_prompt=f"{system_msg}\n\n{prompt}",
                user_message="Generate the compliant message templates for this campaign.",
                step_id="template_writer",
                json_mode=True
            )

            if llm_res.get("status") == "success":
                raw = llm_res.get("raw_response", "")
                try:
                    clean = re.sub(r"^```json\s*", "", raw.strip())
                    clean = re.sub(r"\s*```$", "", clean)
                    data = json.loads(clean)
                    if "templates" in data and len(data["templates"]) > 0:
                        parsed = data
                        break
                except Exception as e:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="template_writer_json_parse",
                        passed=False,
                        details={"error": str(e), "raw": raw, "attempt": attempt}
                    )

        if not parsed:
            # Fallback structured templates
            parsed = {
                "sequence_summary": f"Standard sequence for {business_name}",
                "recommended_schedule": "Day 1 -> Day 3 -> Day 7",
                "templates": [
                    {
                        "message_index": 1,
                        "stage": "Announcement",
                        "tone": selected_tone,
                        "body": f"Hello {{{{customer_name}}}}, {business_name} is excited to announce {campaign_name}! {campaign_goal}. Visit {{{{link}}}} for details.\n\n{optout_text}",
                        "tags_used": ["customer_name", "business_name", "link"],
                        "estimated_word_count": 28
                    }
                ]
            }

        # Validate and repair each template
        validated_templates = []
        all_compliant = True

        for tmpl in parsed.get("templates", []):
            body = tmpl.get("body", "")
            comp = check_template_compliance(
                body=body,
                business_name=business_name,
                word_limit=word_limit,
                optout_text=optout_text,
                link_or_contact=link_or_contact
            )

            retries_done = 0
            # If not compliant, run repair loop up to max_retries
            while not comp["compliant"] and retries_done < max_retries:
                retries_done += 1
                repair_prompt = self.prompts.render("validator_repair", {
                    "violations_list": "\n".join(f"- {v}" for v in comp["violations"]),
                    "word_limit": str(word_limit),
                    "optout_text": optout_text,
                    "business_name": business_name,
                    "original_content": body
                })

                rep_res = await self.llm.call_llm(
                    session_id=session_id,
                    purpose="repair",
                    system_prompt=f"{system_msg}\n\n{repair_prompt}",
                    user_message="Repair this template to strictly satisfy all compliance rules.",
                    step_id="validator_repair",
                    json_mode=True
                )

                if rep_res.get("status") == "success":
                    try:
                        c_raw = re.sub(r"^```json\s*", "", rep_res.get("raw_response", "").strip())
                        c_raw = re.sub(r"\s*```$", "", c_raw)
                        rep_data = json.loads(c_raw)
                        repaired_body = rep_data.get("repaired_body")
                        if repaired_body:
                            body = repaired_body
                            comp = check_template_compliance(
                                body=body,
                                business_name=business_name,
                                word_limit=word_limit,
                                optout_text=optout_text,
                                link_or_contact=link_or_contact
                            )
                    except Exception:
                        pass

            # Final safety check on the template body
            safety_res = await self.safety.evaluate_safety(session_id, body, f"template_{tmpl.get('message_index')}")
            is_safe = safety_res.get("allowed", True)

            # Record validation log
            self.storage.log_validation(
                session_id=session_id,
                validation_type=f"template_compliance_{tmpl.get('message_index')}",
                passed=comp["compliant"] and is_safe,
                details={
                    "message_index": tmpl.get("message_index"),
                    "word_count": comp["word_count"],
                    "word_limit": word_limit,
                    "violations": comp["violations"],
                    "safety": safety_res,
                    "retries": retries_done
                }
            )

            if not comp["compliant"] or not is_safe:
                all_compliant = False

            # Build preview with actual uploaded/pasted contact data
            previews = []
            has_real_contacts = contacts_sample is not None and len(contacts_sample) > 0
            if has_real_contacts:
                for sc in contacts_sample[:5]:
                    rendered_preview = body
                    c_name = sc.get("name", "Customer")
                    c_phone = sc.get("phone", sc.get("masked_phone", ""))
                    
                    # Standard tags
                    rendered_preview = rendered_preview.replace("{{customer_name}}", c_name)
                    rendered_preview = rendered_preview.replace("{{name}}", c_name)
                    rendered_preview = rendered_preview.replace("{{donor_name}}", c_name)
                    rendered_preview = rendered_preview.replace("{{client_name}}", c_name)
                    rendered_preview = rendered_preview.replace("{{business_name}}", business_name)
                    rendered_preview = rendered_preview.replace("{{org_name}}", business_name)
                    rendered_preview = rendered_preview.replace("{{campaign_name}}", campaign_name)
                    rendered_preview = rendered_preview.replace("{{link}}", link_or_contact or "https://example.com")
                    rendered_preview = rendered_preview.replace("{{donation_link}}", link_or_contact or "https://example.com")
                    rendered_preview = rendered_preview.replace("{{phone}}", c_phone)

                    # Dynamically replace any extra columns from the input row (e.g. {{city}}, {{donation_amount}}, {{appointment_time}})
                    extra_data = sc.get("extra", {})
                    for col_k, col_v in extra_data.items():
                        tag = "{{" + col_k.lower().strip().replace(" ", "_") + "}}"
                        rendered_preview = rendered_preview.replace(tag, str(col_v))
                        # Also try raw column name
                        rendered_preview = rendered_preview.replace("{{" + col_k + "}}", str(col_v))

                    previews.append({
                        "contact_name": c_name,
                        "phone": sc.get("phone", ""),
                        "masked_phone": sc.get("masked_phone", ""),
                        "extra": extra_data,
                        "rendered_text": rendered_preview
                    })

            validated_templates.append({
                "message_index": tmpl.get("message_index", 1),
                "stage": tmpl.get("stage", "Message"),
                "tone": selected_tone,
                "body": body,
                "word_count": comp["word_count"],
                "word_limit": word_limit,
                "compliant": comp["compliant"] and is_safe,
                "violations": comp["violations"],
                "safety_status": "safe" if is_safe else "blocked",
                "retries": retries_done,
                "previews": previews
            })

        return {
            "sequence_summary": parsed.get("sequence_summary", ""),
            "recommended_schedule": parsed.get("recommended_schedule", "Day 1 -> Day 3 -> Day 7"),
            "all_compliant": all_compliant,
            "templates": validated_templates
        }

_orchestrator_engine: Optional[OrchestratorEngine] = None

def get_orchestrator_engine() -> OrchestratorEngine:
    global _orchestrator_engine
    if _orchestrator_engine is None:
        _orchestrator_engine = OrchestratorEngine()
    return _orchestrator_engine
