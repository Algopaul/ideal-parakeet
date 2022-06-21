"""Library for the incompressible structured mesh Navier-Stokes solver."""

import functools
import os
from typing import Any, Dict, Optional, Tuple, TypeVar

from absl import flags
import numpy as np
from swirl_lm.base import driver_tpu
from swirl_lm.base import parameters as parameters_lib
from swirl_lm.core import simulation
from swirl_lm.utility import get_kernel_fn
import tensorflow as tf

from google3.research.simulation.tensorflow.fluid.framework import checkpoint_handler
from google3.research.simulation.tensorflow.fluid.framework import util
from google3.research.simulation.tensorflow.fluid.framework.tf1 import parallel_io
# tpu_simulation_manager needed for 'target' flag
from google3.research.simulation.tensorflow.fluid.framework.tf1 import tpu_simulation_manager  # pylint: disable=unused-import
# BEGIN GOOGLE-INTERNAL
from google3.research.simulation.tensorflow.fluid.models.incompressible_structured_mesh import legacy_solvers
# END GOOGLE-INTERNAL

# TODO(b/142351512) Clean up the common flags. Consolidate them to a
# configuration proto and use a common module to parse them.
flags.DEFINE_integer('num_steps', 1, 'number of steps to run before generating '
                     'an output.')
flags.DEFINE_integer('start_step', 0,
                     'The beginning step count for the current simulation.')
flags.DEFINE_integer(
    'loading_step', None,
    'When this is set, it is the step count from which to '
    'load the initial states.')
flags.DEFINE_integer(
    'min_steps_for_output', 1, 'Total number of steps before '
    'the output start to be generated.')
flags.DEFINE_integer(
    'num_cycles', 1, 'number of cycles to run. Each cycle '
    'generates a set of output')
flags.DEFINE_string(
    'data_dump_prefix', '/tmp/data', 'The output `ser` or `h5` '
    'files prefix. This will be suffixed with the field '
    'components and step count.')
flags.DEFINE_string(
    'data_load_prefix', '/tmp/data', 'The input `ser` or `h5` '
    'files prefix. This will be suffixed with the field '
    'components and step count.')
flags.DEFINE_bool(
    'apply_data_load_filter', False,
    'If True, only variables with names provided in field `variable_from_file` '
    'in the config file will be loaded from files (at step specified by flag '
    '`loading_step`.')
flags.DEFINE_enum_class(
    'input_format', parallel_io.FileFormat.SER, parallel_io.FileFormat,
    'Input file format for the checkpoint files to be loaded. Currently both '
    '`ser` (serialized Tensor proto) and `h5` (HDF5 format) are supported.')
flags.DEFINE_multi_enum_class(
    'output_formats', [parallel_io.FileFormat.SER], parallel_io.FileFormat,
    'A list indicating output file formats. Currently both `ser` (serialized '
    'Tensor proto) and `h5` (HDF5 format) are supported.')
flags.DEFINE_bool(
    'apply_auto_ckpt', True,
    'An indicator of whether to save and load data files based on a checkpoint '
    'record created by the parallel IO library.')
flags.DEFINE_enum_class(
    'ckpt_in_out_option', checkpoint_handler.CkptFormat.GROUP_BY_ID,
    checkpoint_handler.CkptFormat,
    'An option for how data files are loaded and saved. Available options are: '
    '"ALL_IN_ONE_DIR", "GROUP_BY_ID". With option "ALL_IN_ONE_DIR", all data '
    'will be written into the same directory. With option "GROUP_BY_ID", data '
    'will be written in subdirectories named by step ID under the root '
    'directory.')
flags.DEFINE_bool(
    'apply_preprocess', False,
    'If True and the `preprocessing_states_update_fn` is defined in `params`, '
    'data from initial condition are processed before the simulation.'
)
flags.DEFINE_integer(
    'preprocess_step_id', 0,
    'The `step_id` for the preprocessing function to be executed at, or if '
    '`preprocess_periodic` is `True`, the period in steps to perform '
    'preprocessing.')
flags.DEFINE_bool(
    'preprocess_periodic', False, 'Whether to do preprocess periodically.')
flags.DEFINE_bool(
    'apply_postprocess', False,
    'If True and the `postprocessing_states_update_fn` is defined in `params`, '
    'a post processing will be executed after the update.')
