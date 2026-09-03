from pathlib import Path


CUTTING_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CUTTING_ROOT.parent.parent
DATA_DIR = CUTTING_ROOT / "data" / "conversations"
ANNOTATION_FILE = CUTTING_ROOT / "annotations" / "initial_60.jsonl"
DEFAULT_OUTPUT_DIR = CUTTING_ROOT / "artifacts" / "cutting_opencode_go"
PROJECT_ENV_FILE = REPO_ROOT / ".env"

PROMPT_VERSION = "conversation-cutting-en-v5-general-boundary-functions"
DEFAULT_MODEL = "gpt-5.6-luna"
