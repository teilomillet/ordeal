from __future__ import annotations
# ruff: noqa
def _discover_handlers(app: Any, *, max_depth: int = 3) -> list[str]:
    """BFS through app routes and call graph up to max_depth."""
    import ast
    import importlib as _il
    import inspect
    import textwrap

    targets: list[str] = []
    seen: set[str] = set()
    routes = getattr(app, "routes", None)
    if not routes:
        return targets
    queue: list[tuple[str, Any, int]] = []
    for route in routes:
        ep = getattr(route, "endpoint", None)
        if ep is None:
            continue
        name = getattr(ep, "__name__", "")
        mod = getattr(ep, "__module__", None)
        if not mod or not name:
            continue
        if any(s in name.lower() for s in ("openapi", "swagger", "docs", "schema")):
            continue
        path = f"{mod}.{name}"
        if path not in seen:
            seen.add(path)
            targets.append(path)
            queue.append((mod, ep, 0))
    while queue:
        mod_name, func, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        # Unwrap decorators (@ray.remote, @functools.wraps, etc.)
        func = getattr(func, "_function", func)
        try:
            func = inspect.unwrap(func)
        except (ValueError, TypeError):
            pass
        try:
            source = textwrap.dedent(inspect.getsource(func))
            tree = ast.parse(source)
        except Exception:
            continue
        mod_obj = _il.import_module(mod_name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            cn = node.func.id
            callee = getattr(mod_obj, cn, None)
            if callee is None or not callable(callee) or inspect.isclass(callee):
                continue
            if getattr(callee, "__module__", None) != mod_name:
                continue
            cp = f"{mod_name}.{cn}"
            if cp not in seen:
                seen.add(cp)
                targets.append(cp)
                queue.append((mod_name, callee, depth + 1))
    return targets
def _resolve_call_target(node: Any, module: Any, module_path: str) -> str | None:
    import ast
    import inspect as _inspect

    func = node.func
    if isinstance(func, ast.Name):
        obj = getattr(module, func.id, None)
        if obj is None or _inspect.isclass(obj) or not callable(obj):
            return None
        if getattr(obj, "__module__", None) in ("builtins", "_operator"):
            return None
        return f"{module_path}.{func.id}"
    return None
# ---------------------------------------------------------------------------
# $ref resolver
# ---------------------------------------------------------------------------


def _resolve_refs(node: Any, root: dict) -> Any:
    """Recursively resolve JSON Schema ``$ref`` pointers against *root*."""
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#/"):
                _log.warning("External $ref not supported: %s", ref)
                return {}
            parts = ref[2:].split("/")
            target = root
            for part in parts:
                part = part.replace("~1", "/").replace("~0", "~")
                target = target[part]
            return _resolve_refs(target, root)
        return {k: _resolve_refs(v, root) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(item, root) for item in node]
    return node
# ---------------------------------------------------------------------------
# JSON Schema -> Hypothesis strategy
# ---------------------------------------------------------------------------


def _schema_to_strategy(
    node: dict | None,
    root: dict,
    *,
    _depth: int = 0,
) -> st.SearchStrategy:
    """Convert a JSON Schema node to a boundary-biased Hypothesis strategy."""
    if _depth > _MAX_REF_DEPTH:
        return st.just(None)

    if node is None or not isinstance(node, dict):
        return st.just(None)

    # Resolve $ref inline
    if "$ref" in node:
        resolved = _resolve_refs(node, root)
        return _schema_to_strategy(resolved, root, _depth=_depth + 1)

    # Composition keywords
    if "oneOf" in node:
        subs = [_schema_to_strategy(s, root, _depth=_depth + 1) for s in node["oneOf"]]
        return st.one_of(*subs) if subs else st.just(None)

    if "anyOf" in node:
        subs = [_schema_to_strategy(s, root, _depth=_depth + 1) for s in node["anyOf"]]
        return st.one_of(*subs) if subs else st.just(None)

    if "allOf" in node:
        merged: dict[str, Any] = {}
        for sub in node["allOf"]:
            resolved = _resolve_refs(sub, root) if "$ref" in sub else sub
            for k, v in resolved.items():
                if k == "properties" and k in merged:
                    merged[k] = {**merged[k], **v}
                elif k == "required" and k in merged:
                    merged[k] = list(set(merged[k]) | set(v))
                else:
                    merged[k] = v
        return _schema_to_strategy(merged, root, _depth=_depth + 1)

    # Enum
    if "enum" in node:
        return st.sampled_from(node["enum"])

    # Const
    if "const" in node:
        return st.just(node["const"])

    schema_type = node.get("type")

    # OpenAPI 3.0 nullable
    nullable = node.get("nullable", False)

    def _wrap_nullable(strategy: st.SearchStrategy) -> st.SearchStrategy:
        return st.one_of(st.none(), strategy) if nullable else strategy

    if schema_type == "integer":
        return _wrap_nullable(
            biased.integers(
                min_value=node.get("minimum"),
                max_value=node.get("maximum"),
            )
        )

    if schema_type == "number":
        return _wrap_nullable(
            biased.floats(
                min_value=node.get("minimum"),
                max_value=node.get("maximum"),
                allow_nan=False,
            )
        )

    if schema_type == "string":
        fmt = node.get("format")
        if fmt == "date-time":
            return _wrap_nullable(st.datetimes().map(lambda d: d.isoformat()))
        if fmt == "date":
            return _wrap_nullable(st.dates().map(str))
        if fmt == "uuid":
            return _wrap_nullable(st.uuids().map(str))
        if fmt == "uri" or fmt == "url":
            return _wrap_nullable(st.just("https://example.com/path"))
        if fmt == "email":
            return _wrap_nullable(st.from_regex(r"[a-z]{3,8}@example\.com", fullmatch=True))
        if "pattern" in node:
            return _wrap_nullable(st.from_regex(node["pattern"], fullmatch=True))
        return _wrap_nullable(
            biased.strings(
                min_size=node.get("minLength", 0),
                max_size=node.get("maxLength", 100),
            )
        )

    if schema_type == "boolean":
        return _wrap_nullable(st.booleans())

    if schema_type == "null":
        return st.none()

    if schema_type == "array":
        items = node.get("items", {})
        item_strat = _schema_to_strategy(items, root, _depth=_depth + 1)
        return _wrap_nullable(
            biased.lists(
                item_strat,
                min_size=node.get("minItems", 0),
                max_size=node.get("maxItems", 10),
            )
        )

    if schema_type == "object" or "properties" in node:
        properties = node.get("properties", {})
        required_keys = set(node.get("required", []))
        required_dict = {
            k: _schema_to_strategy(v, root, _depth=_depth + 1)
            for k, v in properties.items()
            if k in required_keys
        }
        optional_dict = {
            k: _schema_to_strategy(v, root, _depth=_depth + 1)
            for k, v in properties.items()
            if k not in required_keys
        }
        if optional_dict:
            return _wrap_nullable(st.fixed_dictionaries(required_dict, optional=optional_dict))
        return _wrap_nullable(st.fixed_dictionaries(required_dict))

    # No type specified — try to infer from other keywords
    if "properties" in node or "required" in node:
        return _schema_to_strategy({**node, "type": "object"}, root, _depth=_depth)

    _log.debug("Unrecognized schema node, falling back to None: %s", node)
    return st.just(None)
# ---------------------------------------------------------------------------
# OpenAPI parser
# ---------------------------------------------------------------------------


@dataclass
class _Endpoint:
    """Parsed representation of a single API endpoint."""

    method: str  # GET, POST, PUT, DELETE, PATCH
    path: str  # /items/{item_id}
    path_params: list[dict[str, Any]] = field(default_factory=list)
    query_params: list[dict[str, Any]] = field(default_factory=list)
    header_params: list[dict[str, Any]] = field(default_factory=list)
    request_body: dict | None = None  # JSON Schema for body
    response_codes: set[int] = field(default_factory=set)
    response_schemas: dict[int, dict] = field(default_factory=dict)  # status -> JSON Schema
    operation_id: str | None = None
    response_links: dict[int, list["_Link"]] = field(default_factory=dict)
@dataclass(frozen=True)
class _Link:
    """Supported subset of the OpenAPI Link Object."""

    operation_id: str | None = None
    operation_ref: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    request_body: Any = None
def _parse_endpoints(spec: dict) -> list[_Endpoint]:
    """Extract endpoints from a resolved OpenAPI 3.x spec."""
    root = spec
    endpoints: list[_Endpoint] = []

    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        # Path-level parameters apply to all operations
        path_level_params = _resolve_refs(path_item.get("parameters", []), root)

        for method in ("get", "post", "put", "delete", "patch"):
            if method not in path_item:
                continue
            operation = path_item[method]
            if not isinstance(operation, dict):
                continue

            # Merge path-level + operation-level params (operation wins)
            op_params = _resolve_refs(operation.get("parameters", []), root)
            params_by_key: dict[tuple[str, str], dict] = {}
            for p in path_level_params:
                resolved = _resolve_refs(p, root)
                params_by_key[(resolved.get("in", ""), resolved.get("name", ""))] = resolved
            for p in op_params:
                resolved = _resolve_refs(p, root)
                params_by_key[(resolved.get("in", ""), resolved.get("name", ""))] = resolved

            all_params = list(params_by_key.values())
            path_params = [p for p in all_params if p.get("in") == "path"]
            query_params = [p for p in all_params if p.get("in") == "query"]
            header_params = [p for p in all_params if p.get("in") == "header"]

            # Request body
            body_schema = None
            rb = _resolve_refs(operation.get("requestBody", {}), root)
            content = rb.get("content", {}) if isinstance(rb, dict) else {}
            if "application/json" in content:
                body_schema = _resolve_refs(content["application/json"].get("schema", {}), root)

            # Response codes, schemas, and link-based transitions
            response_codes: set[int] = set()
            response_schemas: dict[int, dict] = {}
            response_links: dict[int, list[_Link]] = {}
            for code_str, resp_obj in operation.get("responses", {}).items():
                try:
                    code = int(code_str)
                    response_codes.add(code)
                except ValueError:
                    continue  # "default", "2XX", etc.
                resp_obj = _resolve_refs(resp_obj, root) if isinstance(resp_obj, dict) else {}
                content = resp_obj.get("content", {})
                if "application/json" in content:
                    schema = content["application/json"].get("schema", {})
                    response_schemas[code] = _resolve_refs(schema, root)
                parsed_links: list[_Link] = []
                for link_obj in resp_obj.get("links", {}).values():
                    if not isinstance(link_obj, dict):
                        continue
                    resolved_link = _resolve_refs(link_obj, root)
                    operation_id = resolved_link.get("operationId")
                    operation_ref = resolved_link.get("operationRef")
                    if operation_id is None and operation_ref is None:
                        continue
                    parameters = resolved_link.get("parameters", {})
                    if not isinstance(parameters, dict):
                        parameters = {}
                    parsed_links.append(
                        _Link(
                            operation_id=operation_id,
                            operation_ref=operation_ref,
                            parameters=parameters,
                            request_body=resolved_link.get("requestBody"),
                        )
                    )
                if parsed_links:
                    response_links[code] = parsed_links

            endpoints.append(
                _Endpoint(
                    method=method.upper(),
                    path=path,
                    path_params=path_params,
                    query_params=query_params,
                    header_params=header_params,
                    request_body=body_schema,
                    response_codes=response_codes,
                    response_schemas=response_schemas,
                    operation_id=operation.get("operationId"),
                    response_links=response_links,
                )
            )

    return endpoints
# ---------------------------------------------------------------------------
# Endpoint -> Hypothesis strategy
# ---------------------------------------------------------------------------

_PATH_PARAM_RE = re.compile(r"\{([^}]+)\}")
@dataclass
class _APICase:
    """Generated test case for one API call."""

    method: str
    path: str  # with path params substituted
    headers: dict[str, str]
    query_params: dict[str, str]
    body: Any  # JSON-serializable or None
    endpoint_path: str  # original path template
    path_params: dict[str, Any] = field(default_factory=dict)
def _endpoint_strategy(endpoint: _Endpoint, root: dict) -> st.SearchStrategy[_APICase]:
    """Build a Hypothesis strategy that generates test cases for *endpoint*."""
    # Path params
    path_strats: dict[str, st.SearchStrategy] = {}
    for p in endpoint.path_params:
        schema = p.get("schema", {"type": "string"})
        path_strats[p["name"]] = _schema_to_strategy(schema, root).map(str)

    # Query params
    query_strats: dict[str, st.SearchStrategy] = {}
    required_query = {p["name"] for p in endpoint.query_params if p.get("required")}
    for p in endpoint.query_params:
        schema = p.get("schema", {"type": "string"})
        query_strats[p["name"]] = _schema_to_strategy(schema, root).map(str)

    # Body
    body_strat = (
        _schema_to_strategy(endpoint.request_body, root)
        if endpoint.request_body
        else st.just(None)
    )

    # Build path/query strategies
    if path_strats:
        path_dict_strat = st.fixed_dictionaries(path_strats)
    else:
        path_dict_strat = st.just({})

    if query_strats:
        required_q = {k: v for k, v in query_strats.items() if k in required_query}
        optional_q = {k: v for k, v in query_strats.items() if k not in required_query}
        if optional_q:
            query_dict_strat = st.fixed_dictionaries(required_q, optional=optional_q)
        else:
            query_dict_strat = st.fixed_dictionaries(required_q)
    else:
        query_dict_strat = st.just({})

    @st.composite
    def build_case(draw: st.DrawFn) -> _APICase:
        path_vals = draw(path_dict_strat)
        query_vals = draw(query_dict_strat)
        body_val = draw(body_strat)

        # Substitute path params
        resolved_path = endpoint.path
        for name, val in path_vals.items():
            resolved_path = resolved_path.replace(f"{{{name}}}", urllib.parse.quote(val, safe=""))

        return _APICase(
            method=endpoint.method,
            path=resolved_path,
            headers={},
            query_params=query_vals,
            body=body_val,
            endpoint_path=endpoint.path,
            path_params=path_vals,
        )

    return build_case()
_MISSING = object()
