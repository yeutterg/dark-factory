"""Versioned JSON contracts for named integration commands."""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = 1
REQUIRED_RESULT_FIELDS = ("ok", "operation", "evidence")


class ContractError(ValueError):
    pass


def make_request(
    operation: str, payload: dict[str, Any], request_id: str
) -> dict[str, Any]:
    if not operation or not isinstance(operation, str):
        raise ContractError("operation must be a non-empty string")
    return {
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "operation": operation,
        "input": payload,
    }


def validate_request(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or (
        type(data.get("contract_version")) is not int
        or data.get("contract_version") != CONTRACT_VERSION
    ):
        raise ContractError("unsupported request contract version")
    if not isinstance(data.get("request_id"), str) or not data["request_id"]:
        raise ContractError("request_id required")
    if not isinstance(data.get("operation"), str) or not data["operation"]:
        raise ContractError("operation required")
    if not isinstance(data.get("input"), dict):
        raise ContractError("input must be an object")
    return data


def validate_result(
    data: Any, operation: str, request_id: str | None = None
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContractError("result must be a JSON object")
    if (
        type(data.get("contract_version")) is not int
        or data.get("contract_version") != CONTRACT_VERSION
    ):
        raise ContractError("unsupported result contract version")
    if request_id is not None and data.get("request_id") != request_id:
        raise ContractError("request identity mismatch")
    for field in REQUIRED_RESULT_FIELDS:
        if field not in data:
            raise ContractError(f"result missing {field!r}")
    if not isinstance(data["ok"], bool):
        raise ContractError("ok must be a boolean")
    if data.get("operation") != operation:
        raise ContractError("operation identity mismatch")
    evidence = data["evidence"]
    if not isinstance(evidence, dict):
        raise ContractError("evidence must be an object")
    if data["ok"] and not evidence:
        raise ContractError("successful result must include evidence")
    if "observations" in data and not isinstance(data["observations"], list):
        raise ContractError("observations must be a list")
    return data
