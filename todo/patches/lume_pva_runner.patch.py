import logging
import math
import os
import platform
import threading
import time
from collections.abc import Callable
from enum import IntEnum, StrEnum
from queue import Empty, Queue
from typing import Any, TypedDict

import p4p.client.thread
import p4p.server
import pcaspy
import pcaspy.cas
import pvua
from lume.model import LUMEModel, Variable
from lume.variables import ConfigEnum, ParticleGroupVariable
from p4p import Type, Value
from p4p.client.thread import Subscription
from p4p.nt import NTEnum, NTScalar
from p4p.server import ServerOperation
from p4p.server.thread import SharedPV

from lume_pva.variables import VariableHandler, find_variable_handler

try:
    from ._version import version as lume_pva_version
except ImportError:
    lume_pva_version = "HEAD"

LOG = logging.getLogger("LumePva")
logging.getLogger("pcaspy").setLevel(logging.WARNING)


class ModelState(IntEnum):
    Idle = 0
    Simulating = 1


class PutMode(StrEnum):
    Immediate = "immediate"
    Complete = "complete"


class VariableMode(StrEnum):
    RO = "ro"
    RW = "rw"
    Remote = "remote"


class ModelMode(StrEnum):
    Continuous = "continuous"
    Snapshot = "snapshot"


DEFAULT_MODEL_MODE = ModelMode.Continuous
DEFAULT_PV_MODE = VariableMode.RW


class RunnerVariable(TypedDict):
    """
    Attributes
    ----------
    name : str
        Name of the input or output. Must match one of the model's supported variables.
    pv : str
        Name of the PV to serve or consume. If not provided, it will be defaulted to 'name'
    mode : str
        Operation mode of the PV. May be one of:
        - 'ro': Read-only PV served by this server
        - 'rw': Read-write PV served by this server. Errors if Variable.read_only
        - 'remote': Remote PV living on some other remote machine.
        Default is 'rw'
    """

    name: str
    pv: str
    mode: str


class RunnerConfig(TypedDict):
    """
    Attributes
    ----------
    remote_model_mode : str
        Remote model mode. Determines behavior of this model's remote PVs.
        - 'continuous': Remote input PVs are updated continuously with PV monitors, and model is evaluated on change.
        - 'snapshot': Remote input PVs are only updated when the 'SNAPSHOT' PV is poked.
        Default is 'continuous'
    prefix : str
        Additional prefix to append to PV names. May be None if you don't need any.
        Remote PVs are unaffected by this setting, this only applies to PVs we are serving.
    variables : Dict[str, RunnerVariable]
        List of model variables
    protocol : list[str]
        List of supported protocols
    put_mode : PutMode
        Determines the behavior of put requests.
        'immediate' (PutMode.Immediate) will ACK the PUT request immediately after inserting into the queue.
        'complete' (PutMode.Complete) will ACK the PUT request only after the model has finished simulating.
    """

    remote_model_mode: str
    prefix: str
    variables: dict[str, RunnerVariable]
    protocol: list[str]
    put_mode: PutMode


