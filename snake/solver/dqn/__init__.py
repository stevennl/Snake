#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=C0111,C0103,E1101

"""DQN Solver package."""

import json
import os

import numpy as np
import tensorflow as tf

from snake.base import Direc, PointType
from snake.solver.base import BaseSolver
from snake.solver.dqn.memory import Memory
from snake.solver.dqn.logger import log

_DIR_LOG = "logs"


class DQNSolver(BaseSolver):

    PATH_VAR = os.path.join(_DIR_LOG, "solver-var-%d.json")
    PATH_NET = os.path.join(_DIR_LOG, "solver-net-%d")

    def __init__(self, snake):
        super().__init__(snake)

        self.__MAX_LEARN_STEP = 1000000   # Expected maximum learning steps
        self.__RESTORE_STEP = 0           # Which learning step to restore (0 means not restore)

        # Rewards
        self.__RWD_EMPTY = -0.005
        self.__RWD_DEAD = -0.5
        self.__RWD_FOOD = 1.0

        # Memory
        self.__MEM_SIZE = 100000
        self.__MEM_BATCH = 32

        # Epsilon-greedy
        self.__EPSILON_MAX = 1.0
        self.__EPSILON_MIN = 0.1
        self.__EPSILON_DEC = (self.__EPSILON_MAX - self.__EPSILON_MIN) / self.__MAX_LEARN_STEP

        self.__LR = 1e-6             # Learning rate
        self.__MOMENTUM = 0.95       # SGD momentum
        self.__GAMMA = 0.99          # Reward discount

        self.__PRI_EPSILON = 0.001   # Small positive value to avoid zero priority
        self.__ALPHA = 0.6           # How much prioritization to use
        self.__BETA_MIN = 0.4        # How much to compensate for the non-uniform probabilities
        self.__BETA_INC = (1.0 - self.__BETA_MIN) / self.__MAX_LEARN_STEP

        # Frequency
        self.__FREQ_LEARN = 4        # Number of new transitions
        self.__FREQ_REPLACE = 10000  # Learning steps
        self.__FREQ_LOG = 500        # Learning steps
        self.__FREQ_SAVE = 20000     # Learning steps

        self.__NUM_AVG_RWD = 100     # How many latest reward history to compute average

        self.__SNAKE_ACTIONS = [Direc.LEFT, Direc.UP, Direc.RIGHT, Direc.DOWN]
        self.__NUM_ACTIONS = len(self.__SNAKE_ACTIONS)
        self.__NUM_FEATURES = snake.map.capacity

        self.__mem = Memory(mem_size=self.__MEM_SIZE,
                            alpha=self.__ALPHA,
                            epsilon=self.__PRI_EPSILON)
        self.__mem_cnt = 0

        self.__learn_step = 1
        self.__epsilon = self.__EPSILON_MAX
        self.__beta = self.__BETA_MIN

        self.__tot_reward = 0
        self.__history_loss = []
        self.__history_reward = []
        self.__history_avg_reward = []

        eval_params, target_params = self.__build_net()
        self.__net_saver = tf.train.Saver(var_list=eval_params + target_params,
                                          max_to_keep=30)

        self.__sess = tf.Session()
        self.__sess.run(tf.global_variables_initializer())
        tf.summary.FileWriter(_DIR_LOG, self.__sess.graph)

        if self.__RESTORE_STEP > 0:
            self.__load_model()

    def __save_model(self):
        self.__net_saver.save(self.__sess, DQNSolver.PATH_NET % self.__learn_step,
                              write_meta_graph=False)
        with open(DQNSolver.PATH_VAR % self.__learn_step, "w") as f:
            json.dump({
                "epsilon": self.__epsilon,
                "beta": self.__beta,
            }, f, indent=2)

    def __load_model(self):
        self.__net_saver.restore(self.__sess, DQNSolver.PATH_NET % self.__RESTORE_STEP)
        with open(DQNSolver.PATH_VAR % self.__RESTORE_STEP, "r") as f:
            var = json.load(f)
        self.__epsilon = var["epsilon"]
        self.__beta = var["beta"]
        self.__learn_step = self.__RESTORE_STEP + 1
        log("model loaded | RESTORE_STEP: %d | epsilon: %.6f | beta: %.6f"
            % (self.__RESTORE_STEP, self.__epsilon, self.__beta))

    def __build_net(self):

        def __build_layers(x, name, w_init_, b_init_):

            input_2d = tf.reshape(tensor=x,
                                  shape=[-1, self.map.num_rows - 2, self.map.num_cols - 2, 1],
                                  name="input_2d")

            conv1 = tf.layers.conv2d(inputs=input_2d,
                                     filters=32,
                                     kernel_size=3,
                                     strides=1,
                                     padding='valid',
                                     activation=tf.nn.relu,
                                     kernel_initializer=w_init_,
                                     bias_initializer=b_init_,
                                     name="conv1")

            conv2 = tf.layers.conv2d(inputs=conv1,
                                     filters=64,
                                     kernel_size=3,
                                     strides=1,
                                     padding='valid',
                                     activation=tf.nn.relu,
                                     kernel_initializer=w_init_,
                                     bias_initializer=b_init_,
                                     name="conv2")

            conv2_flat = tf.reshape(tensor=conv2,
                                    shape=[-1, 4 * 4 * 64],
                                    name="conv2_flat")

            fc1 = tf.layers.dense(inputs=conv2_flat,
                                  units=512,
                                  activation=tf.nn.relu,
                                  kernel_initializer=w_init_,
                                  bias_initializer=b_init_,
                                  name="fc1")

            q_all = tf.layers.dense(inputs=fc1,
                                    units=self.__NUM_ACTIONS,
                                    kernel_initializer=w_init_,
                                    bias_initializer=b_init_,
                                    name=name)

            return q_all  # Shape: (None, num_actions)

        def __filter_actions(q_all, actions):
            with tf.variable_scope("action_filter"):
                indices = tf.range(tf.shape(q_all)[0], dtype=tf.int32)
                action_indices = tf.stack([indices, actions], axis=1)
                return tf.gather_nd(q_all, action_indices)  # Shape: (None, )

        # Input tensor for eval net
        self.__state_eval = tf.placeholder(
            tf.float32, [None, self.__NUM_FEATURES], name="state_eval")

        # Input tensor for target net
        self.__state_target = tf.placeholder(
            tf.float32, [None, self.__NUM_FEATURES], name="state_target")

        # Input tensor for actions taken by agent
        self.__action = tf.placeholder(
            tf.int32, [None, ], name="action")

        # Input tensor for rewards received by agent
        self.__reward = tf.placeholder(
            tf.float32, [None, ], name="reward")

        # Input tensor for whether episodes are finished
        self.__done = tf.placeholder(
            tf.bool, [None, ], name="done")

        # Input tensor for eval net output of next state
        self.__q_eval_all_nxt = tf.placeholder(
            tf.float32, [None, self.__NUM_ACTIONS], name="q_eval_all_nxt")

        # Input tensor for importance-sampling weights
        self.__IS_weights = tf.placeholder(
            tf.float32, [None, ], name="IS_weights")

        SCOPE_EVAL_NET = "eval_net"
        SCOPE_TARGET_NET = "target_net"

        w_init = tf.truncated_normal_initializer(mean=0, stddev=0.1)
        b_init = tf.constant_initializer(0.1)

        with tf.variable_scope(SCOPE_EVAL_NET):
            # Eval net output
            self.__q_eval_all = __build_layers(self.__state_eval, "q_eval_all", w_init, b_init)

        with tf.variable_scope("q_eval"):
            q_eval = __filter_actions(self.__q_eval_all, self.__action)

        with tf.variable_scope(SCOPE_TARGET_NET):
            # Target net output
            q_nxt_all = __build_layers(self.__state_target, "q_nxt_all", w_init, b_init)

        with tf.variable_scope("q_target"):
            # Double DQN: choose max reward actions using eval net
            max_actions = tf.argmax(self.__q_eval_all_nxt, axis=1, output_type=tf.int32)
            q_nxt = __filter_actions(q_nxt_all, max_actions)
            q_target = self.__reward + self.__GAMMA * q_nxt * \
                (1.0 - tf.cast(self.__done, tf.float32))
            q_target = tf.stop_gradient(q_target)

        with tf.variable_scope("loss"):
            self.__loss = tf.reduce_mean(self.__IS_weights \
                * tf.squared_difference(q_eval, q_target), name="loss")
            self.__abs_errs = tf.abs(q_eval - q_target, name="abs_errs")  # To update sum tree

        with tf.variable_scope("train"):
            self.__train = tf.train.RMSPropOptimizer(
                learning_rate=self.__LR, momentum=self.__MOMENTUM
            ).minimize(self.__loss)

        # Replace target net params with eval net's
        with tf.variable_scope("replace"):
            eval_params = tf.get_collection(
                tf.GraphKeys.GLOBAL_VARIABLES, scope=SCOPE_EVAL_NET)
            target_params = tf.get_collection(
                tf.GraphKeys.GLOBAL_VARIABLES, scope=SCOPE_TARGET_NET)
            self.__replace_target = [
                tf.assign(t, e) for t, e in zip(target_params, eval_params)
            ]

        return eval_params, target_params

    def next_direc(self):
        """Override super class."""
        return self.__SNAKE_ACTIONS[self.__choose_action(e_greedy=False)]

    def loss_history(self):
        steps = list(range(self.__RESTORE_STEP + 1, self.__learn_step))
        if len(steps) + 1 == len(self.__history_loss):  # Keyboard interrupt err
            steps.append(self.__learn_step)
        return steps, self.__history_loss

    def reward_history(self):
        episodes = range(1, len(self.__history_reward) + 1)
        return episodes, self.__history_reward

    def avg_reward_history(self):
        steps = list(range(self.__RESTORE_STEP + 1, self.__learn_step))
        if len(steps) + 1 == len(self.__history_loss):  # Keyboard interrupt err
            steps.append(self.__learn_step)
        return steps, self.__history_avg_reward

    def train(self):
        state_cur = self.map.state()
        action = self.__choose_action()
        reward, state_nxt, done = self.__step(action)
        self.__store_transition(state_cur, action, reward, state_nxt, done)

        self.__tot_reward += reward
        if done:
            self.__history_reward.append(self.__tot_reward)
            self.__tot_reward = 0

        if self.__mem_cnt >= self.__MEM_SIZE:
            if self.__mem_cnt % self.__FREQ_LEARN == 0:
                self.__learn()
        elif self.__mem_cnt % self.__FREQ_LOG == 0:
            log("mem_cnt: %d" % self.__mem_cnt)

        return done

    def __choose_action(self, e_greedy=True):
        action_idx = None

        if e_greedy and np.random.uniform() < self.__epsilon:
            while True:
                action_idx = np.random.randint(0, self.__NUM_ACTIONS)
                if Direc.opposite(self.snake.direc) != self.__SNAKE_ACTIONS[action_idx]:
                    break
        else:
            state = self.map.state()[np.newaxis, :]
            q_eval_all = self.__sess.run(
                self.__q_eval_all,
                feed_dict={
                    self.__state_eval: state,
                }
            )
            q_eval_all = q_eval_all[0]
            # Find indices of actions with 1st and 2nd largest q value
            action_indices = np.argpartition(q_eval_all, q_eval_all.size - 2)
            action_idx = action_indices[-1]
            # If opposite direction, return direction with 2nd largest q value
            if Direc.opposite(self.snake.direc) == self.__SNAKE_ACTIONS[action_idx]:
                action_idx = action_indices[-2]

        return action_idx

    def __step(self, action_idx):
        direc = self.__SNAKE_ACTIONS[action_idx]
        nxt_pos = self.snake.head().adj(direc)
        nxt_type = self.map.point(nxt_pos).type
        self.snake.move(direc)

        reward = 0
        if nxt_type == PointType.EMPTY:
            reward = self.__RWD_EMPTY
        elif nxt_type == PointType.FOOD:
            reward = self.__RWD_FOOD
        else:
            reward = self.__RWD_DEAD

        state_nxt = self.map.state()
        done = self.snake.dead or self.map.is_full()

        return reward, state_nxt, done

    def __store_transition(self, state_cur, action, reward, state_nxt, done):
        self.__mem.store((state_cur, action, reward, state_nxt, done))
        self.__mem_cnt += 1

    def __learn(self):
        log_msg = "step %d | mem_cnt: %d | epsilon: %.6f | beta: %.6f" % \
                  (self.__learn_step, self.__mem_cnt, self.__epsilon, self.__beta)

        # Sample batch from memory
        batch, IS_weights, tree_indices = self.__mem.sample(self.__MEM_BATCH, self.__beta)
        batch_state_cur = [x[0] for x in batch]
        batch_action = [x[1] for x in batch]
        batch_reward = [x[2] for x in batch]
        batch_state_nxt = [x[3] for x in batch]
        batch_done = [x[4] for x in batch]

        # Compute eval net output for next state (to compute q target)
        q_eval_all_nxt = self.__sess.run(
            self.__q_eval_all,
            feed_dict={
                self.__state_eval: batch_state_nxt,
            }
        )

        # Learn
        _, loss, abs_errs = self.__sess.run(
            [self.__train, self.__loss, self.__abs_errs],
            feed_dict={
                self.__state_eval: batch_state_cur,
                self.__state_target: batch_state_nxt,
                self.__action: batch_action,
                self.__reward: batch_reward,
                self.__done: batch_done,
                self.__q_eval_all_nxt: q_eval_all_nxt,
                self.__IS_weights: IS_weights,
            }
        )
        self.__history_loss.append(loss)
        log_msg += " | loss: %.6f" % loss

        # Compute average reward
        avg_reward = 0
        if self.__history_reward:
            avg_reward = np.mean(self.__history_reward[-self.__NUM_AVG_RWD:])
        self.__history_avg_reward.append(avg_reward)
        log_msg += " | avg_reward: %.6f" % avg_reward

        # Update sum tree
        self.__mem.update(tree_indices, abs_errs)

        # Replace target
        if self.__learn_step == 1 or self.__learn_step % self.__FREQ_REPLACE == 0:
            self.__sess.run(self.__replace_target)
            log_msg += " | target net replaced"

        # Save model
        if self.__learn_step % self.__FREQ_SAVE == 0:
            self.__save_model()
            log_msg += " | model saved"

        if self.__learn_step == 1 or self.__learn_step % self.__FREQ_LOG == 0:
            log(log_msg)

        self.__learn_step += 1
        self.__epsilon = max(self.__EPSILON_MIN, self.__epsilon - self.__EPSILON_DEC)
        self.__beta = min(1.0, self.__beta + self.__BETA_INC)
