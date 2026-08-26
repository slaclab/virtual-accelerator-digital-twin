from __future__ import annotations

import logging
import operator
from abc import ABC, abstractmethod
from functools import reduce
from typing import Any, Generic, TypeVar

import numpy as np
from lume.variables import (
    BoolVariable,
    EnumVariable,
    IntVariable,
    NDVariable,
    ScalarVariable,
    StrVariable,
    Variable,
)
from numpy import ndarray
from p4p import Type, Value
from p4p.nt import NTEnum, NTNDArray, NTScalar

from lume_pva.epics import epicsAlarmSeverity, epicsAlarmStatus

# torch and lume-torch are optional; the Torch* variable types are only
# supported when the 'torch' extra is installed.
try:
    import torch
    from lume_torch.variables import TorchNDVariable, TorchScalarVariable

    TORCH_AVAILABLE = True
except ImportError:
    torch = None
    TorchNDVariable = None
    TorchScalarVariable = None
    TORCH_AVAILABLE = False

logger = logging.getLogger(__name__)

# torch dtype -> NTNDArray typecode. Empty when torch is not installed; torch dtypes
# cannot be referenced in match patterns without torch present, hence the lookup table.
_TORCH_TYPECODES = (
    {}
    if not TORCH_AVAILABLE
    else {
        torch.float64: "doubleValue",
        torch.float32: "floatValue",
        torch.int8: "byteValue",
        torch.bool: "booleanValue",
        torch.int16: "shortValue",
        torch.int32: "intValue",
        torch.int64: "longValue",
        torch.uint8: "ubyteValue",
        torch.uint16: "ushortValue",
        torch.uint32: "uintValue",
        torch.uint64: "ulongValue",
    }
)

_NUMPY_TYPECODES = {
    np.dtype(np.float64): "doubleValue",
    np.dtype(np.float32): "floatValue",
    np.dtype(np.byte): "byteValue",
    np.dtype(np.bool): "booleanValue",
    np.dtype(np.int16): "shortValue",
    np.dtype(np.int32): "intValue",
    np.dtype(np.int64): "longValue",
    np.dtype(np.ubyte): "ubyteValue",
    np.dtype(np.uint16): "ushortValue",
    np.dtype(np.uint32): "uintValue",
    np.dtype(np.uint64): "ulongValue",
    np.dtype(np.str_): "stringValue",
    np.dtypes.StringDType(): "stringValue",
}


VariableT = TypeVar("VariableT", bound=Variable)


