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

"""A library of density update with a constant."""

from swirl_lm.base import parameters as parameters_lib
from swirl_lm.physics.thermodynamics import thermodynamics_generic
import tensorflow as tf

TF_DTYPE = thermodynamics_generic.TF_DTYPE

FlowFieldVal = thermodynamics_generic.FlowFieldVal
FlowFieldMap = thermodynamics_generic.FlowFieldMap


class ConstantDensity(thermodynamics_generic.ThermodynamicModel):
  """A library of constant density."""

  def __init__(self, params: parameters_lib.SwirlLMParameters):
    """Initializes the constant density object."""
    super(ConstantDensity, self).__init__(params)

    self.rho = params.rho

  def update_density(
      self,
      states: FlowFieldMap,
      additional_states: FlowFieldMap,
  ) -> FlowFieldVal:
    """Updates the density with the ideal gas law."""
    del additional_states
    return tf.nest.map_structure(
        lambda x: self.rho * tf.ones_like(x, dtype=TF_DTYPE),
        list(states.values())[0])
