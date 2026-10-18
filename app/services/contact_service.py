import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from app.core.config import get_config
from app.core.storage import get_storage
from app.adapters.input_adapter import (
    get_adapter_for_file,
    detect_columns,
    normalize_phone_e164,
    mask_phone
)

class ContactService:
    def __init__(self):
        self.config = get_config()
        self.storage = get_storage()

    def load_suppression_list(self) -> set:
        cfg = get_config(reload=True)
        supp_path = Path(cfg.paths.suppression_file)
        if not supp_path.exists():
            return set()
        try:
            with open(supp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
                elif isinstance(data, dict):
                    return set(data.get("suppressed_phones", []))
        except Exception:
            return set()
        return set()

    def process_contacts(
        self,
        session_id: str,
        content: bytes,
        filename: str = "",
        country_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Parses, auto-detects columns, normalizes phones, checks suppression,
        deduplicates, and generates masked previews.
        """
        adapter = get_adapter_for_file(filename, content)
        raw_rows = adapter.parse(content, filename)
        total_rows = len(raw_rows)

        if total_rows == 0:
            return {
                "success": False,
                "error": "The file is empty. Please upload a file with contact data.",
                "total_rows": 0
            }

        name_col, phone_col, extra_cols = detect_columns(raw_rows[0])
        if not name_col or not phone_col:
            return {
                "success": False,
                "needs_column_mapping": True,
                "detected_columns": list(raw_rows[0].keys()),
                "total_rows": total_rows,
                "message": f"Could not confidently identify Name and Phone columns. Found: {list(raw_rows[0].keys())}"
            }

        # Suppression list check
        suppression_set = self.load_suppression_list()
        suppression_active = len(suppression_set) > 0

        valid_contacts: List[Dict[str, Any]] = []
        seen_phones = set()
        invalid_count = 0
        duplicates_count = 0
        suppressed_count = 0

        for r in raw_rows:
            raw_name = str(r.get(name_col, "")).strip()
            raw_phone = str(r.get(phone_col, "")).strip()

            if not raw_name or not raw_phone:
                invalid_count += 1
                continue

            normalized = normalize_phone_e164(raw_phone, country_code)
            if not normalized:
                invalid_count += 1
                continue

            if normalized in seen_phones:
                duplicates_count += 1
                continue

            if suppression_active and normalized in suppression_set:
                suppressed_count += 1
                continue

            seen_phones.add(normalized)
            extra_data = {k: v for k, v in r.items() if k not in (name_col, phone_col)}
            valid_contacts.append({
                "name": raw_name,
                "phone": normalized,
                "masked_phone": mask_phone(normalized),
                "extra": extra_data
            })

        if len(valid_contacts) == 0:
            return {
                "success": False,
                "error": f"Invalid contact data: All {total_rows} rows were invalid or missing valid phone numbers. Please upload/paste valid numbers with country codes (e.g. +14155552671 or +919876543210).",
                "total_rows": total_rows,
                "invalid_count": invalid_count
            }

        # 3-row preview
        preview_rows = [
            {"name": c["name"], "masked_phone": c["masked_phone"]}
            for c in valid_contacts[:3]
        ]

        # Log validation in DB & JSONL
        self.storage.log_validation(
            session_id=session_id,
            validation_type="contact_sanitization",
            passed=len(valid_contacts) > 0,
            details={
                "total_rows": total_rows,
                "valid": len(valid_contacts),
                "invalid": invalid_count,
                "duplicates": duplicates_count,
                "suppressed": suppressed_count,
                "name_col": name_col,
                "phone_col": phone_col
            }
        )

        return {
            "success": True,
            "filename": filename,
            "total_rows": total_rows,
            "columns": {
                "name": name_col,
                "phone": phone_col,
                "extra": extra_cols
            },
            "valid_count": len(valid_contacts),
            "invalid_count": invalid_count,
            "duplicates_count": duplicates_count,
            "suppressed_count": suppressed_count,
            "preview": preview_rows,
            "contacts": valid_contacts
        }

_contact_service: Optional[ContactService] = None

def get_contact_service() -> ContactService:
    global _contact_service
    if _contact_service is None:
        _contact_service = ContactService()
    return _contact_service
