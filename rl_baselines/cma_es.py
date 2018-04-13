import time
import pickle

import cma
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from baselines.common.vec_env.subproc_vec_env import SubprocVecEnv
from baselines.common.vec_env.vec_frame_stack import VecFrameStack

import environments.kuka_button_gym_env as kuka_env
from environments.utils import makeEnv
from rl_baselines.utils import CustomVecNormalize
from srl_priors.utils import printYellow

class Policy(object):
    """
    The policy object for genetic algorithms
    :param continuous_actions: (bool)
    """
    def __init__(self, continuous_actions):
        self.continuous_actions = continuous_actions

    def getAction(self, obs):
        raise NotImplementedError

    def getParamSpace(self):
        raise NotImplementedError

    def setParam(self):
        raise NotImplementedError


class PytorchPolicy(Policy):
    """
    The policy object for genetic algorithms, using Pytorch networks
    :param model: (Pytorch nn.Module)
    :param continuous_actions: (bool)
    """
    def __init__(self, model, continuous_actions):
        super(PytorchPolicy, self).__init__(continuous_actions)
        self.model = model
        self.param_len = np.sum([np.prod(x.shape) for x in self.model.parameters()])
        self.continuous_actions = continuous_actions

    def getAction(self, obs):
        """
        Returns an action for the given observation
        :param obs: ([float])
        :return: the action
        """
        if self.continuous_actions:
            return self.model(self.make_var(obs.reshape(-1))).data.numpy()
        else:
            return np.argmax(F.softmax(self.model(self.make_var(obs.reshape(-1))), dim=-1).data)

    @staticmethod
    def make_var(arr):
        """
        Returns a pytorch Variable object from a numpy array
        :param arr: ([float])
        :return: (Variable)
        """
        return Variable(torch.from_numpy(arr))

    def getParamSpace(self):
        """
        Returns the size of the parameters for the pytorch network
        :return: (int)
        """
        return self.param_len

    def setParam(self, param):
        """
        Set the network bias and weights
        :param param: ([float])
        """
        nn.utils.vector_to_parameters(self.make_var(param).contiguous(), self.model.parameters())


class CNNPolicyPytorch(nn.Module):
    """
    A simple CNN policy using pytorch
    :param out_dim: (int)
    """
    #TODO remove sequencial, as it breaks .shape function
    def __init__(self, out_dim):
        super(CNNPolicyPytorch, self).__init__()
        self.layer1 = nn.Sequential(
            nn.Conv2d(1, 16, kernel_size=5, padding=2),
            nn.BatchNorm2d(16),
            nn.ReLU(),
            nn.MaxPool2d(2))
        self.layer2 = nn.Sequential(
            nn.Conv2d(16, 32, kernel_size=5, padding=2),
            nn.BatchNorm2d(32),
            nn.ReLU(),
            nn.MaxPool2d(2))
        self.fc = nn.Linear(7*7*32, out_dim)
        
    def forward(self, x):
        x = self.layer1(x)
        x = self.layer2(x)
        print(x.dim())
        x = x.view(x.size(0), -1)
        x = self.fc(x)
        return x

class MLPPolicyPytorch(nn.Module):
    """
    A simple MLP policy using pytorch
    :param in_dim: (int)
    :param hidden_dims: ([int])
    :param out_dim: (int)
    """
    def __init__(self, in_dim, hidden_dims, out_dim):
        super(MLPPolicyPytorch, self).__init__()
        self.fc_hidden_name = []

        self.fc_in = nn.Linear(int(in_dim), int(hidden_dims[0]))
        for i in range(len(hidden_dims)-1):
           self.add_module("fc_"+str(i), nn.Linear(int(hidden_dims[i]), int(hidden_dims[i+1])))
           self.fc_hidden_name.append("fc_"+str(i))
        self.fc_out = nn.Linear(int(hidden_dims[-1]), int(out_dim))

    def forward(self, x):
        x = F.relu(self.fc_in(x))
        for name in self.fc_hidden_name:
            x = F.relu(getattr(self, name)(x))
        x = self.fc_out(x)
        return x


