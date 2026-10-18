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

        min_candidates = getattr(self.config.orchestrator, "min_tone_candidates", 3)
        raw_tones = parsed.get("candidate_tones", []) if parsed else []

        cleaned_tones = []
        seen_names = set()
        for t in raw_tones:
            name = (t.get("name") or "").strip()
            desc = (t.get("description") or "").strip()
            ex = t.get("example_message")
            if not ex and t.get("example_messages"):
                ex = t["example_messages"][0]
            if name and name.lower() not in seen_names:
                seen_names.add(name.lower())
                cleaned_tones.append({
                    "id": t.get("id") or f"tone_{len(cleaned_tones)+1}",
                    "name": name,
                    "description": desc or f"Campaign messaging tone: {name}",
                    "example_message": ex or f"Update from {business_name}: {campaign_goal}. {self.config.orchestrator.optout_text}"
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
      "example_message": "Single preview message ending with {self.config.orchestrator.optout_text}"
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
                try:
                    c_clean = re.sub(r"^```json\s*", "", rep_res.get("raw_response", "").strip())
                    c_clean = re.sub(r"\s*```$", "", c_clean)
                    rep_data = json.loads(c_clean)
                    new_tones = rep_data.get("additional_tones") or rep_data.get("candidate_tones") or []
                    for t in new_tones:
                        name = (t.get("name") or "").strip()
                        desc = (t.get("description") or "").strip()
                        ex = t.get("example_message") or (t.get("example_messages")[0] if t.get("example_messages") else "")
                        if name and name.lower() not in seen_names:
                            seen_names.add(name.lower())
                            cleaned_tones.append({
                                "id": t.get("id") or f"tone_{len(cleaned_tones)+1}",
                                "name": name,
                                "description": desc or f"Campaign messaging tone: {name}",
                                "example_message": ex or f"Update from {business_name}: {campaign_goal}. {self.config.orchestrator.optout_text}"
                            })
                            if len(cleaned_tones) >= min_candidates:
                                break
                except Exception as e:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="tone_repair_json_parse",
                        passed=False,
                        details={"error": str(e)}
                    )

        # Fallback complement if still under min_candidates
        if len(cleaned_tones) < min_candidates:
            fallback_options = [
                ("warm_friendly", "✨ Warm & Friendly", "Engaging, conversational tone focused on customer connection."),
                ("clear_direct", "🎯 Clear & Direct", "Concise and action-driven message with clear next steps."),
                ("formal_admin", "🏛️ Formal & Professional", "Professional, structured communication adhering to official policy standards.")
            ]
            for f_id, f_name, f_desc in fallback_options:
                if f_name.lower() not in seen_names:
                    seen_names.add(f_name.lower())
                    cleaned_tones.append({
                        "id": f_id,
                        "name": f_name,
                        "description": f_desc,
                        "example_message": f"Hello {{{{customer_name}}}}, important update from {business_name} regarding {campaign_goal}.\n\n{self.config.orchestrator.optout_text}"
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
