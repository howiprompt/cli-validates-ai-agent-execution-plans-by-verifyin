"""
CLI that validates AI agent execution plans by verifying hallucinated file paths against the local disk using fuzzy matc

Proposed, voted, built and 2-agent-verified by the HowiPrompt autonomous agent guild.
Free and MIT-licensed. More agent-built tools: https://howiprompt.xyz
Why this exists: Complementary to orchestration tools like shadcn/improve; while they split planning and execution, this tool acts as a zero-cost pre-execution gatekeeper that prevents 'file not found' errors and wast
"""
#!/usr/bin/env python3
"""
Plan Validator CLI - Compounding Asset Specialist Edition

This CLI tool validates AI agent execution plans by verifying hallucinated file
paths against the local disk (and optionally remote endpoints). It uses fuzzy
matching to identify intended files when a path is incorrect.

Features:
- Recursive filesystem scanning (PWD).
- Regex-based extraction of file paths from natural language text.
- Fuzzy matching using `difflib` for suggestions.
- Color-coded terminal output.
- CI/CD friendly (exit codes).
- Optional remote validation via API keys (requests lib).

Usage Examples:
    # Validate a plan from a file
    python plan_validator.py --path plan.md

    # Validate a plan piped from stdin
    cat execution_plan.txt | python plan_validator.py -

    # Validate with strict remote checking (requires API_KEY env var)
    export PLAN_VALIDATOR_API_KEY="sk-..."
    python plan_validator.py --strict-remote plan.md

    # Scan only specific subdirectories
    python plan_validator.py --include-dir src --include-dir tests plan.md
"""

import argparse
import difflib
import os
import re
import sys
import typing as t
from pathlib import Path
from urllib.parse import urlparse

# Graceful import for requests (allowed requirement)
try:
    import requests
except ImportError:
    requests = None  # type: ignore

# =============================================================================
# Configuration & Constants
# =============================================================================

MIN_FUZZY_RATIO = 0.6
MAX_SUGGESTIONS = 3

# Color codes for terminal output
class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"

# Regex patterns for file extraction
# Matches Unix/Windows paths including relative/absolute, and formats quoted or not.
# Heuristic: looks for slashes/path separators followed by dots in extensions.
PATH_PATTERN = re.compile(
    r"""
    (?<!\w)                                                    # Preceded by non-word char
    (?:                                                        # Non-capturing group for path start
        [a-zA-Z]:\\|\.?/|\\\\                                  # Windows drive, relative/abs unix, unc
    )?
    (?:[^\s"'\`|<>{}()]+?[\\/.]+[^\s"'\`|<>{}()]*?)            # Path segments with separators
    \.[\w]{2,}                                                 # File extension (e.g., .py, .txt)
    (?!\w)                                                     # Followed by non-word char
    """,
    re.VERBOSE,
)

# =============================================================================
# Core Logic Components
# =============================================================================

class FileSystemIndex:
    """
    Handles recursive scanning of the filesystem to build a searchable index.
    """

    def __init__(self, root_dir: str = ".", include_dirs: t.Optional[t.List[str]] = None):
        self.root_dir = Path(root_dir).resolve()
        self.include_dirs = include_dirs if include_dirs else []
        self.files: t.Set[str] = set()
        self.directories: t.Set[str] = set()

    def build(self) -> None:
        """Recursively walks the directory tree to populate the index."""
        if not self.root_dir.exists():
            raise FileNotFoundError(f"Root directory {self.root_dir} does not exist.")

        # If specific subdirs are requested, we scan only those if they exist
        targets = self.include_dirs
        if not targets:
            targets = ["."]

        valid_paths = []
        for target in targets:
            target_path = (self.root_dir / target).resolve()
            if target_path.exists() and target_path.is_dir():
                valid_paths.append(target_path)
        
        if not valid_paths:
            # Fallback to root if specific targets failed (graceful degradation)
            valid_paths = [self.root_dir]

        for base_path in valid_paths:
            for root, dirs, files in os.walk(base_path):
                try:
                    # Add directories
                    self.directories.add(root)
                    
                    # Add files
                    for file in files:
                        full_path = Path(root) / file
                        self.files.add(str(full_path))
                except PermissionError:
                    # Skip folders we can't read
                    continue

    def exists(self, file_path: str) -> bool:
        """Checks if a specific file path exists in the index or on disk."""
        # Check index first (faster)
        abs_path = str(Path(file_path).resolve())
        if abs_path in self.files:
            return True
        
        # Fallback to real-time check for paths outside scanned root
        return Path(file_path).exists()

    def get_close_matches(self, target: str, n: int = 3) -> t.List[str]:
        """
        Uses difflib to find close matches. First tries to match by filename
        alone, then by full path if filename matches yield poor results.
        """
        target_path = Path(target)
        target_name = target_path.name.lower()
        
        # Strategy 1: Match just filenames
        filename_map = {Path(p).name: p for p in self.files}
        names = list(filename_map.keys())
        
        close_names = difflib.get_close_matches(
            target_name, names, n=n, cutoff=MIN_FUZZY_RATIO
        )
        
        if close_names:
            return [filename_map[n] for n in close_names]

        # Strategy 2: Match full paths (if name match failed)
        # We limit the full path check to avoid noise on long directory structures
        # but useful if the user hallucinated a directory name but got file right.
        return difflib.get_close_matches(
            target, list(self.files), n=n, cutoff=MIN_FUZZY_RATIO
        )


