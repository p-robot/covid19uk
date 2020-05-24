"""MCMC Update classes for stochastic epidemic models"""
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow_probability.python.mcmc.internal import util as mcmc_util
from tensorflow_probability.python.mcmc.random_walk_metropolis import UncalibratedRandomWalkResults
from tensorflow_probability.python.util import SeedStream

tfd = tfp.distributions

DTYPE = tf.float64


def _is_within(x, low, high):
    """Returns true if low <= x < high"""
    return tf.logical_and(tf.less_equal(low, x), tf.less(x, high))


def _max_free_events(events, target_t, target_id, constraint_t, constraint_id):
    """Returns the maximum number of free events to move in target_events constrained by
    constraining_events.
    :param events: a [T, M, X] tensor of transition events
    :param target_t: the target time
    :param target_id: the Xth index of the target event
    :param constraining_t: the Tth time of the constraint
    :param constraining_id: the Xth index of the constraining event, -1 implies no constraint
    :returns: a tensor of shape [M] of max free events, dtype=target_events.dtype
    """
    def true_fn():
        target_events_ = tf.cast(tf.gather(events, target_id, axis=-1), tf.int32)
        target_cumsum = tf.cumsum(target_events_, axis=0)
        constraining_cumsum = tf.cumsum(tf.cast(tf.gather(events, constraint_id, axis=-1), tf.int32), axis=0)

        free_events = tf.abs(
            tf.gather(target_cumsum, constraint_t, axis=0) - tf.gather(constraining_cumsum, constraint_t,
                                                                         axis=0))
        max_free_events = tf.minimum(free_events, tf.gather(target_events_, target_t, axis=0))
        return tf.cast(max_free_events, dtype=events.dtype)

    def false_fn():
        return tf.gather(events[..., target_id], target_t, axis=0)

    return tf.cond(constraint_id != -1, true_fn, false_fn)


def _move_events(event_tensor, event_id, from_t, to_t, n_move):
    """Subtracts n_move from event_tensor[from_t, :, event_id]
    and adds n_move to event_tensor[to_t, :, event_id]."""
    num_meta = n_move.shape[-1]
    indices = tf.stack([tf.broadcast_to(from_t, [num_meta]),  # Timepoint
                        tf.range(num_meta),  # All meta-populations
                        tf.broadcast_to([event_id], [num_meta])], axis=-1)  # Event
    # Subtract x_star from the [from_t, :, event_id] row of the state tensor
    next_state = tf.tensor_scatter_nd_sub(event_tensor, indices, n_move)
    indices = tf.stack([tf.broadcast_to(to_t, [num_meta]),
                        tf.range(num_meta),
                        tf.broadcast_to(event_id, [num_meta])], axis=-1)
    # Add x_star to the [to_t, :, event_id] row of the state tensor
    next_state = tf.tensor_scatter_nd_add(next_state, indices, n_move)
    return next_state


def random_walk_mvnorm_fn(covariance, p_u=0.95, name=None):
    """Returns callable that adds Multivariate Normal noise to the input
    :param covariance: the covariance matrix of the mvnorm proposal
    :param p_u: the bounded convergence parameter.  If equal to 1, then all proposals are drawn from the
                MVN(0, covariance) distribution, if less than 1, proposals are drawn from MVN(0, covariance)
                with probabilit p_u, and MVN(0, 0.1^2I_d/d) otherwise.
    :returns: a function implementing the proposal.
    """
    covariance = covariance + tf.eye(covariance.shape[0], dtype=DTYPE) * 1.e-9
    scale_tril = tf.linalg.cholesky(covariance)
    rv_adapt = tfp.distributions.MultivariateNormalTriL(loc=tf.zeros(covariance.shape[0], dtype=DTYPE),
                                                        scale_tril=scale_tril)
    rv_fix = tfp.distributions.Normal(loc=tf.zeros(covariance.shape[0], dtype=DTYPE),
                                      scale=0.01 / covariance.shape[0], )
    u = tfp.distributions.Bernoulli(probs=p_u)

    def _fn(state_parts, seed):
        with tf.name_scope(name or 'random_walk_mvnorm_fn'):
            def proposal():
                rv = tf.stack([rv_fix.sample(), rv_adapt.sample()])
                uv = u.sample()
                return tf.gather(rv, uv)

            new_state_parts = [proposal() + state_part for state_part in state_parts]
            return new_state_parts

    return _fn


