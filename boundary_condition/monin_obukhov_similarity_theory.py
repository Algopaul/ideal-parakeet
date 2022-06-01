"""A library of the Monin-Obukhov Similarity Theory.

This library is useful for simulating atmospheric boundary layers [1], in
applications such as cloud simulations [2]. Neumann boundary conditions are
enforced for variable u, v, and T (optional).

References:
1. Mahrt, Larry. 2014. “Stably Stratified Atmospheric Boundary Layers.” Annual
Review of Fluid Mechanics 46 (1): 23–45.
2. Stevens, Bjorn, Chin-Hoh Moeng, Andrew S. Ackerman, Christopher S.
Bretherton, Andreas Chlond, Stephan de Roode, James Edwards, et al. 2005.
“Evaluation of Large-Eddy Simulations via Observations of Nocturnal Marine
Stratocumulus.” Monthly Weather Review 133 (6): 1443–62.

The computation is mainly based on the references, and the only 2 exceptions are
both computational, to avoid `nan`s:
1. tf.math.divide_no_nan in divisions
2. Enforcing numbers to be non-negative when computing sqrt

The main idea for case #1 is the following:
1. When the Obukhov length is `0`, it indicates there's no friction, and it's
   safe to set the shear stress to `0`
2. When the velocity at the first fluid layer is `0`, it means the velocity is
   the same as a non-slip wall, and the shear stress at the wall is `0`
In both cases `tf.math.divide_no_nan` gives the desired output.
"""
import functools
from typing import Mapping, Text, Tuple

import numpy as np
from swirl_lm.boundary_condition import monin_obukhov_similarity_theory_pb2
from swirl_lm.equations import common
from swirl_lm.numerics import root_finder
from swirl_lm.utility import common_ops
from swirl_lm.utility import get_kernel_fn
from swirl_lm.utility import grid_parametrization
from swirl_lm.utility import types
import tensorflow as tf

from google3.research.simulation.tensorflow.fluid.framework import initializer
from google3.research.simulation.tensorflow.fluid.framework import util
from google3.research.simulation.tensorflow.fluid.models.incompressible_structured_mesh import incompressible_structured_mesh_config
from google3.research.simulation.tensorflow.fluid.models.incompressible_structured_mesh import physical_variable_keys_manager

# The type of a state variable.
FlowFieldVal = types.FlowFieldVal
FlowFieldMap = types.FlowFieldMap

# The von Karman constant.
_KAPPA = 0.4
# The stability correction for momentum.
_PHI_M = 0.0
# The acceleration of gravity.
_G = 9.81