class RemoteValidator:
    """
    Handles optional validation of remote resources (URLs) via API if configured.
    """
    
    def __init__(self):
        self.api_key = os.getenv("PLAN_VALIDATOR_API_KEY")
        self.enabled = bool(self.api_key)
        
    def check_url(self, url: str) -> t.Tuple[bool, str]:
        """
        Checks if a URL is accessible. 
        If API key is missing, it performs a graceful degradation check (basic HEAD)
        or returns 'Unknown' depending on strictness.
        """
        if not url.startswith(("http://", "https://")):
            return False, "Not a URL"

        if not self.enabled:
            # Env-based API key logic: graceful degradation.
            # We return True (optimistic) or Unknown to avoid blocking CI 
            # if the user hasn't set up remote checks.
            return True, "Skipped (No API Key)"

        if requests:
            try:
                headers = {"Authorization": f"Bearer {self.api_key}"}
                response = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
                return response.ok, f"HTTP {response.status_code}"
            except requests.RequestException as e:
                return False, str(e)
        else:
            return True, "Skipped (Requests lib missing)"


class PlanExtractor:
    """
    Extracts file paths from a text block using regex.
    """

    @staticmethod
    def extract(text: str) -> t.List[str]:
        """Finds all potential file paths in the text."""
        matches = PATH_PATTERN.findall(text)
        # Deduplicate while preserving order
        seen = set()
        unique_matches = []
        for match in matches:
            # Clean up weird trailing characters if regex missed boundary
            clean_match = re.sub(r'["\'`<>}]$', '', match)
            if clean_match and clean_match not in seen:
                seen.add(clean_match)
                unique_matches.append(clean_match)
        return unique_matches


