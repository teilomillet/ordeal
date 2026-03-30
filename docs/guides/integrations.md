# Integrations

## When to use integrations

ordeal's core -- ChaosTest, buggify, the Explorer -- works standalone with no extra dependencies beyond Hypothesis. Integrations extend it with specialized engines for specific problem domains:

- **Atheris**: when you want coverage-guided fuzzing at the byte level, steering buggify() decisions based on code coverage feedback. Best for protocol parsing, serialization logic, and complex input processing where the input space is vast and you need systematic exploration.
- **Schemathesis**: when you have an OpenAPI or GraphQL spec and want to chaos-test your API layer. Best for web services, REST APIs, and microservices where you want to verify that fault conditions do not produce crashes or inconsistent responses.

If you are testing a stateful service or data pipeline, the built-in Explorer is usually the right tool. Reach for integrations when you need deeper coverage (Atheris) or schema-driven API testing (Schemathesis).

---

## Atheris (coverage-guided fuzzing)

### Install

```bash
pip install ordeal[atheris]
```

### How it works

Atheris is Google's libFuzzer port for Python. It generates byte sequences and mutates them based on coverage feedback -- when a mutation reaches new code paths, the fuzzer remembers it and builds on it.

ordeal bridges Atheris to buggify(). Each buggify() call in your code consumes bytes from the fuzzer's input stream. The return value of buggify() (True or False) is determined by those bytes. Coverage feedback then steers the fuzzer toward byte sequences -- which correspond to specific fault combinations -- that reach new code paths.

The result: instead of random fault injection, Atheris systematically discovers which combinations of buggify() decisions lead to interesting behavior.

### When to use Atheris

Use Atheris when you need the deepest possible exploration. It is slower than the Explorer (each run has overhead from coverage instrumentation), but more systematic. It will find fault combinations that random exploration misses.

Best for:

- **Security-sensitive code**: parsers, deserializers, protocol handlers where a crash is a vulnerability.
- **Complex input processing**: functions with many conditional branches that depend on input structure.
- **Exhaustive validation**: when you need confidence that no combination of buggify() decisions triggers a bug.

For general-purpose chaos testing of services, the Explorer is faster and easier to configure. Reach for Atheris when the Explorer has stopped finding new edges and you want to go deeper.

### Limitations

- Requires the `ordeal[atheris]` extra. Atheris itself can be tricky to install on some platforms (it needs Clang for native extension compilation).
- Coverage instrumentation adds overhead. Expect 5-10x slower execution per test case compared to the Explorer.
- Atheris works at the function level. For stateful testing, use `fuzz_chaos_test` (see below), but note that it drives rule selection with less sophistication than Hypothesis's stateful engine.

### Fuzz a function with buggify

```python
from ordeal.integrations.atheris_engine import fuzz

def my_function():
    data = get_input()
    if buggify():
        data = corrupt(data)
    process(data)

fuzz(my_function, max_time=60)
```

Each buggify() call consumes bytes from the fuzzer's stream. Coverage feedback steers toward fault combinations that reach new code.

### Fuzz a ChaosTest class

```python
from ordeal.integrations.atheris_engine import fuzz_chaos_test
fuzz_chaos_test(MyServiceChaos, max_time=300)
```

In this mode, Atheris drives rule selection, fault toggling, and step counts. The fuzzer's byte stream determines which rules execute and when faults activate, with coverage feedback guiding exploration.

### Practical tips for Atheris

**Start with a time limit, not an iteration count.** Atheris explores breadth-first initially, then narrows as it finds interesting paths. 60 seconds is enough for most functions; security-critical code deserves 300s+.

**Combine with buggify gates.** The power of the Atheris integration is that each `buggify()` call consumes fuzzer bytes. More buggify gates = more decisions for the fuzzer to optimize. Place gates at every fault injection point:

```python
def parse_message(data: bytes) -> Message:
    if buggify():
        data = data[:len(data)//2]  # truncated input
    if buggify():
        data = data + b"\x00" * 10  # padded input
    header = parse_header(data[:8])
    if buggify():
        header.version = 0  # force old code path
    return decode_body(header, data[8:])
```

**Check the crash corpus.** Atheris saves inputs that caused crashes to a corpus directory. Replay them to verify fixes:

```bash
ls crash-*  # Atheris crash files
python -c "import mymodule; mymodule.parse(open('crash-abc123', 'rb').read())"
```

---

## Schemathesis (API chaos testing)

### Install

```bash
pip install ordeal[api]
```

### How it works

Schemathesis reads your OpenAPI or GraphQL schema and generates HTTP requests that exercise every endpoint, including edge cases like boundary values, missing fields, and unusual content types. ordeal wraps this with fault injection: while Schemathesis sends requests, ordeal randomly toggles faults on your backend.

