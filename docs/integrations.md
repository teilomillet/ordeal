# Integrations

## Atheris (coverage-guided fuzzing)

```bash
pip install ordeal[atheris]
```

Atheris (Google's libFuzzer for Python) drives `buggify()` decisions with coverage feedback:

```python
from ordeal.integrations.atheris_engine import fuzz

def my_function():
    data = get_input()
    if buggify():
        data = corrupt(data)
    process(data)

fuzz(my_function, max_time=60)
```

Each `buggify()` call consumes bytes from Atheris's fuzzer. Coverage feedback steers toward fault combinations that reach new code.

For ChaosTest exploration:

```python
from ordeal.integrations.atheris_engine import fuzz_chaos_test
fuzz_chaos_test(MyServiceChaos, max_time=300)
```

## Schemathesis (API chaos testing)

```bash
pip install ordeal[api]
```

Inject faults while Schemathesis exercises every API endpoint:

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

Or as a decorator for more control:

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
