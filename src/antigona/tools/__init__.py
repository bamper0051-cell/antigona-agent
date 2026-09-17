"""Tools package — contract-based tool abstractions.

Every tool implements the Tool ABC. Tools are registered in ToolRegistry
and executed via the Policy → Executor pipeline.
"""

from __future__ import annotations

from antigona.tools.action_executor import (
    Action,
    ActionExecutor,
    ActionResult,
    ActionType,
    ExecutionMode,
)
from antigona.tools.archiver import (
    ArchiveError,
    ArchiveExistsError,
    ArchiveResult,
    ArchiveTooLargeError,
    create_targz,
    create_zip,
)
from antigona.tools.contracts import (
    FailingTool,
    MockTool,
    RiskLevel,
    Tool,
    ToolCategory,
    ToolInput,
    ToolOutput,
    ToolSpec,
    ToolStatus,
)
from antigona.tools.documents import (
    CollectedFile,
    RootBoundaryError,
    collect_by_content,
    collect_by_metadata,
    collect_results_to_list,
)
from antigona.tools.execution_service import ToolExecutionService
from antigona.tools.filesystem_read import FilesystemReadTool, _resolve_safe
from antigona.tools.filesystem_write import FilesystemWriteTool
from antigona.tools.image_gen import (
    ImageGenerator,
)
from antigona.tools.key_manager import (
    apply_provider,
    configure_full_keyflow,
    detect_provider,
    get_provider_config,
    parse_key,
    verify_key,
    write_key,
)
from antigona.tools.provider_switcher import (
    format_provider_list,
    get_available_providers,
    switch_to_provider,
    test_current_provider,
)
from antigona.tools.registry import ToolNotFoundError, ToolRegistrationError
from antigona.tools.terminal import TerminalTool
from antigona.tools.vision import (
    VisionAnalyzer,
    analyze_image,
)
from antigona.tools.web_search import (
    ExtractResult,
    SearchResult,
    WebSearchTool,
    extract,
    search,
    search_and_format,
)

__all__ = [
    "Action",
    "ActionExecutor",
    "ActionResult",
    "ActionType",
    "ArchiveError",
    "ArchiveExistsError",
    "ArchiveResult",
    "ArchiveTooLargeError",
    "CollectedFile",
    "ExecutionMode",
    "FailingTool",
    "FilesystemReadTool",
    "FilesystemWriteTool",
    "ImageGenerator",
    "MockTool",
    "RiskLevel",
    "RootBoundaryError",
    "Tool",
    "ToolCategory",
    "ToolExecutionService",
    "ToolInput",
    "ToolNotFoundError",
    "ToolOutput",
    "ToolRegistrationError",
    "ToolSpec",
    "ToolStatus",
    "TerminalTool",
    "_resolve_safe",
    "apply_provider",
    "collect_by_content",
    "collect_by_metadata",
    "collect_results_to_list",
    "configure_full_keyflow",
    "create_targz",
    "create_zip",
    "detect_provider",
    "execute_actions_from_text",
    "ExtractResult",
    "SearchResult",
    "VisionAnalyzer",
    "WebSearchTool",
    "analyze_image",
    "extract",
    "format_provider_list",
    "get_available_providers",
    "search",
    "search_and_format",
    "get_provider_config",
    "parse_key",
    "switch_to_provider",
    "test_current_provider",
    "verify_key",
    "write_key",
]
