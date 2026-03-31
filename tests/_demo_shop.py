"""Demo shop API for testing ordeal's schemathesis integration.

A small but realistic API with multiple handlers, shared state,
and dependency functions. NOT pre-designed for ordeal to catch
specific bugs — the handlers are written the way a developer
would actually write them, bugs and all.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Shared state (simulates a database)
# ---------------------------------------------------------------------------

_inventory: dict[str, int] = {"widget": 10, "gadget": 5, "doohickey": 0}
_orders: list[dict] = []
_next_order_id = 1


def reset_state() -> None:
    global _next_order_id
    _inventory.clear()
    _inventory.update({"widget": 10, "gadget": 5, "doohickey": 0})
    _orders.clear()
    _next_order_id = 1


# ---------------------------------------------------------------------------
# "Service layer" — functions the handlers call
# ---------------------------------------------------------------------------


def get_stock_level(item: str) -> int:
    """Look up stock for an item. Returns 0 for unknown items."""
    return _inventory.get(item, 0)


def place_new_order(item: str, quantity: int) -> dict:
    """Create an order and decrement inventory. Raises ValueError on insufficient stock."""
    global _next_order_id
    stock = get_stock_level(item)
    if stock < quantity:
        raise ValueError(f"insufficient stock: {stock} < {quantity}")
    _inventory[item] = stock - quantity
    order = {"id": _next_order_id, "item": item, "quantity": quantity, "status": "confirmed"}
    _orders.append(order)
    _next_order_id += 1
    return order


def get_order_by_id(order_id: int) -> dict | None:
    """Find an order by ID."""
    for o in _orders:
        if o["id"] == order_id:
            return o
    return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def list_stock(request: Request) -> JSONResponse:
    """GET /stock — list all stock levels."""
    return JSONResponse([{"item": k, "stock": v} for k, v in _inventory.items()])


async def get_stock(request: Request) -> JSONResponse:
    """GET /stock/{item} — get stock for one item."""
    item = request.path_params["item"]
    stock = get_stock_level(item)
    return JSONResponse({"item": item, "stock": stock})


async def create_order(request: Request) -> JSONResponse:
    """POST /orders — place an order."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected object"}, status_code=422)
    item = body.get("item")
    quantity = body.get("quantity")
    if not isinstance(item, str) or not isinstance(quantity, int):
        return JSONResponse({"error": "invalid types"}, status_code=422)
    if quantity <= 0:
        return JSONResponse({"error": "quantity must be > 0"}, status_code=422)
    try:
        order = place_new_order(item, quantity)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return JSONResponse(order, status_code=201)


async def get_order(request: Request) -> JSONResponse:
    """GET /orders/{order_id} — get an order."""
    order_id = int(request.path_params["order_id"])
    order = get_order_by_id(order_id)
    if order is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(order)


# ---------------------------------------------------------------------------
# Schema + App
# ---------------------------------------------------------------------------

OPENAPI_SCHEMA = {
    "openapi": "3.0.3",
    "info": {"title": "Shop", "version": "1.0"},
    "paths": {
        "/stock": {
            "get": {
                "operationId": "listStock",
                "responses": {
                    "200": {
                        "description": "All stock",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["item", "stock"],
                                        "properties": {
                                            "item": {"type": "string"},
                                            "stock": {"type": "integer", "minimum": 0},
                                        },
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
        "/stock/{item}": {
            "get": {
                "operationId": "getStock",
                "parameters": [
                    {
                        "name": "item",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Stock level",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["item", "stock"],
                                    "properties": {
                                        "item": {"type": "string"},
                                        "stock": {"type": "integer", "minimum": 0},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
        "/orders": {
            "post": {
                "operationId": "createOrder",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["item", "quantity"],
                                "properties": {
                                    "item": {"type": "string"},
                                    "quantity": {"type": "integer", "minimum": 1},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "Created",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["id", "item", "quantity", "status"],
                                    "properties": {
                                        "id": {"type": "integer"},
                                        "item": {"type": "string"},
                                        "quantity": {"type": "integer"},
                                        "status": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Invalid JSON"},
                    "409": {"description": "Insufficient stock"},
                    "422": {"description": "Validation error"},
                },
            }
        },
        "/orders/{order_id}": {
            "get": {
                "operationId": "getOrder",
                "parameters": [
                    {
                        "name": "order_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Order",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["id", "item", "quantity", "status"],
                                    "properties": {
                                        "id": {"type": "integer"},
                                        "item": {"type": "string"},
                                        "quantity": {"type": "integer"},
                                        "status": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "404": {"description": "Not found"},
                },
            },
        },
    },
}


async def openapi_schema(request: Request) -> JSONResponse:
    return JSONResponse(OPENAPI_SCHEMA)


app = Starlette(
    routes=[
        Route("/stock", list_stock),
        Route("/stock/{item}", get_stock),
        Route("/orders", create_order, methods=["POST"]),
        Route("/orders/{order_id:int}", get_order),
        Route("/openapi.json", openapi_schema),
    ]
)
