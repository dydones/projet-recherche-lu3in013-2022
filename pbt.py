import copy
import imp  # used for multiprocessing
import multiprocessing
import random
import time
from turtle import forward
from typing import Dict, List, Union

import gym
import hydra
import matplotlib.pyplot as plt
import numpy as np
import omegaconf
import salina
import torch
import torch.nn as nn
import torch.nn.functional as F
from gym.wrappers import TimeLimit
from omegaconf import DictConfig, OmegaConf
from salina import (Agent, Workspace, get_arguments, get_class,
                    instantiate_class)
from salina.agents import Agents, NRemoteAgent, RemoteAgent, TemporalAgent
from salina.agents.asynchronous import AsynchronousAgent
from salina.agents.gyma import AutoResetGymAgent
from salina.logger import TFLogger

from utils import build_nn

import my_gym


class Logger():

  def __init__(self, cfg):
    self.logger = instantiate_class(cfg.logger)

  def add_log(self, log_string, loss, epoch):
    self.logger.add_scalar(log_string, loss.item(), epoch)

  # Log losses
  def log_losses(self, epoch, critic_loss, entropy_loss, a2c_loss):
    self.add_log("critic_loss", critic_loss, epoch)
    self.add_log("entropy_loss", entropy_loss, epoch)
    self.add_log("a2c_loss", a2c_loss, epoch)

class EnvAgent(AutoResetGymAgent):
    def __init__(self, cfg: OmegaConf):
        super().__init__(
            get_class(cfg.env),
            get_arguments(cfg.env),
            n_envs=cfg.algorithm.number_environments
        )
        env = instantiate_class(cfg.env)
        self.observation_space = env.observation_space
        self.action_space = env.action_space
        del(env)

    def get_observation_size(self):
        if isinstance(self.observation_space, gym.spaces.Box):
            return self.observation_space.shape[0]
        elif isinstance(self.observation_space, gym.spaces.Discrete):
            return self.observation_space.n
        else:
            ValueError("Incorrect space type")

    def get_action_size(self):
        if isinstance(self.action_space, gym.spaces.Box):
            return self.action_space.shape[0]
        elif isinstance(self.action_space, gym.spaces.Discrete):
            return self.action_space.n
        else:
            ValueError("Incorrect space type")

class A2CAgent(salina.TAgent):
    # TAgent != TemporalAgent, TAgent is only an extension of the Agent interface to say that this agent accepts the current timestep parameter in the forward method
    '''This agent implements an Advantage Actor-Critic agent (A2C).
    The hyperparameters of the agent are customizable.'''

    def __init__(self, parameters, observation_size, hidden_layer_sizes, action_size, stochastic=True, std_param=None, discount_factor=0.95):
        super().__init__()
        self.action_model = build_nn([observation_size] + hidden_layer_sizes + [action_size], output_activation=nn.Tanh, activation=nn.ReLU)
        self.critic_model = build_nn([observation_size] + hidden_layer_sizes + [1], activation=nn.ReLU, output_activation=nn.Identity)

        self.observation_size = observation_size
        self.hidden_layer_sizes = hidden_layer_sizes
        self.action_size = action_size
        self.stochastic = stochastic
        self.discount_factor = discount_factor
        self.params = omegaconf.DictConfig(content=parameters)
        if std_param is None:
            self.std_param = nn.parameter.Parameter(torch.randn(action_size,1)) # TODO: What is this? Should we copy it too?
        else:
            self.std_param = std_param.clone()
        self.softplus = torch.nn.Softplus()

    def forward(self, time, **kwargs):
        input = self.get(("env/env_obs", time))
        scores = self.action_model(input)
        dist = torch.distributions.Normal(scores, self.softplus(self.std_param))
        self.set(("entropy", time), dist.entropy())

        if self.stochastic:
            action = torch.tanh(dist.sample())
        else:
            action = torch.tanh(scores)

        logprobs = dist.log_prob(action).sum(axis=-1)

        self.set(("action", time), action)
        self.set(("action_logprobs", time), logprobs)

        critic = self.critic_model(input).squeeze(-1)
        self.set(("critic", time), critic)

    @staticmethod
    def _index_3d_tensor_with_2d_tensor(tensor_3d: torch.Tensor, tensor_2d: torch.Tensor):
        # TODO: What is the purpose of this function?
        x, y, z = tensor_3d.size()
        t = tensor_3d.reshape(x*y, z)
        tt = tensor_2d.reshape(x*y)
        v = t[torch.arrange(x*y), tt]
        v = v.reshape(x, y)
        return v
    
    def compute_a2c_loss(self, action_logprobs: torch.Tensor, td: float) -> float:
        a2c_loss = action_logprobs[:-1] * td.detach() # TODO: Is it OK to calculate it like this?
        return a2c_loss.mean()

    def get_hyperparameter(self, param_name):
        return self.params[param_name]

    def set_hyperparameter(self, param_name, value):
        self.params[param_name] = value

    def clone(self):
        new = A2CAgent(self.params, self.observation_size, self.hidden_layer_sizes, self.action_size, self.stochastic, std_param=self.std_param)
        return new

    def compute_critic_loss(self, reward, done, critic) -> Union[float, float]:
        # Compute temporal difference
        target = reward[1:] + self.discount_factor * critic[1:].detach() * (1 - done[1:].float())
        td = target - critic[:-1]

        # Compute critic loss
        td_error = td ** 2
        critic_loss = td_error.mean()
        return critic_loss, td
    
    def compute_loss(self, workspace: Workspace, timestep, logger) -> float:
        critic, done, action_logprobs, reward, action, entropy = workspace[
            "critic", "env/done", "action_logprobs", "env/reward", "action", "entropy"
        ] # TODO: Can we use action_logprobs instead of action_probs?

        critic_loss, td = self.compute_critic_loss(reward, done, critic)

        entropy_loss = entropy.mean() # TODO: What's the difference between entropy and entropy_loss?

        a2c_loss = self.compute_a2c_loss(action_logprobs, td)

        logger.log_losses(timestep, critic_loss, entropy_loss, a2c_loss)

        loss = (
            - self.params.entropy_coef * entropy_loss
            + self.params.critic_coef * critic_loss
            - self.params.a2c_coef * a2c_loss
        )

        return loss