flags.DEFINE_integer(
    'postprocess_step_id', 0,
    'The `step_id` for the postprocessing function to be executed at, or if '
    '`postprocess_periodic` is `True`, the period in steps to perform '
    'postprocessing.')
flags.DEFINE_bool(
    'postprocess_periodic', False, 'Whether to do postprocess periodically.')

FLAGS = flags.FLAGS

CKPT_DIR_FORMAT = '{filename_prefix}-ckpts/'

Array = Any
PerReplica = Any
Structure = Any
T = TypeVar('T')
S = TypeVar('S')


def _stateless_update_if_present(mapping: Dict[T, S],
                                 updates: Dict[T, S]) -> Dict[T, S]:
  """Returns a copy of `mapping` with only existing keys updated."""
  mapping = mapping.copy()
  mapping.update({key: val for key, val in updates.items() if key in mapping})
  return mapping


def _get_checkpoint_manager(
    step_id: Array,
    output_dir: str,
    filename_prefix: str,
) -> tf.train.CheckpointManager:
  """Returns a `tf.train.CheckpointManager` that records just the step id."""
  checkpoint = tf.train.Checkpoint(step_id=step_id)
  checkpoint_dir = os.path.join(
      output_dir, CKPT_DIR_FORMAT.format(filename_prefix=filename_prefix))
  return tf.train.CheckpointManager(
      checkpoint, directory=checkpoint_dir, max_to_keep=3)


def _local_state(
    strategy: tf.distribute.Strategy,
    distributed_state: tf.distribute.DistributedValues,
) -> Tuple[Structure]:
  """Retrieves the local results from a `tf.distribute.DistributedValues`."""
  # In the single replica case, the state returned by strategy.run is a
  # dictionary of Tensors instead of PerReplica object. Since
  # `distributed_write_state` expects a tuple of length `num_replicas`, we
  # convert the state to a single element tuple.
  if strategy.num_replicas_in_sync == 1:
    return (distributed_state,)
  return strategy.experimental_local_results(distributed_state)


def _get_state_keys(params):
  """Returns essential, additional and helper var state keys."""
  # TODO(b/149124896): Clean up `states` and `additional_states` so that to
  # prevent allocating redundant memory.
  # Essential flow field variables:
  # u: velocity in dimension 0;
  # v: velocity in dimension 1;
  # w: velocity in dimension 2;
  # p: pressure.
  essential_keys = ['u', 'v', 'w', 'p'] + params.transport_scalars_names
  if params.solver_procedure == parameters_lib.SolverProcedure.VARIABLE_DENSITY:
    essential_keys += ['rho']
  additional_keys = list(
      params.additional_state_keys if params.additional_state_keys else [])
  helper_var_keys = list(
      params.helper_var_keys if params.helper_var_keys else [])
  for state_analytics_info in params.monitor_spec.state_analytics:
    for analytics_spec in state_analytics_info.analytics:
      helper_var_keys.append(analytics_spec.key)

  return essential_keys, additional_keys, helper_var_keys


def _get_model(kernel_op, params):
  """Returns the appropriate Navier-Stokes model from `params`."""
  # TODO(anudhyan): We should introduce a V2 ModelFn interface.
  if params.solver_procedure == parameters_lib.SolverProcedure.VARIABLE_DENSITY:
    return simulation.Simulation(kernel_op, params)
  # BEGIN GOOGLE-INTERNAL
  elif params.solver_procedure == parameters_lib.SolverProcedure.SEQUENTIAL:
    raise ValueError('Sequential solver is not yet supported in TF2 mode.')
  elif (params.solver_procedure ==
        parameters_lib.SolverProcedure.PREDICTOR_CORRECTOR):
    return (legacy_solvers
            .IncompressibleStructuredMeshNavierStokesPredictorCorrector(
                kernel_op, params))
  # END GOOGLE-INTERNAL
  raise ValueError('Solver procedure not recognized: '
                   f'{params.solver_procedure}')


