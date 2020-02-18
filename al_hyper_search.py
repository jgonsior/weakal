import argparse
import contextlib
import datetime
import io
import json
import multiprocessing
import os
import random
import sys
from itertools import chain, combinations
from timeit import default_timer as timer

import numpy as np
import pandas as pd
import peewee
from evolutionary_search import EvolutionaryAlgorithmSearchCV
from scipy.stats import randint, uniform
from sklearn.base import BaseEstimator
from sklearn.datasets import load_iris
from sklearn.model_selection import (GridSearchCV, ParameterGrid,
                                     RandomizedSearchCV, train_test_split)

from cluster_strategies import (DummyClusterStrategy,
                                MostUncertainClusterStrategy,
                                RandomClusterStrategy,
                                RoundRobinClusterStrategy)
from dataStorage import DataStorage
from experiment_setup_lib import (Logger,
                                  classification_report_and_confusion_matrix,
                                  load_and_prepare_X_and_Y, standard_config,
                                  store_pickle, store_result)
from sampling_strategies import (BoundaryPairSampler, CommitteeSampler,
                                 RandomSampler, UncertaintySampler)

standard_config = standard_config()

param_distribution = {}

standard_param_distribution = {
    "dataset_path": [standard_config.dataset_path],
    "classifier": [standard_config.classifier],
    "cores": [standard_config.cores],
    "output_dir": [standard_config.output_dir],
    "random_seed": [standard_config.random_seed],
    "test_fraction": [standard_config.test_fraction],
    "sampling": [
        'random',
        'uncertainty_lc',
        'uncertainty_max_margin',
        'uncertainty_entropy',
    ],
    "cluster": [
        'dummy', 'random', 'MostUncertain_lc', 'MostUncertain_max_margin',
        'MostUncertain_entropy'
        #  'dummy',
    ],
    "nr_learning_iterations": [1000000000],
    "nr_queries_per_iteration":
    np.random.randint(1, 2000, size=100),
    "start_set_size":
    np.random.uniform(0.01, 0.5, size=100),
    "with_uncertainty_recommendation": [False],
    "with_cluster_recommendation": [False],
    "with_snuba_lite": [False]
}

uncertainty_recommendation_grid = {
    "uncertainty_recommendation_certainty_threshold":
    np.random.uniform(0.5, 1, size=100),
    "uncertainty_recommendation_ratio": [1 / 10, 1 / 100, 1 / 1000, 1 / 10000]
}

snuba_lite_grid = {
    "snuba_lite_minimum_heuristic_accuracy": np.random.uniform(0.5,
                                                               1,
                                                               size=100)
}

cluster_recommendation_grid = {
    "cluster_recommendation_minimum_cluster_unity_size":
    np.random.uniform(0.5, 1, size=100),
    "cluster_recommendation_ratio_labeled_unlabeled":
    np.random.uniform(0.5, 1, size=100)
}

# create databases for storing the results
db = peewee.SqliteDatabase('experiment_results.db')


class BaseModel(peewee.Model):
    class Meta:
        database = db


class ExperimentResult(BaseModel):
    id_field = peewee.AutoField()

    # hyper params
    dataset_path = peewee.TextField()
    classifier = peewee.TextField()
    cores = peewee.IntegerField()
    output_dir = peewee.TextField()
    test_fraction = peewee.FloatField()
    sampling = peewee.TextField()
    random_seed = peewee.IntegerField()
    cluster = peewee.TextField()
    nr_learning_iterations = peewee.IntegerField()
    nr_queries_per_iteration = peewee.IntegerField()
    start_set_size = peewee.FloatField()
    with_uncertainty_recommendation = peewee.BooleanField()
    with_cluster_recommendation = peewee.BooleanField()
    with_snuba_lite = peewee.BooleanField()
    uncertainty_recommendation_certainty_threshold = peewee.FloatField(
        null=True)
    uncertainty_recommendation_ratio = peewee.FloatField(null=True)
    snuba_lite_minimum_heuristic_accuracy = peewee.FloatField(null=True)
    cluster_recommendation_minimum_cluster_unity_size = peewee.FloatField(
        null=True)
    cluster_recommendation_ratio_labeled_unlabeled = peewee.FloatField(
        null=True)
    metrics_per_al_cycle = peewee.TextField(null=True)  # json string
    amount_of_user_asked_queries = peewee.IntegerField(null=True)

    # information of hyperparam run
    experiment_run_date = peewee.DateTimeField(default=datetime.datetime.now)
    fit_time = peewee.TextField()  # timedelta
    confusion_matrix_test = peewee.TextField()  # json
    confusion_matrix_train = peewee.TextField()  # json
    classification_report_train = peewee.TextField()  # json
    classification_report_test = peewee.TextField()  # json
    acc_train = peewee.FloatField()
    acc_test = peewee.FloatField()