class UncalibratedLogRandomWalk(tfp.mcmc.UncalibratedRandomWalk):
    def one_step(self, current_state, previous_kernel_results):
        with tf.name_scope(mcmc_util.make_name(self.name, 'rwm', 'one_step')):
            with tf.name_scope('initialize'):
                if mcmc_util.is_list_like(current_state):
                    current_state_parts = list(current_state)
                else:
                    current_state_parts = [current_state]
                current_state_parts = [
                    tf.convert_to_tensor(s, name='current_state')
                    for s in current_state_parts
                ]

            # Log random walk
            next_state_parts = self.new_state_fn([tf.zeros_like(s) for s in current_state_parts],
                                                 # pylint: disable=not-callable
                                                 self._seed_stream())
            next_state_parts = [cs * tf.exp(ns) for cs, ns in zip(current_state_parts, next_state_parts)]

            # Compute `target_log_prob` so its available to MetropolisHastings.
            next_target_log_prob = self.target_log_prob_fn(*next_state_parts)  # pylint: disable=not-callable

            def maybe_flatten(x):
                return x if mcmc_util.is_list_like(current_state) else x[0]

            log_acceptance_correction = tf.reduce_sum(next_state_parts) - tf.reduce_sum(current_state_parts)

            return [
                maybe_flatten(next_state_parts),
                UncalibratedRandomWalkResults(
                    log_acceptance_correction=log_acceptance_correction,
                    target_log_prob=next_target_log_prob,
                ),
            ]


class UncalibratedEventTimesUpdate(tfp.mcmc.TransitionKernel):
    def __init__(self,
                 target_log_prob_fn,
                 target_event_id,
                 prev_event_id,
                 next_event_id,
                 initial_state,
                 seed=None,
                 name=None):
        """An uncalibrated random walk for event times.
        :param target_log_prob_fn: the log density of the target distribution
        :param target_event_id: the position in the first dimension of the events tensor that we wish to move
        :param prev_event_id: the position of the previous event in the events tensor
        :param next_event_id: the position of the next event in the events tensor
        :param initial_state: the initial state tensor
        :param seed: a random seed
        :param name: the name of the update step
        """
        self._target_log_prob_fn = target_log_prob_fn
        self._seed_stream = SeedStream(seed, salt='UncalibratedEventTimesUpdate')
        self._name = name
        self._parameters = dict(
            target_log_prob_fn=target_log_prob_fn,
            target_event_id=target_event_id,
            prev_event_id=prev_event_id,
            next_event_id=next_event_id,
            initial_state=initial_state,
            seed=seed,
            name=name)

    @property
    def target_log_prob_fn(self):
        return self._parameters['target_log_prob_fn']

    @property
    def target_event_id(self):
        return self._parameters['target_event_id']

    @property
    def prev_event_id(self):
        return self._parameters['prev_event_id']

    @property
    def next_event_id(self):
        return self._parameters['next_event_id']

    @property
    def seed(self):
        return self._parameters['seed']

    @property
    def name(self):
        return self._parameters['name']

    @property
    def parameters(self):
        """Return `dict` of ``__init__`` arguments and their values."""
        return self._parameters

    @property
    def is_calibrated(self):
        return False

    def one_step(self, current_state, previous_kernel_results):
        """One update of event times.
        :param current_state: a [T, M, X] tensor containing number of events per time t, metapopulation m,
                              and transition x.
        :param previous_kernel_results: an object of type UncalibratedRandomWalkResults.
        :returns: a tuple containing new_state and UncalibratedRandomWalkResults.
        """
        with tf.name_scope('uncalibrated_event_times_rw/onestep'):
            target_events = current_state[..., self.target_event_id]

            num_times = target_events.shape[0]
            num_meta = target_events.shape[1]

            # 1. Choose a timepoint to move, conditional on it having events to move
            current_p = tf.cast(tf.reduce_sum(target_events, axis=1) > 0., target_events.dtype)
            current_t = tf.squeeze(tf.random.categorical(logits=[tf.math.log(current_p)],
                                                         num_samples=1,
                                                         seed=self._seed_stream(),
                                                         dtype=tf.int32))

            # 2. Choose to move +1 or -1 in time
            u = tf.squeeze(tf.random.uniform(shape=[1], seed=self._seed_stream(),
                                             minval=0, maxval=2, dtype=tf.int32))
            direction = tf.gather([-1, 1], u)
            next_t = current_t + direction
            # Select either previous or next event for constraint calculation
            constraint_t = tf.gather([current_t - 1, current_t], u)
            constraining_event_id = tf.gather([self.prev_event_id or -1, self.next_event_id], u)

            # 3. Calculate max number of events to move subject to constraints
            n_max = _max_free_events(current_state, current_t, self.target_event_id, constraint_t,
                                     constraining_event_id)

            # Draw number to move uniformly from n_max
            x_star = tf.floor(tf.random.uniform((), minval=0., maxval=n_max + 1., dtype=current_state.dtype))

            # Propose next_state
            next_state = _move_events(event_tensor=current_state, event_id=self.target_event_id,
                                      from_t=current_t, to_t=next_t,
                                      n_move=x_star)
            next_target_log_prob = self.target_log_prob_fn(next_state)

            # Trap out-of-bounds moves that go outside [0, num_times)
            next_target_log_prob = tf.where(_is_within(next_t, 0, num_times),
                                            next_target_log_prob,
                                            tf.constant(-np.inf, dtype=current_state.dtype))

            # Calculate proposal density
            # 1. Calculate probability of choosing a timepoint
            next_p = tf.cast(tf.reduce_sum(next_state[..., self.target_event_id], axis=1) > 0.,
                             next_state.dtype)
            log_acceptance_correction = tf.math.log(tf.reduce_sum(next_p) / tf.reduce_sum(current_p))

            # 2. Calculate probability of selecting events
            next_n_max = _max_free_events(next_state, next_t, self.target_event_id,
                                          constraint_t, constraining_event_id)
            log_acceptance_correction += tf.reduce_sum(tf.math.log(n_max + 1.) - tf.math.log(next_n_max + 1.))

            return [next_state,
                    tfp.mcmc.random_walk_metropolis.UncalibratedRandomWalkResults(
                        log_acceptance_correction=log_acceptance_correction,
                        target_log_prob=next_target_log_prob
                    )]

    def bootstrap_results(self, init_state):
        with tf.name_scope('uncalibrated_event_times_rw/bootstrap_results'):
            init_state = tf.convert_to_tensor(init_state, dtype=DTYPE)
            init_target_log_prob = self.target_log_prob_fn(init_state)
            return tfp.mcmc.random_walk_metropolis.UncalibratedRandomWalkResults(
                log_acceptance_correction=tf.constant(0., dtype=DTYPE),
                target_log_prob=init_target_log_prob
            )


