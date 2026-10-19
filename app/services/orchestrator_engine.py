import json
import re
import asyncio
from typing import Dict, Any, List, Optional
from app.core.config import get_config
from app.core.storage import get_storage
from app.core.prompt_manager import get_prompt_manager
from app.core.llm_client import get_llm_client
from app.services.compliance_service import check_template_compliance, count_words
from app.services.safety_service import get_safety_service

def parse_json_robust(raw: str) -> Optional[Dict[str, Any]]:
    """
    Robust JSON parser handling common LLM quirks:
    - Markdown code fences (```json ... ```)
    - Trailing commas before } or ]
    - Unclosed brackets/braces
    - Outermost JSON object extraction
    """
    if not raw or not isinstance(raw, str):
        return None
    clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    clean = re.sub(r"\s*```$", "", clean).strip()
    if not clean:
        return None

    # Attempt 1: Direct JSON parse
    try:
        res = json.loads(clean)
        if isinstance(res, dict):
            return res
    except Exception:
        pass

    # Attempt 2: Strip trailing commas before } or ]
    candidate = re.sub(r",\s*([\}\]])", r"\1", clean)
    try:
        res = json.loads(candidate)
        if isinstance(res, dict):
            return res
    except Exception:
        pass

    # Attempt 3: Close unclosed braces/brackets
    open_curly = candidate.count("{") - candidate.count("}")
    open_square = candidate.count("[") - candidate.count("]")
    candidate_balanced = candidate + ("]" * max(0, open_square)) + ("}" * max(0, open_curly))
    try:
        res = json.loads(candidate_balanced)
        if isinstance(res, dict):
            return res
    except Exception:
        pass

    # Attempt 4: Extract the outermost {...} substring
    first_brace = clean.find("{")
    last_brace = clean.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        snippet = clean[first_brace:last_brace+1]
        try:
            res = json.loads(snippet)
            if isinstance(res, dict):
                return res
        except Exception:
            snippet_commas = re.sub(r",\s*([\}\]])", r"\1", snippet)
            try:
                res = json.loads(snippet_commas)
                if isinstance(res, dict):
                    return res
            except Exception:
                pass

    return None

