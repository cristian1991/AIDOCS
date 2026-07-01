from __future__ import annotations

import re
from pathlib import Path

from ._sqlite_index_store_base import SQLiteIndexStoreBase


class SchemaIndexStore(SQLiteIndexStoreBase):
    """Derived schema/catalog index for DB tables and C# data structures."""

    def init_db(self, project_root: Path) -> None:
        with self.connect(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_entities (
                    entity_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    container TEXT,
                    line_number INTEGER,
                    PRIMARY KEY (entity_name, kind, source_type, path)
                );

                CREATE TABLE IF NOT EXISTS schema_fields (
                    entity_name TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    field_type TEXT,
                    field_kind TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    line_number INTEGER,
                    PRIMARY KEY (entity_name, field_name, field_kind, source_type, path)
                );

                CREATE TABLE IF NOT EXISTS schema_relationships (
                    source_entity TEXT NOT NULL,
                    target_entity TEXT NOT NULL,
                    relation_kind TEXT NOT NULL,
                    relationship_family TEXT NOT NULL DEFAULT 'structural',
                    source_path TEXT NOT NULL,
                    line_number INTEGER,
                    PRIMARY KEY (source_entity, target_entity, relation_kind, source_path)
                );
                """,
            )
            self._ensure_column(
                conn,
                "schema_relationships",
                "relationship_family",
                "TEXT NOT NULL DEFAULT 'structural'",
            )

    def sync_schema(self, project_root: Path) -> dict[str, int]:
        self.init_db(project_root)
        entity_rows: list[tuple[str, str, str, str, str | None, int | None]] = []
        field_rows: list[tuple[str, str, str | None, str, str, str, int | None]] = []
        relationship_rows: list[tuple[str, str, str, str, int | None]] = []
        ef_table_specs: list[tuple[str, str | None, str, int | None]] = []
        ef_dbset_specs: list[tuple[str, str, str, int | None]] = []
        ef_field_specs: list[tuple[str, str, str | None, str, str, int | None]] = []
        ef_relationship_specs: list[tuple[str, str, str, str, int | None]] = []

        for path in sorted(project_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(project_root).as_posix()
            if self._should_skip(rel):
                continue

            suffix = path.suffix.lower()
            if suffix == ".cs":
                entities, fields, nav_rels = self._extract_csharp_schema(path, rel)
                entity_rows.extend(entities)
                field_rows.extend(fields)
                relationship_rows.extend(nav_rels)
                ef_tables, ef_dbsets, ef_fields, ef_relationships = self._extract_ef_core_schema(
                    path,
                    rel,
                )
                ef_table_specs.extend(ef_tables)
                ef_dbset_specs.extend(ef_dbsets)
                ef_field_specs.extend(ef_fields)
                ef_relationship_specs.extend(ef_relationships)
                di_rels = self._extract_di_registrations(path, rel)
                relationship_rows.extend(di_rels)
                impl_rels = self._extract_interface_implementations(path, rel)
                relationship_rows.extend(impl_rels)
            elif suffix == ".prisma":
                entities, fields, relationships = self._extract_prisma_schema(path, rel)
                entity_rows.extend(entities)
                field_rows.extend(fields)
                relationship_rows.extend(relationships)
            elif suffix == ".sql":
                entities, fields, relationships = self._extract_sql_schema(path, rel)
                entity_rows.extend(entities)
                field_rows.extend(fields)
                relationship_rows.extend(relationships)

        entity_type_to_table: dict[str, str] = {}
        configured_entity_types = {
            entity_type for entity_type, table_name, _, _ in ef_table_specs if table_name
        }
        for entity_type, table_name, _, _ in ef_dbset_specs:
            entity_type_to_table.setdefault(entity_type, table_name)
        for entity_type, table_name, _, _ in ef_table_specs:
            if table_name:
                entity_type_to_table[entity_type] = table_name

        for entity_type, table_name, rel, line_number in ef_table_specs:
            resolved_table = table_name or entity_type_to_table.get(entity_type) or entity_type
            entity_rows.append((resolved_table, "table", "ef_table", rel, entity_type, line_number))
        for entity_type, table_name, rel, line_number in ef_dbset_specs:
            if entity_type in configured_entity_types:
                continue
            resolved_table = entity_type_to_table.get(entity_type, table_name)
            entity_rows.append((resolved_table, "table", "ef_table", rel, entity_type, line_number))
        for (
            entity_type,
            field_name,
            field_type,
            rel,
            source_type,
            line_number,
        ) in ef_field_specs:
            resolved_table = entity_type_to_table.get(entity_type, entity_type)
            field_rows.append(
                (
                    resolved_table,
                    field_name,
                    field_type,
                    "column",
                    source_type,
                    rel,
                    line_number,
                ),
            )
        for (
            source_entity_type,
            target_entity_type,
            relation_kind,
            rel,
            line_number,
        ) in ef_relationship_specs:
            source_table = entity_type_to_table.get(source_entity_type, source_entity_type)
            target_table = entity_type_to_table.get(target_entity_type, target_entity_type)
            relationship_rows.append((source_table, target_table, relation_kind, rel, line_number))

        entity_rows = list({(row[0], row[1], row[2], row[3]): row for row in entity_rows}.values())
        field_rows = list(
            {(row[0], row[1], row[3], row[4], row[5]): row for row in field_rows}.values(),
        )
        relationship_rows = list(
            {(row[0], row[1], row[2], row[3]): row for row in relationship_rows}.values(),
        )
        relationship_rows_with_family = [
            (
                source_entity,
                target_entity,
                relation_kind,
                self._classify_relationship_family(relation_kind),
                source_path,
                line_number,
            )
            for source_entity, target_entity, relation_kind, source_path, line_number in relationship_rows
        ]

        with self.connect(project_root) as conn:
            conn.execute("DELETE FROM schema_entities")
            conn.execute("DELETE FROM schema_fields")
            conn.execute("DELETE FROM schema_relationships")
            conn.executemany(
                "INSERT INTO schema_entities (entity_name, kind, source_type, path, container, line_number) VALUES (?, ?, ?, ?, ?, ?)",
                entity_rows,
            )
            conn.executemany(
                "INSERT INTO schema_fields (entity_name, field_name, field_type, field_kind, source_type, path, line_number) VALUES (?, ?, ?, ?, ?, ?, ?)",
                field_rows,
            )
            conn.executemany(
                "INSERT INTO schema_relationships (source_entity, target_entity, relation_kind, relationship_family, source_path, line_number) VALUES (?, ?, ?, ?, ?, ?)",
                relationship_rows_with_family,
            )

        return {
            "entities": len(entity_rows),
            "fields": len(field_rows),
            "relationships": len(relationship_rows),
        }

    def schema_status(self, project_root: Path) -> dict[str, int | str]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            entity_count = conn.execute("SELECT COUNT(*) FROM schema_entities").fetchone()[0]
            field_count = conn.execute("SELECT COUNT(*) FROM schema_fields").fetchone()[0]
            relationship_count = conn.execute(
                "SELECT COUNT(*) FROM schema_relationships",
            ).fetchone()[0]
            source_rows = conn.execute(
                "SELECT source_type, COUNT(*) AS count FROM schema_entities GROUP BY source_type ORDER BY count DESC, source_type ASC",
            ).fetchall()
            kind_rows = conn.execute(
                "SELECT kind, COUNT(*) AS count FROM schema_entities GROUP BY kind ORDER BY count DESC, kind ASC",
            ).fetchall()
            relationship_family_rows = conn.execute(
                "SELECT relationship_family, COUNT(*) AS count FROM schema_relationships GROUP BY relationship_family ORDER BY count DESC, relationship_family ASC",
            ).fetchall()

        by_source_type = {row["source_type"]: int(row["count"]) for row in source_rows}
        by_kind = {row["kind"]: int(row["count"]) for row in kind_rows}
        by_relationship_family = {
            row["relationship_family"]: int(row["count"]) for row in relationship_family_rows
        }
        categories: dict[str, dict[str, object]] = {}
        for source_type, count in by_source_type.items():
            category = self._schema_category_for_source_type(source_type)
            bucket = categories.setdefault(category, {"entities": 0, "source_types": {}})
            bucket["entities"] = int(bucket["entities"]) + count
            bucket_source_types = dict(bucket["source_types"])
            bucket_source_types[source_type] = count
            bucket["source_types"] = bucket_source_types
        return {
            "db_path": str(self.db_path(project_root)),
            "schema_entities": int(entity_count),
            "schema_fields": int(field_count),
            "schema_relationships": int(relationship_count),
            "entity_breakdown": {
                "by_source_type": by_source_type,
                "by_kind": by_kind,
                "categories": categories,
            },
            "relationship_breakdown": {
                "by_family": by_relationship_family,
            },
        }

    def find_schema_entities(
        self,
        project_root: Path,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        self.init_db(project_root)
        sql = "SELECT entity_name, kind, source_type, path, container, line_number FROM schema_entities"
        params: list[object] = []
        if query and query.strip():
            sql += " WHERE entity_name LIKE ? OR path LIKE ?"
            needle = f"%{query.strip()}%"
            params.extend([needle, needle])
        sql += " ORDER BY entity_name ASC, path ASC LIMIT ?"
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._compact_entity(dict(row)) for row in rows]

    def get_schema_entity(self, project_root: Path, entity_name: str) -> dict[str, object]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            entities = conn.execute(
                "SELECT entity_name, kind, source_type, path, container, line_number FROM schema_entities WHERE entity_name = ? ORDER BY path",
                (entity_name,),
            ).fetchall()
            fields = conn.execute(
                "SELECT entity_name, field_name, field_type, field_kind, source_type, path, line_number FROM schema_fields WHERE entity_name = ? ORDER BY path, line_number, field_name",
                (entity_name,),
            ).fetchall()
        enriched_fields = [
            self._compact_field(self._enrich_field_metadata(dict(row))) for row in fields
        ]
        # Deduplicate: if all fields share the same path/source_type, hoist to top level
        field_paths = {f.get("path") for f in enriched_fields if f.get("path")}
        if len(field_paths) == 1:
            common_path = field_paths.pop()
            for f in enriched_fields:
                f.pop("path", None)
                f.pop("source_type", None)
            return {
                "entity_name": entity_name,
                "source": "schema_index",
                "path": common_path,
                "entities": [self._compact_entity(dict(row)) for row in entities],
                "field_count": len(enriched_fields),
                "fields": enriched_fields,
            }
        return {
            "entity_name": entity_name,
            "source": "schema_index",
            "entities": [self._compact_entity(dict(row)) for row in entities],
            "field_count": len(enriched_fields),
            "fields": enriched_fields,
        }

    # Rename DB columns to match code_* tool vocabulary in output
    _OUTPUT_KEY_RENAMES: dict[str, str] = {
        "source_type": "source",
        "field_kind": "kind",
    }

    @classmethod
    def _compact_field(
        cls,
        field: dict[str, object],
        strip_entity_name: bool = True,
    ) -> dict[str, object]:
        """Remove redundant/falsy keys and normalize vocabulary to match code_* tools."""
        result: dict[str, object] = {}
        for k, v in field.items():
            if k == "entity_name" and strip_entity_name:
                continue
            if k in ("required", "optional", "defaulted", "computed") and not v:
                continue
            if v is None or v == "":
                continue
            out_key = cls._OUTPUT_KEY_RENAMES.get(k, k)
            result[out_key] = v
        return result

    @classmethod
    def _compact_entity(cls, entity: dict[str, object]) -> dict[str, object]:
        """Remove redundant/empty keys and normalize vocabulary."""
        return {
            cls._OUTPUT_KEY_RENAMES.get(k, k): v
            for k, v in entity.items()
            if v is not None and v != ""
        }

    def find_schema_field(
        self,
        project_root: Path,
        field_name: str,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT entity_name, field_name, field_type, field_kind, source_type, path, line_number FROM schema_fields WHERE field_name LIKE ? ORDER BY entity_name, path LIMIT ?",
                (f"%{field_name.strip()}%", limit),
            ).fetchall()
        return [
            self._compact_field(self._enrich_field_metadata(dict(row)), strip_entity_name=False)
            for row in rows
        ]

    def get_constructor_params(
        self,
        project_root: Path,
        entity_name: str,
        include_related: bool = False,
    ) -> dict[str, object]:
        try:
            from .code_index_store import CodeIndexStore

            code = CodeIndexStore()
            return code.get_constructor_params(
                project_root,
                entity_name,
                include_related=include_related,
            )
        except Exception:
            return {"type": entity_name, "matches": []}

    def get_entity_properties(self, project_root: Path, entity_name: str) -> dict[str, object]:
        result = self.get_schema_entity(project_root, entity_name)
        properties = [
            {
                k: v
                for k, v in {
                    "field_name": field.get("field_name"),
                    "field_type": field.get("field_type"),
                    **({"required": True} if field.get("required") else {}),
                    **({"optional": True} if field.get("optional") else {}),
                    **({"defaulted": True} if field.get("defaulted") else {}),
                    **({"computed": True} if field.get("computed") else {}),
                }.items()
                if v is not None
            }
            for field in result.get("fields", [])
        ]
        return {
            "entity_name": entity_name,
            "properties": properties,
        }

    def get_schema_entities_batch(
        self,
        project_root: Path,
        entity_names: list[str],
    ) -> dict[str, object]:
        items = []
        for name in entity_names:
            entity_name = name.strip()
            if not entity_name:
                continue
            items.append(self.get_schema_entity(project_root, entity_name))
        return {
            "entities": items,
        }

    def trace_entity_flow(
        self,
        project_root: Path,
        entity_name: str,
        limit: int = 50,
    ) -> dict[str, object]:
        self.init_db(project_root)
        entity = entity_name.strip()
        if not entity:
            return {
                "entity": entity_name,
                "entities": [],
                "fields": [],
                "code_matches": [],
            }

        entities = self.find_schema_entities(project_root, query=entity, limit=limit)
        fields = self.get_schema_entity(project_root, entity).get("fields", [])

        code_matches = []
        try:
            from .code_index_store import CodeIndexStore

            code = CodeIndexStore()
            symbol_bundle = code.get_symbol_bundle(project_root, symbol=entity, limit=limit)
            code_matches.extend(
                {
                    "source": "definition",
                    "path": item["path"],
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item["container"],
                }
                for item in symbol_bundle.get("definitions", [])
            )
            code_matches.extend(
                {
                    "source": "reference",
                    "path": item["path"],
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item["container"],
                    "layer": item["layer"],
                }
                for item in symbol_bundle.get("references", [])[:limit]
            )
        except Exception:
            pass

        return {
            "entity": entity_name,
            "entities": entities,
            "fields": fields,
            "code_matches": code_matches,
        }

    def _enrich_field_metadata(self, field: dict[str, object]) -> dict[str, object]:
        field_name = str(field.get("field_name") or "")
        field_kind = str(field.get("field_kind") or "")
        field_type = str(field.get("field_type") or "")

        field["required"] = field_name.lower() == "id" or field_kind in {
            "relation",
            "enum_field",
        }
        field["optional"] = "?" in field_type or field_type.endswith("[]")
        field["defaulted"] = field_name.lower() in {
            "id",
            "createdat",
            "updatedat",
            "status",
        }
        field["computed"] = field_name.lower() in {
            "linetotal",
            "total",
            "subtotal",
            "amountdue",
        }

        # value_type: helps agents distinguish stored data from derived/inferred values
        if field["computed"]:
            field["value_type"] = "computed"
        elif field["defaulted"]:
            field["value_type"] = "defaulted"
        elif field_kind in {"relation", "nav_property", "nav_collection"}:
            field["value_type"] = "navigation"
        elif field_kind in {"enum_field"}:
            field["value_type"] = "enum"
        else:
            field["value_type"] = "stored"

        return field

    def trace_relationship_path(
        self,
        project_root: Path,
        source_entity: str,
        target_entity: str,
        limit: int = 20,
    ) -> dict[str, object]:
        self.init_db(project_root)
        source = source_entity.strip()
        target = target_entity.strip()
        if not source or not target:
            return {
                "source_entity": source_entity,
                "target_entity": target_entity,
                "paths": [],
            }

        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT source_entity, target_entity, relation_kind, relationship_family, source_path, line_number FROM schema_relationships",
            ).fetchall()

        # Build case-insensitive entity name lookup
        entity_names: dict[str, str] = {}  # lowercase -> canonical
        for row in rows:
            for name in (row["source_entity"], row["target_entity"]):
                lower = name.lower()
                if lower not in entity_names:
                    entity_names[lower] = name

        # Resolve source/target to canonical names (case-insensitive + pluralization)
        def resolve_name(name: str) -> str:
            lower = name.lower()
            if lower in entity_names:
                return entity_names[lower]
            # Try singular/plural
            if lower.endswith("s") and lower[:-1] in entity_names:
                return entity_names[lower[:-1]]
            if lower + "s" in entity_names:
                return entity_names[lower + "s"]
            return name

        source = resolve_name(source)
        target = resolve_name(target)

        graph: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            graph.setdefault(row["source_entity"], []).append(dict(row))
        for edges in graph.values():
            edges.sort(key=lambda item: self._relationship_priority(item["relation_kind"]))

        paths: list[list[dict[str, object]]] = []
        queue: list[tuple[str, list[dict[str, object]]]] = [(source, [])]
        seen = {(source, ())}

        while queue and len(paths) < limit:
            current, path = queue.pop(0)
            if current == target and path:
                paths.append(path)
                continue
            for edge in graph.get(current, []):
                next_node = edge["target_entity"]
                # Skip self-referential edges (e.g., Document -> Document)
                if next_node == current:
                    continue
                candidate = path + [edge]
                key = (
                    next_node,
                    tuple((item["source_entity"], item["target_entity"]) for item in candidate),
                )
                if key in seen:
                    continue
                seen.add(key)
                if len(candidate) <= 6:
                    queue.append((next_node, candidate))

        return {
            "source_entity": source_entity,
            "target_entity": target_entity,
            "paths": paths,
        }

    def _should_skip(self, rel: str) -> bool:
        prefixes = (
            ".git/",
            "core/",
            "mcp/",
            ".MEMORY/",
        )
        if rel.startswith(prefixes):
            return True
        parts = rel.lower().split("/")
        return any(
            segment in parts
            for segment in (
                "node_modules",
                "dist",
                "coverage",
                "__pycache__",
                ".venv",
                "venv",
                ".backup",
                ".backups",
                "temp",
            )
        )

    def _extract_csharp_schema(self, path: Path, rel: str):
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        namespace_name = None
        entity_rows = []
        field_rows = []

        namespace_pattern = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_\.]*)")
        type_pattern = re.compile(
            r"^\s*(?:public|private|internal|protected|sealed|abstract|static|unsafe|new|file|readonly|partial|\s)*\b(partial\s+)?(class|interface|struct|record|enum)\s+([A-Za-z_][A-Za-z0-9_]*)",
        )
        property_pattern = re.compile(
            r"^\s*(?:public|private|internal|protected|static|virtual|override|abstract|sealed|required|init|readonly|unsafe|new|\s)+([A-Za-z_<>,\[\]\.?]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{\s*(?:get;|set;|init;)",
        )
        field_pattern = re.compile(
            r"^\s*(?:public|private|internal|protected|static|readonly|const|volatile|unsafe|new|\s)+([A-Za-z_<>,\[\]\.?]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|;)",
        )

        nav_relationships: list[tuple[str, str, str, str, int]] = []
        current_entity = None
        current_kind = None
        brace_depth = 0
        entity_depth = None
        inside_enum = False

        for line_number, line in enumerate(lines, start=1):
            opens = line.count("{")
            closes = line.count("}")

            ns = namespace_pattern.match(line)
            if ns:
                namespace_name = ns.group(1)

            tm = type_pattern.match(line)
            if tm:
                kind = tm.group(2)
                entity = tm.group(3)
                current_entity = entity
                current_kind = kind
                entity_depth = brace_depth + 1
                inside_enum = kind == "enum"
                source_type = self._classify_csharp_entity(rel, entity, kind)
                entity_rows.append((entity, kind, source_type, rel, namespace_name, line_number))

            if current_entity is not None:
                pm = property_pattern.match(line)
                if pm and current_kind != "enum":
                    prop_type = pm.group(1)
                    prop_name = pm.group(2)
                    field_rows.append(
                        (
                            current_entity,
                            prop_name,
                            prop_type,
                            "property",
                            "csharp",
                            rel,
                            line_number,
                        ),
                    )
                    # Detect navigation properties (virtual + entity type)
                    if "virtual" in line:
                        nav_rel = self._detect_nav_relationship(
                            prop_type,
                            prop_name,
                            current_entity,
                        )
                        if nav_rel:
                            nav_relationships.append((*nav_rel, rel, line_number))
                    # Convention-based FK: PropertyNameId → PropertyName entity
                    elif (
                        prop_name.endswith("Id")
                        and len(prop_name) > 2
                        and prop_type.rstrip("?")
                        in ("Guid", "int", "long", "Guid?", "int?", "long?")
                    ):
                        target = prop_name[:-2]  # Strip "Id" suffix
                        if target[:1].isupper():
                            nav_relationships.append(
                                (
                                    current_entity,
                                    target,
                                    "fk_convention",
                                    rel,
                                    line_number,
                                ),
                            )

                fm = field_pattern.match(line)
                if fm and current_kind != "enum":
                    field_rows.append(
                        (
                            current_entity,
                            fm.group(2),
                            fm.group(1),
                            "field",
                            "csharp",
                            rel,
                            line_number,
                        ),
                    )

                if inside_enum:
                    em = re.match(
                        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*([^,\s]+))?\s*,?\s*$",
                        line,
                    )
                    if em:
                        symbol = em.group(1)
                        if symbol not in {"public", "private", "internal", "protected"}:
                            enum_val = em.group(2)  # e.g., "0", "1", etc.
                            field_rows.append(
                                (
                                    current_entity,
                                    symbol,
                                    enum_val,
                                    "enum_member",
                                    "csharp",
                                    rel,
                                    line_number,
                                ),
                            )

            brace_depth += opens
            brace_depth -= closes
            if entity_depth is not None and brace_depth < entity_depth - 1:
                current_entity = None
                current_kind = None
                entity_depth = None
                inside_enum = False

        return entity_rows, field_rows, nav_relationships

    def _detect_nav_relationship(
        self,
        prop_type: str,
        prop_name: str,
        current_entity: str,
    ) -> tuple[str, str, str] | None:
        """Detect navigation property relationships from type signatures."""
        clean = prop_type.rstrip("?").strip()
        # ICollection<T>, IList<T>, List<T>, IEnumerable<T>, HashSet<T>
        collection_match = re.match(
            r"(?:I?(?:Collection|List|Enumerable|Set)|List|HashSet)<([A-Za-z_][A-Za-z0-9_]*)>",
            clean,
        )
        if collection_match:
            target = collection_match.group(1)
            return (current_entity, target, "nav_collection")
        # Simple navigation property (not a primitive type)
        if clean[:1].isupper() and not clean.startswith(
            (
                "Guid",
                "DateTime",
                "String",
                "Decimal",
                "Int",
                "Boolean",
                "Byte",
                "Double",
                "Float",
                "Long",
                "Short",
                "Nullable",
            ),
        ):
            # Likely a reference navigation
            return (current_entity, clean, "nav_reference")
        return None

    def _extract_di_registrations(
        self,
        path: Path,
        rel: str,
    ) -> list[tuple[str, str, str, str, int | None]]:
        """Extract DI service registrations: AddScoped<IFoo, Foo>(), AddTransient<>(), AddSingleton<>()."""
        relationships: list[tuple[str, str, str, str, int | None]] = []
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Match: Add{Scoped|Transient|Singleton}<IService, Implementation>()
        di_pattern = re.compile(
            r"\.Add(Scoped|Transient|Singleton)<([A-Za-z_][A-Za-z0-9_]*),\s*([A-Za-z_][A-Za-z0-9_]*)>",
        )
        # Match: Add{Scoped|Transient|Singleton}<ConcreteOnly>()
        di_single_pattern = re.compile(
            r"\.Add(Scoped|Transient|Singleton)<([A-Za-z_][A-Za-z0-9_]*)>\s*\(",
        )
        for line_number, line in enumerate(text.splitlines(), start=1):
            # Collect spans matched by two-type pattern to avoid double-counting
            two_type_spans: set[tuple[int, int]] = set()
            for m in di_pattern.finditer(line):
                lifetime = m.group(1).lower()
                interface = m.group(2)
                implementation = m.group(3)
                relationships.append(
                    (interface, implementation, f"di_{lifetime}", rel, line_number),
                )
                two_type_spans.add((m.start(), m.end()))
            for m in di_single_pattern.finditer(line):
                # Skip if this match overlaps with a two-type match
                if any(m.start() >= s and m.start() < e for s, e in two_type_spans):
                    continue
                lifetime = m.group(1).lower()
                concrete = m.group(2)
                relationships.append((concrete, concrete, f"di_{lifetime}", rel, line_number))
        return relationships

    def _extract_interface_implementations(
        self,
        path: Path,
        rel: str,
    ) -> list[tuple[str, str, str, str, int | None]]:
        """Extract interface implementation relationships from class declarations."""
        relationships: list[tuple[str, str, str, str, int | None]] = []
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Match: class Foo : IBar, IBaz  or  class Foo : BaseClass, IBar
        class_pattern = re.compile(
            r"^\s*(?:public|private|internal|protected|sealed|abstract|static|partial|\s)*\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>]*>)?\s*:\s*(.+)",
        )
        for line_number, line in enumerate(text.splitlines(), start=1):
            m = class_pattern.match(line)
            if m:
                class_name = m.group(1)
                bases = m.group(2).split("{")[0]  # trim any opening brace
                for base in bases.split(","):
                    base = base.strip().split("<")[0].strip()  # remove generics
                    if base.startswith("I") and len(base) > 1 and base[1:2].isupper():
                        relationships.append((base, class_name, "implements", rel, line_number))
        return relationships

    def _extract_ef_core_schema(self, path: Path, rel: str):
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        ef_tables: list[tuple[str, str | None, str, int | None]] = []
        ef_dbsets: list[tuple[str, str, str, int | None]] = []
        ef_fields: list[tuple[str, str, str | None, str, str, int | None]] = []
        ef_relationships: list[tuple[str, str, str, str, int | None]] = []

        dbset_pattern = re.compile(r"DbSet<([A-Za-z_][A-Za-z0-9_]*)>\s+([A-Za-z_][A-Za-z0-9_]*)")
        config_pattern = re.compile(
            r"class\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*IEntityTypeConfiguration<([A-Za-z_][A-Za-z0-9_]*)>",
        )
        property_pattern = re.compile(
            r"\bProperty\(\s*\w+\s*=>\s*\w+\.([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        )
        table_pattern = re.compile(r'\bToTable\(\s*"([^"]+)"')
        has_one_pattern = re.compile(
            r"HasOne(?:<(?P<generic>[A-Za-z_][A-Za-z0-9_]*)>)?\(\s*(?:(?P<param>\w+)\s*=>\s*(?P=param)\.(?P<nav>[A-Za-z_][A-Za-z0-9_]*))?",
        )
        has_many_pattern = re.compile(
            r"HasMany(?:<(?P<generic>[A-Za-z_][A-Za-z0-9_]*)>)?\(\s*(?:(?P<param>\w+)\s*=>\s*(?P=param)\.(?P<nav>[A-Za-z_][A-Za-z0-9_]*))?",
        )
        with_one_pattern = re.compile(
            r"WithOne\(\s*(?:(?P<param>\w+)\s*=>\s*(?P=param)\.(?P<nav>[A-Za-z_][A-Za-z0-9_]*))?",
        )
        with_many_pattern = re.compile(
            r"WithMany\(\s*(?:(?P<param>\w+)\s*=>\s*(?P=param)\.(?P<nav>[A-Za-z_][A-Za-z0-9_]*))?",
        )
        fk_pattern = re.compile(
            r"HasForeignKey(?:<(?P<fk_entity>[A-Za-z_][A-Za-z0-9_]*)>)?\(\s*\w+\s*=>\s*\w+\.([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        )
        owns_one_pattern = re.compile(
            r"OwnsOne(?:<(?P<generic>[A-Za-z_][A-Za-z0-9_]*)>)?\(\s*(?:(?P<param>\w+)\s*=>\s*(?P=param)\.(?P<nav>[A-Za-z_][A-Za-z0-9_]*))?",
        )
        owns_many_pattern = re.compile(
            r"OwnsMany(?:<(?P<generic>[A-Za-z_][A-Za-z0-9_]*)>)?\(\s*(?:(?P<param>\w+)\s*=>\s*(?P=param)\.(?P<nav>[A-Za-z_][A-Za-z0-9_]*))?",
        )
        has_query_filter = re.compile(r"\.HasQueryFilter\(")
        ignore_pattern = re.compile(r"\.Ignore\(\s*\w+\s*=>\s*\w+\.([A-Za-z_][A-Za-z0-9_]*)\s*\)")

        brace_depth = 0
        current_entity_type: str | None = None
        current_start_line: int | None = None
        entity_depth: int | None = None
        current_table_name: str | None = None
        pending_target: str | None = None
        pending_relation_kind: str | None = None

        for line_number, line in enumerate(lines, start=1):
            for match in dbset_pattern.finditer(line):
                ef_dbsets.append((match.group(1), match.group(2), rel, line_number))

            config_match = config_pattern.search(line)
            if config_match and current_entity_type is None:
                current_entity_type = config_match.group(1)
                current_start_line = line_number
                entity_depth = brace_depth + line.count("{")
                current_table_name = None
                pending_target = None
                pending_relation_kind = None

            if current_entity_type is not None:
                table_match = table_pattern.search(line)
                if table_match:
                    current_table_name = table_match.group(1)

                for property_match in property_pattern.finditer(line):
                    ef_fields.append(
                        (
                            current_entity_type,
                            property_match.group(1),
                            None,
                            rel,
                            "ef",
                            line_number,
                        ),
                    )

                # HasOne → target entity (one-to-one or many-to-one)
                has_one_match = has_one_pattern.search(line)
                if has_one_match:
                    pending_target = has_one_match.group("generic") or has_one_match.group("nav")
                    pending_relation_kind = "ef_has_one"

                # HasMany → target entity (one-to-many)
                has_many_match = has_many_pattern.search(line)
                if has_many_match:
                    target = has_many_match.group("generic") or has_many_match.group("nav")
                    if target:
                        target = self._capitalize_identifier(target.rstrip("?"))
                        ef_relationships.append(
                            (
                                current_entity_type,
                                target,
                                "ef_has_many",
                                rel,
                                line_number,
                            ),
                        )
                    pending_target = target
                    pending_relation_kind = "ef_has_many"

                # WithOne / WithMany — refine the relationship direction
                with_one_match = with_one_pattern.search(line)
                if with_one_match and pending_target:
                    nav = with_one_match.group("nav")
                    if nav and pending_relation_kind == "ef_has_many":
                        # HasMany(...).WithOne(...) — standard one-to-many
                        pass  # already recorded above

                with_many_match = with_many_pattern.search(line)
                if with_many_match and pending_target and pending_relation_kind == "ef_has_one":
                    # HasOne(...).WithMany(...) — many-to-one from current to target
                    pass  # FK will capture this below, or we record it now

                # OwnsOne / OwnsMany
                owns_one_match = owns_one_pattern.search(line)
                if owns_one_match:
                    target = owns_one_match.group("generic") or owns_one_match.group("nav")
                    if target:
                        target = self._capitalize_identifier(target.rstrip("?"))
                        ef_relationships.append(
                            (
                                current_entity_type,
                                target,
                                "ef_owns_one",
                                rel,
                                line_number,
                            ),
                        )

                owns_many_match = owns_many_pattern.search(line)
                if owns_many_match:
                    target = owns_many_match.group("generic") or owns_many_match.group("nav")
                    if target:
                        target = self._capitalize_identifier(target.rstrip("?"))
                        ef_relationships.append(
                            (
                                current_entity_type,
                                target,
                                "ef_owns_many",
                                rel,
                                line_number,
                            ),
                        )

                # HasForeignKey — captures FK field and resolves pending relationship
                fk_match = fk_pattern.search(line)
                if fk_match:
                    fk_name = fk_match.group(2)
                    target = pending_target or fk_name[:-2]
                    target = self._capitalize_identifier(target.rstrip("?"))
                    ef_relationships.append(
                        (
                            current_entity_type,
                            target,
                            "ef_foreign_key",
                            rel,
                            line_number,
                        ),
                    )
                    ef_fields.append((current_entity_type, fk_name, None, rel, "ef", line_number))
                    pending_target = None
                    pending_relation_kind = None

                # HasQueryFilter — note as metadata
                if has_query_filter.search(line):
                    ef_fields.append(
                        (
                            current_entity_type,
                            "__query_filter__",
                            None,
                            rel,
                            "ef",
                            line_number,
                        ),
                    )

                # Ignore — unmapped property
                ignore_match = ignore_pattern.search(line)
                if ignore_match:
                    ef_fields.append(
                        (
                            current_entity_type,
                            ignore_match.group(1),
                            "__ignored__",
                            rel,
                            "ef",
                            line_number,
                        ),
                    )

            brace_depth += line.count("{")
            brace_depth -= line.count("}")

            if (
                current_entity_type is not None
                and entity_depth is not None
                and brace_depth < entity_depth
            ):
                # If there was a pending HasOne without FK, still record the relationship
                if pending_target and pending_relation_kind == "ef_has_one":
                    target = self._capitalize_identifier(pending_target.rstrip("?"))
                    ef_relationships.append(
                        (
                            current_entity_type,
                            target,
                            "ef_has_one",
                            rel,
                            current_start_line or line_number,
                        ),
                    )
                ef_tables.append((current_entity_type, current_table_name, rel, current_start_line))
                current_entity_type = None
                current_start_line = None
                entity_depth = None
                current_table_name = None
                pending_target = None
                pending_relation_kind = None

        return ef_tables, ef_dbsets, ef_fields, ef_relationships

    def _extract_sql_schema(self, path: Path, rel: str):
        text = path.read_text(encoding="utf-8", errors="ignore")
        entity_rows = []
        field_rows = []
        relationship_rows = []
        for statement, start_offset in self._split_sql_statements(text):
            stripped = statement.strip()
            if not stripped:
                continue

            enum_match = re.match(
                r'^CREATE\s+TYPE\s+"?([A-Za-z_][A-Za-z0-9_]*)"?\s+AS\s+ENUM\s*\((.*)\)$',
                stripped,
                re.IGNORECASE | re.DOTALL,
            )
            if enum_match:
                enum_name = enum_match.group(1)
                entity_rows.append(
                    (
                        enum_name,
                        "enum",
                        "sql_enum",
                        rel,
                        None,
                        self._line_number(text, start_offset),
                    ),
                )
                for value in re.findall(r"'([^']+)'", enum_match.group(2)):
                    field_rows.append(
                        (
                            enum_name,
                            value,
                            None,
                            "enum_member",
                            "sql_enum",
                            rel,
                            self._line_number(text, start_offset),
                        ),
                    )
                continue

            table_match = re.match(
                r'^CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(?:(?:"?([A-Za-z_][A-Za-z0-9_]*)"?)\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?\s*\((.*)\)$',
                stripped,
                re.IGNORECASE | re.DOTALL,
            )
            if table_match:
                schema = table_match.group(1)
                table = table_match.group(2)
                body = table_match.group(3)
                line_number = self._line_number(text, start_offset)
                entity_rows.append((table, "table", "sql_table", rel, schema, line_number))
                for item in self._split_sql_items(body):
                    piece = item.strip()
                    if not piece:
                        continue
                    piece_upper = piece.upper()
                    reference_targets = re.findall(
                        r'REFERENCES\s+"?([A-Za-z_][A-Za-z0-9_]*)"?',
                        piece,
                        re.IGNORECASE,
                    )
                    if piece_upper.startswith(
                        ("CONSTRAINT", "PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK"),
                    ):
                        for target in reference_targets:
                            relationship_rows.append(
                                (table, target, "foreign_key", rel, line_number),
                            )
                        continue
                    col = re.match(
                        r'^"?([A-Za-z_][A-Za-z0-9_]*)"?\s+(.+)$',
                        piece,
                        re.IGNORECASE | re.DOTALL,
                    )
                    if not col:
                        continue
                    column = col.group(1)
                    column_type = self._extract_sql_column_type(col.group(2))
                    field_rows.append(
                        (table, column, column_type, "column", "sql", rel, line_number),
                    )
                    for target in reference_targets:
                        relationship_rows.append((table, target, "foreign_key", rel, line_number))
                    if (
                        not reference_targets
                        and column.lower().endswith("id")
                        and column.lower() != "id"
                    ):
                        for target in self._id_reference_targets(column[:-2]):
                            relationship_rows.append(
                                (table, target, "id_reference", rel, line_number),
                            )
                continue

            alter_table_match = re.match(
                r'^ALTER\s+TABLE\s+"?([A-Za-z_][A-Za-z0-9_]*)"?\s+(.*)$',
                stripped,
                re.IGNORECASE | re.DOTALL,
            )
            if alter_table_match:
                table = alter_table_match.group(1)
                remainder = alter_table_match.group(2)
                line_number = self._line_number(text, start_offset)
                for add_column in re.finditer(
                    r'ADD\s+COLUMN\s+"?([A-Za-z_][A-Za-z0-9_]*)"?\s+([^,]+)',
                    remainder,
                    re.IGNORECASE,
                ):
                    column = add_column.group(1)
                    column_type = self._extract_sql_column_type(add_column.group(2))
                    field_rows.append(
                        (table, column, column_type, "column", "sql", rel, line_number),
                    )
                    if column.lower().endswith("id") and column.lower() != "id":
                        for target in self._id_reference_targets(column[:-2]):
                            relationship_rows.append(
                                (table, target, "id_reference", rel, line_number),
                            )
                for target in re.findall(
                    r'FOREIGN\s+KEY\s*\([^)]+\)\s+REFERENCES\s+"?([A-Za-z_][A-Za-z0-9_]*)"?',
                    remainder,
                    re.IGNORECASE,
                ):
                    relationship_rows.append((table, target, "foreign_key", rel, line_number))
        return entity_rows, field_rows, relationship_rows

    def _extract_prisma_schema(self, path: Path, rel: str):
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        entity_rows = []
        field_rows = []
        relationship_rows = []
        pending_fields: list[tuple[str, str, str, int]] = []
        model_names: set[str] = set()
        enum_names: set[str] = set()

        current_entity: str | None = None
        current_kind: str | None = None

        for line_number, raw_line in enumerate(lines, start=1):
            stripped = raw_line.split("//", 1)[0].strip()
            if not stripped:
                continue

            if current_entity is None:
                entity_match = re.match(r"^(model|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{", stripped)
                if not entity_match:
                    continue
                current_kind = entity_match.group(1)
                current_entity = entity_match.group(2)
                if current_kind == "model":
                    model_names.add(current_entity)
                    entity_rows.append(
                        (
                            current_entity,
                            "model",
                            "prisma_model",
                            rel,
                            None,
                            line_number,
                        ),
                    )
                else:
                    enum_names.add(current_entity)
                    entity_rows.append(
                        (current_entity, "enum", "prisma_enum", rel, None, line_number),
                    )
                continue

            if stripped.startswith("}"):
                current_entity = None
                current_kind = None
                continue

            if current_kind == "enum":
                value = stripped.rstrip(",")
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
                    field_rows.append(
                        (
                            current_entity,
                            value,
                            None,
                            "enum_member",
                            "prisma_enum",
                            rel,
                            line_number,
                        ),
                    )
                continue

            if stripped.startswith("@@"):
                continue
            field_match = re.match(
                r"^([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*\??(?:\[\])?)(?:\s+.*)?$",
                stripped,
            )
            if not field_match:
                continue
            pending_fields.append(
                (
                    current_entity,
                    field_match.group(1),
                    field_match.group(2),
                    line_number,
                ),
            )

        model_lookup = {name.lower(): name for name in model_names}
        enum_lookup = {name.lower(): name for name in enum_names}

        for entity_name, field_name, field_type, line_number in pending_fields:
            base_type = field_type.rstrip("?")
            is_list = base_type.endswith("[]")
            if is_list:
                base_type = base_type[:-2]

            field_kind = "field"
            if base_type.lower() in enum_lookup:
                field_kind = "enum_field"
            elif base_type.lower() in model_lookup:
                field_kind = "relation"
                relationship_rows.append(
                    (
                        entity_name,
                        model_lookup[base_type.lower()],
                        "prisma_relation",
                        rel,
                        line_number,
                    ),
                )

            field_rows.append(
                (
                    entity_name,
                    field_name,
                    field_type,
                    field_kind,
                    "prisma",
                    rel,
                    line_number,
                ),
            )

            if (
                field_kind == "field"
                and field_name.lower().endswith("id")
                and field_name.lower() != "id"
            ):
                target = self._match_entity_name(field_name[:-2], model_lookup)
                if target is not None:
                    relationship_rows.append(
                        (entity_name, target, "id_reference", rel, line_number),
                    )

        return entity_rows, field_rows, relationship_rows

    def _split_sql_statements(self, text: str) -> list[tuple[str, int]]:
        statements: list[tuple[str, int]] = []
        start = 0
        in_single_quote = False
        i = 0
        while i < len(text):
            char = text[i]
            if char == "'":
                if in_single_quote and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_single_quote = not in_single_quote
            elif char == ";" and not in_single_quote:
                statements.append((text[start:i], start))
                start = i + 1
            i += 1
        if start < len(text):
            statements.append((text[start:], start))
        return statements

    def _split_sql_items(self, body: str) -> list[str]:
        items: list[str] = []
        start = 0
        paren_depth = 0
        in_single_quote = False
        for index, char in enumerate(body):
            if char == "'":
                in_single_quote = not in_single_quote
            elif not in_single_quote:
                if char == "(":
                    paren_depth += 1
                elif char == ")":
                    paren_depth = max(0, paren_depth - 1)
                elif char == "," and paren_depth == 0:
                    items.append(body[start:index])
                    start = index + 1
        tail = body[start:]
        if tail.strip():
            items.append(tail)
        return items

    def _extract_sql_column_type(self, remainder: str) -> str:
        cleaned = remainder.strip()
        split = re.split(
            r"\s+(?:NOT\s+NULL|NULL|DEFAULT|PRIMARY\s+KEY|REFERENCES|CONSTRAINT|UNIQUE|CHECK)\b",
            cleaned,
            maxsplit=1,
            flags=re.IGNORECASE,
        )
        return split[0].strip().rstrip(",")

    # Relationship family classification
    _STRUCTURAL_KINDS = {
        "ef_foreign_key",
        "ef_has_one",
        "ef_has_many",
        "ef_owns_one",
        "ef_owns_many",
        "foreign_key",
        "nav_reference",
        "nav_collection",
        "prisma_relation",
    }
    _PROCEDURAL_KINDS = {
        "di_scoped",
        "di_transient",
        "di_singleton",
        "implements",
    }
    # Everything else defaults to structural

    def _classify_relationship_family(self, relation_kind: str) -> str:
        if relation_kind in self._PROCEDURAL_KINDS:
            return "procedural"
        if relation_kind in self._STRUCTURAL_KINDS:
            return "structural"
        # Execution family will come from execution_events, not from static code extraction
        return "structural"

    def _capitalize_identifier(self, value: str) -> str:
        if not value:
            return value
        return value[0].upper() + value[1:]

    def _id_reference_targets(self, stem: str) -> list[str]:
        base = self._capitalize_identifier(stem)
        candidates = [base]
        if base.endswith("s"):
            singular = base[:-1]
            if singular:
                candidates.append(singular)
        else:
            candidates.append(base + "s")
        return list(dict.fromkeys(item for item in candidates if item))

    def _match_entity_name(self, stem: str, lookup: dict[str, str]) -> str | None:
        if not stem:
            return None
        lower = stem.lower()
        if lower in lookup:
            return lookup[lower]
        if lower.endswith("s") and lower[:-1] in lookup:
            return lookup[lower[:-1]]
        if lower + "s" in lookup:
            return lookup[lower + "s"]
        return None

    def _classify_csharp_entity(self, rel: str, entity: str, kind: str) -> str:
        lower_rel = rel.lower()
        parts = lower_rel.split("/")
        lower_entity = entity.lower()
        if kind == "enum":
            return "csharp_enum"
        if ("pages" in parts or lower_rel.endswith(".cshtml.cs")) and lower_entity.endswith(
            "model",
        ):
            return "csharp_page_model"
        if lower_entity.endswith(("configuration", "config")) or any(
            token in parts for token in ("configurations", "entityconfigurations", "mapping")
        ):
            return "csharp_ef_config"
        if lower_entity.endswith("service"):
            return "csharp_service"
        if lower_entity.endswith("controller"):
            return "csharp_controller"
        if lower_entity.endswith("policy"):
            return "csharp_policy"
        if lower_entity.endswith(("validator", "validation")):
            return "csharp_validator"
        if lower_entity.endswith(("repository", "store")):
            return "csharp_repository"
        if lower_entity.endswith(("dto", "request", "response")) or any(
            token in parts for token in ("dto", "dtos", "contracts", "requests", "responses")
        ):
            return "csharp_dto"
        if lower_entity.endswith("viewmodel") or "viewmodels" in parts:
            return "csharp_view_model"
        if lower_entity.endswith("entity") or any(
            token in parts for token in ("entities", "domain")
        ):
            return "csharp_entity"
        if lower_entity.endswith(("model", "record")):
            return "csharp_model"
        return "csharp_support"

    def _schema_category_for_source_type(self, source_type: str) -> str:
        if source_type in {
            "sql_table",
            "sql_enum",
            "prisma_model",
            "prisma_enum",
            "ef_table",
        }:
            return "persistence"
        if source_type in {
            "csharp_entity",
            "csharp_dto",
            "csharp_view_model",
            "csharp_model",
            "csharp_enum",
        }:
            return "data_shapes"
        if source_type in {"csharp_service", "csharp_policy"}:
            return "logic"
        if source_type in {"csharp_page_model", "csharp_controller"}:
            return "presentation"
        if source_type in {"csharp_ef_config", "csharp_validator", "csharp_repository"}:
            return "infrastructure"
        if source_type in {"csharp_support"}:
            return "support"
        return "other"

    def _relationship_priority(self, relation_kind: str) -> int:
        priorities = {
            "foreign_key": 0,
            "prisma_relation": 1,
            "id_reference": 2,
        }
        return priorities.get(relation_kind, 10)

    def _line_number(self, text: str, offset: int) -> int:
        return text[:offset].count("\n") + 1
