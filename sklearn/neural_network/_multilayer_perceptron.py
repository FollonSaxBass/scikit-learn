"""Multi-layer Perceptron
"""

# Authors: Issam H. Laradji <issam.laradji@gmail.com>
#          Andreas Mueller
#          Jiyuan Qian
# License: BSD 3 clause

import numpy as np

from abc import ABCMeta, abstractmethod
import warnings

import scipy.optimize

from ..base import BaseEstimator, ClassifierMixin, RegressorMixin
from ..base import is_classifier
from ._base import ACTIVATIONS, DERIVATIVES, LOSS_FUNCTIONS
from ._stochastic_optimizers import SGDOptimizer, AdamOptimizer
from ..model_selection import train_test_split
from ..preprocessing import LabelBinarizer
from ..utils import gen_batches, check_random_state
from ..utils import shuffle
from ..utils import check_array, check_X_y, column_or_1d
from ..exceptions import ConvergenceWarning
from ..utils.extmath import safe_sparse_dot
from ..utils.validation import check_is_fitted
from ..utils.multiclass import _check_partial_fit_first_call, unique_labels
from ..utils.multiclass import type_of_target
from ..utils.optimize import _check_optimize_result


_STOCHASTIC_SOLVERS = ['sgd', 'adam']


def _pack(coefs_, intercepts_):
    """Pack the parameters into a single vector."""
    return np.hstack([l.ravel() for l in coefs_ + intercepts_])