def normalize_discovery_output(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes discovery output dictionary:
    - Hoists top-level fields (discovery_complete, assumptions_made, next_question_to_user, business_type)
      if the LLM accidentally nested them inside `slots`.
    - Normalizes discovery_complete boolean.
    - Ensures non-empty next_question_to_user when discovery is incomplete.
    """
    if not isinstance(data, dict):
        return {}

    slots = data.get("slots", {})
    if not isinstance(slots, dict):
        slots = {}
        data["slots"] = slots

    # Hoist any top-level keys that the model inadvertently placed inside slots
    hoist_keys = ["discovery_complete", "assumptions_made", "next_question_to_user", "business_type"]
    for k in hoist_keys:
        if k in slots:
            val = slots.pop(k)
            if k not in data or data[k] is None or data[k] == "":
                data[k] = val

    # Normalize discovery_complete
    is_complete = data.get("discovery_complete")
    next_q = (data.get("next_question_to_user") or "").strip()

    if is_complete is None:
        if not next_q and any(k in slots for k in ["offer_or_cta", "business_name", "campaign_name", "campaign_goal"]):
            data["discovery_complete"] = True
        else:
            data["discovery_complete"] = False
    elif is_complete is False and not next_q:
        if any(k in slots for k in ["offer_or_cta", "business_name", "campaign_name", "campaign_goal"]):
            data["discovery_complete"] = True
        else:
            data["next_question_to_user"] = "Could you tell me your business name and the main discount or offer you'd like to share?"

    if data.get("discovery_complete"):
        data["next_question_to_user"] = ""

    return data

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
                data = parse_json_robust(raw)
                if data and ("slots" in data or "business_type" in data or "next_question_to_user" in data):
                    parsed = normalize_discovery_output(data)
                    break
                else:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="discovery_json_parse",
                        passed=False,
                        details={"error": "Malformed or non-dict discovery JSON output", "raw": raw, "attempt": attempt}
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
        else:
            parsed = normalize_discovery_output(parsed)

        # Check if max discovery rounds reached
        if round_index >= self.config.orchestrator.max_discovery_rounds:
            parsed["discovery_complete"] = True
            parsed["next_question_to_user"] = ""
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
                data = parse_json_robust(raw)
                if data and "candidate_tones" in data and len(data["candidate_tones"]) > 0:
                    parsed = data
                    break
                else:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="tone_explorer_json_parse",
                        passed=False,
                        details={"error": "Failed to parse candidate_tones", "raw": raw, "attempt": attempt}
                    )

        min_candidates = getattr(self.config.orchestrator, "min_tone_candidates", 3)
        raw_tones = parsed.get("candidate_tones", []) if parsed else []

        def build_multi_examples(t_obj: Dict[str, Any], tone_name: str) -> List[str]:
            raw_list = t_obj.get("example_messages")
            if isinstance(raw_list, list) and len(raw_list) >= 2:
                return [str(m).strip() for m in raw_list if str(m).strip()]
            
            single = (t_obj.get("example_message") or "").strip()
            if not single:
                single = f"Hello {{{{customer_name}}}}, important update from {business_name} regarding {campaign_goal}. {self.config.orchestrator.optout_text}"
            
            return [
                single,
                f"Follow-up from {business_name}: We wanted to check in regarding {campaign_goal}. Please review details at {{{{link}}}}. {self.config.orchestrator.optout_text}",
                f"Final reminder from {business_name}: Last notice regarding {campaign_goal}. Immediate action required. {self.config.orchestrator.optout_text}"
            ]

        cleaned_tones = []
        seen_names = set()
        for t in raw_tones:
            name = (t.get("name") or "").strip()
            desc = (t.get("description") or "").strip()
            if name and name.lower() not in seen_names:
                seen_names.add(name.lower())
                msgs = build_multi_examples(t, name)
                cleaned_tones.append({
                    "id": t.get("id") or f"tone_{len(cleaned_tones)+1}",
                    "name": name,
                    "description": desc or f"Campaign messaging tone: {name}",
                    "example_message": msgs[0],
                    "example_messages": msgs
                })

        # If fewer than min_candidates, run a lightweight repair prompt (up to 1 retry)
        if len(cleaned_tones) < min_candidates:
            needed = min_candidates - len(cleaned_tones)
            existing_names_str = ", ".join(f"'{t['name']}'" for t in cleaned_tones) if cleaned_tones else "none"
            repair_prompt = f"""You previously proposed {len(cleaned_tones)} tone(s): {existing_names_str}.
We need at least {min_candidates} candidate tones. Please provide {needed} more DISTINCT candidate tone(s) for business '{business_name}' ({business_type}).
Do NOT repeat existing tones.
Respond in strict JSON:
{{
  "additional_tones": [
    {{
      "id": "new_tone_id",
      "name": "Distinct Tone Name with Emoji",
      "description": "Short explanation of the tone",
      "example_message": "Message 1 preview...",
      "example_messages": [
        "Message 1 (Announcement) preview...",
        "Message 2 (Follow-up) preview...",
        "Message 3 (Final Call) preview..."
      ]
    }}
  ]
}}"""
            rep_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="tone_proposal",
                system_prompt="You are a tone suggestion assistant. Output valid JSON.",
                user_message=repair_prompt,
                step_id="tone_repair",
                json_mode=True
            )
            if rep_res.get("status") == "success":
                rep_data = parse_json_robust(rep_res.get("raw_response", ""))
                if rep_data:
                    new_tones = rep_data.get("additional_tones") or rep_data.get("candidate_tones") or []
                    for t in new_tones:
                        name = (t.get("name") or "").strip()
                        desc = (t.get("description") or "").strip()
                        if name and name.lower() not in seen_names:
                            seen_names.add(name.lower())
                            msgs = build_multi_examples(t, name)
                            cleaned_tones.append({
                                "id": t.get("id") or f"tone_{len(cleaned_tones)+1}",
                                "name": name,
                                "description": desc or f"Campaign messaging tone: {name}",
                                "example_message": msgs[0],
                                "example_messages": msgs
                            })
                            if len(cleaned_tones) >= min_candidates:
                                break

        # Fallback complement if still under min_candidates
        if len(cleaned_tones) < min_candidates:
            fallback_options = [
                ("warm_friendly", "✨ Warm & Friendly", "Engaging, conversational tone focused on customer connection.", [
                    f"Hello {{{{customer_name}}}}, we hope this message finds you well! {business_name} wanted to let you know about {campaign_goal}. {self.config.orchestrator.optout_text}",
                    f"Hi {{{{customer_name}}}}, just a gentle follow-up from {business_name} regarding {campaign_goal}. We are here to help if you have questions! {self.config.orchestrator.optout_text}",
                    f"Hello {{{{customer_name}}}}, final friendly reminder from {business_name} regarding {campaign_goal}. Please let us know if we can assist you. {self.config.orchestrator.optout_text}"
                ]),
                ("clear_direct", "🎯 Clear & Direct", "Concise and action-driven message with clear next steps.", [
                    f"Notice from {business_name}: Please be informed regarding {campaign_goal}. Visit {{{{link}}}} to complete your action. {self.config.orchestrator.optout_text}",
                    f"Reminder from {business_name}: This is a follow-up regarding {campaign_goal}. Timely action is required. {self.config.orchestrator.optout_text}",
                    f"Final Notice from {business_name}: Urgent action required regarding {campaign_goal}. Immediate payment/response needed. {self.config.orchestrator.optout_text}"
                ]),
                ("formal_admin", "🏛️ Formal & Professional", "Professional, structured communication adhering to official policy standards.", [
                    f"Dear Parent/Guardian, this is an official notification from {business_name} concerning {campaign_goal}. {self.config.orchestrator.optout_text}",
                    f"Dear Parent/Guardian, this is a formal follow-up from {business_name} regarding {campaign_goal}. {self.config.orchestrator.optout_text}",
                    f"Dear Parent/Guardian, final administrative reminder from {business_name}: {campaign_goal} must be settled immediately to avoid restriction. {self.config.orchestrator.optout_text}"
                ])
            ]
            for f_id, f_name, f_desc, f_msgs in fallback_options:
                if f_name.lower() not in seen_names:
                    seen_names.add(f_name.lower())
                    cleaned_tones.append({
                        "id": f_id,
                        "name": f_name,
                        "description": f_desc,
                        "example_message": f_msgs[0],
                        "example_messages": f_msgs
                    })
                if len(cleaned_tones) >= min_candidates:
                    break

        return {
            "rationale": parsed.get("rationale", f"Adaptive tones selected for {business_type}") if parsed else f"Adaptive tones selected for {business_type}",
            "candidate_tones": cleaned_tones
        }

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
                data = parse_json_robust(raw)
                if data and "steps" in data and len(data["steps"]) > 0:
                    parsed = data
                    break
                else:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="planner_json_parse",
                        passed=False,
                        details={"error": "Failed to parse planning blueprint", "raw": raw, "attempt": attempt}
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
                data = parse_json_robust(raw)
                if data and "templates" in data and len(data["templates"]) > 0:
                    parsed = data
                    break
                else:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="template_writer_json_parse",
                        passed=False,
                        details={"error": "Failed to parse templates JSON", "raw": raw, "attempt": attempt}
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

        # Validate and repair templates in parallel using asyncio.gather
        async def process_single_template(tmpl: Dict[str, Any]) -> Dict[str, Any]:
            body = tmpl.get("body", "")
            comp = check_template_compliance(
                body=body,
                business_name=business_name,
                word_limit=word_limit,
                optout_text=optout_text,
                link_or_contact=link_or_contact
            )

            retries_done = 0
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
                    rep_data = parse_json_robust(rep_res.get("raw_response", ""))
                    if rep_data:
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

                    # Extra columns
                    extra_data = sc.get("extra", {})
                    for col_k, col_v in extra_data.items():
                        tag = "{{" + col_k.lower().strip().replace(" ", "_") + "}}"
                        rendered_preview = rendered_preview.replace(tag, str(col_v))
                        rendered_preview = rendered_preview.replace("{{" + col_k + "}}", str(col_v))

                    previews.append({
                        "contact_name": c_name,
                        "phone": sc.get("phone", ""),
                        "masked_phone": sc.get("masked_phone", ""),
                        "extra": extra_data,
                        "rendered_text": rendered_preview
                    })

            return {
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
            }

        raw_templates = parsed.get("templates", [])
        if raw_templates:
            validated_templates = await asyncio.gather(*(process_single_template(t) for t in raw_templates))
        else:
            validated_templates = []
        all_compliant = all(t["compliant"] for t in validated_templates) if validated_templates else True

        return {
            "sequence_summary": parsed.get("sequence_summary", ""),
            "recommended_schedule": parsed.get("recommended_schedule", "Day 1 -> Day 3 -> Day 7"),
            "all_compliant": all_compliant,
            "templates": list(validated_templates)
        }

_orchestrator_engine: Optional[OrchestratorEngine] = None

def get_orchestrator_engine() -> OrchestratorEngine:
    global _orchestrator_engine
    if _orchestrator_engine is None:
        _orchestrator_engine = OrchestratorEngine()
    return _orchestrator_engine
