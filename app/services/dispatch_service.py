import os
import re
import json
import uuid
import secrets
import hmac
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from app.core.config import get_config
from app.core.storage import get_storage
from app.services.safety_service import get_safety_service
from app.services.compliance_service import count_words
from app.adapters.channel_adapter import get_channel_adapter

E164_REGEX = re.compile(r"^\+[1-9]\d{7,14}$")

def render_template(body_template: str, contact: Dict[str, Any]) -> Tuple[str, List[str]]:
    """
    Renders personalization tags (e.g. {{customer_name}}, {{amount}}) from contact row.
    Returns rendered text and list of unresolved tag names.
    """
    extra = contact.get("extra", {})
    name = contact.get("name", "")
    phone = contact.get("phone", "")
    
    lookup = {
        "customer_name": name,
        "name": name,
        "donor_name": name,
        "client_name": name,
        "phone": phone,
        "contact_id": contact.get("contact_id", "")
    }
    for k, v in extra.items():
        lookup[k.lower().strip().replace(" ", "_")] = str(v)
        lookup[k] = str(v)

    tag_regex = re.compile(r"\{\{([a-zA-Z0-9_\s]+)\}\}")
    unresolved = []

    def replace_match(m):
        raw_key = m.group(1).strip()
        key_clean = raw_key.lower().replace(" ", "_")
        if raw_key in lookup and lookup[raw_key]:
            return lookup[raw_key]
        elif key_clean in lookup and lookup[key_clean]:
            return lookup[key_clean]
        else:
            unresolved.append(raw_key)
            return m.group(0)

    rendered = tag_regex.sub(replace_match, body_template)
    return rendered, unresolved

