from abc import ABC, abstractmethod
from typing import Any

from beamphysics import ParticleGroup

from lume.model import LUMEModel
from lume.variables.variable import Variable


class InitialParticlesMixIn(ABC):
    """
    Mix in to LUMEModel to indicate support for initial particles.
    """

    @property
    @abstractmethod
    def initial_particles(self) -> ParticleGroup: ...

    @initial_particles.setter
    @abstractmethod
    def initial_particles(self, val: ParticleGroup): ...


class FinalParticlesMixIn(ABC):
    """
    Mix in to LUMEModel to indicate support for final particles.
    """

    @property
    @abstractmethod
    def final_particles(self) -> ParticleGroup: ...


class StagedModel(LUMEModel):
    """
    Composes multiple LUMEModel instances in sequence, passing final particles
    from each stage as initial particles to the next.
    """

    def __init__(self, lume_model_instances: list[LUMEModel]):
        """
        Parameters
        ----------
        lume_model_instances: list[LUMEModel]
            Ordered list of LUMEModel instances to stage.
        """
        super().__init__()
        self.validate_lume_model_instances(lume_model_instances)
        self.lume_model_instances = lume_model_instances

    @classmethod
    def validate_lume_model_instances(cls, models: list[LUMEModel]):
        """
        Parameters
        ----------
        models: list[LUMEModel]
            Models to validate for staging compatibility.
        """
        for i, model in enumerate(models[:-1]):
            if not isinstance(model, FinalParticlesMixIn):
                raise ValueError(
                    f"Model {i} must implement FinalParticlesMixIn to stage models."
                )

        for i, model in enumerate(models[1:], start=1):
            if not isinstance(model, InitialParticlesMixIn):
                raise ValueError(
                    f"Model {i} must implement InitialParticlesMixIn to stage models."
                )

        seen: dict[str, int] = {}
        for i, model in enumerate(models):
            for name in model.supported_variables:
                if name in seen:
                    raise ValueError(
                        f"Variable '{name}' is defined in both model {seen[name]} and model {i}."
                    )
                seen[name] = i

    @property
    def supported_variables(self) -> dict[str, Variable]:
        return {
            name: var
            for model in self.lume_model_instances
            for name, var in model.supported_variables.items()
        }

    def _get(self, names: list[str]) -> dict[str, Any]:
        values = {}
        for model in self.lume_model_instances:
            model_names = [n for n in names if n in model.supported_variables]
            if model_names:
                values.update(model.get(model_names))
        return values

    def _set(self, values: dict[str, Any]) -> None:
        """
        Parameters
        ----------
        values: dict[str, Any]
            Variable names and values to set across the staged models.
        """
        incoming_particles = None
        for i, model in enumerate(self.lume_model_instances):
            model_values = {
                k: v for k, v in values.items() if k in model.supported_variables
            }

            if i > 0 and incoming_particles is not None:
                # Pass particles unconditionally — beam changes every cycle.
                # The setter cost is reduced by lume_bmad_model patches (no
                # redundant comb_ds_save cmd or double update_state per call).
                model.initial_particles = incoming_particles

            if model_values:
                model.set(model_values)

            if isinstance(model, FinalParticlesMixIn):
                incoming_particles = model.final_particles

    def reset(self):
        for model in self.lume_model_instances:
            model.reset()