class VariableHandler(ABC, Generic[VariableT]):
    """Base class for all variable type handlers"""

    def __init__(self):
        pass

    def create_type(self, variable: VariableT) -> Type:
        """
        Creates p4p Type describing the Variable

        Parameters
        ----------
        variable : Variable
            The variable

        Returns
        -------
        Type :
            A p4p type describing the variable's value and other properties
        """
        raise NotImplementedError()

    @abstractmethod
    def pack_value(self, variable: VariableT, type_: Type, value: Any | None) -> Value:
        """
        Generates a p4p Value type off of a Variable and its associated value

        Parameters
        ----------
        variable : Variable
            The variable. Must be a subclass of Variable
        value : Any | None
            The value. If None is specified, the default of the variable should be used instead.

        Returns
        -------
        Value :
            A fully constructed Value() type that may be posted by p4p
        """
        raise NotImplementedError()

    @abstractmethod
    def unpack_value(self, variable: VariableT, value: Value) -> Any:
        """
        Unpacks a p4p Value into the native Python type

        Parameters
        ----------
        variable : Variable
            The variable. Must be a subclass of Variable
        value : Value
            The value to unpack

        Returns
        -------
        Any :
            The unpacked value
        """
        raise NotImplementedError()

    def is_supported(self, variable: VariableT) -> bool:
        """
        Checks if a variable can be supported by the handler.
        Used to determine if the variable lives within the bounds of the handler, i.e. the variable's specific dtype being supported.

        Parameters
        ----------
        variable : Variable
            The variable. Must be a subclass of Variable

        Returns
        -------
        bool :
            True if the variable can be handled by this handler
        """
        return True

    @abstractmethod
    def default_value(
        self, variable: VariableT, flatten: bool = False, native_python: bool = False
    ) -> Any:
        """
        Fetches the default value for the Variable.
        This will always return a valid object that matches the requested dtype or underlying datatype
        of the variable.

        Parameters
        ----------
        variable : Variable
            The variable to generate a default for
        flatten : bool
            For N-dimensional types, flatten before returning
        native_python : bool
            Convert the type to a native python type (i.e. np.array -> list).
            Raises TypeError() if that is not possible.
        """
        raise NotImplementedError()

    @abstractmethod
    def value_to_native(self, variable: VariableT, value: Any) -> Any:
        """
        Performs fixups for the specified value so pcaspy can understand it. For most variable types, this function
        won't need to do anything (default impl is fine). For variable types dealing with Numpy or Tensor types, this
        function will need to convert to the appropriate native Python type.

        Parameters
        ----------
        variable : Variable
            The variable to pack for
        value : Any
            The value to pack
        """
        raise NotImplementedError()

    @abstractmethod
    def native_to_value(self, variable: VariableT, value: Any) -> Any:
        """
        Unpacks (converts) the native Python type to a type that underlying variable can understand.

        Parameters
        ----------
        variable : Variable
            The variable to pack for.
        value : Any
            The value to convert

        Returns
        -------
        Any :
            Converted value that can be accepted by `variable`
        """
        raise NotImplementedError()

    def update_value(self, v: Value, variable: VariableT, value: Any | None) -> None:
        """
        Mutate an existing Value in-place with a new simulation result.

        Avoids allocating a new C++ PVStructure on every post() cycle.
        Subclasses should override for efficiency; this fallback copies field-by-field
        from a freshly-packed Value (safe but still allocates once).
        """
        packed = self.pack_value(variable, v.type(), value)
        for key in packed.type().keys():
            try:
                v[key] = packed[key]
            except Exception:
                pass

    def ca_pvspec(self, variable: VariableT) -> dict:
        """
        Returns a dict of additional to be passed to pcaspy's PV database.
        Use this to set count, type, etc., if necessary.
        Default implementation returns an empty dict.

        Parameters
        ----------
        variable : Variable
            The variable to create the spec for

        Returns
        -------
        dict :
            List of args to be unpacked into PVSpec constructor
        """
        return {}


class ScalarVariableHandler(VariableHandler[ScalarVariable | IntVariable]):
    """Variable handler for LUME ScalarVariable, and the TorchScalarVariable type"""

    ScalarType = int | float | np.floating | np.integer

    @staticmethod
    def set_metadata(variable: Variable, v: Value, value: Any) -> None:
        """
        Sets control, display and alarm metadata on the value
        """
        value_range = getattr(variable, "value_range", None)
        if value_range is not None:
            v["control"]["limitLow"] = value_range[0]
            v["control"]["limitHigh"] = value_range[1]
            v["display"]["limitLow"] = value_range[0]
            v["display"]["limitHigh"] = value_range[1]

        unit = getattr(variable, "unit")
        if unit is not None:
            v["display"]["units"] = unit

        # This should arguably be moved somewhere else..but since value_range is specific to
        # variable types, we pretty much have to handle it here.
        # TODO: Could detect presence of limitLow/limitHigh in common code, and set based on that
        if value_range is not None:
            if value < value_range[0]:
                v["alarm"]["severity"] = int(epicsAlarmSeverity.MAJOR_ALARM)
                v["alarm"]["status"] = int(epicsAlarmStatus.DRIVER_STATUS)
            elif value > value_range[1]:
                v["alarm"]["severity"] = int(epicsAlarmSeverity.MAJOR_ALARM)
                v["alarm"]["status"] = int(epicsAlarmStatus.DRIVER_STATUS)
            else:
                v["alarm"]["severity"] = int(epicsAlarmSeverity.NO_ALARM)
                v["alarm"]["status"] = int(epicsAlarmStatus.NO_STATUS)

    def create_type(self, variable: ScalarVariable | IntVariable) -> Type:
        return NTScalar.buildType(
            "d" if isinstance(variable, ScalarVariable) else "l",
            control=True,
            display=True,
        )

    def pack_value(
        self,
        variable: ScalarVariable | IntVariable,
        type_: Type,
        value: ScalarType | None,
    ) -> Value:
        if value is None:  # Use default if not provided
            value = self.default_value(variable)

        # Force cast to int for int variables, otherwise we trip validation
        if isinstance(variable, IntVariable):
            value = int(value)

        variable.validate_value(value)

        v = Value(type_, {"value": float(value)})
        self.set_metadata(variable, v, value)
        return v

    def update_value(self, v: Value, variable: ScalarVariable | IntVariable, value: Any | None) -> None:
        if value is None:
            value = self.default_value(variable)
        if isinstance(variable, IntVariable):
            value = int(value)
        variable.validate_value(value)
        v["value"] = float(value)
        self.set_metadata(variable, v, value)

    def unpack_value(self, variable: ScalarVariable | IntVariable, value: Value) -> float | int:
        if isinstance(variable, IntVariable):
            return int(value["value"])
        else:
            return float(value["value"])

    def default_value(
        self,
        variable: ScalarVariable | IntVariable,
        flatten: bool = False,
        native_python: bool = False,
    ):
        v = variable.default_value if variable.default_value is not None else 0
        if isinstance(variable, IntVariable):
            return int(v)
        else:
            return float(v)

    def value_to_native(self, variable: ScalarVariable | IntVariable, value: ScalarType) -> Any:
        if isinstance(variable, IntVariable):
            return int(value)
        else:
            return float(value)

    def native_to_value(
        self, variable: ScalarVariable | IntVariable, value: float | int
    ) -> ScalarType:
        return value

    def ca_pvspec(self, variable: ScalarVariable | IntVariable) -> dict:
        value_range = getattr(variable, "value_range", (0, 0))
        value_range = (0, 0) if value_range is None else value_range
        if isinstance(variable, IntVariable):
            type_ = "int"
        else:
            type_ = "float"
        return {
            "unit": variable.unit,
            "type": type_,
            "lolim": value_range[0],
            "hilim": value_range[1],
            "lolo": value_range[0],
            "hihi": value_range[1],
        }


