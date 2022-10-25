import os
from typing import Any, Dict, Tuple, Union

import jax
import jax.numpy as jnp
import tensorflow_probability.substrates.jax.distributions as tfd
from chex import Array
from haiku._src.basic import merge_leading_dims
from jax.config import config as jax_config

from mava.types import OLT


def action_mask_categorical_policies(
    distribution: tfd.Categorical, mask: Array
) -> tfd.Categorical:
    """TODO Add description"""
    masked_logits = jnp.where(
        mask.astype(bool),
        distribution.logits,
        jnp.finfo(distribution.logits.dtype).min,
    )

    return tfd.Categorical(logits=masked_logits, dtype=distribution.dtype)


def init_norm_params(stats_shape: Tuple) -> Dict[str, Union[jnp.array, float]]:
    """Initialise normalistion parameters"""

    stats = dict(
        mean=jnp.zeros(shape=stats_shape),
        var=jnp.zeros(shape=stats_shape),
        std=jnp.ones(shape=stats_shape),
        count=jnp.array([1e-4]),
    )

    return stats


def compute_running_mean_var_count(
    stats: Dict[str, Union[jnp.array, float]],
    batch: jnp.ndarray,
    start_axes: int = 0,
) -> jnp.ndarray:
    """Updates the running mean, variance and data counts during training.

    stats (Any)   -- dictionary with running mean, var, std, count
    batch (array) -- current batch of data.
    start_axes (int or None) -- number of axes we do not want to normalise

    Returns:
        stats (array)
    """

    batch_mean = jnp.mean(batch, axis=0)
    batch_var = jnp.var(batch, axis=0)
    batch_count = batch.shape[0]

    mean, var, count = stats["mean"], stats["var"], stats["count"]

    delta = batch_mean - mean
    tot_count = count + batch_count

    mean = mean + delta * batch_count / tot_count
    m_a = var * count
    m_b = batch_var * batch_count
    M2 = m_a + m_b + jnp.square(delta) * count * batch_count / tot_count
    var = M2 / tot_count
    std = jnp.sqrt(var)
    new_count = tot_count

    # This assumes the all the features we don't want to
    # normalise are all always at the front.
    new_mean = jnp.zeros_like(mean)
    new_var = jnp.zeros_like(var)
    new_std = jnp.ones_like(std)

    new_mean = new_mean.at[start_axes:].set(mean[start_axes:])
    new_var = new_var.at[start_axes:].set(var[start_axes:])
    new_std = new_std.at[start_axes:].set(std[start_axes:])

    return dict(mean=new_mean, var=new_var, std=new_std, count=new_count)


def normalize(
    stats: Dict[str, Union[jnp.array, float]], batch: jnp.ndarray
) -> jnp.ndarray:
    """Normlaise batch of data using the running mean and variance.

    stats (Any)   -- dictionary with running mean, var, std, count.
    batch (array) -- current batch of data.

    Returns:
        denormalize batch (array)
    """

    mean, std = stats["mean"], stats["std"]
    normalize_batch = (batch - mean) / jnp.fmax(std, 1e-6)

    return normalize_batch


def denormalize(
    stats: Dict[str, Union[jnp.array, float]], batch: jnp.ndarray
) -> jnp.ndarray:
    """Transform normalized data back into original distribution

    stats (Any)   -- dictionary with running mean, var, count.
    batch (array) -- current batch of data

    Returns:
        denormalize batch (array)
    """

    mean, std = stats["mean"], stats["std"]
    denormalize_batch = batch * jnp.fmax(std, 1e-6) + mean

    return denormalize_batch


def update_and_normalize_observations(
    stats: Dict[str, Union[jnp.array, float]],
    observation: OLT,
    start_axes: int = 0,
) -> Tuple[Any, OLT]:
    """Update running stats and normalise observations

    stats (Dictionary)   -- array with running mean, var, count.
    batch (OLT namespace)   -- current batch of data for a single agent.

    Returns:
        normalize batch (Dictionary)
    """

    obs_shape = observation.observation.shape
    obs = jax.tree_util.tree_map(
        lambda x: merge_leading_dims(x, num_dims=2), observation.observation
    )
    upd_stats = compute_running_mean_var_count(stats, obs, start_axes)
    norm_obs = normalize(upd_stats, obs)

    # the following code makes sure we do not normalise
    # death masked observations. This uses the assumption
    # that all death masked agens have zeroed observations
    sum_obs = jnp.sum(obs[:, start_axes:], axis=1)
    mask = jnp.array(sum_obs != 0, dtype=obs.dtype)
    norm_obs = norm_obs.at[:, start_axes:].set(norm_obs[:, start_axes:] * mask[:, None])

    # reshape before returning
    norm_obs = jnp.reshape(norm_obs, obs_shape)

    return upd_stats, observation._replace(observation=norm_obs)


def normalize_observations(
    stats: Dict[str, Union[jnp.array, float]], observation: OLT
) -> OLT:
    """Normalise a single observation

    stats (Dictionary)   -- array with running mean, var, count.
    batch (OLT namespace) -- current batch of data in for an agent

    Returns:
        denormalize batch (Dictionary)
    """

    # The type casting is required because we need to preserve
    # the data type before the policy info is computed else we will get
    # an error from the table about the type of policy being double instead of float.
    dtype = observation.observation.dtype
    stats_cast = {key: jnp.array(value, dtype=dtype) for key, value in stats.items()}

    obs = observation.observation
    norm_obs = normalize(stats_cast, obs)

    return observation._replace(observation=norm_obs)


def set_growing_gpu_memory_jax() -> None:
    """Solve gpu mem issues.

    More on this - https://jax.readthedocs.io/en/latest/gpu_memory_allocation.html.
    """
    os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"


def set_jax_double_precision() -> None:
    """Set JAX to use double precision.

    This is usually for env that use int64 action space due to the use of spac.Discrete.

    More on this - https://jax.readthedocs.io/en/latest/notebooks/Common_Gotchas_in_JAX.html#double-64bit-precision. # noqa: E501
    """
    jax_config.update("jax_enable_x64", True)
