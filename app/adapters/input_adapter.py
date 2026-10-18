from abc import ABC, abstractmethod
from typing import List, Dict, Any, Tuple
import io
import csv
import phonenumbers
from openpyxl import load_workbook

class BaseInputAdapter(ABC):
    @abstractmethod
    def parse(self, content: bytes, filename: str = "") -> List[Dict[str, Any]]:
        """Parse raw input content into a list of raw row dictionaries."""
        pass

class TextInputAdapter(BaseInputAdapter):
    def parse(self, content: bytes, filename: str = "") -> List[Dict[str, Any]]:
        text = content.decode("utf-8", errors="replace").strip()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            return []

        # Check if first line is a header (contains 'name' or 'phone' or 'mobile')
        first_line_parts = [p.strip() for p in lines[0].split(",") if p.strip()]
        has_header = any(h.lower() in ("name", "full name", "phone", "mobile", "number", "email", "city") for h in first_line_parts)

        headers = []
        start_idx = 0
        if has_header:
            headers = first_line_parts
            start_idx = 1
        else:
            # Default headers based on number of columns
            headers = ["Name", "Phone"]

        rows = []
        for line in lines[start_idx:]:
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if not parts:
                continue
            row_dict = {}
            for idx, val in enumerate(parts):
                col_name = headers[idx] if idx < len(headers) else f"col_{idx+1}"
                row_dict[col_name] = val
            rows.append(row_dict)
        return rows

class CsvInputAdapter(BaseInputAdapter):
    def parse(self, content: bytes, filename: str = "") -> List[Dict[str, Any]]:
        text = content.decode("utf-8-sig", errors="replace")
        # Detect delimiter (comma or tab)
        delimiter = "\t" if "\t" in text.split("\n")[0] or filename.endswith(".tsv") else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        rows = []
        for r in reader:
            rows.append({k.strip(): str(v).strip() for k, v in r.items() if k is not None})
        return rows

class ExcelInputAdapter(BaseInputAdapter):
    def parse(self, content: bytes, filename: str = "") -> List[Dict[str, Any]]:
        wb = load_workbook(filename=io.BytesIO(content), read_only=True, data_only=True)
        sheet = wb.active
        rows = []
        headers = []
        for idx, row_vals in enumerate(sheet.iter_rows(values_only=True)):
            if idx == 0:
                headers = [str(c).strip() if c is not None else f"col_{i}" for i, c in enumerate(row_vals)]
            else:
                if any(v is not None for v in row_vals):
                    row_dict = {}
                    for col_idx, cell_value in enumerate(row_vals):
                        h = headers[col_idx] if col_idx < len(headers) else f"col_{col_idx}"
                        row_dict[h] = str(cell_value).strip() if cell_value is not None else ""
                    rows.append(row_dict)
        return rows

def get_adapter_for_file(filename: str, content: bytes) -> BaseInputAdapter:
    lower = filename.lower()
    if lower.endswith(".xlsx") or lower.endswith(".xls"):
        return ExcelInputAdapter()
    elif lower.endswith(".csv") or lower.endswith(".tsv"):
        return CsvInputAdapter()
    else:
        # Default fallback to text / csv
        return TextInputAdapter() if "\n" in content.decode("utf-8", errors="replace") else CsvInputAdapter()

# Sanitization & column auto-detection
NAME_CANDIDATES = {"name", "full name", "fullname", "donor name", "customer name", "client name", "patient name", "contact name", "first name"}
PHONE_CANDIDATES = {"phone", "mobile", "cell", "number", "contact", "whatsapp", "tel", "phone number", "mobile no", "mobile number"}

def detect_columns(sample_row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], List[str]]:
    name_col = None
    phone_col = None
    extra_cols = []
    
    for col in sample_row.keys():
        cleaned = col.lower().strip()
        if not name_col and cleaned in NAME_CANDIDATES:
            name_col = col
        elif not phone_col and cleaned in PHONE_CANDIDATES:
            phone_col = col
        else:
            extra_cols.append(col)

    # Secondary fuzzy check if still not found
    if not name_col:
        for col in list(extra_cols):
            if "name" in col.lower():
                name_col = col
                extra_cols.remove(col)
                break

    if not phone_col:
        for col in list(extra_cols):
            if any(term in col.lower() for term in ["phone", "mobile", "cell", "tel", "num"]):
                phone_col = col
                extra_cols.remove(col)
                break

    return name_col, phone_col, extra_cols

def normalize_phone_e164(raw_phone: str, default_country: Optional[str] = None) -> Optional[str]:
    cleaned = "".join(ch for ch in str(raw_phone) if ch.isdigit() or ch == "+")
    if not cleaned:
        return None
    try:
        # Try parsing without country code if starts with '+'
        if cleaned.startswith("+"):
            parsed = phonenumbers.parse(cleaned, None)
        else:
            region = default_country.upper() if default_country else None
            parsed = phonenumbers.parse(cleaned, region)
        
        if phonenumbers.is_valid_number(parsed) or phonenumbers.is_possible_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        pass

    # Fallback for standard E.164 strings (e.g. test simulator / mock aggregator numbers)
    if cleaned.startswith("+") and 8 <= len(cleaned) <= 16 and cleaned[1:].isdigit():
        return cleaned

    return None

def mask_phone(e164: str) -> str:
    if len(e164) <= 6:
        return e164
    # e.g. +91••••••3210
    country_prefix = e164[:3]
    last_four = e164[-4:]
    return f"{country_prefix}••••••{last_four}"
