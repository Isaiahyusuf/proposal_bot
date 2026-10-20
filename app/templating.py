import re
from typing import Dict, Optional

DEFAULT_TEMPLATE = (
    "Hey I came across {project_name} on DexScreener and wanted to ask if you’re the main developer "
    "behind the project or if there’s someone else handling the development\n\n"
    "I build crypto projects and Telegram bots and I’m looking to connect with teams that might need "
    "development help"
)

SUPPORTED_VARIABLES = ["project_name", "token_symbol", "blockchain", "developer_username", "project_website"]
_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
MAX_LENGTH = 2000
# Only project_name gets a neutral wording fallback so the default message stays grammatical.
_FALLBACKS = {"project_name": "your project"}


def unknown_variables(body: str) -> list:
    return sorted({m for m in _VAR.findall(body) if m not in SUPPORTED_VARIABLES})


def validate_template(body: str) -> Optional[str]:
    if not body.strip():
        return "Message cannot be empty."
    if len(body) > MAX_LENGTH:
        return f"Message must be {MAX_LENGTH} characters or fewer."
    bad = unknown_variables(body)
    if bad:
        return "Unsupported variables: " + ", ".join("{%s}" % b for b in bad)
    return None


def _tidy(text: str) -> str:
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)            # empty brackets left by missing values
    text = re.sub(r"[ \t]+([,.!?;:])", r"\1", text)         # space before punctuation
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render_message(body: str, values: Dict[str, Optional[str]]) -> str:
    """Substitute only supplied, non-empty values; missing/unknown variables never leave placeholders."""
    def sub(m):
        name = m.group(1)
        val = (values.get(name) or "").strip() if name in SUPPORTED_VARIABLES else ""
        return val or _FALLBACKS.get(name, "")
    return _tidy(_VAR.sub(sub, body))


SAMPLE_VALUES = {
    "project_name": "MoonCat", "token_symbol": "MCAT", "blockchain": "solana",
    "developer_username": "@sample_dev", "project_website": "https://mooncat.example",
}