class MoninObukhovSimilarityTheory(object):
  """A library of the Monin-Obukhov Similarity Theory."""

  def __init__(
      self,
      params: monin_obukhov_similarity_theory_pb2.MoninObukhovSimilarityTheory,
      nu: float,
      vertical_dim: int,
      height: float,
      halo_width: int,
  ):
    """Initializes the library."""
    self.nu = nu
    self.height = height
    self.halo_width = halo_width

    self.z_0 = params.z_0
    self.z_t = params.z_t
    self.u_star = params.u_star
    self.t_0 = params.t_0
    self.t_s = params.t_s
    self.heat_flux = params.heat_flux
    self.beta_m = params.beta_m
    self.beta_h = params.beta_h
    self.gamma_m = params.gamma_m
    self.gamma_h = params.gamma_h
    self.alpha = params.alpha

    self.enable_theta_reg = params.enable_theta_reg
    self.theta_max = params.theta_max
    self.theta_min = params.theta_min

    self.bc_manager = (
        physical_variable_keys_manager.BoundaryConditionKeysHelper())
    # Precompute the vertical axis and the horizontal dimensions and axes from
    # the vertical dimension.
    self.vertical_dim = vertical_dim
    self.horizontal_dims = [0, 1, 2]
    self.horizontal_dims.remove(vertical_dim)
    self.dim_to_v_key = (common.KEY_U, common.KEY_V, common.KEY_W)
    dim_to_axis = (1, 2, 0)
    self.vertical_axis = dim_to_axis[self.vertical_dim]
    self.horizontal_axes = [dim_to_axis[dim] for dim in self.horizontal_dims]

  def _stability_correction_function(
      self,
      zeta: FlowFieldVal,
      theta: FlowFieldVal,
  ) -> Tuple[FlowFieldVal, FlowFieldVal]:
    """Computes the stability correction function based on buoyancy condition.

    Args:
      zeta: The normalized height that is defined as z / L, where z is the
        symbolic representation of a vertical coordinate, and L is the Obukhov
        length.
      theta: The potential temperature in units of K. It will be used to compute
        the buoyancy and determine the stability of the boundary layer.

    Returns:
      The value of the stability function computed at a specific height.
    """
    b = tf.nest.map_structure(lambda t: t - self.t_s, theta)

    def stable(z: tf.Tensor, option: Text) -> Tuple[tf.Tensor, tf.Tensor]:
      """Computes the correction functions for a stable boundary layer.

      References"
      [1] Stoll, Rob, and Fernando Porté-Agel. 2009. “Surface Heterogeneity
          Effects on Regional-Scale Fluxes in Stable Boundary Layers: Surface
          Temperature Transitions.” Journal of the Atmospheric Sciences 66 (2):
          412–31.
      [2] Stoll, Rob, and Fernando Porté-Agel. 2008. “Large-Eddy Simulation of
          the Stable Atmospheric Boundary Layer Using Dynamic Models with
          Different Averaging Schemes.” Boundary-Layer Meteorology 126 (1):
          1–28.

      Args:
        z: The normalized vertical coordinates.
        option: The type of stability function to be returned. If it's 'M',
          the stability function for momentum will be returned; otherwise
          the stability function for energy will be returned.

      Returns:
        A tuple of state variables with the first and second element being the
        stability correction functions for the momemtum and energy,
        respectively.
      """
      return -self.beta_m * z if option == 'M' else -self.beta_h * z

    def neutral(z: tf.Tensor) -> Tuple[tf.Tensor, tf.Tensor]:
      """Computes the correction functions for a neutral boundary layer.

      Reference:
      [1] Stoll, Rob, and Fernando Porté-Agel. 2006. “Dynamic Subgrid-Scale
          Models for Momentum and Scalar Fluxes in Large-Eddy Simulations of
          Neutrally Stratified Atmospheric Boundary Layers over Heterogeneous
          Terrain.” Water Resources Research 42 (1): 2121.

      Args:
        z: The normalized vertical coordinates.

      Returns:
        A tuple of state variables with the first and second element being the
        stability correction functions for the momemtum and energy,
        respectively.
      """
      return tf.zeros_like(z)

    def unstable(z: tf.Tensor, option: Text) -> Tuple[tf.Tensor, tf.Tensor]:
      """Computes the correction functions for a unstable boundary layer.

      References"
      [1] Stoll, Rob, and Fernando Porté-Agel. 2009. “Surface Heterogeneity
          Effects on Regional-Scale Fluxes in Stable Boundary Layers: Surface
          Temperature Transitions.” Journal of the Atmospheric Sciences 66 (2):
          412–31.

      Args:
        z: The normalized vertical coordinates.
        option: The type of stability function to be returned. If it's 'M',
          the stability function for momentum will be returned; otherwise
          the stability function for energy will be returned.

      Returns:
        A tuple of state variables with the first and second element being the
        stability correction functions for the momemtum and energy,
        respectively.
      """
      alpha = 1.0

      if option == 'M':
        x = tf.math.pow(tf.maximum(1.0 - self.gamma_m * z, 0.0), 0.25)

        psi = 2.0 * tf.math.log(0.5 * (1.0 + x)) + tf.math.log(
            0.5 * (1.0 + x**2)) - 2.0 * tf.math.atan(x) + 0.5 * np.pi
      else:
        y = tf.math.pow(tf.maximum(1.0 - self.gamma_h * z, 0.0), 0.5)
        psi = 2.0 * alpha * tf.math.log(0.5 * (1.0 + y))

      return psi

    def stability_fn(
        bi: tf.Tensor,
        zi: tf.Tensor,
        option: Text,
    ) -> Tuple[tf.Tensor, tf.Tensor]:
      """Generates the correct stability function based on buoyancy."""
      return tf.where(
          tf.less(bi, 0.0), unstable(zi, option),
          tf.where(tf.greater(bi, 0.0), stable(zi, option), neutral(zi)))

    psi_m = tf.nest.map_structure(
        functools.partial(stability_fn, option='M'), b, zeta)
    psi_h = tf.nest.map_structure(
        functools.partial(stability_fn, option='H'), b, zeta)

    return psi_m, psi_h

  def _richardson_number(
      self,
      theta: FlowFieldVal,
      u1: FlowFieldVal,
      u2: FlowFieldVal,
      height: float,
  ) -> FlowFieldVal:
    """Computes the bulk Richardson number.

    Args:
      theta: The potential temperature (in units of K) at the first node obove
        ground.
      u1: The first component of the free stream velocity.
      u2: The second component of the free stream velocity.
      height: The height of the first grid point.

    Returns:
      The bulk Richardson number.
    """

    def richardson_number(
        t: tf.Tensor,
        u: tf.Tensor,
        v: tf.Tensor,
    ) -> tf.Tensor:
      """Computes the Richardson number."""
      return _G * height * tf.math.divide_no_nan(t - self.t_s,
                                                 (u**2 + v**2) * t)

    return tf.nest.map_structure(richardson_number, theta, u1, u2)

  def _normalized_height(
      self,
      theta: FlowFieldVal,
      u1: FlowFieldVal,
      u2: FlowFieldVal,
      height: float,
  ) -> FlowFieldVal:
    """Computes the height normalized by the Obukhov length 𝜁 = z / L.

    Based on the definition of the Obukhov length, surface shear stress and heat
    flux [1], an equation for the bulk Richardson number is derived as follows:
    Rb = g z (θ(z) - θₛ) / [|u|²θ(z)]
       = 𝜁 [ln(z / z₀) - 𝚿ₕ(𝜁)] / [ln(z / z₀) - 𝚿ᴍ(𝜁)]²,
    where 𝚿ₕ(𝜁) and 𝚿ᴍ(𝜁) are the stability correction functions for energy and
    momentum, respectively. The form of these two functions are determined by
    the buoyancy of the flow. 𝜁 can be solved iteratively with this equation.

    Reference:
    [1] Stoll, Rob, and Fernando Porté-Agel. 2009. “Surface Heterogeneity
          Effects on Regional-Scale Fluxes in Stable Boundary Layers: Surface
          Temperature Transitions.” Journal of the Atmospheric Sciences 66 (2):
          412–31.

    Args:
      theta: The potential temperature (in units of K) at the first node obove
        ground.
      u1: The first component of the free stream velocity.
      u2: The second component of the free stream velocity.
      height: The height of the first grid point.

    Returns:
      The Oubkhov length normalized height.
    """
    ln_z_by_z0 = np.log(height / self.z_0)
    r_b = self._richardson_number(theta, u1, u2, height)
    max_iter = 10

    def err_fn(
        r: tf.Tensor,
        z: tf.Tensor,
        p_h: tf.Tensor,
        p_m: tf.Tensor,
    ) -> tf.Tensor:
      """Computes the error function for the iterative solve with tf.Tensor."""
      return r - z * (ln_z_by_z0 - p_h) / (ln_z_by_z0 - p_m)**2

    def rhs_fn(zeta: FlowFieldVal) -> FlowFieldVal:
      """Defines the right hand side function for the iterative solve."""
      psi_m, psi_h = self._stability_correction_function(zeta, theta)
      err = tf.nest.map_structure(err_fn, r_b, zeta, psi_h, psi_m)

      return err

    zeta_init = tf.nest.map_structure(tf.zeros_like, theta)

    return root_finder.newton_method(rhs_fn, zeta_init, max_iter)

  def _maybe_regularize_potential_temperature(
      self, theta: FlowFieldVal) -> FlowFieldVal:
    """Applies bounds to the potential temperature is requested.

    Args:
      theta: The potential temperature.

    Returns:
      The potential temperature bounded by the user specified limites. If
      `enable_theta_reg` is `false`, the input theta will be returned without
      modifications.
    """
    if self.enable_theta_reg:
      theta = tf.nest.map_structure(
          lambda t: tf.maximum(tf.minimum(t, self.theta_max), self.theta_min),
          theta)

    return theta

  def _surface_shear_stress_and_heat_flux(
      self,
      theta: FlowFieldVal,
      u1: FlowFieldVal,
      u2: FlowFieldVal,
      height: float,
  ) -> Tuple[FlowFieldVal, FlowFieldVal, FlowFieldVal]:
    """Computes the surface shear stress and heat flux.

    Reference:
    Stoll, Rob, and Fernando Porté-Agel. 2008. “Large-Eddy Simulation of the
    Stable Atmospheric Boundary Layer Using Dynamic Models with Different
    Averaging Schemes.” Boundary-Layer Meteorology 126 (1): 1–28.

    Args:
      theta: The potential temperature (in units of K) at the first node above
        ground.
      u1: The first component of the free stream velocity.
      u2: The second component of the free stream velocity.
      height: The height of the first grid point.

    Returns:
      A 3 component tuple, with elements being (in order) the surface shear
      stress for velocity component u1 and u2, and the surface heat flux.
    """
    zeta = self._normalized_height(theta, u1, u2, height)
    phi_m, phi_h = self._stability_correction_function(zeta, theta)

    u_mag = tf.nest.map_structure(lambda u, v: tf.math.sqrt(u**2 + v**2), u1,
                                  u2)

    def surface_shear_stress(
        u_i: tf.Tensor,
        u_r: tf.Tensor,
        phi: tf.Tensor,
    ) -> tf.Tensor:
      """Computes the surface shear stress for a specific velocity component."""
      return -_KAPPA**2 * u_r * u_i / (tf.math.log(height / self.z_0) - phi)**2

    def surface_heat_flux(
        theta_i: tf.Tensor,
        u_s_i,
        phi: tf.Tensor,
    ) -> tf.Tensor:
      """Computes the surface heat flux."""
      return (self.t_s - theta_i) * u_s_i * _KAPPA / (
          tf.math.log(height / self.z_0) - phi)

    tau_13 = tf.nest.map_structure(surface_shear_stress, u1, u_mag, phi_m)
    tau_23 = tf.nest.map_structure(surface_shear_stress, u2, u_mag, phi_m)

    u_s = tf.nest.map_structure(
        lambda t_13, t_23: tf.math.pow(t_13**2 + t_23**2, 0.25), tau_13, tau_23)

    q_3 = tf.nest.map_structure(surface_heat_flux, theta, u_s, phi_h)

    return tau_13, tau_23, q_3

  def surface_shear_stress_and_heat_flux_update_fn(
      self,
      states: FlowFieldMap,
  ) -> Tuple[FlowFieldVal, FlowFieldVal, FlowFieldVal]:
    """Computes the wall shear stress and heat flux.

    Args:
      states: A keyed dictionary of states. Must include 'u', 'v', 'w', 'theta'.

    Returns:
      A 3 component tuple, with elements being (in order) the surface shear
      stress for velocity component u1 and u2, and the surface heat flux. Note
      that each component is a 2D slice of a 3D tensor.
    """
    # Get the velocity components that are tangential to the ground.
    velocity_keys = list(common.KEYS_VELOCITY)
    del velocity_keys[self.vertical_dim]

    # Get the slice of the first fluid layer above the ground for the ground
    # tangential velocity and potential temperature. Assume the ground is always
    # on the low-index end in a dimension.
    u1 = util.get_slice(states[velocity_keys[0]], self.vertical_dim, 0,
                        self.halo_width)[0]
    u2 = util.get_slice(states[velocity_keys[1]], self.vertical_dim, 0,
                        self.halo_width)[0]
    theta = self._maybe_regularize_potential_temperature(
        util.get_slice(states['theta'], self.vertical_dim, 0,
                       self.halo_width)[0])

    # Because the wall is at the mid-point face between the first fluid layer
    # and the halo layers, the height of the first fluid layer above the ground
    # is half of the grid spacing.
    return self._surface_shear_stress_and_heat_flux(theta, u1, u2,
                                                    self.height / 2.0)

  def _exchange_coefficient(
      self,
      theta: FlowFieldVal,
      u1: FlowFieldVal,
      u2: FlowFieldVal,
      height: float,
  ) -> FlowFieldVal:
    """Computes the exchange coefficient for the energy equation.

    Reference:
    Schneider, T. (n.d.). CLIMA Atmosphere Model. Caltech.

    Args:
      theta: The potential temperature (in units of K) at the first node above
        ground.
      u1: The first component of the free stream velocity.
      u2: The second component of the free stream velocity.
      height: The height of the first grid point.

    Returns:
      The exchange coefficient for the energy equation.
    """
    zeta = self._normalized_height(theta, u1, u2, height)
    phi_m, phi_h = self._stability_correction_function(zeta, theta)

    ln_z = tf.math.log(height / self.z_0)

    # The coefficient is set to 0 when ln(z_m / z_0) equals Psi_M or Psi_H,
    # which suggests a 0 surface flux.
    return tf.nest.map_structure(
        lambda p_m, p_h: tf.math.divide_no_nan(_KAPPA**2, (ln_z - p_h) *  # pylint: disable=g-long-lambda
                                               (ln_z - p_m)), phi_m, phi_h)

  def surface_scalar_flux_update_fn(
      self,
      states: FlowFieldMap,
  ) -> FlowFieldVal:
    """Computes the scalar flux at the surface.

    Reference:
    Schneider, T. (n.d.). CLIMA Atmosphere Model. Caltech. (Eq. 5.7)

    Args:
      states: A keyed dictionary of states. Must include 'u', 'v', 'w', 'theta',
      'rho', 'phi'.

    Returns:
      The flux of `phi` at the surface.
    """
    # Get the velocity components that are tangential to the ground.
    velocity_keys = list(common.KEYS_VELOCITY)
    del velocity_keys[self.vertical_dim]

    # Get the slice of the first fluid layer above the ground for the ground
    # tangential velocity and potential temperature. Assume the ground is always
    # on the low-index end in a dimension.
    u1 = util.get_slice(states[velocity_keys[0]], self.vertical_dim, 0,
                        self.halo_width)[0]
    u2 = util.get_slice(states[velocity_keys[1]], self.vertical_dim, 0,
                        self.halo_width)[0]
    theta = self._maybe_regularize_potential_temperature(
        util.get_slice(states['theta'], self.vertical_dim, 0,
                       self.halo_width)[0])
    rho = util.get_slice(states['rho'], self.vertical_dim, 0,
                         self.halo_width)[0]
    phi_zm = util.get_slice(states['phi'], self.vertical_dim, 0,
                            self.halo_width)[0]
    phi_z0 = util.get_slice(states['phi'], self.vertical_dim, 0,
                            self.halo_width - 1)[0]

    # Because the wall is at the mid-point face between the first fluid layer
    # and the halo layers, the height of the first fluid layer above the ground
    # is half of the grid spacing.
    c_h = self._exchange_coefficient(theta, u1, u2, self.height / 2.0)

    def scalar_flux(
        rho_i: tf.Tensor,
        c_h_i: tf.Tensor,
        u1_i: tf.Tensor,
        u2_i: tf.Tensor,
        phi_zm_i: tf.Tensor,
        phi_z0_i: tf.Tensor,
    ) -> tf.Tensor:
      """Computes the energy flux."""
      return -rho_i * c_h_i * tf.math.sqrt(u1_i**2 + u2_i**2) * (
          phi_zm_i - phi_z0_i)

    return tf.nest.map_structure(scalar_flux, rho, c_h, u1, u2, phi_zm, phi_z0)

  def _compute_obukhov_length(
      self,
      m: tf.Tensor,
      temperature: tf.Tensor,
      z_m: tf.Tensor,
  ) -> tf.Tensor:
    """Computes the Obukhov length.

    From Stoll and Porte-Agel [1],
      <tau_s> = -Cm <M(z_m)>^2,
      <q_s> = -Ch <M(z_m)>[T(z_m) - T_s],
      L = -u*^3 T_0 / (kappa g <q_s>),
    where Cm and Ch are the transfer coefficients that are functions of z_m / L.
    Based on these formulations, a quadratic equation can be derived by letting
    x = z_m / L, which takes the form a x^2 + b x + c = 0. The coefficients are:
      a = beta_m^2 + C / z_m * beta_h,
      b = 2 beta_m ln(z_m / z_0) + alpha C / z_m ln(z_m / z_t),
      c = ln(z_m / z_0)^2,
    where:
      C = (u^2 + v^2) / g * t_0 / (t - t_s).
    Note that the computation is all based on the equations, and the only
    exception is to avoid `nan`s with `tf.math.divide_no_nan`, and enforcing
    numbers to be non-negative when taking the square root. For example, for the
    former, while `g` and `t_0` are guaranteed to be non-zero, `(t - t_s)` in
    denominator could be `0` and needs special handling.

    Reference:
    1. Stoll, Rob, and Fernando Porté-Agel. 2009. “Surface Heterogeneity Effects
       on Regional-Scale Fluxes in Stable Boundary Layers: Surface Temperature
       Transitions.” Journal of the Atmospheric Sciences 66 (2): 412–31.

    Args:
      m: The mean velocity magnitude over the x-y plane at `z_m`.
      temperature: The mean temperature over the x-y plane at `z_m`.
      z_m: The height of the first grid point in the z direction.

    Returns:
      The Obukhov length.
    """
    param = tf.math.divide_no_nan(m**2 / _G * self.t_0, temperature - self.t_s)

    a = self.beta_m**2 + tf.math.divide_no_nan(param * self.beta_h, z_m)
    b = 2.0 * self.beta_m * tf.math.log(z_m / self.z_0) + tf.math.divide_no_nan(
        self.alpha * param * tf.math.log(z_m / self.z_t), z_m)
    c = tf.math.log(z_m / self.z_0)**2

    delta = tf.math.sqrt(tf.maximum(b**2 - 4.0 * a * c, 0.0))
    l_inv_1 = tf.math.divide_no_nan(-b - delta, 2.0 * a)
    l_inv_2 = tf.math.divide_no_nan(-b + delta, 2.0 * a)
    l_inv = tf.cond(
        pred=tf.less(a, 0.0), true_fn=lambda: l_inv_1, false_fn=lambda: l_inv_2)

    return tf.math.divide_no_nan(z_m, l_inv)

  def _compute_monin_obukhov_length_scale(self, u_star, temperature, heat_flux):
    """Computes the Monin-Obukhov length scale."""
    return [
        tf.math.divide_no_nan(-u_star_i**3 * t_i, _KAPPA * _G * heat_flux)
        for u_star_i, t_i in zip(u_star, temperature)
    ]

  def _compute_surface_heat(self, u_star):
    """Computes the surface heat -T*."""
    return [
        tf.math.divide_no_nan(self.heat_flux, u_star_i) for u_star_i in u_star
    ]

  def _compute_shear_stresses(self, u, v, z, replicas):
    """Computes the shear stresses 𝛕₀₂ and 𝛕₁₂."""
    u_norm = [tf.math.sqrt(u_i**2 + v_i**2) for u_i, v_i in zip(u, v)]
    u_mean = tf.squeeze(common_ops.global_mean(u_norm, replicas))
    u_star = tf.math.divide_no_nan(u_mean * _KAPPA,
                                   tf.math.log(z / self.z_0) - _PHI_M)
    return [tf.math.divide_no_nan(-u_star**2 * u_i, u_mean) for u_i in u
           ], [tf.math.divide_no_nan(-u_star**2 * v_i, u_mean) for v_i in v]

  def _compute_friction_velocity(self, u, v, z, replicas):
    """Computes the friction velocity."""
    tau_vertical_0, tau_vertical_1 = self._compute_shear_stresses(
        u, v, z, replicas)
    return [
        tf.math.pow(tau_0_i**2 + tau_1_i**2, 0.25)
        for tau_0_i, tau_1_i in zip(tau_vertical_0, tau_vertical_1)
    ]

  def _compute_nondimensional_gradient(self, u, v, temperature, z, replicas):
    """Computes the nondimensional gradient."""
    u_star = self._compute_friction_velocity(u, v, z, replicas)
    l = [
        -l_i for l_i in self._compute_monin_obukhov_length_scale(
            u_star, temperature, self.heat_flux)
    ]
    if self.heat_flux >= 0.0:
      return [
          tf.math.pow(
              tf.maximum(1.0 - tf.math.divide_no_nan(15.0 * z, l_i), 0.0),
              -0.25) for l_i in l
      ]
    return [1.0 + tf.math.divide_no_nan(4.7 * z, l_i) for l_i in l]

  def _compute_dimensional_gradient(self, f_star, phi, z):
    """Computes the dimensional gradient that is used for the Neumann BC."""
    return [
        tf.math.divide_no_nan(f_star_i * phi_i, _KAPPA * z)
        for f_star_i, phi_i in zip(f_star, phi)
    ]

  def _check_additional_states_keys(
      self,
      additional_states: FlowFieldMap,
      update_bc_t: bool,
  ) -> None:
    """Checks if all required keys exist in `additional_states`.

    Args:
      additional_states: A list of states that are needed by the update fn, but
        will not be updated by the main governing equations.
      update_bc_t: An indicator of whether the temperature boundary condition
        will be updated.

    Raises:
      ValueError: If not all required keys are contained in `additional_states`.
    """
    velocity_keys = [self.dim_to_v_key[dim] for dim in self.horizontal_dims]
    required_bc_keys = set()
    for horizontal_v_key in velocity_keys:
      bc_v_key = self.bc_manager.generate_bc_key(horizontal_v_key,
                                                 self.vertical_dim, 0)
      required_bc_keys.add(bc_v_key)
    required_t_bc_key = self.bc_manager.generate_bc_key('T', self.vertical_dim,
                                                        0)
    if not required_bc_keys.issubset(additional_states.keys()):
      raise ValueError(
          'Required fields {} missing from `additional_states`.'.format(
              required_bc_keys))

    if update_bc_t and required_t_bc_key not in additional_states.keys():
      raise ValueError(
          '{} is not in `additional_states` but needs to be updated'.format(
              required_t_bc_key))

  def init_fn(
      self,
      config: grid_parametrization.GridParametrization,
      coordinates: initializer.ThreeIntTuple,
      update_bc_t: bool,
  ) -> Mapping[Text, tf.Tensor]:
    """Generates the required initial fields by the simulation.

    Args:
      config: An instance of `grid_parametrization.GridParametrization`.
      coordinates: A tuple that specifies the replica's grid coordinates in
        physical space.
      update_bc_t: An option of whether the Monin-Obukhov Similarity Theory is
        applied to temperature. If true, the temperature boundary condition
        will be included (e.g. 'bc_T_2_0' if the height dimension is along
        the z direction). Otherwise, only the horizontal velocity components'
        boundary conditions will be included (e.g. 'bc_u_2_0' and 'bc_v_2_0' if
        the height dimension is along the z direction).

    Returns:
      A dictionary of state variables that are required by the Monin-Obukhov
      Similarity Theory.
    """

    def states_init(initial_value_fn) -> tf.Tensor:
      """Assigns value to a tensor with `initial_value_fn`."""
      return initializer.partial_mesh_for_core(
          config,
          coordinates,
          initial_value_fn,
          pad_mode='SYMMETRIC',
          num_boundary_points=0,
          mesh_choice=initializer.MeshChoice.PARAMS,
      )
    # pylint: disable=g-long-lambda
    init_fn_zeros = lambda xx, yy, zz, lx, ly, lz, coord: tf.zeros_like(
        xx, dtype=xx.dtype)
    # pylint: enable=g-long-lambda

    output = {}
    velocity_keys = [self.dim_to_v_key[dim] for dim in self.horizontal_dims]
    for horizontal_v_key in velocity_keys:
      bc_v_key = self.bc_manager.generate_bc_key(horizontal_v_key,
                                                 self.vertical_dim, 0)
      output.update({bc_v_key: states_init(init_fn_zeros)})
    if update_bc_t:
      bc_t_key = self.bc_manager.generate_bc_key('T', self.vertical_dim, 0)
      output.update({bc_t_key: states_init(init_fn_zeros)})

    return output

  def _psi_m(self, z_m, l):
    """The stability correction for momentum."""
    return tf.math.divide_no_nan(-self.beta_m * z_m, l)

  def _psi_h(self, z_m, l):
    """The stability correction for heat."""
    return tf.math.divide_no_nan(-self.beta_h * z_m, l)

  def _c_m(self, z_m, l):
    """The stability corrected log-law for momentum."""
    return tf.math.divide_no_nan(_KAPPA**2, (tf.math.log(z_m / self.z_0) -
                                             self._psi_m(z_m, l))**2)

  def _c_h(self, z_m, l):
    """The stability corrected log-law for heat."""
    return tf.math.divide_no_nan(
        _KAPPA**2, (tf.math.log(z_m / self.z_0) - self._psi_m(z_m, l)) *
        (self.alpha * tf.math.log(z_m / self.z_t) - self._psi_h(z_m, l)))

  def _tau_s_average(self, z_m, m, l):
    """The average surface stress."""
    return -self._c_m(z_m, l) * m**2

  def _q_s_average(self, z_m, m, t_m, t_s, l):
    """The average surface heat flux."""
    return -self._c_h(z_m, l) * m * (t_m - t_s)

  def _get_slice(
      self,
      f: FlowFieldVal,
      idx: int,
  ) -> FlowFieldVal:
    """Returns a horizontal slice of `f` at level `idx`."""
    slices = util.get_slice(f, self.vertical_dim, 0, idx)
    return slices if self.vertical_dim == 2 else slices[0]

  def _expand_state(
      self, f: FlowFieldVal,
      params: grid_parametrization.GridParametrization) -> FlowFieldVal:
    """Expands the state variable along the vertical dimension."""
    if self.vertical_dim == 2:
      return f * params.nz
    else:
      ns = [params.nx, params.ny]
      repeats = [1, 1]
      repeats[self.vertical_dim] = ns[self.vertical_dim]
      return [tf.tile(f_i, repeats) for f_i in f]

  def _get_horizontal_slices(
      self,
      states: FlowFieldMap,
      t: FlowFieldVal,
      params: grid_parametrization.GridParametrization,
      idx: int,
      strip_halos: bool = False,
  ):
    """Gets horizontal velocity components and temperature fields at `idx`."""
    halo = params.halo_width
    halos = [halo] * 3
    halos[self.vertical_dim] = 0
    dim_to_horizontal_velocity = {}
    for dim in self.horizontal_dims:
      v_key = self.dim_to_v_key[dim]
      horizontal_slice = self._get_slice(states[v_key], idx)
      dim_to_horizontal_velocity.update({dim: horizontal_slice})
    temperature = self._get_slice(t, idx)
    if strip_halos:
      dim_to_horizontal_velocity.update({
          dim: common_ops.strip_halos(f, halos)
          for dim, f in dim_to_horizontal_velocity.items()
      })
      temperature = common_ops.strip_halos(temperature, halos)
    return dim_to_horizontal_velocity, temperature

  def porte_agel_model_update_fn(
      self,
      kernel_op: get_kernel_fn.ApplyKernelOp,
      replica_id: tf.Tensor,
      replicas: np.ndarray,
      states: FlowFieldMap,
      additional_states: FlowFieldMap,
      params: grid_parametrization.GridParametrization,
  ) -> FlowFieldMap:
    """Computes the Neumann BC for u, v, T (optional) with Porte Agel's model.

    The wall shear stress is computed and applied as boundary conditions to the
    wall normal shear components of u and v (stream and spanwise velocity). The
    mean shear stress is computed based on the Monin-Obukhov similarity theory
    [1], which is applied locally by accounting for the velocity fluctuation in
    the first computation layer above the ground[2]. Note that field `T` must
    exist in either `states` or `additional_states`, but not both.

    Note that the computation is all based on the references, and the only
    exception is to avoid `nan`s with `tf.math.divide_no_nan`.

    References:
    [1] Stoll, Rob, and Fernando Porté-Agel. 2009. “Surface Heterogeneity
        Effects on Regional-Scale Fluxes in Stable Boundary Layers: Surface
        Temperature Transitions.” Journal of the Atmospheric Sciences 66 (2):
        412–31.
    [2] Porté-Agel, Fernando, Charles Meneveau, and Marc B. Parlange. 2000. “A
        Scale-Dependent Dynamic Model for Large-Eddy Simulation: Application to
        a Neutral Atmospheric Boundary Layer.” Journal of Fluid Mechanics 415
        (July): 261–84.

    Args:
      kernel_op: An object holding a library of kernel operations.
      replica_id: The id of the replica.
      replicas: The replicas. In particular, a numpy array that maps grid
        coordinates to replica id numbers.
      states: A keyed dictionary of states that will be updated. If `T` is in
        `states`, the boundary condition for `T` (a.k.a `bc_T_2_0`) will be
        updated.
      additional_states: A list of states that are needed by the update fn, but
        will not be updated by the main governing equations. If `T` is in
        `additional_states`, the boundary condition for `T` (a.k.a `bc_T_2_0`)
        will not be updated.
      params: An instance of `grid_parametrization.GridParametrization`.

    Returns:
      An update function for `additional_states` that updates the boundary
      condition.
    """
    del kernel_op, replica_id

    if 'T' in states.keys():
      update_bc_t = True
      state_t = states['T']
    elif 'T' in additional_states.keys():
      update_bc_t = False
      state_t = additional_states['T']
    else:
      raise ValueError('Field `T` is required to generate the Neumann boundary '
                       'condition with the Monin-Obukhov similarity theory, '
                       'but is not found.')

    dh = [params.dx, params.dy, params.dz]
    height_m = dh[self.vertical_dim]

    dim_to_horizontal_velocity, t = self._get_horizontal_slices(
        states, state_t, params, params.halo_width)
    horizontal_velocity_fields = list(dim_to_horizontal_velocity.values())

    nu_slice = self._get_slice(additional_states['nu_t'], params.halo_width)
    nu = [nu_slice_i + self.nu for nu_slice_i in nu_slice]
    v_0_sq = [v_i**2 for v_i in horizontal_velocity_fields[0]]
    v_1_sq = [v_i**2 for v_i in horizontal_velocity_fields[1]]
    m = [tf.math.sqrt(v_0_i + v_1_i) for v_0_i, v_1_i in zip(v_0_sq, v_1_sq)]

    m_avg = tf.squeeze(
        common_ops.global_mean(m, replicas, axis=self.horizontal_dims)[0])
    t_avg = tf.squeeze(
        common_ops.global_mean(t, replicas, axis=self.horizontal_dims)[0])

    l = self._compute_obukhov_length(m_avg, t_avg, height_m)

    tau_s_avg = self._tau_s_average(height_m, m_avg, l)

    tau = {}
    for dim, v in dim_to_horizontal_velocity.items():
      tau.update(
          {dim: [tf.math.divide_no_nan(-tau_s_avg * v_i, m_avg) for v_i in v]})

    # Regularizes the change in velocity so that flow at the boundary is not
    # in the reverted direction.
    dv = {}
    for dim, u in dim_to_horizontal_velocity.items():
      dv.update({dim: [
          tf.sign(u_i) * tf.minimum(
              tf.abs(tf.math.divide_no_nan(tau_i * height_m, nu_i)),
              tf.abs(u_i)) for u_i, tau_i, nu_i in zip(u, tau[dim], nu)
      ]})

    additional_states_new = {}
    most_bc_keys = set()
    for dim in dim_to_horizontal_velocity:
      bc_key_v = self.bc_manager.generate_bc_key(self.dim_to_v_key[dim],
                                                 self.vertical_dim, 0)
      if bc_key_v in additional_states:
        most_bc_keys.add(bc_key_v)
        additional_states_new.update(
            {bc_key_v: self._expand_state(dv[dim], params)})
      bc_key_tau = (
          'bc_tau{horizontal_dim}{vertical_dim}_{vertical_dim}_0').format(
              horizontal_dim=dim, vertical_dim=self.vertical_dim)
      if bc_key_tau in additional_states:
        most_bc_keys.add(bc_key_tau)
        additional_states_new.update(
            {bc_key_tau: self._expand_state(tau[dim], params)})

    additional_states_new.update(
        {k: v for k, v in additional_states.items() if k not in most_bc_keys})

    if update_bc_t:
      q_s_avg = self._q_s_average(height_m, m_avg, t_avg, self.t_s, l)

      tau_t_vertical = [-q_s_avg * tf.math.divide_no_nan(
          (m_i * (t_avg - self.t_s) + m_avg * (t_i - t_avg)),
          (m_avg * (t_avg - self.t_s))) * height_m for m_i, t_i in zip(m, t)]
      # Regularizes the temperature change so that the temperature at the
      # ground will not drop below the reference surface temperature.
      dt_max = t_avg - self.t_s
      dt = [
          tf.sign(dt_max) * tf.minimum(
              tf.abs(tau_t_vertical_i * height_m / nu_i), tf.abs(dt_max))
          for tau_t_vertical_i, nu_i in zip(tau_t_vertical, nu)
      ]
      bc_t_key = self.bc_manager.generate_bc_key('T', self.vertical_dim, 0)
      additional_states_new.update({bc_t_key: self._expand_state(dt, params)})
      bc_tau_t_key = 'bc_tauT{vertical_dim}_{vertical_dim}_0'.format(
          vertical_dim=self.vertical_dim)
      if bc_tau_t_key in additional_states:
        additional_states_new.update(
            {bc_tau_t_key: self._expand_state(tau_t_vertical, params)})

    return additional_states_new

  def moeng_model_update_fn(
      self,
      kernel_op: get_kernel_fn.ApplyKernelOp,
      replica_id: tf.Tensor,
      replicas: np.ndarray,
      states: FlowFieldMap,
      additional_states: FlowFieldMap,
      params: grid_parametrization.GridParametrization,
  ) -> FlowFieldMap:
    """Computes the Neumann BC for u, v, and T (optional) with Moeng's model.

    The boundary condition is updated for a wall following the Monin-Obukhov
    Similarity Theory, where `additional_states` with key `bc_u_2_0`,
    'bc_v_2_0', and 'bc_T_2_0 (optional) are updated and to be used as Neuamnn
    boundary conditions. Field `T` must exist in either `states` or
    `additional_states`, but not both.

    For F as a short-hand notation for u, v, and T:

    ∂F/∂z = φ(z/L)F* / κz. [2]
      where:
        κ: The von Karman constant (0.4),
        F*: for u, v, and T are computed as:
          u∗ = (𝛕₀₂² + 𝛕₁₂²)¹/⁴ is the friction velocity [1],
            where:
              𝛕ⱼ₂ = -[U(z)κ / {ln(z/z₀) − ΨM}]² uⱼ / U(z) [3],
                where:
                  U(z): <u>, the mean resolved horizontal velocity,
                  ΨM: The stability correction for momentum, in the case of
                      neutral stability, ΨM = 0
                  z₀:  The roughness length.
          T* = (w'T') / u∗ [2]
            where:
              w'T': The surface heat flux.
        φ(z/L) [1]:
          = [1 - (15 z/L)]⁻¹/⁴, for positive heat flux, w'T' (surface heating);
          = 1 + (4.7z/L), negative heat flux, w'T' (surface cooling).
            where:
              L ≡ u∗³ T/(κg (w'T')) [2] is the Monin-Obukhov length scale.
                where:
                  g: The acceleration of gravity,

    The following reference is use in this implementation:
    [1] Moeng, C.H., 1984. A large-eddy-simulation model for the study of
        planetary boundary-layer turbulence. Journal of the Atmospheric
        Sciences, 41(13), pp.2052-2062.
    [2] Mahrt, L., 2014. Stably stratified atmospheric boundary layers. Annual
        Review of Fluid Mechanics, 46, pp.23-45.
    [3] Porté-Agel, F., Meneveau, C. and Parlange, M.B., 2000. A scale-dependent
        dynamic model for large-eddy simulation: application to a neutral
        atmospheric boundary layer. Journal of Fluid Mechanics, 415, pp.261-284.

    Args:
      kernel_op: An object holding a library of kernel operations.
      replica_id: The id of the replica.
      replicas: The replicas. In particular, a numpy array that maps grid
        coordinates to replica id numbers.
      states: A keyed dictionary of states that will be updated. If `T` is in
        `states`, the boundary condition for `T` (a.k.a `bc_T_2_0`) will be
        updated.
      additional_states: A list of states that are needed by the update fn, but
        will not be updated by the main governing equations. If `T` is in
        `additional_states`, the boundary condition for `T` (a.k.a `bc_T_2_0`)
        will not be updated.
      params: An instance of `grid_parametrization.GridParametrization`.

    Returns:
      An update function for `additional_states` that updates the boundary
      condition.

    Raises:
      ValueError: If 'T' is not found in neither `states` nor
        `additional_states`.
    """
    del kernel_op, replica_id

    if 'T' in states.keys():
      update_bc_t = True
      t_full = states['T']
    elif 'T' in additional_states.keys():
      update_bc_t = False
      t_full = additional_states['T']
    else:
      raise ValueError('Field `T` is required to generate the Neumann boundary '
                       'condition with the Monin-Obukhov similarity theory, '
                       'but is not found.')

    self._check_additional_states_keys(additional_states, update_bc_t)

    dh = [params.dx, params.dy, params.dz]
    height = dh[self.vertical_dim]

    dim_to_horizontal_velocity, temperature = self._get_horizontal_slices(
        states, t_full, params, params.halo_width, strip_halos=True)
    horizontal_velocity_fields = list(dim_to_horizontal_velocity.values())

    phi = self._compute_nondimensional_gradient(horizontal_velocity_fields[0],
                                                horizontal_velocity_fields[1],
                                                temperature, height, replicas)
    u_star = self._compute_friction_velocity(horizontal_velocity_fields[0],
                                             horizontal_velocity_fields[1],
                                             height, replicas)

    paddings = [(params.halo_width, params.halo_width)] * 3
    paddings[self.vertical_dim] = (0, 0)
    dimensional_grad = self._compute_dimensional_gradient(u_star, phi, height)
    du = [dg_i * height for dg_i in dimensional_grad]
    du = common_ops.pad(du, paddings, value=0.0)

    additional_states_new = {}
    most_bc_keys = set()
    for dim in dim_to_horizontal_velocity:
      bc_key_v = self.bc_manager.generate_bc_key(self.dim_to_v_key[dim],
                                                 self.vertical_dim, 0)
      if bc_key_v in additional_states:
        most_bc_keys.add(bc_key_v)
        additional_states_new.update({bc_key_v: self._expand_state(du, params)})

    for key, value in additional_states.items():
      if key not in most_bc_keys:
        additional_states_new.update({key: value})

    if update_bc_t:
      t_star = self._compute_surface_heat(u_star)
      dimensional_grad = self._compute_dimensional_gradient(t_star, phi, height)
      dt = [dg_i * height for dg_i in dimensional_grad]
      dt = common_ops.pad(dt, paddings, value=0.0)
      bc_t_key = self.bc_manager.generate_bc_key('T', self.vertical_dim, 0)
      additional_states_new.update({bc_t_key: self._expand_state(dt, params)})

    return additional_states_new


