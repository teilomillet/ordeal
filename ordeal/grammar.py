"""Grammar-based / structure-aware input generation for structured data types.

Random testing with ``st.text()`` or ``st.binary()`` produces syntactically
invalid inputs that get rejected by the parser layer and never reach the
interesting business logic underneath.  Grammar-aware generation produces
**syntactically valid** inputs with **semantically interesting** variations,
so the fuzzer spends its cycles testing real code paths.

This is the Python equivalent of libFuzzer's structure-aware custom mutators
(``LLVMFuzzerCustomMutator``).  Where libFuzzer requires hand-written C++
mutators, ordeal provides composable Hypothesis strategies that generate
valid structured data out of the box.

How it complements the rest of ordeal:

- **CMPLOG** (:mod:`ordeal.cmplog`) cracks guarded branches on existing
  parameters by extracting literal comparison values from source code.
  Grammar strategies solve the *prior* problem: getting a well-formed input
  past the parser so those guarded branches are even reachable.
- **Adversarial strategies** (:mod:`ordeal.strategies`) inject known-bad
  values (SQL injection, NaN floats).  Grammar strategies inject
  *structurally valid* values with adversarial variation (deep nesting,
  edge-case keys, unusual but legal characters).
- **Coverage-guided exploration** (:mod:`ordeal.explore`) benefits directly:
  valid inputs produce longer execution traces with more edge diversity,
  feeding the AFL-style energy scheduling loop.

Each function returns a ``hypothesis.strategies.SearchStrategy`` suitable for
``@given()``, ``@rule()``, ``data.draw()``, or any Hypothesis context.

Discover all grammar strategies programmatically::

    from ordeal.grammar import catalog
    for entry in catalog():
        print(f"{entry['name']}  -- {entry['doc']}")
"""

from __future__ import annotations

import inspect
import string
import sys
from typing import Any

import hypothesis.strategies as st

# ============================================================================
# JSON
# ============================================================================


@st.composite
def json_strategy(
    draw: st.DrawFn,
    schema: dict[str, Any] | None = None,
    max_depth: int = 3,
) -> st.SearchStrategy:
    """Hypothesis strategy for valid JSON values.

    Without *schema*, generates random valid JSON: objects, arrays, strings,
    numbers, booleans, and null, nested up to *max_depth* levels.

    With *schema* (a dict describing structure), generates conforming
    instances.  Schema keys are field names; values are either:

    - A Python type (``str``, ``int``, ``float``, ``bool``)
    - ``None`` for JSON null
    - A ``list`` with one element describing the item type
    - A nested ``dict`` for sub-objects

    Example::

        from ordeal.grammar import json_strategy

        # Random valid JSON
        @given(json_strategy())
        def test_roundtrip(val):
            assert json.loads(json.dumps(val)) == val or isinstance(val, float)

        # Schema-constrained
        @given(json_strategy(schema={"name": str, "age": int, "tags": [str]}))
        def test_user(user):
            assert isinstance(user["name"], str)
            assert isinstance(user["age"], int)
    """
    if schema is not None:
        return draw(_json_from_schema(schema, max_depth))
    return draw(_json_value(max_depth))


def _json_value(depth: int) -> st.SearchStrategy:
    """Random valid JSON value up to *depth* levels of nesting."""
    leaves = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**53), max_value=2**53),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(
            alphabet=st.characters(blacklist_categories=("Cs",)),
            min_size=0,
            max_size=50,
        ),
    )
    if depth <= 0:
        return leaves
    nested = st.one_of(
        leaves,
        st.lists(_json_value(depth - 1), max_size=5),
        st.dictionaries(
            st.text(
                alphabet=st.characters(
                    whitelist_categories=("L", "N"),
                    min_codepoint=32,
                    max_codepoint=126,
                ),
                min_size=1,
                max_size=20,
            ),
            _json_value(depth - 1),
            max_size=5,
        ),
    )
    return nested


@st.composite
def _json_from_schema(
    draw: st.DrawFn,
    schema: dict[str, Any],
    max_depth: int,
) -> dict:
    """Generate a JSON object conforming to a schema dict."""
    result: dict[str, Any] = {}
    for key, spec in schema.items():
        result[key] = draw(_strategy_for_schema_value(spec, max_depth))
    return result


