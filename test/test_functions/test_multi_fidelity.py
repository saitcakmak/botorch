#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from itertools import product

import torch
from botorch.test_functions.multi_fidelity import (
    AugmentedBranin,
    AugmentedHartmann,
    AugmentedRosenbrock,
    BoreholeMultiFidelity,
    WingWeightMultiFidelity,
)
from botorch.utils.testing import (
    BaseTestProblemTestCaseMixIn,
    BotorchTestCase,
    SyntheticTestFunctionTestCaseMixin,
)


class TestAugmentedBranin(
    BotorchTestCase, BaseTestProblemTestCaseMixIn, SyntheticTestFunctionTestCaseMixin
):
    functions = [
        AugmentedBranin(),
        AugmentedBranin(negate=True),
        AugmentedBranin(noise_std=0.1),
    ]


class TestAugmentedHartmann(
    BotorchTestCase, BaseTestProblemTestCaseMixIn, SyntheticTestFunctionTestCaseMixin
):
    functions = [
        AugmentedHartmann(),
        AugmentedHartmann(negate=True),
        AugmentedHartmann(noise_std=0.1),
    ]


class TestAugmentedRosenbrock(
    BotorchTestCase, BaseTestProblemTestCaseMixIn, SyntheticTestFunctionTestCaseMixin
):
    functions = [
        AugmentedRosenbrock(),
        AugmentedRosenbrock(negate=True),
        AugmentedRosenbrock(noise_std=0.1),
        AugmentedRosenbrock(dim=4),
        AugmentedRosenbrock(dim=4, negate=True),
        AugmentedRosenbrock(dim=4, noise_std=0.1),
    ]

    def test_min_dimension(self):
        with self.assertRaises(ValueError):
            AugmentedRosenbrock(dim=2)


class TestDiscreteMultiFidelity(
    BotorchTestCase, BaseTestProblemTestCaseMixIn, SyntheticTestFunctionTestCaseMixin
):
    functions = [WingWeightMultiFidelity(), BoreholeMultiFidelity()]

    def test_optimizer(self):
        pass

    def test_each_fidelity_and_cost(self):
        dtypes = (torch.float, torch.double)
        batch_shapes = (torch.Size(), torch.Size([2]), torch.Size([2, 3]))
        for dtype, batch_shape, f in product(dtypes, batch_shapes, self.functions):
            f.to(device=self.device, dtype=dtype)
            X = torch.rand(*batch_shape, f.dim, device=self.device, dtype=dtype)
            X = f.bounds[0] + X * (f.bounds[1] - f.bounds[0])
            for fidelity in f.fidelities:
                if X.ndim == 1:
                    X[-1] = fidelity
                else:
                    # only change one fidelity value to test that the masking in
                    # evaluate_true still yields the expected shapes
                    X[..., 0, -1] = fidelity
                res_forward = f(X)
                res_evaluate_true = f.evaluate_true(X)
                res_cost = f.cost(X)
                for method, res in {
                    "forward": res_forward,
                    "evaluate_true": res_evaluate_true,
                    "cost": res_cost,
                }.items():
                    with self.subTest(
                        f"{dtype}_{batch_shape}_{f.__class__.__name__}_{method}"
                        f"_{fidelity}"
                    ):
                        self.assertEqual(res.dtype, dtype)
                        self.assertEqual(res.device.type, self.device.type)
                        tail_shape = torch.Size(
                            [f.num_objectives] if f.num_objectives > 1 else []
                        )
                        self.assertEqual(res.shape, batch_shape + tail_shape)