class NDVariableHandler(VariableHandler[NDVariable | TorchNDVariable]):
    """Variable handler for LUME NDVariable type"""

    def _typecode(self, variable: NDVariable | TorchNDVariable) -> str:
        typecode = _TORCH_TYPECODES.get(variable.dtype)
        if typecode is not None:
            return typecode

        typecode = _NUMPY_TYPECODES.get(variable.dtype)
        if typecode is not None:
            return typecode

        raise TypeError(f'{variable.name}: Unsupported type "{variable.dtype.__class__}"')

    def is_supported(self, variable: NDVariable | TorchNDVariable):
        """Checks if the variable has a supported dtype"""
        try:
            self._typecode(variable)
            return True
        except TypeError:
            return False

    def create_type(self, variable: NDVariable | TorchNDVariable) -> Type:
        # NTNDArray (per the NT spec) does not support string[] as a value type. We'll deviate from the standard a bit here
        if variable.dtype in [np.str_, np.dtypes.StringDType()]:
            extras = [
                ("value", ("U", None, [("stringValue", "as")])),
            ]
            return Type(extras, base=NTNDArray.buildType())
        else:
            return NTNDArray.buildType()

    def pack_value(
        self,
        variable: NDVariable | TorchNDVariable,
        type_: Type,
        value: ndarray | torch.Tensor | None,
    ) -> Value:
        if value is None:  # Use default if not provided
            value = self.default_value(variable)

        variable.validate_value(value)

        # Convert to numpy type for p4p's sake
        if TORCH_AVAILABLE and isinstance(variable, TorchNDVariable):
            value = value.numpy()

        v = Value(type_, {"value": (self._typecode(variable), value.flatten())})

        v["compressedSize"] = value.nbytes
        v["uncompressedSize"] = value.nbytes

        v["dimension"] = [
            {
                "size": dim,
                "fullSize": dim,  # No compression
                "binning": 1,
                "reverse": False,
                "offset": 0,
            }
            for dim in variable.shape
        ]

        return v

    def update_value(self, v: Value, variable: NDVariable | TorchNDVariable, value: Any | None) -> None:
        if value is None:
            value = self.default_value(variable)
        variable.validate_value(value)
        if TORCH_AVAILABLE and isinstance(variable, TorchNDVariable):
            value = value.numpy()
        v["value"] = (self._typecode(variable), value.flatten())
        v["compressedSize"] = value.nbytes
        v["uncompressedSize"] = value.nbytes

    def unpack_value(
        self, variable: NDVariable | TorchNDVariable, value: Value
    ) -> ndarray | torch.Tensor:
        arr = value["value"]
        if isinstance(arr, np.ndarray):
            if TORCH_AVAILABLE and isinstance(variable, TorchNDVariable):
                return torch.reshape(torch.from_numpy(arr), variable.shape)
            else:
                return arr.reshape(variable.shape)
        else:
            raise ValueError(f"Internal error: invalid value type {type(arr)}")

    def default_value(
        self,
        variable: NDVariable | TorchNDVariable,
        flatten: bool = False,
        native_python: bool = False,
    ) -> ndarray | torch.Tensor:
        value = variable.default_value
        if value is None:
            if variable.dtype in [np.str_, np.dtypes.StringDType()]:
                value = np.full(shape=(variable.shape), fill_value="", dtype=variable.dtype)
            elif TORCH_AVAILABLE and isinstance(variable, TorchNDVariable):
                value = torch.zeros(size=variable.shape, dtype=variable.dtype)
            elif isinstance(variable, NDVariable):
                value = np.zeros(shape=variable.shape, dtype=variable.dtype)
            else:
                raise TypeError()
        if flatten:
            value = value.flatten()
        if native_python:
            value = value.tolist()
        return value

    def value_to_native(
        self, variable: NDVariable | TorchNDVariable, value: ndarray | torch.Tensor
    ) -> list:
        return value.flatten().tolist()

    def native_to_value(
        self, variable: NDVariable | TorchNDVariable, value: list
    ) -> ndarray | torch.Tensor:
        if TORCH_AVAILABLE and isinstance(variable, TorchNDVariable):
            return torch.Tensor(value, size=variable.shape, dtype=variable.dtype)
        elif isinstance(variable, NDVariable):
            return np.array(value, dtype=variable.dtype).reshape(variable.shape)
        else:
            raise NotImplementedError()

    def ca_pvspec(self, variable: NDVariable | TorchNDVariable) -> dict:
        spec = {"count": reduce(operator.mul, variable.shape), "type": "int"}
        tc = self._typecode(variable)
        if tc in ["floatValue", "doubleValue"]:
            spec["type"] = "float"
        elif tc in ["stringValue"]:
            raise TypeError(f"Invalid type code {variable.dtype}. Cannot support string arrays.")
        return spec


