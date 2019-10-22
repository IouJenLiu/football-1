# coding=utf-8
# Copyright 2019 Google LLC
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


"""Remote football environment."""
import pickle
import time
from absl import logging
from gfootball.env import football_action_set
from gfootball.eval_server import config
from gfootball.eval_server import utils
from gfootball.eval_server.proto import game_server_pb2
from gfootball.eval_server.proto import game_server_pb2_grpc
from gfootball.eval_server.proto import master_pb2
from gfootball.eval_server.proto import master_pb2_grpc
import grpc
import gym


CONNECTION_TRIALS = 20


# This config is needed in order to run various env wrappers.
class FakeConfig(object):
  """An immitation of Config with the set of fields necessary to run wrappers.
  """

  def __init__(self):
    self._values = {
        'enable_sides_swap': True,
    }

  def number_of_players_agent_controls(self):
    return 1

  def __getitem__(self, key):
    return self._values[key]


class RemoteFootballEnv(gym.Env):

  def __init__(self, username, token, model_name='', track='default'):
    self._config = FakeConfig()
    self._num_actions = len(football_action_set.action_set_dict['default'])
    self._track = track

    self._username = username
    self._token = token
    self._model_name = model_name

    self._game_id = None
    self._channel = None

    master_address = utils.get_master_address(self._track)
    self._master_channel = utils.get_grpc_channel(master_address)
    logging.info('Connecting to %s', master_address)
    grpc.channel_ready_future(self._master_channel).result()

  @property
  def action_space(self):
    return gym.spaces.Discrete(self._num_actions)

  def step(self, action):
    if self._game_id is None:
      raise RuntimeError('Environment should be reset!')
    if action < 0 or action >= self._num_actions:
      raise RuntimeError('Bad action number!')
    request = game_server_pb2.StepRequest(
        game_version=config.game_version, game_id=self._game_id,
        username=self._username, token=self._token, action=action,
        model_name=self._model_name)
    return self._get_env_result(request, 'Step')

  def reset(self):
    if self._channel is not None:
      # Client surrenders in the current game and starts next one.

      self._channel.close()
      self._channel = None

    # Get game server address and side id from master.
    stub = master_pb2_grpc.MasterStub(self._master_channel)
    start_game_request = master_pb2.StartGameRequest(
        game_version=config.game_version, username=self._username,
        token=self._token, model_name=self._model_name)
    response = stub.StartGame(start_game_request)
    self._game_id = response.game_id
    self._channel = utils.get_grpc_channel(response.game_server_address)
    grpc.channel_ready_future(self._channel).result()
    get_env_result_request = game_server_pb2.GetEnvResultRequest(
        game_version=config.game_version, game_id=self._game_id,
        username=self._username, token=self._token, model_name=self._model_name)
    return self._get_env_result(get_env_result_request, 'GetEnvResult')[0]

  def _get_env_result(self, request, rpc_name):
    assert rpc_name in ['GetEnvResult', 'Step']

    response = None
    for _ in range(CONNECTION_TRIALS):
      time_to_sleep = 1
      try:
        stub = game_server_pb2_grpc.GameServerStub(self._channel)
        response = getattr(stub, rpc_name)(request)
        break
      except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.INVALID_ARGUMENT:
          raise e
        if e.code() == grpc.StatusCode.FAILED_PRECONDITION:
          raise e
        logging.warning('Exception during request: %s', e)
        time.sleep(time_to_sleep)
        if time_to_sleep < 1000:
          time_to_sleep *= 2
      except BaseException as e:
        logging.warning('Exception during request: %s', e)
        time.sleep(time_to_sleep)
        if time_to_sleep < 1000:
          time_to_sleep *= 2
    if response is None:
      raise RuntimeError('Connection problems!')

    env_result = pickle.loads(response.env_result)
    self._process_env_result(env_result)
    return env_result[0], env_result[1], env_result[2], env_result[3]

  def _process_env_result(self, env_result):
    done = env_result[2]
    if done:
      self._game_id = None
      self._channel.close()
      self._channel = None