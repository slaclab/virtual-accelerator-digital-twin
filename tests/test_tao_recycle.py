"""Unit tests for the Tao recycling logic.

Covers the pure logic only -- the config-command replay log, model discovery, and the
state verification that guards against publishing unverified physics. RecyclableTao itself
needs pytao and is exercised by the probe deployment, not here.
"""

import tao_recycle


class FakeVariable:
    def __init__(self, read_only=False):
        self.read_only = read_only


class BeamAtElementVariable(FakeVariable):
    """Name matters: verify_state excludes this type by class name."""


class FakeTao:
    def __init__(self, track_type="beam", track_start="OTR2", comb_len=120):
        self._track_type = track_type
        self._track_start = track_start
        self._comb_len = comb_len

    def tao_global(self):
        return {"track_type": self._track_type}

    def beam(self, _ix):
        return {"track_start": self._track_start}

    def bunch_comb(self, _who):
        return list(range(self._comb_len))


class FakeBmad:
    def __init__(self, tao, state, variables):
        self.tao = tao
        self._state = state
        self.supported_variables = variables
        self.restored_state = None

    def update_state(self):
        if self.restored_state is not None:
            self._state = dict(self.restored_state)


def _bmad(controls, *, read_only_extra=True, **tao_kwargs):
    variables = {name: FakeVariable(read_only=False) for name in controls}
    if read_only_extra:
        variables["norm_emit_x"] = FakeVariable(read_only=True)
    state = dict(controls)
    state.setdefault("norm_emit_x", 1.0)
    return FakeBmad(FakeTao(**tao_kwargs), state, variables)


# --- config command log ----------------------------------------------------


def test_records_configuration_commands():
    log = {}
    for cmd in (
        "set beam track_start = OTR2",
        "set beam comb_ds_save = 0.1",
        "set beam_init position_file = /tmp/input_beam.h5",
        "set global track_type = beam",
        "set ele QA11 K1 = 1.5",
    ):
        assert tao_recycle.record_config_command(log, cmd), cmd
    assert len(log) == 5


def test_excludes_lattice_calc_toggle():
    log = {}
    assert not tao_recycle.record_config_command(log, "set global lattice_calc_on = F")
    assert not tao_recycle.record_config_command(log, "set global lattice_calc_on = T")
    assert log == {}


def test_ignores_reads_and_unrelated_commands():
    log = {}
    for cmd in ("show global", "pipe bunch_comb s 1@0 1", "set global plot_on = F", ""):
        assert not tao_recycle.record_config_command(log, cmd)
    assert log == {}


def test_ignores_non_strings():
    log = {}
    assert not tao_recycle.record_config_command(log, None)
    assert not tao_recycle.record_config_command(log, 42)
    assert log == {}


def test_dedupes_by_target_keeping_latest_value_and_first_position():
    log = {}
    tao_recycle.record_config_command(log, "set ele QA11 K1 = 1.0")
    tao_recycle.record_config_command(log, "set beam comb_ds_save = 0.1")
    tao_recycle.record_config_command(log, "set ele QA11 K1 = 9.0")

    assert len(log) == 2, "repeated writes to the same target must not accumulate"
    commands = list(log.values())
    assert commands[0] == "set ele QA11 K1 = 9.0", "latest value, original position"
    assert commands[1] == "set beam comb_ds_save = 0.1"


def test_log_stays_bounded_across_many_cycles():
    log = {}
    for cycle in range(500):
        for quad in ("QA11", "QA12", "Q21201"):
            tao_recycle.record_config_command(log, f"set ele {quad} K1 = {cycle}")
    assert len(log) == 3


# --- model discovery -------------------------------------------------------


def test_find_bmad_model_direct():
    model = _bmad({"QA11": 1.0})
    assert tao_recycle.find_bmad_model(model) is model


def test_find_bmad_model_in_staged():
    bmad = _bmad({"QA11": 1.0})

    class Staged:
        lume_model_instances = [object(), bmad]

    assert tao_recycle.find_bmad_model(Staged()) is bmad


def test_find_bmad_model_absent():
    class Staged:
        lume_model_instances = [object()]

    assert tao_recycle.find_bmad_model(Staged()) is None


# --- value comparison ------------------------------------------------------


def test_values_match():
    assert tao_recycle._values_match(1.0, 1.0)
    assert tao_recycle._values_match("beam", "beam")
    assert tao_recycle._values_match([1.0, 2.0], [1.0, 2.0])
    assert not tao_recycle._values_match(1.0, 1.1)
    assert not tao_recycle._values_match("beam", "single")
    assert not tao_recycle._values_match([1.0], [1.0, 2.0])
    assert not tao_recycle._values_match(1.0, tao_recycle._MISSING)


