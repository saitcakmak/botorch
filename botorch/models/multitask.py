#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

r"""
Multi-Task GP models.

References

.. [Doucet2010sampl]
    A. Doucet. A Note on Efficient Conditional Simulation of Gaussian Distributions.
    http://www.stats.ox.ac.uk/~doucet/doucet_simulationconditionalgaussian.pdf,
    Apr 2010.

.. [Maddox2021bohdo]
    W. Maddox, M. Balandat, A. Wilson, and E. Bakshy. Bayesian Optimization with
    High-Dimensional Outputs. https://arxiv.org/abs/2106.12997, Jun 2021.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple, Union

import torch
from botorch.acquisition.objective import PosteriorTransform
from botorch.models.gp_regression import MIN_INFERRED_NOISE_LEVEL
from botorch.models.gpytorch import GPyTorchModel
from botorch.models.gpytorch import MultiTaskGPyTorchModel
from botorch.models.transforms.input import InputTransform
from botorch.models.transforms.outcome import OutcomeTransform
from botorch.posteriors.multitask import MultitaskGPPosterior
from botorch.utils.containers import TrainingData
from gpytorch.constraints import GreaterThan
from gpytorch.distributions.multitask_multivariate_normal import (
    MultitaskMultivariateNormal,
)
from gpytorch.distributions.multivariate_normal import MultivariateNormal
from gpytorch.kernels.index_kernel import IndexKernel
from gpytorch.kernels.matern_kernel import MaternKernel
from gpytorch.kernels.multitask_kernel import MultitaskKernel
from gpytorch.kernels.scale_kernel import ScaleKernel
from gpytorch.lazy import (
    BatchRepeatLazyTensor,
    DiagLazyTensor,
    KroneckerProductLazyTensor,
    KroneckerProductDiagLazyTensor,
    lazify,
    RootLazyTensor,
)
from gpytorch.likelihoods.gaussian_likelihood import (
    FixedNoiseGaussianLikelihood,
    GaussianLikelihood,
)
from gpytorch.likelihoods.multitask_gaussian_likelihood import (
    MultitaskGaussianLikelihood,
)
from gpytorch.means import MultitaskMean
from gpytorch.means.constant_mean import ConstantMean
from gpytorch.models.exact_gp import ExactGP
from gpytorch.priors.lkj_prior import LKJCovariancePrior
from gpytorch.priors.prior import Prior
from gpytorch.priors.smoothed_box_prior import SmoothedBoxPrior
from gpytorch.priors.torch_priors import GammaPrior
from gpytorch.settings import cholesky_jitter, detach_test_caches
from gpytorch.utils.errors import CachingError
from gpytorch.utils.memoize import cached, pop_from_cache
from torch import Tensor


class MultiTaskGP(ExactGP, MultiTaskGPyTorchModel):
    r"""Multi-Task GP model using an ICM kernel, inferring observation noise.

    Multi-task exact GP that uses a simple ICM kernel. Can be single-output or
    multi-output. This model uses relatively strong priors on the base Kernel
    hyperparameters, which work best when covariates are normalized to the unit
    cube and outcomes are standardized (zero mean, unit variance).

    This model infers the noise level. WARNING: It currently does not support
    different noise levels for the different tasks. If you have known observation
    noise, please use `FixedNoiseMultiTaskGP` instead.
    """

    def __init__(
        self,
        train_X: Tensor,
        train_Y: Tensor,
        task_feature: int,
        task_covar_prior: Optional[Prior] = None,
        output_tasks: Optional[List[int]] = None,
        rank: Optional[int] = None,
        input_transform: Optional[InputTransform] = None,
        outcome_transform: Optional[OutcomeTransform] = None,
    ) -> None:
        r"""Multi-Task GP model using an ICM kernel, inferring observation noise.

        Args:
            train_X: A `n x (d + 1)` or `b x n x (d + 1)` (batch mode) tensor
                of training data. One of the columns should contain the task
                features (see `task_feature` argument).
            train_Y: A `n` or `b x n` (batch mode) tensor of training observations.
            task_feature: The index of the task feature (`-d <= task_feature <= d`).
            output_tasks: A list of task indices for which to compute model
                outputs for. If omitted, return outputs for all task indices.
            rank: The rank to be used for the index kernel. If omitted, use a
                full rank (i.e. number of tasks) kernel.
            task_covar_prior : A Prior on the task covariance matrix. Must operate
                on p.s.d. matrices. A common prior for this is the `LKJ` prior.
            input_transform: An input transform that is applied in the model's
                forward pass.

        Example:
            >>> X1, X2 = torch.rand(10, 2), torch.rand(20, 2)
            >>> i1, i2 = torch.zeros(10, 1), torch.ones(20, 1)
            >>> train_X = torch.cat([
            >>>     torch.cat([X1, i1], -1), torch.cat([X2, i2], -1),
            >>> ])
            >>> train_Y = torch.cat(f1(X1), f2(X2)).unsqueeze(-1)
            >>> model = MultiTaskGP(train_X, train_Y, task_feature=-1)
        """
        with torch.no_grad():
            transformed_X = self.transform_inputs(
                X=train_X, input_transform=input_transform
            )
        self._validate_tensor_args(X=transformed_X, Y=train_Y)
        all_tasks, task_feature, d = self.get_all_tasks(
            transformed_X, task_feature, output_tasks
        )
        if outcome_transform is not None:
            train_Y, _ = outcome_transform(train_Y)

        # squeeze output dim
        train_Y = train_Y.squeeze(-1)
        if output_tasks is None:
            output_tasks = all_tasks
        else:
            if set(output_tasks) - set(all_tasks):
                raise RuntimeError("All output tasks must be present in input data.")
        self._output_tasks = output_tasks
        self._num_outputs = len(output_tasks)

        # TODO (T41270962): Support task-specific noise levels in likelihood
        likelihood = GaussianLikelihood(noise_prior=GammaPrior(1.1, 0.05))

        # construct indexer to be used in forward
        self._task_feature = task_feature
        self._base_idxr = torch.arange(d)
        self._base_idxr[task_feature:] += 1  # exclude task feature

        super().__init__(
            train_inputs=train_X, train_targets=train_Y, likelihood=likelihood
        )
        self.mean_module = ConstantMean()
        self.covar_module = ScaleKernel(
            base_kernel=MaternKernel(
                nu=2.5, ard_num_dims=d, lengthscale_prior=GammaPrior(3.0, 6.0)
            ),
            outputscale_prior=GammaPrior(2.0, 0.15),
        )
        num_tasks = len(all_tasks)
        self._rank = rank if rank is not None else num_tasks

        self.task_covar_module = IndexKernel(
            num_tasks=num_tasks, rank=self._rank, prior=task_covar_prior
        )
        if input_transform is not None:
            self.input_transform = input_transform
        if outcome_transform is not None:
            self.outcome_transform = outcome_transform
        self.to(train_X)

    def _split_inputs(self, x: Tensor) -> Tuple[Tensor, Tensor]:
        r"""Extracts base features and task indices from input data.

        Args:
            x: The full input tensor with trailing dimension of size `d + 1`.
                Should be of float/double data type.

        Returns:
            2-element tuple containing

            - A `q x d` or `b x q x d` (batch mode) tensor with trailing
            dimension made up of the `d` non-task-index columns of `x`, arranged
            in the order as specified by the indexer generated during model
            instantiation.
            - A `q` or `b x q` (batch mode) tensor of long data type containing
            the task indices.
        """
        batch_shape, d = x.shape[:-2], x.shape[-1]
        x_basic = x[..., self._base_idxr].view(batch_shape + torch.Size([-1, d - 1]))
        task_idcs = (
            x[..., self._task_feature]
            .view(batch_shape + torch.Size([-1, 1]))
            .to(dtype=torch.long)
        )
        return x_basic, task_idcs

    def forward(self, x: Tensor) -> MultivariateNormal:
        if self.training:
            x = self.transform_inputs(x)
        x_basic, task_idcs = self._split_inputs(x)
        # Compute base mean and covariance
        mean_x = self.mean_module(x_basic)
        covar_x = self.covar_module(x_basic)
        # Compute task covariances
        covar_i = self.task_covar_module(task_idcs)
        # Combine the two in an ICM fashion
        covar = covar_x.mul(covar_i)
        return MultivariateNormal(mean_x, covar)

    @classmethod
    def get_all_tasks(
        cls,
        train_X: Tensor,
        task_feature: int,
        output_tasks: Optional[List[int]] = None,
    ) -> Tuple[List[int], int, int]:
        if train_X.ndim != 2:
            # Currently, batch mode MTGPs are blocked upstream in GPyTorch
            raise ValueError(f"Unsupported shape {train_X.shape} for train_X.")
        d = train_X.shape[-1] - 1
        if not (-d <= task_feature <= d):
            raise ValueError(f"Must have that -{d} <= task_feature <= {d}")
        task_feature = task_feature % (d + 1)
        all_tasks = train_X[:, task_feature].unique().to(dtype=torch.long).tolist()
        return all_tasks, task_feature, d

    @classmethod
    def construct_inputs(cls, training_data: TrainingData, **kwargs) -> Dict[str, Any]:
        r"""Construct kwargs for the `Model` from `TrainingData` and other options.

        Args:
            training_data: `TrainingData` container with data for single outcome
                or for multiple outcomes for batched multi-output case.
            **kwargs: Additional options for the model that pertain to the
                training data, including:

                - `task_features`: Indices of the input columns containing the task
                  features (expected list of length 1),
                - `task_covar_prior`: A GPyTorch `Prior` object to use as prior on
                  the cross-task covariance matrix,
                - `prior_config`: A dict representing a prior config, should only be
                  used if `prior` is not passed directly. Should contain:
                  `use_LKJ_prior` (whether to use LKJ prior) and `eta` (eta value,
                  float),
                - `rank`: The rank of the cross-task covariance matrix.
        """

        task_features = kwargs.pop("task_features", None)
        if task_features is None:
            raise ValueError(f"`task_features` required for {cls.__name__}.")
        task_feature = task_features[0]
        inputs = {
            "train_X": training_data.X,
            "train_Y": training_data.Y,
            "task_feature": task_feature,
            "rank": kwargs.get("rank"),
        }

        prior = kwargs.get("task_covar_prior")
        prior_config = kwargs.get("prior_config")
        if prior and prior_config:
            raise ValueError(
                "Only one of `prior` and `prior_config` arguments expected."
            )

        if prior_config:
            if not prior_config.get("use_LKJ_prior"):
                raise ValueError("Currently only config for LKJ prior is supported.")
            all_tasks, _, _ = MultiTaskGP.get_all_tasks(training_data.X, task_feature)
            num_tasks = len(all_tasks)
            sd_prior = GammaPrior(1.0, 0.15)
            sd_prior._event_shape = torch.Size([num_tasks])
            eta = prior_config.get("eta", 0.5)
            if not isinstance(eta, float) and not isinstance(eta, int):
                raise ValueError(f"eta must be a real number, your eta was {eta}.")
            prior = LKJCovariancePrior(num_tasks, eta, sd_prior)

        inputs["task_covar_prior"] = prior
        return inputs


class FixedNoiseMultiTaskGP(MultiTaskGP):
    r"""Multi-Task GP model using an ICM kernel, with known observation noise.

    Multi-task exact GP that uses a simple ICM kernel. Can be single-output or
    multi-output. This model uses relatively strong priors on the base Kernel
    hyperparameters, which work best when covariates are normalized to the unit
    cube and outcomes are standardized (zero mean, unit variance).

    This model requires observation noise data (specified in `train_Yvar`).
    """

    def __init__(
        self,
        train_X: Tensor,
        train_Y: Tensor,
        train_Yvar: Tensor,
        task_feature: int,
        task_covar_prior: Optional[Prior] = None,
        output_tasks: Optional[List[int]] = None,
        rank: Optional[int] = None,
        input_transform: Optional[InputTransform] = None,
    ) -> None:
        r"""Multi-Task GP model using an ICM kernel and known observation noise.

        Args:
            train_X: A `n x (d + 1)` or `b x n x (d + 1)` (batch mode) tensor
                of training data. One of the columns should contain the task
                features (see `task_feature` argument).
            train_Y: A `n` or `b x n` (batch mode) tensor of training
                observations.
            train_Yvar: A `n` or `b x n` (batch mode) tensor of observation
                noise standard errors.
            task_feature: The index of the task feature (`-d <= task_feature <= d`).
            task_covar_prior : A Prior on the task covariance matrix. Must operate
                on p.s.d. matrices. A common prior for this is the `LKJ` prior.
            output_tasks: A list of task indices for which to compute model
                outputs for. If omitted, return outputs for all task indices.
            rank: The rank to be used for the index kernel. If omitted, use a
                full rank (i.e. number of tasks) kernel.
            input_transform: An input transform that is applied in the model's
                forward pass.

        Example:
            >>> X1, X2 = torch.rand(10, 2), torch.rand(20, 2)
            >>> i1, i2 = torch.zeros(10, 1), torch.ones(20, 1)
            >>> train_X = torch.cat([
            >>>     torch.cat([X1, i1], -1), torch.cat([X2, i2], -1),
            >>> ], dim=0)
            >>> train_Y = torch.cat(f1(X1), f2(X2))
            >>> train_Yvar = 0.1 + 0.1 * torch.rand_like(train_Y)
            >>> model = FixedNoiseMultiTaskGP(train_X, train_Y, train_Yvar, -1)
        """
        with torch.no_grad():
            transformed_X = self.transform_inputs(
                X=train_X, input_transform=input_transform
            )
        self._validate_tensor_args(X=transformed_X, Y=train_Y, Yvar=train_Yvar)
        # We'll instatiate a MultiTaskGP and simply override the likelihood
        super().__init__(
            train_X=train_X,
            train_Y=train_Y,
            task_feature=task_feature,
            output_tasks=output_tasks,
            rank=rank,
            task_covar_prior=task_covar_prior,
            input_transform=input_transform,
        )
        self.likelihood = FixedNoiseGaussianLikelihood(noise=train_Yvar.squeeze(-1))
        self.to(train_X)

    @classmethod
    def construct_inputs(cls, training_data: TrainingData, **kwargs) -> Dict[str, Any]:
        r"""Construct kwargs for the `Model` from `TrainingData` and other options.

        Args:
            training_data: `TrainingData` container with data for single outcome
                or for multiple outcomes for batched multi-output case.
            **kwargs: Additional options for the model that pertain to the
                training data, including:

                - `task_features`: Indices of the input columns containing the task
                  features (expected list of length 1),
                - `task_covar_prior`: A GPyTorch `Prior` object to use as prior on
                  the cross-task covariance matrix,
                - `prior_config`: A dict representing a prior config, should only be
                  used if `prior` is not passed directly. Should contain:
                  use_LKJ_prior` (whether to use LKJ prior) and `eta` (eta value,
                  float),
                - `rank`: The rank of the cross-task covariance matrix.
        """
        if training_data.Yvar is None:
            raise ValueError(f"Yvar required for {cls.__name__}.")

        inputs = super().construct_inputs(training_data=training_data, **kwargs)
        inputs["train_Yvar"] = training_data.Yvar
        return inputs


class KroneckerMultiTaskGP(ExactGP, GPyTorchModel):
    """Multi-task GP with Kronecker structure, using an ICM kernel.

    This model assumes the "block design" case, i.e., it requires that all tasks
    are observed at all data points.

    For posterior sampling, this model uses Matheron's rule [Doucet2010sampl] to compute
    the posterior over all tasks as in [Maddox2021bohdo] by exploiting Kronecker
    structure.
    """

    def __init__(
        self,
        train_X: Tensor,
        train_Y: Tensor,
        likelihood: Optional[MultitaskGaussianLikelihood] = None,
        task_covar_prior: Optional[Prior] = None,
        rank: Optional[int] = None,
        input_transform: Optional[InputTransform] = None,
        outcome_transform: Optional[OutcomeTransform] = None,
        **kwargs: Any,
    ) -> None:
        r"""Multi-task GP with Kronecker structure, using a simple ICM kernel.

        Args:
            train_X: A `batch_shape x n x d` tensor of training features.
            train_Y: A `batch_shape x n x m` tensor of training observations.
            likelihood: A `MultitaskGaussianLikelihood`. If omitted, uses a
                `MultitaskGaussianLikelihood` with a `GammaPrior(1.1, 0.05)`
                noise prior.
            task_covar_prior : A Prior on the task covariance matrix. Must operate
                on p.s.d. matrices. A common prior for this is the `LKJ` prior. If
                omitted, uses `LKJCovariancePrior` with `eta` parameter as specified
                in the keyword arguments (if not specified, use `eta=1.5`).
            rank: The rank of the ICM kernel. If omitted, use a full rank kernel.
            kwargs: Additional arguments to override default settings of priors,
                including:
                - eta: The eta parameter on the default LKJ task_covar_prior.
                A value of 1.0 is uninformative, values <1.0 favor stronger
                correlations (in magnitude), correlations vanish as eta -> inf.
                - sd_prior: A scalar prior over nonnegative numbers, which is used
                for the default LKJCovariancePrior task_covar_prior.
                - likelihood_rank: The rank of the task covariance matrix to fit.
                Defaults to 0 (which corresponds to a diagonal covariance matrix).

        Example:
            >>> train_X = torch.rand(10, 2)
            >>> train_Y = torch.cat([f_1(X), f_2(X)], dim=-1)
            >>> model = KroneckerMultiTaskGP(train_X, train_Y)
        """
        with torch.no_grad():
            transformed_X = self.transform_inputs(
                X=train_X, input_transform=input_transform
            )
        if outcome_transform is not None:
            train_Y, _ = outcome_transform(train_Y)

        self._validate_tensor_args(X=transformed_X, Y=train_Y)
        self._num_outputs = train_Y.shape[-1]
        batch_shape, ard_num_dims = train_X.shape[:-2], train_X.shape[-1]
        num_tasks = train_Y.shape[-1]

        if rank is None:
            rank = num_tasks
        if likelihood is None:
            noise_prior = GammaPrior(1.1, 0.05)
            noise_prior_mode = (noise_prior.concentration - 1) / noise_prior.rate
            likelihood = MultitaskGaussianLikelihood(
                num_tasks=num_tasks,
                batch_shape=batch_shape,
                noise_prior=noise_prior,
                noise_constraint=GreaterThan(
                    MIN_INFERRED_NOISE_LEVEL,
                    transform=None,
                    initial_value=noise_prior_mode,
                ),
                rank=kwargs.get("likelihood_rank", 0),
            )
        if task_covar_prior is None:
            task_covar_prior = LKJCovariancePrior(
                n=num_tasks,
                eta=kwargs.get("eta", 1.5),
                sd_prior=kwargs.get(
                    "sd_prior",
                    SmoothedBoxPrior(math.exp(-6), math.exp(1.25), 0.05),
                ),
            )
        super().__init__(train_X, train_Y, likelihood)
        self.mean_module = MultitaskMean(
            base_means=ConstantMean(batch_shape=batch_shape), num_tasks=num_tasks
        )
        self.covar_module = MultitaskKernel(
            data_covar_module=MaternKernel(
                nu=2.5,
                ard_num_dims=ard_num_dims,
                lengthscale_prior=GammaPrior(3.0, 6.0),
                batch_shape=batch_shape,
            ),
            num_tasks=num_tasks,
            rank=rank,
            batch_shape=batch_shape,
            task_covar_prior=task_covar_prior,
        )

        if outcome_transform is not None:
            self.outcome_transform = outcome_transform
        if input_transform is not None:
            self.input_transform = input_transform
        self.to(train_X)

    def forward(self, X: Tensor) -> MultitaskMultivariateNormal:
        if self.training:
            X = self.transform_inputs(X)

        mean_x = self.mean_module(X)
        covar_x = self.covar_module(X)
        return MultitaskMultivariateNormal(mean_x, covar_x)

    @property
    def _task_covar_matrix(self):
        res = self.covar_module.task_covar_module.covar_matrix
        if detach_test_caches.on():
            res = res.detach()
        return res

    @property
    @cached(name="train_full_covar")
    def train_full_covar(self):
        train_x = self.transform_inputs(self.train_inputs[0])

        # construct Kxx \otimes Ktt
        train_full_covar = self.covar_module(train_x).evaluate_kernel()
        if detach_test_caches.on():
            train_full_covar = train_full_covar.detach()
        return train_full_covar

    @property
    @cached(name="predictive_mean_cache")
    def predictive_mean_cache(self):
        train_x = self.transform_inputs(self.train_inputs[0])
        train_noise = self.likelihood._shaped_noise_covar(train_x.shape)
        if detach_test_caches.on():
            train_noise = train_noise.detach()

        train_diff = self.train_targets - self.mean_module(train_x)
        train_solve = (self.train_full_covar + train_noise).inv_matmul(
            train_diff.reshape(*train_diff.shape[:-2], -1)
        )
        if detach_test_caches.on():
            train_solve = train_solve.detach()

        return train_solve

    def posterior(
        self,
        X: Tensor,
        output_indices: Optional[List[int]] = None,
        observation_noise: Union[bool, Tensor] = False,
        posterior_transform: Optional[PosteriorTransform] = None,
        **kwargs: Any,
    ) -> MultitaskGPPosterior:
        self.eval()

        X = self.transform_inputs(X)
        train_x = self.transform_inputs(self.train_inputs[0])

        # construct Ktt
        task_covar = self._task_covar_matrix
        task_rootlt = self._task_covar_matrix.root_decomposition(
            method="diagonalization"
        )
        task_root = task_rootlt.root
        if task_covar.batch_shape != X.shape[:-2]:
            task_covar = BatchRepeatLazyTensor(task_covar, batch_repeat=X.shape[:-2])
            task_root = BatchRepeatLazyTensor(
                lazify(task_root), batch_repeat=X.shape[:-2]
            )

        task_covar_rootlt = RootLazyTensor(task_root)

        # construct RR' \approx Kxx
        data_data_covar = self.train_full_covar.lazy_tensors[0]
        # populate the diagonalziation caches for the root and inverse root
        # decomposition
        data_data_evals, data_data_evecs = data_data_covar.diagonalization()

        # construct K_{xt, x}
        test_data_covar = self.covar_module.data_covar_module(X, train_x)
        # construct K_{xt, xt}
        test_test_covar = self.covar_module.data_covar_module(X)

        # now update root so that \tilde{R}\tilde{R}' \approx K_{(x,xt), (x,xt)}
        # cloning preserves the gradient history
        with cholesky_jitter(cholesky_jitter.value(X.dtype)):
            updated_lazy_tensor = data_data_covar.cat_rows(
                cross_mat=test_data_covar.clone(),
                new_mat=test_test_covar,
                method="diagonalization",
            )
            updated_root = updated_lazy_tensor.root_decomposition().root
            # occasionally, there's device errors so enforce this comes out right
            updated_root = updated_root.to(data_data_covar.device)

        # build a root decomposition of the joint train/test covariance matrix
        # construct (\tilde{R} \otimes M)(\tilde{R} \otimes M)' \approx
        # (K_{(x,xt), (x,xt)} \otimes Ktt)
        joint_covar = RootLazyTensor(
            KroneckerProductLazyTensor(updated_root, task_covar_rootlt.root.detach())
        )

        # construct K_{xt, x} \otimes Ktt
        test_obs_kernel = KroneckerProductLazyTensor(test_data_covar, task_covar)

        # collect y - \mu(x) and \mu(X)
        train_diff = self.train_targets - self.mean_module(train_x)
        if detach_test_caches.on():
            train_diff = train_diff.detach()
        test_mean = self.mean_module(X)

        train_noise = self.likelihood._shaped_noise_covar(train_x.shape)
        diagonal_noise = isinstance(train_noise, DiagLazyTensor)
        if detach_test_caches.on():
            train_noise = train_noise.detach()
        test_noise = (
            self.likelihood._shaped_noise_covar(X.shape) if observation_noise else None
        )

        # predictive mean and variance for the mvn
        # first the predictive mean
        pred_mean = (
            test_obs_kernel.matmul(self.predictive_mean_cache).reshape_as(test_mean)
            + test_mean
        )
        # next the predictive variance, assume diagonal noise
        test_var_term = KroneckerProductLazyTensor(test_test_covar, task_covar).diag()

        if diagonal_noise:
            task_evals, task_evecs = self._task_covar_matrix.diagonalization()
            # TODO: make this be the default KPMatmulLT diagonal method in gpytorch
            full_data_inv_evals = (
                KroneckerProductDiagLazyTensor(
                    DiagLazyTensor(data_data_evals), DiagLazyTensor(task_evals)
                )
                + train_noise
            ).inverse()
            test_train_hadamard = KroneckerProductLazyTensor(
                test_data_covar.matmul(data_data_evecs).evaluate() ** 2,
                task_covar.matmul(task_evecs).evaluate() ** 2,
            )
            data_var_term = test_train_hadamard.matmul(full_data_inv_evals).sum(dim=-1)
        else:
            # if non-diagonal noise (but still kronecker structured), we have to pull
            # across the noise because the inverse is not closed form
            # should be a kronecker lt, R = \Sigma_X^{-1/2} \kron \Sigma_T^{-1/2}
            # TODO: enforce the diagonalization to return a KPLT for all shapes in
            # gpytorch or dense linear algebra for small shapes
            data_noise, task_noise = train_noise.lazy_tensors
            data_noise_root = data_noise.root_inv_decomposition(
                method="diagonalization"
            )
            task_noise_root = task_noise.root_inv_decomposition(
                method="diagonalization"
            )

            # ultimately we need to compute the diagonal of
            # (K_{x* X} \kron K_T)(K_{XX} \kron K_T + \Sigma_X \kron \Sigma_T)^{-1}
            #                           (K_{x* X} \kron K_T)^T
            # = (K_{x* X} \Sigma_X^{-1/2} Q_R)(\Lambda_R + I)^{-1}
            #                       (K_{x* X} \Sigma_X^{-1/2} Q_R)^T
            # where R = (\Sigma_X^{-1/2T}K_{XX}\Sigma_X^{-1/2} \kron
            #                   \Sigma_T^{-1/2T}K_{T}\Sigma_T^{-1/2})
            # first we construct the components of R's eigen-decomposition
            # TODO: make this be the default KPMatmulLT diagonal method in gpytorch
            whitened_data_covar = (
                data_noise_root.transpose(-1, -2)
                .matmul(data_data_covar)
                .matmul(data_noise_root)
            )
            w_data_evals, w_data_evecs = whitened_data_covar.diagonalization()
            whitened_task_covar = (
                task_noise_root.transpose(-1, -2)
                .matmul(self._task_covar_matrix)
                .matmul(task_noise_root)
            )
            w_task_evals, w_task_evecs = whitened_task_covar.diagonalization()

            # we add one to the eigenvalues as above (not just for stability)
            full_data_inv_evals = (
                KroneckerProductDiagLazyTensor(
                    DiagLazyTensor(w_data_evals), DiagLazyTensor(w_task_evals)
                )
                .add_jitter(1.0)
                .inverse()
            )

            test_data_comp = (
                test_data_covar.matmul(data_noise_root).matmul(w_data_evecs).evaluate()
                ** 2
            )
            task_comp = (
                task_covar.matmul(task_noise_root).matmul(w_task_evecs).evaluate() ** 2
            )

            test_train_hadamard = KroneckerProductLazyTensor(test_data_comp, task_comp)
            data_var_term = test_train_hadamard.matmul(full_data_inv_evals).sum(dim=-1)

        pred_variance = test_var_term - data_var_term
        specialized_mvn = MultitaskMultivariateNormal(
            pred_mean, DiagLazyTensor(pred_variance)
        )
        if observation_noise:
            specialized_mvn = self.likelihood(specialized_mvn)

        posterior = MultitaskGPPosterior(
            mvn=specialized_mvn,
            joint_covariance_matrix=joint_covar,
            test_train_covar=test_obs_kernel,
            train_diff=train_diff,
            test_mean=test_mean,
            train_train_covar=self.train_full_covar,
            train_noise=train_noise,
            test_noise=test_noise,
        )

        if hasattr(self, "outcome_transform"):
            posterior = self.outcome_transform.untransform_posterior(posterior)
        if posterior_transform is not None:
            return posterior_transform(posterior)
        else:
            return posterior

    def train(self, val=True, *args, **kwargs):
        if val:
            fixed_cache_names = ["data_data_roots", "train_full_covar", "task_root"]
            for name in fixed_cache_names:
                try:
                    pop_from_cache(self, name)
                except CachingError:
                    pass

        return super().train(val, *args, **kwargs)
