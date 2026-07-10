from __future__ import annotations
# ruff: noqa
def mine_module(
    module: str | ModuleType,
    *,
    max_examples: int = 200,
    cross_max_examples: int = 30,
    mine_per_function: bool = True,
    ignore_properties: list[str] | tuple[str, ...] = (),
    ignore_relations: list[str] | tuple[str, ...] = (),
    property_overrides: dict[str, list[str]] | None = None,
    relation_overrides: dict[str, list[str]] | None = None,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> MineModuleResult:
    """Discover properties across an entire module — both per-function and cross-function.

    Single-function mining (via ``mine()``) finds properties like "output >= 0"
    or "deterministic".  Cross-function mining finds relationships that only
    exist *between* functions — the kind of properties that break during
    refactoring because no single unit test covers the contract.

    Three cross-function relationships are checked for every compatible pair:

    - **roundtrip**: ``g(f(x)) == x``.  Discovered when f's return type matches
      g's parameter type.  Classic examples: ``decode(encode(x))``,
      ``deserialize(serialize(x))``, ``decompress(compress(x))``.

    - **commutative_composition**: ``f(g(x)) == g(f(x))``.  Discovered when
      both functions accept and return the same type.  Examples: two
      normalization passes that can be applied in either order, or
      ``sort(reverse(xs)) == reverse(sort(xs))`` (which would *not* hold
      and produce a counterexample).

    - **equivalent**: ``f(x) == g(x)``.  Discovered when both functions
      accept the same input types.  Flags duplicate implementations,
      reference/optimized pairs, or accidental copies that should be
      consolidated.

    Because the number of pairs grows as O(n^2), ``cross_max_examples`` is
    kept low (default 30) to avoid combinatorial blowup.  For a module with
    10 functions there are 45 directed pairs; at 30 examples each that is
    1350 calls per relationship check — fast enough for CI.

    Args:
        module: Dotted module path (``"myapp.scoring"``) or an already-imported
            module object.
        max_examples: Examples per function for individual ``mine()`` calls.
        cross_max_examples: Examples per function pair for cross-function checks.
            Kept low because there are O(n^2) pairs.
        mine_per_function: If ``True`` (default), also run ``mine()`` on each
            function individually.  Set to ``False`` to only discover
            cross-function relationships.
        ignore_properties: Property names to suppress for every function.
        ignore_relations: Cross-function relation names to suppress.
        property_overrides: Per-function property suppressions.
        relation_overrides: Per-function relation suppressions.
        **fixtures: Strategy overrides or plain values passed through to
            ``mine()`` and ``_infer_strategies()``.

    Returns:
        A ``MineModuleResult`` containing per-function ``MineResult`` objects
        and a list of ``CrossFunctionProperty`` relationships.

    Example::

        result = mine_module("myapp.codecs")
        print(result.summary())
        # mine_module(myapp.codecs)
        #   4 functions, 2 cross-function relationships
        #
        #   mine(encode): 200 examples
        #     ALWAYS  output type is bytes (200/200)
        #     ...
        #
        #   Cross-function relationships:
        #     ALWAYS  encode <-> decode: roundtrip (30/30)
        #      97%    fast_encode <-> encode: equivalent (29/30)
    """
    if isinstance(module, str):
        mod = importlib.import_module(module)
        mod_name = module
    else:
        mod = module
        mod_name = getattr(mod, "__name__", str(mod))

    funcs = _get_public_functions(mod)

    # --- Per-function mining ---
    per_function: dict[str, MineResult] = {}
    if mine_per_function:
        for name, fn in funcs:
            try:
                per_function[name] = mine(
                    fn,
                    max_examples=max_examples,
                    ignore_properties=sorted(
                        {
                            *ignore_properties,
                            *(property_overrides or {}).get(name, []),
                        }
                    ),
                    **fixtures,
                )
            except (ValueError, TypeError):
                pass  # can't infer strategies — skip

    # --- Cross-function mining ---
    # Build a lookup of functions with their signatures resolved
    typed_funcs: list[tuple[str, Callable[..., Any]]] = []
    for name, fn in funcs:
        # Only include functions where we can infer at least the first param
        param_name, _param_type = _first_param_type(fn)
        if param_name is not None:
            typed_funcs.append((name, fn))

    cross_function: list[CrossFunctionProperty] = []
    ignored_relations = _suppressed_names(list(ignore_relations))

    for i, (fname, f) in enumerate(typed_funcs):
        for j, (gname, g) in enumerate(typed_funcs):
            if i == j:
                continue
            pair_ignored_relations = ignored_relations | _suppressed_names(
                [
                    *(relation_overrides or {}).get(fname, []),
                    *(relation_overrides or {}).get(gname, []),
                ]
            )

            # Roundtrip: g(f(x)) == x — directed, so check both (i,j) and (j,i)
            # Only check (i,j) direction here; (j,i) is checked when i/j swap
            if i < j:
                prop = _check_roundtrip(f, g, fname, gname, max_examples=cross_max_examples)
                if (
                    prop is not None
                    and prop.total > 0
                    and _normalize_property_token(prop.relation) not in pair_ignored_relations
                ):
                    cross_function.append(prop)

                prop = _check_roundtrip(g, f, gname, fname, max_examples=cross_max_examples)
                if (
                    prop is not None
                    and prop.total > 0
                    and _normalize_property_token(prop.relation) not in pair_ignored_relations
                ):
                    cross_function.append(prop)

            # Commutative composition: f(g(x)) == g(f(x)) — symmetric, check once
            if i < j:
                prop = _check_composition_commutativity(
                    f, g, fname, gname, max_examples=cross_max_examples
                )
                if (
                    prop is not None
                    and prop.total > 0
                    and _normalize_property_token(prop.relation) not in pair_ignored_relations
                ):
                    cross_function.append(prop)

            # Output equivalence: f(x) == g(x) — symmetric, check once
            if i < j:
                prop = _check_output_equivalence(
                    f, g, fname, gname, max_examples=cross_max_examples
                )
                if (
                    prop is not None
                    and prop.total > 0
                    and _normalize_property_token(prop.relation) not in pair_ignored_relations
                ):
                    cross_function.append(prop)

    return MineModuleResult(
        module=mod_name,
        per_function=per_function,
        cross_function=cross_function,
    )