if TORCH_AVAILABLE:

    class TorchScalarVariableHandler(VariableHandler[TorchScalarVariable]):
        """Handler for TorchScalarVariable. Only available when torch and lume-torch are installed."""

        TorchScalarType = torch.Tensor | float | int

        def create_type(self, variable: TorchScalarVariable) -> Type:
            return NTScalar.buildType("d", control=True, display=True)

        def pack_value(
            self,
            variable: TorchScalarVariable,
            type_: Type,
            value: TorchScalarType | None,
        ) -> Value:
            if value is None:  # Use default if not provided
                value = self.default_value(variable)

            variable.validate_value(value)

            v = Value(type_, {"value": float(value)})
            ScalarVariableHandler.set_metadata(variable, v, float(value))
            return v

        def update_value(self, v: Value, variable: TorchScalarVariable, value: Any | None) -> None:
            if value is None:
                value = self.default_value(variable)
            variable.validate_value(value)
            v["value"] = float(value)
            ScalarVariableHandler.set_metadata(variable, v, float(value))

        def unpack_value(self, variable: TorchScalarVariable, value: Value) -> float:
            return float(value["value"])

        def default_value(
            self,
            variable: TorchScalarVariable,
            flatten: bool = False,
            native_python: bool = False,
        ):
            return variable.default_value if variable.default_value is not None else 0.0

        def native_to_value(self, variable: TorchScalarVariable, value: float) -> TorchScalarType:
            return value

        def value_to_native(self, variable: TorchScalarVariable, value: TorchScalarType) -> float:
            return float(value)


