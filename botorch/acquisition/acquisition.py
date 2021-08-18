#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

r"""
Abstract base module for all botorch acquisition functions.
"""

from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from typing import Optional, Callable

from botorch.exceptions import BotorchWarning, UnsupportedError
from botorch.models.model import Model
from botorch.posteriors.posterior import Posterior
from torch import Tensor
from torch.nn import Module


class AcquisitionFunction(Module, ABC):
    r"""Abstract base class for acquisition functions."""

    def __init__(self, model: Model) -> None:
        r"""Constructor for the AcquisitionFunction base class.

        Args:
            model: A fitted model.
        """
        super().__init__()
        self.add_module("model", model)

    def _deprecate_acqf_objective(
        self,
        posterior_transform: Optional[Callable[[Posterior], Posterior]],
        objective: Optional[Module],
    ) -> Optional[Callable[[Posterior], Posterior]]:
        from botorch.acquisition.objective import (
            ScalarizedObjective,
            ScalarizedPosteriorTransform,
        )

        if objective is None:
            return posterior_transform
        warnings.warn(
            f"{self.__class__.__name__} got a non-MC `objective`. The non-MC "
            "AcquisitionObjectives and the `objective` argument to"
            "AnalyticAcquisitionFunctions are DEPRECATED and will be removed in the"
            "next version. Use `posterior_transform` instead.",
            DeprecationWarning,
        )
        if not isinstance(objective, ScalarizedObjective):
            raise UnsupportedError(
                f"{self.__class__.__name__} only supports ScalarizedObjective "
                "(DEPRECATED) type objectives."
            )
        return ScalarizedPosteriorTransform(
            weights=objective.weights, offset=objective.offset
        )

    def set_X_pending(self, X_pending: Optional[Tensor] = None) -> None:
        r"""Informs the acquisition function about pending design points.

        Args:
            X_pending: `n x d` Tensor with `n` `d`-dim design points that have
                been submitted for evaluation but have not yet been evaluated.
        """
        if X_pending is not None:
            if X_pending.requires_grad:
                warnings.warn(
                    "Pending points require a gradient but the acquisition function"
                    " will not provide a gradient to these points.",
                    BotorchWarning,
                )
            self.X_pending = X_pending.detach().clone()
        else:
            self.X_pending = X_pending

    @abstractmethod
    def forward(self, X: Tensor) -> Tensor:
        r"""Evaluate the acquisition function on the candidate set X.

        Args:
            X: A `(b) x q x d`-dim Tensor of `(b)` t-batches with `q` `d`-dim
                design points each.

        Returns:
            A `(b)`-dim Tensor of acquisition function values at the given
            design points `X`.
        """
        pass  # pragma: no cover


class OneShotAcquisitionFunction(AcquisitionFunction, ABC):
    r"""Abstract base class for acquisition functions using one-shot optimization"""

    @abstractmethod
    def get_augmented_q_batch_size(self, q: int) -> int:
        r"""Get augmented q batch size for one-shot optimziation.

        Args:
            q: The number of candidates to consider jointly.

        Returns:
            The augmented size for one-shot optimization (including variables
            parameterizing the fantasy solutions).
        """
        pass  # pragma: no cover

    @abstractmethod
    def extract_candidates(self, X_full: Tensor) -> Tensor:
        r"""Extract the candidates from a full "one-shot" parameterization.

        Args:
            X_full: A `b x q_aug x d`-dim Tensor with `b` t-batches of `q_aug`
                design points each.

        Returns:
            A `b x q x d`-dim Tensor with `b` t-batches of `q` design points each.
        """
        pass  # pragma: no cover
