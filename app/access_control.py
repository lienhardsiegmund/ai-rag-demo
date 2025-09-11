import json
from typing import List

ROLES_FILE = "config/roles.json"

def load_roles() -> dict:
    try:
        with open(ROLES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def get_allowed_sources(role: str) -> List[str]:
    roles = load_roles()
    role_def = roles.get(role) or {}
    return role_def.get("allowed_sources", [])