def _process_at_step_id(process_fn, essential_states, additional_states,
                        step_id, process_step_id, is_periodic):
  """Executes `process_fn` conditionally depending on `step_id`.

  Args:
    process_fn: Function accepting `essential_states` and `additional_states`,
      and returning the updated values of individual states in a dictionary.
    essential_states: The essential states, corresponds to the`states` keyword
      argument of `process_fn`.
    additional_states: The additional states, corresponds to the
      `additional_states` keyword argument of `process_fn`.
    step_id: The current step id.
    process_step_id: The fixed value at which to trigger execution of
      `process_fn`. In other words, if the current step id is equal to
      `process_step_id`, then the execution of `process_fn` is triggered. If,
      additionally, `is_periodic` is True, then the execution is also triggered
      whenever `step_id % process_step_id == 0`.
    is_periodic: If True, then `process_fn` is executed whenever `step_id %
      process_step_id == 0`.

  Returns:
    The updated `essential_states` and `additional_states`.
  """
  should_process = ((step_id % process_step_id == 0)
                    if is_periodic else step_id == process_step_id)
  if should_process:
    updated_states = process_fn(
        states=essential_states, additional_states=additional_states)
    essential_states = _stateless_update_if_present(essential_states,
                                                    updated_states)
    additional_states = _stateless_update_if_present(additional_states,
                                                     updated_states)

  return essential_states, additional_states


@tf.function
def _one_cycle(
    strategy: tf.distribute.Strategy, init_state: PerReplica,
    init_step_id: Array, num_steps: Array,
    params: parameters_lib.SwirlLMParameters) -> PerReplica:
  """Runs one cycle of the Navier-Stokes solver.

  Args:
    strategy: A distributed strategy to run computations. Currently, only
      `TPUStrategy` is supported.
    init_state: The initial state on each device.
    init_step_id: An integer scalar denoting the initial step id at which to
      start the cycle.
    num_steps: An integer scalar; the number of steps to run in the cycle.
    params: The solver configuration.

  Returns:
    The final state at the end of the cycle.
  """
  essential_keys, additional_keys, helper_var_keys = _get_state_keys(params)
  computation_shape = np.array([params.cx, params.cy, params.cz])
  logical_replicas = np.arange(
      strategy.num_replicas_in_sync, dtype=np.int32).reshape(computation_shape)

  kernel_op = get_kernel_fn.ApplyKernelConvOp(params.kernel_size)
  model = _get_model(kernel_op, params)

  def step_fn(state):
    # Common keyword arguments to various step functions.
    common_kwargs = dict(
        kernel_op=kernel_op,
        replica_id=state['replica_id'],
        replicas=logical_replicas,
        params=params)
    # Split essential/additional states into lists of 2D Tensors.
    keys_to_split = (
        set.union(set(essential_keys), set(additional_keys)) -
        set(helper_var_keys))
    for key in keys_to_split:
      state[key] = tf.unstack(state[key])

    for cycle_step_id in tf.range(num_steps):
      step_id = init_step_id + cycle_step_id
      # Split the state into essential states and additional states. Note that
      # `additional_states` consists of both additional state keys and helper
      # var keys.
      additional_states = dict(
          (key, state[key]) for key in additional_keys + helper_var_keys)
      essential_states = dict((key, state[key]) for key in essential_keys)

      # Perform a preprocessing step, if configured.
      if FLAGS.apply_preprocess:
        essential_states, additional_states = _process_at_step_id(
            process_fn=functools.partial(params.preprocessing_states_update_fn,
                                         **common_kwargs),
            essential_states=essential_states,
            additional_states=additional_states,
            step_id=step_id,
            process_step_id=FLAGS.preprocess_step_id,
            is_periodic=FLAGS.preprocess_periodic)

      # Perform the additional states update, if present.
      if params.additional_states_update_fn is not None:
        additional_states = params.additional_states_update_fn(
            states=essential_states,
            additional_states=additional_states,
            step_id=step_id,
            **common_kwargs)

      # Perform one step of the Navier-Stokes model. The updated state should
      # contain both the essential and additional states.
      updated_state = model.step(
          replica_id=state['replica_id'],
          replicas=logical_replicas,
          step_id=step_id,
          states=essential_states,
          additional_states=additional_states)

      # Perform a postprocessing step, if configured.
      if FLAGS.apply_postprocess:
        essential_states, additional_states = _process_at_step_id(
            process_fn=functools.partial(params.postprocessing_states_update_fn,
                                         **common_kwargs),
            essential_states=essential_states,
            additional_states=additional_states,
            step_id=step_id,
            process_step_id=FLAGS.postprocess_step_id,
            is_periodic=FLAGS.postprocess_periodic)

      # Some state keys such as `replica_id` may not lie in either of the three
      # categories. Just pass them through.
      state = _stateless_update_if_present(state, updated_state)

    # Unsplit the keys that were previously split.
    for key in keys_to_split:
      state[key] = tf.stack(state[key])

    return state

  return strategy.run(step_fn, args=(init_state,))


