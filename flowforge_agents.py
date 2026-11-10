"""Private agent library, inspectable migration and OpenCode directory injection.

Markdown is retained verbatim; OpenCode, not a partial YAML reimplementation,
loads tool/permission/provider metadata. Catalog parsing only reads display fields.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from flowforge_storage import atomic_json, file_transaction

FILE_REF = re.compile(r"\{file:([^}]+)\}")
ABS_REF = re.compile(
    r"(?<![\w:/.])(?:~/|/(?:home|srv|opt|tmp|etc|var|usr|mnt|media|data|run|Users|root)/)[^\s`<>\"'{}|]+"
)
REL_REF = re.compile(r"(?<![\w/])(?:docs/|skills/|prompts/|agents/|\.opencode/)[\w./*-]+")
AGENT_ID = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
SECRET_NAME = re.compile(
    r"(?:^|[._-])(?:auth|credentials?|secrets?|tokens?|\.env)(?:$|[._-])", re.I
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scalar(front: str, key: str) -> str | None:
    match = re.search(r"(?m)^" + re.escape(key) + r":\s*([^\n]*)", front)
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value in {">", "|", ">-", "|-"}:
        tail = front[match.end() :]
        lines = []
        for line in tail.splitlines():
            if line.strip() and not line.startswith((" ", "\t")):
                break
            if line.strip():
                lines.append(line.strip())
        value = " ".join(lines)
    return value.strip("\"'") or None


def metadata(path: Path, text: str | None = None) -> dict:
    text = path.read_text(encoding="utf-8") if text is None else text
    parts = re.split(r"(?m)^---\s*$", text, maxsplit=2)
    issues = []
    front = parts[1] if len(parts) == 3 and not parts[0].strip() else ""
    if not front:
        issues.append("Missing or malformed YAML frontmatter")
    if front and not scalar(front, "description"):
        issues.append("Missing agent description")
    for line in front.splitlines():
        if "\t" in line[: len(line) - len(line.lstrip())]:
            issues.append("Tabs in YAML indentation")
        value = line.split(":", 1)[1].strip() if ":" in line else ""
        if value.startswith(("'", '"')) and value.count(value[0]) % 2:
            issues.append("Unclosed quoted YAML scalar")
    mode = scalar(front, "mode")
    if mode and mode not in {"primary", "subagent", "all"}:
        issues.append(f"Invalid mode: {mode}")
    if scalar(front, "disable") == "true":
        issues.append("Agent is disabled")
    # Common legacy typos are reported, never silently rewritten.
    for key in ("disabled", "disbled"):
        if scalar(front, key) is not None:
            issues.append(f"Unsupported '{key}'; OpenCode uses 'disable'")
    return {
        "description": scalar(front, "description"),
        "model": scalar(front, "model"),
        "mode": mode,
        "issues": issues,
    }


def references(text: str) -> list[str]:
    explicit = set(FILE_REF.findall(text))
    relative = {
        ref
        for ref in REL_REF.findall(text)
        if not ref.startswith(".opencode/tasks/")
        and Path(ref).suffix in {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh"}
    }
    refs = explicit | set(ABS_REF.findall(text)) | relative
    return sorted({ref.rstrip(".,;:)").removesuffix("**") for ref in refs if ref})


def dependencies(text: str, agent_file: Path, workspace: Path | None = None) -> list[dict]:
    def exists(path):
        if any(c in str(path) for c in "*?["):
            return any(path.parent.glob(path.name)) if path.parent.is_dir() else False
        return path.exists()

    result = []
    for ref in references(text):
        path = Path(ref).expanduser()
        if not path.is_absolute():
            local = agent_file.parent / path
            sibling = agent_file.parent.parent / path
            path = (
                local
                if exists(local)
                else sibling
                if exists(sibling)
                else (workspace / path if workspace else local)
            )
        pattern = any(c in str(path) for c in "*?[")
        kind = (
            "agent_pattern"
            if pattern and ref.startswith("agents/")
            else "permission_pattern"
            if pattern
            else "file_or_directory"
        )
        result.append(
            {"reference": ref, "path": str(path.resolve()), "exists": exists(path), "kind": kind}
        )
    return result


def transformed(context, text: str) -> str:
    mappings = {}
    if context.agents_dir and (context.agents_dir / "paths.json").exists():
        mappings.update(json.loads((context.agents_dir / "paths.json").read_text(encoding="utf-8")))
    mappings.update(context.agent_paths)
    for old, new in sorted(mappings.items(), key=lambda item: -len(item[0])):
        new = new.replace("{{workspace}}", str(context.workspace)).replace(
            "{{artifacts_dir}}", str(context.artifacts_dir or "")
        )
        text = text.replace(old, new)
    return text


def agent_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files = []
    for parent, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(parent) / d).is_symlink())
        for name in sorted(names):
            path = Path(parent) / name
            if path.suffix == ".md" and not path.is_symlink():
                files.append(path)
    return sorted(files)


def catalog(context) -> list[dict]:
    library = context.agents_dir
    origins = []
    if context.legacy:
        origins += [
            (Path.home() / ".opencode/agents", "user"),
            (Path.home() / ".config/opencode/agents", "user"),
        ]
    if library:
        origins.append((library / "agents", "central"))
    project = context.workspace / ".opencode/agents"
    if context.project_agents or context.legacy:
        origins.extend([(context.workspace / ".opencode/agent", "project"), (project, "project")])
    result = {}
    for directory, origin in origins:
        for path in agent_files(directory):
            name = path.relative_to(directory).with_suffix("").as_posix()
            text = transformed(context, path.read_text(encoding="utf-8"))
            info = metadata(path, text)
            if not AGENT_ID.fullmatch(name):
                info["issues"].append("Invalid agent identifier")
            deps = dependencies(text, path, context.workspace)
            info["issues"].extend(
                f"Unresolved dependency: {d['reference']}"
                for d in deps
                if not d["exists"] and d["kind"] != "permission_pattern"
            )
            result[name] = {
                "id": name,
                "origin": origin,
                "path": str(path),
                **info,
                "dependencies": deps,
            }
    project_json = {}
    for config_file in (
        context.workspace / "opencode.json",
        context.workspace / "opencode.jsonc",
        context.workspace / ".opencode/opencode.json",
        context.workspace / ".opencode/opencode.jsonc",
    ):
        if config_file.is_file():
            for name, definition in jsonc_agents(config_file).items():
                project_json[name] = (config_file, definition)
    if context.project_agents:
        for name, (config_file, definition) in project_json.items():
            if not isinstance(definition, dict):
                raise ValueError(f"Invalid agent {name} in {config_file}")
            info = {
                "id": name,
                "origin": "project_json",
                "path": str(config_file),
                "model": definition.get("model"),
                "description": definition.get("description"),
                "mode": definition.get("mode"),
                "dependencies": [],
                "issues": [],
            }
            if not AGENT_ID.fullmatch(name):
                info["issues"].append("Invalid agent identifier")
            info["dependencies"] = dependencies(
                transformed(context, json.dumps(definition)), config_file, context.workspace
            )
            info["issues"].extend(
                f"Unresolved dependency: {dep['reference']}"
                for dep in info["dependencies"]
                if not dep["exists"] and dep["kind"] != "permission_pattern"
            )
            if definition.get("disable"):
                info["issues"].append("Agent is disabled")
            result[name] = info
    if not context.project_agents and not context.legacy:
        for name, info in result.items():
            copies = [project / f"{name}.md", context.workspace / ".opencode/agent" / f"{name}.md"]
            different = any(
                path.exists() and path.read_bytes() != Path(info["path"]).read_bytes()
                for path in copies
            )
            if name in project_json or different:
                info["issues"].append(
                    "Project agent collision; explicitly enable project_agents or rename the project definition"
                )
    return [result[name] for name in sorted(result)]


def validate_agents(context, names) -> dict:
    names = list(names)
    content = os.getenv("OPENCODE_CONFIG_CONTENT")
    if content:
        try:
            overrides = json.loads(content).get("agent", {})
        except (ValueError, AttributeError):
            raise ValueError(
                "Invalid OPENCODE_CONFIG_CONTENT; cannot validate effective agents"
            ) from None
        if set(names) & set(overrides):
            raise ValueError(
                "OPENCODE_CONFIG_CONTENT overrides selected agents; move agent/model overrides into FlowForge configuration"
            )
    entries = {item["id"]: item for item in catalog(context)}
    for name in names:
        item = entries.get(name)
        if not item:
            raise ValueError(f"Agent not found in effective catalog: {name}")
        if item["issues"]:
            raise ValueError(f"Invalid agent {name}: {'; '.join(item['issues'])}")
    return entries


def _resolve_file_values(value: Any, base: Path) -> Any:
    if isinstance(value, str):
        return FILE_REF.sub(
            lambda match: (
                "{file:" + str((base / Path(match.group(1)).expanduser()).resolve()) + "}"
            ),
            value,
        )
    if isinstance(value, dict):
        return {key: _resolve_file_values(child, base) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve_file_values(child, base) for child in value]
    return value


def _relocate_file_references(text: str, agent_file: Path, workspace: Path) -> str:
    def relocate(match: re.Match[str]) -> str:
        choices = dependencies(match.group(0), agent_file, workspace)
        source = Path(choices[0]["path"]) if choices else Path(match.group(1)).expanduser()
        return "{file:" + str(source.resolve()) + "}"

    return FILE_REF.sub(relocate, text)


def prepare_opencode(context, directory: Path) -> dict:
    """Snapshot effective Markdown, preserving all native OpenCode metadata."""
    directory.mkdir(parents=True, exist_ok=True)
    if context.agents_dir and context.agents_dir.exists():
        # Only our imported library is copied, never project/global config or caches.
        for name in ("agents", "resources", "prompts", "skills"):
            source = context.agents_dir / name
            if source.exists():
                if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
                    raise ValueError(
                        f"Linked library resources must be imported as regular files: {source}"
                    )
                shutil.copytree(source, directory / name)
    json_definitions = {}
    for item in catalog(context):
        if not AGENT_ID.fullmatch(item["id"]):
            raise ValueError(f"Invalid agent identifier: {item['id']}")
        if item["origin"] == "project_json":
            definition = jsonc_agents(Path(item["path"]))[item["id"]]
            definition = json.loads(transformed(context, json.dumps(definition)))

            json_definitions[item["id"]] = _resolve_file_values(
                definition, Path(item["path"]).parent
            )
            existing = directory / "agents" / f"{item['id']}.md"
            if existing.exists():
                existing.unlink()
            continue
        target = directory / "agents" / f"{item['id']}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        text = transformed(context, Path(item["path"]).read_text(encoding="utf-8"))

        text = _relocate_file_references(text, Path(item["path"]), context.workspace)
        if context.agents_dir:
            text = text.replace(str(context.agents_dir), str(directory))
        target.write_text(text, encoding="utf-8")
    for subtree in ("resources", "prompts", "skills"):
        for resource in sorted((directory / subtree).rglob("*")):
            if resource.is_file() and resource.suffix in {
                ".md",
                ".txt",
                ".json",
                ".yaml",
                ".yml",
                ".py",
                ".sh",
                ".js",
                ".ts",
                ".toml",
            }:
                text = transformed(context, resource.read_text(encoding="utf-8"))
                if context.agents_dir:
                    text = text.replace(str(context.agents_dir), str(directory))
                resource.write_text(text, encoding="utf-8")
    if json_definitions:
        atomic_json(
            directory / "opencode.json",
            {"$schema": "https://opencode.ai/config.json", "agent": json_definitions},
        )
    return {"OPENCODE_CONFIG_DIR": str(directory)}


def preview_import(
    source: Path, library: Path, resources: dict | None = None, path_maps: dict | None = None
) -> dict:
    source, library = source.resolve(), library.resolve()
    if not source.is_dir():
        raise ValueError(f"Agent directory not found: {source}")
    resources = resources or {}
    path_maps = path_maps or {}
    files, skipped, issues = [], [], []
    discovered = sorted(source.rglob("*"))
    for path in discovered:
        if path.is_symlink():
            skipped.append({"path": str(path), "reason": "symlink is not portable"})
        elif path.is_file() and path.suffix != ".md":
            skipped.append({"path": str(path), "reason": "unsupported agent file (expected .md)"})
    for path in agent_files(source):
        rel = path.relative_to(source)
        target = library / "agents" / rel
        state = (
            "new"
            if not target.exists()
            else "identical"
            if path.read_bytes() == target.read_bytes()
            else "collision"
        )
        effective_text = path.read_text(encoding="utf-8")
        for old, new in sorted(path_maps.items(), key=lambda item: -len(item[0])):
            effective_text = effective_text.replace(old, new)
        info = metadata(path, effective_text)
        if not AGENT_ID.fullmatch(rel.with_suffix("").as_posix()):
            info["issues"].append("Invalid agent identifier")
        deps = dependencies(path.read_text(encoding="utf-8"), path)
        for dep in deps:
            dependency = Path(dep["path"])
            # Dependencies inside agents are migrated with the complete library.
            covered = (
                any(ref in dep["reference"] for ref in path_maps)
                or dependency.is_relative_to(source)
                or any(
                    dependency == Path(p).resolve() or dependency.is_relative_to(Path(p).resolve())
                    for p in resources
                )
            )
            dep["portable"] = covered
            if dep["kind"] != "permission_pattern" and not covered:
                issues.append(
                    f"{rel}: dependency requires resource mapping or project agent_paths: {dep['reference']}"
                )
            if (
                dep["kind"] != "permission_pattern"
                and not dep["exists"]
                and not any(ref in dep["reference"] for ref in path_maps)
            ):
                issues.append(f"{rel}: missing dependency: {dep['reference']}")
        issues.extend(f"{rel}: {issue}" for issue in info["issues"] if issue != "Agent is disabled")
        if state == "collision":
            issues.append(f"{rel}: destination differs; import never overwrites local edits")
        files.append(
            {
                "id": rel.with_suffix("").as_posix(),
                "source": str(path),
                "destination": str(target),
                "sha256": digest(path),
                "action": state,
                "dependencies": deps,
                "issues": info["issues"],
            }
        )
    if not files:
        issues.append("No Markdown agent definitions found")
    for item in skipped:
        issues.append(f"{item['path']}: {item['reason']}")
    resource_rows = []
    for src, dest in resources.items():
        src_path, dest_path = Path(src).resolve(), Path(dest)
        if (
            dest_path.is_absolute()
            or ".." in dest_path.parts
            or not dest_path.parts
            or dest_path.parts[0] not in {"resources", "prompts", "skills"}
        ):
            raise ValueError(
                "Resource destinations must be relative under resources/, prompts/ or skills/"
            )
        candidates = [src_path] if src_path.is_file() else sorted(src_path.rglob("*"))
        if not src_path.exists():
            issues.append(f"Resource missing: {src_path}")
        for path in candidates:
            if path.is_symlink():
                issues.append(f"Refusing linked resource: {path}")
                continue
            if path.is_dir():
                continue
            if (
                path.is_symlink()
                or SECRET_NAME.search(path.name)
                or path.name.startswith(".env")
                or path.suffix in {".pem", ".key", ".db", ".sqlite"}
                or path.name in {"id_rsa", "id_ed25519"}
                or any(
                    part in {"node_modules", ".git", "cache", "caches"}
                    for part in path.relative_to(src_path.parent).parts
                )
            ):
                issues.append(f"Refusing potentially private or linked resource: {path}")
                continue
            target = (
                library / dest_path / path.relative_to(src_path)
                if src_path.is_dir()
                else library / dest_path
            )
            state = (
                "new"
                if not target.exists()
                else "identical"
                if target.read_bytes() == path.read_bytes()
                else "collision"
            )
            if state == "collision":
                issues.append(f"Resource destination collision: {target}")
            if path.suffix in {".md", ".txt"}:
                for ref in dependencies(path.read_text(encoding="utf-8"), path):
                    dep_path = Path(ref["path"])
                    covered = (
                        dep_path.is_relative_to(source)
                        or any(
                            dep_path == Path(p).resolve()
                            or dep_path.is_relative_to(Path(p).resolve())
                            for p in resources
                        )
                        or any(old in ref["reference"] for old in path_maps)
                    )
                    if ref["kind"] != "permission_pattern" and (not ref["exists"] or not covered):
                        issues.append(
                            f"Resource {path}: unresolved nested dependency: {ref['reference']}"
                        )
            resource_rows.append(
                {
                    "source": str(path),
                    "destination": str(target),
                    "sha256": digest(path),
                    "action": state,
                }
            )
    return {
        "source": str(source),
        "library": str(library),
        "agents": files,
        "resources": resource_rows,
        "skipped": skipped,
        "issues": sorted(set(issues)),
        "ready": not issues,
        "path_maps": path_maps,
    }


def import_library(
    source: Path,
    library: Path,
    resources: dict | None = None,
    apply: bool = False,
    path_maps: dict | None = None,
) -> dict:
    preview = preview_import(source, library, resources, path_maps)
    if not apply:
        return preview
    if not preview["ready"]:
        raise ValueError("Import blocked; resolve preview issues before --apply")
    with file_transaction(library.parent / "library-import"):
        preview = preview_import(source, library, resources, path_maps)
        if not preview["ready"]:
            raise ValueError("Import changed during preview; retry")
        library.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".library-stage-", dir=library.parent))
        backup = None
        try:
            if library.exists():
                shutil.copytree(library, stage, dirs_exist_ok=True)
            # Copy each listed file, then verify the staged bytes before cutover.
            for row in preview["agents"] + preview["resources"]:
                target = stage / Path(row["destination"]).relative_to(library)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(row["source"], target)
                if digest(target) != row["sha256"]:
                    raise ValueError(f"Source changed while staging: {row['source']}")
            # Explicit resource mappings relocate references only in execution snapshots.
            mappings = {}
            if (stage / "paths.json").exists():
                mappings.update(json.loads((stage / "paths.json").read_text()))
            for src, dest in (resources or {}).items():
                mappings[str(Path(src).resolve())] = str(library / dest)
            for row in preview["agents"] + preview["resources"]:
                deps = row.get("dependencies")
                if deps is None and Path(row["source"]).suffix in {".md", ".txt"}:
                    deps = dependencies(
                        Path(row["source"]).read_text(encoding="utf-8"), Path(row["source"])
                    )
                for dep in deps or []:
                    ref_path = Path(dep["path"])
                    if ref_path.is_relative_to(source.resolve()):
                        mappings[dep["reference"]] = str(
                            library / "agents" / ref_path.relative_to(source.resolve())
                        )
                    for src, dest in (resources or {}).items():
                        source_path = Path(src).resolve()
                        if ref_path == source_path or ref_path.is_relative_to(source_path):
                            mappings[dep["reference"]] = str(
                                library / dest / ref_path.relative_to(source_path)
                            )
            mappings.update(path_maps or {})
            atomic_json(stage / "paths.json", mappings)
            atomic_json(stage / "import-manifest.json", preview)
            if library.exists():
                backup = library.with_name(library.name + ".backup-" + uuid.uuid4().hex)
                library.rename(backup)
            try:
                stage.rename(library)
            except BaseException:
                if backup:
                    backup.rename(library)
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {**preview, "applied": True, "backup": str(backup) if backup else None}


def jsonc_agents(path: Path) -> dict:
    """Read just agent declarations; leave OpenCode/MCP configuration untouched."""
    text = path.read_text(encoding="utf-8")
    # Preserve quoted strings while removing JSONC comments and trailing commas.
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')
    text = tokens.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    text = re.sub(
        r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])',
        lambda m: m.group(0) if m.group(0).startswith('"') else "",
        text,
    )
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError(f"OpenCode configuration must be an object: {path}")
    result = value.get("agent", {})
    if not isinstance(result, dict):
        raise ValueError(f"Invalid agent configuration: {path}")
    return result