def _strategy_for_schema_value(spec: Any, max_depth: int) -> st.SearchStrategy:
    """Map a schema spec to a Hypothesis strategy."""
    if spec is None:
        return st.none()
    if spec is str:
        return st.text(min_size=0, max_size=50)
    if spec is int:
        return st.integers(min_value=-(2**31), max_value=2**31)
    if spec is float:
        return st.floats(allow_nan=False, allow_infinity=False)
    if spec is bool:
        return st.booleans()
    if isinstance(spec, list) and len(spec) == 1:
        return st.lists(_strategy_for_schema_value(spec[0], max_depth - 1), max_size=5)
    if isinstance(spec, dict):
        return _json_from_schema(spec, max_depth - 1)
    # Fallback: treat as a concrete value
    return st.just(spec)


# ============================================================================
# SQL
# ============================================================================


@st.composite
def sql_strategy(
    draw: st.DrawFn,
    dialect: str = "sqlite",
    tables: dict[str, list[str]] | None = None,
) -> st.SearchStrategy[str]:
    """Hypothesis strategy for valid SQL queries.

    Generates SELECT, INSERT, UPDATE, and DELETE statements with valid
    syntax.  If *tables* is provided (mapping table names to column name
    lists), uses those names; otherwise generates plausible identifiers.

    The *dialect* parameter is reserved for future dialect-specific syntax;
    currently all output follows ANSI/SQLite conventions.

    Example::

        from ordeal.grammar import sql_strategy

        tables = {"users": ["id", "name", "email"], "orders": ["id", "user_id", "total"]}

        @given(sql_strategy(tables=tables))
        def test_parser_accepts(query):
            # Should never raise a parse error
            parse_sql(query)
    """
    if tables is None:
        tables = draw(
            st.dictionaries(
                _sql_identifier(),
                st.lists(_sql_identifier(), min_size=1, max_size=6),
                min_size=1,
                max_size=3,
            )
        )

    table_name = draw(st.sampled_from(sorted(tables.keys())))
    columns = tables[table_name]
    stmt_type = draw(st.sampled_from(["SELECT", "INSERT", "UPDATE", "DELETE"]))

    if stmt_type == "SELECT":
        return draw(_sql_select(table_name, columns))
    elif stmt_type == "INSERT":
        return draw(_sql_insert(table_name, columns))
    elif stmt_type == "UPDATE":
        return draw(_sql_update(table_name, columns))
    else:
        return draw(_sql_delete(table_name, columns))


def _sql_identifier() -> st.SearchStrategy[str]:
    """Generate a valid SQL identifier."""
    first = st.sampled_from(list(string.ascii_lowercase))
    rest = st.text(
        alphabet=string.ascii_lowercase + string.digits + "_",
        min_size=0,
        max_size=10,
    )
    return st.tuples(first, rest).map(lambda t: t[0] + t[1])


def _sql_literal() -> st.SearchStrategy[str]:
    """Generate a SQL literal value."""
    return st.one_of(
        st.integers(min_value=-10000, max_value=10000).map(str),
        st.text(
            alphabet=string.ascii_letters + string.digits + " ",
            min_size=0,
            max_size=20,
        ).map(lambda s: f"'{s}'"),
        st.just("NULL"),
    )


@st.composite
def _sql_where(draw: st.DrawFn, columns: list[str]) -> str:
    """Generate an optional WHERE clause."""
    if not draw(st.booleans()):
        return ""
    col = draw(st.sampled_from(columns))
    op = draw(st.sampled_from(["=", "!=", "<", ">", "<=", ">=", "LIKE", "IS"]))
    val = draw(_sql_literal())
    if op == "IS":
        val = draw(st.sampled_from(["NULL", "NOT NULL"]))
    return f" WHERE {col} {op} {val}"