class BaseMultilayerPerceptron(BaseEstimator, metaclass=ABCMeta):
    """Base class for MLP classification and regression.

    Warning: This class should not be used directly.
    Use derived classes instead.

    .. versionadded:: 0.18
    """

    @abstractmethod
    def __init__(self, hidden_layer_sizes, activation, solver,
                 alpha, batch_size, learning_rate, learning_rate_init, power_t,
                 max_iter, loss, shuffle, random_state, tol, verbose,
                 warm_start, momentum, nesterovs_momentum, early_stopping,
                 validation_fraction, beta_1, beta_2, epsilon,
                 n_iter_no_change, max_fun):
        self.activation = activation
        self.solver = solver
        self.alpha = alpha
        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.learning_rate_init = learning_rate_init
        self.power_t = power_t
        self.max_iter = max_iter
        self.loss = loss
        self.hidden_layer_sizes = hidden_layer_sizes
        self.shuffle = shuffle
        self.random_state = random_state
        self.tol = tol
        self.verbose = verbose
        self.warm_start = warm_start
        self.momentum = momentum
        self.nesterovs_momentum = nesterovs_momentum
        self.early_stopping = early_stopping
        self.validation_fraction = validation_fraction
        self.beta_1 = beta_1
        self.beta_2 = beta_2
        self.epsilon = epsilon
        self.n_iter_no_change = n_iter_no_change
        self.max_fun = max_fun

    def _unpack(self, packed_parameters):
        """Extract the coefficients and intercepts from packed_parameters."""
        for i in range(self.n_layers_ - 1):
            start, end, shape = self._coef_indptr[i]
            self.coefs_[i] = np.reshape(packed_parameters[start:end], shape)

            start, end = self._intercept_indptr[i]
            self.intercepts_[i] = packed_parameters[start:end]

    def _forward_pass(self, activations):
        """Perform a forward pass on the network by computing the values
        of the neurons in the hidden layers and the output layer.

        Parameters
        ----------
        activations : list, length = n_layers - 1
            The ith element of the list holds the values of the ith layer.
        """
        hidden_activation = ACTIVATIONS[self.activation]
        # Iterate over the hidden layers
        for i in range(self.n_layers_ - 1):
            activations[i + 1] = safe_sparse_dot(activations[i],
                                                 self.coefs_[i])
            activations[i + 1] += self.intercepts_[i]

            # For the hidden layers
            if (i + 1) != (self.n_layers_ - 1):
                activations[i + 1] = hidden_activation(activations[i + 1])

        # For the last layer
        output_activation = ACTIVATIONS[self.out_activation_]
        activations[i + 1] = output_activation(activations[i + 1])

        return activations

    def _compute_loss_grad(self, layer, n_samples, activations, deltas,
                           coef_grads, intercept_grads):
        """Compute the gradient of loss with respect to coefs and intercept for
        specified layer.

        This function does backpropagation for the specified one layer.
        """
        coef_grads[layer] = safe_sparse_dot(activations[layer].T,
                                            deltas[layer])
        coef_grads[layer] += (self.alpha * self.coefs_[layer])
        coef_grads[layer] /= n_samples

        intercept_grads[layer] = np.mean(deltas[layer], 0)

        return coef_grads, intercept_grads

    def _loss_grad_lbfgs(self, packed_coef_inter, X, y, activations, deltas,
                         coef_grads, intercept_grads):
        """Compute the MLP loss function and its corresponding derivatives
        with respect to the different parameters given in the initialization.

        Returned gradients are packed in a single vector so it can be used
        in lbfgs

        Parameters
        ----------
        packed_coef_inter : array-like
            A vector comprising the flattened coefficients and intercepts.

        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            The input data.

        y : array-like, shape (n_samples,)
            The target values.

        activations : list, length = n_layers - 1
            The ith element of the list holds the values of the ith layer.

        deltas : list, length = n_layers - 1
            The ith element of the list holds the difference between the
            activations of the i + 1 layer and the backpropagated error.
            More specifically, deltas are gradients of loss with respect to z
            in each layer, where z = wx + b is the value of a particular layer
            before passing through the activation function

        coef_grads : list, length = n_layers - 1
            The ith element contains the amount of change used to update the
            coefficient parameters of the ith layer in an iteration.

        intercept_grads : list, length = n_layers - 1
            The ith element contains the amount of change used to update the
            intercept parameters of the ith layer in an iteration.

        Returns
        -------
        loss : float
        grad : array-like, shape (number of nodes of all layers,)
        """
        self._unpack(packed_coef_inter)
        loss, coef_grads, intercept_grads = self._backprop(
            X, y, activations, deltas, coef_grads, intercept_grads)
        grad = _pack(coef_grads, intercept_grads)
        return loss, grad

    def _backprop(self, X, y, activations, deltas, coef_grads,
                  intercept_grads):
        """Compute the MLP loss function and its corresponding derivatives
        with respect to each parameter: weights and bias vectors.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            The input data.

        y : array-like, shape (n_samples,)
            The target values.

        activations : list, length = n_layers - 1
             The ith element of the list holds the values of the ith layer.

        deltas : list, length = n_layers - 1
            The ith element of the list holds the difference between the
            activations of the i + 1 layer and the backpropagated error.
            More specifically, deltas are gradients of loss with respect to z
            in each layer, where z = wx + b is the value of a particular layer
            before passing through the activation function

        coef_grads : list, length = n_layers - 1
            The ith element contains the amount of change used to update the
            coefficient parameters of the ith layer in an iteration.

        intercept_grads : list, length = n_layers - 1
            The ith element contains the amount of change used to update the
            intercept parameters of the ith layer in an iteration.

        Returns
        -------
        loss : float
        coef_grads : list, length = n_layers - 1
        intercept_grads : list, length = n_layers - 1
        """
        n_samples = X.shape[0]

        # Forward propagate
        activations = self._forward_pass(activations)

        # Get loss
        loss_func_name = self.loss
        if loss_func_name == 'log_loss' and self.out_activation_ == 'logistic':
            loss_func_name = 'binary_log_loss'
        loss = LOSS_FUNCTIONS[loss_func_name](y, activations[-1])
        # Add L2 regularization term to loss
        values = np.sum(
            np.array([np.dot(s.ravel(), s.ravel()) for s in self.coefs_]))
        loss += (0.5 * self.alpha) * values / n_samples

        # Backward propagate
        last = self.n_layers_ - 2

        # The calculation of delta[last] here works with following
        # combinations of output activation and loss function:
        # sigmoid and binary cross entropy, softmax and categorical cross
        # entropy, and identity with squared loss
        deltas[last] = activations[-1] - y

        # Compute gradient for the last layer
        coef_grads, intercept_grads = self._compute_loss_grad(
            last, n_samples, activations, deltas, coef_grads, intercept_grads)

        # Iterate over the hidden layers
        for i in range(self.n_layers_ - 2, 0, -1):
            deltas[i - 1] = safe_sparse_dot(deltas[i], self.coefs_[i].T)
            inplace_derivative = DERIVATIVES[self.activation]
            inplace_derivative(activations[i], deltas[i - 1])

            coef_grads, intercept_grads = self._compute_loss_grad(
                i - 1, n_samples, activations, deltas, coef_grads,
                intercept_grads)

        return loss, coef_grads, intercept_grads

    def _initialize(self, y, layer_units):
        # set all attributes, allocate weights etc for first call
        # Initialize parameters
        self.n_iter_ = 0
        self.t_ = 0
        self.n_outputs_ = y.shape[1]

        # Compute the number of layers
        self.n_layers_ = len(layer_units)

        # Output for regression
        if not is_classifier(self):
            self.out_activation_ = 'identity'
        # Output for multi class
        elif self._label_binarizer.y_type_ == 'multiclass':
            self.out_activation_ = 'softmax'
        # Output for binary class and multi-label
        else:
            self.out_activation_ = 'logistic'

        # Initialize coefficient and intercept layers
        self.coefs_ = []
        self.intercepts_ = []

        for i in range(self.n_layers_ - 1):
            coef_init, intercept_init = self._init_coef(layer_units[i],
                                                        layer_units[i + 1])
            self.coefs_.append(coef_init)
            self.intercepts_.append(intercept_init)

        if self.solver in _STOCHASTIC_SOLVERS:
            self.loss_curve_ = []
            self._no_improvement_count = 0
            if self.early_stopping:
                self.validation_scores_ = []
                self.best_validation_score_ = -np.inf
            else:
                self.best_loss_ = np.inf

    def _init_coef(self, fan_in, fan_out):
        # Use the initialization method recommended by
        # Glorot et al.
        factor = 6.
        if self.activation == 'logistic':
            factor = 2.
        init_bound = np.sqrt(factor / (fan_in + fan_out))

        # Generate weights and bias:
        coef_init = self._random_state.uniform(-init_bound, init_bound,
                                               (fan_in, fan_out))
        intercept_init = self._random_state.uniform(-init_bound, init_bound,
                                                    fan_out)
        return coef_init, intercept_init

    def _fit(self, X, y, incremental=False):
        # Make sure self.hidden_layer_sizes is a list
        hidden_layer_sizes = self.hidden_layer_sizes
        if not hasattr(hidden_layer_sizes, "__iter__"):
            hidden_layer_sizes = [hidden_layer_sizes]
        hidden_layer_sizes = list(hidden_layer_sizes)

        # Validate input parameters.
        self._validate_hyperparameters()
        if np.any(np.array(hidden_layer_sizes) <= 0):
            raise ValueError("hidden_layer_sizes must be > 0, got %s." %
                             hidden_layer_sizes)

        X, y = self._validate_input(X, y, incremental)
        n_samples, n_features = X.shape

        # Ensure y is 2D
        if y.ndim == 1:
            y = y.reshape((-1, 1))

        self.n_outputs_ = y.shape[1]

        layer_units = ([n_features] + hidden_layer_sizes +
                       [self.n_outputs_])

        # check random state
        self._random_state = check_random_state(self.random_state)

        if not hasattr(self, 'coefs_') or (not self.warm_start and not
                                           incremental):
            # First time training the model
            self._initialize(y, layer_units)

        # lbfgs does not support mini-batches
        if self.solver == 'lbfgs':
            batch_size = n_samples
        elif self.batch_size == 'auto':
            batch_size = min(200, n_samples)
        else:
            if self.batch_size < 1 or self.batch_size > n_samples:
                warnings.warn("Got `batch_size` less than 1 or larger than "
                              "sample size. It is going to be clipped")
            batch_size = np.clip(self.batch_size, 1, n_samples)

        # Initialize lists
        activations = [X] + [None] * (len(layer_units) - 1)
        deltas = [None] * (len(activations) - 1)

        coef_grads = [np.empty((n_fan_in_, n_fan_out_)) for n_fan_in_,
                      n_fan_out_ in zip(layer_units[:-1],
                                        layer_units[1:])]

        intercept_grads = [np.empty(n_fan_out_) for n_fan_out_ in
                           layer_units[1:]]

        # Run the Stochastic optimization solver
        if self.solver in _STOCHASTIC_SOLVERS:
            self._fit_stochastic(X, y, activations, deltas, coef_grads,
                                 intercept_grads, layer_units, incremental)

        # Run the LBFGS solver
        elif self.solver == 'lbfgs':
            self._fit_lbfgs(X, y, activations, deltas, coef_grads,
                            intercept_grads, layer_units)
        return self

    def _validate_hyperparameters(self):
        if not isinstance(self.shuffle, bool):
            raise ValueError("shuffle must be either True or False, got %s." %
                             self.shuffle)
        if self.max_iter <= 0:
            raise ValueError("max_iter must be > 0, got %s." % self.max_iter)
        if self.max_fun <= 0:
            raise ValueError("max_fun must be > 0, got %s." % self.max_fun)
        if self.alpha < 0.0:
            raise ValueError("alpha must be >= 0, got %s." % self.alpha)
        if (self.learning_rate in ["constant", "invscaling", "adaptive"] and
                self.learning_rate_init <= 0.0):
            raise ValueError("learning_rate_init must be > 0, got %s." %
                             self.learning_rate)
        if self.momentum > 1 or self.momentum < 0:
            raise ValueError("momentum must be >= 0 and <= 1, got %s" %
                             self.momentum)
        if not isinstance(self.nesterovs_momentum, bool):
            raise ValueError("nesterovs_momentum must be either True or False,"
                             " got %s." % self.nesterovs_momentum)
        if not isinstance(self.early_stopping, bool):
            raise ValueError("early_stopping must be either True or False,"
                             " got %s." % self.early_stopping)
        if self.validation_fraction < 0 or self.validation_fraction >= 1:
            raise ValueError("validation_fraction must be >= 0 and < 1, "
                             "got %s" % self.validation_fraction)
        if self.beta_1 < 0 or self.beta_1 >= 1:
            raise ValueError("beta_1 must be >= 0 and < 1, got %s" %
                             self.beta_1)
        if self.beta_2 < 0 or self.beta_2 >= 1:
            raise ValueError("beta_2 must be >= 0 and < 1, got %s" %
                             self.beta_2)
        if self.epsilon <= 0.0:
            raise ValueError("epsilon must be > 0, got %s." % self.epsilon)
        if self.n_iter_no_change <= 0:
            raise ValueError("n_iter_no_change must be > 0, got %s."
                             % self.n_iter_no_change)

        # raise ValueError if not registered
        if self.activation not in ACTIVATIONS:
            raise ValueError("The activation '%s' is not supported. Supported "
                             "activations are %s."
                             % (self.activation, list(sorted(ACTIVATIONS))))
        if self.learning_rate not in ["constant", "invscaling", "adaptive"]:
            raise ValueError("learning rate %s is not supported. " %
                             self.learning_rate)
        supported_solvers = _STOCHASTIC_SOLVERS + ["lbfgs"]
        if self.solver not in supported_solvers:
            raise ValueError("The solver %s is not supported. "
                             " Expected one of: %s" %
                             (self.solver, ", ".join(supported_solvers)))

    def _fit_lbfgs(self, X, y, activations, deltas, coef_grads,
                   intercept_grads, layer_units):
        # Store meta information for the parameters
        self._coef_indptr = []
        self._intercept_indptr = []
        start = 0

        # Save sizes and indices of coefficients for faster unpacking
        for i in range(self.n_layers_ - 1):
            n_fan_in, n_fan_out = layer_units[i], layer_units[i + 1]

            end = start + (n_fan_in * n_fan_out)
            self._coef_indptr.append((start, end, (n_fan_in, n_fan_out)))
            start = end

        # Save sizes and indices of intercepts for faster unpacking
        for i in range(self.n_layers_ - 1):
            end = start + layer_units[i + 1]
            self._intercept_indptr.append((start, end))
            start = end

        # Run LBFGS
        packed_coef_inter = _pack(self.coefs_,
                                  self.intercepts_)

        if self.verbose is True or self.verbose >= 1:
            iprint = 1
        else:
            iprint = -1

        opt_res = scipy.optimize.minimize(
                self._loss_grad_lbfgs, packed_coef_inter,
                method="L-BFGS-B", jac=True,
                options={
                    "maxfun": self.max_fun,
                    "maxiter": self.max_iter,
                    "iprint": iprint,
                    "gtol": self.tol
                },
                args=(X, y, activations, deltas, coef_grads, intercept_grads))
        self.n_iter_ = _check_optimize_result("lbfgs", opt_res, self.max_iter)
        self.loss_ = opt_res.fun
        self._unpack(opt_res.x)

    def _fit_stochastic(self, X, y, activations, deltas, coef_grads,
                        intercept_grads, layer_units, incremental):

        if not incremental or not hasattr(self, '_optimizer'):
            params = self.coefs_ + self.intercepts_

            if self.solver == 'sgd':
                self._optimizer = SGDOptimizer(
                    params, self.learning_rate_init, self.learning_rate,
                    self.momentum, self.nesterovs_momentum, self.power_t)
            elif self.solver == 'adam':
                self._optimizer = AdamOptimizer(
                    params, self.learning_rate_init, self.beta_1, self.beta_2,
                    self.epsilon)

        # early_stopping in partial_fit doesn't make sense
        early_stopping = self.early_stopping and not incremental
        if early_stopping:
            # don't stratify in multilabel classification
            should_stratify = is_classifier(self) and self.n_outputs_ == 1
            stratify = y if should_stratify else None
            X, X_val, y, y_val = train_test_split(
                X, y, random_state=self._random_state,
                test_size=self.validation_fraction,
                stratify=stratify)
            if is_classifier(self):
                y_val = self._label_binarizer.inverse_transform(y_val)
        else:
            X_val = None
            y_val = None

        n_samples = X.shape[0]

        if self.batch_size == 'auto':
            batch_size = min(200, n_samples)
        else:
            batch_size = np.clip(self.batch_size, 1, n_samples)

        try:
            for it in range(self.max_iter):
                if self.shuffle:
                    X, y = shuffle(X, y, random_state=self._random_state)
                accumulated_loss = 0.0
                for batch_slice in gen_batches(n_samples, batch_size):
                    activations[0] = X[batch_slice]
                    batch_loss, coef_grads, intercept_grads = self._backprop(
                        X[batch_slice], y[batch_slice], activations, deltas,
                        coef_grads, intercept_grads)
                    accumulated_loss += batch_loss * (batch_slice.stop -
                                                      batch_slice.start)

                    # update weights
                    grads = coef_grads + intercept_grads
                    self._optimizer.update_params(grads)

                self.n_iter_ += 1
                self.loss_ = accumulated_loss / X.shape[0]

                self.t_ += n_samples
                self.loss_curve_.append(self.loss_)
                if self.verbose:
                    print("Iteration %d, loss = %.8f" % (self.n_iter_,
                                                         self.loss_))

                # update no_improvement_count based on training loss or
                # validation score according to early_stopping
                self._update_no_improvement_count(early_stopping, X_val, y_val)

                # for learning rate that needs to be updated at iteration end
                self._optimizer.iteration_ends(self.t_)

                if self._no_improvement_count > self.n_iter_no_change:
                    # not better than last `n_iter_no_change` iterations by tol
                    # stop or decrease learning rate
                    if early_stopping:
                        msg = ("Validation score did not improve more than "
                               "tol=%f for %d consecutive epochs." % (
                                   self.tol, self.n_iter_no_change))
                    else:
                        msg = ("Training loss did not improve more than tol=%f"
                               " for %d consecutive epochs." % (
                                   self.tol, self.n_iter_no_change))

                    is_stopping = self._optimizer.trigger_stopping(
                        msg, self.verbose)
                    if is_stopping:
                        break
                    else:
                        self._no_improvement_count = 0

                if incremental:
                    break

                if self.n_iter_ == self.max_iter:
                    warnings.warn(
                        "Stochastic Optimizer: Maximum iterations (%d) "
                        "reached and the optimization hasn't converged yet."
                        % self.max_iter, ConvergenceWarning)
        except KeyboardInterrupt:
            warnings.warn("Training interrupted by user.")

        if early_stopping:
            # restore best weights
            self.coefs_ = self._best_coefs
            self.intercepts_ = self._best_intercepts

    def _update_no_improvement_count(self, early_stopping, X_val, y_val):
        if early_stopping:
            # compute validation score, use that for stopping
            self.validation_scores_.append(self.score(X_val, y_val))

            if self.verbose:
                print("Validation score: %f" % self.validation_scores_[-1])
            # update best parameters
            # use validation_scores_, not loss_curve_
            # let's hope no-one overloads .score with mse
            last_valid_score = self.validation_scores_[-1]

            if last_valid_score < (self.best_validation_score_ +
                                   self.tol):
                self._no_improvement_count += 1
            else:
                self._no_improvement_count = 0

            if last_valid_score > self.best_validation_score_:
                self.best_validation_score_ = last_valid_score
                self._best_coefs = [c.copy() for c in self.coefs_]
                self._best_intercepts = [i.copy()
                                         for i in self.intercepts_]
        else:
            if self.loss_curve_[-1] > self.best_loss_ - self.tol:
                self._no_improvement_count += 1
            else:
                self._no_improvement_count = 0
            if self.loss_curve_[-1] < self.best_loss_:
                self.best_loss_ = self.loss_curve_[-1]

    def fit(self, X, y):
        """Fit the model to data matrix X and target(s) y.

        Parameters
        ----------
        X : array-like or sparse matrix, shape (n_samples, n_features)
            The input data.

        y : array-like, shape (n_samples,) or (n_samples, n_outputs)
            The target values (class labels in classification, real numbers in
            regression).

        Returns
        -------
        self : returns a trained MLP model.
        """
        return self._fit(X, y, incremental=False)

    @property
    def partial_fit(self):
        """Update the model with a single iteration over the given data.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            The input data.

        y : array-like, shape (n_samples,)
            The target values.

        Returns
        -------
        self : returns a trained MLP model.
        """
        if self.solver not in _STOCHASTIC_SOLVERS:
            raise AttributeError("partial_fit is only available for stochastic"
                                 " optimizers. %s is not stochastic."
                                 % self.solver)
        return self._partial_fit

    def _partial_fit(self, X, y):
        return self._fit(X, y, incremental=True)

    def _predict(self, X):
        """Predict using the trained model

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            The input data.

        Returns
        -------
        y_pred : array-like, shape (n_samples,) or (n_samples, n_outputs)
            The decision function of the samples for each class in the model.
        """
        X = check_array(X, accept_sparse=['csr', 'csc', 'coo'])

        # Make sure self.hidden_layer_sizes is a list
        hidden_layer_sizes = self.hidden_layer_sizes
        if not hasattr(hidden_layer_sizes, "__iter__"):
            hidden_layer_sizes = [hidden_layer_sizes]
        hidden_layer_sizes = list(hidden_layer_sizes)

        layer_units = [X.shape[1]] + hidden_layer_sizes + \
            [self.n_outputs_]

        # Initialize layers
        activations = [X]

        for i in range(self.n_layers_ - 1):
            activations.append(np.empty((X.shape[0],
                                         layer_units[i + 1])))
        # forward propagate
        self._forward_pass(activations)
        y_pred = activations[-1]

        return y_pred