def solver(
    init_fn,
    params_input: Optional[parameters_lib.SwirlLMParameters] = None,
):
  """Runs the Navier-Stokes Solver with TF2 Distribution strategy.

  Args:
    init_fn: The function that initializes the flow field. The function needs to
      be replica dependent.
    params_input: An instance of parameters that will be used in the simulation,
      e.g. the mesh size, fluid properties.

  Returns:
    A tuple of the final state on each replica.
  """
  # Obtain params either from the provided input or the flags.
  params = params_input
  if params is None:
    params = parameters_lib.params_from_config_file_flag()

  # Initialize the TPU.
  # TODO(anudhyan): Create a compound data structure, say a DistributionStrategy
  # class, to associate the TPUStrategy and the various helper arrays:
  # computation_shape, DeviceAssignment, logical_replicas, logical_coordinates.
  # This should reduce boilerplate and help in factoring out modular functions.
  computation_shape = np.array([params.cx, params.cy, params.cz])
  strategy = driver_tpu.initialize_tpu(
      tpu_address=FLAGS.target, computation_shape=computation_shape)
  logical_coordinates = util.grid_coordinates(computation_shape).tolist()

  # In order to save and restore from the filesystem, we use tf.train.Checkpoint
  # on the step id. The `step_id` Variable should be placed on the TPU device so
  # that we don't block TPU execution when writing the state to filenames
  # formatted dynamically with the step id.
  with strategy.scope():
    step_id = tf.Variable(FLAGS.start_step, dtype=tf.int32)

  output_dir, filename_prefix = os.path.split(FLAGS.data_dump_prefix)
  ckpt_manager = _get_checkpoint_manager(
      step_id=step_id, output_dir=output_dir, filename_prefix=filename_prefix)
  write_state = functools.partial(
      driver_tpu.distributed_write_state,
      strategy,
      logical_coordinates=logical_coordinates,
      output_dir=output_dir,
      filename_prefix=filename_prefix,
      step_id=step_id)
  read_state = functools.partial(
      driver_tpu.distributed_read_state,
      strategy,
      logical_coordinates=logical_coordinates,
      output_dir=output_dir,
      filename_prefix=filename_prefix,
      step_id=step_id)

  # Prepare the initial state in each replica.
  state = driver_tpu.distribute_values(
      strategy, value_fn=init_fn, logical_coordinates=logical_coordinates)
  # In the single core case, strategy.run returns a Tensor instead of a
  # PerReplica object. Using the Tensor as the state ensures that the structure
  # remains the same in the TF while loops.
  if strategy.num_replicas_in_sync == 1:
    state, = strategy.experimental_local_results(state)

  # Restore from an existing checkpoint if present.
  if ckpt_manager.latest_checkpoint:
    # The checkpoint restore updates the `step_id` variable; which is then used
    # to read in the state.
    ckpt_manager.restore_or_initialize()
    state = read_state(_local_state(strategy, state))
  else:
    # In case we're not restoring from a checkpoint, write the initial state.
    write_state(_local_state(strategy, state))

  # Run the solver for multiple cycles and save the state after each cycle.
  if (step_id - FLAGS.start_step) % FLAGS.num_steps != 0:
    raise ValueError('Incompatible step_id detected. `step_id` is expected '
                     'to be `start_step` + N * `num_steps` but (step_id: {}, '
                     'start_step: {}, num_steps: {}) is detected. Maybe the '
                     'checkpoint step is inconsistent?'.format(
                         step_id, FLAGS.start_step, FLAGS.num_steps))
  while step_id < FLAGS.start_step + FLAGS.num_steps * FLAGS.num_cycles:
    state = _one_cycle(
        strategy=strategy,
        init_state=state,
        init_step_id=step_id,
        num_steps=FLAGS.num_steps,
        params=params)
    # Make sure we first increment `step_id`, write the state, and then save
    # a checkpoint. `write_state` should have automatic control dependencies
    # on `step_id` since it uses the `step_id` for the filename; but we need to
    # add the latter dependency explicitly.
    step_id.assign_add(FLAGS.num_steps)
    with tf.control_dependencies([write_state(_local_state(strategy, state))]):
      ckpt_manager.save()

  return strategy.experimental_local_results(state)