@st.composite
def _sql_select(draw: st.DrawFn, table: str, columns: list[str]) -> str:
    """Generate a SELECT statement."""
    use_star = draw(st.booleans())
    if use_star:
        col_clause = "*"
    else:
        selected = draw(
            st.lists(st.sampled_from(columns), min_size=1, max_size=len(columns), unique=True)
        )
        col_clause = ", ".join(selected)
    where = draw(_sql_where(columns))
    order = ""
    if draw(st.booleans()) and columns:
        order_col = draw(st.sampled_from(columns))
        direction = draw(st.sampled_from(["ASC", "DESC"]))
        order = f" ORDER BY {order_col} {direction}"
    limit = ""
    if draw(st.booleans()):
        n = draw(st.integers(min_value=1, max_value=1000))
        limit = f" LIMIT {n}"
    return f"SELECT {col_clause} FROM {table}{where}{order}{limit}"


@st.composite
def _sql_insert(draw: st.DrawFn, table: str, columns: list[str]) -> str:
    """Generate an INSERT statement."""
    selected = draw(
        st.lists(st.sampled_from(columns), min_size=1, max_size=len(columns), unique=True)
    )
    values = [draw(_sql_literal()) for _ in selected]
    col_clause = ", ".join(selected)
    val_clause = ", ".join(values)
    return f"INSERT INTO {table} ({col_clause}) VALUES ({val_clause})"


@st.composite
def _sql_update(draw: st.DrawFn, table: str, columns: list[str]) -> str:
    """Generate an UPDATE statement."""
    selected = draw(
        st.lists(st.sampled_from(columns), min_size=1, max_size=len(columns), unique=True)
    )
    assignments = [f"{col} = {draw(_sql_literal())}" for col in selected]
    set_clause = ", ".join(assignments)
    where = draw(_sql_where(columns))
    return f"UPDATE {table} SET {set_clause}{where}"


@st.composite
def _sql_delete(draw: st.DrawFn, table: str, columns: list[str]) -> str:
    """Generate a DELETE statement."""
    where = draw(_sql_where(columns))
    return f"DELETE FROM {table}{where}"


# ============================================================================
# URL
# ============================================================================


@st.composite
def url_strategy(
    draw: st.DrawFn,
    schemes: list[str] | None = None,
) -> st.SearchStrategy[str]:
    """Hypothesis strategy for valid URLs.

    Generates URLs with various schemes, hosts, paths, query parameters,
    and fragments.  If *schemes* is given, restricts to those schemes;
    otherwise picks from common ones (http, https, ftp, etc.).

    Example::

        from ordeal.grammar import url_strategy

        @given(url_strategy(schemes=["https"]))
        def test_https_only(url):
            assert url.startswith("https://")
    """
    if schemes is None:
        schemes = ["http", "https", "ftp", "file", "ssh"]
    scheme = draw(st.sampled_from(schemes))

    # Host
    domain_part = st.text(
        alphabet=string.ascii_lowercase + string.digits,
        min_size=1,
        max_size=12,
    )
    tld = st.sampled_from(["com", "org", "net", "io", "dev", "co.uk", "edu"])
    host = draw(st.tuples(domain_part, domain_part, tld).map(lambda t: f"{t[0]}.{t[1]}.{t[2]}"))

    # Port
    port = ""
    if draw(st.booleans()):
        port = f":{draw(st.integers(min_value=1, max_value=65535))}"

    # Path
    path_segment = st.text(
        alphabet=string.ascii_lowercase + string.digits + "-_",
        min_size=1,
        max_size=15,
    )
    path_parts = draw(st.lists(path_segment, min_size=0, max_size=5))
    path = "/" + "/".join(path_parts) if path_parts else ""

    # Query
    query = ""
    if draw(st.booleans()):
        param_name = st.text(
            alphabet=string.ascii_lowercase + "_",
            min_size=1,
            max_size=10,
        )
        param_value = st.text(
            alphabet=string.ascii_letters + string.digits,
            min_size=0,
            max_size=20,
        )
        params = draw(st.lists(st.tuples(param_name, param_value), min_size=1, max_size=5))
        query = "?" + "&".join(f"{k}={v}" for k, v in params)

    # Fragment
    fragment = ""
    if draw(st.booleans()):
        fragment = "#" + draw(
            st.text(
                alphabet=string.ascii_lowercase + string.digits + "-_",
                min_size=1,
                max_size=20,
            )
        )

    return f"{scheme}://{host}{port}{path}{query}{fragment}"