The combination tests a question that neither tool answers alone: **does your API behave correctly when the backend is experiencing faults?** Schemathesis generates the traffic; ordeal creates the adverse conditions.

### What it catches

- **Endpoints that crash under fault conditions**: a database timeout causes an unhandled exception instead of a 503.
- **Inconsistent error responses**: some faults produce proper error JSON, others produce stack traces or empty bodies.
- **Data corruption through the API layer**: a fault during write causes partial state that subsequent reads expose.
- **Missing error handling**: faults in dependencies that the API layer does not catch or translate.

### Two patterns

**Quick one-liner with `chaos_api_test()`**: loads the schema, generates test cases for every endpoint, and randomly injects faults. Good for CI or quick validation.

```python
from ordeal.integrations.schemathesis_ext import chaos_api_test
from ordeal.faults import timing, io

chaos_api_test(
    "http://localhost:8080/openapi.json",
    faults=[
        timing.slow("myapp.db.query", delay=2.0),
        io.error_on_call("myapp.storage.save"),
    ],
)
```

**`@with_chaos` decorator for pytest integration**: gives you more control over assertions, fault selection, and test structure. Use this when you want to combine Schemathesis with your existing test suite.

```python
import schemathesis
from ordeal.integrations.schemathesis_ext import with_chaos

schema = schemathesis.from_uri("http://localhost:8080/openapi.json")

@schema.parametrize()
@with_chaos(faults=[timing.timeout("myapp.api.call")])
def test_api(case):
    response = case.call()
    case.validate_response(response)
```

The decorator randomly activates and deactivates faults before each API call, then resets them afterward to avoid cross-request interference.

### Practical tips for Schemathesis

**Your API must be running.** Schemathesis sends real HTTP requests, so start your server before running tests. In CI, use a Docker container or a test server:

```bash
# Start server in background
uvicorn myapp:app --port 8080 &
sleep 2

# Run chaos API tests
python -c "
from ordeal.integrations.schemathesis_ext import chaos_api_test
from ordeal.faults import timing, io
chaos_api_test(
    'http://localhost:8080/openapi.json',
    faults=[timing.slow('myapp.db.query', delay=2.0)],
)
"
```

**Choose faults that match real failure modes.** Don't just inject random faults — inject the ones your API actually encounters in production:

- Database slow/down: `timing.slow("myapp.db.query")`, `io.error_on_call("myapp.db.execute")`
- Upstream API failure: `timing.timeout("myapp.external.call")`, `io.return_empty("myapp.cache.get")`
- Storage issues: `io.disk_full()`, `io.permission_denied()`

**Check status codes, not just crashes.** A 500 Internal Server Error is a bug. But so is a 200 with corrupted data. Use the `@with_chaos` decorator when you need to assert on response content:

```python
@schema.parametrize()
@with_chaos(faults=[io.error_on_call("myapp.db.execute")])
def test_api_handles_db_failure(case):
    response = case.call()
    # Should return 503, not 500 or 200 with empty data
    assert response.status_code in (200, 503), f"Unexpected: {response.status_code}"
    if response.status_code == 200:
        assert response.json()  # body should not be empty
```

---

## Choosing between Explorer, Atheris, and Schemathesis

| | **Explorer** | **Atheris** | **Schemathesis** |
|---|---|---|---|
| **What it tests** | ChaosTest state machines | Functions or ChaosTests | API endpoints |
| **Guidance** | Edge-coverage hashing | Byte-level coverage (libFuzzer) | Schema-driven generation |
| **Best for** | Services, data pipelines, stateful systems | Parsers, security-critical code, serialization | Web services, REST APIs, microservices |
| **Speed** | Fast (no instrumentation overhead) | Slower (coverage instrumentation) | Depends on API response time |
| **Depth** | Good (energy scheduling, swarm mode) | Deepest (systematic byte mutation) | Broad (every endpoint, every parameter) |
| **Setup** | ordeal.toml + ChaosTest class | `pip install ordeal[atheris]` | `pip install ordeal[api]` + running API + schema |
| **Output** | Traces, JSON report, property results | Crash corpus (libFuzzer style) | Schemathesis failure report + ordeal faults |

**Rules of thumb**:

- Start with the Explorer. It works with any ChaosTest, requires no extra dependencies, and finds most bugs.
- Add Atheris when you need deeper coverage on specific functions, especially parsers or security-sensitive code.
- Add Schemathesis when you have an API with a schema and want to verify fault tolerance at the HTTP layer.
- All three can run in CI. The Explorer and Schemathesis produce structured reports; Atheris produces a crash corpus.