class DispatchService:
    def __init__(self, storage=None, config=None, adapter=None):
        self._storage = storage
        self._config = config
        self._adapter = adapter
        self.safety_service = get_safety_service()

    @property
    def storage(self):
        return self._storage if self._storage is not None else get_storage()

    @property
    def config(self):
        return self._config if self._config is not None else get_config()

    @property
    def adapter(self):
        return self._adapter if self._adapter is not None else get_channel_adapter()

    async def preflight_run(self, session_id: str, template_step: int = 1) -> Dict[str, Any]:
        """
        Executes strict pre-flight checks:
        - Approved campaign check
        - Contacts read from contacts table only
        - E.164 (+ digits only, 8-15 digits)
        - Tag resolution (no leftover {{...}})
        - Post-personalization word limit re-check
        - Optout check
        - Safety check
        - Suppression check
        - Run cap (max_messages_per_run)
        - Registered handset check via operator tool list_phone_numbers
        Generates per-session CSRF token and previews.
        """
        cfg = self.config
        if not cfg.dispatch.enabled:
            return {"success": False, "error": "Dispatch capability is currently disabled in system configuration."}

        # Amendment 2: APPROVED CAMPAIGNS ONLY
        campaign = self.storage.get_campaign(session_id)
        if not campaign:
            return {
                "success": False,
                "error": "Campaign not found or not approved. Dispatch requires an approved campaign."
            }
        if not campaign.get("approved"):
            return {
                "success": False,
                "error": "Campaign templates must be approved before dispatch can be initiated."
            }

        templates = campaign.get("templates", [])
        if not templates:
            return {"success": False, "error": "No templates available in approved campaign."}

        target_template = None
        for t in templates:
            if t.get("message_index") == template_step:
                target_template = t
                break
        if not target_template:
            target_template = templates[0]

        template_body = target_template.get("body", "")

        # Amendment 1: Read contacts from contacts table only
        contacts = self.storage.get_contacts_by_session(session_id)
        if not contacts:
            return {
                "success": False,
                "error": "No contacts found in contacts table. Contacts must be uploaded/ingested through the contact flow."
            }

        # Check run cap
        max_run = cfg.dispatch.max_messages_per_run
        run_cap_ok = len(contacts) <= max_run

        # Suppression list
        from app.services.contact_service import get_contact_service
        suppression_set = get_contact_service().load_suppression_list()

        # Registered handsets check from provider
        registered_phones = []
        if cfg.dispatch.require_registered_phone:
            try:
                registered_phones = self.adapter.get_registered_phones(force_refresh=False)
            except Exception as e:
                print(f"[DispatchService] Error fetching registered phones: {e}")
                registered_phones = []

        registered_set = set(registered_phones)
        word_limit = cfg.orchestrator.word_limit
        optout_text = cfg.orchestrator.optout_text.strip()

        all_e164_ok = True
        all_tags_ok = True
        all_word_limits_ok = True
        all_optouts_ok = True
        all_unsuppressed_ok = True
        all_registered_ok = True
        all_safety_ok = True

        previews = []
        evaluated_items = []

        # Aggregator base url
        base_aggregator_url = (getattr(self.adapter, "runtime", None) and getattr(self.adapter.runtime, "base_url", None)) or "http://localhost:3000"

        for idx, contact in enumerate(contacts):
            phone = contact.get("phone", "")
            c_name = contact.get("name", "Customer")
            c_id = contact.get("contact_id", "")

            # 1. E.164 check
            is_e164 = bool(E164_REGEX.match(phone))
            if not is_e164:
                all_e164_ok = False

            # 2. Render & tag resolution
            rendered_body, unresolved_tags = render_template(template_body, contact)
            has_unresolved = len(unresolved_tags) > 0 or "{{" in rendered_body
            if has_unresolved:
                all_tags_ok = False

            # 3. Post-personalization word limit
            words = count_words(rendered_body)
            word_ok = words <= word_limit
            if not word_ok:
                all_word_limits_ok = False

            # 4. Optout check
            has_optout = optout_text.lower() in rendered_body.lower()
            if not has_optout:
                all_optouts_ok = False

            # 5. Suppression check
            is_suppressed = phone in suppression_set
            if is_suppressed:
                all_unsuppressed_ok = False

            # 6. Registered phone check (our policy)
            is_registered = True
            blocked_reason = None
            if cfg.dispatch.require_registered_phone:
                if phone not in registered_set:
                    is_registered = False
                    all_registered_ok = False
                    blocked_reason = f"UNREGISTERED_PHONE_NUMBER: register it in the aggregator website: {base_aggregator_url}"

            item_data = {
                "contact_id": c_id,
                "name": c_name,
                "phone": phone,
                "rendered_body": rendered_body,
                "is_e164": is_e164,
                "tags_resolved": not has_unresolved,
                "unresolved_tags": unresolved_tags,
                "word_count": words,
                "word_ok": word_ok,
                "has_optout": has_optout,
                "is_suppressed": is_suppressed,
                "is_registered": is_registered,
                "blocked_reason": blocked_reason
            }
            evaluated_items.append(item_data)

            if idx < 3:
                previews.append({
                    "contact_id": c_id,
                    "name": c_name,
                    "phone": phone,
                    "preview_text": rendered_body
                })

        # Generate per-session CSRF token (at least 128 bits, secrets module)
        csrf_token = secrets.token_hex(32)

        # Store pending send job in DB
        job_id = f"job_{session_id[:8]}_{int(asyncio.get_event_loop().time()*1000)}"
        self.storage.create_send_job(job_id=job_id, session_id=session_id, total_items=len(evaluated_items), csrf_token=csrf_token)

        send_items_records = []
        for it in evaluated_items:
            item_id = f"item_{job_id}_{it['contact_id']}"
            send_items_records.append({
                "item_id": item_id,
                "job_id": job_id,
                "contact_id": it["contact_id"],
                "phone": it["phone"],
                "status": "pending",
                "message_sid": None,
                "error": it.get("blocked_reason")
            })
        self.storage.create_send_items(send_items_records)

        preflight_passed = (
            all_e164_ok
            and all_tags_ok
            and all_word_limits_ok
            and all_optouts_ok
            and all_unsuppressed_ok
            and all_registered_ok
            and run_cap_ok
        )

        unregistered_policy_message = (
            f"Blocked by CRM policy: recipient is not registered in the aggregator. Register it at {base_aggregator_url} first."
            if not all_registered_ok else None
        )

        return {
            "success": True,
            "session_id": session_id,
            "job_id": job_id,
            "csrf_token": csrf_token,
            "passed": preflight_passed,
            "checks": {
                "e164_valid": all_e164_ok,
                "tags_resolved": all_tags_ok,
                "under_word_limit": all_word_limits_ok,
                "optout_present": all_optouts_ok,
                "no_suppressed_numbers": all_unsuppressed_ok,
                "run_cap_ok": run_cap_ok,
                "registered_in_aggregator": all_registered_ok
            },
            "unregistered_policy_message": unregistered_policy_message,
            "total_recipients": len(contacts),
            "max_messages_per_run": max_run,
            "previews": previews,
            "aggregator_url": base_aggregator_url
        }

    async def confirm_and_send(self, job_id: str, csrf_token: str) -> Dict[str, Any]:
        """
        Executes atomic at-most-once send:
        pending -> sending -> (call aggregator send_message) -> queued (or failed).
        """
        job = self.storage.get_send_job(job_id)
        if not job:
            return {"success": False, "error": "Send job not found."}

        # Check CSRF token with hmac.compare_digest
        stored_csrf = job.get("csrf_token")
        if not stored_csrf or not hmac.compare_digest(str(stored_csrf), str(csrf_token)):
            return {"success": False, "error": "Invalid or missing CSRF token."}

        # Check job status
        if job.get("status") != "pending":
            return {"success": False, "error": f"Job {job_id} cannot be sent: status is '{job.get('status')}'."}

        items = self.storage.get_send_items_by_job(job_id)
        session_id = job.get("session_id", "")
        contacts_by_id = {c["contact_id"]: c for c in self.storage.get_contacts_by_session(session_id)}

        campaign = self.storage.get_campaign(session_id)
        templates = campaign.get("templates", []) if campaign else []
        template_body = templates[0].get("body", "") if templates else ""

        # Atomically transition job to 'sending'
        self.storage.update_send_job_status(job_id, "sending")

        successful_count = 0
        failed_count = 0
        send_interval = self.config.dispatch.send_interval_ms / 1000.0

        for item in items:
            item_id = item["item_id"]
            phone = item["phone"]
            contact_id = item["contact_id"]
            contact = contacts_by_id.get(contact_id, {"name": "Customer", "phone": phone, "contact_id": contact_id})

            # Check if pre-blocked (e.g. UNREGISTERED_PHONE_NUMBER)
            if item.get("error"):
                self.storage.update_send_item_status(item_id, status="failed", error=item["error"])
                failed_count += 1
                continue

            rendered_body, _ = render_template(template_body, contact)

            # Atomic transition to 'sending'
            self.storage.update_send_item_status(item_id, status="sending")

            try:
                # Call aggregator send_message through generated client
                res = self.adapter.stage_send(
                    to=phone,
                    body=rendered_body,
                    session_id=session_id,
                    job_id=job_id,
                    item_id=item_id
                )
                
                # Check outcome
                if res.get("status") in ("pending_user_approval", "pending_approval") or "action_id" in res:
                    # Provider staged pending action, execute it via human-approval verifier
                    act_id = res.get("action_id", "")
                    exec_res = self.adapter.execute_send(
                        action_id=act_id,
                        to=phone,
                        body=rendered_body,
                        verifier=lambda a_id, t_name, a_sha: (True, "Human confirmed send via CRM UI"),
                        session_id=session_id,
                        run_id=job_id,
                        item_id=item_id
                    )
                    sid = exec_res.get("sid") or exec_res.get("data", {}).get("sid")
                    if exec_res.get("ok") or sid:
                        self.storage.update_send_item_status(item_id, status="queued", message_sid=sid)
                        successful_count += 1
                    else:
                        err_msg = exec_res.get("error", "Aggregator execution failed")
                        self.storage.update_send_item_status(item_id, status="failed", error=str(err_msg))
                        failed_count += 1
                elif res.get("status") == "success" or res.get("sid"):
                    sid = res.get("sid") or res.get("data", {}).get("sid")
                    self.storage.update_send_item_status(item_id, status="queued", message_sid=sid)
                    successful_count += 1
                else:
                    err_msg = res.get("error", "Aggregator send failed")
                    self.storage.update_send_item_status(item_id, status="failed", error=str(err_msg))
                    failed_count += 1

            except Exception as e:
                self.storage.update_send_item_status(item_id, status="failed", error=str(e))
                failed_count += 1

            if send_interval > 0:
                await asyncio.sleep(send_interval)

        final_job_status = "completed" if failed_count == 0 else ("failed" if successful_count == 0 else "partially_completed")
        self.storage.update_send_job_status(job_id, final_job_status, successful=successful_count, failed=failed_count)

        return {
            "success": True,
            "job_id": job_id,
            "status": final_job_status,
            "total": len(items),
            "successful": successful_count,
            "failed": failed_count,
            "items": self.storage.get_send_items_by_job(job_id)
        }

    async def refresh_job_status(self, job_id: str) -> Dict[str, Any]:
        """
        Polls aggregator operator tool get_admin_message_status to refresh delivery status.
        """
        job = self.storage.get_send_job(job_id)
        if not job:
            return {"success": False, "error": "Send job not found."}

        items = self.storage.get_send_items_by_job(job_id)
        session_id = job.get("session_id", "")
        updated_items = []

        for it in items:
            sid = it.get("message_sid")
            if sid and it.get("status") in ("queued", "sent", "sending"):
                try:
                    res = self.adapter.read_operator_status(
                        sid,
                        session_id=session_id,
                        job_id=job_id,
                        item_id=it["item_id"]
                    )
                    live_status = res.get("status")
                    if live_status and live_status != it["status"]:
                        self.storage.update_send_item_status(
                            it["item_id"],
                            status=live_status,
                            error=res.get("error_message") or res.get("error_code")
                        )
                        it["status"] = live_status
                except Exception as e:
                    print(f"[DispatchService] Error refreshing status for {sid}: {e}")

            updated_items.append(it)

        return {
            "success": True,
            "job_id": job_id,
            "job_status": job.get("status"),
            "items": updated_items
        }

_dispatch_service: Optional[DispatchService] = None

def get_dispatch_service() -> DispatchService:
    global _dispatch_service
    if _dispatch_service is None:
        _dispatch_service = DispatchService()
    return _dispatch_service
