from os import getcwd
import ctypes
import logging
from beamphysics import ParticleGroup
from typing import Any

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:
    _libc = None
from lume.staged_model import InitialParticlesMixIn, FinalParticlesMixIn
from pytao import Tao
from lume_bmad.utils import (
    get_tao_output_variables,
    TAO_COMB_OUTPUT_UNITS,
    get_tao_comb_output_variables,
)
from lume.actions import ActionModel, ActionVariable
from lume_bmad.actions import TrackTypeAction, BeamAtElementVariable

logger = logging.getLogger(__name__)


class LUMEBmadModel(ActionModel, InitialParticlesMixIn, FinalParticlesMixIn):
    """
    Subclasses ActionModel to create a Bmad model given a tao init file.

    Attributes
    ----------
    device_to_element: dict[str, str]
        Mapping between device PV names and Bmad element names.
    element_to_device: dict[str, str]
        Mapping between Bmad element names and device PV names.
    control_variables: dict[str, Variable]
        Dictionary of control variables that can be read/written.
    read_only_variables: dict[str, Variable]
        Dictionary of read-only output variables.
    supported_variables: dict[str, Variable]
        Dictionary of all supported variables (control + read-only).

    """

    def __init__(
        self,
        tao: Tao,
        action_variables: list[ActionVariable],
        dump_locations: list[str] = [],
        comb_ds_save: float = 0.1,
    ):
        """
        Initialize the Bmad model.

        Arguments
        ---------
        tao: Tao
            Tao object initialized with the desired init file.
        action_variables: list[ActionVariable]
            List of action variables.
        dump_locations: list[str], optional
            List of element names at which to dump the beam distribution in addition to the start and end of the lattice.
        comb_ds_save: float, optional
            Length frequency of dumping tracked beam parameters for tao comb command. Default is 0.1 m.

        """
        self._state = {}
        super().__init__(simulator=tao, action_variables=action_variables)

        self.comb_ds_save = comb_ds_save
        logger.debug("Initializing LUMEBmadModel with comb_ds_save=%s", comb_ds_save)
        self.simulator.cmd(f"set beam comb_ds_save = {self.comb_ds_save}")

        # raise a warning if track_start is not the first element in the lattice
        # NOTE: the first element in self.tao.lat_list("*", "ele.name") is "BEGINNING", so we grab the second element for the check
        if self.tao.beam(0)["track_start"] != self.tao.lat_list("*", "ele.name")[1]:
            msg = (
                f"Warning: track_start is set to {self.tao.beam(0)['track_start']}, "
                f"which is not the first element in the lattice {self.tao.lat_list('*', 'ele.name')[1]}. "
                "This may lead to unexpected behavior if the initial particles "
                "are not properly initialized at the track_start element."
            )
            logger.warning(msg)

        # Add model parameters read_only_variables
        model_output_variables = get_tao_output_variables(self.simulator)

        for var in model_output_variables:
            self.register_action_variable(var)

        # add track_type variable to control variables to allow toggling between single particle and beam tracking
        self.register_action_variable(TrackTypeAction())

        # set dumping of beam distributions at the beginning and end of the lattice
        self._dump_locations = dump_locations
        elements = ",".join(dump_locations + ["BEGINNING", "END"])
        self.simulator.cmd(f"set beam saved_at = {elements}")

        # Register dynamic outputs that depend on the current tracking mode.
        self._refresh_dynamic_action_variables()

        # get initial state of the model
        self.update_state()

        self._initial_state = self._state.copy()
        logger.info(
            "Initialized LUMEBmadModel ",
        )

    def _refresh_dynamic_action_variables(self) -> None:
        """Synchronize comb and dumped-beam action variables with the current track type."""
        in_beam_mode = self.simulator.tao_global()["track_type"] == "beam"

        comb_names = list(TAO_COMB_OUTPUT_UNITS.keys())
        if in_beam_mode:
            for variable in get_tao_comb_output_variables(self.simulator):
                self.register_action_variable(variable)
        else:
            for parameter_name in comb_names:
                if parameter_name in self.supported_variables:
                    self.unregister_action_variable(parameter_name)

        for element_name in self._dump_locations:
            beam_variable_name = f"{element_name}_beam"
            if in_beam_mode:
                self.register_action_variable(
                    BeamAtElementVariable(
                        name=beam_variable_name, element_name=element_name
                    )
                )
            elif beam_variable_name in self.supported_variables:
                self.unregister_action_variable(beam_variable_name)

    def _get(self, names: list[str]) -> dict[str, Any]:
        """
        Internal method to retrieve current values for specified variables.

        Parameters
        ----------
        names : list[str]
            List of variable names to retrieve

        Returns
        -------
        dict[str, Any]
            Dictionary mapping variable names to their current values
        """
        return {name: self._state[name] for name in names}

    def _set(self, values: dict[str, Any]) -> None:
        """
        Internal method to set input variables and compute outputs.

        This method:
        1. Updates input variables in the state
        2. Performs calculations to update output variables
        3. Stores results in the state

        Parameters
        ----------
        values : dict[str, Any]
            Dictionary of variable names and values to set
        """
        logger.debug("_set called with keys: %s", list(values.keys()))

        # turn tao eager mode off to speed up setting multiple variables
        self.simulator.cmd("set global lattice_calc_on = F")

        # set control variables using their respective set methods
        try:
            super()._set(values)
        except Exception:
            logger.error("Error setting variables: %s")
            raise
        finally:
            # after setting all variables, turn eager mode back on
            self.simulator.cmd("set global lattice_calc_on = T")

        # update state with new input / output values
        self.update_state()

        # Return glibc heap fragmentation to OS after every simulation cycle.
        # pytao output buffers + numpy temporaries from update_state() fragment
        # the heap between live allocations; malloc_trim() compacts and returns
        # top-of-heap free pages via sbrk(-n).
        if _libc is not None:
            _libc.malloc_trim(0)

    def register_action_variable(self, variable: ActionVariable) -> None:
        """
        Register an action variable with the model.

        This method adds the variable to the supported variables and initializes its value in the state.

        Parameters
        ----------
        variable : ActionVariable
            The action variable to register
        """
        super().register_action_variable(variable)
        self._state[variable.name] = variable._get(self.simulator)

    def update_state(self) -> None:
        """
        Update the model state by reading all supported variables.
        """
        logger.debug(
            "Updating model state for %d supported variables",
            len(self.supported_variables),
        )
        for name in self.supported_variables.keys():
            try:
                self._state[name] = self.supported_variables[name]._get(self.simulator)
            except Exception:
                logger.error("Error getting variable %s", name)
                raise

        logger.debug("Model state update complete")

    @property
    def start_element(self):
        """name of element at which beam is initialized for tracking"""
        return (
            self.simulator.beam(0)["track_start"]
            if self.simulator.beam(0)["track_start"] != ""
            else self.simulator.lat_list("*", "ele.name")[0]
        )

    @property
    def end_element(self):
        """name of element at which beam is tracked to"""
        return (
            self.simulator.beam(0)["track_end"]
            if self.simulator.beam(0)["track_end"] != ""
            else self.simulator.lat_list("*", "ele.name")[-1]
        )

    @property
    def initial_particles(self) -> ParticleGroup:
        """initial particle distribution for tracking"""
        if self.simulator.tao_global()["track_type"] == "beam":
            return self.simulator.particles(self.start_element)
        else:
            return None

    @property
    def tao(self) -> Tao:
        """access to the underlying Tao simulator object"""
        return self.simulator

    @initial_particles.setter
    def initial_particles(self, particles: ParticleGroup):
        """set the initial particle distribution for tracking"""
        if self.simulator.tao_global()["track_type"] == "beam":
            fname = getcwd() + "/input_beam.h5"
            logger.debug("Setting initial particles from %s", fname)
            particles.write(fname)
            self.simulator.cmd(f"set beam_init position_file = {fname}")

            # Refresh dynamic action variables to reflect new particle distribution.
            # NOTE: comb_ds_save is static config set once at __init__ — not per-cycle.
            # NOTE: update_state() omitted here; LUMEBmadModel._set() calls it right after.
            self._refresh_dynamic_action_variables()
        else:
            raise ValueError(
                "Cannot set initial_particles when track_type is not 'beam'"
            )

    @property
    def final_particles(self) -> ParticleGroup:
        """final particle distribution after tracking"""
        if self.simulator.tao_global()["track_type"] == "beam":
            return self.simulator.particles(self.end_element)
        else:
            return None

    def reset(self):
        """Reset the model to its initial state."""
        logger.info("Resetting model to initial state")

        # get subset of initial state that corresponds to writable control variables
        control_variable_names = [
            name
            for name, var in self.supported_variables.items()
            if var.read_only is False
        ]
        # set initial control state, excluding beam-at-element variables since they are not writable
        initial_control_state = {
            name: self._initial_state[name]
            for name in control_variable_names
            if not isinstance(self.supported_variables[name], BeamAtElementVariable)
        }

        self.set(initial_control_state)
