from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import copy
import numpy as np
from six.moves import xrange
import tensorflow as tf
import warnings
import pdb
import gc
from cleverhans import utils
import logging

_logger = utils.create_logger("cleverhans.attacks_tf_SAE")
_logger.setLevel(logging.INFO)

class SmoothCarliniWagnerDense(object):

    def __init__(self, sess, model, batch_size, confidence,
                 targeted, learning_rate,
                 binary_search_steps, max_iterations,
                 abort_early, initial_const,
                 clip_min, clip_max, flag, num_labels, shape):
        """
        Return a tensor that constructs adversarial examples for the given
        input. Generate uses tf.py_func in order to operate over tensors.
        :param sess: a TF session.
        :param model: a cleverhans.model.Model object.
        :param batch_size: Number of attacks to run simultaneously.
        :param confidence: Confidence of adversarial examples: higher produces
                           examples with larger l2 distortion, but more
                           strongly classified as adversarial.
        :param targeted: boolean controlling the behavior of the adversarial
                         examples produced. If set to False, they will be
                         misclassified in any wrong class. If set to True,
                         they will be misclassified in a chosen target class.
        :param learning_rate: The learning rate for the attack algorithm.
                              Smaller values produce better results but are
                              slower to converge.
        :param binary_search_steps: The number of times we perform binary
                                    search to find the optimal tradeoff-
                                    constant between norm of the purturbation
                                    and confidence of the classification.
        :param max_iterations: The maximum number of iterations. Setting this
                               to a larger value will produce lower distortion
                               results. Using only a few iterations requires
                               a larger learning rate, and will produce larger
                               distortion results.
        :param abort_early: If true, allows early aborts if gradient descent
                            is unable to make progress (i.e., gets stuck in
                            a local minimum).
        :param initial_const: The initial tradeoff-constant to use to tune the
                              relative importance of size of the pururbation
                              and confidence of classification.
                              If binary_search_steps is large, the initial
                              constant is not important. A smaller value of
                              this constant gives lower distortion results.
        :param clip_min: (optional float) Minimum input component value.
        :param clip_max: (optional float) Maximum input component value.
        :param num_labels: the number of classes in the model's output.
        :param shape: the shape of the model's input tensor.
        :param A: the tensor of matrix of the smooth
        """
        from utils_SAE import CG

        self.sess = sess
        self.TARGETED = targeted
        self.LEARNING_RATE = learning_rate
        self.MAX_ITERATIONS = max_iterations
        self.BINARY_SEARCH_STEPS = binary_search_steps
        self.ABORT_EARLY = abort_early
        self.CONFIDENCE = confidence
        self.initial_const = initial_const
        self.batch_size = batch_size
        self.clip_min = clip_min
        self.clip_max = clip_max
        self.model = model
        self.flag = flag

        self.repeat = binary_search_steps >= 10
        self.terminate_situation = 0
        self.iteration = 0

        self.shape = shape = tuple([batch_size] + list(shape))

        # the variable we're going to optimize over
        modifier = tf.Variable(
            np.zeros((batch_size, shape[1], shape[2], shape[3]), dtype=np.float32))

        self.timg = tf.Variable(np.zeros(shape), dtype=tf.float32,
                                name='timg')
        self.tlab = tf.Variable(np.zeros((batch_size, num_labels)),
                                dtype=tf.float32, name='tlab')
        self.const = tf.Variable(np.zeros(batch_size), dtype=tf.float32,
                                 name='const')

        # and here's what we use to assign them
        self.assign_timg = tf.placeholder(tf.float32, shape,
                                          name='assign_timg')
        self.assign_tlab = tf.placeholder(tf.float32, (batch_size, num_labels),
                                          name='assign_tlab')
        self.assign_const = tf.placeholder(tf.float32, [batch_size],
                                           name='assign_const')

        self.A = tf.Variable(np.zeros((batch_size, shape[1]*shape[2],shape[1]*shape[2]),
            dtype=np.float32))
        self.assign_At = tf.placeholder(tf.float32, (batch_size, self.eig_num, self.eig_num),
                                                        name='assign_At')
        temp = tf.reshape(modifier,(batch_size,shape[1]*shape[2]))
        smo_mod = tf.matmul(modifier,self.A)

        # the resulting instance, tanh'd to keep bounded from clip_min
        # to clip_max
        self.newimg = tf.clip_by_value(self.timg+smo_mod, clip_min, clip_max)

        # prediction BEFORE-SOFTMAX of the model
        self.output = model.get_logits(self.newimg)

        # distance to the input data
        self.l2dist = tf.reduce_sum(tf.square(smo_mod),
                                    list(range(1, len(shape))))

        # true l2 distortion
        oo = tf.zeros_like(self.l2dist)
        noeq = tf.equal(oo, self.l2dist)
        noeq_int = tf.to_int32(noeq)
        noeq_res = tf.equal(tf.reduce_sum(noeq_int), tf.reduce_sum(tf.ones_like(noeq_int)))
        def f_t(mm):
            return mm
        def f_f(mm):
            return tf.sqrt(mm)
        self.l2 = tf.cond(noeq_res, lambda: f_t(self.l2dist),lambda: f_f(self.l2dist))

        # compute the probability of the label class versus the maximum other
        real = tf.reduce_sum((self.tlab) * self.output, 1)
        other = tf.reduce_max(
            (1 - self.tlab) * self.output - self.tlab * 10000,
            1)

        if self.TARGETED:
            # if targeted, optimize for making the other class most likely
            loss1 = tf.maximum(0.0, other - real + self.CONFIDENCE)
        else:
            # if untargeted, optimize for making this class least likely.
            loss1 = tf.maximum(0.0, real - other + self.CONFIDENCE)

        # sum up the losses
        self.loss2 = tf.reduce_sum(self.l2)
        #self.loss2 = tf.reduce_sum(self.l2dist)
        self.loss1 = tf.reduce_sum(self.const * loss1)
        self.loss = self.loss1 + self.loss2
        self.loss_each = self.l2 + (self.const*loss1)

        # Setup the adam optimizer and keep track of variables we're creating
        start_vars = set(x.name for x in tf.global_variables())

        self.grads = tf.gradients(self.loss, smo_mod)
        self.grads_before = tf.gradients(self.loss, modifier)
        #r = self.grads
        #p = r
        #s = tf.matmul(r,tf.transpose(r,[0,2,1]))
        #x = self.grads
        #i,r,p,s,self.smo_grads = tf.while_loop(while_condition,body,[i,r,p,s,x])

        optimizer = tf.train.AdamOptimizer(self.LEARNING_RATE)
        self.train = optimizer.minimize(self.loss, var_list=[modifier])
        end_vars = tf.global_variables()
        new_vars = [x for x in end_vars if x.name not in start_vars]

        # these are the variables to initialize when we run
        self.setup = []
        self.setup.append(self.timg.assign(self.assign_timg))
        self.setup.append(self.tlab.assign(self.assign_tlab))
        self.setup.append(self.const.assign(self.assign_const))
        self.setup.append(self.A.assign(self.assign_A))

        self.init = tf.variables_initializer(var_list=[modifier] + new_vars)

    def attack(self, imgs, targets, As):
        """
        Perform the L_2 attack on the given instance for the given targets.
        If self.targeted is true, then the targets represents the target labels
        If self.targeted is false, then targets are the original class labels
        """

        r = []
        for i in range(0, len(imgs), self.batch_size):
            _logger.debug(("Running CWL2 attack on instance " +
                           "{} of {}").format(i, len(imgs)))
            r.extend(self.attack_batch(imgs[i:i + self.batch_size],
                                       targets[i:i + self.batch_size],
                                       As[i:i + self.batch_size]))
        return np.array(r)

    def attack_batch(self, imgs, labs, As):
        """
        Run the attack on a batch of instance and labels.
        """
        def compare(x, y):
            if not isinstance(x, (float, int, np.int64)):
                x = np.copy(x)
                if self.TARGETED:
                    x[y] -= self.CONFIDENCE
                else:
                    x[y] += self.CONFIDENCE
                x = np.argmax(x)
            if self.TARGETED:
                return x == y
            else:
                return x != y

        batch_size = self.batch_size

        oimgs = np.clip(imgs, self.clip_min, self.clip_max)

        # set the lower and upper bounds accordingly
        lower_bound = np.zeros(batch_size)
        CONST = np.ones(batch_size) * self.initial_const
        upper_bound = np.ones(batch_size) * 1e10

        # placeholders for the best l2, score, and instance attack found so far
        o_bestl2 = [1e10] * batch_size
        o_bestscore = [-1] * batch_size
        o_bestattack = np.copy(oimgs)

        for outer_step in range(self.BINARY_SEARCH_STEPS):
            # completely reset adam's internal state.
            self.sess.run(self.init)
            batch = imgs[:batch_size]
            batchlab = labs[:batch_size]
            batchA = As[:batch_size]

            bestl2 = [1e10] * batch_size
            bestscore = [-1] * batch_size
            _logger.debug("  Binary search step {} of {}".
                          format(outer_step, self.BINARY_SEARCH_STEPS))

            # The last iteration (if we run many steps) repeat the search once.
            if self.repeat and outer_step == self.BINARY_SEARCH_STEPS - 1:
                CONST = upper_bound

            # set the variables so that we don't have to send them over again
            self.sess.run(self.setup, {self.assign_timg: batch,
                                       self.assign_tlab: batchlab,
                                       self.assign_const: CONST,
                                       self.assign_A: batchA})

            prev = 1e6
            prev_each = 1e6 * np.ones((batch_size,))
            final_iteration = 0
            for iteration in range(self.MAX_ITERATIONS):
                final_iteration = iteration
                # perform the attack
                _, l, l2s, scores, nimg, l_each = self.sess.run([self.train,
                                                         self.loss,
                                                         self.l2,
                                                         self.output,
                                                         self.newimg,
                                                         self.loss_each])

                if iteration % ((self.MAX_ITERATIONS // 10) or 1) == 0:
                    _logger.debug(("    Iteration {} of {}: loss={:.3g} " +
                                   "l2={:.3g} f={:.3g}")
                                  .format(iteration, self.MAX_ITERATIONS,
                                          l, np.mean(l2s), np.mean(scores)))

                # check if we should abort search if we're getting nowhere.
                if self.ABORT_EARLY and \
                   iteration % ((self.MAX_ITERATIONS // 10) or 1) == 0:
                    #num_agree = sum(l_each >= (prev_each* .9999))
                    #if num_agree >= batch_size:
                    if l > prev * .9999:
                        msg = "    Failed to make progress; stop early"
                        _logger.debug(msg)
                        self.terminate_situation = self.terminate_situation+1
                        #print('stop from the l> prev')
                        break
                    prev = l
                    prev_each =l_each

                # adjust the best result found so far
                for e, (l2, sc, ii) in enumerate(zip(l2s, scores, nimg)):
                    lab = np.argmax(batchlab[e])
                    if l2 < bestl2[e] and compare(sc, lab):
                        bestl2[e] = l2
                        bestscore[e] = np.argmax(sc)
                    if l2 < o_bestl2[e] and compare(sc, lab):
                        o_bestl2[e] = l2
                        o_bestscore[e] = np.argmax(sc)
                        o_bestattack[e] = ii
            self.iteration = final_iteration+self.iteration

            # adjust the constant as needed
            for e in range(batch_size):
                if compare(bestscore[e], np.argmax(batchlab[e])) and \
                   bestscore[e] != -1:
                    # success, divide const by two
                    upper_bound[e] = min(upper_bound[e], CONST[e])
                    if upper_bound[e] < 1e9:
                        CONST[e] = (lower_bound[e] + upper_bound[e]) / 2
                    #print('success:%f',CONST[e])
                else:
                    # failure, either multiply by 10 if no solution found yet
                    #          or do binary search with the known upper bound
                    lower_bound[e] = max(lower_bound[e], CONST[e])
                    if upper_bound[e] < 1e9:
                        CONST[e] = (lower_bound[e] + upper_bound[e]) / 2
                    else:
                        CONST[e] *= 10
                    #print('fail:%f',CONST[e])
            _logger.debug("  Successfully generated adversarial examples " +
                          "on {} of {} instances.".
                          format(sum(upper_bound < 1e9), batch_size))
            o_bestl2 = np.array(o_bestl2)
            mean = np.mean(np.sqrt(o_bestl2[o_bestl2 < 1e9]))
            _logger.debug("   Mean successful distortion: {:.4g}".format(mean))

        # return the best solution found
        o_bestl2 = np.array(o_bestl2)
        return o_bestattack

class SmoothCarliniWagnerL2Sparse(object):

    def __init__(self, sess, model, batch_size, confidence,
                 targeted, learning_rate,
                 binary_search_steps, max_iterations,
                 abort_early, initial_const,
                 clip_min, clip_max, flag, num_labels, shape, alpha):
        """
        Return a tensor that constructs adversarial examples for the given
        input. Generate uses tf.py_func in order to operate over tensors.
        :param sess: a TF session.
        :param model: a cleverhans.model.Model object.
        :param batch_size: Number of attacks to run simultaneously.
        :param confidence: Confidence of adversarial examples: higher produces
                           examples with larger l2 distortion, but more
                           strongly classified as adversarial.
        :param targeted: boolean controlling the behavior of the adversarial
                         examples produced. If set to False, they will be
                         misclassified in any wrong class. If set to True,
                         they will be misclassified in a chosen target class.
        :param learning_rate: The learning rate for the attack algorithm.
                              Smaller values produce better results but are
                              slower to converge.
        :param binary_search_steps: The number of times we perform binary
                                    search to find the optimal tradeoff-
                                    constant between norm of the purturbation
                                    and confidence of the classification.
        :param max_iterations: The maximum number of iterations. Setting this
                               to a larger value will produce lower distortion
                               results. Using only a few iterations requires
                               a larger learning rate, and will produce larger
                               distortion results.
        :param abort_early: If true, allows early aborts if gradient descent
                            is unable to make progress (i.e., gets stuck in
                            a local minimum).
        :param initial_const: The initial tradeoff-constant to use to tune the
                              relative importance of size of the pururbation
                              and confidence of classification.
                              If binary_search_steps is large, the initial
                              constant is not important. A smaller value of
                              this constant gives lower distortion results.
        :param clip_min: (optional float) Minimum input component value.
        :param clip_max: (optional float) Maximum input component value.
        :param num_labels: the number of classes in the model's output.
        :param shape: the shape of the model's input tensor.
        :param A: the tensor of matrix of the smooth
        :param alpha: the parameter for smooth matrix
        """
        from utils_SAE import CG

        self.sess = sess
        self.TARGETED = targeted
        self.LEARNING_RATE = learning_rate
        self.MAX_ITERATIONS = max_iterations
        self.BINARY_SEARCH_STEPS = binary_search_steps
        self.ABORT_EARLY = abort_early
        self.CONFIDENCE = confidence
        self.initial_const = initial_const
        self.batch_size = batch_size
        self.clip_min = clip_min
        self.clip_max = clip_max
        self.model = model
        self.flag = flag
        self.alpha = alpha

        self.repeat = binary_search_steps >= 10
        self.terminate_situation = 0
        self.iteration = 0

        self.shape = shape = tuple([batch_size] + list(shape))

        # the variable we're going to optimize over
        modifier = tf.Variable(
            np.zeros((batch_size, shape[1], shape[2], shape[3]), dtype=np.float32))

        self.timg = tf.Variable(np.zeros(shape), dtype=tf.float32,
                                name='timg')
        self.tlab = tf.Variable(np.zeros((batch_size, num_labels)),
                                dtype=tf.float32, name='tlab')
        self.const = tf.Variable(np.zeros(batch_size), dtype=tf.float32,
                                 name='const')

        # and here's what we use to assign them
        self.assign_timg = tf.placeholder(tf.float32, shape,
                                          name='assign_timg')
        self.assign_tlab = tf.placeholder(tf.float32, (batch_size, num_labels),
                                          name='assign_tlab')
        self.assign_const = tf.placeholder(tf.float32, [batch_size],
                                           name='assign_const')

        self.A = tf.Variable(
            np.zeros((batch_size, 4,shape[1],shape[2],shape[3]), dtype=np.float32))

        self.assign_A = tf.placeholder(tf.float32, (batch_size, 4,shape[1],shape[2], shape[3]),
                                       name='assign_A')

        # zeros situation
        nn = tf.reduce_sum(tf.multiply(modifier,modifier),axis=[1,2])
        oo = tf.zeros_like(nn)
        noeq = tf.equal(nn, oo)
        noeq_int = tf.to_int32(noeq)
        noeq_res = tf.equal(tf.reduce_sum(noeq_int), tf.reduce_sum(tf.ones_like(noeq_int)))

        def f_false(modifier):
            smo_mod = CG(self.A, modifier, shape)
            return smo_mod

        def f_true(mm):
            smo_mod = tf.reshape(mm,shape)
            return smo_mod

        smo_mod = tf.cond(noeq_res, lambda: f_true(modifier),lambda: f_false(modifier))
        smo_mod = (1-self.alpha)*smo_mod

        # the resulting instance, tanh'd to keep bounded from clip_min
        # to clip_max
        self.newimg = tf.clip_by_value(self.timg+smo_mod, clip_min, clip_max)

        # prediction BEFORE-SOFTMAX of the model
        self.output = model.get_logits(self.newimg)

        # distance to the input data
        self.l2dist = tf.reduce_sum(tf.square(smo_mod),
                                    list(range(1, len(shape))))

        # true l2 distortion
        oo = tf.zeros_like(self.l2dist)
        noeq = tf.equal(oo, self.l2dist)
        noeq_int = tf.to_int32(noeq)
        noeq_res = tf.equal(tf.reduce_sum(noeq_int), tf.reduce_sum(tf.ones_like(noeq_int)))
        def f_t(mm):
            return mm
        def f_f(mm):
            return tf.sqrt(mm)
        self.l2 = tf.cond(noeq_res, lambda: f_t(self.l2dist),lambda: f_f(self.l2dist))

        # compute the probability of the label class versus the maximum other
        real = tf.reduce_sum((self.tlab) * self.output, 1)
        other = tf.reduce_max(
            (1 - self.tlab) * self.output - self.tlab * 10000,
            1)

        if self.TARGETED:
            # if targeted, optimize for making the other class most likely
            loss1 = tf.maximum(0.0, other - real + self.CONFIDENCE)
        else:
            # if untargeted, optimize for making this class least likely.
            loss1 = tf.maximum(0.0, real - other + self.CONFIDENCE)

        # sum up the losses
        self.loss2 = tf.reduce_sum(self.l2)
        #self.loss2 = tf.reduce_sum(self.l2dist)
        self.loss1 = tf.reduce_sum(self.const * loss1)
        self.loss = self.loss1 + self.loss2
        self.loss_each = self.l2 + (self.const*loss1)

        # Setup the adam optimizer and keep track of variables we're creating
        start_vars = set(x.name for x in tf.global_variables())

        self.grads = tf.gradients(self.loss, smo_mod)
        self.grads_before = tf.gradients(self.loss, modifier)
        #r = self.grads
        #p = r
        #s = tf.matmul(r,tf.transpose(r,[0,2,1]))
        #x = self.grads
        #i,r,p,s,self.smo_grads = tf.while_loop(while_condition,body,[i,r,p,s,x])

        optimizer = tf.train.AdamOptimizer(self.LEARNING_RATE)
        self.train = optimizer.minimize(self.loss, var_list=[modifier])
        end_vars = tf.global_variables()
        new_vars = [x for x in end_vars if x.name not in start_vars]

        # these are the variables to initialize when we run
        self.setup = []
        self.setup.append(self.timg.assign(self.assign_timg))
        self.setup.append(self.tlab.assign(self.assign_tlab))
        self.setup.append(self.const.assign(self.assign_const))
        self.setup.append(self.A.assign(self.assign_A))

        self.init = tf.variables_initializer(var_list=[modifier] + new_vars)

    def attack(self, imgs, targets, As):
        """
        Perform the L_2 attack on the given instance for the given targets.
        If self.targeted is true, then the targets represents the target labels
        If self.targeted is false, then targets are the original class labels
        """

        r = []
        for i in range(0, len(imgs), self.batch_size):
            _logger.debug(("Running CWL2 attack on instance " +
                           "{} of {}").format(i, len(imgs)))
            r.extend(self.attack_batch(imgs[i:i + self.batch_size],
                                       targets[i:i + self.batch_size],
                                       As[i:i + self.batch_size]))
        return np.array(r)

    def attack_batch(self, imgs, labs, As):
        """
        Run the attack on a batch of instance and labels.
        """
        def compare(x, y):
            if not isinstance(x, (float, int, np.int64)):
                x = np.copy(x)
                if self.TARGETED:
                    x[y] -= self.CONFIDENCE
                else:
                    x[y] += self.CONFIDENCE
                x = np.argmax(x)
            if self.TARGETED:
                return x == y
            else:
                return x != y

        batch_size = self.batch_size

        oimgs = np.clip(imgs, self.clip_min, self.clip_max)

        # set the lower and upper bounds accordingly
        lower_bound = np.zeros(batch_size)
        CONST = np.ones(batch_size) * self.initial_const
        upper_bound = np.ones(batch_size) * 1e10

        # placeholders for the best l2, score, and instance attack found so far
        o_bestl2 = [1e10] * batch_size
        o_bestscore = [-1] * batch_size
        o_bestattack = np.copy(oimgs)

        for outer_step in range(self.BINARY_SEARCH_STEPS):
            # completely reset adam's internal state.
            self.sess.run(self.init)
            batch = imgs[:batch_size]
            batchlab = labs[:batch_size]
            batchA = As[:batch_size]

            bestl2 = [1e10] * batch_size
            bestscore = [-1] * batch_size
            _logger.debug("  Binary search step {} of {}".
                          format(outer_step, self.BINARY_SEARCH_STEPS))

            # The last iteration (if we run many steps) repeat the search once.
            if self.repeat and outer_step == self.BINARY_SEARCH_STEPS - 1:
                CONST = upper_bound

            # set the variables so that we don't have to send them over again
            self.sess.run(self.setup, {self.assign_timg: batch,
                                       self.assign_tlab: batchlab,
                                       self.assign_const: CONST,
                                       self.assign_A: batchA})

            prev = 1e6
            prev_each = 1e6 * np.ones((batch_size,))
            final_iteration = 0
            for iteration in range(self.MAX_ITERATIONS):
                final_iteration = iteration
                # perform the attack
                _, l, l2s, scores, nimg, l_each = self.sess.run([self.train,
                                                         self.loss,
                                                         self.l2,
                                                         self.output,
                                                         self.newimg,
                                                         self.loss_each])

                if iteration % ((self.MAX_ITERATIONS // 10) or 1) == 0:
                    _logger.debug(("    Iteration {} of {}: loss={:.3g} " +
                                   "l2={:.3g} f={:.3g}")
                                  .format(iteration, self.MAX_ITERATIONS,
                                          l, np.mean(l2s), np.mean(scores)))

                # check if we should abort search if we're getting nowhere.
                if self.ABORT_EARLY and \
                   iteration % ((self.MAX_ITERATIONS // 10) or 1) == 0:
                    #num_agree = sum(l_each >= (prev_each* .9999))
                    #if num_agree >= batch_size:
                    if l > prev * .9999:
                        msg = "    Failed to make progress; stop early"
                        _logger.debug(msg)
                        self.terminate_situation = self.terminate_situation+1
                        #print('stop from the l> prev')
                        break
                    prev = l
                    prev_each =l_each

                # adjust the best result found so far
                for e, (l2, sc, ii) in enumerate(zip(l2s, scores, nimg)):
                    lab = np.argmax(batchlab[e])
                    if l2 < bestl2[e] and compare(sc, lab):
                        bestl2[e] = l2
                        bestscore[e] = np.argmax(sc)
                    if l2 < o_bestl2[e] and compare(sc, lab):
                        o_bestl2[e] = l2
                        o_bestscore[e] = np.argmax(sc)
                        o_bestattack[e] = ii
            self.iteration = final_iteration+self.iteration

            # adjust the constant as needed
            for e in range(batch_size):
                if compare(bestscore[e], np.argmax(batchlab[e])) and \
                   bestscore[e] != -1:
                    # success, divide const by two
                    upper_bound[e] = min(upper_bound[e], CONST[e])
                    if upper_bound[e] < 1e9:
                        CONST[e] = (lower_bound[e] + upper_bound[e]) / 2
                    #print('success:%f',CONST[e])
                else:
                    # failure, either multiply by 10 if no solution found yet
                    #          or do binary search with the known upper bound
                    lower_bound[e] = max(lower_bound[e], CONST[e])
                    if upper_bound[e] < 1e9:
                        CONST[e] = (lower_bound[e] + upper_bound[e]) / 2
                    else:
                        CONST[e] *= 10
                    #print('fail:%f',CONST[e])
            _logger.debug("  Successfully generated adversarial examples " +
                          "on {} of {} instances.".
                          format(sum(upper_bound < 1e9), batch_size))
            o_bestl2 = np.array(o_bestl2)
            mean = np.mean(np.sqrt(o_bestl2[o_bestl2 < 1e9]))
            _logger.debug("   Mean successful distortion: {:.4g}".format(mean))

        # return the best solution found
        o_bestl2 = np.array(o_bestl2)
        return o_bestattack