class MLPClassifier(ClassifierMixin, BaseMultilayerPerceptron):
    """Multi-layer Perceptron classifier.

    This model optimizes the log-loss function using LBFGS or stochastic
    gradient descent.

    .. versionadded:: 0.18

    Parameters
    ----------
    hidden_layer_sizes : tuple, length = n_layers - 2, default (100,)
        The ith element represents the number of neurons in the ith
        hidden layer.

    activation : {'identity', 'logistic', 'tanh', 'relu'}, default 'relu'
        Activation function for the hidden layer.

        - 'identity', no-op activation, useful to implement linear bottleneck,
          returns f(x) = x

        - 'logistic', the logistic sigmoid function,
          returns f(x) = 1 / (1 + exp(-x)).

        - 'tanh', the hyperbolic tan function,
          returns f(x) = tanh(x).

        - 'relu', the rectified linear unit function,
          returns f(x) = max(0, x)

    solver : {'lbfgs', 'sgd', 'adam'}, default 'adam'
        The solver for weight optimization.

        - 'lbfgs' is an optimizer in the family of quasi-Newton methods.

        - 'sgd' refers to stochastic gradient descent.

        - 'adam' refers to a stochastic gradient-based optimizer proposed
          by Kingma, Diederik, and Jimmy Ba

        Note: The default solver 'adam' works pretty well on relatively
        large datasets (with thousands of training samples or more) in terms of
        both training time and validation score.
        For small datasets, however, 'lbfgs' can converge faster and perform
        better.

    alpha : float, optional, default 0.0001
        L2 penalty (regularization term) parameter.

    batch_size : int, optional, default 'auto'
        Size of minibatches for stochastic optimizers.
        If the solver is 'lbfgs', the classifier will not use minibatch.
        When set to "auto", `batch_size=min(200, n_samples)`

    learning_rate : {'constant', 'invscaling', 'adaptive'}, default 'constant'
        Learning rate schedule for weight updates.

        - 'constant' is a constant learning rate given by
          'learning_rate_init'.

        - 'invscaling' gradually decreases the learning rate at each
          time step 't' using an inverse scaling exponent of 'power_t'.
          effective_learning_rate = learning_rate_init / pow(t, power_t)

        - 'adaptive' keeps the learning rate constant to
          'learning_rate_init' as long as training loss keeps decreasing.
          Each time two consecutive epochs fail to decrease training loss by at
          least tol, or fail to increase validation score by at least tol if
          'early_stopping' is on, the current learning rate is divided by 5.

        Only used when ``solver='sgd'``.

    learning_rate_init : double, optional, default 0.001
        The initial learning rate used. It controls the step-size
        in updating the weights. Only used when solver='sgd' or 'adam'.

    power_t : double, optional, default 0.5
        The exponent for inverse scaling learning rate.
        It is used in updating effective learning rate when the learning_rate
        is set to 'invscaling'. Only used when solver='sgd'.

    max_iter : int, optional, default 200
        Maximum number of iterations. The solver iterates until convergence
        (determined by 'tol') or this number of iterations. For stochastic
        solvers ('sgd', 'adam'), note that this determines the number of epochs
        (how many times each data point will be used), not the number of
        gradient steps.

    shuffle : bool, optional, default True
        Whether to shuffle samples in each iteration. Only used when
        solver='sgd' or 'adam'.

    random_state : int, RandomState instance or None, optional, default None
        If int, random_state is the seed used by the random number generator;
        If RandomState instance, random_state is the random number generator;
        If None, the random number generator is the RandomState instance used
        by `np.random`.

    tol : float, optional, default 1e-4
        Tolerance for the optimization. When the loss or score is not improving
        by at least ``tol`` for ``n_iter_no_change`` consecutive iterations,
        unless ``learning_rate`` is set to 'adaptive', convergence is
        considered to be reached and training stops.

    verbose : bool, optional, default False
        Whether to print progress messages to stdout.

    warm_start : bool, optional, default False
        When set to True, reuse the solution of the previous
        call to fit as initialization, otherwise, just erase the
        previous solution. See :term:`the Glossary <warm_start>`.

    momentum : float, default 0.9
        Momentum for gradient descent update. Should be between 0 and 1. Only
        used when solver='sgd'.

    nesterovs_momentum : boolean, default True
        Whether to use Nesterov's momentum. Only used when solver='sgd' and
        momentum > 0.

    early_stopping : bool, default False
        Whether to use early stopping to terminate training when validation
        score is not improving. If set to true, it will automatically set
        aside 10% of training data as validation and terminate training when
        validation score is not improving by at least tol for
        ``n_iter_no_change`` consecutive epochs. The split is stratified,
        except in a multilabel setting.
        Only effective when solver='sgd' or 'adam'

    validation_fraction : float, optional, default 0.1
        The proportion of training data to set aside as validation set for
        early stopping. Must be between 0 and 1.
        Only used if early_stopping is True

    beta_1 : float, optional, default 0.9
        Exponential decay rate for estimates of first moment vector in adam,
        should be in [0, 1). Only used when solver='adam'

    beta_2 : float, optional, default 0.999
        Exponential decay rate for estimates of second moment vector in adam,
        should be in [0, 1). Only used when solver='adam'

    epsilon : float, optional, default 1e-8
        Value for numerical stability in adam. Only used when solver='adam'

    n_iter_no_change : int, optional, default 10
        Maximum number of epochs to not meet ``tol`` improvement.
        Only effective when solver='sgd' or 'adam'

        .. versionadded:: 0.20

    max_fun : int, optional, default 15000
        Only used when solver='lbfgs'. Maximum number of loss function calls.
        The solver iterates until convergence (determined by 'tol'), number
        of iterations reaches max_iter, or this number of loss function calls.
        Note that number of loss function calls will be greater than or equal
        to the number of iterations for the `MLPClassifier`.

        .. versionadded:: 0.22

    Attributes
    ----------
    classes_ : array or list of array of shape (n_classes,)
        Class labels for each output.

    loss_ : float
        The current loss computed with the loss function.

    coefs_ : list, length n_layers - 1
        The ith element in the list represents the weight matrix corresponding
        to layer i.

    intercepts_ : list, length n_layers - 1
        The ith element in the list represents the bias vector corresponding to
        layer i + 1.

    n_iter_ : int,
        The number of iterations the solver has ran.

    n_layers_ : int
        Number of layers.

    n_outputs_ : int
        Number of outputs.

    out_activation_ : string
        Name of the output activation function.


    Examples
    --------
    >>> from sklearn.neural_network import MLPClassifier
    >>> from sklearn.datasets import make_classification

    >>> X, y = make_classification(random_state=1)

    >>> clf = MLPClassifier(random_state=1, max_iter=300).fit(X, y)
    >>> clf.predict_proba(X[:2, :])
    array([[0.00925907, 0.99074093],
       [0.03290972, 0.96709028]])

    >>> clf.predict(X[:2, :])
    array([1, 1])

    >>> clf.score(X,y)
    1.0

    Notes
    -----
    MLPClassifier trains iteratively since at each time step
    the partial derivatives of the loss function with respect to the model
    parameters are computed to update the parameters.

    It can also have a regularization term added to the loss function
    that shrinks model parameters to prevent overfitting.

    This implementation works with data represented as dense numpy arrays or
    sparse scipy arrays of floating point values.

    References
    ----------
    Hinton, Geoffrey E.
        "Connectionist learning procedures." Artificial intelligence 40.1
        (1989): 185-234.

    Glorot, Xavier, and Yoshua Bengio. "Understanding the difficulty of
        training deep feedforward neural networks." International Conference
        on Artificial Intelligence and Statistics. 2010.

    He, Kaiming, et al. "Delving deep into rectifiers: Surpassing human-level
        performance on imagenet classification." arXiv preprint
        arXiv:1502.01852 (2015).

    Kingma, Diederik, and Jimmy Ba. "Adam: A method for stochastic
        optimization." arXiv preprint arXiv:1412.6980 (2014).
    """
    def __init__(self, hidden_layer_sizes=(100,), activation="relu",
                 solver='adam', alpha=0.0001,
                 batch_size='auto', learning_rate="constant",
                 learning_rate_init=0.001, power_t=0.5, max_iter=200,
                 shuffle=True, random_state=None, tol=1e-4,
                 verbose=False, warm_start=False, momentum=0.9,
                 nesterovs_momentum=True, early_stopping=False,
                 validation_fraction=0.1, beta_1=0.9, beta_2=0.999,
                 epsilon=1e-8, n_iter_no_change=10, max_fun=15000):
        super().__init__(
            hidden_layer_sizes=hidden_layer_sizes,
            activation=activation, solver=solver, alpha=alpha,
            batch_size=batch_size, learning_rate=learning_rate,
            learning_rate_init=learning_rate_init, power_t=power_t,
            max_iter=max_iter, loss='log_loss', shuffle=shuffle,
            random_state=random_state, tol=tol, verbose=verbose,
            warm_start=warm_start, momentum=momentum,
            nesterovs_momentum=nesterovs_momentum,
            early_stopping=early_stopping,
            validation_fraction=validation_fraction,
            beta_1=beta_1, beta_2=beta_2, epsilon=epsilon,
            n_iter_no_change=n_iter_no_change, max_fun=max_fun)

    def _validate_input(self, X, y, incremental):
        X, y = check_X_y(X, y, accept_sparse=['csr', 'csc', 'coo'],
                         multi_output=True)
        if y.ndim == 2 and y.shape[1] == 1:
            y = column_or_1d(y, warn=True)

        if not incremental:
            self._label_binarizer = LabelBinarizer()
            self._label_binarizer.fit(y)
            self.classes_ = self._label_binarizer.classes_
        elif self.warm_start:
            classes = unique_labels(y)
            if set(classes) != set(self.classes_):
                raise ValueError("warm_start can only be used where `y` has "
                                 "the same classes as in the previous "
                                 "call to fit. Previously got %s, `y` has %s" %
                                 (self.classes_, classes))
        else:
            classes = unique_labels(y)
            if len(np.setdiff1d(classes, self.classes_, assume_unique=True)):
                raise ValueError("`y` has classes not in `self.classes_`."
                                 " `self.classes_` has %s. 'y' has %s." %
                                 (self.classes_, classes))

        y = self._label_binarizer.transform(y)
        return X, y

    def predict(self, X):
        """Predict using the multi-layer perceptron classifier

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            The input data.

        Returns
        -------
        y : array-like, shape (n_samples,) or (n_samples, n_classes)
            The predicted classes.
        """
        check_is_fitted(self)
        y_pred = self._predict(X)

        if self.n_outputs_ == 1:
            y_pred = y_pred.ravel()

        return self._label_binarizer.inverse_transform(y_pred)

    def fit(self, X, y):
        """Fit the model to data matrix X and target(s) y.

        Parameters
        ----------
        X : array-like or sparse matrix, shape (n_samples, n_features)
            The input data.

        y : array-like, shape (n_samples,) or (n_samples, n_outputs)
            The target values (class labels in classification, real numbers in
            regression).

        Returns
        -------
        self : returns a trained MLP model.
        """
        return self._fit(X, y, incremental=(self.warm_start and
                                            hasattr(self, "classes_")))

    @property
    def partial_fit(self):
        """Update the model with a single iteration over the given data.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            The input data.

        y : array-like, shape (n_samples,)
            The target values.

        classes : array, shape (n_classes), default None
            Classes across all calls to partial_fit.
            Can be obtained via `np.unique(y_all)`, where y_all is the
            target vector of the entire dataset.
            This argument is required for the first call to partial_fit
            and can be omitted in the subsequent calls.
            Note that y doesn't need to contain all labels in `classes`.

        Returns
        -------
        self : returns a trained MLP model.
        """
        if self.solver not in _STOCHASTIC_SOLVERS:
            raise AttributeError("partial_fit is only available for stochastic"
                                 " optimizer. %s is not stochastic"
                                 % self.solver)
        return self._partial_fit

    def _partial_fit(self, X, y, classes=None):
        if _check_partial_fit_first_call(self, classes):
            self._label_binarizer = LabelBinarizer()
            if type_of_target(y).startswith('multilabel'):
                self._label_binarizer.fit(y)
            else:
                self._label_binarizer.fit(classes)

        super()._partial_fit(X, y)

        return self

    def predict_log_proba(self, X):
        """Return the log of probability estimates.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            The input data.

        Returns
        -------
        log_y_prob : array-like, shape (n_samples, n_classes)
            The predicted log-probability of the sample for each class
            in the model, where classes are ordered as they are in
            `self.classes_`. Equivalent to log(predict_proba(X))
        """
        y_prob = self.predict_proba(X)
        return np.log(y_prob, out=y_prob)

    def predict_proba(self, X):
        """Probability estimates.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            The input data.

        Returns
        -------
        y_prob : array-like, shape (n_samples, n_classes)
            The predicted probability of the sample for each class in the
            model, where classes are ordered as they are in `self.classes_`.
        """
        check_is_fitted(self)
        y_pred = self._predict(X)

        if self.n_outputs_ == 1:
            y_pred = y_pred.ravel()

        if y_pred.ndim == 1:
            return np.vstack([1 - y_pred, y_pred]).T
        else:
            return y_pred


