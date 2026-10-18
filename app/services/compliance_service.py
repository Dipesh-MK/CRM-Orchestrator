import re
from typing import Dict, Any, List
from app.core.config import get_config

def count_words(text: str) -> int:
    """Accurately count words in a string."""
    tokens = [t for t in re.split(r"\s+", text.strip()) if t]
    return len(tokens)

def check_template_compliance(
    body: str,
    business_name: str,
    word_limit: int,
    optout_text: str,
    link_or_contact: str = ""
) -> Dict[str, Any]:
    """
    Validates message compliance rules:
    - Max word_limit words
    - Ends with optout_text
    - Includes business_name at least once
    - No ALL-CAPS sentences
    - No '!!!' or '???'
    - Includes link/contact if provided
    """
    words = count_words(body)
    violations: List[str] = []

    # 1. Word limit
    if words > word_limit:
        violations.append(f"Exceeds word limit: {words} words (limit: {word_limit})")

    # 2. Ends with optout text
    clean_body = body.strip()
    clean_optout = optout_text.strip()
    if not clean_body.endswith(clean_optout):
        violations.append(f"Does not end with mandatory opt-out text: '{clean_optout}'")

    # 3. Includes business name (case-insensitive) or business placeholder
    if business_name and business_name.strip():
        has_business = (
            business_name.lower() in body.lower()
            or "{{business_name}}" in body.lower()
            or "{{organization_name}}" in body.lower()
            or "{{org_name}}" in body.lower()
        )
        if not has_business:
            violations.append(f"Missing business name: '{business_name}'")

    # 4. No ALL CAPS sentences
    sentences = re.split(r"[.!?]\s+", clean_body)
    for s in sentences:
        s_clean = re.sub(r"[^a-zA-Z\s]", "", s).strip()
        words_in_s = s_clean.split()
        if len(words_in_s) >= 4 and s_clean.isupper():
            violations.append(f"Found ALL-CAPS sentence: '{s[:40]}...'")
            break

    # 5. No '!!!' or '???'
    if "!!!" in body:
        violations.append("Contains excessive punctuation: '!!!'")
    if "???" in body:
        violations.append("Contains excessive punctuation: '???'")

    # 6. Includes link/contact if provided
    if link_or_contact and link_or_contact.strip():
        # Tag {{link}} or raw link or mention
        if "{{link}}" not in body and link_or_contact.lower() not in body.lower():
            violations.append(f"Missing link or contact placeholder: '{link_or_contact}'")

    return {
        "compliant": len(violations) == 0,
        "word_count": words,
        "word_limit": word_limit,
        "violations": violations
    }
