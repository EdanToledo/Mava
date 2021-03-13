# python3
# Copyright 2021 InstaDeep Ltd. All rights reserved.
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


"""A multi-agent/environment training loop for Petting Zoo."""
from typing import Any, Dict, Optional

import dm_env
import numpy as np
import operator
import time
import tree
from acme.utils import counting, loggers
from dm_env import specs

import acme
import mava
from mava.wrappers.pettingzoo import PettingZooAECEnvWrapper
from mava.wrappers.pettingzoo import PettingZooParallelEnvWrapper


class PettingZooAECEnvironmentLoop(acme.core.Worker):
    """A Petting Zoo RL environment loop.
    This takes `Environment` and list of `Actor` instances and coordinates their
    interaction. Agents are updated if `should_update=True`. This can be used as:
        loop = EnvironmentLoop(environment, actors)
        loop.run(num_episodes)
    A `Counter` instance can optionally be given in order to maintain counts
    between different Acme components. If not given a local Counter will be
    created to maintain counts between calls to the `run` method.
    A `Logger` instance can also be passed in order to control the output of the
    loop. If not given a platform-specific default logger will be used as defined
    by utils.loggers.make_default_logger. A string `label` can be passed to easily
    change the label associated with the default logger; this is ignored if a
    `Logger` instance is given.
    """

    def __init__(
        self,
        environment: PettingZooAECEnvWrapper,
        executor: mava.core.Executor,
        counter: counting.Counter = None,
        logger: loggers.Logger = None,
        should_update: bool = True,
        label: str = "petting_zoo_environment_loop",
    ):
        # Internalize agent and environment.
        self._environment = environment
        self._executor = executor
        self._counter = counter or counting.Counter()
        self._logger = logger or loggers.make_default_logger(label)
        self._should_update = should_update

    def _get_action(self, timestep: dm_env.TimeStep, agent_id: str) -> Any:
        return self._executor.agent_select_action(agent_id, timestep.observation)

    def run_episode(self) -> loggers.LoggingData:
        """Run one episode.
        Each episode is a loop which interacts first with the environment to get an
        observation and then give that observation to the agent in order to retrieve
        an action.
        Returns:
            An instance of `loggers.LoggingData`.
        """
        # Reset any counts and start the environment.
        start_time = time.time()
        episode_steps = 0

        timestep = self._environment.reset()
        agent = self._environment.agent_selection

        # Make the first observation.
        self._executor.agent_observe_first(agent, timestep)

        n_agents = self._environment.num_agents
        rewards = {
            agent: _generate_zeros_from_spec(spec)
            for agent, spec in self._environment.reward_spec().items()
        }

        # For evaluation, this keeps track of the total undiscounted reward
        # for each agent accumulated during the episode.
        multiagent_reward_spec = specs.Array((n_agents,), np.float32)
        episode_return = tree.map_structure(
            _generate_zeros_from_spec, multiagent_reward_spec
        )

        # Run an episode.
        while not timestep.last():

            # Generate an action from the agent's policy and step the environment.
            for agent in self._environment.agent_iter(n_agents):
                action = self._get_action(timestep, agent)
                timestep = self._environment.step(action)

                rewards[agent] = timestep.reward

                # Have the agent observe the timestep and let the actor update itself.
                self._executor.agent_observe(agent, action, next_timestep=timestep)

                if self._should_update:
                    self._executor.update(agent)

            # Book-keeping.
            episode_steps += 1

            # Equivalent to: episode_return += timestep.reward
            # We capture the return value because if timestep.reward is a JAX
            # DeviceArray, episode_return will not be mutated in-place. (In all other
            # cases, the returned episode_return will be the same object as the
            # argument episode_return.)
            episode_return = tree.map_structure(
                operator.iadd, episode_return, np.array(list(rewards.values()))
            )

        # Record counts.
        counts = self._counter.increment(episodes=1, steps=episode_steps)

        # Collect the results and combine with counts.
        steps_per_second = episode_steps / (time.time() - start_time)
        result = {
            "episode_length": episode_steps,
            "mean_episode_return": episode_return,
            "steps_per_second": steps_per_second,
        }
        result.update(counts)

        return result

    def run(
        self, num_episodes: Optional[int] = None, num_steps: Optional[int] = None
    ) -> None:
        """Perform the run loop.
        Run the environment loop either for `num_episodes` episodes or for at
        least `num_steps` steps (the last episode is always run until completion,
        so the total number of steps may be slightly more than `num_steps`).
        At least one of these two arguments has to be None.
        Upon termination of an episode a new episode will be started. If the number
        of episodes and the number of steps are not given then this will interact
        with the environment infinitely.
        Args:
            num_episodes: number of episodes to run the loop for.
            num_steps: minimal number of steps to run the loop for.
        Raises:
            ValueError: If both 'num_episodes' and 'num_steps' are not None.
        """

        if not (num_episodes is None or num_steps is None):
            raise ValueError('Either "num_episodes" or "num_steps" should be None.')

        def should_terminate(episode_count: int, step_count: int) -> bool:
            return (num_episodes is not None and episode_count >= num_episodes) or (
                num_steps is not None and step_count >= num_steps
            )

        episode_count, step_count = 0, 0
        while not should_terminate(episode_count, step_count):
            result = self.run_episode()
            episode_count += 1
            step_count += result["episode_length"]
            # Log the given results.
            self._logger.write(result)