def get_accepted_results(results):
    if hasattr(results, 'accepted_results'):
        return results.accepted_results
    else:
        return get_accepted_results(results.inner_results)


def set_accepted_results(results, accepted_results):
    if hasattr(results, 'accepted_results'):
        results = results._replace(accepted_results=accepted_results)
        return results
    else:
        next_inner_results = set_accepted_results(results.inner_results, accepted_results)
        return results._replace(inner_results=next_inner_results)


def advance_target_log_prob(next_results, previous_results):
    prev_accepted_results = get_accepted_results(previous_results)
    next_accepted_results = get_accepted_results(next_results)
    next_accepted_results = next_accepted_results._replace(
        target_log_prob=prev_accepted_results.target_log_prob)
    return set_accepted_results(next_results, next_accepted_results)


class MH_within_Gibbs(tfp.mcmc.TransitionKernel):

    def __init__(self, target_log_prob_fn, make_kernel_fns):
        """Metropolis within Gibbs sampling.

        Based on Gibbs idea posted by Pavel Sountsov https://github.com/tensorflow/probability/issues/495

        :param target_log_prob_fn: a function which given a list of state parts calculated the joint logp
        :param make_kernel_fns: a list of functions that return an MH-compatible kernel.  Functions accept a
                                log_prob function which in turn takes a state part.
        """
        self._target_log_prob_fn = target_log_prob_fn
        self._make_kernel_fns = make_kernel_fns

    def is_calibrated(self):
        return True

    def one_step(self, state, step_results):
        prev_step = np.roll(np.arange(len(self._make_kernel_fns)), 1)
        for i, make_kernel_fn in enumerate(self._make_kernel_fns):
            def _target_log_prob_fn_part(state_part):
                state[i] = state_part
                return self._target_log_prob_fn(*state)

            kernel = make_kernel_fn(_target_log_prob_fn_part)
            results = advance_target_log_prob(step_results[i],
                                              step_results[prev_step[i]]) or kernel.bootstrap_results(
                state[i])
            state[i], step_results[i] = kernel.one_step(state[i], results)
        return state, step_results

    def bootstrap_results(self, state):
        results = []
        for i, make_kernel_fn in enumerate(self._make_kernel_fns):
            def _target_log_prob_fn_part(state_part):
                state[i] = state_part
                return self._target_log_prob_fn(*state)

            kernel = make_kernel_fn(_target_log_prob_fn_part)
            results.append(kernel.bootstrap_results(init_state=state[i]))

        return results