class A2CParameterizedAgent(salina.TAgent):
    # TAgent != TemporalAgent, TAgent is only an extension of the Agent interface to say that this agent accepts the current timestep parameter in the forward method
    '''This agent implements an Advantage Actor-Critic agent (A2C).
    The hyperparameters of the agent are customizable.'''


    def __init__(self, parameters, observation_size, hidden_layer_sizes, action_size, mutation_rate, stochastic=True, discount_factor=0.95):
        super().__init__()

        self.mutation_rate = mutation_rate
        
        simplified_parameters = omegaconf.DictConfig(content={}) # The A2C Agent only sees a dictionary of (param_name, param_value) entries
        self.params_metadata = omegaconf.DictConfig(content={}) # This wrapper will store the metadata for the parameters of the A2C agent, so it knows how to change them when needed
        for param in parameters:
            generated_val = torch.distributions.Uniform(parameters[param].min, parameters[param].max).sample().item() # We get a 0D tensor, so we do .item(), to get the value
            self.params_metadata[param] = {'min': parameters[param].min, 'max': parameters[param].max}
            simplified_parameters[param] = generated_val

        self.a2c_agent = A2CAgent(parameters=simplified_parameters, observation_size=observation_size, hidden_layer_sizes=hidden_layer_sizes, action_size=action_size, stochastic=stochastic, discount_factor=discount_factor)
        
    def mutate_hyperparameters(self):
        '''This function mutates, randomly, all the hyperparameters of this agent, according to the mutation rate'''
        for param in self.params_metadata:
            # We'll generate a completely random value, and mutate the original one according to the mutation rate.
            old_val = self.a2c_agent.get_hyperparameter(param)
            generated_val = torch.distributions.Uniform(self.params_metadata[param].min, self.params_metadata[param].max).sample().item() # We get a 0D tensor, so we do .item(), to get the value
            discriminator = torch.distributions.Uniform(0, 1).sample().item()
            if discriminator > 0.5:
                mutated_val = 0.8 * old_val
            else:
                mutated_val = 1.2 * old_val
            self.a2c_agent.set_hyperparameter(param, mutated_val)
    
    def get_agent(self):
        return self.a2c_agent

    def compute_critic_loss(self, reward, done, critic):
        return self.a2c_agent.compute_critic_loss(reward, done, critic)
    
    def compute_a2c_loss(self, action_probs, action, td):
        return self.a2c_agent.compute_a2c_loss(action_probs, action)

    def copy(self, other):
        self.a2c_agent = other.get_agent().clone()

    def get_cumulated_reward(self):
        return self.a2c_agent.get_cumulated_reward()

    def __call__(self, workspace, t, **kwargs):
        return self.a2c_agent(time=t, workspace=workspace, kwargs=kwargs)
    
    def compute_loss(self, **kwargs):
        return self.a2c_agent.compute_loss(**kwargs)
    