class SimpleScalarHandler(VariableHandler[StrVariable | BoolVariable]):
    """Handler for StrVariable, BoolVariable"""

    def create_type(self, variable: StrVariable | BoolVariable):
        return NTScalar.buildType("s" if isinstance(variable, StrVariable) else "?")

    def pack_value(
        self,
        variable: StrVariable | BoolVariable,
        type_: Type,
        value: str | bool | None,
    ) -> Value:
        if value is None:
            value = self.default_value(variable)

        variable.validate_value(value)
        if isinstance(variable, StrVariable) and not isinstance(value, str):
            raise ValueError(f"StrVariable {variable.name} expects str, but got {type(value)}")

        if isinstance(variable, BoolVariable) and not isinstance(value, bool | int):
            raise ValueError(f"StrVariable {variable.name} expects str, but got {type(value)}")

        return Value(type_, {"value": value})

    def update_value(self, v: Value, variable: StrVariable | BoolVariable, value: Any | None) -> None:
        if value is None:
            value = self.default_value(variable)
        variable.validate_value(value)
        v["value"] = value

    def unpack_value(self, variable: StrVariable | BoolVariable, value: Value) -> str | bool:
        if isinstance(variable, BoolVariable):
            return bool(value["value"])
        else:
            return str(value["value"])

    def default_value(
        self,
        variable: StrVariable | BoolVariable,
        flatten: bool = False,
        native_python: bool = False,
    ) -> bool | str:
        if variable.default_value is not None:
            return variable.default_value
        elif isinstance(variable, BoolVariable):
            return False
        elif isinstance(variable, StrVariable):
            return ""
        else:
            raise TypeError("Unsupported variable type for SimpleScalarHandler")

    def value_to_native(self, variable: StrVariable | BoolVariable, value):
        return value

    def native_to_value(self, variable: StrVariable | BoolVariable, value):
        return value

    def ca_pvspec(self, variable: StrVariable | BoolVariable):
        if isinstance(variable, StrVariable):
            # Need to force record type and length for strings, otherwise default_value dictates the length.
            return {"type": "char", "count": 1024}
        else:
            return {}


class EnumVariableHandler(VariableHandler):
    """Handler for EnumVariable"""

    def create_type(self, variable: EnumVariable) -> Type:
        return NTEnum.buildType()

    def pack_value(self, variable: EnumVariable, type_: Type, value: int | str | None) -> Value:
        if value is None:
            value = self.default_value(variable)

        idx = value
        if isinstance(value, str):
            idx = variable.options.index(value)

        return Value(
            type_,
            {
                "value": {
                    "choices": variable.options,
                    "index": idx,
                }
            },
        )

    def update_value(self, v: Value, variable: EnumVariable, value: Any | None) -> None:
        if value is None:
            value = self.default_value(variable)
        idx = value
        if isinstance(value, str):
            idx = variable.options.index(value)
        v["value"]["choices"] = variable.options
        v["value"]["index"] = idx

    def unpack_value(self, variable: EnumVariable, value: Value) -> str:
        idx = value["value"]["index"]
        if idx > len(variable.options):
            raise IndexError("Index is out of range")
        return variable.options[idx]

    def default_value(
        self, variable: EnumVariable, flatten: bool = False, native_python: bool = False
    ) -> int:
        if variable.default_value is not None:
            return variable.default_value
        return variable.options[0]

    def value_to_native(self, variable: EnumVariable, value: str | int):
        if isinstance(value, str):
            return variable.options.index(value)
        return value

    def native_to_value(self, variable: EnumVariable, value: str | int):
        if isinstance(value, int):
            return variable.options[value]
        return value

    def ca_pvspec(self, variable: EnumVariable):
        return {
            "type": "enum",
            "enums": variable.options,
        }


def find_variable_handler(variable_type: type[Variable]) -> VariableHandler | None:
    VARIABLE_HANDLERS: dict[type[Variable], VariableHandler] = {
        ScalarVariable: ScalarVariableHandler(),
        IntVariable: ScalarVariableHandler(),
        NDVariable: NDVariableHandler(),
        BoolVariable: SimpleScalarHandler(),
        StrVariable: SimpleScalarHandler(),
        EnumVariable: EnumVariableHandler(),
    }
    if TORCH_AVAILABLE:
        VARIABLE_HANDLERS[TorchScalarVariable] = TorchScalarVariableHandler()
        VARIABLE_HANDLERS[TorchNDVariable] = NDVariableHandler()

    # Resolve using the class MRO so subclasses of supported variable types
    # automatically use their nearest matching base handler.
    for cls in variable_type.__mro__:
        handler = VARIABLE_HANDLERS.get(cls)
        if handler is not None:
            return handler

    return None
