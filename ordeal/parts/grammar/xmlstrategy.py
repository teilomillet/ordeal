from __future__ import annotations
# ruff: noqa
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
