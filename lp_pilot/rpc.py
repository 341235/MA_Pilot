"""Read-only JSON-RPC client with caching and request budgets."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .data import DataContractError, POOL, RequestBudget

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
SELECTORS = {
    "token0": "0x0dfe1681", "token1": "0xd21220a7", "fee": "0xddca3f43",
    "tickSpacing": "0xd0c93a7c", "decimals": "0x313ce567", "slot0": "0x3850c7bd",
    "liquidity": "0x1a686502", "feeGrowthGlobal0X128": "0xf3058399",
    "feeGrowthGlobal1X128": "0x46141319", "tickBitmap": "0x5339c296", "ticks": "0xf30dba93",
}
READ_METHODS = {"eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_call", "eth_getLogs", "eth_getTransactionReceipt", "eth_getTransactionByHash"}


class RpcError(RuntimeError):
    """RPC error safe to include in logs."""


# Official Alchemy standard JSON-RPC CU schedule, checked 2026-10-22.
# This is an estimate, not a billing statement or account-quota inspection.
METHOD_CU = {"eth_chainId": 0, "eth_blockNumber": 10, "eth_call": 26,
             "eth_getLogs": 60, "eth_getBlockByNumber": 20,
             "eth_getTransactionReceipt": 20, "eth_getTransactionByHash": 20}


def _historical(params: list[Any]) -> bool:
    return any((isinstance(value, str) and value.startswith("0x") and len(value) < 20)
               or (isinstance(value, dict) and "blockHash" in value)
               for value in params)


class RpcClient:
    def __init__(self, url: str, *, cache_dir: str | Path | None = None,
                 timeout: float = 10, max_requests: int = 20,
                 max_bytes: int = 25_000_000, retries: int = 1, session: Any = None,
                 max_compute_units: int | None = None, cu_per_second: float = 0):
        if not url.startswith(("https://", "http://")):
            raise RpcError("RPC endpoint must be HTTP(S)")
        if cu_per_second < 0 or (max_compute_units is not None and max_compute_units < 0):
            raise RpcError("RPC compute-unit limits must be nonnegative")
        self._url = url
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout = timeout
        self.retries = min(max(retries, 0), 2)
        self.budget = RequestBudget(max_requests=max_requests, max_bytes=max_bytes)
        self.session = session or requests.Session()
        self.max_compute_units = max_compute_units
        self.used_compute_units = 0
        self.cu_per_second = cu_per_second
        self._next_request_at = 0.0
        self.http_requests = 0
        self.cache_hits = 0
        self.method_counts: dict[str, int] = {}
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def statistics(self) -> dict:
        return {"logical_requests": self.budget.used_requests, "http_requests": self.http_requests,
                "downloaded_bytes": self.budget.used_bytes,
                "estimated_compute_units": self.used_compute_units, "cache_hits": self.cache_hits,
                "method_counts": dict(self.method_counts),
                "limits": {"requests": self.budget.max_requests, "bytes": self.budget.max_bytes,
                           "estimated_compute_units": self.max_compute_units,
                           "estimated_cu_per_second": self.cu_per_second},
                "cu_note": "Method-cost estimate; not observed provider billing or remaining account quota"}

    def _validate(self, method: str, params: list[Any]) -> None:
        if method not in READ_METHODS:
            raise RpcError("Only explicitly supported read-only RPC methods are allowed")
        if method == "eth_getLogs":
            if len(params) != 1 or not isinstance(params[0], dict):
                raise RpcError("Log calls require one filter object")
            criteria = params[0]
            if "blockHash" not in criteria:
                try:
                    first, last = int(criteria["fromBlock"], 16), int(criteria["toBlock"], 16)
                except (KeyError, ValueError, TypeError):
                    raise RpcError("Log calls require an explicit bounded historical block interval") from None
                if first < 0 or last < first or last - first > 100:
                    raise RpcError("RPC log interval exceeds the 101-block pilot cap")

    def _cache_path(self, method: str, params: list[Any], use_cache: bool) -> Path | None:
        cacheable = method in {"eth_getTransactionReceipt", "eth_getTransactionByHash", "eth_getLogs"} or _historical(params)
        key = hashlib.sha256(json.dumps([self._url, method, params], sort_keys=True).encode()).hexdigest()
        return self.cache_dir / (key + ".json") if use_cache and self.cache_dir and cacheable else None

    def evidence_reference(self, method: str, params: list[Any]) -> dict:
        """Locate a cached response without including the endpoint or key."""
        path = self._cache_path(method, params, True)
        if path is None or not path.is_file():
            raise RpcError("Saved RPC response evidence is unavailable")
        return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def _reserve(self, methods: list[str]) -> None:
        units = sum(METHOD_CU[method] for method in methods)
        if self.max_compute_units is not None and self.used_compute_units + units > self.max_compute_units:
            raise RpcError("RPC estimated compute-unit budget exhausted")
        if self.budget.used_requests + len(methods) > self.budget.max_requests:
            raise RpcError("Network request budget exhausted")
        for method in methods:
            self.budget.claim()
            self.method_counts[method] = self.method_counts.get(method, 0) + 1
        self.used_compute_units += units
        if self.cu_per_second:
            now = time.monotonic()
            delay = max(0.0, self._next_request_at - now)
            if delay:
                time.sleep(delay)
            self._next_request_at = max(now, self._next_request_at) + max(units, 5 * len(methods)) / self.cu_per_second

    def _post(self, payload: Any, methods: list[str]) -> Any:
        for attempt in range(self.retries + 1):
            try:
                self._reserve(methods)
                self.http_requests += 1
                response = self.session.post(self._url, json=payload, timeout=self.timeout, stream=True)
                response.raise_for_status()
                parts = []
                for chunk in response.iter_content(65536):
                    self.budget.consume(len(chunk))
                    parts.append(chunk)
                return json.loads(b"".join(parts))
            except (requests.RequestException, json.JSONDecodeError) as exc:
                status = exc.response.status_code if isinstance(exc, requests.HTTPError) and exc.response is not None else None
                retryable = status is None or status == 429 or status >= 500
                if attempt >= self.retries or not retryable:
                    status_note = f", HTTP {status}" if status is not None else ""
                    raise RpcError(f"RPC transport failure ({type(exc).__name__}{status_note}); endpoint redacted") from None
                time.sleep(0.25 * (2 ** attempt))
            except DataContractError as exc:
                raise RpcError(str(exc)) from None
        raise RpcError("RPC request failed")

    @staticmethod
    def _result(body: Any, method: str) -> Any:
        if not isinstance(body, dict):
            raise RpcError("Malformed JSON-RPC response")
        if body.get("error"):
            code = body["error"].get("code", "unknown") if isinstance(body["error"], dict) else "unknown"
            if not isinstance(code, int) or isinstance(code, bool):
                code = "unknown"
            raise RpcError(f"{method} RPC error code {code}; provider message redacted")
        if "result" not in body:
            raise RpcError(f"{method} missing JSON-RPC result")
        return body["result"]

    @staticmethod
    def _save(cached: Path | None, method: str, params: list[Any], result: Any) -> None:
        if cached and result is not None:
            temp = cached.with_suffix(".tmp")
            temp.write_text(json.dumps({"method": method, "params": params,
                                       "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                       "result": result}, sort_keys=True) + "\n")
            temp.replace(cached)

    def call(self, method: str, params: list[Any] | None = None, *, use_cache: bool = True) -> Any:
        params = params or []
        self._validate(method, params)
        cached = self._cache_path(method, params, use_cache)
        if cached and cached.exists():
            self.cache_hits += 1
            return json.loads(cached.read_text())["result"]
        body = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, [method])
        if isinstance(body, dict) and "id" in body and (isinstance(body["id"], bool) or body["id"] != 1):
            raise RpcError("Single JSON-RPC response ID differs from request")
        result = self._result(body, method)
        self._save(cached, method, params, result)
        return result

    def batch_call(self, calls: list[tuple[str, list[Any]]], *, use_cache: bool = True) -> list[Any]:
        """Return batch results in request order, charging each subrequest.

        Batches contain at most ten requests. Reject missing, duplicate or
        unexpected IDs, and propagate any subrequest error.
        """
        if len(calls) > 10:
            raise RpcError("RPC batch exceeds ten logical requests")
        results: list[Any] = [None] * len(calls)
        pending = []
        for index, (method, params) in enumerate(calls):
            self._validate(method, params)
            cached = self._cache_path(method, params, use_cache)
            if cached and cached.exists():
                self.cache_hits += 1
                results[index] = json.loads(cached.read_text())["result"]
            else:
                pending.append((index, method, params, cached))
        if not pending:
            return results
        payload = [{"jsonrpc": "2.0", "id": index, "method": method, "params": params}
                   for index, method, params, _ in pending]
        body = self._post(payload, [method for _, method, _, _ in pending])
        if not isinstance(body, list) or len(body) != len(pending):
            raise RpcError("Incomplete JSON-RPC batch response")
        indexed = {}
        expected = {item[0] for item in pending}
        for response in body:
            response_id = response.get("id") if isinstance(response, dict) else None
            if not isinstance(response_id, int) or isinstance(response_id, bool) or response_id not in expected or response_id in indexed:
                raise RpcError("Invalid, duplicate or foreign JSON-RPC batch ID")
            indexed[response_id] = response
        for index, method, params, cached in pending:
            result = self._result(indexed[index], method)
            results[index] = result
            self._save(cached, method, params, result)
        return results

    def eth_call(self, address: str, selector: str, block: int | str | dict = "latest", argument: int | None = None) -> list[int]:
        if argument is not None:
            selector += format(argument % (1 << 256), "064x")
        block_tag = hex(block) if isinstance(block, int) else block
        result = self.call("eth_call", [{"to": address, "data": selector}, block_tag])
        if not isinstance(result, str) or not result.startswith("0x") or len(result[2:]) % 64 or len(result) <= 2:
            raise RpcError("Malformed or empty eth_call result")
        try:
            return [int(result[index:index + 64], 16) for index in range(2, len(result), 64)]
        except ValueError:
            raise RpcError("Non-hex eth_call result") from None


def _signed(value: int, bits: int) -> int:
    # ABI signed values are sign-extended across the whole 256-bit word.
    signed = value - (1 << 256) if value >= 1 << 255 else value
    if not -(1 << (bits - 1)) <= signed < 1 << (bits - 1):
        raise RpcError("ABI signed integer out of range")
    return signed


def verify_pool_identity(client: RpcClient, pool_address: str = POOL) -> dict[str, Any]:
    chain_id = int(client.call("eth_chainId"), 16)
    values: dict[str, Any] = {"chain_id": chain_id, "pool_address": pool_address.lower()}
    for name in ("token0", "token1", "fee", "tickSpacing"):
        result = client.eth_call(pool_address, SELECTORS[name])
        if len(result) != 1:
            raise RpcError("Unexpected pool immutable result shape")
        values[name] = "0x" + format(result[0], "040x") if name.startswith("token") else result[0]
    for index in (0, 1):
        values[f"decimals{index}"] = client.eth_call(values[f"token{index}"], SELECTORS["decimals"])[0]
    expected = {"chain_id": 1, "pool_address": POOL, "token0": USDC, "token1": WETH, "fee": 500, "tickSpacing": 10, "decimals0": 6, "decimals1": 18}
    values["status"] = "PASS" if all(values[key] == value for key, value in expected.items()) else "FAIL"
    values["evidence"] = "eth_chainId and current contract immutable/ERC20 decimals calls; does not establish historical state access"
    return values