# --- state verification (the safety net) -----------------------------------


def test_verify_state_clean_when_everything_restored():
    bmad = _bmad({"QA11": 1.5, "QA12": -2.5})
    snapshot = tao_recycle.capture_state(bmad)
    bmad.restored_state = dict(bmad._state)

    assert tao_recycle.verify_state(bmad, snapshot) == []


def test_verify_state_detects_control_values_reverted_to_design():
    """The dangerous case: magnets back at design while the service claims live values."""
    bmad = _bmad({"QA11": 1.5, "QA12": -2.5})
    snapshot = tao_recycle.capture_state(bmad)
    bmad.restored_state = {"QA11": 0.0, "QA12": 0.0, "norm_emit_x": 1.0}

    problems = tao_recycle.verify_state(bmad, snapshot)
    assert problems
    assert "2 of 2 control variables differ" in problems[0]
    assert any("QA11" in p for p in problems)


def test_verify_state_detects_track_type_regression():
    bmad = _bmad({"QA11": 1.5})
    snapshot = tao_recycle.capture_state(bmad)
    bmad.restored_state = dict(bmad._state)
    bmad.tao._track_type = "single"

    problems = tao_recycle.verify_state(bmad, snapshot)
    assert any("track_type" in p for p in problems)


def test_verify_state_detects_track_start_regression():
    bmad = _bmad({"QA11": 1.5})
    snapshot = tao_recycle.capture_state(bmad)
    bmad.restored_state = dict(bmad._state)
    bmad.tao._track_start = "BEGINNING"

    problems = tao_recycle.verify_state(bmad, snapshot)
    assert any("track_start" in p for p in problems)


def test_verify_state_detects_comb_resolution_change():
    """A lost comb_ds_save shows up as a different comb length."""
    bmad = _bmad({"QA11": 1.5}, comb_len=120)
    snapshot = tao_recycle.capture_state(bmad)
    bmad.restored_state = dict(bmad._state)
    bmad.tao._comb_len = 12

    problems = tao_recycle.verify_state(bmad, snapshot)
    assert any("comb length" in p for p in problems)


def test_verify_state_ignores_stochastic_read_only_outputs():
    """Beam generation is unseeded, so emittance varies between tracks by design."""
    bmad = _bmad({"QA11": 1.5})
    snapshot = tao_recycle.capture_state(bmad)
    restored = dict(bmad._state)
    restored["norm_emit_x"] = 1.05
    bmad.restored_state = restored

    assert tao_recycle.verify_state(bmad, snapshot) == []


def test_capture_state_excludes_beam_at_element_variables():
    bmad = _bmad({"QA11": 1.5})
    bmad.supported_variables["OTR2_beam"] = BeamAtElementVariable(read_only=False)
    bmad._state["OTR2_beam"] = object()

    snapshot = tao_recycle.capture_state(bmad)
    assert "OTR2_beam" not in snapshot["controls"]
    assert "QA11" in snapshot["controls"]


def test_capture_state_skips_comb_when_not_beam_tracking():
    bmad = _bmad({"QA11": 1.5}, track_type="single")
    snapshot = tao_recycle.capture_state(bmad)
    assert snapshot["comb_len"] is None


# --- install_recycling guards ---------------------------------------------


def test_descendants_rss_degrades_gracefully_without_proc():
    """Returns a float (nan off-Linux) rather than raising, so [mem] logging never breaks."""
    v = tao_recycle.descendants_rss_mb()
    assert isinstance(v, float)


def test_install_recycling_disabled_is_a_noop():
    bmad = _bmad({"QA11": 1.5})
    original = bmad.set = lambda values: values
    assert tao_recycle.install_recycling(bmad, enabled=False) is False
    assert bmad.set is original


def test_install_recycling_refuses_when_tao_not_recyclable():
    """Guards against the pytao.Tao substitution silently not taking effect."""
    bmad = _bmad({"QA11": 1.5})
    bmad.set = lambda values: values
    assert tao_recycle.install_recycling(bmad, enabled=True) is False


def test_install_recycling_refuses_when_no_bmad_model():
    class Staged:
        lume_model_instances = [object()]
        set = staticmethod(lambda values: values)

    assert tao_recycle.install_recycling(Staged(), enabled=True) is False