# ============================================================================
# Email
# ============================================================================


@st.composite
def email_strategy(draw: st.DrawFn) -> st.SearchStrategy[str]:
    """Hypothesis strategy for valid email addresses.

    Generates addresses with varied local parts (dots, plus tags, digits),
    domains, and TLDs.  Focuses on structurally valid but diverse inputs.

    Example::

        from ordeal.grammar import email_strategy

        @given(email_strategy())
        def test_validate_accepts(email):
            assert "@" in email
            assert validate_email(email)
    """
    local_chars = string.ascii_lowercase + string.digits
    local_base = draw(st.text(alphabet=local_chars, min_size=1, max_size=20))

    # Optional dot-separated parts
    if draw(st.booleans()):
        extra = draw(st.text(alphabet=local_chars, min_size=1, max_size=10))
        local_base = f"{local_base}.{extra}"

    # Optional +tag
    if draw(st.booleans()):
        tag = draw(st.text(alphabet=local_chars, min_size=1, max_size=8))
        local_base = f"{local_base}+{tag}"

    # Domain
    domain_part = draw(
        st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=12)
    )
    tld = draw(st.sampled_from(["com", "org", "net", "io", "dev", "co.uk", "edu", "gov", "info"]))

    return f"{local_base}@{domain_part}.{tld}"


# ============================================================================
# File path
# ============================================================================


@st.composite
def path_strategy(draw: st.DrawFn) -> st.SearchStrategy[str]:
    """Hypothesis strategy for valid file paths (Unix and Windows).

    Generates both Unix-style (``/usr/local/bin``) and Windows-style
    (``C:\\\\Users\\\\docs``) paths with varied depth, extensions, and
    special directory names.

    Example::

        from ordeal.grammar import path_strategy

        @given(path_strategy())
        def test_path_handling(path):
            result = normalize(path)
            assert isinstance(result, str)
    """
    style = draw(st.sampled_from(["unix", "windows"]))

    name_chars = string.ascii_lowercase + string.digits + "-_"
    segment = st.text(alphabet=name_chars, min_size=1, max_size=15)
    parts = draw(st.lists(segment, min_size=1, max_size=8))

    # Optional file extension on last segment
    if draw(st.booleans()):
        ext = draw(
            st.sampled_from(
                ["txt", "py", "json", "csv", "xml", "html", "log", "cfg", "dat", "tmp"]
            )
        )
        parts[-1] = f"{parts[-1]}.{ext}"

    if style == "unix":
        return "/" + "/".join(parts)
    else:
        drive = draw(st.sampled_from(["C", "D", "E"]))
        return f"{drive}:\\" + "\\".join(parts)


# ============================================================================
# CSV
# ============================================================================


@st.composite
def csv_strategy(
    draw: st.DrawFn,
    columns: list[str] | None = None,
    rows: int | None = None,
) -> st.SearchStrategy[str]:
    """Hypothesis strategy for valid CSV data.

    Generates CSV text with a header row and data rows.  If *columns* is
    provided, uses those as headers; otherwise generates random headers.
    If *rows* is given, generates exactly that many data rows; otherwise
    picks a random count (0-20).

    Example::

        from ordeal.grammar import csv_strategy

        @given(csv_strategy(columns=["name", "age", "score"]))
        def test_csv_parser(csv_text):
            records = parse_csv(csv_text)
            assert all("name" in r for r in records)
    """
    if columns is None:
        col_name = st.text(
            alphabet=string.ascii_lowercase + "_",
            min_size=1,
            max_size=12,
        )
        columns = draw(st.lists(col_name, min_size=1, max_size=8, unique=True))

    if rows is None:
        rows = draw(st.integers(min_value=0, max_value=20))

    header = ",".join(columns)
    data_rows: list[str] = []
    for _ in range(rows):
        cells: list[str] = []
        for _ in columns:
            cell = draw(_csv_cell())
            cells.append(cell)
        data_rows.append(",".join(cells))

    return header + "\n" + "\n".join(data_rows)


