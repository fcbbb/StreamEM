from pathlib import Path


# All paths are anchored here so commands work from the repository root or
# from analyse4/cutting itself.
CUTTING_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CUTTING_ROOT.parent.parent
DATA_DIR = CUTTING_ROOT / "data" / "conversations"
ANNOTATION_FILE = CUTTING_ROOT / "data" / "gold" / "initial_60.jsonl"
PROMPTS_DIR = CUTTING_ROOT / "prompts"
RUNS_DIR = CUTTING_ROOT / "runs"
REPORTS_DIR = CUTTING_ROOT / "reports"
PROJECT_ENV_FILE = REPO_ROOT / ".env"

DEFAULT_PROMPT_VERSION = "v001_fact_single"
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_RUN_ID = "latest"