def _generate_zeros_from_spec(spec: specs.Array) -> np.ndarray:
    return np.zeros(spec.shape, spec.dtype)


class PettingZooParallelEnvironmentLoop(acme.core.Worker):
    """A Petting Zoo RL environment loop.
    This takes `Environment` and list of `Actor` instances and coordinates their
    interaction. Agents are updated if `should_update=True`. This can be used as:
        loop = EnvironmentLoop(environment, actors)
        loop.run(num_episodes)
    A `Counter` instance can optionally be given in order to maintain counts
    between different Acme components. If not given a local Counter will be
    created to maintain counts between calls to the `run` method.
    A `Logger` instance can also be passed in order to control the output of the
    loop. If not given a platform-specific default logger will be used as defined
    by utils.loggers.make_default_logger. A string `label` can be passed to easily
    change the label associated with the default logger; this is ignored if a
    `Logger` instance is given.
    """

    def __init__(
        self,
        environment: PettingZooParallelEnvWrapper,
        executor: mava.core.Executor,
        counter: counting.Counter = None,
        logger: loggers.Logger = None,
        should_update: bool = True,
        label: str = "petting_zoo_environment_loop",
    ):
        # Internalize agent and environment.
        self._environment = environment
        self._executor = executor
        self._counter = counter or counting.Counter()
        self._logger = logger or loggers.make_default_logger(label)
        self._should_update = should_update

    def _get_actions(self, timestep: dm_env.TimeStep) -> Any:
        return self._executor.select_actions(timestep.observation)

    def run_episode(self) -> loggers.LoggingData:
        """Run one episode.
        Each episode is a loop which interacts first with the environment to get an
        observation and then give that observation to the agent in order to retrieve
        an action.
        Returns:
            An instance of `loggers.LoggingData`.
        """
        # Reset any counts and start the environment.
        start_time = time.time()
        episode_steps = 0

        timestep = self._environment.reset()

        # Make the first observation.
        self._executor.observe_first(timestep)

        n_agents = self._environment.num_agents
        rewards = {
            agent: _generate_zeros_from_spec(spec)
            for agent, spec in self._environment.reward_spec().items()
        }

        # For evaluation, this keeps track of the total undiscounted reward
        # for each agent accumulated during the episode.
        multiagent_reward_spec = specs.Array((n_agents,), np.float32)
        episode_return = tree.map_structure(
            _generate_zeros_from_spec, multiagent_reward_spec
        )

        # Run an episode.
        while not timestep.last():

            # Generate an action from the agent's policy and step the environment.
            actions = self._get_actions(timestep)
            timestep = self._environment.step(actions)

            # Have the agent observe the timestep and let the actor update itself.
            self._executor.observe(actions, next_timestep=timestep)

            if self._should_update:
                self._executor.update()

            # Book-keeping.
            episode_steps += 1

            # Equivalent to: episode_return += timestep.reward
            # We capture the return value because if timestep.reward is a JAX
            # DeviceArray, episode_return will not be mutated in-place. (In all other
            # cases, the returned episode_return will be the same object as the
            # argument episode_return.)
            episode_return = tree.map_structure(
                operator.iadd, episode_return, np.array(list(rewards.values()))
            )

        # Record counts.
        counts = self._counter.increment(episodes=1, steps=episode_steps)

        # Collect the results and combine with counts.
        steps_per_second = episode_steps / (time.time() - start_time)
        result = {
            "episode_length": episode_steps,
            "mean_episode_return": episode_return,
            "steps_per_second": steps_per_second,
        }
        result.update(counts)

        return result

    def run(
        self, num_episodes: Optional[int] = None, num_steps: Optional[int] = None
    ) -> None:
        """Perform the run loop.
        Run the environment loop either for `num_episodes` episodes or for at
        least `num_steps` steps (the last episode is always run until completion,
        so the total number of steps may be slightly more than `num_steps`).
        At least one of these two arguments has to be None.
        Upon termination of an episode a new episode will be started. If the number
        of episodes and the number of steps are not given then this will interact
        with the environment infinitely.
        Args:
            num_episodes: number of episodes to run the loop for.
            num_steps: minimal number of steps to run the loop for.
        Raises:
            ValueError: If both 'num_episodes' and 'num_steps' are not None.
        """

        if not (num_episodes is None or num_steps is None):
            raise ValueError('Either "num_episodes" or "num_steps" should be None.')

        def should_terminate(episode_count: int, step_count: int) -> bool:
            return (num_episodes is not None and episode_count >= num_episodes) or (
                num_steps is not None and step_count >= num_steps
            )

        episode_count, step_count = 0, 0
        while not should_terminate(episode_count, step_count):
            result = self.run_episode()
            episode_count += 1
            step_count += result["episode_length"]
            # Log the given results.
            self._logger.write(result)


def _generate_zeros_from_spec(spec: specs.Array) -> np.ndarray:
    return np.zeros(spec.shape, spec.dtype)