class Runner:
    """Simple runner for LUMEModel derived models"""

    pvs: dict[str, SharedPV]
    ca_pvs: dict[str, str]
    pv_handlers: dict[str, VariableHandler]
    # List of all output PVs that need to be updated after simulation
    outputs: list[str]
    values: dict[str, Value]
    subs: dict[str, Subscription]

    class Handler:
        """
        Handles PUT and RPC requests to a specific PV
        """

        model: LUMEModel
        variable: Variable

        def __init__(
            self,
            variable: Variable,
            handler: VariableHandler,
            runner: "Runner",
            read_only: bool,
            put_mode: PutMode,
        ):
            self.model = runner.model
            self.variable = variable
            self.runner = runner
            self.ro = read_only
            self.handler = handler
            self.put_mode = put_mode

        def put(self, pv: SharedPV, op: ServerOperation):
            if self.ro:
                op.done(error="Read only PV")
            else:
                # Validate values and reject bad values immediately
                try:
                    self.variable.validate_value(
                        self.handler.unpack_value(self.variable, op.value()),
                        config=ConfigEnum.ERROR,
                    )
                except Exception as e:
                    LOG.warning(f"{self.variable.name}: Rejected invalid value: {e!s}")
                    op.done(error=str(e))
                    return

                # Update PVs in simulator
                self.runner._enqueue(
                    {self.variable.name: {"value": op.value(), "ts": time.time()}},
                    done=lambda error: op.done(error=error),
                )
                pv.post(op.value())
                if self.put_mode == PutMode.Immediate:
                    op.done()
                LOG.debug(f"Setting PVA: {self.variable.name} -> {op.value()}")

        def rpc(self, op: ServerOperation):
            op.done()

    class CaDriver(pcaspy.Driver):
        """ChannelAccess driver handling operations on behalf of the Runner class"""

        def __init__(self, runner: "Runner"):
            super().__init__()
            self.runner = runner

        def write(self, reason, value) -> bool:
            if reason == self.runner.snapshot_control_pv:
                self.runner.take_snapshot()
                self.setParam(reason, value)
                self.callbackPV(reason)
                return True

            if reason == self.runner.reset_control_pv:
                self.runner._enqueue({}, reset=True)
                self.setParam(reason, value)
                self.callbackPV(reason)
                return True

            # Lookup variable based on name
            vn = self.runner.pv_to_var[reason]
            if vn is None:
                return False

            var: Variable = self.runner.model.supported_variables.get(vn, None)
            if var is None:
                raise NameError(f"No variable named {vn} associated with pv {reason}")

            # Reject writes to read-only PVs
            if var.read_only:
                return False

            nv = value

            # Transform int -> str for enums. Must be done before we submit it to the variable queue
            desc = self.runner.pvdb[reason]
            if desc.get("type") == "enum":
                # Check range
                if value < 0 or value >= len(desc["enums"]):
                    LOG.info(f"{reason}: Rejected invalid enum value {value} for")
                    return False
                nv = desc["enums"][value]

            # Insert into update queue
            def _complete_put():
                self.setParam(reason, value)
                self.callbackPV(reason)

            # Validate values before updating the model
            try:
                var.validate_value(nv, ConfigEnum.ERROR)
            except Exception as e:
                # Doesn't seem to be a way to reject CA updates with an error message.
                LOG.warning(f"{reason}: Rejected invalid value: {e!s}")
                self.callbackPV(reason)
                return False

            self.runner._enqueue(
                {vn: {"value": nv, "ts": time.time()}},
                done=lambda error: _complete_put(),
            )

            if self.runner.put_mode == PutMode.Immediate:
                _complete_put()
            return True

    def __init__(
        self,
        model: LUMEModel,
        prefix="",
        config: RunnerConfig | None = None,
    ):
        """
        Init a Runner for the specified model

        Parameters
        ----------
        model : LUMEModel
            A LUMEModel object implementing the LUMEModel interface
        prefix: str
            Prefix to append to PV names. Only applies to PVs served by the Runner
        config: RunnerConfig|None
            Configuration for this runner. If 'None' a default configuration is generated.
            Note that you may call Runner.generate_config yourself to get+modify a configuration.
            Overrides the 'prefix' parameter.
        """
        self.model = model
        self.pvs = {}
        self.pv_handlers = {}
        self.queue = Queue()
        self.new_values = {}
        self.outputs = []
        self.types = {}
        self.subs = {}
        self.context = p4p.client.thread.Context()
        self.providers: dict[str, SharedPV] = {}  # Just for renaming
        self.pvdb = {}  # For pcaspy
        self.snapshot_pvs = []
        self.pv_to_var: dict[str, str] = {}  # Map pv name -> variable name
        self.var_to_pv = {}
        self.ca_pvs = {}
        self.snapshot_control_pv = ""
        self.reset_control_pv = ""
        self.pvua_context = pvua.Context()
        self.ca_server: pcaspy.SimpleServer | None = None
        self.ca_driver: Runner.CaDriver | None = None

        # Cache for previous state, value per name
        self._cached_state: dict[str, Any] = {}

        # Cache of pre-built Value objects per PV name, reused each post() cycle to
        # avoid allocating a new C++ PVStructure (~180 allocations/cycle otherwise).
        self._cached_pv_values: dict[str, Value] = {}

        # Generate default config
        if config is None:
            config = self.generate_config(model, prefix)
        self._config = config

        # Grab list of supported protocols
        self.protos = self._config.get("protocol", ["ca", "pva"])
        self.supports_ca = "ca" in self.protos
        self.supports_pva = "pva" in self.protos

        # Validate some configuration options
        if config.get("remote_model_mode", DEFAULT_MODEL_MODE) not in ModelMode:
            raise KeyError(
                f"Model has invalid model mode {config['remote_model_mode']}. Must be one of {', '.join([x.name for x in ModelMode])}"
            )

        self.update_rate = config.get("update_rate", 0.1)

        # Configure CA environment
        os.environ["EPICS_CA_MAX_ARRAY_BYTES"] = self.config.get("max_array_bytes", 80000000)

        # Setup PVs
        for c in self.config["variables"].values():
            # Set default PV name if not provided
            if "pv" not in c:
                c["pv"] = c["name"]

            # Lookup variable based on name
            var = self.model.supported_variables.get(c["name"])

            # Validate some other things first
            if var is None:
                raise KeyError(f'Variable "{c["name"]}" not found in model variables')
            if "mode" in c and c["mode"] not in VariableMode:
                raise KeyError(
                    f'Variable "{c["name"]} has invalid mode "{c["mode"]}". Must be one of {", ".join([x.name for x in VariableMode])}'
                )

            # Determine a default mode, if there is none
            if "mode" not in c:
                c["mode"] = VariableMode.RO if var.read_only else VariableMode.RW

            # Validate r/w setting
            if c["mode"] == VariableMode.RW and var.read_only:
                raise ValueError(
                    f"Variable {c['name']} was configured with read-write permissions, but the variable is read-only"
                )

            handler = find_variable_handler(type(var))
            if handler is None:
                if isinstance(var, ParticleGroupVariable):
                    continue  # ParticleGroupVariable is a special case that doesn't have a handler
                raise RuntimeError(f'Unknown type "{type(var)}"')

            # Skip unsupported variable types
            if not handler.is_supported(var):
                LOG.warning(f'Unsupported variable "{var.name}". Skipping.')
                continue

            # Remap the PV name. Prefix is applied only to standalone inputs and outputs, since clients are usually
            # consuming remote data that doesn't need to be made unique.
            if c["mode"] == "remote":
                pv = c["pv"]
            else:
                pv = self.config.get("prefix", "") + c["pv"]

            # Cache handler and type for later
            self.pv_handlers[var.name] = handler
            self.types[var.name] = handler.create_type(var)

            # Map pv -> var (and vice versa)
            self.pv_to_var[pv] = var.name
            self.var_to_pv[var.name] = pv

            if c["mode"] in [VariableMode.RO, VariableMode.RW]:
                # Generate a PV to be served
                self._add_pv(
                    pv,
                    var,
                    ro=c["mode"] == VariableMode.RO,
                    handler=handler,
                )
            else:
                # Create a client monitor
                self._add_client(
                    pv,
                    var,
                    monitor=self.config["remote_model_mode"]
                    == ModelMode.Continuous,  # Use monitor if in continuous mode
                )

        # Create an informational PV (i.e. including list of variables, etc.)
        # Only supported for PVA since it uses structures
        if self.supports_pva:
            self._create_model_info()

        # Create additional control PVs
        self._create_control_pvs()

        # Start the server
        self.server = p4p.server.Server(providers=[self.providers])

        # Start the CA server under the shared async context
        if len(self.pvdb.keys()) > 0:
            self.ca_server = pcaspy.SimpleServer()
            self.ca_server.createPV("", self.pvdb)
            self.ca_driver = Runner.CaDriver(self)

            # Spin up a thread to run the pcaspy update loop
            self.ca_thread = threading.Thread(target=self._run_pcaspy, daemon=True)

            self.ca_thread.start()

        # Kick off an initial update to propagate any defaults the model may have set
        self._enqueue({})

    def _run_pcaspy(self):
        """Run pcaspy forever"""
        while True:
            self.ca_server.process(0.1)

    @staticmethod
    def generate_config(
        model: LUMEModel,
        prefix: str = "",
        remote_inputs: bool = False,
        name_transformer: Callable[[Variable, str], str] | None = None,
        put_mode: PutMode = PutMode.Immediate,
    ) -> RunnerConfig:
        """
        Generate a configuration for the specified model.

        Parameters
        ----------
        model : LUMEModel
            Instance of a LUMEModel object
        prefix : str
            PV name prefix
        remote_inputs : bool
            When true, model inputs (values not marked as rw) are configured as monitors for remote variables
        name_transformer: Callable[[Variable, str], str] | None
            A callable that transforms a variable's name into a new PV name. by default it just maps variable.name -> pv_name
        put_mode : PutMode
            Determines the behavior of put requests.
            'immediate' (PutMode.Immediate) will ACK the PUT request immediately after inserting into the queue.
            'complete' (PutMode.Complete) will ACK the PUT request only after the model has finished simulating.
            For PutComplete mode, client timeouts must be adjusted to account for model simulation time.
        Returns
        -------
        RunnerConfig :
            A new configuration built based on the supplied parameters. May be tweaked as you wish before
            passing to the Runner() constructor.
        """
        config = {
            "description": "",
            "remote_model_mode": ModelMode.Continuous,
            "prefix": prefix,
            "max_array_bytes": os.environ.get("EPICS_CA_MAX_ARRAY_BYTES", "80000000"),
            "put_mode": put_mode,
            "update_rate": 0.1,
            "variables": {},
        }
        for k, v in model.supported_variables.items():
            mode = VariableMode.RO if v.read_only else VariableMode.RW
            if remote_inputs and not v.read_only:
                mode = VariableMode.Remote
            if name_transformer is not None:
                pv = name_transformer(v, v.name)
            else:
                pv = k
            config["variables"][k] = {
                "name": k,
                "pv": pv,
                "mode": mode,
            }
        return config

    def _enqueue(
        self,
        values: dict[str, Any],
        done: Callable[[str | None], None] | None = None,
        reset: bool = False,
    ) -> None:
        """
        Enqueue a batch of PV updates to be applied to the model.

        Parameters
        ----------
        values : Dict[str, Any]
            Mapping of variable name -> {"value": ..., "ts": ...}
        done : Callable[[str | None], None] | None
            Optional completion callback. Invoked once the simulation that
            consumes these values has finished (or failed). Receives an error
            string on failure, or None on success. Used to defer signalling
            put-completion to clients until results are actually available.
        reset : bool
            When true, request model.reset() before applying this batch.
        """
        self.queue.put(
            {
                "values": values,
                "done": [done] if done is not None else [],
                "reset": reset,
            }
        )

    def _add_pv(self, pv: str, var: Variable, ro: bool, handler: VariableHandler) -> None:
        """
        Create a new PV for CA and/or PVA

        Parameters
        ----------
        pv : str
            Name of the PV
        var : Variable
            LUME variable this PV is implementing
        ro : bool
            True if read-only
        prefix : str
            String to prefix the PV name with
        handler : VariableHandler
            The variable handler for this variable type
        """
        if self.supports_pva:
            LOG.debug(f"Creating PVA PV: {pv}")
            initial_val = self._generate_value(var.name, None)
            self._cached_pv_values[var.name] = initial_val
            pvobj = SharedPV(
                handler=Runner.Handler(
                    variable=var,
                    runner=self,
                    read_only=ro,
                    handler=handler,
                    put_mode=self.put_mode,
                ),
                initial=initial_val,
            )
            self.pvs[var.name] = pvobj
            self.providers[pv] = pvobj

        if self.supports_ca:
            # Generate a default value suitable for pcaspy
            default_value = handler.default_value(var, flatten=True, native_python=True)

            # String arrays are not really supported in channel access. Skip it.
            if isinstance(default_value, list) and isinstance(default_value[0], str):
                return

            LOG.debug(f"Creating CA PV: {pv}")
            spec = handler.ca_pvspec(var)

            self.pvdb[pv] = spec
            # enable async for put-completion
            self.pvdb[pv].update({"asyn": True})
            self.ca_pvs[var.name] = pv

    def _add_client(self, pv: str, var: Variable, monitor: bool) -> bool:
        """Setup a new monitor for the specified PV"""
        if monitor:
            self.subs[pv] = self.pvua_context.monitor(pv, self._monitor_callback)
        else:
            self.snapshot_pvs.append(pv)
        return True

    def _create_model_info(self):
        """Creates a model info PV for PVA"""
        pv = "model_info"

        envs = [
            "EPICS_CA_ADDR_LIST",
            "EPICS_CA_AUTO_ADDR_LIST",
            "EPICS_CA_SERVER_PORT",
            "EPICS_CA_CONN_TMO",
            "EPICS_CA_MAX_ARRAY_BYTES",
            "EPICS_CA_REPEATER_PORT",
            "EPICS_PVA_ADDR_LIST",
            "EPICS_PVA_AUTO_ADDR_LIST",
            "EPICS_PVA_SERVER_PORT",
            "EPICS_PVA_CONN_TMO",
            "EPICS_PVA_BROADCAST_PORT",
        ]

        self.types[pv] = Type(
            [
                ("class", "s"),
                ("description", "s"),
                ("lume_pva_version", "s"),
                ("hostname", "s"),
                (
                    "env",
                    ("S", None, [(x, "s") for x in envs]),
                ),
                (
                    "supported_variables",
                    (
                        "aS",
                        None,
                        [
                            ("name", "s"),
                            ("pvname", "s"),
                            ("type", "s"),
                            ("read_only", "?"),
                            ("mode", "s"),
                        ],
                    ),
                ),
            ]
        )

        val = Value(self.types[pv])
        val["class"] = self.model.__class__.__name__
        val["description"] = self.config["description"]
        val["lume_pva_version"] = lume_pva_version
        val["hostname"] = platform.node()

        for e in envs:
            val["env"][e] = os.environ.get(e, "")

        vars = []
        for k, v in self.model.supported_variables.items():
            info = {
                "name": v.name,
                "read_only": v.read_only,
                "pvname": self.config["variables"][k]["pv"],
                "type": v.__class__.__name__,
                "mode": self.config["variables"][k]["mode"],
            }
            vars.append(info)

        val["supported_variables"] = vars

        self.pvs[pv] = SharedPV(initial=val)
        self.providers[f"{self.config['prefix']}{pv}"] = self.pvs[pv]

    def _create_control_pvs(self):
        """Create any required control PVs"""
        # Create a snapshot PV and a reset PV. These are used to trigger a snapshot of remote PVs, and to reset the model.
        snapshot_pvname = f"{self.config['prefix']}SNAPSHOT"
        self.snapshot_control_pv = snapshot_pvname

        reset_pvname = f"{self.config['prefix']}RESET"
        self.reset_control_pv = reset_pvname

        status_control_pvname = f"{self.config['prefix']}STATUS"
        self.status_control_pv = status_control_pvname

        def conflict_check(x):
            if x in self.pvdb or x in self.providers:
                raise RuntimeError(f"Fatal name conflict: {x} already exists in the PV database!")

        [conflict_check(x) for x in [snapshot_pvname, reset_pvname, status_control_pvname]]

        # Create PVA shared PVs for snapshot and reset if PVA is enabled
        if self.supports_pva:
            self.providers[snapshot_pvname] = SharedPV(initial=NTScalar("d").wrap(0))

            @self.providers[snapshot_pvname].put
            def onPut(pv, op):
                self.take_snapshot()
                op.done()

            self.providers[reset_pvname] = SharedPV(initial=NTScalar("i").wrap(0))

            @self.providers[reset_pvname].put
            def onResetPut(pv, op):
                self._enqueue(
                    {},
                    reset=True,
                )
                op.done()

            self.status_control_pva_enum = NTEnum()
            self.providers[status_control_pvname] = SharedPV(
                initial=self.status_control_pva_enum.wrap(
                    0, timestamp=time.time(), choices=list(ModelState.__members__.keys())
                )
            )

            @self.providers[status_control_pvname].put
            def onStatusPut(pv, op):
                op.done(error="Read-only PV")

        # Create CA PVs for snapshot and reset if CA is enabled
        if self.supports_ca:
            self.pvdb[snapshot_pvname] = {
                "type": "int",
                "value": 0,
                "asyn": False,
            }
            self.pvdb[reset_pvname] = {
                "type": "int",
                "value": 0,
                "asyn": False,
            }
            self.pvdb[status_control_pvname] = {"type": "enum", "enums": ["Idle", "Simulating"]}

        return None

    def take_snapshot(self) -> None:
        """
        Take a snapshot of the remote PVs, and simulate the model
        """
        LOG.debug(f"Snapshot taken for PVs: {self.snapshot_pvs}")
        new_values = {}
        for pv in self.snapshot_pvs:
            new_values[self.pv_to_var[pv]] = {
                "value": self.pvua_context.get(pv),
                "ts": time.time(),
            }
        self._enqueue(new_values)

    def _monitor_callback(self, pvname, value, **kwargs):
        """Callback from p4p monitor updates"""
        self._enqueue({self.pv_to_var[pvname]: {"value": value, "ts": time.time()}})

    def _generate_value(self, pv: str, value: Any | None, ts: float | None = None) -> Value:
        """
        Returns a Value ready to be posted to the PV.

        On the first call (startup / no cached Value yet) a new Value is allocated via
        pack_value.  On every subsequent call the cached Value is mutated in-place via
        update_value, avoiding a C++ PVStructure allocation per post() cycle (~180/cycle
        at normal operation).
        """
        cached = self._cached_pv_values.get(pv)
        if cached is None:
            v = self.pv_handlers[pv].pack_value(
                self.model.supported_variables[pv], self.types[pv], value
            )
            self._cached_pv_values[pv] = v
        else:
            self.pv_handlers[pv].update_value(
                cached, self.model.supported_variables[pv], value
            )
            v = cached

        self._update_timestamp(v, ts=ts)
        return v

    def _update_timestamp(self, value: Value, ts=None) -> None:
        """
        Helper to update timestamp on a value
        """
        if ts is None:
            ts = time.time()
        value["timeStamp"]["nanoseconds"] = math.fmod(ts, 1.0) * 1e9
        value["timeStamp"]["secondsPastEpoch"] = int(ts)

    @property
    def config(self) -> RunnerConfig:
        """Access the underlying config"""
        return self._config

    def _run(self):
        """
        Runs the simulation, blocks forever.
        Dequeues PV updates from the updater thread, sets values on the model, and updates outputs.
        """
        while True:
            # Wait for new data to come in
            item = self.queue.get()

            value_data: dict = item["values"]
            done_callbacks: list = item["done"]
            reset_requested: bool = item.get("reset", False)

            # Wait for a time window of 'update_rate' seconds to pass before continuing
            until = time.monotonic() + self.update_rate
            while time.monotonic() < until:
                try:
                    next_update = self.queue.get_nowait()
                    value_data.update(next_update["values"])
                    if next_update.get("done") is not None:
                        done_callbacks.extend(next_update["done"])
                    reset_requested = reset_requested or next_update.get("reset", False)
                except Empty:
                    pass

            self._set_model_state(ModelState.Simulating)

            new_values = {}
            latest_ts = 0.0
            for k, g in value_data.items():
                v = g["value"]
                ts = g["ts"]

                # Record newest timestamp from the inputs
                if ts > latest_ts:
                    latest_ts = ts

                # If needed, unpack value and add it to the new list of PVs
                if isinstance(v, Value):
                    new_values[k] = self.pv_handlers[k].unpack_value(
                        self.model.supported_variables[k], v
                    )
                else:
                    new_values[k] = v

            # Use current time if we're missing a latest timestamp
            if latest_ts <= 0:
                latest_ts = time.time()

            # Stash previous state
            settable_var_names = [
                key for key, var in self.model.supported_variables.items() if not var.read_only
            ]
            self._set_cached_state(self.model.get(settable_var_names))

            # Set and simulate
            sim_error = None
            try:
                if reset_requested:
                    reset_start = time.perf_counter()
                    LOG.info("Reset requested through RESET control PV")
                    self.model.reset()
                    LOG.debug(
                        f"Model reset() took {(time.perf_counter() - reset_start) * 1000.0:.3f} ms"
                    )

                set_start = time.perf_counter()
                LOG.debug(f"Setting model with new values: {new_values}")
                self.model.set(new_values)
                LOG.debug(f"Model set() took {(time.perf_counter() - set_start) * 1000.0:.3f} ms")

                # Get new simulated values
                get_start = time.perf_counter()
                out_values = self.model.get(self.model.supported_variables)
                LOG.debug(f"Model get() took {(time.perf_counter() - get_start) * 1000.0:.3f} ms")

                # Update output PVs with new values
                pv_update_start = time.perf_counter()
                LOG.debug(f"writing {len(out_values)} PVs")
                for k, v in out_values.items():
                    # Avoid attempting to post to client monitors
                    if k in self.subs:
                        continue

                    # The model my return None for an output. Let's reject those variable updates.
                    if v is None:
                        LOG.warning(f"Model returned None for output '{k}'; skipping update")
                        continue

                    # Update PVA component
                    if (pv := self.pvs.get(k)) is not None:
                        try:
                            pv.post(self._generate_value(k, v, latest_ts))
                        except Exception as e:
                            LOG.error(f"Error posting value for {k}: {e}")

                    # Update CA component
                    if (capv := self.ca_pvs.get(k)) is not None and self.ca_driver is not None:
                        # pcaspy can only understand native python types, not necessarily what the model gives us.
                        nv = self.pv_handlers[k].value_to_native(
                            self.model.supported_variables[k], v
                        )

                        self.ca_driver.setParam(
                            capv,
                            nv,
                            pcaspy.cas.epicsTimeStamp.fromPosixTimeStamp(latest_ts),
                        )

                if self.ca_driver is not None:
                    self.ca_driver.updatePVs()

                LOG.debug(
                    f"PV update loop took {(time.perf_counter() - pv_update_start) * 1000.0:.3f} ms"
                )
            except Exception as exc:
                sim_error = str(exc)
                LOG.error(f"Simulation Cycle Failed: ({sim_error}), resetting to cached value")
                self._reset_to_cached_state()
            finally:
                # With simulation completed, signal put completion to any waiting clients.
                for cb in done_callbacks if self.put_mode == PutMode.Complete else []:
                    try:
                        cb(sim_error)
                    except Exception as excp:
                        LOG.error(f"Error signalling put-completion: {excp}")
                self._set_model_state(ModelState.Idle)

    def run(self):
        """
        Runs the simulation, blocks forever (until there's a keyboard interrupt)
        Dequeues PV updates from the updater thread, sets values on the model, and updates outputs.
        """
        try:
            self._run()
        except KeyboardInterrupt:
            return
        except Exception as e:
            raise e

    def _reset_to_cached_state(self) -> None:
        """Apply cached values to the model"""
        LOG.debug(f"Resetting model with new values: {self._cached_state}")
        self.model.set(self._cached_state)

    def _set_cached_state(self, state: dict[str, Any]) -> None:
        """Save `state` to the cache"""
        LOG.debug(f"Caching model state: {state}")
        self._cached_state = state

    @property
    def put_mode(self) -> PutMode:
        return self.config.get("put_mode", PutMode.Immediate)

    def _set_model_state(self, state: ModelState):
        """Updates the model state and related PVs"""
        if (pv := self.providers.get(self.status_control_pv)) is not None:
            pv.post(self.status_control_pva_enum.wrap(state, timestamp=time.time()))
        if self.status_control_pv in self.pvdb.keys():
            self.ca_driver.setParam(
                self.status_control_pv,
                state,
                timestamp=pcaspy.cas.epicsTimeStamp.fromPosixTimeStamp(time.time()),
            )
            self.ca_driver.updatePV(self.status_control_pv)
