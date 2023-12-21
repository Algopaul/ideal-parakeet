# Copyright 2023 The swirl_lm Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implementation of a radiative transfer solver."""

import collections
from typing import Optional

import numpy as np
from swirl_lm.base import parameters as parameters_lib
from swirl_lm.physics.atmosphere import microphysics_one_moment
from swirl_lm.physics.radiation.optics import optics_utils
from swirl_lm.physics.radiation.rte import two_stream
from swirl_lm.physics.thermodynamics import water
from swirl_lm.utility import types
import tensorflow as tf


OrderedDict = collections.OrderedDict
FlowFieldMap = types.FlowFieldMap
FlowFieldVal = types.FlowFieldVal


class RRTMGP:
  """Rapid Radiative Transfer Model for General Circulation Models (RRTMGP)."""

  def __init__(
      self,
      config: parameters_lib.SwirlLMParameters,
      g_dim: int,
  ):
    self._kernel_op = config.kernel_op
    self._kernel_op.add_kernel({
        'shift_up': ([1.0, 0.0, 0.0], 1),
        'shift_dn': ([0.0, 0.0, 1.0], 1)
    })
    # A thermodynamics manager that handles moisture related physics.
    self._water = water.Water(config)
    # The vertical dimension.
    self._g_dim = g_dim
    # The number of ghost points on a side of the subgrid.
    self._halos = config.halo_width
    # The vertical grid spacing used in computing the local water path for an
    # atmospheric grid cell.
    self._dh = (config.dx, config.dy, config.dz)[g_dim]
    # The two-stream radiative transfer solver.
    self._two_stream_solver = two_stream.TwoStreamSolver(
        config.radiative_transfer,
        config,
        self._kernel_op,
        g_dim,
    )
    # Data library containing atmospheric gas concentrations.
    self._atmospheric_state = self._two_stream_solver.atmospheric_state
    # Library for 1-moment microphysics.
    self._microphysics_lib = microphysics_one_moment.Adapter(
        config, self._water
    )

  def _compute_cloud_path(self, rho: FlowFieldVal, q_c: FlowFieldVal):
    """Computes the cloud water/ice path in an atmospheric grid cell."""

    def cloud_path_fn(rho: tf.Tensor, q_c: tf.Tensor) -> tf.Tensor:
      return rho * q_c * self._dh

    return tf.nest.map_structure(cloud_path_fn, rho, q_c)

  def _reconstruct_vmr(self, p: FlowFieldVal, vmr_profile: tf.Tensor):
    """Reconstructs the volume mixing ratio field for a given pressure field."""
    p_for_interp = self._atmospheric_state.vmr.p_ref
    def vmr_fn(p: tf.Tensor) -> tf.Tensor:
      interp = optics_utils.create_linear_interpolant(p, p_for_interp)
      return optics_utils.interpolate(
          vmr_profile, OrderedDict({'p': lambda _: interp})
      )

    return tf.nest.map_structure(vmr_fn, p)

  def compute_heating_rate(
      self,
      replica_id: tf.Tensor,
      replicas: np.ndarray,
      states: FlowFieldMap,
      additional_states: FlowFieldMap,
      sfc_temperature: Optional[FlowFieldVal] = None,
  ):
    """Computes the local heating rate due to radiative transfer.

    The optical properties of the layered atmosphere are computed using RRTMGP
    and the two-stream radiative transfer equation is solved for the net fluxes
    at the atmospheric grid cell faces. Based on the overall net radiative flux
    of the grid cell, a local heating rate is determined.

    Args:
      replica_id: The index of the current TPU replica.
      replicas: A numpy array that maps grid coordinates to replica id numbers.
      states: A dictionary that holds all flow field variables and must include
        the total specific humidity (`q_t`).
      additional_states: A dictionary that holds all helper variables and must
        include temperatue (`T`).
      sfc_temperature: The optional surface temperature [K] represented as
        either a 3D `tf.Tensor` or as a list of 2D `tf.Tensor`s but having a
        single vertical dimension.

    Returns:
      A `FlowFieldVal` for the local heating rate due to radiative fluxes [K/s].
    """
    assert 'q_t' in states, (
        'RRTMGP requires the total specific humidity (`q_t`) to be present in'
        ' `states`.'
    )
    assert 'T' in additional_states, (
        'RRTMGP requires the temperature (`T`) to be present in'
        ' `additional_states`.'
    )
    q_liq, q_ice = self._water.equilibrium_phase_partition(
        additional_states['T'], states['rho'], states['q_t']
    )
    pressure = self._water.p_ref(additional_states['zz'], additional_states)

    q_c = tf.nest.map_structure(tf.math.add, q_liq, q_ice)
    vmr_h2o = self._water.humidity_to_volume_mixing_ratio(states['q_t'], q_c)
    vmr_o3 = self._reconstruct_vmr(pressure, self._atmospheric_state.vmr.vmr_o3)
    vmr_fields = {
        1: vmr_h2o,
        3: vmr_o3,
    }
    molecules_per_area = self._water.air_molecules_per_area(
        self._kernel_op, pressure, self._g_dim, vmr_h2o
    )
    lwp = self._compute_cloud_path(states['rho'], q_liq)
    iwp = self._compute_cloud_path(states['rho'], q_ice)
    cloud_r_eff_liq = self._microphysics_lib.cloud_particle_effective_radius(
        states['rho'], q_liq, 'l'
    )
    cloud_r_eff_ice = self._microphysics_lib.cloud_particle_effective_radius(
        states['rho'], q_ice, 'i'
    )

    lw_fluxes = self._two_stream_solver.solve_lw(
        replica_id,
        replicas,
        pressure,
        additional_states['T'],
        molecules_per_area,
        vmr_fields=vmr_fields,
        sfc_temperature=sfc_temperature,
        cloud_r_eff_liq=cloud_r_eff_liq,
        cloud_path_liq=lwp,
        cloud_r_eff_ice=cloud_r_eff_ice,
        cloud_path_ice=iwp,
    )
    sw_fluxes = self._two_stream_solver.solve_sw(
        replica_id,
        replicas,
        pressure,
        additional_states['T'],
        molecules_per_area,
        vmr_fields=vmr_fields,
        cloud_r_eff_liq=cloud_r_eff_liq,
        cloud_path_liq=lwp,
        cloud_r_eff_ice=cloud_r_eff_ice,
        cloud_path_ice=iwp,
    )
    flux_net = tf.nest.map_structure(
        tf.math.add, lw_fluxes['flux_net'], sw_fluxes['flux_net']
    )
    return self._two_stream_solver.compute_heating_rate(flux_net, pressure)
