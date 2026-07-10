from __future__ import annotations
# ruff: noqa
def _bytecode_equal(a: types.CodeType, b: types.CodeType) -> bool:
    """Compare instructions, referenced names, and constants recursively."""
    return _code_fingerprint(a) == _code_fingerprint(b)
def _is_equivalent_mutant(
    original_tree: ast.Module,
    mutated_tree: ast.Module,
    operator: str,
    description: str,
    line: int,
) -> bool:
    """Detect mutants that are semantically equivalent to the original.

    Checks:
    1. Algebraic identities in the *mutated* tree (the result of the
       mutation is a no-op expression like ``x + 0``).
    2. Algebraic identities in the *original* tree at the mutation site
       (mutating a no-op is still a no-op for the swapped neutral).
    3. Duplicate AST output (original and mutated compile to identical code).
    """
    # 1+2: Check for algebraic identities at the mutation site
    if operator == "arithmetic":
        for tree in (original_tree, mutated_tree):
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.BinOp)
                    and hasattr(node, "lineno")
                    and node.lineno == line
                    and _is_algebraic_identity(node)
                ):
                    return True

    # 3: AST-level deduplication — compile both and compare bytecode
    try:
        orig_code = compile(original_tree, "<orig>", "exec")
        mut_code = compile(mutated_tree, "<mut>", "exec")
        if _bytecode_equal(orig_code, mut_code):
            return True
    except Exception:
        pass

    return False