def monin_obukhov_similarity_theory_factory(
    params: incompressible_structured_mesh_config
    .IncompressibleNavierStokesParameters,
) -> MoninObukhovSimilarityTheory:
  """Generaets an object of `MoninObukhovSimilarityTheory`.

  Args:
    params: A object of the simulation parameter context. `boundary_models.most`
      and `nu` are used here.

  Returns:
    An instance of the `MoninObukhovSimilarityTheory` object.

  Raises:
    ValueError: If `most` is not defined in the parameter context.
    ValueError: If `gravity_direction` is absent, or is not aligned with a
      particular dimension.
  """
  if not params.boundary_models.HasField('most'):
    raise ValueError(
        'Parameters for the Monin-Obukhov boundary layer model are not defined '
        'in the config.'
    )

  eps = np.finfo(np.float32).resolution
  vertical_dim = -1
  for i in range(3):
    if abs(abs(params.gravity_direction[i]) - 1.0) < eps:
      if vertical_dim == -1:
        vertical_dim = i
      else:
        raise ValueError(
            f'More than one dimension is defined as gravity dimension '
            f'({vertical_dim} and {i}), but only one is allowed by the '
            f'Monin-Obukhov boundary layer model.')

  if vertical_dim == -1:
    raise ValueError(
        'Gravity must be defined to use the Monin-Obukhov boundary layer '
        'model.')

  return MoninObukhovSimilarityTheory(params.boundary_models.most, params.nu,
                                      vertical_dim, (params.dx, params.dy,
                                                     params.dz)[vertical_dim],
                                      params.halo_width)