def make_env(**kwargs) -> gym.Env:
    # We set a timestep limit on the environment of max_episode_steps
    # We can also add a seed to the environment here
    return TimeLimit(gym.make(kwargs['env_name']), kwargs['max_episode_steps'])

def visualize_performances(workspaces: List[Workspace]):
    # We visualize the performances of the agents
    fig, ax = plt.subplots()
    for workspace in workspaces:
        visualize_performance(ax, workspace)
    ax.set(xlabel='timestep', ylabel='creward',
       title='Evolution of crewards')
    ax.grid()

    fig.savefig("test.png")
    plt.show()

def visualize_performance(axes, workspace: Workspace):
    axes.plot(workspace['env/cumulated_reward'].mean(dim=1))


def create_population(cfg):
    # We'll run this number of agents in parallel
    n_cpus = multiprocessing.cpu_count()

    # Create the required number of agents
    population = []
    workspaces = {} # A dictionary of the workspace of each agent

    environment = EnvAgent(cfg)
    # observation_size: the number of features of the observation (in Pendulum-v1, it is 3)
    observation_size = environment.get_observation_size()
    # hidden_layer_size: the number of neurons in the hidden layer
    hidden_layer_sizes = list(cfg.algorithm.neural_network.hidden_layer_sizes)
    # action_size: the number of parameters to output as actions (in Pendulum-v1, it is 1)
    action_size = environment.get_action_size()

    for i in range(cfg.algorithm.population_size):

        workspace = Workspace()
        # raise Error()

        # The agent that we'll train will use the A2C algorithm
        a2c_agent = A2CParameterizedAgent(cfg.algorithm.hyperparameters, observation_size, hidden_layer_sizes, action_size, cfg.algorithm.mutation_rate, discount_factor=cfg.algorithm.discount_factor)
        # To generate the observations, we need a gym agent, which will provide the values in 'env/env_obs'
        environment_agent = EnvAgent(cfg)
        temporal_agent = TemporalAgent(Agents(environment_agent, a2c_agent))
        temporal_agent.seed(cfg.algorithm.stochasticity_seed)
        population.append(temporal_agent)
        
        workspaces[temporal_agent] = workspace
        
        async_agent = AsynchronousAgent(temporal_agent) # TODO: Implement async operation

    return population, workspaces

def get_cumulated_reward(cumulated_rewards_dict: Dict[Agent, torch.Tensor], agent: Agent):
    cumulated_rewards_of_agent = cumulated_rewards_dict[agent]
    return torch.mean(cumulated_rewards_of_agent)
    
def sort_performance(agents_list: List[TemporalAgent], five_last_rewards: Dict[TemporalAgent, torch.Tensor]):
    agents_list.sort(key=lambda agent: get_cumulated_reward(five_last_rewards,agent), reverse=True)

def select_pbt(portion, agents_list):
    random_index = torch.distributions.Uniform(0, portion * len(agents_list)).sample().item()
    return agents_list[int(random_index)]

def _index_3d_2d(tensor_3d, tensor_2d):
    """This function is used to index a 3d tensor using a 2d tensor"""
    x, y, z = tensor_3d.size()
    t = tensor_3d.reshape(x * y, z)
    tt = tensor_2d.reshape(x * y)
    v = t[torch.arange(x * y), tt]
    v = v.reshape(x, y)
    return v

class CrewardsLogger:
    def __init__(self) -> None:
        self.crewards: torch.Tensor = torch.tensor([]) # An empty tensor of the form ([ [],[] ], [ [],[] ], ...)

    def log_epoch(self, timestep, crewards):
        plt.close() # Clear the last figure
        mean_of_crewards = crewards.mean()
        tensor_to_cat = torch.tensor([timestep, mean_of_crewards]).unsqueeze(-1) # Gives us a tensor like [[timestep], [mean_of_crewards]] 
        self.crewards = torch.cat((self.crewards, tensor_to_cat), dim=1)
        self.fig, self.ax = plt.subplots()
        self.ax.set_ylim([0, self.crewards[1].max(0)[0].item()])
        plt.scatter(self.crewards[0], self.crewards[1])
        plt.plot(self.crewards[0], self.crewards[1])
        self.ax.set(xlabel='timestep', ylabel='creward', title='Evolution of crewards')
        self.ax.grid()
        plt.savefig('/home/acmc/repos/projet-recherche-lu3in013-2022/fig.png')
    
    def show(self):
        plt.show()