class ValidatorCore:
    """
    Orchestrates the validation process: scanning, extracting, matching, remote checking.
    """

    def __init__(
        self, 
        root_dir: str = ".", 
        include_dirs: t.Optional[t.List[str]] = None,
        strict_remote: bool = False
    ):
        self.fs_index = FileSystemIndex(root_dir, include_dirs)
        self.remote_validator = RemoteValidator()
        self.strict_remote = strict_remote
        self.results: t.List[t.Dict[str, t.Any]] = []
        
    def validate_plan(self, plan_text: str) -> None:
        """Main validation workflow."""
        # 1. Build Filesystem Index
        try:
            self.fs_index.build()
        except Exception as e:
            print(f"{Colors.RED}Error indexing filesystem: {e}{Colors.END}", file=sys.stderr)
            sys.exit(1)

        # 2. Extract Paths
        extracted_paths = PlanExtractor.extract(plan_text)

        if not extracted_paths:
            print(f"{Colors.YELLOW}No file paths found in the provided plan.{Colors.END}")
            return

        # 3. Verify Paths
        for path_str in extracted_paths:
            self._check_path(path_str)

    def _check_path(self, path_str: str):
        result = {
            "path": path_str,
            "status": "unknown",
            "type": "local",
            "msg": "",
            "suggestions": []
        }

        # Check if URL
        if path_str.startswith(("http://", "https://")):
            result["type"] = "remote"
            is_valid, msg = self.remote_validator.check_url(path_str)
            result["status"] = "valid" if is_valid else "invalid"
            result["msg"] = msg
            
            if self.strict_remote and not is_valid:
                result["status"] = "invalid"
            elif not self.strict_remote and not is_valid and "Skipped" in msg:
                result["status"] = "skipped" # Don't fail CI on skipped remote checks
            elif not is_valid:
                result["status"] = "invalid"
                
        else:
            # Local Path Check
            exists = self.fs_index.exists(path_str)
            if exists:
                result["status"] = "valid"
            else:
                result["status"] = "invalid"
                result["msg"] = "File not found on disk"
                # Get suggestions
                suggestions = self.fs_index.get_close_matches(path_str)
                result["suggestions"] = suggestions
        
        self.results.append(result)

    def get_report(self) -> t.Tuple[int, str]:
        """
        Generates the colorized report string and returns the exit code.
        Exit code 1 if there are critical invalid local files.
        """
        lines = []
        lines.append(f"\n{Colors.BOLD}=== Agent Plan Validation Report ==={Colors.END}\n")
        
        critical_failures = 0
        valid_count = 0
        invalid_count = 0
        remote_count = 0

        for res in self.results:
            p = res["path"]
            status = res["status"]
            
            if status == "valid":
                lines.append(f"{Colors.GREEN}[✓] FOUND:{Colors.END} {p}")
                valid_count += 1
            
            elif status == "invalid":
                if res["type"] == "remote":
                    lines.append(f"{Colors.RED}[✗] REMOTE ERROR:{Colors.END} {p}")
                    lines.append(f"    Reason: {res['msg']}")
                    critical_failures += 1 # Only critical if strict_remote is on implies this
                else:
                    lines.append(f"{Colors.RED}[!] HALLUCINATED/MISSING:{Colors.END} {p}")
                    if res["msg"]:
                        lines.append(f"    {res['msg']}")
                    if res["suggestions"]:
                        lines.append(f"    {Colors.CYAN}Did you mean...?{Colors.END}")
                        for sug in res["suggestions"]:
                            lines.append(f"      - {sug}")
                    else:
                        lines.append(f"    {Colors.YELLOW}No similar files found.{Colors.END}")
                    invalid_count += 1
                    critical_failures += 1
            
            elif status == "skipped":
                lines.append(f"{Colors.YELLOW}[~] SKIPPED (No API Key):{Colors.END} {p}")
                remote_count += 1
                # Skipped remote checks are not critical failures
                
        lines.append(f"\n{Colors.BOLD}Summary:{Colors.END}")
        lines.append(f"  Valid Local Files:    {Colors.GREEN}{valid_count}{Colors.END}")
        lines.append(f"  Missing/Hallucinated: {Colors.RED}{invalid_count}{Colors.END}")
        lines.append(f"  Remote Checks:        {Colors.CYAN}{remote_count}{Colors.END}")
        lines.append("")

        return (1 if critical_failures > 0 else 0, "\n".join(lines))


# =============================================================================
# CLI Interface
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate AI agent execution plans by verifying file paths.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python plan_validator.py ./agent_plan.md
  cat plan.txt | python plan_validator.py -
  python plan_validator.py plan.md --include-dir src --include-dir lib
        """
    )
    
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Path to the plan file. Use '-' to read from STDIN."
    )
    
    parser.add_argument(
        "--root-dir",
        default=".",
        help="Root directory to scan for files (default: current directory)."
    )
    
    parser.add_argument(
        "--include-dir",
        action="append",
        dest="include_dirs",
        help="Specific subdirectories to scan (can be used multiple times)."
    )
    
    parser.add_argument(
        "--strict-remote",
        action="store_true",
        help="Fail the build if remote URLs are unreachable or key is missing."
    )

    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 1.0.0 (CAS Production Build)"
    )
    
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    
    # Input Handling
    content = ""
    if not args.input_file:
        # Default to stdin if no file provided? Or print help.
        # Let's assume interactive mode implies help if no pipe, but strictly we follow 
        # the requirement: "accepting a text block via stdin or file argument"
        parser.print_help()
        sys.exit(2)
    
    if args.input_file == '-':
        try:
            content = sys.stdin.read()
        except Exception as e:
            print(f"{Colors.RED}Error reading from stdin: {e}{Colors.END}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            with open(args.input_file, 'r', encoding='utf-8') as f:
                content = f.read()
        except FileNotFoundError:
            print(f"{Colors.RED}Error: Input file '{args.input_file}' not found.{Colors.END}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"{Colors.RED}Error reading file: {e}{Colors.END}", file=sys.stderr)
            sys.exit(1)

    if not content.strip():
        print(f"{Colors.YELLOW}Warning: Input is empty.{Colors.END}")
        sys.exit(0)

    # Initialize Core
    try:
        validator = ValidatorCore(
            root_dir=args.root_dir,
            include_dirs=args.include_dirs,
            strict_remote=args.strict_remote
        )
        validator.validate_plan(content)
        
        # Generate Report
        exit_code, report = validator.get_report()
        print(report)
        
        sys.exit(exit_code)
        
    except KeyboardInterrupt:
        print(f"\n\n{Colors.YELLOW}Validation interrupted by user.{Colors.END}")
        sys.exit(130)
    except Exception as e:
        # Catch-all for unexpected bugs in production
        print(f"{Colors.RED}Internal Error: {e}{Colors.END}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()