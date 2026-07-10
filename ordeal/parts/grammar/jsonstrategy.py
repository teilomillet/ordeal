from __future__ import annotations
# ruff: noqa
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
