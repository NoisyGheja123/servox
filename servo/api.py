# Copyright 2022 Cisco Systems, Inc. and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import copy
import enum
from typing import Any, Dict, List, Optional, Union, TYPE_CHECKING

import curlify2
import httpx
import pydantic

import servo
import servo.errors
import servo.types
import servo.utilities

if TYPE_CHECKING:
    from pydantic.typing import DictStrAny

USER_AGENT = "github.com/opsani/servox"


class OptimizerStatuses(str, enum.Enum):
    """An enumeration of status types sent by the optimizer."""

    ok = "ok"
    invalid = "invalid"
    unexpected_event = "unexpected-event"
    cancelled = "cancel"


class ServoStatuses(str, enum.Enum):
    """An enumeration of status types sent from the servo."""

    ok = "ok"
    failed = "failed"
    rejected = "rejected"
    aborted = "aborted"
    cancelled = "cancelled"


Statuses = Union[OptimizerStatuses, ServoStatuses]


class Reasons(str, enum.Enum):
    success = "success"
    unknown = "unknown"
    unstable = "unstable"


class Events(str, enum.Enum):
    hello = "HELLO"
    whats_next = "WHATS_NEXT"
    describe = "DESCRIPTION"
    measure = "MEASUREMENT"
    adjust = "ADJUSTMENT"
    goodbye = "GOODBYE"


class Commands(str, enum.Enum):
    describe = "DESCRIBE"
    measure = "MEASURE"
    adjust = "ADJUST"
    sleep = "SLEEP"

    @property
    def response_event(self) -> Events:
        if self == Commands.describe:
            return Events.describe
        elif self == Commands.measure:
            return Events.measure
        elif self == Commands.adjust:
            return Events.adjust
        else:
            raise ValueError(f"unknown command: {self}")


class Request(pydantic.BaseModel):
    event: Union[Events, str]  # TODO: Needs to be rethought -- used adhoc in some cases
    param: Optional[Dict[str, Any]]  # TODO: Switch to a union of supported types

    class Config:
        json_encoders = {
            Events: lambda v: str(v),
        }


class Status(pydantic.BaseModel):
    status: Statuses
    message: Optional[str] = None
    reason: Optional[str] = None
    state: Optional[Dict[str, Any]] = None
    descriptor: Optional[Dict[str, Any]] = None

    @classmethod
    def ok(
        cls, message: Optional[str] = None, reason: str = Reasons.success, **kwargs
    ) -> "Status":
        """Return a success (status="ok") status object."""
        return cls(status=ServoStatuses.ok, message=message, reason=reason, **kwargs)

    @classmethod
    def from_error(cls, error: servo.errors.BaseError) -> "Status":
        """Return a status object representation from the given error."""
        if isinstance(error, servo.errors.AdjustmentRejectedError):
            status = ServoStatuses.rejected
        elif isinstance(error, servo.errors.EventAbortedError):
            status = ServoStatuses.aborted
        elif isinstance(error, servo.errors.EventCancelledError):
            status = ServoStatuses.cancelled
        else:
            status = ServoStatuses.failed

        return cls(status=status, message=str(error), reason=error.reason)

    def dict(
        self,
        *,
        exclude_unset: bool = True,
        **kwargs,
    ) -> DictStrAny:
        return super().dict(exclude_unset=exclude_unset, **kwargs)


class SleepResponse(pydantic.BaseModel):
    pass


# SleepResponse '{"cmd": "SLEEP", "param": {"duration": 60, "data": {"reason": "no active optimization pipeline"}}}'

# Instructions from servo on what to measure
class MeasureParams(pydantic.BaseModel):
    metrics: List[str]
    control: servo.types.Control

    @pydantic.validator("metrics", always=True, pre=True)
    @classmethod
    def coerce_metrics(cls, value) -> List[str]:
        if isinstance(value, dict):
            return list(value.keys())

        return value

    @pydantic.validator("metrics", each_item=True, pre=True)
    def _map_metrics(cls, v) -> str:
        if isinstance(v, servo.Metric):
            return v.name

        return v


class CommandResponse(pydantic.BaseModel):
    command: Commands = pydantic.Field(alias="cmd")
    param: Optional[
        Union[MeasureParams, Dict[str, Any]]
    ]  # TODO: Switch to a union of supported types, remove isinstance check from ServoRunner.measure when done

    class Config:
        json_encoders = {
            Commands: lambda v: str(v),
        }


def descriptor_to_adjustments(descriptor: dict) -> List[servo.types.Adjustment]:
    """Return a list of adjustment objects from an Opsani API app descriptor."""
    adjustments = []
    for component_name, component in descriptor["application"]["components"].items():
        for setting_name, attrs in component["settings"].items():
            adjustment = servo.types.Adjustment(
                component_name=component_name,
                setting_name=setting_name,
                value=attrs["value"],
            )
            adjustments.append(adjustment)
    return adjustments


def adjustments_to_descriptor(
    adjustments: List[servo.types.Adjustment],
) -> Dict[str, Any]:
    components = {}
    descriptor = {"state": {"application": {"components": components}}}

    for adjustment in adjustments:
        if not adjustment.component_name in components:
            components[adjustment.component_name] = {"settings": {}}

        components[adjustment.component_name]["settings"][adjustment.setting_name] = {
            "value": adjustment.value
        }

    return descriptor


def is_fatal_status_code(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code < 500:
            servo.logger.error(
                f"Giving up on non-retryable HTTP status code {error.response.status_code} ({error.response.reason_phrase}) "
            )
            return True
    return False


def user_agent() -> str:
    return f"{USER_AGENT} v{servo.__version__}"


def redacted_to_curl(request: httpx.Request) -> str:
    """Pass through to curlify2.to_curl that redacts the authorization in the headers"""
    if (auth_header := request.headers.get("authorization")) is None:
        return curlify2.to_curl(request)

    req_copy = copy.copy(request)
    req_copy.headers = copy.deepcopy(request.headers)
    if "Bearer" in auth_header:
        req_copy.headers["authorization"] = "Bearer [REDACTED]"
    else:
        req_copy.headers["authorization"] = "[REDACTED]"

    return curlify2.to_curl(req_copy)