class CMAES:
    """
    An implementation of the CMA-ES algorithme
    :param n_population: (int)
    :param policy: (Policy Object)
    :param mu: (float) default=0
    :param sigma: (float) default=1
    :param continuous_actions: (bool) default=False
    """
    def __init__(self, n_population, policy, mu=0, sigma=1, continuous_actions=False):
        self.policy = policy
        self.n_population = n_population
        self.init_mu = mu
        self.init_sigma = sigma
        self.continuous_actions = continuous_actions
        self.es = cma.CMAEvolutionStrategy(self.policy.getParamSpace() * [mu], sigma, {'popsize': n_population})
        self.best_model = self.es.result.xbest

    def getAction(self, obs):
        """
        Returns an action for the given observation
        :param obs: ([float])
        :return: the action
        """
        return self.policy.getAction(obs)

    def save(self, save_path):
        """
        :param save_path: (str)
        """
        with open(save_path, "wb") as f:
            pickle.dump(self.__dict__, f)

    def train(self, env, callback, num_updates=1e6):
        """
        :param env: (gym enviroment)
        :param callback: (function)
        :param num_updates: (int) the number of updates to do (default=100000)
        """
        start_time = time.time()
        step = 0

        while(step < num_updates):
            obs = env.reset()
            r = np.zeros((self.n_population,))
            population = self.es.ask()
            done = np.full((self.n_population,), False)
            while (not done.all()):
                actions = []
                for k in range(self.n_population):
                    if not done[k]:
                        current_obs = obs[k].reshape(-1)
                        self.policy.setParam(population[k])
                        action = self.policy.getAction(obs[k])
                        actions.append(action)
                    else:
                        actions.append(None) # do nothing, as we are done

                obs, reward, new_done, info = env.step(actions)
                step += self.n_population

                done = np.bitwise_or(done,new_done)

                # cumulate the reward for every enviroment that is not finished
                r[~done] += reward[~done]

                if callback is not None:
                    callback(locals(), globals())
                    
            print("{} steps - {:.2f} FPS".format(step, step / (time.time() - start_time)))
            self.es.tell(population, -r)
            self.best_model = self.es.result.xbest

def load(save_path):
    """
    :param save_path: (str)
    :return: (CMAES Object)
    """
    with open(save_path, "rb") as f:
        class_dict = pickle.load(f)
    model = CMAES(class_dict["n_population"], class_dict["policy"], class_dict["init_mu"], class_dict["init_sigma"])
    model.__dict__ = class_dict
    return model

def customArguments(parser):
    """
    :param parser: (ArgumentParser Object)
    :return: (ArgumentParser Object)
    """
    parser.add_argument('--num-population', help='Number of population', type=int, default=20)
    parser.add_argument('--mu', type=float, default=0,
                        help='inital location for gaussian sampling of network parameters')
    parser.add_argument('--sigma', type=float, default=0.2,
                        help='inital scale for gaussian sampling of network parameters')
    return parser

def main(args, callback=None):
    """
    :param args: (argparse.Namespace Object)
    :param callback: (function)
    """

    envs = [makeEnv(args.env, args.seed, i, args.log_dir, allow_early_resets=True)
            for i in range(args.num_population)]
    envs = SubprocVecEnv(envs)
    envs = VecFrameStack(envs, args.num_stack)

    if args.continuous_actions:
        action_space = np.prod(envs.action_space.shape)
    else:
        action_space = envs.action_space.n

    if args.srl_model != "":
        printYellow("Using MLP policy because working on state representation")
        args.policy = "mlp"
        envs = CustomVecNormalize(envs, norm_obs=True, norm_rewards=False)
        net = MLPPolicyPytorch(np.prod(envs.observation_space.shape), [100], action_space)
    else:
        net = CNNPolicyPytorch(action_space)

    policy = PytorchPolicy(net, args.continuous_actions)

    model = CMAES(
        args.num_population, 
        policy, 
        mu=args.mu,
        sigma=args.sigma,
        continuous_actions=args.continuous_actions
    )

    model.train(envs, callback, num_updates=(int(args.num_timesteps) // args.num_population*2))