def plot_hyperparams(agents_list: List[A2CAgent]):
    plt.close()
    hyperparams = {} # Will contain the hyperparameters of each agent, in the form {'a2c_coef': [0.8, 0.7, ...], 'gamma': [0.2, 0.1, ...], ...}
    for hyperparam in agents_list[0].params.keys():
        hyperparams[hyperparam] = torch.tensor([]) # Put an empty tensor in the dictionary for each hyperparam, to be filled later
    for hyperparam in hyperparams.keys():
        for agent in agents_list:
            hyperparams[hyperparam] = torch.cat((hyperparams[hyperparam], torch.tensor([agent.params[hyperparam]])))
    plt.boxplot(hyperparams.values())
    plt.xticks(range(1, len(hyperparams) + 1), hyperparams.keys())
    plt.savefig('/home/acmc/repos/projet-recherche-lu3in013-2022/hyperparams.png')

def train(cfg, population: List[TemporalAgent], workspaces: Dict[Agent, Workspace], logger: TFLogger):
    epoch_logger = CrewardsLogger()

    total_timesteps = 0

    five_last_rewards = {}
    for agent in population:
        five_last_rewards[agent] = torch.tensor([])

    optimizers = {}
    for agent in population:
        # Configure the optimizer over the a2c agent
        optimizer_args = get_arguments(cfg.optimizer)
        optimizer = get_class(cfg.optimizer)(
            agent.parameters(), **optimizer_args
        )
        optimizers[agent] = optimizer

    for epoch in range(cfg.algorithm.max_epochs):
        print("Epoch: {}".format(epoch))
        for agent in population:
            consumed_budget = 0
            while consumed_budget < cfg.algorithm.train_budget:
                workspace = workspaces[agent]
                if consumed_budget > 0:
                    workspace.zero_grad()
                    workspace.copy_n_last_steps(1)
                    agent(t=1, workspace=workspace, n_steps=cfg.algorithm.num_timesteps - 1)
                else:
                    agent(t=0, workspace=workspace, n_steps=cfg.algorithm.num_timesteps)

                steps = (workspace.time_size() - 1) * workspace.batch_size()
                consumed_budget += steps
                
                done = workspace["env/done"]

                loss = agent.agent[1].compute_loss(workspace=workspace, timestep=total_timesteps + consumed_budget, logger=logger)

                optimizer = optimizers[agent]
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                creward = workspace["env/cumulated_reward"]
                creward = creward[done]

                five_last_rewards[agent] = torch.cat((five_last_rewards[agent], creward))[-5:]

                if creward.size()[0] > 0:
                    logger.add_log("reward", creward.mean(), total_timesteps + consumed_budget)
            

                # stop = [workspace['env/done'][-1].all() for workspace in workspaces.values()]
                # if all(stop):
                #     break
        
        total_timesteps += consumed_budget

        # They have all finished executing
        print('Finished epoch {}'.format(epoch))

        # We sort the agents by their performance
        sort_performance(population, five_last_rewards)

        cumulated_rewards = {agent: get_cumulated_reward(five_last_rewards, agent).item() for agent in population}
        
        print('Cumulated rewards at epoch {}: {}'.format(epoch, cumulated_rewards.values()))

        epoch_logger.log_epoch(total_timesteps, torch.tensor(list(cumulated_rewards.values())))
        plot_hyperparams([a.agent[1].a2c_agent for a in population])

        for bad_agent in population[-1 * int(cfg.algorithm.pbt_portion * len(population)) : ]:
            # Select randomly one agent to replace the current one
            agent_to_copy = select_pbt(cfg.algorithm.pbt_portion, population)
            print('Copying agent with creward = {} into agent with creward {}'.format(cumulated_rewards[agent_to_copy], cumulated_rewards[bad_agent]))
            bad_agent.agent[1].copy(agent_to_copy.agent[1])
            bad_agent.agent[1].mutate_hyperparameters()
        
        for _, workspace in workspaces.items():
            # workspace.clear() # TODO: Is this the right way to do it?
            workspace.zero_grad()
            pass
    
    epoch_logger.show()


@hydra.main(config_path=".", config_name="pbt.yaml")
def main(cfg):
    # First, build the  logger
    logger = Logger(cfg)
    population, workspaces = create_population(cfg)
    train(cfg, population, workspaces, logger=logger)

if __name__ == '__main__':
    main()