@st.composite
def _csv_cell(draw: st.DrawFn) -> str:
    """Generate a single CSV cell value."""
    cell_type = draw(st.sampled_from(["string", "int", "float", "empty", "quoted"]))
    if cell_type == "string":
        return draw(
            st.text(
                alphabet=string.ascii_letters + string.digits + " ",
                min_size=0,
                max_size=20,
            )
        )
    elif cell_type == "int":
        return str(draw(st.integers(min_value=-10000, max_value=10000)))
    elif cell_type == "float":
        return str(round(draw(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False)), 4))
    elif cell_type == "empty":
        return ""
    else:
        # Quoted string (may contain commas)
        inner = draw(
            st.text(
                alphabet=string.ascii_letters + string.digits + " ,",
                min_size=0,
                max_size=20,
            )
        )
        return f'"{inner}"'


# ============================================================================
# XML
# ============================================================================


@st.composite
def xml_strategy(
    draw: st.DrawFn,
    tag: str | None = None,
    max_depth: int = 2,
) -> st.SearchStrategy[str]:
    """Hypothesis strategy for valid XML documents.

    Generates well-formed XML with nested elements, attributes, and text
    content.  If *tag* is provided, uses it as the root element name;
    otherwise generates a random tag name.

    Example::

        from ordeal.grammar import xml_strategy

        @given(xml_strategy(tag="config", max_depth=3))
        def test_xml_parser(xml_text):
            root = ET.fromstring(xml_text)
            assert root.tag == "config"
    """
    if tag is None:
        tag = draw(_xml_tag_name())
    return draw(_xml_element(tag, max_depth))


def _xml_tag_name() -> st.SearchStrategy[str]:
    """Generate a valid XML tag name."""
    first = st.sampled_from(list(string.ascii_lowercase))
    rest = st.text(
        alphabet=string.ascii_lowercase + string.digits + "_-",
        min_size=0,
        max_size=10,
    )
    return st.tuples(first, rest).map(lambda t: t[0] + t[1])


@st.composite
def _xml_element(draw: st.DrawFn, tag: str, depth: int) -> str:
    """Generate a single XML element, possibly with children."""
    # Attributes
    attrs = ""
    n_attrs = draw(st.integers(min_value=0, max_value=3))
    attr_parts: list[str] = []
    for _ in range(n_attrs):
        attr_name = draw(_xml_tag_name())
        attr_value = draw(
            st.text(
                alphabet=string.ascii_letters + string.digits + " ",
                min_size=0,
                max_size=15,
            )
        )
        attr_parts.append(f'{attr_name}="{attr_value}"')
    if attr_parts:
        attrs = " " + " ".join(attr_parts)

    # Content: text, children, or empty
    if depth <= 0:
        content = draw(
            st.text(
                alphabet=st.characters(
                    whitelist_categories=("L", "N", "Z"),
                    min_codepoint=32,
                    max_codepoint=126,
                ),
                min_size=0,
                max_size=30,
            )
        )
    else:
        content_type = draw(st.sampled_from(["text", "children", "mixed", "empty"]))
        if content_type == "text":
            content = draw(
                st.text(
                    alphabet=st.characters(
                        whitelist_categories=("L", "N", "Z"),
                        min_codepoint=32,
                        max_codepoint=126,
                    ),
                    min_size=0,
                    max_size=30,
                )
            )
        elif content_type == "children":
            n_children = draw(st.integers(min_value=1, max_value=4))
            children: list[str] = []
            for _ in range(n_children):
                child_tag = draw(_xml_tag_name())
                children.append(draw(_xml_element(child_tag, depth - 1)))
            content = "".join(children)
        elif content_type == "mixed":
            text_before = draw(
                st.text(
                    alphabet=string.ascii_letters + " ",
                    min_size=0,
                    max_size=15,
                )
            )
            child_tag = draw(_xml_tag_name())
            child = draw(_xml_element(child_tag, depth - 1))
            content = text_before + child
        else:
            content = ""

    if not content:
        return f"<{tag}{attrs}/>"
    return f"<{tag}{attrs}>{content}</{tag}>"


# ============================================================================
# Regex
# ============================================================================