db.connect()
db.create_tables([ExperimentResult])


# generate all possible combinations of the three recommendations
def powerset(iterable):
    """
    powerset([1,2,3]) --> () (1,) (2,) (3,) (1,2) (1,3) (2,3) (1,2,3)
    """
    xs = list(iterable)
    # note we return an iterator rather than a list
    return chain.from_iterable(combinations(xs, n) for n in range(len(xs) + 1))


param_distribution_list = []

for recommendation_param_distributions in powerset([
    ("with_uncertainty_recommendation", uncertainty_recommendation_grid),
    ("with_cluster_recommendation", cluster_recommendation_grid)
]):
    param_distribution = {**standard_param_distribution}
    for recommendation_param_distribution in recommendation_param_distributions:
        param_distribution = {
            **param_distribution,
            **recommendation_param_distribution[1]
        }
        param_distribution[recommendation_param_distribution[0]] = [True]
        param_distribution[
            "minimum_test_accuracy_before_recommendations"] = np.random.uniform(
                0.5, 1, size=100)
    if 'minimum_test_accuracy_before_recommendations' not in param_distribution.keys(
    ):
        param_distribution['minimum_test_accuracy_before_recommendations'] = [
            1
        ]
    param_distribution_list.append(param_distribution)

# create estimater object


