from collections import deque, defaultdict

import numpy as np
from mpi4py import MPI
# from utils import add_noise
from recorder import Recorder


class Rollout(object):
    def __init__(self, ob_space, ac_space, nenvs, nsteps_per_seg, nsegs_per_env, nlumps, envs, policy,
                 int_rew_coeff, ext_rew_coeff, record_rollouts, dynamic_bottleneck):  #, noisy_box, noisy_p):
        # int_rew_coeff=1.0, ext_rew_coeff=0.0, record_rollouts=True 
        self.nenvs = nenvs                                          # 128
        self.nsteps_per_seg = nsteps_per_seg                        # 128
        self.nsegs_per_env = nsegs_per_env                          # 1
        self.nsteps = self.nsteps_per_seg * self.nsegs_per_env      # 128
        self.ob_space = ob_space                                    # Box (84,84,4)
        self.ac_space = ac_space                                    # Discrete(4)
        self.nlumps = nlumps                                        # 1
        self.lump_stride = nenvs // self.nlumps                     # 128
        self.envs = envs                
        self.policy = policy
        self.dynamic_bottleneck = dynamic_bottleneck                # Dynamic Bottleneck

        self.reward_fun = lambda ext_rew, int_rew: ext_rew_coeff * np.clip(ext_rew, -1., 1.) + int_rew_coeff * int_rew

        self.buf_vpreds = np.empty((nenvs, self.nsteps), np.float32)
        self.buf_nlps = np.empty((nenvs, self.nsteps), np.float32)
        self.buf_rews = np.empty((nenvs, self.nsteps), np.float32)
        self.buf_ext_rews = np.empty((nenvs, self.nsteps), np.float32)
        self.buf_acs = np.empty((nenvs, self.nsteps, *self.ac_space.shape), self.ac_space.dtype)
        self.buf_obs = np.empty((nenvs, self.nsteps, *self.ob_space.shape), self.ob_space.dtype)
        self.buf_obs_last = np.empty((nenvs, self.nsegs_per_env, *self.ob_space.shape), np.float32)

        self.buf_news = np.zeros((nenvs, self.nsteps), np.float32)
        self.buf_new_last = self.buf_news[:, 0, ...].copy()
        self.buf_vpred_last = self.buf_vpreds[:, 0, ...].copy()

        self.env_results = [None] * self.nlumps
        self.int_rew = np.zeros((nenvs,), np.float32)

        self.recorder = Recorder(nenvs=self.nenvs, nlumps=self.nlumps) if record_rollouts else None
        self.statlists = defaultdict(lambda: deque([], maxlen=100))
        self.stats = defaultdict(float)
        self.best_ext_ret = None
        self.all_visited_rooms = []
        self.all_scores = []

        self.step_count = 0

        # add bai. Noise box in observation
        # self.noisy_box = noisy_box
        # self.noisy_p = noisy_p

    def collect_rollout(self):
        self.ep_infos_new = []
        for t in range(self.nsteps):
            self.rollout_step()
        self.calculate_reward()
        self.update_info()

    def calculate_reward(self):               # Reward comes from Dynamic Bottleneck
        db_rew = self.dynamic_bottleneck.calculate_db_reward(
                    ob=self.buf_obs, last_ob=self.buf_obs_last, acs=self.buf_acs)
        self.buf_rews[:] = self.reward_fun(int_rew=db_rew, ext_rew=self.buf_ext_rews)

    def rollout_step(self):
        t = self.step_count % self.nsteps
        s = t % self.nsteps_per_seg
        for l in range(self.nlumps):         # nclumps=1
            obs, prevrews, news, infos = self.env_get(l)
            # if t > 0:
            #     prev_feat = self.prev_feat[l]
            #     prev_acs = self.prev_acs[l]
            for info in infos:
                epinfo = info.get('episode', {})
                mzepinfo = info.get('mz_episode', {})
                retroepinfo = info.get('retro_episode', {})
                epinfo.update(mzepinfo)
                epinfo.update(retroepinfo)
                if epinfo:
                    if "n_states_visited" in info:
                        epinfo["n_states_visited"] = info["n_states_visited"]
                        epinfo["states_visited"] = info["states_visited"]
                    self.ep_infos_new.append((self.step_count, epinfo))

            # slice(0,128) lump_stride=128
            sli = slice(l * self.lump_stride, (l + 1) * self.lump_stride)

            acs, vpreds, nlps = self.policy.get_ac_value_nlp(obs)
            self.env_step(l, acs)

            # self.prev_feat[l] = dyn_feat
            # self.prev_acs[l] = acs
            self.buf_obs[sli, t] = obs        # obs.shape=(128,84,84,4)
            self.buf_news[sli, t] = news      # shape=(128,)  True/False
            self.buf_vpreds[sli, t] = vpreds  # shape=(128,)
            self.buf_nlps[sli, t] = nlps      # -log pi(a|s), shape=(128,)
            self.buf_acs[sli, t] = acs        # shape=(128,)
            if t > 0:
                self.buf_ext_rews[sli, t - 1] = prevrews   # prevrews.shape=(128,) 
            if self.recorder is not None:
                self.recorder.record(timestep=self.step_count, lump=l, acs=acs, infos=infos, int_rew=self.int_rew[sli],
                                     ext_rew=prevrews, news=news)
        
        self.step_count += 1
        if s == self.nsteps_per_seg - 1:     # nsteps_per_seg=128
            for l in range(self.nlumps):     # nclumps=1
                sli = slice(l * self.lump_stride, (l + 1) * self.lump_stride)
                nextobs, ext_rews, nextnews, _ = self.env_get(l)
                self.buf_obs_last[sli, t // self.nsteps_per_seg] = nextobs
                if t == self.nsteps - 1:        # t=127
                    self.buf_new_last[sli] = nextnews
                    self.buf_ext_rews[sli, t] = ext_rews    # 
                    _, self.buf_vpred_last[sli], _ = self.policy.get_ac_value_nlp(nextobs)  # 

    def update_info(self):
        all_ep_infos = MPI.COMM_WORLD.allgather(self.ep_infos_new)
        all_ep_infos = sorted(sum(all_ep_infos, []), key=lambda x: x[0])
        if all_ep_infos:
            all_ep_infos = [i_[1] for i_ in all_ep_infos]  # remove the step_count
            keys_ = all_ep_infos[0].keys()
            all_ep_infos = {k: [i[k] for i in all_ep_infos] for k in keys_}
            # all_ep_infos: {'r': [0.0, 0.0, 0.0], 'l': [124, 125, 127], 't': [6.60745, 12.034875, 10.772788]}

            self.statlists['eprew'].extend(all_ep_infos['r'])
            self.stats['eprew_recent'] = np.mean(all_ep_infos['r'])
            self.statlists['eplen'].extend(all_ep_infos['l'])
            self.stats['epcount'] += len(all_ep_infos['l'])
            self.stats['tcount'] += sum(all_ep_infos['l'])
            if 'visited_rooms' in keys_:
                # Montezuma specific logging.
                self.stats['visited_rooms'] = sorted(list(set.union(*all_ep_infos['visited_rooms'])))
                self.stats['pos_count'] = np.mean(all_ep_infos['pos_count'])
                self.all_visited_rooms.extend(self.stats['visited_rooms'])
                self.all_scores.extend(all_ep_infos["r"])
                self.all_scores = sorted(list(set(self.all_scores)))
                self.all_visited_rooms = sorted(list(set(self.all_visited_rooms)))
                if MPI.COMM_WORLD.Get_rank() == 0:
                    print("All visited rooms")
                    print(self.all_visited_rooms)
                    print("All scores")
                    print(self.all_scores)
            if 'levels' in keys_:
                # Retro logging
                temp = sorted(list(set.union(*all_ep_infos['levels'])))
                self.all_visited_rooms.extend(temp)
                self.all_visited_rooms = sorted(list(set(self.all_visited_rooms)))
                if MPI.COMM_WORLD.Get_rank() == 0:
                    print("All visited levels")
                    print(self.all_visited_rooms)

            current_max = np.max(all_ep_infos['r'])
        else:
            current_max = None
        self.ep_infos_new = []

        # best_ext_ret
        if current_max is not None:
            if (self.best_ext_ret is None) or (current_max > self.best_ext_ret):
                self.best_ext_ret = current_max
        self.current_max = current_max

    def env_step(self, l, acs):
        self.envs[l].step_async(acs)
        self.env_results[l] = None

    def env_get(self, l):
        if self.step_count == 0:
            ob = self.envs[l].reset()
            out = self.env_results[l] = (ob, None, np.ones(self.lump_stride, bool), {})
        else:
            if self.env_results[l] is None:
                out = self.env_results[l] = self.envs[l].step_wait()
                # if self.noisy_box:
                #     obs = add_noise(out[0], noisy_p=self.noisy_p)
                #     out = (obs, *out[1:])
                #     self.env_results[l] = out
            else:
                out = self.env_results[l]
        return out