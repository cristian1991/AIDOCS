from __future__ import annotations

from pathlib import Path
from typing import Any, Literal


def register_code_edit_tools(
    *,
    server: Any,
    hub: Any,
    require_indexed_read_gate: Any,
    post_edit_reindex_and_grant: Any,
    file_get_lines: Any,
    file_create_file: Any,
    file_edit_lines: Any,
    file_batch_edit: Any,
    file_str_replace: Any,
    file_batch_str_replace: Any,
    available_config_edit_modes: Any,
    self_edit_available_in_profile: Any,
) -> None:
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Code Lines",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def code_get_lines(
        path: str,
        start_line: int = 1,
        count: int = 30,
        show_line_numbers: bool = True,
        known_exact_path: bool = False,
        root: str = "",
    ) -> dict[str, Any]:
        """Read specific lines from any file after indexed retrieval has established enough context."""
        project_root = Path(root)
        gate = require_indexed_read_gate(
            hub,
            project_root,
            exact_path=path,
            known_exact_path=known_exact_path,
        )
        if gate:
            return gate
        result = file_get_lines(
            project_root,
            path,
            start_line=start_line,
            count=count,
            show_line_numbers=show_line_numbers,
        )
        if isinstance(result, dict) and hub.code.is_file_stale(project_root, path):
            result["stale"] = "File modified since last index — content may differ from indexed symbols. Run code_index_sync if needed."
        return result

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Create File",
        }
    )
    def code_create_file(
        path: str,
        content: str,
        config_edit_mode: str | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Create a new file at a relative path with exact content."""
        project_root = Path(root)
        result = file_create_file(
            project_root,
            path,
            content,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success"):
            post_edit_reindex_and_grant(
                hub,
                project_root,
                "code_create_file",
                str(result.get("path") or path),
            )
        return result

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Insert Lines",
        }
    )
    def code_insert_lines(
        path: str,
        before_line: int,
        content: str,
        config_edit_mode: Literal["explicit_user_permitted"] | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Insert content before a specific line. Clearer than code_edit_lines insert mode."""
        project_root = Path(root)
        result = file_edit_lines(
            project_root, path,
            start_line=before_line,
            end_line=before_line - 1,
            new_content=content,
            mode="insert",
            config_edit_mode=config_edit_mode,
        )
        if result.get("success"):
            post_edit_reindex_and_grant(hub, project_root, "code_insert_lines", str(result.get("path") or path))
        return result



    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Edit Lines",
        }
    )
    def code_edit_lines(
        path: str,
        start_line: int,
        end_line: int,
        new_content: str,
        expect: str | None = None,
        dry_run: bool = False,
        mode: str = "auto",
        config_edit_mode: str | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Replace a range of lines with new content, with safety verification."""
        project_root = Path(root)
        result = file_edit_lines(
            project_root,
            path,
            start_line=start_line,
            end_line=end_line,
            new_content=new_content,
            expect=expect,
            dry_run=dry_run,
            mode=mode,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success") and not result.get("dry_run"):
            post_edit_reindex_and_grant(
                hub,
                project_root,
                "code_edit_lines",
                str(result.get("path") or path),
            )
        return result

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Batch Edit",
        }
    )
    def code_batch_edit(
        edits: list[dict[str, Any]],
        dry_run: bool = False,
        atomic: bool = True,
        config_edit_mode: str | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Apply multiple line edits atomically across one or more files."""
        project_root = Path(root)
        result = file_batch_edit(
            project_root,
            edits,
            dry_run=dry_run,
            atomic=atomic,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success") and not dry_run:
            for item in result.get("results", []):
                if isinstance(item, dict) and item.get("success"):
                    post_edit_reindex_and_grant(
                        hub,
                        project_root,
                        "code_batch_edit",
                        str(item.get("path") or ""),
                    )
        return result

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "String Replace",
        }
    )
    def code_str_replace(
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
        config_edit_mode: str | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Quick string-match edit for small changes (old_str under 500 chars, unique in file)."""
        from .mcp_server_runtime_helpers import resolve_project_root
        project_root = resolve_project_root(root)
        result = file_str_replace(
            project_root,
            path,
            old_str,
            new_str,
            replace_all=replace_all,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success"):
            post_edit_reindex_and_grant(
                hub,
                project_root,
                "code_str_replace",
                str(result.get("path") or path),
            )
        return result

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Batch String Replace",
        }
    )
    def code_batch_str_replace(
        edits: list[dict[str, Any]],
        atomic: bool = True,
        config_edit_mode: str | None = None,
        root: str = "",
    ) -> dict[str, Any]:
        """Multiple string-match replacements across files, atomic."""
        project_root = Path(root)
        result = file_batch_str_replace(
            project_root,
            edits,
            atomic=atomic,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success"):
            for item in result.get("results", []):
                if isinstance(item, dict) and item.get("success"):
                    post_edit_reindex_and_grant(
                        hub,
                        project_root,
                        "code_batch_str_replace",
                        str(item.get("path") or ""),
                    )
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Config Edit Policy",
        }
    )
    def config_edit_policy_get(profile: str = "release") -> dict[str, Any]:
        """Return the release-profile config edit policy visible to agents."""
        return {
            "profile": profile,
            "available_modes": available_config_edit_modes(profile),
            "security": {
                "self_edit_available": self_edit_available_in_profile(profile),
            },
        }

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "openWorldHint": False,
            "title": "Protect File",
        },
    )
    def protect_file(pattern: str, root: str = "") -> dict[str, Any]:
        """Add a file or pattern to the protected files list. Protects against agent access. Examples: 'secrets.json', '*.local.json', 'config/keys/*'."""
        from .mcp_server_runtime_helpers import resolve_project_root
        project_root = Path(resolve_project_root(root))
        pattern = pattern.strip()
        if not pattern:
            return {"success": False, "error": "Pattern cannot be empty."}

        # Update aidocs.toml
        toml_path = project_root / "aidocs.toml"
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]

        config: dict[str, Any] = {}
        if toml_path.is_file():
            config = tomllib.loads(toml_path.read_text(encoding="utf-8"))

        gate = config.setdefault("gate", {})
        protected: list[str] = list(gate.get("protected_patterns", []))
        if pattern in protected:
            return {"success": True, "already_protected": True, "pattern": pattern}

        protected.append(pattern)
        gate["protected_patterns"] = protected

        # Write back — preserve structure as much as possible
        lines: list[str] = []
        if toml_path.is_file():
            lines = toml_path.read_text(encoding="utf-8").splitlines()

        # Find or create [gate] section and protected_patterns key
        import re
        gate_section_idx = None
        patterns_line_idx = None
        for i, line in enumerate(lines):
            if re.match(r"^\[gate\]", line.strip()):
                gate_section_idx = i
            if gate_section_idx is not None and "protected_patterns" in line:
                patterns_line_idx = i
                break

        patterns_toml = "protected_patterns = " + str(protected).replace("'", '"')
        if patterns_line_idx is not None:
            lines[patterns_line_idx] = patterns_toml
        elif gate_section_idx is not None:
            lines.insert(gate_section_idx + 1, patterns_toml)
        else:
            lines.append("")
            lines.append("[gate]")
            lines.append(patterns_toml)

        toml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"success": True, "pattern": pattern, "total_protected": len(protected)}