def regex_strategy(pattern: str) -> st.SearchStrategy[str]:
    """Hypothesis strategy that generates strings matching a regex pattern.

    Wraps ``hypothesis.strategies.from_regex`` with ``fullmatch=True`` so
    every generated string matches the entire pattern.

    Example::

        from ordeal.grammar import regex_strategy

        @given(regex_strategy(r"[A-Z]{2,4}-\\d{1,5}"))
        def test_ticket_format(ticket_id):
            assert re.fullmatch(r"[A-Z]{2,4}-\\d{1,5}", ticket_id)
    """
    return st.from_regex(pattern, fullmatch=True)


# ============================================================================
# Structured (infer strategy from example)
# ============================================================================


@st.composite
def structured_strategy(
    draw: st.DrawFn,
    example: Any,
) -> st.SearchStrategy:
    """Infer a strategy from a concrete example value.

    Takes a concrete value and produces structurally similar values.  The
    structure (dict keys, list element types, nesting) is preserved while
    the leaf values vary.

    Supported types: ``dict``, ``list``, ``tuple``, ``str``, ``int``,
    ``float``, ``bool``, ``None``.

    Example::

        from ordeal.grammar import structured_strategy

        template = {"name": "Alice", "age": 30, "scores": [95, 88]}

        @given(structured_strategy(template))
        def test_user_record(user):
            assert isinstance(user["name"], str)
            assert isinstance(user["age"], int)
            assert all(isinstance(s, int) for s in user["scores"])
    """
    return draw(_infer_strategy(example))


def _infer_strategy(example: Any) -> st.SearchStrategy:
    """Recursively infer a strategy matching the shape of *example*."""
    if example is None:
        return st.none()
    if isinstance(example, bool):
        return st.booleans()
    if isinstance(example, int):
        return st.integers(min_value=-(2**31), max_value=2**31)
    if isinstance(example, float):
        return st.floats(allow_nan=False, allow_infinity=False)
    if isinstance(example, str):
        return st.text(min_size=0, max_size=max(50, len(example) * 2))
    if isinstance(example, list):
        if not example:
            return st.just([])
        # Infer from first element
        elem_strat = _infer_strategy(example[0])
        return st.lists(elem_strat, min_size=0, max_size=max(10, len(example) * 2))
    if isinstance(example, tuple):
        if not example:
            return st.just(())
        strats = [_infer_strategy(e) for e in example]
        return st.tuples(*strats)
    if isinstance(example, dict):
        if not example:
            return st.just({})
        fixed: dict[str, st.SearchStrategy] = {}
        for key, val in example.items():
            fixed[key] = _infer_strategy(val)
        return st.fixed_dictionaries(fixed)
    # Fallback: return the exact value
    return st.just(example)


# ============================================================================
# Catalog — introspect available grammar strategies at runtime
# ============================================================================

# Public strategies exported by this module (used by catalog).
_PUBLIC_STRATEGIES = (
    "json_strategy",
    "sql_strategy",
    "url_strategy",
    "email_strategy",
    "path_strategy",
    "csv_strategy",
    "xml_strategy",
    "regex_strategy",
    "structured_strategy",
)


def catalog() -> list[dict[str, str]]:
    """Discover all grammar-based strategies via runtime introspection.

    Returns a list of dicts with ``name``, ``qualname``, ``doc``,
    ``signature``, and ``parameters``.
    """
    mod = sys.modules[__name__]
    entries: list[dict[str, str]] = []
    for attr_name in _PUBLIC_STRATEGIES:
        obj = getattr(mod, attr_name, None)
        if obj is None:
            continue
        try:
            sig = inspect.signature(obj)
        except (ValueError, TypeError):
            sig_str = "(...)"
            params: dict = {}
        else:
            sig_str = str(sig)
            params = {
                p.name: getattr(p.annotation, "__name__", str(p.annotation))
                for p in sig.parameters.values()
                if p.name != "draw" and p.annotation is not inspect.Parameter.empty
            }
        entries.append(
            {
                "name": attr_name,
                "qualname": f"ordeal.grammar.{attr_name}",
                "signature": sig_str,
                "doc": (inspect.getdoc(obj) or "").split("\n")[0],
                "parameters": params,
            }
        )
    return entries