def _is_inside_skip_method(tree: ast.Module, line: int) -> bool:
    """Check if a mutation line falls inside a method we should skip."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _SKIP_METHODS:
                if node.lineno <= line <= node.end_lineno:
                    return True
    return False
def generate_mutants(
    source: str,
    operators: list[str] | None = None,
    *,
    extra_mutants: list[str | tuple[str, str]] | None = None,
    llm: Callable[[str], str] | None = None,
    concern: str | None = None,
    _stats: dict[str, int] | None = None,
    timeout: float | None = None,
) -> list[tuple[Mutant, ast.Module]]:
    """Generate mutants from source code, filtering out noise.

    Skips mutations inside ``__repr__``, ``__str__``, and other display
    methods (they produce equivalent mutants that always survive).

    When *extra_mutants* is provided, the given source strings are
    validated (parse, compile, dedup) and appended after rule-based ones.
    This is the primary interface for AI assistants and humans to supply
    mutants they wrote directly — no API call needed::

        result = generate_mutants(source, extra_mutants=[
            "def compute(a, b):\\n    if a <= 0: return 0\\n    return a + b",
        ])

    When *llm* is provided, it is called to generate additional mutant
    source strings automatically.  This is a convenience for automated
    pipelines — under the hood it feeds the results through the same
    validation as *extra_mutants*.

    Args:
        source: Python source code to mutate.
        operators: Operator names to use (default: all rule-based operators).
        extra_mutants: Source strings (or ``(description, source)`` tuples)
            to validate and add alongside rule-based mutants.  Written by
            an AI assistant, a human, or any other author.
        llm: Optional callable ``(prompt: str) -> str`` for automated
            mutant generation.  ordeal crafts the prompt; the user
            provides any LLM backend.
        _stats: Optional mutable dict for diagnostic counters.  When
            provided, keys ``generated``, ``filtered_ast_equivalent``,
            and ``skipped_display_method`` are incremented in-place.
        timeout: Maximum seconds for mutant generation.  When exceeded,
            returns whatever mutants have been generated so far instead
            of hanging on complex AST expressions (numpy, cv2, etc.).

    Returns a list of ``(Mutant, mutated_AST)`` pairs.
    """
    deadline = time.monotonic() + timeout if timeout is not None else None
    tree = ast.parse(source)
    qualname_ranges = _qualname_ranges(tree)
    source_lines = source.splitlines()
    ops = operators or _default_operator_order()
    results: list[tuple[Mutant, ast.Module]] = []
    st = _stats  # alias for brevity

    timed_out = False
    for op_name in ops:
        if timed_out:
            break
        if op_name not in OPERATORS:
            raise ValueError(f"Unknown operator: {op_name!r}. Available: {list(OPERATORS)}")
        counter_cls, applicator_cls = OPERATORS[op_name]

        counter = counter_cls()
        counter.visit(tree)

        for i in range(counter.count):
            # Check deadline before each mutant (catches hangs on complex ASTs)
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                if st is not None:
                    st["generation_timed_out"] = 1
                break

            mutated_tree = ast.parse(source)
            applicator = applicator_cls(target_idx=i)
            applicator.visit(mutated_tree)
            ast.fix_missing_locations(mutated_tree)

            if applicator.applied:
                if st is not None:
                    st["generated"] = st.get("generated", 0) + 1
                # Skip mutations in display/repr methods
                if _is_inside_skip_method(tree, applicator.line):
                    if st is not None:
                        st["skipped_display_method"] = st.get("skipped_display_method", 0) + 1
                    continue
                # Skip semantically equivalent mutants
                if _is_equivalent_mutant(
                    tree, mutated_tree, op_name, applicator.description, applicator.line
                ):
                    if st is not None:
                        st["filtered_ast_equivalent"] = st.get("filtered_ast_equivalent", 0) + 1
                    continue
                # Capture the source line for context
                src_line = ""
                if 0 < applicator.line <= len(source_lines):
                    src_line = source_lines[applicator.line - 1].strip()
                try:
                    msrc = ast.unparse(mutated_tree)
                except Exception:
                    msrc = None
                mutant = Mutant(
                    operator=op_name,
                    description=applicator.description,
                    line=applicator.line,
                    col=applicator.col,
                    source_line=src_line,
                    qualname=_qualname_for_line(qualname_ranges, applicator.line),
                    _mutant_source=msrc,
                )
                results.append((mutant, mutated_tree))

    # --- Extra mutants: from AI assistant, human, or LLM ---
    additional: list[tuple[Mutant, ast.Module]] = []
    if extra_mutants is not None:
        additional.extend(_validate_extra_mutants(source, extra_mutants))
    if llm is not None:
        additional.extend(_generate_llm_mutants(source, llm, concern=concern))

    if additional:
        _dedup_into(results, additional)

    return results
def _dedup_into(
    existing: list[tuple[Mutant, ast.Module]],
    new: list[tuple[Mutant, ast.Module]],
) -> None:
    """Append *new* mutants to *existing*, skipping semantic-code duplicates."""
    existing_codes: set[tuple[Any, ...]] = set()
    for _, etree in existing:
        try:
            code = compile(etree, "<existing>", "exec")
            existing_codes.add(_code_fingerprint(code))
        except Exception:
            pass
    for mutant, mtree in new:
        try:
            mcode = compile(mtree, "<new>", "exec")
            key = _code_fingerprint(mcode)
            if key not in existing_codes:
                existing.append((mutant, mtree))
                existing_codes.add(key)
        except Exception:
            pass
# ============================================================================
# Extra mutants — accept source code from any author (AI assistant, human, LLM)
# ============================================================================


def _strip_python_comments(source: str) -> str:
    """Remove comments and normalize whitespace for diff comparison.

    Strips ``# ...`` comments, blank lines, and trailing whitespace so that
    comment-only mutations (a known failure mode — 61% of Meta ACH's
    false equivalents were comment-only) are detected as identical.
    """
    import re

    lines = []
    for line in source.splitlines():
        # Remove inline comments (but not inside strings — good enough heuristic)
        stripped = re.sub(r"#[^\"']*$", "", line).rstrip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)
def _validate_extra_mutants(
    source: str,
    extra_mutants: list[str | tuple[str, str]],
) -> list[tuple[Mutant, ast.Module]]:
    """Validate externally-provided mutant source code.

    Accepts raw source strings — written by an AI assistant, a human, or
    any other author — and runs the full validation pipeline:

    1. ``ast.parse()`` — reject syntax errors
    2. ``compile()`` — reject semantic errors
    3. Comment-strip diff — reject comment-only changes
    4. ``_bytecode_equal()`` — reject identical-to-original
    5. Deduplicate against each other

    Each item in *extra_mutants* is either a source string or a
    ``(description, source)`` tuple.

    Returns ``(Mutant, ast.Module)`` pairs with ``operator="extra"``.
    """
    # Compile the original for comparison
    try:
        original_tree = ast.parse(source)
        original_code = compile(original_tree, "<orig>", "exec")
    except Exception:
        return []

    original_stripped = _strip_python_comments(source)

    results: list[tuple[Mutant, ast.Module]] = []
    seen_fingerprints: set[tuple[Any, ...]] = set()

    for item in extra_mutants:
        if isinstance(item, tuple):
            desc, mutant_source = item
        else:
            desc = "extra mutant"
            mutant_source = item

        # 1. Parse
        try:
            mutant_tree = ast.parse(mutant_source)
        except SyntaxError:
            continue

        # 2. Compile
        try:
            mutant_code = compile(mutant_tree, "<extra-mutant>", "exec")
        except Exception:
            continue

        # 3. Comment-strip diff — reject comment-only changes
        mutant_stripped = _strip_python_comments(mutant_source)
        if mutant_stripped == original_stripped:
            continue

        # 4. Bytecode dedup — reject identical to original
        if _bytecode_equal(original_code, mutant_code):
            continue

        # 5. Deduplicate against other extra mutants
        fingerprint = _code_fingerprint(mutant_code)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)

        # Find the mutation line by diffing source lines
        orig_lines = source.splitlines()
        mut_lines = mutant_source.splitlines()
        line = 1
        col = 0
        for i, (ol, ml) in enumerate(zip(orig_lines, mut_lines)):
            if ol != ml:
                line = i + 1
                for j, (oc, mc) in enumerate(zip(ol, ml)):
                    if oc != mc:
                        col = j
                        break
                break

        src_line = mut_lines[line - 1].strip() if line <= len(mut_lines) else ""

        mutant = Mutant(
            operator="extra",
            description=desc,
            line=line,
            col=col,
            source_line=src_line,
            qualname=_qualname_for_line(_qualname_ranges(mutant_tree), line),
            _mutant_source=mutant_source,
        )
        results.append((mutant, mutant_tree))

    return results
# ============================================================================
# LLM-automated mutant generation (convenience wrapper over extra_mutants)
# ============================================================================

#: Prompt template for LLM mutant generation.  Follows Meta ACH pattern:
#: show the source, ask for realistic bugs, forbid trivial/comment-only changes.
_LLM_MUTANT_PROMPT = """\
Given this Python function:

```python
{source}
```
{concern_block}
Generate {n} mutated versions that each introduce a single, subtle, realistic bug — \
the kind a developer might introduce that passes code review but causes failures in production.

Types of bugs to introduce (vary across mutants):
- Off-by-one errors (< vs <=, wrong range bound)
- Wrong variable used (similar names swapped)
- Missing edge case handling (None, empty, zero, negative)
- Incorrect operator (+/-, and/or, ==/is)
- Swapped arguments in a function call
- Wrong return value on one code path
- Missing or extra negation
- Incorrect default value
- Type coercion error (int vs float, str vs bytes)

Rules:
- Each mutant must be the COMPLETE function (same signature, same name)
- Change only 1-2 lines per mutant — subtle, not obvious
- Do NOT add or change comments
- Do NOT change the function signature
- Do NOT make trivially detectable changes (like always returning None)
- Do NOT import new modules

Output format — use exactly this delimiter between mutants:

---MUTANT---
Description: <one line: what changed and why it is a realistic bug>
```python
<the full mutated function>
```
"""
_LLM_EQUIVALENCE_PROMPT = """\
Are these two Python functions semantically equivalent? \
That is, do they produce the same output for ALL possible inputs?

Original:
```python
{original}
```

Mutant:
```python
{mutant}
```

Answer with exactly YES or NO on the first line, then a one-sentence reason.
"""
def _parse_llm_response(response: str) -> list[tuple[str, str]]:
    """Parse LLM response into ``(description, source_code)`` pairs.

    Expects ``---MUTANT---`` delimiters separating blocks, each with a
    ``Description:`` line and a fenced Python code block.
    """
    import re

    blocks = re.split(r"---\s*MUTANT\s*---", response)
    results: list[tuple[str, str]] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        desc_match = re.search(r"Description:\s*(.+?)(?:\n|$)", block)
        desc = desc_match.group(1).strip() if desc_match else "LLM-generated mutant"
        code_match = re.search(r"```(?:python)?\s*\n(.*?)```", block, re.DOTALL)
        if code_match:
            results.append((desc, code_match.group(1).strip()))
    return results
def _generate_llm_mutants(
    source: str,
    llm: Callable[[str], str],
    *,
    n: int = 5,
    concern: str | None = None,
) -> list[tuple[Mutant, ast.Module]]:
    """Generate mutants via an LLM, with full validation pipeline.

    Convenience wrapper: calls *llm* to produce source strings, then
    feeds them through :func:`_validate_extra_mutants`.  The LLM is just
    one way to produce extra mutants — an AI assistant or human can also
    pass source strings directly via ``extra_mutants``.

    Args:
        source: The original function source code.
        llm: Callable that takes a prompt string and returns a response string.
        n: Number of mutants to request from the LLM.  Default ``5``.
        concern: Optional free-text concern description (Meta ACH pattern).
            When provided, the LLM is asked to generate mutations that
            target this specific concern (e.g. "privacy: user data should
            not leak into error messages").
    """
    concern_block = ""
    if concern:
        concern_block = (
            f"\nCONCERN: {concern}\n"
            "Focus mutations on bugs that would manifest this concern. "
            "The mutations should be realistic instances of this class of fault.\n"
        )
    prompt = _LLM_MUTANT_PROMPT.format(source=source, n=n, concern_block=concern_block)
    try:
        response = llm(prompt)
    except Exception:
        return []  # LLM failure is non-fatal

    parsed = _parse_llm_response(response)
    if not parsed:
        return []

    # Feed through the same validation as extra_mutants
    validated = _validate_extra_mutants(source, parsed)
    # Relabel operator as "llm" to distinguish from hand-written extras
    for mutant, _ in validated:
        mutant.operator = "llm"
    return validated
def _is_llm_equivalent(
    original_source: str,
    mutant_source: str,
    llm: Callable[[str], str],
) -> bool:
    """Ask an LLM whether a mutant is semantically equivalent to the original.

    Used as an additional equivalence filter for surviving mutants.  The LLM
    sees both versions and answers YES (equivalent, skip) or NO (genuine
    gap, keep).  Falls back to ``False`` (keep the mutant) on any error.

    Evidence: Meta ACH achieved 0.95 precision / 0.96 recall with this
    approach after trivial preprocessing (ISSTA 2024).
    """
    prompt = _LLM_EQUIVALENCE_PROMPT.format(original=original_source, mutant=mutant_source)
    try:
        response = llm(prompt)
    except Exception:
        return False  # on error, assume not equivalent (safe default)

    first_line = response.strip().split("\n")[0].strip().upper()
    return first_line.startswith("YES")
