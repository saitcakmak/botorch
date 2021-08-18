#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import torch
from botorch.acquisition.objective import ScalarizedPosteriorTransform
from botorch.exceptions.errors import UnsupportedError
from botorch.models.deterministic import (
    AffineDeterministicModel,
    DeterministicModel,
    GenericDeterministicModel,
)
from botorch.models.transforms.input import Normalize
from botorch.models.transforms.outcome import Standardize
from botorch.posteriors.deterministic import DeterministicPosterior
from botorch.utils.testing import BotorchTestCase


class DummyDeterministicModel(DeterministicModel):
    r"""A dummy deterministic model that uses transforms."""

    def __init__(self, outcome_transform, input_transform):
        super().__init__()
        self.input_transform = input_transform
        self.outcome_transform = outcome_transform

    def forward(self, X):
        # just a non-linear objective that is sure to break without transforms
        return (X - 1.0).pow(2).sum(dim=-1, keepdim=True) - 5.0


class TestDeterministicModels(BotorchTestCase):
    def test_abstract_base_model(self):
        with self.assertRaises(TypeError):
            DeterministicModel()

    def test_GenericDeterministicModel(self):
        def f(X):
            return X.mean(dim=-1, keepdim=True)

        model = GenericDeterministicModel(f)
        self.assertEqual(model.num_outputs, 1)
        X = torch.rand(3, 2)
        # basic test
        p = model.posterior(X)
        self.assertIsInstance(p, DeterministicPosterior)
        self.assertTrue(torch.equal(p.mean, f(X)))
        # check that w/ observation noise this errors properly
        with self.assertRaises(UnsupportedError):
            model.posterior(X, observation_noise=True)
        # check output indices
        model = GenericDeterministicModel(lambda X: X, num_outputs=2)
        self.assertEqual(model.num_outputs, 2)
        p = model.posterior(X, output_indices=[0])
        self.assertTrue(torch.equal(p.mean, X[..., [0]]))
        # test subset output
        subset_model = model.subset_output([0])
        self.assertIsInstance(subset_model, GenericDeterministicModel)
        p_sub = subset_model.posterior(X)
        self.assertTrue(torch.equal(p_sub.mean, X[..., [0]]))

    def test_AffineDeterministicModel(self):
        # test error on bad shape of a
        with self.assertRaises(ValueError):
            AffineDeterministicModel(torch.rand(2))
        # test error on bad shape of b
        with self.assertRaises(ValueError):
            AffineDeterministicModel(torch.rand(2, 1), torch.rand(2, 1))
        # test one-dim output
        a = torch.rand(3, 1)
        model = AffineDeterministicModel(a)
        self.assertEqual(model.num_outputs, 1)
        for shape in ((4, 3), (1, 4, 3)):
            X = torch.rand(*shape)
            p = model.posterior(X)
            mean_exp = model.b + (X.unsqueeze(-1) * a).sum(dim=-2)
            self.assertTrue(torch.equal(p.mean, mean_exp))
        # # test two-dim output
        a = torch.rand(3, 2)
        model = AffineDeterministicModel(a)
        self.assertEqual(model.num_outputs, 2)
        for shape in ((4, 3), (1, 4, 3)):
            X = torch.rand(*shape)
            p = model.posterior(X)
            mean_exp = model.b + (X.unsqueeze(-1) * a).sum(dim=-2)
            self.assertTrue(torch.equal(p.mean, mean_exp))
        # test subset output
        X = torch.rand(4, 3)
        subset_model = model.subset_output([0])
        self.assertIsInstance(subset_model, AffineDeterministicModel)
        p = model.posterior(X)
        p_sub = subset_model.posterior(X)
        self.assertTrue(torch.equal(p_sub.mean, p.mean[..., [0]]))

    def test_with_transforms(self):
        dim = 2
        bounds = torch.stack([torch.zeros(dim), torch.ones(dim) * 3])
        intf = Normalize(d=dim, bounds=bounds)
        octf = Standardize(m=1)
        # update octf state with dummy data
        octf(torch.rand(5, 1) * 7)
        octf.eval()
        model = DummyDeterministicModel(octf, intf)
        # check that the posterior output agrees with the manually transformed one
        test_X = torch.rand(3, dim)
        expected_Y, _ = octf.untransform(model.forward(intf(test_X)))
        posterior = model.posterior(test_X)
        self.assertTrue(torch.allclose(expected_Y, posterior.mean))

    def test_posterior_transform(self):
        def f(X):
            return X

        model = GenericDeterministicModel(f)
        test_X = torch.rand(3, 2)
        post_tf = ScalarizedPosteriorTransform(weights=torch.rand(2))
        # expect error due to post_tf expecting an MVN
        with self.assertRaises(AttributeError):
            model.posterior(test_X, posterior_transform=post_tf)