class MLPRegressor(RegressorMixin, BaseMultilayerPerceptron):
    """Multi-layer Perceptron regressor.

    This model optimizes the squared-loss using LBFGS or stochastic gradient
    descent.

    .. versionadded:: 0.18

    Parameters
    ----------
    hidden_layer_sizes : tuple, length = n_layers - 2, default (100,)
        The ith element represents the number of neurons in the ith
        hidden layer.

    activation : {'identity', 'logistic', 'tanh', 'relu'}, default 'relu'
        Activation function for the hidden layer.

        - 'identity', no-op activation, useful to implement linear bottleneck,
          returns f(x) = x

        - 'logistic', the logistic sigmoid function,
          returns f(x) = 1 / (1 + exp(-x)).

        - 'tanh', the hyperbolic tan function,
          returns f(x) = tanh(x).

        - 'relu', the rectified linear unit function,
          returns f(x) = max(0, x)

    solver : {'lbfgs', 'sgd', 'adam'}, default 'adam'
        The solver for weight optimization.

        - 'lbfgs' is an optimizer in the family of quasi-Newton methods.

        - 'sgd' refers to stochastic gradient descent.

        - 'adam' refers to a stochastic gradient-based optimizer proposed by
          Kingma, Diederik, and Jimmy Ba

        Note: The default solver 'adam' works pretty well on relatively
        large datasets (with thousands of training samples or more) in terms of
        both training time and validation score.
        For small datasets, however, 'lbfgs' can converge faster and perform
        better.

    alpha : float, optional, default 0.0001
        L2 penalty (regularization term) parameter.

    batch_size : int, optional, default 'auto'
        Size of minibatches for stochastic optimizers.
        If the solver is 'lbfgs', the classifier will not use minibatch.
        When set to "auto", `batch_size=min(200, n_samples)`

    learning_rate : {'constant', 'invscaling', 'adaptive'}, default 'constant'
        Learning rate schedule for weight updates.

        - 'constant' is a constant learning rate given by
          'learning_rate_init'.

        - 'invscaling' gradually decreases the learning rate ``learning_rate_``
          at each time step 't' using an inverse scaling exponent of 'power_t'.
          effective_learning_rate = learning_rate_init / pow(t, power_t)

        - 'adaptive' keeps the learning rate constant to
          'learning_rate_init' as long as training loss keeps decreasing.
          Each time two consecutive epochs fail to decrease training loss by at
          least tol, or fail to increase validation score by at least tol if
          'early_stopping' is on, the current learning rate is divided by 5.

        Only used when solver='sgd'.

    learning_rate_init : double, optional, default 0.001
        The initial learning rate used. It controls the step-size
        in updating the weights. Only used when solver='sgd' or 'adam'.

    power_t : double, optional, default 0.5
        The exponent for inverse scaling learning rate.
        It is used in updating effective learning rate when the learning_rate
        is set to 'invscaling'. Only used when solver='sgd'.

    max_iter : int, optional, default 200
        Maximum number of iterations. The solver iterates until convergence
        (determined by 'tol') or this number of iterations. For stochastic
        solvers ('sgd', 'adam'), note that this determines the number of epochs
        (how many times each data point will be used), not the number of
        gradient steps.

    shuffle : bool, optional, default True
        Whether to shuffle samples in each iteration. Only used when
        solver='sgd' or 'adam'.

    random_state : int, RandomState instance or None, optional, default None
        If int, random_state is the seed used by the random number generator;
        If RandomState instance, random_state is the random number generator;
        If None, the random number generator is the RandomState instance used
        by `np.random`.

    tol : float, optional, default 1e-4
        Tolerance for the optimization. When the loss or score is not improving
        by at least ``tol`` for ``n_iter_no_change`` consecutive iterations,
        unless ``learning_rate`` is set to 'adaptive', convergence is
        considered to be reached and training stops.

    verbose : bool, optional, default False
        Whether to print progress messages to stdout.

    warm_start : bool, optional, default False
        When set to True, reuse the solution of the previous
        call to fit as initialization, otherwise, just erase the
        previous solution. See :term:`the Glossary <warm_start>`.

    momentum : float, default 0.9
        Momentum for gradient descent update.  Should be between 0 and 1. Only
        used when solver='sgd'.

    nesterovs_momentum : boolean, default True
        Whether to use Nesterov's momentum. Only used when solver='sgd' and
        momentum > 0.

    early_stopping : bool, default False
        Whether to use early stopping to terminate training when validation
        score is not improving. If set to true, it will automatically set
        aside 10% of training data as validation and terminate training when
        validation score is not improving by at least ``tol`` for
        ``n_iter_no_change`` consecutive epochs.
        Only effective when solver='sgd' or 'adam'

    validation_fraction : float, optional, default 0.1
        The proportion of training data to set aside as validation set for
        early stopping. Must be between 0 and 1.
        Only used if early_stopping is True

    beta_1 : float, optional, default 0.9
        Exponential decay rate for estimates of first moment vector in adam,
        should be in [0, 1). Only used when solver='adam'

    beta_2 : float, optional, default 0.999
        Exponential decay rate for estimates of second moment vector in adam,
        should be in [0, 1). Only used when solver='adam'

    epsilon : float, optional, default 1e-8
        Value for numerical stability in adam. Only used when solver='adam'

    n_iter_no_change : int, optional, default 10
        Maximum number of epochs to not meet ``tol`` improvement.
        Only effective when solver='sgd' or 'adam'

        .. versionadded:: 0.20

    max_fun : int, optional, default 15000
        Only used when solver='lbfgs'. Maximum number of function calls.
        The solver iterates until convergence (determined by 'tol'), number
        of iterations reaches max_iter, or this number of function calls.
        Note that number of function calls will be greater than or equal to
        the number of iterations for the MLPRegressor.

        .. versionadded:: 0.22

    Attributes
    ----------
    loss_ : float
        The current loss computed with the loss function.

    coefs_ : list, length n_layers - 1
        The ith element in the list represents the weight matrix corresponding
        to layer i.

    intercepts_ : list, length n_layers - 1
        The ith element in the list represents the bias vector corresponding to
        layer i + 1.

    n_iter_ : int,
        The number of iterations the solver has ran.

    n_layers_ : int
        Number of layers.

    n_outputs_ : int
        Number of outputs.

    out_activation_ : string
        Name of the output activation function.

    Examples
    --------
    >>> from sklearn.neural_network import MLPRegressor
    >>> from sklearn.datasets import make_regression

    >>> X, y = make_regression(random_state=1)

    >>> clf = MLPRegressor(random_state=1,max_iter=1000).fit(X[0:20], y[0:20])
    >>> clf.predict(X[:2,:])
    array([ 120.36246143, -264.13833921])

    >>> clf.score(X,y)
    0.42937...

    Notes
    -----
    MLPRegressor trains iteratively since at each time step
    the partial derivatives of the loss function with respect to the model
    parameters are computed to update the parameters.

    It can also have a regularization term added to the loss function
    that shrinks model parameters to prevent overfitting.

    This implementation works with data represented as dense and sparse numpy
    arrays of floating point values.

    References
    ----------
    Hinton, Geoffrey E.
        "Connectionist learning procedures." Artificial intelligence 40.1
        (1989): 185-234.

    Glorot, Xavier, and Yoshua Bengio. "Understanding the difficulty of
        training deep feedforward neural networks." International Conference
        on Artificial Intelligence and Statistics. 2010.

    He, Kaiming, et al. "Delving deep into rectifiers: Surpassing human-level
        performance on imagenet classification." arXiv preprint
        arXiv:1502.01852 (2015).

    Kingma, Diederik, and Jimmy Ba. "Adam: A method for stochastic
        optimization." arXiv preprint arXiv:1412.6980 (2014).
    """
    def __init__(self, hidden_layer_sizes=(100,), activation="relu",
                 solver='adam', alpha=0.0001,
                 batch_size='auto', learning_rate="constant",
                 learning_rate_init=0.001,
                 power_t=0.5, max_iter=200, shuffle=True,
                 random_state=None, tol=1e-4,
                 verbose=False, warm_start=False, momentum=0.9,
                 nesterovs_momentum=True, early_stopping=False,
                 validation_fraction=0.1, beta_1=0.9, beta_2=0.999,
                 epsilon=1e-8, n_iter_no_change=10, max_fun=15000):
        super().__init__(
            hidden_layer_sizes=hidden_layer_sizes,
            activation=activation, solver=solver, alpha=alpha,
            batch_size=batch_size, learning_rate=learning_rate,
            learning_rate_init=learning_rate_init, power_t=power_t,
            max_iter=max_iter, loss='squared_loss', shuffle=shuffle,
            random_state=random_state, tol=tol, verbose=verbose,
            warm_start=warm_start, momentum=momentum,
            nesterovs_momentum=nesterovs_momentum,
            early_stopping=early_stopping,
            validation_fraction=validation_fraction,
            beta_1=beta_1, beta_2=beta_2, epsilon=epsilon,
            n_iter_no_change=n_iter_no_change, max_fun=max_fun)

    def predict(self, X):
        """Predict using the multi-layer perceptron model.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            The input data.

        Returns
        -------
        y : array-like, shape (n_samples, n_outputs)
            The predicted values.
        """
        check_is_fitted(self)
        y_pred = self._predict(X)
        if y_pred.shape[1] == 1:
            return y_pred.ravel()
        return y_pred

    def _validate_input(self, X, y, incremental):
        X, y = check_X_y(X, y, accept_sparse=['csr', 'csc', 'coo'],
                         multi_output=True, y_numeric=True)
        if y.ndim == 2 and y.shape[1] == 1:
            y = column_or_1d(y, warn=True)
        return X, y