class Estimator(BaseEstimator):
    def __init__(self,
                 dataset_path=None,
                 classifier=None,
                 cores=None,
                 output_dir=None,
                 random_seed=None,
                 test_fraction=None,
                 sampling=None,
                 cluster=None,
                 nr_learning_iterations=None,
                 nr_queries_per_iteration=None,
                 start_set_size=None,
                 minimum_test_accuracy_before_recommendations=None,
                 uncertainty_recommendation_certainty_threshold=None,
                 uncertainty_recommendation_ratio=None,
                 snuba_lite_minimum_heuristic_accuracy=None,
                 cluster_recommendation_minimum_cluster_unity_size=None,
                 cluster_recommendation_ratio_labeled_unlabeled=None,
                 with_uncertainty_recommendation=None,
                 with_cluster_recommendation=None,
                 with_snuba_lite=None,
                 plot=None):
        self.dataset_path = dataset_path
        self.classifier = classifier
        self.cores = cores
        self.output_dir = output_dir
        self.random_seed = random_seed
        self.test_fraction = test_fraction
        self.sampling = sampling
        self.cluster = cluster
        self.nr_learning_iterations = nr_learning_iterations
        self.nr_queries_per_iteration = nr_queries_per_iteration
        self.start_set_size = start_set_size
        self.minimum_test_accuracy_before_recommendations = minimum_test_accuracy_before_recommendations
        self.uncertainty_recommendation_certainty_threshold = uncertainty_recommendation_certainty_threshold
        self.uncertainty_recommendation_ratio = uncertainty_recommendation_ratio
        self.snuba_lite_minimum_heuristic_accuracy = snuba_lite_minimum_heuristic_accuracy
        self.cluster_recommendation_minimum_cluster_unity_size = cluster_recommendation_minimum_cluster_unity_size
        self.cluster_recommendation_ratio_labeled_unlabeled = cluster_recommendation_ratio_labeled_unlabeled
        self.with_uncertainty_recommendation = with_uncertainty_recommendation
        self.with_cluster_recommendation = with_cluster_recommendation
        self.with_snuba_lite = with_snuba_lite
        self.plot = plot

        if with_snuba_lite or with_cluster_recommendation or with_uncertainty_recommendation:
            if minimum_test_accuracy_before_recommendations is None:
                print("oh mein gott")

        self.dataset_storage = DataStorage(random_seed)

    def fit(self, X, Y, **kwargs):
        self.dataset_storage.load_csv(self.dataset_path)
        self.dataset_storage.divide_data(self.test_fraction,
                                         self.start_set_size)

        if self.sampling == 'random':
            active_learner = RandomSampler(self.random_seed, self.cores,
                                           self.nr_learning_iterations,
                                           self.nr_queries_per_iteration)
        elif self.sampling == 'boundary':
            active_learner = BoundaryPairSampler(self.random_seed, self.cores,
                                                 self.nr_learning_iterations,
                                                 self.nr_queries_per_iteration)
        elif self.sampling == 'uncertainty_lc':
            active_learner = UncertaintySampler(self.random_seed, self.cores,
                                                self.nr_learning_iterations,
                                                self.nr_queries_per_iteration)
            active_learner.set_uncertainty_strategy('least_confident')
        elif self.sampling == 'uncertainty_max_margin':
            active_learner = UncertaintySampler(self.random_seed, self.cores,
                                                self.nr_learning_iterations,
                                                self.nr_queries_per_iteration)
            active_learner.set_uncertainty_strategy('max_margin')
        elif self.sampling == 'uncertainty_entropy':
            active_learner = UncertaintySampler(self.random_seed, self.cores,
                                                self.nr_learning_iterations,
                                                self.nr_queries_per_iteration)
            active_learner.set_uncertainty_strategy('entropy')
        #  elif self.sampling == 'committee':
        #  active_learner = CommitteeSampler(self.random_seed, self.cores, self.nr_learning_iterations)
        else:
            print("No Active Learning Strategy specified")

        if self.cluster == 'dummy':
            cluster_strategy = DummyClusterStrategy()
        elif self.cluster == 'random':
            cluster_strategy = RandomClusterStrategy()
        elif self.cluster == "MostUncertain_lc":
            cluster_strategy = MostUncertainClusterStrategy()
            cluster_strategy.set_uncertainty_strategy('least_confident')
        elif self.cluster == "MostUncertain_max_margin":
            cluster_strategy = MostUncertainClusterStrategy()
            cluster_strategy.set_uncertainty_strategy('max_margin')
        elif self.cluster == "MostUncertain_entropy":
            cluster_strategy = MostUncertainClusterStrategy()
            cluster_strategy.set_uncertainty_strategy('entropy')
        elif self.cluster == 'RoundRobin':
            cluster_strategy = RoundRobinClusterStrategy()

        active_learner.set_data_storage(self.dataset_storage)
        cluster_strategy.set_data_storage(self.dataset_storage)
        active_learner.set_cluster_strategy(cluster_strategy)

        start = timer()
        trained_active_clf_list, metrics_per_al_cycle = active_learner.learn(
            self.minimum_test_accuracy_before_recommendations,
            self.with_cluster_recommendation,
            self.with_uncertainty_recommendation, self.with_snuba_lite,
            self.cluster_recommendation_minimum_cluster_unity_size,
            self.cluster_recommendation_ratio_labeled_unlabeled,
            self.uncertainty_recommendation_certainty_threshold,
            self.uncertainty_recommendation_ratio,
            self.snuba_lite_minimum_heuristic_accuracy)
        end = timer()

        # display quick results
        self.amount_of_user_asked_queries = active_learner.get_amount_of_user_asked_queries(
        )

        classification_report_and_confusion_matrix_test = classification_report_and_confusion_matrix(
            trained_active_clf_list[0], self.dataset_storage.X_test,
            self.dataset_storage.Y_test, self.dataset_storage.label_encoder)
        classification_report_and_confusion_matrix_train = classification_report_and_confusion_matrix(
            trained_active_clf_list[0], self.dataset_storage.X_train_unlabeled,
            self.dataset_storage.Y_train_unlabeled,
            self.dataset_storage.label_encoder)

        experiment_result = ExperimentResult(
            **self.get_params(),
            amount_of_user_asked_queries=self.amount_of_user_asked_queries,
            metrics_per_al_cycle=metrics_per_al_cycle,
            fit_time=str(end - start),
            confusion_matrix_test=
            classification_report_and_confusion_matrix_test[1],
            confusion_matrix_train=
            classification_report_and_confusion_matrix_train[1],
            classification_report_test=
            classification_report_and_confusion_matrix_test[0],
            classification_report_train=
            classification_report_and_confusion_matrix_train[0],
            acc_train=classification_report_and_confusion_matrix_train[0]
            ['accuracy'],
            acc_test=classification_report_and_confusion_matrix_test[0]
            ['accuracy'])
        experiment_result.save()

    def score(self, X, y):
        return self.amount_of_user_asked_queries


with Logger(
        standard_config.output_dir + "/" + str(datetime.datetime.now()) +
        "al_hyper_search.txt", "w"):
    active_learner = Estimator()

    #  grid = RandomizedSearchCV(active_learner,
    #  param_distribution_list,
    #  n_iter=3,
    #  cv=2,
    #  verbose=9999999999999999999999999999999999)

    evolutionary_search = EvolutionaryAlgorithmSearchCV(
        estimator=active_learner,
        params=param_distribution_list,
        verbose=True,
        cv=2,
        population_size=50,
        gene_mutation_prob=0.10,
        tournament_size=3,
        generations_number=10,
        n_jobs=multiprocessing.cpu_count())

    # @todo: remove cross validation
    iris = load_iris()

    #  search = grid.fit(iris.data, iris.target)
    evolutionary_search.fit(iris.data, iris.target)

    print(evolutionary_search.best_params_)
    print(evolutionary_search.best_score_)
    print(
        pd.DataFrame(evolutionary_search.cv_results_).sort_values(
            "mean_test_score", ascending=False).head())